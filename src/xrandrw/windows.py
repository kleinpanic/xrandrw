"""Window identity + state capture (Phase 9, WM-03/WM-04).

Resolve every dwm client window to its owning LOCAL process identity
``(pid, starttime)`` and capture its window state, associating each record to
the output/EDID xrandrw already tracks.

Design mirrors ``xrandr.py::RandRReader``: a thin, main-thread-only live Xlib
seam (:class:`WindowXReader`) that opens its own ``Display`` per call and shares
nothing, plus pure, X-free/socket-free helpers (``/proc`` parsers,
``match_dwm_monitor_to_output``, ``build_record``) that are unit-testable
headless. READ/model ONLY -- no window movement or control here.
"""
from __future__ import annotations

import logging
import socket

from Xlib import X, display
from Xlib.ext import res

from xrandrw.logging_utils import logev

# Module logger; the seam is the only place that touches a live Display, so its
# degrade events log through this shared "xrandrw" logger (mirrors the codebase).
_LOG = logging.getLogger("xrandrw")

# X-Resource identification mask selecting the local PID of a client (proven in
# spike 002; equals ``res.LocalClientPIDMask`` == 2).
_LOCAL_CLIENT_PID_MASK = res.LocalClientPIDMask


class WindowXReader:
    """Thin main-thread-only live Xlib seam mirroring ``RandRReader``.

    Every method opens its OWN ``display.Display()``, shares no state, and closes
    it in a ``finally`` block (Xlib's Display is not thread-safe). No method ever
    raises past the seam: any Xlib error is logged via ``logev`` and the method
    returns ``None`` so a single bad window never propagates out. This class is
    the ONLY place that touches a live Display; all resolution logic consumes it
    through the seam so tests inject a fake reader with no X server.
    """

    def __init__(self) -> None:
        # Degrade-once flag so a missing X-Resource extension logs a single
        # notice per reader rather than once per window.
        self._xres_absent_logged = False

    def net_wm_pid(self, xid):
        """Return the ``_NET_WM_PID`` CARDINAL of ``xid`` as a positive int.

        PRIMARY identity path (decision D of 09-CONTEXT). Returns ``None`` when
        the property is absent, empty, or non-positive.
        """
        d = None
        try:
            d = display.Display()
            atom = d.get_atom("_NET_WM_PID")
            win = d.create_resource_object("window", xid)
            prop = win.get_full_property(atom, X.AnyPropertyType)
            if prop is None or not prop.value:
                return None
            pid = int(prop.value[0])
            return pid if pid > 0 else None
        except Exception as e:  # never propagate out of the seam
            logev(_LOG, logging.WARNING, "window_pid_prop_fail",
                  "reading _NET_WM_PID failed", xid=xid, error=str(e))
            return None
        finally:
            _safe_close(d)

    def client_machine(self, xid):
        """Return ``WM_CLIENT_MACHINE`` of ``xid`` as a str, or ``None``."""
        d = None
        try:
            d = display.Display()
            atom = d.get_atom("WM_CLIENT_MACHINE")
            win = d.create_resource_object("window", xid)
            prop = win.get_full_property(atom, X.AnyPropertyType)
            if prop is None or not prop.value:
                return None
            raw = prop.value
            if isinstance(raw, bytes):
                text = raw.decode("utf-8", "replace")
            else:
                # python-xlib may hand back an array of ints for 8-bit props
                text = bytes(raw).decode("utf-8", "replace")
            return text.rstrip("\x00")
        except Exception as e:
            logev(_LOG, logging.WARNING, "window_machine_fail",
                  "reading WM_CLIENT_MACHINE failed", xid=xid, error=str(e))
            return None
        finally:
            _safe_close(d)

    def has_xres(self, d=None) -> bool:
        """Return True only when the X-Resource extension is present.

        On any error return False and log a ``window_xres_absent`` degrade event
        ONCE per reader. A missing XRes must never crash -- it degrades the
        caller to ``_NET_WM_PID``-only.
        """
        own = d is None
        try:
            if own:
                d = display.Display()
            return bool(d.has_extension(res.extname))
        except Exception as e:
            if not self._xres_absent_logged:
                self._xres_absent_logged = True
                logev(_LOG, logging.INFO, "window_xres_absent",
                      "X-Resource extension unavailable; _NET_WM_PID only",
                      error=str(e))
            return False
        finally:
            if own:
                _safe_close(d)

    def xres_pid(self, xid):
        """Return the local PID of ``xid`` via XRes, or ``None``.

        Guards availability via :meth:`has_xres` first; any Xlib error logs a
        ``window_xres_degrade`` event and returns ``None`` (fall back to
        property-only). This is the FALLBACK path when ``_NET_WM_PID`` is absent.
        """
        d = None
        try:
            d = display.Display()
            if not self.has_xres(d):
                return None
            reply = d.res_query_client_ids(
                [{"client": int(xid), "mask": _LOCAL_CLIENT_PID_MASK}])
            for idv in getattr(reply, "ids", []) or []:
                spec = getattr(idv, "spec", None)
                mask = getattr(spec, "mask", None) if spec is not None else None
                if mask == _LOCAL_CLIENT_PID_MASK and idv.value:
                    pid = int(idv.value[0])
                    return pid if pid > 0 else None
            return None
        except Exception as e:
            logev(_LOG, logging.WARNING, "window_xres_degrade",
                  "XRes res_query_client_ids failed; falling back", xid=xid, error=str(e))
            return None
        finally:
            _safe_close(d)


def _safe_close(d) -> None:
    if d is not None:
        try:
            d.close()
        except Exception:
            pass
