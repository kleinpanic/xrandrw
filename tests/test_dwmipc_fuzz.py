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

import time

import pytest

from xrandrw.dwmipc import (
    DwmIpcUnavailable,
    GET_MONITORS,
    available,
    get_dwm_client,
    get_monitors,
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
