from __future__ import annotations
import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time

from xrandrw import dwmipc
from xrandrw.config import load_config
from xrandrw.logging_utils import _setup_logging, logev, wait_for_x
from xrandrw.xrandr import read_xrandr, read_edids
from xrandrw.state import load_state, save_state, ensure_profile, get_profile, state_lock
from xrandrw.apply import apply_once, _sd_notify, _watchdog_thread
from xrandrw.watch import stop_evt, watch_loop, _install_signals
from xrandrw.relocate import RelocationCoordinator
from xrandrw.windows import capture_windows

SIDES_VALID = ("right-of", "left-of", "above", "below")

# WR-03 boot-seed retry budget: dwm-ipc may not be up yet when the daemon boots
# (e.g. dwm still starting). We wait a bounded time for the endpoint before
# seeding so the seed captures the FULL pre-unplug topology; never an infinite
# wait (mirrors wait_for_x's bounded poll).
_SEED_RETRIES = 20      # ~10s total
_SEED_DELAY = 0.5       # seconds between availability probes
# Modest per-call dwm-ipc timeout for the relocation coordinator (IN-01): keeps
# each synchronous restore round-trip short so it cannot stall the watch loop.
_RELOCATE_IPC_TIMEOUT = 0.25

def set_pref(env: dict[str, str], output_or_id: str, side: str, logger: logging.Logger):
    if side not in SIDES_VALID:
        raise SystemExit(f"invalid side: {side} (valid: {', '.join(SIDES_VALID)})")
    outs = read_xrandr(logger)
    read_edids(outs, logger)
    # HARD-03/D-03a: serialize the RMW against apply_once on the SHARED state-lock only.
    # set_pref must NOT touch the apply-lock (env["LOCKFILE"]) — state-lock-only keeps the
    # two-lock system acyclic (no process waits for the apply-lock while holding the state-lock).
    with state_lock(env["STATE_LOCKFILE"]):
        st = load_state()
        matched: list[str] = []
        for o in outs.values():
            if not o.connected:
                continue
            pid = ensure_profile(o, st, logger, env["PREF_DEFAULT_SIDE"])
            if o.name == output_or_id or ("edid:"+o.edid_sha1 == output_or_id if o.edid_sha1 else False) or ("conn:"+o.name == output_or_id):
                get_profile(st, pid)["preferred_side"] = side
                matched.append(pid)
        if not matched and output_or_id in st.get("profiles", {}):
            get_profile(st, output_or_id)["preferred_side"] = side
            matched.append(output_or_id)
        if not matched:
            raise SystemExit(f"no such connected output or known id/profile: {output_or_id}")
        save_state(st)
    logev(logger, logging.INFO, "set_pref", "preferred side updated", side=side, profiles=",".join(matched))

def list_state():
    st = load_state()
    print(json.dumps(st, indent=2, sort_keys=True))

def window_state(env: dict[str, str], logger: logging.Logger) -> int:
    # WM-07 SC3: read-only diagnostic of the window-management feature state.
    # Degrades cleanly on every path (off / no-endpoint / available) and always
    # exits 0 -- never crashes. `displaced` is always [] here: this one-shot has
    # no live coordinator, so there is no displaced set to report; the key stays
    # present for a stable schema.
    enabled = env.get("WINDOW_MANAGEMENT") == "1"
    dwmipc_available = dwmipc.available(dwmipc.DEFAULT_SOCK_PATH, timeout=_RELOCATE_IPC_TIMEOUT)
    result = {
        "enabled": enabled,
        "dwmipc_available": dwmipc_available,
        "captured": [],
        "displaced": [],
    }
    if not enabled:
        result["reason"] = "window management disabled; set WINDOW_MANAGEMENT=1 to enable"
    elif not dwmipc_available:
        result["reason"] = ("dwm-ipc endpoint unavailable; needs a dwm built with the "
                            "mihirlad55/dwm-ipc patch exposing /tmp/dwm.sock")
    else:
        try:
            recs = capture_windows(timeout=_RELOCATE_IPC_TIMEOUT, logger=logger)
            result["captured"] = [r.to_dict() for r in recs]
        except Exception as e:
            # capture_windows already swallows DwmIpcUnavailable and guards its
            # live X read; wrap defensively so any unexpected error still exits 0.
            logev(logger, logging.WARNING, "window_state_capture_fail",
                  "window-state capture failed; leaving captured empty", error=str(e))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0

def _event_source_from_env() -> str:
    if os.getenv("ACTION") or os.getenv("OUTPUT"):
        return "xplugd"
    return "manual"

