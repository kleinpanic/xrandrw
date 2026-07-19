from __future__ import annotations
import errno
import fcntl
import logging
import os
import socket
import threading
from typing import Protocol

from xrandrw.logging_utils import run, wait_for_x, loge, logev
from xrandrw.xrandr import Output, read_xrandr, read_edids
from xrandrw.state import load_state, save_state, ensure_profile, get_profile, _open_lock_fd, state_lock
from xrandrw.policy import is_internal_lcd, current_or_preferred_mode, assign_placements
from xrandrw.profiles import parse_all_profiles, match_profile, build_xrandr_argv
from xrandrw.wallpaper import apply_wallpaper
from xrandrw.touch import remap_touch

def xrandr_output_off(connector: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--off"], logger=logger)

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

def get_apply_backend(env: dict[str, str]) -> ApplyBackend:
    if env.get("APPLY_BACKEND") == "native":
        return NativeRandRBackend()
    return SubprocessBackend()

def reapply_wallpaper(env: dict[str, str], logger: logging.Logger):
    # Thin delegator to the pluggable wallpaper dispatch (WALL-01). The name is kept so the
    # existing call sites and the mock_x monkeypatch continue to resolve here.
    apply_wallpaper(env, logger)

def scrub_stale(outs: dict[str, Output], logger: logging.Logger, backend: ApplyBackend = None):
    # Only power off disconnected heads; avoid pre-apply resets that blank active screens
    off = backend.output_off if backend is not None else xrandr_output_off
    for connector, o in outs.items():
        if o.connected:
            continue
        # EFFICIENCY, NOT SAFETY (14-08): a disconnected output that already has no
        # CRTC is dark, so the --off is pure waste -- measured 3-35 ms each, issued on
        # every apply, and xrandr's own crtc_apply early-returns on them anyway. This
        # skip does NOT fire on the live trace, where HDMI-1 was disconnected and LIT.
        # The scrub's real job is untouched: a disconnected head whose CRTC is STILL
        # LIT is still powered off, which is what xrandr.py's topology_hash self-heal
        # rationale depends on.
        #
        # SCOPE: the WIDER desired-vs-current diff that would also skip a redundant
        # auto_pos re-issue is deliberately NOT implemented. Output (xrandr.py:13-24)
        # carries no rotation, scale or panning fields, so the daemon cannot observe
        # whether an already-correctly-positioned output still has the scale and panning
        # auto_pos would set; skipping that call on a position-and-mode match alone would
        # silently leave a stale scale or panning in place. The wasteful re-modeset is real
        # and is filed as a follow-up in 14-09, not fixed here on unobservable state.
        if o.position is None and o.current_mode is None:
            continue
        off(connector, logger)

# ---------------- external placement (shared by both primary branches) ----------------
def _place_externals(st: dict[str, dict], exts: list[Output], primary_name: str,
                     default_side: str, backend: ApplyBackend, outs: dict[str, Output],
                     logger: logging.Logger) -> None:
    # Shared placement policy for the internal-primary and no-internal branches (CI-04 dedup):
    # resolve each external's stable profile, maintain the newest-last attach_stack, then place
    # connectors newest-first relative to `primary_name`. WR-03: identical-EDID heads share one
    # profile id but must each get a connector placement; HARD-04: once four sides are filled the
    # next connector chains off the previously-placed external (place_chain), not the primary.
    pid_by_output: dict[str, str] = {}
    for o in exts:
        pid_by_output[o.name] = ensure_profile(o, st, logger, default_side)

    # update attach_stack: keep only currently connected pids, append new ones at end
    cur_pids = [pid_by_output[o.name] for o in exts]
    attach_stack = [pid for pid in st.setdefault("attach_stack", []) if pid in cur_pids]
    for pid in cur_pids:
        if pid not in attach_stack:
            attach_stack.append(pid)
    st["attach_stack"] = attach_stack

    # newest-first order + WR-03 connector-expansion (each pid -> ALL its connectors)
    ordered_pids = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
    conns_by_pid: dict[str, list[str]] = {}
    for name, pid in pid_by_output.items():
        conns_by_pid.setdefault(pid, []).append(name)
    ordered_conns = [c for pid in ordered_pids for c in sorted(conns_by_pid[pid])]
    ordered = [(c, get_profile(st, pid_by_output[c]).get("preferred_side") or default_side)
               for c in ordered_conns]
    placements = assign_placements(ordered, primary_name)
    for connector, rel_opt, anchor_connector in placements:
        pid = pid_by_output[connector]
        if anchor_connector == primary_name:
            logev(logger, logging.INFO, "place", "external placement (stack)",
                  output=connector, side=rel_opt, anchor=anchor_connector, profile=pid)
        else:
            logev(logger, logging.INFO, "place_chain", "chained external placement",
                  output=connector, side=rel_opt, anchor=anchor_connector, profile=pid)
        backend.auto_pos(connector, rel_opt, anchor_connector, logger)
        backend.rotate_left_if_portrait(connector, outs[connector], logger)

def apply_once(env: dict[str, str], logger: logging.Logger, event_source: str = "manual") -> bool:
    """Run one full apply pass. Returns True IFF a complete apply ran.

    The return value is LOAD-BEARING for the watch loop (BL-01, 14-08). Every
    early return here is a BAIL on an unknown or unusable topology -- the lock was
    refused, another apply owns it, or a read failed -- and in every one of those
    cases the daemon has NOT healed anything. `_apply_if_changed` must be able to
    tell a bail from a completed apply, because on a bail it must NOT absorb the
    new topology hash: absorbing it freezes change detection on an unhealed state
    and no further apply fires until the next physical event (the Phase-4.1
    phantom-monitor bug, reintroduced when the scrub moved below read #2 -- the
    read-#2 bail now returns 33 lines BEFORE scrub_stale, so a transient failure
    leaves a disconnected-but-lit head powered on forever).

    True  == a full apply completed (including the legitimate "no connected
             outputs" path, which scrubs and reapplies the wallpaper).
    False == bailed; the caller must treat the topology as unknown/unhealed.
    """
    lockfile = env["LOCKFILE"]
    # HARD-02: open the apply-lock with O_NOFOLLOW; a symlinked lock path (CWE-59) raises ELOOP.
    try:
        fd = _open_lock_fd(lockfile)
    except OSError as e:
        if e.errno == errno.ELOOP:
            logev(logger, logging.WARNING, "lock_symlink_refused",
                  "apply-lock path is a symlink; refusing to run", lockfile=str(lockfile))
            return False  # refused the lock -> nothing applied
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        loge(logger, logging.INFO, "apply_skip", "Another apply is running")
        os.close(fd)
        return False  # another daemon owns the apply; WE applied nothing

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
            return False  # topology unknown

        logev(logger, logging.INFO, "apply_start", "apply: start", source=event_source)

        # reread + EDID
        # WR-01: a hotplug mid-apply can make this second read fail transiently; degrade
        # like the first read instead of propagating and killing the watch loop.
        try:
            outs = read_xrandr(logger)
            read_edids(outs, logger)
        except Exception as e:
            logev(logger, logging.ERROR, "xrandr_unavail", "xrandr read failed mid-apply", error=str(e))
            return False  # topology unknown; NOTHING below (incl. scrub_stale) has run

        # Power off any newly disconnected heads only (avoid wiping transforms on active
        # heads). THIS RUNS ON READ #2, NOT READ #1 (moved 14-08) -- do not move it back.
        #
        # The value is STRUCTURAL, not suppressive. Placement filters to
        # `connected = [o for o in outs.values() if o.connected]` (just below) derived from
        # read #2, so scrub and placement now compute their decisions from the SAME
        # snapshot and can no longer contradict each other inside one apply. It becomes
        # structurally impossible for a single apply_once to power a connector off and then
        # bring it straight back up -- which is exactly what happened live (--off at
        # 04:21:43,291 and --auto at 04:21:44,851, ONE apply). An off/on cycle that begins
        # and ends inside one apply is INVISIBLE to the relocation coordinator, which gets
        # one observation per apply; forcing the two halves into different applies is what
        # makes the displacement observable at all.
        #
        # This does NOT prevent the logged --off, and no comment or commit may claim it
        # does. Measured from evidence/newdaemon2.log, the read#1->read#2 gap IS the scrub:
        # 9 ms at 40,728->40,737 when the scrub had only CRTC-less calls to make, 1.55 s at
        # 43,291->44,840 when it had a real HDMI modeset. Post-move, read #2 on that trace
        # lands at ~43,300, when HDMI was still down -- so the --off still happens and dwm
        # still evacuates. See .planning/debug/relocate-replug-bounce.md for the
        # measurement. A future reader who believes the suppression story will delete the
        # CRTC-liveness edge predicate in relocate.py as redundant, and the bug returns.
        #
        # Secondary benefit, stated honestly: the window in which read #1 and read #2 can
        # disagree shrinks from ~1.5 s to ~10 ms.
        #
        # Position is BEFORE the `if not connected` early-return, so the all-disconnected
        # apply_none path still scrubs exactly as it did before the move.
        #
        # CONSEQUENCE OF THE MOVE, AND WHY apply_once RETURNS A BOOL (BL-01). A read-#2
        # failure bails ~30 lines ABOVE this call, so it now issues no --off at all. That
        # is NOT self-evidently an improvement, and an earlier revision of this comment
        # wrongly called it "a deliberate improvement" -- leaving that claim would re-teach
        # the wrong lesson. Pre-move the scrub ran BEFORE the second read, so a transient
        # read-#2 failure still powered off a disconnected-but-lit head. Skipping the scrub
        # on an unknown topology is only safe BECAUSE the bail returns False and the watch
        # loop therefore refuses to absorb the new hash (watch.py) and re-applies. Without
        # that gate this skip is the Phase-4.1 phantom-monitor bug: the head stays lit,
        # topology_hash keeps including it, the digest never changes again, and no further
        # apply fires until another physical event -- a phantom dwm monitor forever.
        # The bool and this ordering are load-bearing TOGETHER; do not keep one without
        # the other.
        scrub_stale(outs, logger, backend)

        connected = [o for o in outs.values() if o.connected]
        if not connected:
            loge(logger, logging.INFO, "apply_none", "no connected outputs found")
            reapply_wallpaper(env, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)
            return True  # a complete apply: the scrub ran and the wallpaper was reapplied

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
            remap_touch(env, {o.name for o in connected}, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done (profile)", source=event_source)
            return True

        # HARD-03: single state-lock span wrapping the entire load_state -> mutate -> save_state
        # read-modify-write, so a concurrent set_pref cannot interleave and lose an update.
        with state_lock(env["STATE_LOCKFILE"]):
            st = load_state()
            default_side = env["PREF_DEFAULT_SIDE"]
            st.setdefault("attach_stack", [])  # profile ids, earliest->latest

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
                _place_externals(st, exts, pnl.name, default_side, backend, outs, logger)
            else:
                # No internal; pick lexicographically first as primary
                first = sorted(connected, key=lambda x: x.name)[0]
                logev(logger, logging.INFO, "primary_set", "primary (no internal)", primary=first.name)
                run(["xrandr", "--output", first.name, "--auto", "--primary"], logger=logger)
                rest = [o for o in connected if o.name != first.name]
                _place_externals(st, rest, first.name, default_side, backend, outs, logger)

            save_state(st)

        reapply_wallpaper(env, logger)
        remap_touch(env, {o.name for o in connected}, logger)
        logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)
        return True
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
