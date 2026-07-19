"""WM-07 SC3: the read-only ``--window-state`` diagnostic degrades cleanly.

Covers the three paths of ``cli.window_state``:
  * OFF -- feature disabled: reason present, capture NEVER invoked, exit 0;
  * UNAVAILABLE -- enabled but no dwm-ipc endpoint: reason present, exit 0;
  * AVAILABLE-WITH-RECORDS -- enabled + endpoint: captured lists the record dicts.

Every path must return 0 and print valid JSON carrying the stable
``{enabled, dwmipc_available, captured, displaced}`` schema, never crashing.
"""
from __future__ import annotations

import json
import logging

import pytest

import xrandrw.cli as cli
from xrandrw.windows import WindowRecord


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


def _fake_record(pid=4321, output="DP-1"):
    return WindowRecord(
        xid=0x1400001, pid=pid, starttime=765, comm="terminal",
        cmdline="terminal --login", output=output, edid="edidAAA",
        monitor_number=0, tags=7, is_floating=True, is_fullscreen=False,
        geometry={"x": 10, "y": 20, "width": 800, "height": 600},
    )


def test_window_state_off_does_not_capture(monkeypatch, capsys, logger):
    def _boom(*a, **k):
        pytest.fail("capture_windows must NOT be called when feature is off")

    monkeypatch.setattr(cli, "capture_windows", _boom)
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: False)

    rc = cli.window_state({"WINDOW_MANAGEMENT": "0"}, logger)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["enabled"] is False
    assert out["captured"] == []
    assert out["displaced"] == []
    assert "reason" in out


def test_window_state_unavailable(monkeypatch, capsys, logger):
    def _boom(*a, **k):
        pytest.fail("capture_windows must NOT be called with no dwm-ipc endpoint")

    monkeypatch.setattr(cli, "capture_windows", _boom)
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: False)

    rc = cli.window_state({"WINDOW_MANAGEMENT": "1"}, logger)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["enabled"] is True
    assert out["dwmipc_available"] is False
    assert out["captured"] == []
    assert out["displaced"] == []
    assert "reason" in out


def test_window_state_available_with_records(monkeypatch, capsys, logger):
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: True)
    monkeypatch.setattr(cli, "capture_windows",
                        lambda **k: [_fake_record(pid=4321, output="DP-1"),
                                     _fake_record(pid=5555, output="DP-2")])

    rc = cli.window_state({"WINDOW_MANAGEMENT": "1"}, logger)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["enabled"] is True
    assert out["dwmipc_available"] is True
    assert out["displaced"] == []
    assert len(out["captured"]) == 2
    by_pid = {r["pid"]: r for r in out["captured"]}
    assert by_pid[4321]["output"] == "DP-1"
    assert by_pid[5555]["output"] == "DP-2"
    assert by_pid[4321]["tags"] == 7


def test_window_state_capture_error_degrades(monkeypatch, capsys, logger):
    # An unexpected capture error must not crash; captured stays [] and rc is 0.
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: True)

    def _explode(**k):
        raise RuntimeError("capture boom")

    monkeypatch.setattr(cli, "capture_windows", _explode)

    rc = cli.window_state({"WINDOW_MANAGEMENT": "1"}, logger)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["captured"] == []
    assert out["displaced"] == []
