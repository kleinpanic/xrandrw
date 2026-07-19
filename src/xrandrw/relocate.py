"""Hotplug relocation lifecycle: put displaced windows back (Phase 10, WM-05/WM-06/WM-08).

On an output unplug dwm auto-evacuates the removed monitor's clients to a
surviving monitor; on replug this module restores each displaced window to where
it was FOR THE SAME PROCESS -- focus, tagmon back to the restored monitor, tag,
floating-state, and (only for floating windows) the saved geometry. Tiled windows
get monitor+tag+floating-state but NO geometry write and are NEVER converted.

KNOWN LIMITATION (IN-03): ``is_fullscreen`` is CAPTURED into the record but is
NOT restored. dwm exposes fullscreen via ``_NET_WM_STATE_FULLSCREEN``, but a
correct headless-testable restore of that state could not be validated against
the fake server + mocked Xlib this late in the phase, so it is intentionally
omitted rather than shipped unverified. A window that was fullscreen on the
removed output returns to its saved monitor/tag/floating-state/geometry but not
its fullscreen flag. Tracked for a follow-up; documented in SECURITY.md.

Besides the productionised :mod:`xrandrw.dwmipc` verbs, this module is the ONLY
place in the codebase that MUTATES window state. The mutation surface is kept
small and independently verifiable: the live-X mutations go through the thin,
mockable :class:`RelocationControl` seam (mirrors ``windows.WindowXReader`` /
``xrandr.RandRReader``: own Display per call, never raises past the seam), and
the ordering/bounding logic lives in the PURE, headless-testable helpers
``tagmon_direction`` and ``plan_restore``.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections import namedtuple
from types import SimpleNamespace

from Xlib import X, display
from Xlib.protocol import event

from xrandrw import dwmipc
from xrandrw.logging_utils import logev
from xrandrw.windows import (WindowXReader, capture_windows,
                             match_dwm_monitor_to_output, read_proc_identity,
                             resolve_pid)
from xrandrw.xrandr import RandRReader
# NOTE: read_edids is intentionally NOT imported -- the coordinator never
# references it (capture_windows calls read_edids internally on the outputs it
# reads), so importing it here would be dead code a Phase-12 vulture/ruff gate
# would flag (W3). Tests monkeypatch xrandrw.windows.read_edids, not relocate.

# Module logger; the seam is the only place that touches a live Display, so its
# degrade events log through this shared "xrandrw" logger (mirrors the codebase).
_LOG = logging.getLogger("xrandrw")

# CR-01 focus-confirm poll budget. _NET_ACTIVE_WINDOW is delivered async over the
# X channel, but the run_command verbs travel the SEPARATE dwm-ipc socket and act
# on dwm's currently-selected client -- which only updates once dwm's event loop
# processes the ClientMessage. So after focus() the coordinator polls get_monitors
# until dwm reports the target as selected BEFORE issuing a verb (spike 003 used a
# fixed d.flush()+sleep(0.15); this deterministic poll replaces the magic sleep).
# A few short tries bounded well under a second; on timeout we log + proceed.
_FOCUS_CONFIRM_TRIES = 6
_FOCUS_CONFIRM_SLEEP = 0.03  # seconds between polls (total budget ~= tries*sleep)


def _selected_confirmed(monitors, xid) -> bool:
    """True iff dwm reports ``xid`` as the selected client of the selected monitor.

    dwm's command verbs act on ``selmon->sel``; a monitor reply carries
    ``is_selected`` (the selected monitor) and ``clients.selected`` (that
    monitor's selected client). We treat the focus as landed once the selected
    monitor's selected client is ``xid`` (falling back to any monitor whose
    ``clients.selected`` is ``xid`` for replies that omit ``is_selected``).
    """
    fallback = False
    for m in monitors:
        clients = m.get("clients")
        sel = clients.get("selected") if isinstance(clients, dict) else None
        if sel == xid:
            if m.get("is_selected"):
                return True
            fallback = True
    return fallback

# One restore delta. ``verb`` is one of "tagmon" | "tag" | "togglefloating" |
# "configure"; ``args`` is the verb argument (a tagmon direction int, a tag
# bitmask int, None for togglefloating, or a geometry dict for configure). The
# coordinator (Plan 03) inserts a focus() before EVERY verb -- focus is NOT an
# Action here because plan_restore is pure and focus is a live-X side effect.
Action = namedtuple("Action", "verb args")


def _safe_close(d) -> None:
    if d is not None:
        with contextlib.suppress(Exception):
            d.close()


class RelocationControl:
    """Thin main-thread-only live Xlib control seam mirroring ``WindowXReader``.

    Every method opens its OWN ``display.Display()``, shares no state, and closes
    it in a ``finally`` block (Xlib's Display is not thread-safe). No method ever
    raises past the seam: any Xlib error is logged via ``logev`` and the method
    returns ``False`` so a single bad window never propagates out. This class is
    the ONLY place that touches a live Display for MUTATION; the coordinator
    drives it through this seam so tests inject a fake control with no X server.
    The exact clientmessage/configure calls are the ones proven live in
    ``.planning/spikes/003-window-move-control/probe_003_live.py``.
    """

    def focus(self, xid) -> bool:
        """Focus ``xid`` by sending ``_NET_ACTIVE_WINDOW`` to the root window.

        The focus-then-act targeting primitive (spike 003, WM-05): dwm's command
        verbs act on the SELECTED client, so the coordinator focuses a window
        before every verb. Returns True on send; on any Xlib error logs
        ``relocate_focus_fail`` and returns False (never raises).
        """
        d = None
        try:
            d = display.Display()
            root = d.screen().root
            atom = d.intern_atom("_NET_ACTIVE_WINDOW")
            win = d.create_resource_object("window", xid)
            ev = event.ClientMessage(window=win, client_type=atom,
                                     data=(32, [2, X.CurrentTime, 0, 0, 0]))
            root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
            d.flush()
            return True
        except Exception as e:  # never propagate out of the seam
            logev(_LOG, logging.WARNING, "relocate_focus_fail",
                  "sending _NET_ACTIVE_WINDOW failed", xid=xid, error=str(e))
            return False
        finally:
            _safe_close(d)

    def configure_geometry(self, xid, geometry) -> bool:
        """Position a FLOATING window via X ConfigureWindow (MONITOR-RELATIVE x/y).

        Restores a saved floating geometry ``{x,y,width,height}`` (spike 003:
        dwm honors the ConfigureWindow for floating clients). Returns True on
        send; on any Xlib error logs ``relocate_configure_fail`` and returns
        False (never raises). The coordinator only ever calls this for floating
        records -- tiled windows are re-tiled by dwm and never get a geometry
        write.

        IMPORTANT: the ``x``/``y`` here must already be MONITOR-RELATIVE, not
        absolute root coords. dwm's ``configurerequest`` is monitor-relative
        (``c->x = c->mon->mx + ev->x``), so the coordinator converts the captured
        ABSOLUTE geometry to target-monitor-relative via
        :func:`to_monitor_relative_geometry` before calling this seam. Passing
        absolute coords would double-shift (and overflow-center) the window on any
        monitor whose origin > 0 -- the exact bug the L1 harness surfaced.
        """
        d = None
        try:
            d = display.Display()
            win = d.create_resource_object("window", xid)
            win.configure(x=geometry["x"], y=geometry["y"],
                          width=geometry["width"], height=geometry["height"])
            d.flush()
            return True
        except Exception as e:  # never propagate out of the seam
            logev(_LOG, logging.WARNING, "relocate_configure_fail",
                  "ConfigureWindow geometry restore failed", xid=xid, error=str(e))
            return False
        finally:
            _safe_close(d)


# --------------------------------------------------------------------------
# Pure planning helpers (no I/O; unit-provable headless)
# --------------------------------------------------------------------------

def monitor_origin(monitors, num) -> tuple[int, int] | None:
    """Return ``(x, y)`` origin of dwm monitor ``num`` from a get_monitors list.

    ``monitors`` is the validated ``get_monitors()`` list (each dict carries
    ``num`` + ``monitor_geometry{x,y,...}``). Returns the monitor's origin as a
    ``(x, y)`` int tuple, or ``None`` when ``num`` is None, no monitor matches, or
    the geometry lacks numeric ``x``/``y`` -- so the caller degrades (skip the
    geometry write) rather than guessing an origin. PURE: no I/O.
    """
    if num is None:
        return None
    for m in monitors:
        if m.get("num") == num:
            mg = m.get("monitor_geometry") or {}
            x, y = mg.get("x"), mg.get("y")
            if isinstance(x, int) and isinstance(y, int):
                return (x, y)
            return None
    return None


def to_monitor_relative_geometry(geometry, origin) -> dict[str, int]:
    """Convert an ABSOLUTE ``{x,y,width,height}`` to a MONITOR-RELATIVE one.

    Capture stores dwm's absolute root coords (``get_dwm_client`` geometry is
    absolute), but dwm's ``configurerequest`` is monitor-relative
    (``c->x = c->mon->mx + ev->x``). To land a floating window back at its saved
    absolute position we must send ``ev->x = abs_x - target_origin_x`` (dwm then
    recomputes ``c->x = target_origin_x + ev->x = abs_x``). ``origin`` is the
    ``(x, y)`` target monitor origin from :func:`monitor_origin`. Returns a NEW
    dict; ``width``/``height`` pass through unchanged. Raises on malformed input
    (missing keys / non-int) so the caller's guard turns it into a logged skip.
    PURE: no I/O.
    """
    ox, oy = origin
    return {
        "x": int(geometry["x"]) - int(ox),
        "y": int(geometry["y"]) - int(oy),
        "width": int(geometry["width"]),
        "height": int(geometry["height"]),
    }

def tagmon_direction(cur_num: int, target_num: int, n_monitors: int) -> int | None:
    """Return the fewest-hop RELATIVE tagmon direction from ``cur_num`` to ``target_num``.

    dwm's ``tagmon`` moves the selected client by a RELATIVE monitor delta
    (spike 003, WM-05), so the coordinator drives it one hop at a time. This
    helper returns ``+1`` (next) or ``-1`` (previous) picking whichever wraps in
    the fewer hops, with a deterministic ``+1`` tie-break. It returns ``None``
    -- meaning "do not move" -- when there are fewer than two monitors, when
    ``target_num`` is outside ``range(n_monitors)``, or when already on target.
    It can NEVER return an unbounded step count; the iteration bound + giveup
    live in the Plan-03 coordinator.
    """
    if n_monitors < 2:
        return None
    if target_num not in range(n_monitors):
        return None
    if cur_num == target_num:
        return None
    forward = (target_num - cur_num) % n_monitors
    backward = (cur_num - target_num) % n_monitors
    return 1 if forward <= backward else -1


def plan_restore(record, live) -> list[Action]:
    """Compute the ordered restore deltas for one displaced window (PURE, no I/O).

    ``record`` is a captured ``WindowRecord`` (or any object exposing
    ``is_floating``, ``tags``, ``geometry``); ``live`` duck-types the CURRENT
    dwm view (``target_monitor``, ``current_monitor``, ``current_floating``,
    ``n_monitors``). The returned ordered list is (spike 003/004, WM-05):

      1. ``tagmon(dir)`` -- only when the window must change monitor and a
         non-None direction exists;
      2. ``tag(tags)`` -- always restore the saved tag bitmask;
      3. ``togglefloating`` -- ONLY when the live floating state differs from the
         saved one (restore the saved state, never a gratuitous conversion);
      4. ``configure(geometry)`` -- IFF the record is floating.

    This is the tiled-vs-floating guarantee: a TILED record (``is_floating``
    False) NEVER yields a ``configure`` and is NEVER toggled beyond restoring its
    saved state, so tiling is preserved and dwm re-tiles it.
    """
    actions: list[Action] = []
    target = live.target_monitor
    if target is not None and target != live.current_monitor:
        direction = tagmon_direction(live.current_monitor, target, live.n_monitors)
        if direction is not None:
            actions.append(Action("tagmon", direction))
    actions.append(Action("tag", int(record.tags)))
    if bool(live.current_floating) != bool(record.is_floating):
        actions.append(Action("togglefloating", None))
    if record.is_floating:
        actions.append(Action("configure", dict(record.geometry)))
    return actions


# --------------------------------------------------------------------------
# RelocationCoordinator: unplug-record / replug-restore state machine (WM-05/08)
# --------------------------------------------------------------------------

class RelocationCoordinator:
    """In-memory owner of displaced-window records + the record/restore machine.

    Composed from the Plan-01 primitives (:class:`RelocationControl`,
    ``plan_restore``, ``tagmon_direction``), the Phase-8 ``dwmipc`` verbs, and the
    Phase-9 ``capture_windows`` / ``resolve_pid`` / ``match_dwm_monitor_to_output``
    seams. ``on_settled`` is the single watch entry point (Plan 04 calls it
    post-apply). The whole lifecycle is a no-op unless :meth:`_enabled`
    (``config_enabled`` AND ``dwmipc.available()``) so the Phase-11 config flag
    ANDs in without refactor. Every per-window IPC/X failure is logged and
    skipped -- never fatal (WM-08); display layout always still applies.
    """

    def __init__(self, *, control=None, reader=None, xreader=None,
                 capture=capture_windows, sock_path=None, proc_root: str = "/proc",
                 config_enabled: bool = True, ipc_timeout: float = dwmipc.DEFAULT_TIMEOUT):
        self._control = control if control is not None else RelocationControl()
        self._reader = reader if reader is not None else RandRReader()
        self._xreader = xreader if xreader is not None else WindowXReader()
        self._capture = capture
        self._sock_path = sock_path
        self._proc_root = proc_root
        self._config_enabled = config_enabled
        # Small per-window IPC timeout so a synchronous restore cannot stall the
        # single-threaded watch select() loop (W2 accepted tradeoff); threaded
        # into EVERY dwmipc call below (including capture_windows via _safe_capture,
        # AUDIT-B). cli.py passes a modest override (_RELOCATE_IPC_TIMEOUT, IN-01).
        self._ipc_timeout = ipc_timeout
        self._displaced: dict[tuple[int, int], object] = {}
        self._snapshot: dict[tuple[int, int], object] = {}
        # PRESENT, not CONNECTED (WR-04). Holds the set of outputs with a LIVE
        # CRTC (Output.is_lit) as of the last settle -- NOT the HPD connected set.
        # The old `_prev_connected` name survived the 14-08 switch to CRTC liveness
        # and became an active lie: it is read cross-module by name (cli.py) and
        # someone debugging a future incident would reach for HPD state.
        self._prev_present: set[str] | None = None

    @property
    def _path(self) -> str:
        return self._sock_path or dwmipc.DEFAULT_SOCK_PATH

    def _enabled(self) -> bool:
        # The AND is where the Phase-11 config flag slots in without refactor:
        # config_enabled (defaults True this phase) AND a live dwm-ipc endpoint.
        return self._config_enabled and dwmipc.available(self._path, timeout=self._ipc_timeout)

    # --- single watch entry point ------------------------------------------

    def on_settled(self, env, logger, stop_evt=None) -> None:
        """Post-apply hook: record on unplug, restore on replug, seed on boot.

        A no-op unless :meth:`_enabled`. On the FIRST call (``_prev_present``
        is None) it only seeds the baseline (``_prev_present`` + ``_snapshot``)
        so the FIRST unplug of the session is recordable -- the first cycle is
        never lost. Thereafter a removed output moves last-snapshot records into
        ``_displaced``; a returned output restores same-identity records; a
        steady settle (no removal) refreshes the snapshot.

        ``stop_evt`` (the watch-loop shutdown flag, WR-01) is threaded into the
        synchronous restore so a SIGTERM during a slow per-window restore cycle
        bails after the current window instead of running the whole batch (which
        could delay shutdown by ``N_windows * per-window IPC`` while the watchdog
        still reports healthy).
        """
        if not self._enabled():
            return
        # WR-01-style guard: a hotplug / X-restart race can make this live read
        # raise transiently; degrade (log + return) instead of propagating.
        try:
            outs = self._reader.read(logger)
        except Exception as e:
            logev(logger, logging.WARNING, "relocate_read_fail",
                  "x read failed at settle; skipping relocation this cycle", error=str(e))
            return
        # CRTC LIVENESS, not HPD `connected` (14-08). An output counts as PRESENT when
        # it has a current mode -- i.e. it is still driving pixels, so dwm still has that
        # monitor. This is the Phase-4.1 lesson already recorded at `xrandr.py`'s
        # topology_hash ("A disconnected head whose CRTC is still lit ... must be visible
        # to change detection or the daemon never heals it"): the watch layer learned it
        # then; the relocation layer never did. See
        # `.planning/debug/relocate-replug-bounce.md`.
        #
        # WHY THIS IS SUFFICIENT NOW AND WAS NOT BEFORE (an earlier plan revision rejected
        # this change, correctly, for the PRE-reorder world):
        #   * Pre-reorder, an --off and its paired --auto could land inside ONE apply_once.
        #     Liveness was then unchanged across the single observation the coordinator
        #     gets per apply, so no edge appeared and this predicate would not have fired.
        #   * Post-reorder (see the scrub_stale comment in apply.py) scrub and placement
        #     read the same snapshot, so that pairing cannot be constructed. An --off with
        #     no matching --auto leaves the CRTC dark, and `cur` sees it. The reorder is
        #     what makes this predicate sufficient; the two changes are load-bearing
        #     TOGETHER and neither alone.
        #   * HPD `connected` cannot substitute. On the live trace, by the post-apply
        #     settle at 47,09x HDMI-1 read a valid EDID again (47,095), so HPD `cur`
        #     equalled `_prev_present` and `removed` was empty. Only the dark CRTC
        #     distinguishes the state.
        #
        # It also correctly STOPS the coordinator recording an unplug whose CRTC the apply
        # has not torn down yet: dwm still has that monitor and has not evacuated, so
        # recording would produce records for windows that were never displaced.
        cur = {name for name, o in outs.items() if o.is_lit}
        if self._prev_present is None:
            self._prev_present = cur
            self._snapshot = self._safe_capture(logger)
            logev(logger, logging.INFO, "relocate_seed",
                  # WR-04: `present` (live CRTC), NOT `connected` (HPD). Logging
                  # `connected=1` for a CRTC count sent the last investigation
                  # looking at HPD state.
                  "seeded steady-state baseline", present=len(cur),
                  windows=len(self._snapshot))
            return
        removed = self._prev_present - cur
        returned = cur - self._prev_present
        self._prev_present = cur
        if removed:
            self._record_displaced(removed, logger)
        if returned:
            self._restore_returned(returned, outs, logger, stop_evt)
        if not removed:
            # Steady/return state = current good placements; keep snapshot fresh
            # and sweep dead displaced records so the map cannot leak unbounded.
            self._sweep_displaced(logger)
            self._snapshot = self._safe_capture(logger)

    # --- helpers -----------------------------------------------------------

    def _safe_capture(self, logger) -> dict[tuple[int, int], object]:
        """Capture the current placements keyed by (pid, starttime).

        On any failure log ``relocate_capture_fail`` and return the PREVIOUS
        snapshot unchanged -- a failed capture must never blow away the last
        good baseline (WM-08).
        """
        try:
            # AUDIT-B: thread ipc_timeout so capture_windows' internal dwmipc
            # calls honour the same per-call bound as the rest of the coordinator.
            recs = self._capture(reader=self._reader, xreader=self._xreader,
                                 proc_root=self._proc_root, sock_path=self._sock_path,
                                 timeout=self._ipc_timeout, logger=logger)
            return {(r.pid, r.starttime): r for r in recs}
        except Exception as e:
            logev(logger, logging.WARNING, "relocate_capture_fail",
                  "capture failed; keeping previous snapshot", error=str(e))
            return self._snapshot

    def _sweep_displaced(self, logger) -> None:
        """Evict displaced records whose ``(pid, starttime)`` is no longer live.

        WR-02/AUDIT-A: displaced records normally evict when their output
        reconnects, but a PERMANENTLY-removed output or a process that exits
        while still displaced would leak forever in a long-lived daemon. On each
        steady-state settle we re-resolve every displaced record's process
        against ``/proc``; a record whose ``(pid, starttime)`` no longer resolves
        (dead process or a reused PID) is dropped. No control/IPC call is made --
        this is a pure ``/proc`` liveness check, so it never touches a window.
        """
        for key, rec in list(self._displaced.items()):
            identity = read_proc_identity(rec.pid, self._proc_root, logger=logger)
            if identity is None or (identity[0], identity[1]) != (rec.pid, rec.starttime):
                del self._displaced[key]
                logev(logger, logging.INFO, "relocate_displaced_evict",
                      "displaced record process gone; evicting stale entry",
                      pid=rec.pid, output=rec.output)

    def _record_displaced(self, removed, logger) -> None:
        """Move last-snapshot records on the removed outputs into ``_displaced``.

        Uses the LAST snapshot (taken before the disruption) -- does NOT
        re-capture now (dwm has already evacuated). We do not fight dwm's
        evacuation; we only remember which PIDs were displaced from where.

        KNOWN LIMITATION -- records are CONNECTOR-keyed, not monitor-identity
        keyed (WR-06). We store ``rec.output`` (e.g. "HDMI-1") and
        :meth:`_restore_returned` matches on ``rec.output in returned``.
        ``rec.edid`` IS captured but is NEVER consulted. So if you unplug monitor
        A from HDMI-1 and plug a DIFFERENT monitor B into the same port, B's
        return looks identical to A's and A's windows are tagmon'd onto B. This
        is pre-existing, but CRTC-liveness edges (14-08) create ``_displaced``
        entries on strictly more occasions, so the exposure GREW.

        Why there is no EDID guard here rather than an oversight: the obvious
        check (``rec.edid == outs[rec.output].edid_sha1`` when both are known)
        cannot fire. ``on_settled``'s ``outs`` comes from ``RandRReader.read()``,
        and ``randr_resources_to_outputs`` never populates ``edid_sha1`` -- only
        ``read_edids`` does, and the coordinator does not call it (``rec.edid``
        exists only because ``capture_windows`` reads EDIDs on its OWN read).
        Adding the guard therefore requires an extra EDID round-trip on the
        settle path plus a new live-X call site in this module. That is a
        behaviour change, not a gap fix, so it is deferred rather than shipped as
        a check that silently never runs -- dead code shaped like protection is
        worse than a documented limitation.
        """
        for key, rec in list(self._snapshot.items()):
            if rec.output in removed:
                self._displaced[key] = rec
                logev(logger, logging.INFO, "relocate_record",
                      "recorded displaced window", pid=rec.pid, output=rec.output)

    def _restore_returned(self, returned, outs, logger, stop_evt=None) -> None:
        """Restore displaced records whose output has returned; drop stale ones.

        Checks ``stop_evt`` at the head of the per-window loop (WR-01): a
        SIGTERM mid-cycle stops after the current window so shutdown is prompt
        even against a slow-but-connected dwm; remaining windows stay displaced
        for a later cycle.

        The ``rec.output in returned`` match is by CONNECTOR NAME and is NOT
        monitor-identity stable -- a different monitor in the same port inherits
        the previous monitor's windows. See :meth:`_record_displaced` for why the
        EDID guard is deferred rather than implemented (WR-06).
        """
        t0 = time.monotonic()
        dropped = skipped = 0
        # SINGLE whole-cycle abort: a DwmIpcUnavailable at cycle ENTRY means the
        # dwm-ipc endpoint itself is gone, so abandon this cycle's relocation
        # entirely -- the display layout still applied (WM-08).
        try:
            monitors = dwmipc.get_monitors(path=self._path, timeout=self._ipc_timeout)
        except dwmipc.DwmIpcUnavailable as e:
            logev(logger, logging.WARNING, "relocate_cycle_abandon",
                  "dwm-ipc unavailable at cycle entry; abandoning this relocation cycle",
                  error=str(e))
            return
        mapping = match_dwm_monitor_to_output(monitors, outs, logger=logger)
        conn_to_mon = {conn: num for num, conn in mapping.items() if conn is not None}
        for key, rec in list(self._displaced.items()):
            if stop_evt is not None and stop_evt.is_set():
                logev(logger, logging.INFO, "relocate_cycle_interrupted",
                      "shutdown requested mid-cycle; stopping after current window",
                      dropped=dropped, skipped=skipped)
                break
            if rec.output not in returned:
                continue
            try:
                result = self._restore_one(rec, monitors, conn_to_mon, logger)
            except Exception as e:
                # ANY raise here -- INCLUDING a per-window DwmIpcUnavailable from
                # get_dwm_client/run_command (dwmipc raises the SAME type for a
                # gone/bad window as for a dead socket and cannot disambiguate) --
                # is treated as THIS window failing: log, skip, leave it displaced
                # for a later cycle so other windows still restore.
                logev(logger, logging.WARNING, "relocate_window_fail",
                      "window restore failed; leaving displaced", pid=rec.pid, error=str(e))
                skipped += 1
                continue
            if result == "drop":
                del self._displaced[key]
                dropped += 1
        logev(logger, logging.INFO, "relocate_cycle_done",
              "relocation cycle complete",
              duration_ms=int((time.monotonic() - t0) * 1000), dropped=dropped, skipped=skipped)

    def _focus_and_confirm(self, xid, logger) -> None:
        """Focus ``xid`` then POLL until dwm reports it selected (CR-01).

        The Xlib ``focus()`` seam only sends ``_NET_ACTIVE_WINDOW`` + flush; dwm's
        selection updates asynchronously. This bounded poll of ``get_monitors``
        (threaded with ``ipc_timeout``) closes the focus->verb race so a verb
        never lands on the previously-selected client. On timeout it logs
        ``relocate_focus_unconfirmed`` and proceeds best-effort; a transient
        ``DwmIpcUnavailable`` mid-poll returns early (the caller's next verb will
        raise and be handled per-window). This lives in the coordinator, not the
        Xlib seam, because confirming needs an IPC read the seam must not own.
        """
        self._control.focus(xid)
        for _ in range(_FOCUS_CONFIRM_TRIES):
            try:
                monitors = dwmipc.get_monitors(path=self._path, timeout=self._ipc_timeout)
            except dwmipc.DwmIpcUnavailable:
                return
            if _selected_confirmed(monitors, xid):
                return
            time.sleep(_FOCUS_CONFIRM_SLEEP)
        logev(logger, logging.INFO, "relocate_focus_unconfirmed",
              "focus selection unconfirmed within budget; proceeding best-effort", xid=xid)

    def _restore_one(self, rec, monitors, conn_to_mon, logger) -> str:
        """Restore ONE displaced record; return "drop" (done OR stale identity).

        Re-resolves ``(pid, starttime)``: a dead OR reused-PID window (identity
        None/mismatch) is dropped WITHOUT any control call -- never touch another
        instance (spike 004 hard rule). Otherwise reads the live client, plans the
        restore, and executes each Action focus-then-act, each wrapped so one
        failed step never aborts the window.
        """
        identity = resolve_pid(rec.xid, self._xreader, proc_root=self._proc_root, logger=logger)
        if identity is None or (identity[0], identity[1]) != (rec.pid, rec.starttime):
            logev(logger, logging.INFO, "relocate_skip_identity",
                  "displaced window identity stale/reused; leaving untouched",
                  pid=rec.pid, xid=rec.xid)
            return "drop"
        client = dwmipc.get_dwm_client(rec.xid, path=self._path, timeout=self._ipc_timeout)
        target = conn_to_mon.get(rec.output)
        live = SimpleNamespace(
            target_monitor=target,
            current_monitor=client["monitor_number"],
            current_floating=bool(client["states"]["is_floating"]),
            n_monitors=len(monitors),
        )
        for action in plan_restore(rec, live):
            try:
                if action.verb == "tagmon":
                    self._tagmon_to_target(rec, action.args, target, len(monitors), logger)
                elif action.verb == "tag":
                    self._focus_and_confirm(rec.xid, logger)
                    dwmipc.run_command("tag", action.args, path=self._path, timeout=self._ipc_timeout)
                elif action.verb == "togglefloating":
                    self._focus_and_confirm(rec.xid, logger)
                    dwmipc.run_command("togglefloating", path=self._path, timeout=self._ipc_timeout)
                elif action.verb == "configure":
                    origin = monitor_origin(monitors, target)
                    if origin is None:
                        # WM-08 fail-safe: without the target monitor origin we
                        # cannot convert the absolute captured geometry to the
                        # monitor-relative coords dwm's configurerequest expects,
                        # so sending it would land the window wrong. Skip the
                        # geometry restore (monitor/tag/floating already applied)
                        # and log, never guess or crash.
                        logev(logger, logging.WARNING, "relocate_geometry_skip",
                              "target monitor origin unresolved; skipping floating "
                              "geometry restore (window kept on target, geometry left "
                              "as dwm placed it)",
                              pid=rec.pid, xid=rec.xid, target=target)
                        continue
                    rel = to_monitor_relative_geometry(action.args, origin)
                    self._focus_and_confirm(rec.xid, logger)
                    self._control.configure_geometry(rec.xid, rel)
            except Exception as e:  # per-step guard is the WM-08 one-failure-never-aborts-window contract
                # One failed step never aborts the window and never crashes.
                logev(logger, logging.WARNING, "relocate_step_fail",
                      "restore step failed; continuing", pid=rec.pid, verb=action.verb, error=str(e))
                continue
        logev(logger, logging.INFO, "relocate_restore",
              "restored displaced window", pid=rec.pid, output=rec.output)
        return "drop"

    def _tagmon_would_crash_dwm(self, xid, direction, logger) -> bool:
        """True iff moving ``xid`` one hop in ``direction`` hits the EXACT dwm crash.

        CRASH-SAFETY GATE (WM-08: never crash dwm). dwm's ``tagmon`` moves the
        selected client to another monitor via ``sendmon`` -> ``focus(NULL)`` ->
        ``arrange(NULL)``. If the move removes the LAST client from the source
        monitor, ``focus(NULL)`` sets ``selmon->sel = NULL`` (``selmon`` stays the
        now-empty source). On dwm builds carrying the "single-window-center"
        layout patch, the subsequent ``arrange`` -> ``tile`` runs
        ``if (n == 1 && selmon->sel->CenterThisWindow) ...`` -- so the SIGSEGV
        needs BOTH (a) the source emptied (NULL ``selmon->sel``) AND (b) SOME
        monitor at exactly ``n == 1`` during that arrange (the buggy line only
        dereferences the NULL when a monitor has a single client). Root cause in
        ``.planning/debug/resolved/tagmon-sigsegv-dwm.md`` (fault at ``tile+384``
        ``cmpl $0x0,0x170(%rdi)``, ``%rdi = selmon->sel = NULL``); precision
        refinement in ``.planning/debug/relocate-guard-precision.md`` (Phase 14).

        The prior guard blocked ANY source-emptying move -- sufficient but not
        necessary, so it wrongly refused the last-window-on-external restore where
        the destination absorbs it and NO monitor lands at ``n == 1`` (provably
        safe on the faithful canary). This computes the POST-MOVE per-monitor
        client counts (source-1, dest+1, others unchanged) where the destination
        is ``dirtomon(source, direction)`` -- the next/previous monitor by ``num``,
        wrapping -- and returns True (UNSAFE, block) IFF the source becomes 0 AND
        any monitor would have exactly 1 client. Otherwise False (safe, allow).

        FAIL SAFE: on ANY IPC failure, malformed reply, an unresolved
        window/monitor, or fewer than two monitors, we return True (refuse the
        move) -- a skipped move never crashes dwm; a wrongly-issued one can. The
        window simply stays where dwm evacuated it (still visible/usable).
        """
        try:
            monitors = dwmipc.get_monitors(path=self._path, timeout=self._ipc_timeout)
        except dwmipc.DwmIpcUnavailable:
            return True
        wid = int(xid)
        counts: dict[int, int] = {}
        source_num: int | None = None
        for m in monitors:
            num = m.get("num")
            clients = m.get("clients")
            allc = clients.get("all") if isinstance(clients, dict) else None
            if not isinstance(num, int) or not isinstance(allc, list):
                return True  # malformed monitor reply -> fail safe
            counts[num] = len(allc)
            if wid in allc:
                source_num = num
        n = len(monitors)
        if source_num is None or n < 2:
            return True  # window not found on any monitor / no destination -> fail safe
        # dirtomon(source, direction): next/previous monitor by num, wrapping. dwm
        # assigns num sequentially (0..n-1) and tagmon walks that ring, so the
        # relative hop lands on (source_num +/- 1) mod n (mirrors tagmon_direction).
        dest_num = (source_num + (1 if direction > 0 else -1)) % n
        if dest_num not in counts:
            return True  # destination monitor unresolved -> fail safe
        post = dict(counts)
        post[source_num] -= 1
        post[dest_num] += 1
        source_emptied = post[source_num] == 0
        any_singleton = any(c == 1 for c in post.values())
        return source_emptied and any_singleton

    def _tagmon_to_target(self, rec, direction, target, n_monitors, logger) -> None:
        """Bounded focus-then-tagmon loop: at most ``n_monitors`` hops.

        Focus precedes EVERY hop (selection drifts), re-reads ``monitor_number``
        after each ``tagmon`` and stops on match; on no-match after the bound logs
        ``relocate_monitor_giveup`` and leaves the window where dwm put it (safe
        default). The target monitor is re-derived from the NEW topology
        (``conn_to_mon`` in the caller), not the stale saved monitor_number.

        CRASH-SAFETY GATE: before EVERY hop we verify this specific tagmon would
        NOT hit the exact dwm crash -- source emptied AND some monitor left at
        ``n == 1`` (see :meth:`_tagmon_would_crash_dwm`). Such a move SIGSEGVs dwm
        builds with the single-window-center patch (NULL ``selmon->sel`` deref in
        ``tile``). If the move is unsafe we SKIP it, log ``relocate_tagmon_unsafe``,
        and leave the window on its current monitor -- degrading gracefully rather
        than crashing dwm. The check is per-hop because each hop's source (and its
        destination = ``dirtomon(source, direction)``) shifts as the client walks
        toward the target.
        """
        for _ in range(n_monitors):
            if self._tagmon_would_crash_dwm(rec.xid, direction, logger):
                logev(logger, logging.WARNING, "relocate_tagmon_unsafe",
                      "skipping tagmon: move would empty the source monitor while "
                      "leaving a monitor at a single client and can crash dwm builds "
                      "with the single-window-center patch; leaving window on its "
                      "current monitor",
                      pid=rec.pid, xid=rec.xid, target=target)
                return
            self._focus_and_confirm(rec.xid, logger)
            dwmipc.run_command("tagmon", direction, path=self._path, timeout=self._ipc_timeout)
            cur = dwmipc.get_dwm_client(rec.xid, path=self._path,
                                        timeout=self._ipc_timeout)["monitor_number"]
            if cur == target:
                return
        logev(logger, logging.WARNING, "relocate_monitor_giveup",
              "tagmon did not reach target monitor within bound; leaving as-is",
              pid=rec.pid, target=target)
