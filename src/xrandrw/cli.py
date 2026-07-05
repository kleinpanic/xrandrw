from __future__ import annotations
import argparse, json, logging, os, subprocess, sys, threading
from typing import Dict, List

from xrandrw.config import load_config
from xrandrw.logging_utils import _setup_logging, logev, wait_for_x
from xrandrw.xrandr import read_xrandr, read_edids
from xrandrw.state import load_state, save_state, ensure_profile, get_profile
from xrandrw.apply import apply_once, _sd_notify, _watchdog_thread
from xrandrw.watch import stop_evt, watch_loop, spawn_xplugd, _install_signals

SIDES_VALID = ("right-of", "left-of", "above", "below")

def set_pref(env: Dict[str, str], output_or_id: str, side: str, logger: logging.Logger):
    if side not in SIDES_VALID:
        raise SystemExit(f"invalid side: {side} (valid: {', '.join(SIDES_VALID)})")
    outs = read_xrandr(logger)
    read_edids(outs, logger)
    st = load_state()
    matched: List[str] = []
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

def _event_source_from_env() -> str:
    if os.getenv("ACTION") or os.getenv("OUTPUT"):
        return "xplugd"
    return "manual"

def main():
    env = load_config()
    logger = _setup_logging(env)
    _install_signals(logger)

    ap = argparse.ArgumentParser(description="xrandrw: robust display policy manager")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply once (default)")
    g.add_argument("--watch", action="store_true", help="poll topology and apply on change")
    g.add_argument("--daemon", action="store_true", help="spawn xplugd (if present) and watch")
    g.add_argument("--print", action="store_true", help="print xrandr --query and exit")
    ap.add_argument("--set-pref", nargs=2, metavar=("OUTPUT_OR_ID", "SIDE"),
                    help="set preferred side: right-of|left-of|above|below")
    ap.add_argument("--list-state", action="store_true", help="dump placement state JSON")
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
    if args.set_pref:
        set_pref(env, args.set_pref[0], args.set_pref[1], logger)
        return 0

    if args.daemon:
        logev(logger, logging.INFO, "daemon_start", "daemon: start",
              log_level=env["LOG_LEVEL"], wall=env["WALL"])
        wait_for_x(logger)
        child = spawn_xplugd(logger)
        wd_thread = threading.Thread(target=_watchdog_thread, args=(stop_evt, logger), daemon=True)
        wd_thread.start()
        try:
            apply_once(env, logger, event_source="daemon_boot")
            _sd_notify("READY=1")  # harmless if Type=simple
            watch_loop(env, logger)
        finally:
            if child and child.poll() is None:
                child.terminate()
            stop_evt.set()
        return 0

    if args.watch:
        wait_for_x(logger)
        # Initial apply so starting with already-plugged displays is handled
        apply_once(env, logger, event_source="watch_boot")
        wd_thread = threading.Thread(target=_watchdog_thread, args=(stop_evt, logger), daemon=True)
        wd_thread.start()
        try:
            watch_loop(env, logger)
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
