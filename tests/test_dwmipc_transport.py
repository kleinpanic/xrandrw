"""Transport + capability-gate tests for dwmipc, driven by the fake AF_UNIX server.

Everything here runs headless (no real dwm, no X): a real ``FakeDwmServer`` binds
a socket under ``tmp_path`` and the production ``request()`` / ``available()``
talk to it. Covers WM-01 transport round-trip, WM-02 gate semantics, and the
SEC-01 timeout / bounded-read / size-cap hardening.
"""
from __future__ import annotations

import json
import socket
import time

import pytest

from xrandrw.dwmipc import DwmIpcUnavailable, GET_MONITORS, available, get_monitors, request
from dwmipc_fake_server import MAGIC, _HDR, FakeDwmServer


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "dwm.sock"


# --- fixture self-check (plan-checker note #1): raw round-trip, no dwmipc ---

def test_fake_server_self_roundtrip(sock_path):
    with FakeDwmServer(sock_path, mode="auto"):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect(str(sock_path))
        c.sendall(_HDR.pack(MAGIC, 1, GET_MONITORS) + b"\x00")
        header = c.recv(_HDR.size)
        magic, size, rtype = _HDR.unpack(header)
        assert magic == MAGIC
        assert rtype == GET_MONITORS
        body = b""
        while len(body) < size:
            body += c.recv(size - len(body))
        obj = json.loads(body.rstrip(b"\x00"))
        assert isinstance(obj, list) and obj and "num" in obj[0]
        c.close()


# --- WM-01 transport round-trip --------------------------------------------

def test_request_roundtrips_monitors(sock_path):
    with FakeDwmServer(sock_path, mode="auto"):
        rtype, data = request(GET_MONITORS, path=str(sock_path))
    assert rtype == GET_MONITORS
    assert isinstance(data, list) and data and data[0]["num"] == 0


# --- SEC-01 transport hardening --------------------------------------------

def test_request_missing_socket_raises_dwmipc_not_oserror(tmp_path):
    missing = tmp_path / "nope.sock"
    with pytest.raises(DwmIpcUnavailable):
        request(GET_MONITORS, path=str(missing))


def test_request_hang_is_time_bounded(sock_path):
    with FakeDwmServer(sock_path, mode="hang"):
        start = time.monotonic()
        with pytest.raises(DwmIpcUnavailable):
            request(GET_MONITORS, path=str(sock_path), timeout=0.2)
        elapsed = time.monotonic() - start
    # Proves no unbounded block: returns within a small multiple of the timeout.
    assert elapsed < 2.0


def test_request_oversized_rejected_before_body_read(sock_path):
    with FakeDwmServer(sock_path, mode="oversized"):
        with pytest.raises(DwmIpcUnavailable):
            request(GET_MONITORS, path=str(sock_path), timeout=0.5)


def test_request_close_mid_message_raises_dwmipc(sock_path):
    with FakeDwmServer(sock_path, mode="close_mid_message"):
        with pytest.raises(DwmIpcUnavailable):
            request(GET_MONITORS, path=str(sock_path), timeout=0.5)


def test_request_rst_on_accept_raises_dwmipc_not_oserror(sock_path):
    # Server RSTs the connection right after accept() (SO_LINGER 0), before
    # reading. The send/recv OSError (BrokenPipeError/ConnectionResetError) must
    # be wrapped as DwmIpcUnavailable, never leak as a raw OSError.
    with FakeDwmServer(sock_path, mode="rst_on_accept"):
        with pytest.raises(DwmIpcUnavailable):
            request(GET_MONITORS, path=str(sock_path), timeout=0.5)


def test_get_monitors_rst_on_accept_raises_dwmipc_not_oserror(sock_path):
    with FakeDwmServer(sock_path, mode="rst_on_accept"):
        with pytest.raises(DwmIpcUnavailable):
            get_monitors(path=str(sock_path), timeout=0.5)


def test_connect_failure_closes_socket_no_resourcewarning(tmp_path):
    # A UNIX socket that is bound but never listen()s refuses connect with
    # ECONNREFUSED -- exercising the connect-failure path AFTER the fd is created.
    # If request() does not close the fd, CPython emits a ResourceWarning when the
    # orphaned socket is finalized. Assert none is emitted.
    import gc
    import warnings

    dead_path = str(tmp_path / "dead.sock")
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(dead_path)  # bound but not listening -> connect refused
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(DwmIpcUnavailable):
                request(GET_MONITORS, path=dead_path, timeout=0.5)
            gc.collect()  # force finalization of any leaked socket object
        leaked = [w for w in caught if issubclass(w.category, ResourceWarning)]
        assert not leaked, [str(w.message) for w in leaked]
    finally:
        dead.close()


# --- WM-02 capability gate --------------------------------------------------

def test_available_true_on_valid_endpoint(sock_path):
    with FakeDwmServer(sock_path, mode="auto"):
        assert available(path=str(sock_path)) is True


def test_available_false_on_missing_socket(tmp_path):
    missing = tmp_path / "nope.sock"
    assert available(path=str(missing)) is False  # must not raise


def test_available_false_on_hostile_endpoint(sock_path):
    with FakeDwmServer(sock_path, mode="wrong_magic"):
        assert available(path=str(sock_path)) is False  # must not raise


def test_available_false_on_non_list_reply(sock_path):
    with FakeDwmServer(sock_path, mode="wrong_schema"):
        assert available(path=str(sock_path)) is False


def test_available_never_raises_on_hang(sock_path):
    with FakeDwmServer(sock_path, mode="hang"):
        # Even a stalled peer degrades to False within the (short) timeout.
        start = time.monotonic()
        assert available(path=str(sock_path), timeout=0.2) is False
        assert time.monotonic() - start < 2.0
