"""SEC-01 negative / fuzz matrix for the untrusted dwm.sock boundary.

Two layers of evidence that only :class:`DwmIpcUnavailable` ever escapes the
wire boundary -- the client never crashes, never hangs, never over-allocates:

1. Transport matrix (Task 1): every hostile reply mode of the real
   :class:`FakeDwmServer` (truncated header, oversized size, wrong magic,
   size==0, non-JSON, wrong-schema JSON, mid-message close, hang) plus the
   missing-socket case, asserting ``DwmIpcUnavailable`` and/or
   ``available() is False`` and that the hang is time-bounded.

2. Direct pure-helper fuzzing (Task 2): deterministic adversarial bytes fed
   straight into ``parse_header`` / ``decode_reply``, asserting they only ever
   return a validated/None result or raise ``DwmIpcUnavailable`` -- never
   ``struct.error`` / ``ValueError`` / ``json.JSONDecodeError`` / ``MemoryError``.

Coverage measurement (Phase 12 will enforce a >=90% CI gate via
``--cov-fail-under``; this phase measures/aims but does NOT add the dependency)::

    python -m pytest tests/test_dwmipc_*.py --cov=xrandrw.dwmipc --cov-report=term-missing

The plain-pytest run below does NOT depend on pytest-cov.
"""
from __future__ import annotations

import random
import struct
import time

import pytest

from xrandrw import dwmipc
from xrandrw.dwmipc import (
    DwmIpcUnavailable,
    GET_MONITORS,
    _HDR,
    available,
    decode_reply,
    get_dwm_client,
    get_monitors,
    parse_header,
    request,
)
from dwmipc_fake_server import FakeDwmServer

# Deliberately tiny so the hang cases keep the suite fast.
_T = 0.25


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "dwm.sock"


# --- Task 1: transport hostile matrix --------------------------------------

# Modes where the corruption is in the framing/transport itself: request()
# raises DwmIpcUnavailable before or during the body read.
_TRANSPORT_HOSTILE = [
    "truncated_header",
    "oversized",
    "wrong_magic",
    "size_zero",
    "non_json",
    "close_mid_message",
]


@pytest.mark.parametrize("mode", _TRANSPORT_HOSTILE)
def test_transport_hostile_request_raises_and_available_false(mode, sock_path):
    with FakeDwmServer(sock_path, mode=mode):
        with pytest.raises(DwmIpcUnavailable):
            request(GET_MONITORS, path=str(sock_path), timeout=_T)
        # The capability gate degrades to False on every hostile endpoint.
        assert available(path=str(sock_path), timeout=_T) is False


def test_wrong_schema_verb_raises_and_available_false(sock_path):
    # decode_reply is shape-agnostic, so request() decodes the bare int without
    # raising -- but the shape-validating verbs (and available()) raise/return
    # False at the boundary. Only DwmIpcUnavailable escapes.
    with FakeDwmServer(sock_path, mode="wrong_schema"):
        _rtype, body = request(GET_MONITORS, path=str(sock_path), timeout=_T)
        assert not isinstance(body, (list, dict))  # bare int slipped through decode
        with pytest.raises(DwmIpcUnavailable):
            get_monitors(path=str(sock_path), timeout=_T)
        with pytest.raises(DwmIpcUnavailable):
            get_dwm_client(0x1, path=str(sock_path), timeout=_T)
        assert available(path=str(sock_path), timeout=_T) is False


def test_hang_is_time_bounded_and_available_false(sock_path):
    with FakeDwmServer(sock_path, mode="hang"):
        start = time.monotonic()
        assert available(path=str(sock_path), timeout=_T) is False
        assert time.monotonic() - start < 2.0  # no unbounded block

        start = time.monotonic()
        with pytest.raises(DwmIpcUnavailable):
            request(GET_MONITORS, path=str(sock_path), timeout=_T)
        assert time.monotonic() - start < 2.0


def test_missing_socket_request_raises_and_available_false(tmp_path):
    missing = str(tmp_path / "nope.sock")
    assert available(path=missing, timeout=_T) is False  # never raises
    with pytest.raises(DwmIpcUnavailable):
        request(GET_MONITORS, path=missing, timeout=_T)


# --- Task 2: direct pure-helper fuzzing ------------------------------------
#
# Only DwmIpcUnavailable (or a clean return/None) may result. Anything else --
# struct.error, ValueError, json.JSONDecodeError, MemoryError, or any other
# class -- is a test failure (T-08-04-E). Randomness is seeded for reproducibility.


def _only_dwmipc(fn):
    """Run ``fn``; pass iff it returns or raises DwmIpcUnavailable; fail on any other exc."""
    try:
        fn()
    except DwmIpcUnavailable:
        return
    except pytest.fail.Exception:  # let an inner pytest.fail propagate
        raise
    except BaseException as exc:  # noqa: BLE001 -- that's the whole point of the assertion
        pytest.fail(f"unexpected {type(exc).__name__} escaped the boundary: {exc!r}")


def test_parse_header_fuzz_random_bytes_only_dwmipc():
    rng = random.Random(1337)
    for _ in range(3000):
        n = rng.choice([0, 1, 5, 11, 12, 13, 20, rng.randint(0, 40)])
        data = bytes(rng.randrange(256) for _ in range(n))
        _only_dwmipc(lambda d=data: parse_header(d))


def test_parse_header_fuzz_structured_headers_only_dwmipc():
    rng = random.Random(4242)
    edge_sizes = [0, 1, dwmipc.MAX_REPLY_SIZE, dwmipc.MAX_REPLY_SIZE + 1]
    for _ in range(3000):
        magic = bytes(rng.randrange(256) for _ in range(7))
        size = rng.choice(edge_sizes + [rng.randint(0, 2**32 - 1)]) & 0xFFFFFFFF
        typ = rng.randint(0, 255)
        hdr = struct.pack("<7sIB", magic, size, typ)

        def call(h=hdr):
            res = parse_header(h)
            # A clean return must be a well-formed (size, rtype) pair.
            assert isinstance(res, tuple) and len(res) == 2

        _only_dwmipc(call)


def test_parse_header_edge_sizes_at_and_over_cap():
    at_cap = struct.pack("<7sIB", dwmipc.MAGIC, dwmipc.MAX_REPLY_SIZE, GET_MONITORS)
    assert parse_header(at_cap) == (dwmipc.MAX_REPLY_SIZE, GET_MONITORS)
    over_cap = struct.pack("<7sIB", dwmipc.MAGIC, dwmipc.MAX_REPLY_SIZE + 1, GET_MONITORS)
    with pytest.raises(DwmIpcUnavailable):
        parse_header(over_cap)


def test_decode_reply_fuzz_random_bodies_only_dwmipc():
    rng = random.Random(2024)
    for _ in range(4000):
        n = rng.randint(0, 64)
        body = bytes(rng.randrange(256) for _ in range(n))
        _only_dwmipc(lambda b=body: decode_reply(rng.randint(0, 6), b))


def test_decode_reply_fuzz_malformed_json_only_dwmipc():
    rng = random.Random(99)
    fragments = [b"{", b"[", b"null", b"123", b'"x', b"{'a':1}", b"", b"[1,2", b"true", b"NaN"]
    for _ in range(3000):
        junk = bytes(rng.randrange(256) for _ in range(rng.randint(0, 8)))
        body = rng.choice(fragments) + junk + b"\x00"
        _only_dwmipc(lambda b=body: decode_reply(1, b))


def test_hdr_size_is_twelve():
    # Guards the wire contract the whole fuzz suite assumes.
    assert _HDR.size == 12