def _seeded_coordinator(env: dict[str, str], logger: logging.Logger,
                        *, retries: int = _SEED_RETRIES,
                        delay: float = _SEED_DELAY) -> RelocationCoordinator:
    # BOOT-SEED (B2): construct the relocation coordinator and seed it ONCE while
    # all outputs are still connected (after boot apply, before watch_loop). The
    # watch hook fires only AFTER a topology CHANGE settles, so without this seed
    # the FIRST unplug of the session takes on_settled's `_prev_connected is None`
    # branch (seed + return, no `removed`), silently missing the first unplug so
    # replug restores nothing. Seeding here populates _prev_connected + _snapshot
    # with the correct pre-unplug placement.
    # WM-07: the feature is opt-in. config_enabled ANDs with dwmipc.available()
    # inside the coordinator's _enabled() gate, so config-off => the whole
    # lifecycle is a silent no-op with display layout untouched.
    config_enabled = env.get("WINDOW_MANAGEMENT") == "1"
    coordinator = RelocationCoordinator(config_enabled=config_enabled,
                                        ipc_timeout=_RELOCATE_IPC_TIMEOUT)
    # WR-03: if dwm-ipc isn't up yet at boot, on_settled is a silent no-op
    # (_enabled() False) and _prev_connected stays None -- a LATER settle would
    # then seed off an ALREADY-REDUCED topology, silently reintroducing the B2
    # first-unplug miss. Wait a BOUNDED time for the endpoint before seeding.
    # WM-07 lean-boot: only run the wait when the feature is enabled. A disabled
    # coordinator's on_settled is already a no-op, so polling ~retries*delay (~10s)
    # for an endpoint that will never be used would add a pointless multi-second
    # boot delay on the common default-off machine with no dwm-ipc (e.g. RPi4
    # vanilla dwm), violating the project's lean-boot goal.
    if config_enabled:
        for _ in range(max(1, retries)):
            if dwmipc.available(dwmipc.DEFAULT_SOCK_PATH, timeout=_RELOCATE_IPC_TIMEOUT):
                break
            if stop_evt.is_set():
                break
            time.sleep(delay)
    try:
        coordinator.on_settled(env, logger)
    except Exception as e:
        logev(logger, logging.WARNING, "relocate_seed_fail",
              "relocation seed failed; continuing without a seeded baseline", error=str(e))
    if coordinator._prev_connected is None:
        # dwm-ipc never came up (or feature disabled): accept and continue. The
        # first unplug this session may not be restorable, but display layout is
        # unaffected and later cycles still self-heal once dwm-ipc is present.
        logev(logger, logging.INFO, "relocate_seed_deferred",
              "dwm-ipc unavailable at boot; seed deferred (first unplug may not restore)")
    return coordinator

def main():
    env, cfg_warnings = load_config()
    logger = _setup_logging(env)
    # D-05: load_config runs before logging exists, so it defers coercion warnings; replay them now.
    for w in cfg_warnings:
        logev(logger, logging.WARNING, "config_coerce_fallback", "config value fell back to default", detail=w)
    _install_signals(logger)

    ap = argparse.ArgumentParser(description="xrandrw: robust display policy manager")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply once (default)")
    g.add_argument("--watch", action="store_true", help="poll topology and apply on change")
    g.add_argument("--daemon", action="store_true", help="event-driven watch + apply on hotplug")
    g.add_argument("--print", action="store_true", help="print xrandr --query and exit")
    ap.add_argument("--set-pref", nargs=2, metavar=("OUTPUT_OR_ID", "SIDE"),
                    help="set preferred side: right-of|left-of|above|below")
    ap.add_argument("--list-state", action="store_true", help="dump placement state JSON")
    ap.add_argument("--window-state", action="store_true",
                    help="print a JSON diagnostic of the window-mgmt feature state")
    args, extra = ap.parse_known_args()
    if extra:
        logev(logger, logging.DEBUG, "cli_extra", "ignoring extra CLI args",
              extra=" ".join(extra))

    if args.print:
        subprocess.run(["xrandr", "--query"])
        return 0
    if args.list_state:
        list_state()
        return 0
    if args.window_state:
        return window_state(env, logger)
    if args.set_pref:
        set_pref(env, args.set_pref[0], args.set_pref[1], logger)
        return 0

    if args.daemon:
        logev(logger, logging.INFO, "daemon_start", "daemon: start",
              log_level=env["LOG_LEVEL"], wall=env["WALL"])
        wait_for_x(logger)
        wd_thread = threading.Thread(target=_watchdog_thread, args=(stop_evt, logger), daemon=True)
        wd_thread.start()
        try:
            apply_once(env, logger, event_source="daemon_boot")
            _sd_notify("READY=1")  # harmless if Type=simple
            coordinator = _seeded_coordinator(env, logger)
            watch_loop(env, logger, coordinator=coordinator)
        finally:
            stop_evt.set()
        return 0

    if args.watch:
        wait_for_x(logger)
        # Initial apply so starting with already-plugged displays is handled
        apply_once(env, logger, event_source="watch_boot")
        coordinator = _seeded_coordinator(env, logger)
        wd_thread = threading.Thread(target=_watchdog_thread, args=(stop_evt, logger), daemon=True)
        wd_thread.start()
        try:
            watch_loop(env, logger, coordinator=coordinator)
        finally:
            stop_evt.set()
        return 0

    src = _event_source_from_env()
    apply_once(env, logger, event_source=src)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _sd_notify("STOPPING=1")
        sys.exit(130)
