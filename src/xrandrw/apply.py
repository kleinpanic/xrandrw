from __future__ import annotations
import errno
import fcntl
import logging
import os
import socket
import threading
from typing import Dict, List, Protocol

from xrandrw.logging_utils import run, wait_for_x, loge, logev
from xrandrw.xrandr import Output, read_xrandr, read_edids
from xrandrw.state import load_state, save_state, ensure_profile, _open_lock_fd, state_lock
from xrandrw.policy import is_internal_lcd, current_or_preferred_mode, assign_placements
from xrandrw.profiles import parse_all_profiles, match_profile, build_xrandr_argv
from xrandrw.wallpaper import apply_wallpaper

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

# ---------------- apply backend seam (D-03/D-03a) ----------------
# The four mutation primitives apply_once drives. SubprocessBackend (default) wraps the
# tested xrandr_* helpers verbatim; NativeRandRBackend is a SEAM-STUB — it warns and
# delegates to the subprocess path (no native CRTC apply this phase, D-03a).
class ApplyBackend(Protocol):
    def output_off(self, connector: str, logger: logging.Logger): ...
    def primary_scale(self, connector: str, scale: str, logger: logging.Logger): ...
    def auto_pos(self, connector: str, rel_opt: str, anchor: str, logger: logging.Logger): ...
    def rotate_left_if_portrait(self, connector: str, o: Output, logger: logging.Logger): ...

class SubprocessBackend:
    def output_off(self, connector: str, logger: logging.Logger):
        xrandr_output_off(connector, logger)
    def primary_scale(self, connector: str, scale: str, logger: logging.Logger):
        xrandr_auto_primary_scale(connector, scale, logger)
    def auto_pos(self, connector: str, rel_opt: str, anchor: str, logger: logging.Logger):
        xrandr_auto_pos(connector, rel_opt, anchor, logger)
    def rotate_left_if_portrait(self, connector: str, o: Output, logger: logging.Logger):
        xrandr_rotate_left_if_portrait(connector, o, logger)

class NativeRandRBackend:
    # SEAM-STUB (D-03a): native CRTC apply is intentionally NOT implemented. Each op logs a
    # warning and delegates to the subprocess primitive so a config typo degrades safely.
    def _warn(self, op: str, logger: logging.Logger, **kw):
        logev(logger, logging.WARNING, "apply_backend",
              "native apply not implemented; using subprocess", op=op, **kw)
    def output_off(self, connector: str, logger: logging.Logger):
        self._warn("output_off", logger, connector=connector)
        xrandr_output_off(connector, logger)
    def primary_scale(self, connector: str, scale: str, logger: logging.Logger):
        self._warn("primary_scale", logger, connector=connector)
        xrandr_auto_primary_scale(connector, scale, logger)
    def auto_pos(self, connector: str, rel_opt: str, anchor: str, logger: logging.Logger):
        self._warn("auto_pos", logger, connector=connector)
        xrandr_auto_pos(connector, rel_opt, anchor, logger)
    def rotate_left_if_portrait(self, connector: str, o: Output, logger: logging.Logger):
        self._warn("rotate_left_if_portrait", logger, connector=connector)
        xrandr_rotate_left_if_portrait(connector, o, logger)

def get_apply_backend(env: Dict[str, str]) -> ApplyBackend:
    if env.get("APPLY_BACKEND") == "native":
        return NativeRandRBackend()
    return SubprocessBackend()

def reapply_wallpaper(env: Dict[str, str], logger: logging.Logger):
    # Thin delegator to the pluggable wallpaper dispatch (WALL-01). The name is kept so the
    # existing call sites and the mock_x monkeypatch continue to resolve here.
    apply_wallpaper(env, logger)

def scrub_stale(outs: Dict[str, Output], logger: logging.Logger, backend: ApplyBackend = None):
    # Only power off disconnected heads; avoid pre-apply resets that blank active screens
    off = backend.output_off if backend is not None else xrandr_output_off
    for connector, o in outs.items():
        if not o.connected:
            off(connector, logger)

