"""Direct, pure-stdlib dwm-ipc client + capability gate (WM-01, WM-02, SEC-01).

This module speaks the mihirlad55/dwm-ipc wire protocol over ``/tmp/dwm.sock``
directly -- NO ``dwm-msg`` subprocess and NO third-party dependency (socket,
struct, json, os, logging only). It is the productionised descendant of the
spike ``.planning/spikes/001-dwm-reality/dwmipc_direct.py``.

Wire protocol -- the header is ``__attribute__((packed))``::

    magic[7]="DWM-IPC" | uint32 size (LE) | uint8 type | JSON payload[size]

Message types: RUN_COMMAND=0 GET_MONITORS=1 GET_TAGS=2 GET_LAYOUTS=3
GET_DWM_CLIENT=4 SUBSCRIBE=5 EVENT=6. Payloads are null-terminated C strings in
BOTH directions and ``size`` INCLUDES the terminator (an empty GET is size 1 =
``b"\\x00"``; size 0 is dwm's "empty message" reject and never a valid reply).

Design seam (mirrors ``RandRReader`` in ``xrandr.py``): transport (connect /
send / recv) is kept separable from parsing. ``pack_header`` / ``parse_header``
/ ``decode_reply`` are PURE functions over bytes so the untrusted-input boundary
(SEC-01) is unit-testable and fuzzable with no real dwm, no X, and no sockets.
Every byte from the untrusted socket is validated here -- magic, hard size cap
(before any allocation), size != 0, and JSON shape -- before any caller sees it.
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import stat
import struct
import time
from typing import Any
from collections.abc import Iterable

from xrandrw.logging_utils import logev

logger = logging.getLogger("xrandrw")

# --- protocol constants ----------------------------------------------------

MAGIC = b"DWM-IPC"
_HDR = struct.Struct("<7sIB")  # packed: 7 magic + uint32 LE size + uint8 type = 12 bytes

# Message-type ints (mihirlad55/dwm-ipc).
RUN_COMMAND = 0
GET_MONITORS = 1
GET_TAGS = 2
GET_LAYOUTS = 3
GET_DWM_CLIENT = 4
SUBSCRIBE = 5
EVENT = 6

# Socket path: defaults to /tmp/dwm.sock, overridable via DWM_SOCKET (matches the binary).
DEFAULT_SOCK_PATH = os.environ.get("DWM_SOCKET", "/tmp/dwm.sock")


class DwmIpcUnavailable(Exception):
    """Raised for EVERY transport or parse failure at the untrusted dwm.sock boundary.

    Callers above :func:`available` treat this as "feature not available this
    cycle" and degrade gracefully -- it is the only exception type allowed to
    escape the wire boundary (SEC-01). No bare ``struct.error`` /
    ``json.JSONDecodeError`` / ``OSError`` may leak past this module.
    """


# --- SEC-01 safety knobs (env-coerced, graceful-degrade-to-default) --------
#
# Modelled on config._coerce_int: a malformed env value falls back to the
# module default and never raises. These are module-local on purpose -- the
# user-facing config key is Phase 11/WM-07; config.py is intentionally untouched.

def _env_float(name: str, default: float, minimum: float) -> float:
    """Read a float from ``os.environ[name]``; fall back to ``default`` gracefully.

    Non-finite inputs (``inf`` / ``-inf`` / ``nan`` / an over-range literal like
    ``1e400`` that ``float()`` rounds to ``inf``) are rejected: an infinite
    timeout would silently defeat the SEC-01 hang guard and later crash
    ``sock.settimeout(inf)`` with ``OverflowError``. Any malformed or non-finite
    value degrades to ``default`` and logs one warning (never raises at import).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw)
        if not math.isfinite(v):
            raise ValueError(f"non-finite value {raw!r}")
        return max(minimum, v)
    except (ValueError, TypeError, OverflowError):
        logev(logger, logging.WARNING, "dwmipc_env_invalid",
              "invalid env value, using default", name=name, value=raw, default=default)
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    """Read an int from ``os.environ[name]``; fall back to ``default`` gracefully.

    Guards the same non-finite hazard as :func:`_env_float`: ``float("1e400")``
    /``"inf"``/``"Infinity"`` yield ``inf`` and ``int(inf)`` raises
    ``OverflowError`` -- uncaught, this crashed ``import xrandrw.dwmipc`` (the
    whole daemon at startup). Non-finite / malformed / over-range values degrade
    to ``default`` with one logged warning and never raise.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        f = float(raw)
        if not math.isfinite(f):
            raise ValueError(f"non-finite value {raw!r}")
        return max(minimum, int(f))
    except (ValueError, TypeError, OverflowError):
        logev(logger, logging.WARNING, "dwmipc_env_invalid",
              "invalid env value, using default", name=name, value=raw, default=default)
        return default


# Socket timeout on every op so a hanging peer cannot block the daemon forever.
DEFAULT_TIMEOUT = _env_float("DWMIPC_TIMEOUT", 1.0, 0.001)
# Hard cap on the reply `size` field, enforced BEFORE any allocation/recv (T-08-01-D).
MAX_REPLY_SIZE = _env_int("DWMIPC_MAX_REPLY", 8 * 1024 * 1024, 1)


# --- pure wire helpers (socket-free) ---------------------------------------

def pack_header(mtype: int, payload_len: int) -> bytes:
    """Pack a 12-byte DWM-IPC header for a message of ``mtype`` with ``payload_len`` bytes."""
    return _HDR.pack(MAGIC, payload_len, mtype)


def parse_header(header: bytes) -> tuple[int, int]:
    """Validate a 12-byte DWM-IPC reply header and return ``(size, rtype)``.

    SEC-01 boundary. Raises :class:`DwmIpcUnavailable` (never ``struct.error``)
    on a truncated header, a wrong magic, a ``size == 0`` "empty message", or a
    ``size`` above :data:`MAX_REPLY_SIZE`. The size cap is enforced HERE, before
    any downstream ``recv``/allocation, so an over-advertised reply can never
    over-allocate (T-08-01-D).
    """
    if len(header) < _HDR.size:
        logev(logger, logging.WARNING, "dwmipc_parse_reject", "truncated reply header",
              reason="truncated", nbytes=len(header))
        raise DwmIpcUnavailable(f"truncated header: {len(header)} < {_HDR.size} bytes")
    magic, size, rtype = _HDR.unpack(header[:_HDR.size])
    if magic != MAGIC:
        logev(logger, logging.WARNING, "dwmipc_parse_reject", "bad reply magic",
              reason="bad_magic")
        raise DwmIpcUnavailable(f"bad reply magic {magic!r}")
    if size == 0:
        logev(logger, logging.WARNING, "dwmipc_parse_reject", "empty message",
              reason="size_zero")
        raise DwmIpcUnavailable("reply size == 0 (dwm empty message)")
    if size > MAX_REPLY_SIZE:
        logev(logger, logging.WARNING, "dwmipc_parse_reject", "oversized reply",
              reason="oversized", size=size, cap=MAX_REPLY_SIZE)
        raise DwmIpcUnavailable(f"reply size {size} exceeds cap {MAX_REPLY_SIZE}")
    return size, rtype


def decode_reply(rtype: int, body: bytes) -> Any:
    """Strip the trailing null terminator, then ``json.loads`` the reply body.

    dwm replies are null-terminated C strings and ``size`` INCLUDES the
    terminator, so it must be stripped before decoding (else json reports
    "Extra data" at the null). An empty / terminator-only body decodes to
    ``None`` without raising. A non-JSON or malformed-JSON body is re-raised as
    :class:`DwmIpcUnavailable` so a ``json.JSONDecodeError`` never leaks to a
    caller (T-08-01-E). This is a PURE function over bytes -- no sockets -- so
    the SEC-01 boundary is directly fuzzable. Shape validation is layered on top
    by :func:`validate_monitors` / :func:`validate_client`.
    """
    data = body.rstrip(b"\x00")
    if not data:
        return None
    try:
        return json.loads(data)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        logev(logger, logging.WARNING, "dwmipc_parse_reject", "reply json decode failed",
              reason="bad_json", rtype=rtype)
        raise DwmIpcUnavailable(f"reply json decode failed: {e}") from e


# --- pure shape validators (SEC-01) ----------------------------------------

def _require_list_of_dicts(obj: Any, required_keys: Iterable[str]) -> list:
    """Return ``obj`` iff it is a non-empty list of dicts each having ``required_keys``."""
    if not isinstance(obj, list) or not obj:
        raise DwmIpcUnavailable(f"expected non-empty list, got {type(obj).__name__}")
    for el in obj:
        if not isinstance(el, dict):
            raise DwmIpcUnavailable(f"expected list of dicts, element is {type(el).__name__}")
        for key in required_keys:
            if key not in el:
                raise DwmIpcUnavailable(f"list element missing required key {key!r}")
    return obj


def _require_dict(obj: Any, required_keys: Iterable[str]) -> dict:
    """Return ``obj`` iff it is a dict having every one of ``required_keys``."""
    if not isinstance(obj, dict):
        raise DwmIpcUnavailable(f"expected dict, got {type(obj).__name__}")
    for key in required_keys:
        if key not in obj:
            raise DwmIpcUnavailable(f"dict missing required key {key!r}")
    return obj


def validate_monitors(obj: Any) -> list:
    """Validate a GET_MONITORS reply: a non-empty list of monitor dicts."""
    return _require_list_of_dicts(obj, ("num", "monitor_geometry"))


def validate_client(obj: Any) -> dict:
    """Validate a GET_DWM_CLIENT reply: a client dict with the expected keys."""
    return _require_dict(obj, ("name", "tags", "geometry", "states"))


# --- socket transport (WM-01) + capability gate (WM-02) --------------------
#
# Seam style mirrors RandRReader (xrandr.py): a fresh AF_UNIX connection per
# call, shares nothing. Every socket op is timed (SEC-01: no unbounded block)
# and every read is bounded (_recvall). The reply size cap lives in parse_header
# and therefore runs BEFORE the body is ever read.

def _preflight_socket(path: str) -> None:
    """Reject a socket ``path`` that is not a current-uid-owned socket (SEC-01).

    ``/tmp/dwm.sock`` lives in world-writable ``/tmp``, where an attacker could
    pre-create a file or a foreign-owned socket at that path before dwm starts.
    This cheap pre-connect guard uses ``os.lstat`` (NOT ``stat`` -- a symlink is
    itself rejected, defeating a symlink-swap) and requires the path to be a real
    socket (:func:`stat.S_ISSOCK`) owned by the current uid. It is defense in
    depth for the local single-user desktop posture, not a substitute for the
    parse-side hardening; the normal case (dwm creates the socket as the same
    user) is unaffected. Raises :class:`DwmIpcUnavailable` on any mismatch so the
    caller degrades gracefully (``available`` -> ``False``) instead of connecting
    to a possibly attacker-preplaced endpoint.
    """
    try:
        st = os.lstat(path)
    except OSError as e:
        raise DwmIpcUnavailable(f"stat {path}: {e}") from e
    if not stat.S_ISSOCK(st.st_mode):
        logev(logger, logging.WARNING, "dwmipc_path_reject", "socket path is not a socket",
              reason="not_socket", path=path)
        raise DwmIpcUnavailable(f"{path} is not a socket")
    if st.st_uid != os.getuid():
        logev(logger, logging.WARNING, "dwmipc_path_reject", "socket path not owned by current uid",
              reason="foreign_owner", path=path, owner=st.st_uid, uid=os.getuid())
        raise DwmIpcUnavailable(f"{path} owned by uid {st.st_uid}, not {os.getuid()}")


def _recvall(sock: socket.socket, n: int, deadline: float | None = None) -> bytes:
    """Read exactly ``n`` bytes from ``sock`` or raise :class:`DwmIpcUnavailable`.

    A per-``recv`` timeout alone is NOT enough: a peer trickling one byte just
    inside the timeout every time resets the budget forever and holds the thread
    open (a slow-trickle DoS). ``deadline`` is a single ``time.monotonic()``
    instant computed once at :func:`request` start; the socket timeout is
    re-armed each loop to the *remaining* budget so TOTAL request wall-time is
    bounded regardless of how the peer paces its bytes. A stalled or closed peer
    still surfaces as :class:`DwmIpcUnavailable` (never ``socket.timeout`` /
    ``OSError``).
    """
    buf = b""
    while len(buf) < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DwmIpcUnavailable("request deadline exceeded during recv")
            sock.settimeout(remaining)
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout as e:
            raise DwmIpcUnavailable("socket timeout during recv") from e
        except OSError as e:
            raise DwmIpcUnavailable(f"socket error during recv: {e}") from e
        if not chunk:
            raise DwmIpcUnavailable("socket closed mid-message")
        buf += chunk
    return buf


def request(mtype: int, payload: bytes | str = b"", *,
            path: str = DEFAULT_SOCK_PATH, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, Any]:
    """Send one DWM-IPC request and return ``(rtype, decoded_body)``.

    Opens a fresh ``AF_UNIX`` ``SOCK_STREAM`` per call, sets ``timeout`` BEFORE
    connect and keeps it for the whole exchange, sends ``pack_header + payload``
    (payload null-terminated; ``size`` INCLUDES the terminator), reads exactly a
    12-byte header, runs :func:`parse_header` (which enforces magic, size!=0, and
    the ``MAX_REPLY_SIZE`` cap before the body is read), then reads exactly
    ``size`` body bytes and hands them to :func:`decode_reply`. Any failure --
    connect error, timeout, short read, bad framing -- raises
    :class:`DwmIpcUnavailable`; nothing else escapes (SEC-01).
    """
    if isinstance(payload, str):
        payload = payload.encode()
    payload = payload + b"\x00"  # size INCLUDES the terminator (empty GET = size 1)
    _preflight_socket(path)  # SEC-01: refuse a non-socket / foreign-owned path before connect
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
    except OSError as e:
        # Close the freshly-created fd on connect failure; otherwise it leaks
        # until GC and emits a ResourceWarning (the socket object was created
        # before connect, so the earlier combined-try left it dangling).
        sock.close()
        raise DwmIpcUnavailable(f"connect {path}: {e}") from e
    # Single monotonic deadline for the whole exchange so a byte-trickling peer
    # cannot hold the thread open past `timeout` by resetting the per-recv budget.
    deadline = time.monotonic() + timeout
    try:
        sock.sendall(pack_header(mtype, len(payload)) + payload)
        size, rtype = parse_header(_recvall(sock, _HDR.size, deadline))
        body = _recvall(sock, size, deadline)
    except socket.timeout as e:
        raise DwmIpcUnavailable("socket timeout during request") from e
    except OSError as e:
        # A peer that RSTs/aborts mid-exchange surfaces as BrokenPipeError /
        # ConnectionResetError on sendall/recv; mirror subscribe() and wrap it
        # so no raw OSError leaks past the SEC-01 boundary.
        raise DwmIpcUnavailable(f"socket error during request: {e}") from e
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return rtype, decode_reply(rtype, body)


def available(path: str = DEFAULT_SOCK_PATH, *, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """WM-02 capability gate: ``True`` iff a live dwm-ipc endpoint answers. NEVER raises.

    Connects, sends GET_MONITORS, and validates a non-empty monitor list. ANY
    failure -- missing socket, ECONNREFUSED, timeout, bad magic, parse/shape
    error -- degrades to ``False`` (feature silently OFF, existing layout
    behavior untouched), logging one ``dwmipc_unavailable`` event. This is the
    graceful-degrade ethos of watch.py's ``xlib_connect_fail``.
    """
    try:
        # request() returns (rtype, body); validate the body ([1]), not the tuple.
        validate_monitors(request(GET_MONITORS, path=path, timeout=timeout)[1])
        return True
    except Exception as e:
        logev(logger, logging.INFO, "dwmipc_unavailable",
              "dwm-ipc endpoint unavailable; window-mgmt feature disabled",
              path=path, reason=type(e).__name__)
        return False


# --- public verbs (WM-01) --------------------------------------------------
#
# Thin wrappers over request() + the 08-01 validators. request() returns a
# (rtype, body) tuple; each verb indexes [1] BEFORE validating so a validator
# never runs on the tuple (plan-checker note 2).

def get_monitors(path: str = DEFAULT_SOCK_PATH, *, timeout: float = DEFAULT_TIMEOUT) -> list:
    """Return the validated non-empty list of monitor dicts from GET_MONITORS."""
    return validate_monitors(request(GET_MONITORS, path=path, timeout=timeout)[1])


def get_dwm_client(win: int, path: str = DEFAULT_SOCK_PATH, *,
                   timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return the validated client dict for window ``win`` from GET_DWM_CLIENT.

    The payload key is ``client_window_id`` and the value MUST be a JSON int
    (confirmed via strace in spike 001); ``win`` is coerced with ``int()``. A
    non-numeric ``win`` is a caller error re-raised as :class:`DwmIpcUnavailable`
    so a raw ``ValueError`` never escapes this module's single boundary type.
    """
    try:
        wid = int(win)
    except (TypeError, ValueError) as e:
        raise DwmIpcUnavailable(f"invalid window id {win!r}: {e}") from e
    payload = json.dumps({"client_window_id": wid})
    return validate_client(request(GET_DWM_CLIENT, payload, path=path, timeout=timeout)[1])


