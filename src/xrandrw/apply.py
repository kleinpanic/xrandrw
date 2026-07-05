from __future__ import annotations
import fcntl, logging, os, shutil, socket, threading
from pathlib import Path
from typing import Dict, List

from xrandrw.logging_utils import run, wait_for_x, loge, logev
from xrandrw.xrandr import Output, read_xrandr, read_edids
from xrandrw.state import load_state, save_state, ensure_profile
from xrandrw.policy import SIDES, is_internal_lcd, current_or_preferred_mode

def xrandr_output_off(connector: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--off"], logger=logger)

def xrandr_reset(connector: str, logger: logging.Logger):
    # Keep for explicit callers if ever needed; no longer used pre-apply for connected outputs
    run(["xrandr", "--output", connector, "--panning", "0x0"], logger=logger)

def xrandr_auto_primary_scale(connector: str, scale: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--auto", "--scale", scale, "--panning", "0x0", "--primary"], logger=logger)

def xrandr_auto_pos(connector: str, rel_opt: str, anchor: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--auto", "--scale", "1x1", "--panning", "0x0", f"--{rel_opt}", anchor], logger=logger)

def xrandr_rotate_left_if_portrait(connector: str, o: Output, logger: logging.Logger):
    m = current_or_preferred_mode(o)
    if m:
        w, h = m
        if h > w:
            run(["xrandr", "--output", connector, "--rotate", "left"], logger=logger)

def reapply_wallpaper(env: Dict[str, str], logger: logging.Logger):
    wall = env["WALL"]
    if env["USE_XWALLPAPER"] == "1":
        if Path(wall).is_file() and shutil.which("xwallpaper"):
            run(["xwallpaper", "--zoom", wall], logger=logger)
            logev(logger, logging.INFO, "wallpaper", "xwallpaper", file=wall)
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "xwallpaper missing or file not found", file=wall)
    else:
        if shutil.which("fehbg"):
            run(["fehbg"], logger=logger)
            loge(logger, logging.INFO, "wallpaper", "fehbg")
        elif shutil.which("feh") and Path(wall).is_file():
            run(["feh", "--no-fehbg", "--bg-fill", wall], logger=logger)
            logev(logger, logging.INFO, "wallpaper", "feh", file=wall)
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "no feh/fehbg or file", file=wall)

def scrub_stale(outs: Dict[str, Output], logger: logging.Logger):
    # Only power off disconnected heads; avoid pre-apply resets that blank active screens
    for connector, o in outs.items():
        if not o.connected:
            xrandr_output_off(connector, logger)

def apply_once(env: Dict[str, str], logger: logging.Logger, event_source: str = "manual") -> None:
    lockfile = env["LOCKFILE"]
    with open(lockfile, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            loge(logger, logging.INFO, "apply_skip", "Another apply is running")
            return

        wait_for_x(logger)

        try:
            outs = read_xrandr(logger)
        except Exception as e:
            logev(logger, logging.ERROR, "xrandr_unavail", "xrandr not available", error=str(e))
            return

        logev(logger, logging.INFO, "apply_start", "apply: start", source=event_source)

        # Power off any newly disconnected heads only (avoid wiping transforms on active heads)
        scrub_stale(outs, logger)

        # reread + EDID
        outs = read_xrandr(logger)
        read_edids(outs, logger)

        connected = [o for o in outs.values() if o.connected]
        if not connected:
            loge(logger, logging.INFO, "apply_none", "no connected outputs found")
            reapply_wallpaper(env, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)
            return

        # Special-case Raspberry Pi 4 dual-head layout:
        # - DSI-1 is the built-in panel and should be primary
        # - HDMI-1 sits to the *left* of DSI-1
        # This matches:
        #   xrandr \
        #     --output DSI-1  --primary --mode 800x480  --pos 1600x0 --scale 1x1 \
        #     --output HDMI-1 --mode 1600x900         --pos 0x0    --scale 1x1
        names = {o.name for o in connected}
        if "DSI-1" in names and "HDMI-1" in names:
            logev(logger, logging.INFO, "apply_pi4", "Raspberry Pi 4 dual-head layout",
                  primary="DSI-1", left="HDMI-1")
            run([
                "xrandr",
                "--output", "DSI-1", "--primary", "--mode", "800x480", "--pos", "1600x0", "--scale", "1x1",
                "--output", "HDMI-1", "--mode", "1600x900", "--pos", "0x0", "--scale", "1x1",
            ], logger=logger)
            reapply_wallpaper(env, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done (pi4 special-case)", source=event_source)
            return

        st = load_state()
        default_side = env["PREF_DEFAULT_SIDE"]
        attach_stack: List[str] = st.setdefault("attach_stack", [])  # profile ids, earliest->latest

        # prefer internal as primary
        internal = [o for o in connected if is_internal_lcd(o.name)]
        if internal:
            pnl = sorted(internal, key=lambda x: x.name)[0]
            hidpi_threshold = int(env["HIDPI_WIDTH"])
            cur = current_or_preferred_mode(pnl)
            width = (cur[0] if cur else 0)
            scale = "0.5x0.5" if width >= hidpi_threshold else "1x1"
            logev(logger, logging.INFO, "primary_set", "eDP/LVDS primary",
                  primary=pnl.name, mode=str(cur), scale=scale)
            xrandr_auto_primary_scale(pnl.name, scale, logger)

            exts = [o for o in connected if o.name != pnl.name]
            pid_by_output: Dict[str, str] = {}
            for o in exts:
                pid_by_output[o.name] = ensure_profile(o, st, logger, default_side)

            # update attach_stack: keep only currently connected pids, append new ones at end
            cur_pids = [pid_by_output[o.name] for o in exts]
            attach_stack = [pid for pid in attach_stack if pid in cur_pids]
            for pid in cur_pids:
                if pid not in attach_stack:
                    attach_stack.append(pid)
            st["attach_stack"] = attach_stack

            # assignment: newest gets right-of, previous left-of, then above, below
            desired_order = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
            sides_order = ["right-of", "left-of", "above", "below"]
            assigned_by_pid: Dict[str, str] = {}
            for pid, side in zip(desired_order, sides_order):
                assigned_by_pid[pid] = side

            # fallback for >4 externals or any remaining
            occupied: Dict[str, str] = {}
            for o in exts:
                pid = pid_by_output[o.name]
                side = assigned_by_pid.get(pid)
                if not side:
                    side = next((s for s in SIDES if s not in occupied), default_side)
                # reserve the side
                if side in occupied:
                    # pick a free one deterministically
                    side = next((s for s in SIDES if s not in occupied), side)
                occupied[side] = o.name
                logev(logger, logging.INFO, "place", "external placement (stack)",
                      output=o.name, side=side, anchor=pnl.name, profile=pid)
                xrandr_auto_pos(o.name, side, pnl.name, logger)
                xrandr_rotate_left_if_portrait(o.name, o, logger)
        else:
            # No internal; pick lexicographically first as primary
            first = sorted(connected, key=lambda x: x.name)[0]
            logev(logger, logging.INFO, "primary_set", "primary (no internal)", primary=first.name)
            run(["xrandr", "--output", first.name, "--auto", "--primary"], logger=logger)
            rest = [o for o in connected if o.name != first.name]

            pid_by_output: Dict[str, str] = {}
            for o in rest:
                pid_by_output[o.name] = ensure_profile(o, st, logger, default_side)

            # update attach_stack with current externals
            cur_pids = [pid_by_output[o.name] for o in rest]
            attach_stack = [pid for pid in st.setdefault("attach_stack", []) if pid in cur_pids]
            for pid in cur_pids:
                if pid not in attach_stack:
                    attach_stack.append(pid)
            st["attach_stack"] = attach_stack

            desired_order = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
            sides_order = ["right-of", "left-of", "above", "below"]
            assigned_by_pid: Dict[str, str] = {pid: side for pid, side in zip(desired_order, sides_order)}

            occupied: Dict[str, str] = {}
            for o in rest:
                pid = pid_by_output[o.name]
                side = assigned_by_pid.get(pid)
                if not side:
                    side = next((s for s in SIDES if s not in occupied), default_side)
                if side in occupied:
                    side = next((s for s in SIDES if s not in occupied), side)
                occupied[side] = o.name
                logev(logger, logging.INFO, "place", "external placement (stack)",
                      output=o.name, side=side, anchor=first.name, profile=pid)
                xrandr_auto_pos(o.name, side, first.name, logger)
                xrandr_rotate_left_if_portrait(o.name, o, logger)

        save_state(st)
        reapply_wallpaper(env, logger)
        logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)

def _sd_notify(msg: str):
    addr = os.getenv("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        try:
            s.connect(addr)
            s.send(msg.encode())
        except Exception:
            pass

def _watchdog_thread(stop_evt: threading.Event, logger: logging.Logger):
    usec = os.getenv("WATCHDOG_USEC")
    if not usec:
        return
    interval = int(int(usec) / 2 / 1_000_000) or 1
    while not stop_evt.wait(interval):
        _sd_notify("WATCHDOG=1")
        loge(logger, logging.DEBUG, "watchdog", "sd_notify WATCHDOG=1")