def apply_once(env: Dict[str, str], logger: logging.Logger, event_source: str = "manual") -> None:
    lockfile = env["LOCKFILE"]
    # HARD-02: open the apply-lock with O_NOFOLLOW; a symlinked lock path (CWE-59) raises ELOOP.
    try:
        fd = _open_lock_fd(lockfile)
    except OSError as e:
        if e.errno == errno.ELOOP:
            logev(logger, logging.WARNING, "lock_symlink_refused",
                  "apply-lock path is a symlink; refusing to run", lockfile=str(lockfile))
            return
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        loge(logger, logging.INFO, "apply_skip", "Another apply is running")
        os.close(fd)
        return

    # HARD-03/03a lock-order invariant: the apply-lock is held OUTER (acquired above), the
    # state-lock INNER (acquired below around the load_state->save_state span). We NEVER acquire
    # the apply-lock while holding the state-lock => no lock cycle, no deadlock (D-03a).
    try:
        wait_for_x(logger)

        # D-03: select the apply backend (subprocess default; native is a warn+delegate seam-stub)
        backend = get_apply_backend(env)

        try:
            outs = read_xrandr(logger)
        except Exception as e:
            logev(logger, logging.ERROR, "xrandr_unavail", "xrandr not available", error=str(e))
            return

        logev(logger, logging.INFO, "apply_start", "apply: start", source=event_source)

        # Power off any newly disconnected heads only (avoid wiping transforms on active heads)
        scrub_stale(outs, logger, backend)

        # reread + EDID
        # WR-01: a hotplug mid-apply can make this second read fail transiently; degrade
        # like the first read instead of propagating and killing the watch loop.
        try:
            outs = read_xrandr(logger)
            read_edids(outs, logger)
        except Exception as e:
            logev(logger, logging.ERROR, "xrandr_unavail", "xrandr read failed mid-apply", error=str(e))
            return

        connected = [o for o in outs.values() if o.connected]
        if not connected:
            loge(logger, logging.INFO, "apply_none", "no connected outputs found")
            reapply_wallpaper(env, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)
            return

        # Config-driven device profile override (PROF-01): a matched LAYOUT_* profile assembles
        # a byte-equivalent xrandr argv and early-returns BEFORE the state-lock/placement path, so
        # it never touches persistent state (D-03a). This generalizes the removed Pi4 hardcode.
        profiles = parse_all_profiles(env)
        match = match_profile(frozenset(o.name for o in connected), profiles)
        if match is not None:
            logev(logger, logging.INFO, "profile_match", "device profile matched",
                  profile=match.name, connectors=sorted(match.connectors))
            run(build_xrandr_argv(match), logger=logger)
            reapply_wallpaper(env, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done (profile)", source=event_source)
            return

        # HARD-03: single state-lock span wrapping the entire load_state -> mutate -> save_state
        # read-modify-write, so a concurrent set_pref cannot interleave and lose an update.
        with state_lock(env["STATE_LOCKFILE"]):
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
                logev(logger, logging.INFO, "primary_set", "internal panel primary",
                      primary=pnl.name, mode=str(cur), scale=scale)
                backend.primary_scale(pnl.name, scale, logger)

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

                # HARD-04: newest-first order -> assign_placements chains the 5th+ external off the
                # previously-placed one instead of overlapping on a 4-side cap.
                # WR-03: identical-EDID monitors share one pid, so a pid->connector dict drops all
                # but one head; expand each pid to ALL its connectors and place connectors instead.
                ordered_pids = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
                conns_by_pid: Dict[str, List[str]] = {}
                for name, pid in pid_by_output.items():
                    conns_by_pid.setdefault(pid, []).append(name)
                ordered_conns = [c for pid in ordered_pids for c in sorted(conns_by_pid[pid])]
                placements = assign_placements(ordered_conns, pnl.name)
                for connector, rel_opt, anchor_connector in placements:
                    pid = pid_by_output[connector]
                    if anchor_connector == pnl.name:
                        logev(logger, logging.INFO, "place", "external placement (stack)",
                              output=connector, side=rel_opt, anchor=anchor_connector, profile=pid)
                    else:
                        logev(logger, logging.INFO, "place_chain", "chained external placement",
                              output=connector, side=rel_opt, anchor=anchor_connector, profile=pid)
                    backend.auto_pos(connector, rel_opt, anchor_connector, logger)
                    backend.rotate_left_if_portrait(connector, outs[connector], logger)
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

                # WR-03: same connector-expansion as the internal-primary branch above.
                ordered_pids = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
                conns_by_pid: Dict[str, List[str]] = {}
                for name, pid in pid_by_output.items():
                    conns_by_pid.setdefault(pid, []).append(name)
                ordered_conns = [c for pid in ordered_pids for c in sorted(conns_by_pid[pid])]
                placements = assign_placements(ordered_conns, first.name)
                for connector, rel_opt, anchor_connector in placements:
                    pid = pid_by_output[connector]
                    if anchor_connector == first.name:
                        logev(logger, logging.INFO, "place", "external placement (stack)",
                              output=connector, side=rel_opt, anchor=anchor_connector, profile=pid)
                    else:
                        logev(logger, logging.INFO, "place_chain", "chained external placement",
                              output=connector, side=rel_opt, anchor=anchor_connector, profile=pid)
                    backend.auto_pos(connector, rel_opt, anchor_connector, logger)
                    backend.rotate_left_if_portrait(connector, outs[connector], logger)

            save_state(st)

        reapply_wallpaper(env, logger)
        logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)
    finally:
        os.close(fd)  # closing the fd releases the apply-lock

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
