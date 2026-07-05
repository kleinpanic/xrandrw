from __future__ import annotations
import logging, os, shutil, signal, subprocess, sys, threading, time
from typing import Dict, List

from xrandrw.logging_utils import loge, logev
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

def watch_loop(env: Dict[str, str], logger: logging.Logger):
    poll = int(env["POLL_INTERVAL"])
    window = int(env["EXCESS_WINDOW_SEC"])
    threshold = int(env["EXCESS_THRESHOLD"])
    last = topology_hash(logger)
    logev(logger, logging.INFO, "watch_start", "watch: started", poll=f"{poll}s")
    change_times: List[float] = []
    backoff_add = 0
    while not stop_evt.is_set():
        cur = topology_hash(logger)
        if cur != last:
            now = time.monotonic()
            change_times.append(now)
            change_times = [t for t in change_times if now - t <= window]
            if len(change_times) > threshold:
                logev(logger, logging.WARNING, "watch_excess", "excess topology churn",
                      count=len(change_times), window=window)
                backoff_add = min(1000, backoff_add + 150)
            else:
                backoff_add = max(0, backoff_add - 50)
            logev(logger, logging.DEBUG, "watch_change", "topology hash changed",
                  debounce_ms=150+backoff_add)
            time.sleep((150 + backoff_add) / 1000.0)
            verify = topology_hash(logger)
            if verify != last:
                logev(logger, logging.INFO, "watch_apply", "apply on topology change",
                      source="watch_poll")
                apply_once(env, logger, event_source="watch_poll")
                last = verify
        stop_evt.wait(poll)

def spawn_xplugd(logger: logging.Logger):
    if shutil.which("xplugd"):
        script_path = os.path.abspath(sys.argv[0])
        logev(logger, logging.INFO, "daemon_xplugd", "launching xplugd", script=script_path)
        proc = subprocess.Popen(["xplugd", "-n", "-s", "-l", "notice", script_path])
        logev(logger, logging.INFO, "daemon_xplugd", "xplugd started", pid=proc.pid)
        return proc
    else:
        loge(logger, logging.INFO, "daemon_xplugd_skip", "xplugd not found; watcher only")
        return None
