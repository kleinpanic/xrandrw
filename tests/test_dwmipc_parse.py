"""Socket-free unit + parse-boundary negative tests for the dwm-ipc pure layer.

These exercise the SEC-01 untrusted-input boundary (magic, size cap, size!=0,
truncation, JSON shape) entirely without sockets or a real dwm/X, mirroring the
"parsing is a pure function over bytes" seam in src/xrandrw/dwmipc.py.
"""
from __future__ import annotations

import struct

import pytest

from xrandrw import dwmipc
from xrandrw.dwmipc import (
    DwmIpcUnavailable,
    GET_MONITORS,
    MAGIC,
    pack_header,
    parse_header,
)


# --- pack/parse round-trip -------------------------------------------------

def test_pack_header_layout():
    hdr = pack_header(GET_MONITORS, 5)
    assert len(hdr) == 12
    assert hdr[:7] == MAGIC
    size, rtype = struct.unpack("<IB", hdr[7:])
    assert size == 5
    assert rtype == GET_MONITORS


def test_pack_parse_roundtrip():
    size, rtype = parse_header(pack_header(3, 42))
    assert size == 42
    assert rtype == 3


# --- parse_header reject paths (SEC-01) ------------------------------------

def test_parse_header_bad_magic():
    bad = struct.pack("<7sIB", b"NOPE-IP", 10, GET_MONITORS)
    with pytest.raises(DwmIpcUnavailable):
        parse_header(bad)


def test_parse_header_size_zero():
    # dwm's "empty message" case is never a valid reply.
    hdr = struct.pack("<7sIB", MAGIC, 0, GET_MONITORS)
    with pytest.raises(DwmIpcUnavailable):
        parse_header(hdr)


def test_parse_header_oversized_rejected_before_allocation():
    hdr = struct.pack("<7sIB", MAGIC, dwmipc.MAX_REPLY_SIZE + 1, GET_MONITORS)
    with pytest.raises(DwmIpcUnavailable):
        parse_header(hdr)


def test_parse_header_at_cap_is_allowed():
    hdr = struct.pack("<7sIB", MAGIC, dwmipc.MAX_REPLY_SIZE, GET_MONITORS)
    size, _ = parse_header(hdr)
    assert size == dwmipc.MAX_REPLY_SIZE


def test_parse_header_truncated_raises_dwmipc_not_struct_error():
    for junk in (b"", b"DWM", b"DWM-IPC", b"DWM-IPC\x01\x00\x00"):
        try:
            parse_header(junk)
        except DwmIpcUnavailable:
            continue
        except struct.error:
            pytest.fail("struct.error leaked from parse_header on truncated input")
        else:
            pytest.fail("truncated header did not raise DwmIpcUnavailable")


# --- env-coerced safety knobs (graceful coerce) ----------------------------

def test_env_float_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("DWMIPC_TIMEOUT", raising=False)
    assert dwmipc._env_float("DWMIPC_TIMEOUT", 1.0, 0.001) == 1.0


def test_env_float_reads_override(monkeypatch):
    monkeypatch.setenv("DWMIPC_TIMEOUT", "2.5")
    assert dwmipc._env_float("DWMIPC_TIMEOUT", 1.0, 0.001) == 2.5


def test_env_float_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DWMIPC_TIMEOUT", "not-a-number")
    assert dwmipc._env_float("DWMIPC_TIMEOUT", 1.0, 0.001) == 1.0


def test_env_int_reads_override(monkeypatch):
    monkeypatch.setenv("DWMIPC_MAX_REPLY", "1024")
    assert dwmipc._env_int("DWMIPC_MAX_REPLY", 8 * 1024 * 1024, 1) == 1024


def test_env_int_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DWMIPC_MAX_REPLY", "garbage")
    assert dwmipc._env_int("DWMIPC_MAX_REPLY", 4096, 1) == 4096
