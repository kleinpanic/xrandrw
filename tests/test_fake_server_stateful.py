"""Prove the stateful fake dwm server mutates per-window state over the REAL wire.

Drives the stateful :class:`FakeDwmServer` through the production
``xrandrw.dwmipc`` client (mirrors tests/test_windows_functional.py wiring): a
``sock_path`` under tmp_path, ``with FakeDwmServer(...) as srv``. Confirms
GET_MONITORS grouping, GET_DWM_CLIENT reflection, and run_command
tagmon/tag/togglefloating mutation of the SELECTED client -- plus a guard that
the existing ``auto`` mode is unregressed.
"""
from __future__ import annotations

import logging
import time

import pytest

from xrandrw import dwmipc
from dwmipc_fake_server import FakeDwmServer


A = 0x1400001
B = 0x1400002


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "dwm.sock"


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


def _wait_for(pred, deadline=2.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _seed_clients():
    # xid A on monitor 0 (tags 1, tiled, geom Ga); xid B on monitor 1 (tags 2, floating, geom Gb).
    return [
        {"xid": A, "name": "term-a", "tags": 1, "monitor_number": 0,
         "geometry": {"current": {"x": 5, "y": 6, "width": 100, "height": 200}},
         "states": {"is_floating": False, "is_fullscreen": False}},
        {"xid": B, "name": "term-b", "tags": 2, "monitor_number": 1,
         "geometry": {"current": {"x": 1930, "y": 40, "width": 300, "height": 400}},
         "states": {"is_floating": True, "is_fullscreen": False}},
    ]


def test_get_monitors_groups_clients_by_monitor(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()):
        mons = dwmipc.get_monitors(path=str(sock_path))
    assert len(mons) == 2
    by_num = {m["num"]: m for m in mons}
    assert by_num[0]["clients"]["all"] == [A]
    assert by_num[1]["clients"]["all"] == [B]
    assert by_num[1]["monitor_geometry"] == {"x": 1920, "y": 0, "width": 1920, "height": 1080}


def test_get_dwm_client_reflects_seed(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()):
        ca = dwmipc.get_dwm_client(A, path=str(sock_path))
        cb = dwmipc.get_dwm_client(B, path=str(sock_path))
    assert ca["tags"] == 1 and ca["monitor_number"] == 0
    assert ca["states"]["is_floating"] is False
    assert ca["geometry"]["current"] == {"x": 5, "y": 6, "width": 100, "height": 200}
    assert cb["tags"] == 2 and cb["monitor_number"] == 1
    assert cb["states"]["is_floating"] is True


def test_run_command_tagmon_moves_selected_client(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()) as srv:
        srv.select(A)
        dwmipc.run_command("tagmon", 1, path=str(sock_path))
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        # A fresh client read agrees with the server's post-state.
        assert dwmipc.get_dwm_client(A, path=str(sock_path))["monitor_number"] == 1
        # B untouched (not selected).
        assert srv.state(B)["monitor_number"] == 1


def test_run_command_tag_sets_tags(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()) as srv:
        srv.select(A)
        dwmipc.run_command("tag", 4, path=str(sock_path))
        assert _wait_for(lambda: srv.state(A)["tags"] == 4)
        assert dwmipc.get_dwm_client(A, path=str(sock_path))["tags"] == 4


def test_run_command_togglefloating_flips_state(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()) as srv:
        srv.select(A)
        assert srv.state(A)["is_floating"] is False
        dwmipc.run_command("togglefloating", path=str(sock_path))
        assert _wait_for(lambda: srv.state(A)["is_floating"] is True)


def test_set_geometry_visible_through_client(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()) as srv:
        srv.set_geometry(A, {"x": 11, "y": 22, "width": 333, "height": 444})
        c = dwmipc.get_dwm_client(A, path=str(sock_path))
    assert c["geometry"]["current"] == {"x": 11, "y": 22, "width": 333, "height": 444}


def test_run_command_no_selection_is_noop(sock_path):
    with FakeDwmServer(sock_path, mode="stateful", clients=_seed_clients()) as srv:
        # Nothing selected -> dwm no-op; state unchanged.
        dwmipc.run_command("tag", 8, path=str(sock_path))
        time.sleep(0.05)
        assert srv.state(A)["tags"] == 1
        assert srv.state(B)["tags"] == 2


def test_auto_mode_unregressed(sock_path):
    # Import-parity guard: the canned "auto" mode still returns the canned shapes.
    with FakeDwmServer(sock_path, mode="auto") as srv:  # noqa: F841
        mons = dwmipc.get_monitors(path=str(sock_path))
        client = dwmipc.get_dwm_client(0x1400001, path=str(sock_path))
    assert isinstance(mons, list) and mons and "num" in mons[0]
    assert client["name"] == "terminal"
