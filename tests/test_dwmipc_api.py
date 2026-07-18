"""Public-verb tests for dwmipc, driven headless by the fake AF_UNIX server.

Each of the four WM-01 verbs (get_monitors / get_dwm_client / run_command /
subscribe) is round-tripped against ``FakeDwmServer`` with no real dwm/X. The
run_command test also inspects the bytes the server actually received to prove
the args are framed as JSON ints (never strings) so dwm never returns a Type
mismatch.
"""
from __future__ import annotations

import json
import socket
import time

import pytest

from xrandrw.dwmipc import (
    DwmIpcUnavailable,
    RUN_COMMAND,
    SUBSCRIBE,
    get_dwm_client,
    get_monitors,
    run_command,
    subscribe,
)
from dwmipc_fake_server import FakeDwmServer


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "dwm.sock"


def _wait_for(pred, deadline=2.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# --- get_monitors -----------------------------------------------------------

def test_get_monitors_returns_validated_list(sock_path):
    with FakeDwmServer(sock_path, mode="auto"):
        mons = get_monitors(path=str(sock_path))
    assert isinstance(mons, list) and mons
    assert mons[0]["num"] == 0
    assert "monitor_geometry" in mons[0]


# --- get_dwm_client ---------------------------------------------------------

def test_get_dwm_client_returns_validated_dict(sock_path):
    with FakeDwmServer(sock_path, mode="auto") as srv:
        client = get_dwm_client(0x1400001, path=str(sock_path))
    assert isinstance(client, dict)
    assert {"name", "tags", "geometry", "states"} <= client.keys()
    # payload key is client_window_id and the value MUST be a JSON int.
    _rtype, _size, payload = srv.received[-1]
    sent = json.loads(payload.rstrip(b"\x00"))
    assert sent == {"client_window_id": 0x1400001}
    assert isinstance(sent["client_window_id"], int)


def test_get_dwm_client_coerces_window_to_int(sock_path):
    with FakeDwmServer(sock_path, mode="auto") as srv:
        get_dwm_client("209715201", path=str(sock_path))  # numeric string
    sent = json.loads(srv.received[-1][2].rstrip(b"\x00"))
    assert sent["client_window_id"] == 209715201
    assert isinstance(sent["client_window_id"], int)


# --- run_command ------------------------------------------------------------

def test_run_command_roundtrips_and_frames_int_args(sock_path):
    with FakeDwmServer(sock_path, mode="run_command") as srv:
        result = run_command("tagmon", 1, path=str(sock_path))
    assert result == {"result": "success"}
    rtype, _size, payload = srv.received[-1]
    assert rtype == RUN_COMMAND
    sent = json.loads(payload.rstrip(b"\x00"))
    assert sent["command"] == "tagmon"
    assert sent["args"] == [1]
    # Strictly JSON ints, never strings (avoids dwm "Type mismatch").
    for a in sent["args"]:
        assert isinstance(a, int) and not isinstance(a, bool)


def test_run_command_coerces_numeric_args_to_int(sock_path):
    with FakeDwmServer(sock_path, mode="run_command") as srv:
        run_command("view", "2", 3, path=str(sock_path))
    sent = json.loads(srv.received[-1][2].rstrip(b"\x00"))
    assert sent["args"] == [2, 3]
    for a in sent["args"]:
        assert isinstance(a, int)


# --- subscribe (transport only) ---------------------------------------------

def test_subscribe_returns_open_socket_without_consuming_events(sock_path):
    # Against a server that never sends an EVENT, subscribe must still return an
    # open socket promptly -- proving it does not loop reading events here.
    with FakeDwmServer(sock_path, mode="hang") as srv:
        start = time.monotonic()
        sock = subscribe(path=str(sock_path))
        elapsed = time.monotonic() - start
        try:
            assert isinstance(sock, socket.socket)
            assert sock.fileno() != -1  # still open
            assert elapsed < 1.0  # did not block on an event read
            assert _wait_for(lambda: any(r[0] == SUBSCRIBE for r in srv.received))
        finally:
            sock.close()


def test_subscribe_missing_socket_raises_dwmipc(tmp_path):
    with pytest.raises(DwmIpcUnavailable):
        subscribe(path=str(tmp_path / "nope.sock"))
