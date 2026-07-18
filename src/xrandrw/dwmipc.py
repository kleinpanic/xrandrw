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
import os
import struct
from typing import Any, Iterable, Optional, Tuple

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
    """Read a float from ``os.environ[name]``; fall back to ``default`` gracefully."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, float(raw))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int, minimum: int) -> int:
    """Read an int from ``os.environ[name]``; fall back to ``default`` gracefully."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(float(raw)))
    except (ValueError, TypeError):
        return default


# Socket timeout on every op so a hanging peer cannot block the daemon forever.
DEFAULT_TIMEOUT = _env_float("DWMIPC_TIMEOUT", 1.0, 0.001)
# Hard cap on the reply `size` field, enforced BEFORE any allocation/recv (T-08-01-D).
MAX_REPLY_SIZE = _env_int("DWMIPC_MAX_REPLY", 8 * 1024 * 1024, 1)


# --- pure wire helpers (socket-free) ---------------------------------------

def pack_header(mtype: int, payload_len: int) -> bytes:
    """Pack a 12-byte DWM-IPC header for a message of ``mtype`` with ``payload_len`` bytes."""
    return _HDR.pack(MAGIC, payload_len, mtype)


def parse_header(header: bytes) -> Tuple[int, int]:
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
