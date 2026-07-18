"""Socket-free unit + parse-boundary negative tests for the dwm-ipc pure layer.

These exercise the SEC-01 untrusted-input boundary (magic, size cap, size!=0,
truncation, JSON shape) entirely without sockets or a real dwm/X, mirroring the
"parsing is a pure function over bytes" seam in src/xrandrw/dwmipc.py.
"""
from __future__ import annotations

import json
import struct

import pytest

from xrandrw import dwmipc
from xrandrw.dwmipc import (
    DwmIpcUnavailable,
    GET_DWM_CLIENT,
    GET_MONITORS,
    MAGIC,
    decode_reply,
    pack_header,
    parse_header,
    validate_client,
    validate_monitors,
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


# --- decode_reply: null-strip + JSON decode (SEC-01) -----------------------

_VALID_MONITORS = [{"num": 0, "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}}]
_VALID_CLIENT = {
    "name": "term",
    "tags": 1,
    "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
    "states": {"is_floating": False},
}


def _framed(obj) -> bytes:
    # dwm replies are null-terminated C strings; size INCLUDES the terminator.
    return json.dumps(obj).encode() + b"\x00"


def test_decode_reply_strips_null_and_json_decodes():
    body = _framed(_VALID_MONITORS)
    assert body.endswith(b"\x00")
    assert decode_reply(GET_MONITORS, body) == _VALID_MONITORS


def test_decode_reply_empty_body_is_none():
    assert decode_reply(GET_MONITORS, b"") is None


def test_decode_reply_terminator_only_body_is_none():
    assert decode_reply(GET_MONITORS, b"\x00") is None


def test_decode_reply_non_json_raises_dwmipc():
    with pytest.raises(DwmIpcUnavailable):
        decode_reply(GET_MONITORS, b"\xff\xfe not json at all \x00")


def test_decode_reply_does_not_leak_jsondecodeerror():
    try:
        decode_reply(GET_MONITORS, b"{not: valid json,,,}\x00")
    except DwmIpcUnavailable:
        pass
    except json.JSONDecodeError:
        pytest.fail("json.JSONDecodeError leaked from decode_reply")


# --- shape validators (SEC-01) ---------------------------------------------

def test_validate_monitors_accepts_valid_list():
    assert validate_monitors(_VALID_MONITORS) == _VALID_MONITORS


def test_validate_monitors_rejects_empty_list():
    with pytest.raises(DwmIpcUnavailable):
        validate_monitors([])


def test_validate_monitors_rejects_bare_int():
    with pytest.raises(DwmIpcUnavailable):
        validate_monitors(5)


def test_validate_monitors_rejects_list_of_non_dicts():
    with pytest.raises(DwmIpcUnavailable):
        validate_monitors([1, 2, 3])


def test_validate_monitors_rejects_missing_keys():
    with pytest.raises(DwmIpcUnavailable):
        validate_monitors([{"num": 0}])  # missing monitor_geometry


def test_validate_client_accepts_valid_dict():
    assert validate_client(_VALID_CLIENT) == _VALID_CLIENT


def test_validate_client_rejects_list_where_dict_required():
    with pytest.raises(DwmIpcUnavailable):
        validate_client([_VALID_CLIENT])


def test_validate_client_rejects_missing_keys():
    with pytest.raises(DwmIpcUnavailable):
        validate_client({"name": "x", "tags": 1})  # missing geometry/states
