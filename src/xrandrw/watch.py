from __future__ import annotations
import logging
import os
import select
import signal
import threading
import time

from Xlib import display
from Xlib.ext import randr

from xrandrw.logging_utils import logev
from xrandrw.xrandr import topology_hash
from xrandrw.apply import apply_once, _sd_notify

stop_evt = threading.Event()

def _install_signals(logger: logging.Logger):
    def _sig(sig, frame):
        logev(logger, logging.INFO, "shutdown", "signal received", sig=sig)
        stop_evt.set()
        _sd_notify("STOPPING=1")
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

def _apply_if_changed(env: dict[str, str], logger: logging.Logger,
                      last_hash: str, churn: dict, got_event: bool, coordinator=None) -> str:
    # Decision logic isolated from the blocking select() so it is unit-testable
    # with topology_hash/apply_once mocked and no live Display.
    cur = topology_hash(logger)
    if cur == last_hash:
        return last_hash
    now = time.monotonic()
    churn["times"].append(now)
    churn["times"] = [t for t in churn["times"] if now - t <= churn["window"]]
    if len(churn["times"]) > churn["threshold"]:
        logev(logger, logging.WARNING, "watch_excess", "excess topology churn",
              count=len(churn["times"]), window=churn["window"])
        churn["backoff"] = min(1000, churn["backoff"] + 150)
    else:
        churn["backoff"] = max(0, churn["backoff"] - 50)
    logev(logger, logging.DEBUG, "watch_change", "topology hash changed",
          debounce_ms=150 + churn["backoff"])
    # Debounce a burst: one physical plug emits Crtc+Output+ScreenChange (Pitfall 6).
    time.sleep((150 + churn["backoff"]) / 1000.0)
    verify = topology_hash(logger)
    if verify == last_hash:
        return last_hash
    src = "randr_event" if got_event else "slow_poll"
    logev(logger, logging.INFO, "watch_apply", "apply on topology change", source=src)
    applied = apply_once(env, logger, event_source=src)
    if not applied:
        # BL-01: apply_once BAILED (lock refused / another apply running / either xrandr
        # read failed). The topology is UNKNOWN and certainly unhealed -- in particular a
        # read-#2 bail returns above scrub_stale, so a disconnected-but-lit head is still
        # powered on. Absorbing the new hash here would freeze change detection on that
        # state forever (no further apply until another physical event = a phantom dwm
        # monitor that never goes away). Returning the OLD hash means the next wakeup
        # still sees a difference and retries.
        #
        # We also do NOT run the settle hook (WR-01). With CRTC-liveness `cur`, a
        # transiently dark read is a `removed` edge; on a bail that edge is an artefact of
        # an observation we could not confirm. A spurious `removed` records windows dwm
        # never evacuated, and the next settle sees `returned` and runs _restore_returned
        # -- whose plan_restore UNCONDITIONALLY re-emits the saved tag bitmask, silently
        # resetting a tag the user changed since the snapshot. Never mutate window state
        # off an unconfirmed observation.
        logev(logger, logging.INFO, "watch_apply_incomplete",
              "apply did not complete; not absorbing topology hash", source=src)
        return last_hash
    # Absorb our own mutations: the apply's xrandr commands emit RandR events; re-read the
    # settled topology so the loop doesn't chase its own change into a redundant 2nd apply.
    settled = topology_hash(logger)
    # Phase-10 additive hook (WM-06/WM-08): the relocation coordinator runs ONLY on this
    # post-apply branch, AFTER the settled hash is frozen. It must NOT alter the returned
    # hash or add a second topology read (preserves the no-double-apply invariant), and a
    # coordinator fault must never break the watch loop -- guarded + swallowed. Default
    # coordinator=None keeps the loop byte-for-byte identical for existing callers/tests.
    if coordinator is not None:
        try:
            # WR-01: thread the shutdown flag so a SIGTERM during the synchronous
            # restore cycle bails promptly instead of masking behind a slow dwm.
            coordinator.on_settled(env, logger, stop_evt=stop_evt)
        except Exception as e:
            logev(logger, logging.WARNING, "relocate_hook_fail",
                  "relocation hook raised; ignoring (display layout unaffected)", error=str(e))
    return settled

def watch_loop(env: dict[str, str], logger: logging.Logger, coordinator=None):
    slow_poll = int(env["POLL_INTERVAL"])  # D-06: safety-net timeout, not a tight loop
    churn = {
        "times": [],
        "backoff": 0,
        "window": int(env["EXCESS_WINDOW_SEC"]),
        "threshold": int(env["EXCESS_THRESHOLD"]),
    }
    try:
        d = display.Display()
    except Exception as e:
        # Graceful-degrade: a failed connect logs and returns (systemd Restart re-runs us).
        logev(logger, logging.ERROR, "xlib_connect_fail", "cannot open X display for watch", error=str(e))
        return
    root = d.screen().root
    ver = d.xrandr_query_version()
    # Pitfall 5: randr.init only wires RRNotify subevents on server RandR >= 1.5.
    events_ok = (ver.major_version, ver.minor_version) >= (1, 5)
    if events_ok:
        mask = (randr.RRScreenChangeNotifyMask
                | randr.RROutputChangeNotifyMask
                | randr.RRCrtcChangeNotifyMask)
        root.xrandr_select_input(mask)
        d.flush()
    else:
        logev(logger, logging.WARNING, "watch_degrade",
              "RandR < 1.5: event registration unavailable, slow-poll only",
              version=f"{ver.major_version}.{ver.minor_version}")
    xfd = d.fileno()
    # D-05/Pitfall 3: signal.set_wakeup_fd writes signo to wpipe on delivery, waking
    # select() instantly so SIGTERM does not wait out the slow-poll timeout.
    rpipe, wpipe = os.pipe()
    os.set_blocking(rpipe, False)
    os.set_blocking(wpipe, False)
    old_wakeup = signal.set_wakeup_fd(wpipe)
    last = topology_hash(logger)
    logev(logger, logging.INFO, "watch_start", "watch: event-driven",
          slow_poll=f"{slow_poll}s", events=events_ok)
    try:
        while not stop_evt.is_set():
            r, _, _ = select.select([xfd, rpipe], [], [], slow_poll)
            if rpipe in r:
                try:  # noqa: SIM105 - deliberate best-effort drain swallow (CLAUDE.md error-handling convention)
                    os.read(rpipe, 64)  # drain; _install_signals already set stop_evt
                except BlockingIOError:
                    pass
                continue  # loop head re-checks stop_evt -> prompt clean exit
            got_event = False
            if events_ok:
                while d.pending_events():
                    d.next_event()
                    got_event = True
            if got_event or (xfd not in r):  # event burst OR slow-poll timeout fired
                last = _apply_if_changed(env, logger, last, churn, got_event, coordinator)
    finally:
        signal.set_wakeup_fd(old_wakeup)
        os.close(rpipe)
        os.close(wpipe)
        d.close()
