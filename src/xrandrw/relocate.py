"""Hotplug relocation lifecycle: put displaced windows back (Phase 10, WM-05/WM-06/WM-08).

On an output unplug dwm auto-evacuates the removed monitor's clients to a
surviving monitor; on replug this module restores each displaced window to where
it was FOR THE SAME PROCESS -- focus, tagmon back to the restored monitor, tag,
floating-state, and (only for floating windows) the saved geometry. Tiled windows
get monitor+tag+floating-state but NO geometry write and are NEVER converted.

Besides the productionised :mod:`xrandrw.dwmipc` verbs, this module is the ONLY
place in the codebase that MUTATES window state. The mutation surface is kept
small and independently verifiable: the live-X mutations go through the thin,
mockable :class:`RelocationControl` seam (mirrors ``windows.WindowXReader`` /
``xrandr.RandRReader``: own Display per call, never raises past the seam), and
the ordering/bounding logic lives in the PURE, headless-testable helpers
``tagmon_direction`` and ``plan_restore``.
"""
from __future__ import annotations

import logging
from collections import namedtuple

from Xlib import X, display
from Xlib.protocol import event

from xrandrw.logging_utils import logev

# Module logger; the seam is the only place that touches a live Display, so its
# degrade events log through this shared "xrandrw" logger (mirrors the codebase).
_LOG = logging.getLogger("xrandrw")

# One restore delta. ``verb`` is one of "tagmon" | "tag" | "togglefloating" |
# "configure"; ``args`` is the verb argument (a tagmon direction int, a tag
# bitmask int, None for togglefloating, or a geometry dict for configure). The
# coordinator (Plan 03) inserts a focus() before EVERY verb -- focus is NOT an
# Action here because plan_restore is pure and focus is a live-X side effect.
Action = namedtuple("Action", "verb args")


def _safe_close(d) -> None:
    if d is not None:
        try:
            d.close()
        except Exception:
            pass


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
        """Absolute-position a FLOATING window via X ConfigureWindow.

        Restores a saved floating geometry ``{x,y,width,height}`` (spike 003:
        dwm honors the ConfigureWindow for floating clients). Returns True on
        send; on any Xlib error logs ``relocate_configure_fail`` and returns
        False (never raises). The coordinator only ever calls this for floating
        records -- tiled windows are re-tiled by dwm and never get a geometry
        write.
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

def tagmon_direction(cur_num: int, target_num: int, n_monitors: int) -> "int | None":
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


def plan_restore(record, live) -> "list[Action]":
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
    actions: "list[Action]" = []
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