def run_command(name: str, *args: int, path: str = DEFAULT_SOCK_PATH,
                timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Run a dwm command ``name`` with strictly int-typed ``args``.

    dwm's arg types are strictly typed for UINT/SINT -- a string arg makes dwm
    return ``{"result":"error","reason":"Type mismatch"}`` -- so every positional
    arg is coerced with ``int()`` before framing. A non-numeric arg is a caller
    error re-raised as :class:`DwmIpcUnavailable` (consistent single escape type),
    never a raw ``ValueError``. Returns the decoded result.
    """
    try:
        int_args = [int(a) for a in args]
    except (TypeError, ValueError) as e:
        raise DwmIpcUnavailable(f"invalid command arg: {e}") from e
    payload = json.dumps({"command": str(name), "args": int_args})
    return request(RUN_COMMAND, payload, path=path, timeout=timeout)[1]


def subscribe(path: str = DEFAULT_SOCK_PATH, *, timeout: float = DEFAULT_TIMEOUT) -> socket.socket:
    """Establish the SUBSCRIBE transport and return the live, still-open socket.

    TRANSPORT ONLY in this phase: opens a fresh AF_UNIX connection, sends a
    SUBSCRIBE header + payload, and returns the open socket for a later phase to
    consume. It does NOT read or loop over EVENT messages -- the EVENT-stream
    consumption loop and the focus-then-act control sequencing are deferred to
    the Phase 10 relocation lifecycle. Raises :class:`DwmIpcUnavailable` if the
    connection or send fails.
    """
    payload = b"\x00"  # size INCLUDES the terminator; concrete event payload is Phase 10
    _preflight_socket(path)  # SEC-01: refuse a non-socket / foreign-owned path before connect
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
    except OSError as e:
        sock.close()  # close the fd on connect failure (no ResourceWarning leak)
        raise DwmIpcUnavailable(f"subscribe connect {path}: {e}") from e
    try:
        sock.sendall(pack_header(SUBSCRIBE, len(payload)) + payload)
    except OSError as e:
        try:
            sock.close()
        except OSError:
            pass
        raise DwmIpcUnavailable(f"subscribe send: {e}") from e
    return sock
