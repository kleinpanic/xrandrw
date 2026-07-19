"""Margin coverage for relocate.py degrade/branch paths (headless: no X, no dwm).

TEST-03 top-up: drives the currently-uncovered best-effort/degrade lines of
``relocate.py`` -- the ``_selected_confirmed`` fallback, ``_safe_close`` swallow,
the settle-time X-read failure, the ``_safe_capture`` keep-previous-snapshot path,
the whole-cycle DwmIpc abandon, the not-in-returned skip, the focus-confirm
transient-unavailable early-return, the focus-unconfirmed timeout log, and the
per-step restore-failure swallow. Every test injects fakes and asserts the
degrade behavior (log fires, no exception escapes); NO product code changes and
NO runtime behavior is exercised beyond the existing hardened error paths. These
lift relocate.py comfortably above the 90% floor.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import xrandrw.relocate as relocate
from xrandrw.relocate import _safe_close, _selected_confirmed


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


class _FakeFocusControl:
    def __init__(self):
        self.focused = []
        self.configured = []

    def focus(self, xid):
        self.focused.append(xid)
        return True

    def configure_geometry(self, xid, geom):
        self.configured.append((xid, dict(geom)))
        return True


def _coord(**over):
    kw = dict(control=_FakeFocusControl(), reader=SimpleNamespace(),
              xreader=SimpleNamespace(), sock_path="/x", proc_root="/proc")
    kw.update(over)
    return relocate.RelocationCoordinator(**kw)


def _has(caplog, event):
    return any(getattr(r, "event", None) == event for r in caplog.records)


# --- _selected_confirmed: is_selected true vs. fallback vs. no-match ---------

def test_selected_confirmed_true_when_selected_monitor():
    monitors = [{"clients": {"selected": 42}, "is_selected": True}]
    assert _selected_confirmed(monitors, 42) is True


def test_selected_confirmed_fallback_without_is_selected():
    # sel matches but the monitor is not the selected one -> fallback True (line 77).
    monitors = [{"clients": {"selected": 42}, "is_selected": False}]
    assert _selected_confirmed(monitors, 42) is True


def test_selected_confirmed_false_when_no_client_match():
    monitors = [{"clients": {"selected": 99}, "is_selected": True}]
    assert _selected_confirmed(monitors, 42) is False


# --- _safe_close: swallow close error, no-op on None ------------------------

def test_safe_close_swallows_close_error():
    class _D:
        def close(self):
            raise RuntimeError("close boom")

    _safe_close(_D())  # must not raise


def test_safe_close_none_is_noop():
    _safe_close(None)


# --- on_settled: settle-time X read failure degrades (log + early return) ----

def test_on_settled_read_fail_degrades(monkeypatch, caplog, logger):
    monkeypatch.setattr(relocate.dwmipc, "available", lambda path=None, **kw: True)

    class _RaisingReader:
        def read(self, logger=None):
            raise RuntimeError("x read boom")

    coord = _coord(reader=_RaisingReader())
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        coord.on_settled({}, logger)
    assert _has(caplog, "relocate_read_fail")
    assert coord._prev_present is None  # returned before seeding


# --- _safe_capture: a failed capture keeps the previous snapshot -------------

def test_safe_capture_failure_keeps_prev_snapshot(caplog, logger):
    def _raising_capture(**kw):
        raise RuntimeError("capture boom")

    coord = _coord(capture=_raising_capture)
    coord._snapshot = {("prev",): "keep"}
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        out = coord._safe_capture(logger)
    assert out == {("prev",): "keep"}
    assert _has(caplog, "relocate_capture_fail")


# --- _restore_returned: DwmIpc unavailable at cycle entry abandons the cycle --

def test_restore_returned_abandons_on_ipc_unavailable(monkeypatch, caplog, logger):
    coord = _coord()
    coord._displaced = {(1, 2): SimpleNamespace(output="DP-1", pid=1, starttime=2, xid=5)}

    def _boom(**kw):
        raise relocate.dwmipc.DwmIpcUnavailable("endpoint gone")

    monkeypatch.setattr(relocate.dwmipc, "get_monitors", _boom)
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        coord._restore_returned({"DP-1"}, {}, logger)
    assert _has(caplog, "relocate_cycle_abandon")
    assert (1, 2) in coord._displaced  # left displaced for a later cycle


# --- _restore_returned: a displaced record whose output did not return is skipped

def test_restore_returned_skips_record_output_not_returned(monkeypatch, logger):
    coord = _coord()
    coord._displaced = {(1, 2): SimpleNamespace(output="DP-OTHER", pid=1, starttime=2, xid=5)}
    monkeypatch.setattr(relocate.dwmipc, "get_monitors", lambda **kw: [{}])
    monkeypatch.setattr(relocate, "match_dwm_monitor_to_output",
                        lambda mons, outs, logger=None: {})
    coord._restore_returned({"DP-1"}, {}, logger)
    assert (1, 2) in coord._displaced  # output not in returned -> untouched


# --- _focus_and_confirm: transient DwmIpc mid-poll returns early -------------

def test_focus_and_confirm_ipc_unavailable_returns(monkeypatch, logger):
    control = _FakeFocusControl()
    coord = _coord(control=control)

    def _boom(**kw):
        raise relocate.dwmipc.DwmIpcUnavailable("mid-poll gone")

    monkeypatch.setattr(relocate.dwmipc, "get_monitors", _boom)
    coord._focus_and_confirm(5, logger)  # returns quietly, no raise
    assert 5 in control.focused


# --- _focus_and_confirm: selection never confirmed -> unconfirmed log --------

def test_focus_and_confirm_unconfirmed_logs(monkeypatch, caplog, logger):
    monkeypatch.setattr(relocate, "_FOCUS_CONFIRM_SLEEP", 0)
    control = _FakeFocusControl()
    coord = _coord(control=control)
    monkeypatch.setattr(relocate.dwmipc, "get_monitors", lambda **kw: [])  # never confirms
    with caplog.at_level(logging.INFO, logger="xrandrw"):
        coord._focus_and_confirm(5, logger)
    assert _has(caplog, "relocate_focus_unconfirmed")


# --- _restore_one: togglefloating branch + a failing step is swallowed -------

def test_restore_one_togglefloating_and_step_fail_swallow(monkeypatch, caplog, logger):
    control = _FakeFocusControl()
    coord = _coord(control=control)
    rec = SimpleNamespace(xid=5, pid=1, starttime=2, tags=4, is_floating=True,
                          geometry={"x": 0, "y": 0, "width": 1, "height": 1}, output="DP-1")
    monkeypatch.setattr(relocate, "resolve_pid",
                        lambda xid, xreader, proc_root=None, logger=None: (1, 2, "a"))
    monkeypatch.setattr(relocate.dwmipc, "get_dwm_client",
                        lambda xid, path=None, **kw: {"monitor_number": 0,
                                                      "states": {"is_floating": False}})
    # focus_and_confirm's poll confirms immediately (no sleep budget spent).
    monkeypatch.setattr(relocate.dwmipc, "get_monitors",
                        lambda **kw: [{"clients": {"selected": 5}, "is_selected": True}])

    def _raising_run(name, *a, **kw):
        raise RuntimeError(f"{name} boom")

    monkeypatch.setattr(relocate.dwmipc, "run_command", _raising_run)
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        # monitor 0 origin (0,0): the monitor-relative transform is a no-op here,
        # so the sent geometry equals the captured absolute geometry (see the
        # cross-monitor case in test_relocate_lifecycle for a non-zero origin).
        monitors = [{"num": 0,
                     "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080}}]
        result = coord._restore_one(rec, monitors=monitors, conn_to_mon={"DP-1": 0}, logger=logger)
    assert result == "drop"
    # tag + togglefloating run_command both raised and were swallowed per-step.
    assert _has(caplog, "relocate_step_fail")
    # configure went through the control seam (not run_command) and applied.
    assert (5, {"x": 0, "y": 0, "width": 1, "height": 1}) in control.configured
