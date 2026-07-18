"""End-to-end functional test of the Phase-9 capture pipeline, headless.

Wires the whole pipeline together with NO live X and NO real dwm:
  * a REAL ``AF_UNIX`` :class:`FakeDwmServer` speaking the DWM-IPC protocol,
  * a mocked Xlib ``WindowXReader``/``RandRReader`` seam,
  * a ``tmp_path`` fake ``/proc`` directory,
driving ``capture_windows`` and asserting resolved identity + captured state +
output/EDID association together (WM-03 + WM-04). Also closes coverage gaps on
``windows.py`` toward the Phase-12 >=90% gate (TEST-03).
"""
from __future__ import annotations

import logging
import socket
import time
from types import SimpleNamespace

import pytest

import xrandrw.windows as win_mod
from xrandrw.windows import WindowXReader, capture_windows
from xrandrw.xrandr import Output
from dwmipc_fake_server import FakeDwmServer


HOST = "func-test-host"


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "dwm.sock"


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")  # module logger, so seam events are captured
    lg.setLevel(logging.DEBUG)
    return lg


def _wait_for(pred, deadline=2.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# Two monitors, each owning one distinct client xid.
_MONITORS = [
    {"num": 0, "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
     "layout": {"symbol": "[]="}, "clients": {"all": [0x1400001]}},
    {"num": 1, "monitor_geometry": {"x": 1920, "y": 0, "width": 1920, "height": 1080},
     "layout": {"symbol": "[]="}, "clients": {"all": [0x1400002]}},
]

# Real nested geometry.current shape (spike 001) so build_record's nested path runs.
_CLIENT = {
    "name": "terminal", "tags": 7, "monitor_number": 0,
    "geometry": {"current": {"x": 10, "y": 20, "width": 800, "height": 600}},
    "states": {"is_floating": True, "is_fullscreen": False},
}


def _outputs():
    return {
        "DP-1": Output(name="DP-1", connected=True, current_mode=(1920, 1080),
                       position=(0, 0), edid_sha1="edidAAA"),
        "DP-2": Output(name="DP-2", connected=True, current_mode=(1920, 1080),
                       position=(1920, 0), edid_sha1="edidBBB"),
    }


def _fake_randr(outs):
    return SimpleNamespace(read=lambda logger=None: dict(outs))


def _fake_xreader(machine_for):
    """machine_for: callable xid -> WM_CLIENT_MACHINE string (local == HOST)."""
    return SimpleNamespace(
        net_wm_pid=lambda xid: 1234,
        client_machine=machine_for,
        xres_pid=lambda xid: None,
        has_xres=lambda: True,
    )


def _make_proc(tmp_path, pid=1234, comm="terminal app", starttime=765):
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    after = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime)] + ["0", "0"]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after) + "\n")
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"terminal\x00--login\x00")
    return str(tmp_path / "proc")


def test_functional_happy_path(sock_path, tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path)
    # read_edids would open a live Display; edids are already set on the outputs.
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    with FakeDwmServer(sock_path, mode="auto", monitors=_MONITORS, client=_CLIENT):
        recs = capture_windows(
            reader=_fake_randr(_outputs()),
            xreader=_fake_xreader(lambda xid: HOST),
            proc_root=proc_root, hostname=HOST,
            sock_path=str(sock_path), logger=logger,
        )

    assert len(recs) == 2
    by_out = {r.output: r for r in recs}
    assert set(by_out) == {"DP-1", "DP-2"}

    r = by_out["DP-1"]
    # WM-03: resolved local identity from the fake /proc
    assert (r.pid, r.starttime, r.comm) == (1234, 765, "terminal app")
    assert r.cmdline == "terminal --login"
    # WM-04: captured state
    assert r.tags == 7
    assert r.is_floating is True and r.is_fullscreen is False
    assert r.geometry == {"x": 10, "y": 20, "width": 800, "height": 600}
    # WM-04: association to the connector + EDID for the monitor it sits on
    assert r.edid == "edidAAA"
    assert by_out["DP-2"].edid == "edidBBB"


def test_functional_nonlocal_skip(sock_path, tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    # xid 0x1400001 (monitor 0 / DP-1) is remote; 0x1400002 (DP-2) is local.
    def machine_for(xid):
        return "remote.example.net" if xid == 0x1400001 else HOST

    with FakeDwmServer(sock_path, mode="auto", monitors=_MONITORS, client=_CLIENT):
        with caplog.at_level(logging.DEBUG, logger="xrandrw"):
            recs = capture_windows(
                reader=_fake_randr(_outputs()),
                xreader=_fake_xreader(machine_for),
                proc_root=proc_root, hostname=HOST,
                sock_path=str(sock_path), logger=logger,
            )

    assert len(recs) == 1
    assert recs[0].output == "DP-2"
    assert any(getattr(r, "event", None) == "window_skip_nonlocal" for r in caplog.records)


def test_functional_degrade_to_empty(sock_path, tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    # A server that RSTs on accept makes get_monitors raise DwmIpcUnavailable.
    with FakeDwmServer(sock_path, mode="rst_on_accept"):
        recs = capture_windows(
            reader=_fake_randr(_outputs()),
            xreader=_fake_xreader(lambda xid: HOST),
            proc_root=proc_root, hostname=HOST,
            sock_path=str(sock_path), logger=logger,
        )
    assert recs == []
