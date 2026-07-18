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


# --------------------------------------------------------------------------
# Pure /proc parsing helpers (X-free, socket-free; injectable proc_root)
# --------------------------------------------------------------------------

def parse_starttime_from_stat(stat_text: str) -> int:
    """Return field 22 (starttime) of a ``/proc/<pid>/stat`` line.

    The line is ``pid (comm) state ppid ...`` where ``comm`` may itself contain
    spaces and parentheses, so the split is anchored on the LAST ``')'``. In the
    whitespace-split remainder the state char is element 0, so proc(5) field 22
    (starttime) is element 19. Raises ``ValueError`` on a structurally
    unparseable line so the caller's try/except turns it into a skip.
    """
    close = stat_text.rindex(")")  # ValueError if no ')' present
    rest = stat_text[close + 1:].split()
    if len(rest) < 20:
        raise ValueError("stat line too short to contain starttime (field 22)")
    return int(rest[19])


def read_proc_comm(pid: int, proc_root: str = "/proc") -> "str | None":
    """Read ``<proc_root>/<pid>/comm`` (trailing newline stripped) or ``None``."""
    try:
        with open(f"{proc_root}/{pid}/comm", "r") as f:
            return f.read().rstrip("\n")
    except OSError:
        return None


def read_proc_cmdline(pid: int, proc_root: str = "/proc") -> "str | None":
    """Read ``<proc_root>/<pid>/cmdline`` with NUL separators turned into spaces.

    ``/proc/<pid>/cmdline`` is a NUL-separated (and NUL-terminated) argv blob.
    Replace the separators with spaces, strip, and return ``None`` on any
    ``OSError`` or when the result is empty (e.g. kernel threads).
    """
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    return text or None


def read_proc_identity(pid: int, proc_root: str = "/proc",
                       logger: "logging.Logger | None" = None) -> "tuple[int, int, str] | None":
    """Return ``(pid, starttime, comm)`` for ``pid`` or ``None`` (logged skip).

    On ANY failure (missing dir, dead pid, unparseable stat) log a
    ``window_proc_missing`` event and return ``None`` -- a dead process is a
    skip, never fatal (matches the dwmipc graceful-degrade ethos).
    """
    try:
        with open(f"{proc_root}/{pid}/stat", "r") as f:
            stat_text = f.read()
        starttime = parse_starttime_from_stat(stat_text)
        comm = read_proc_comm(pid, proc_root)
        if comm is None:
            raise ValueError("comm unreadable")
        return (int(pid), starttime, comm)
    except (OSError, ValueError) as e:
        logev(logger or _LOG, logging.DEBUG, "window_proc_missing",
              "process /proc entry missing or unparseable", pid=pid, error=str(e))
        return None


def resolve_pid(xid, reader, *, hostname: "str | None" = None,
                proc_root: str = "/proc",
                logger: "logging.Logger | None" = None) -> "tuple[int, int, str] | None":
    """Resolve a dwm client window ``xid`` to LOCAL ``(pid, starttime, comm)``.

    WM-03 entry point. ``_NET_WM_PID`` is the PRIMARY pid; XRes
    ``res_query_client_ids`` is the fallback when the property is absent. A
    ``WM_CLIENT_MACHINE`` whose stripped value differs from ``hostname`` (default
    ``socket.gethostname()``) is skipped -- a remote client's PID is meaningless
    locally (decision D of 09-CONTEXT). Any failure logs and returns ``None`` so
    resolving one window never crashes the capture loop.
    """
    lg = logger or _LOG
    try:
        if hostname is None:
            hostname = socket.gethostname()
        machine = reader.client_machine(xid)
        if machine and machine.strip() and machine.strip() != hostname:
            logev(lg, logging.INFO, "window_skip_nonlocal",
                  "window client-machine is non-local; skipping",
                  xid=xid, machine=machine.strip(), hostname=hostname)
            return None
        pid = reader.net_wm_pid(xid)
        if not pid:
            pid = reader.xres_pid(xid)
        if not pid:
            return None
        identity = read_proc_identity(pid, proc_root, logger=lg)
        if identity is not None:
            logev(lg, logging.DEBUG, "window_pid_resolve",
                  "resolved window to local process",
                  xid=xid, pid=identity[0], comm=identity[2])
        return identity
    except Exception as e:  # a single window must never crash capture
        logev(lg, logging.WARNING, "window_resolve_fail",
              "unexpected error resolving window identity", xid=xid, error=str(e))
        return None


# --------------------------------------------------------------------------
# Pure dwm-monitor <-> output geometry matcher (no X, no sockets, no dwm)
# --------------------------------------------------------------------------

def match_dwm_monitor_to_output(dwm_monitors, outputs,
                                logger: "logging.Logger | None" = None):
    """Map each dwm ``monitor_number`` to an xrandrw connector by geometry.

    ``dwm_monitors`` is the validated ``get_monitors()`` list (each dict has
    ``num`` and ``monitor_geometry{x,y,width,height}``); ``outputs`` is the
    ``{connector: Output}`` mapping from ``RandRReader().read()``. For each
    monitor, find the single CONNECTED output whose ``position == (mg.x, mg.y)``
    AND ``current_mode == (mg.width, mg.height)``; map ``num -> connector``.
    Zero or MORE-THAN-ONE matches map ``num -> None`` and log a
    ``window_monitor_unmatched`` event with the raw geometry -- never
    guess-associate (decision D of 09-CONTEXT).
    """
    lg = logger or _LOG
    result: "dict[int, str | None]" = {}
    for mon in dwm_monitors:
        num = mon.get("num")
        mg = mon.get("monitor_geometry") or {}
        want_pos = (mg.get("x"), mg.get("y"))
        want_mode = (mg.get("width"), mg.get("height"))
        matches = [
            name for name, o in outputs.items()
            if o.connected and o.position == want_pos and o.current_mode == want_mode
        ]
        if len(matches) == 1:
            result[num] = matches[0]
        else:
            result[num] = None
            logev(lg, logging.INFO, "window_monitor_unmatched",
                  "no confident single output match for dwm monitor",
                  monitor=num, geometry=mg, candidates=len(matches))
    return result
