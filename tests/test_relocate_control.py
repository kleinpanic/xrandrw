"""Unit tests for the Phase-10 relocation primitives (headless: no X, no dwm).

Covers the mockable :class:`RelocationControl` seam (own-Display-per-call,
never-raise-past-seam, both happy and Xlib-error paths) and the PURE planning
helpers ``tagmon_direction`` / ``plan_restore`` (bounded fewest-hop direction +
the tiled-vs-floating restore-delta guarantee).
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import xrandrw.relocate as relocate
from xrandrw.relocate import Action, RelocationControl, plan_restore, tagmon_direction


# ---------------------------------------------------------------------------
# Fake Display that records sent events + configure calls (mirrors the fake in
# tests/test_windows_functional.py) and asserts the own-Display-closed invariant.
# ---------------------------------------------------------------------------

class _FakeWin:
    def __init__(self, xid, disp):
        self.id = xid
        self._disp = disp

    def __index__(self):
        # The Xlib ClientMessage window field packs to an int at construction.
        return int(self.id) if isinstance(self.id, int) else 0

    def send_event(self, ev, event_mask=0):
        self._disp.sent.append((ev, event_mask))

    def configure(self, **kw):
        self._disp.configured.append((self.id, kw))


class _FakeXDisplay:
    def __init__(self, *, fail=frozenset()):
        self.closed = False
        self.flushed = False
        self.sent = []
        self.configured = []
        self.last_atom = None
        self._fail = fail

    def screen(self):
        return SimpleNamespace(root=_FakeWin("root", self))

    def intern_atom(self, name):
        if "intern_atom" in self._fail:
            raise RuntimeError("atom boom")
        self.last_atom = name
        return 4242

    def create_resource_object(self, kind, xid):
        if "create" in self._fail:
            raise RuntimeError("create boom")
        return _FakeWin(xid, self)

    def flush(self):
        if "flush" in self._fail:
            raise RuntimeError("flush boom")
        self.flushed = True

    def close(self):
        self.closed = True


def _patch_display(monkeypatch, fake):
    monkeypatch.setattr(relocate.display, "Display", lambda: fake)
    return fake


# --- RelocationControl.focus -----------------------------------------------

def test_focus_sends_active_window_and_closes(monkeypatch):
    fake = _patch_display(monkeypatch, _FakeXDisplay())
    assert RelocationControl().focus(0x1400001) is True
    assert fake.closed, "seam must close its own Display"
    assert fake.flushed
    assert fake.last_atom == "_NET_ACTIVE_WINDOW"
    assert len(fake.sent) == 1
    ev, mask = fake.sent[0]
    assert ev.client_type == 4242
    assert ev.window.id == 0x1400001, "clientmessage targets the requested xid"


def test_focus_xlib_error_returns_false_and_logs(monkeypatch, caplog):
    fake = _patch_display(monkeypatch, _FakeXDisplay(fail={"create"}))
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        assert RelocationControl().focus(0x1400001) is False
    assert fake.closed, "Display still closed on error"
    assert any(getattr(r, "event", None) == "relocate_focus_fail" for r in caplog.records)


# --- RelocationControl.configure_geometry ----------------------------------

def test_configure_geometry_configures_and_closes(monkeypatch):
    fake = _patch_display(monkeypatch, _FakeXDisplay())
    geom = {"x": 140, "y": 120, "width": 700, "height": 500}
    assert RelocationControl().configure_geometry(0x1400002, geom) is True
    assert fake.closed and fake.flushed
    assert fake.configured == [(0x1400002, {"x": 140, "y": 120, "width": 700, "height": 500})]


def test_configure_geometry_xlib_error_returns_false_and_logs(monkeypatch, caplog):
    fake = _patch_display(monkeypatch, _FakeXDisplay(fail={"create"}))
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        assert RelocationControl().configure_geometry(0x1, {"x": 0, "y": 0, "width": 1, "height": 1}) is False
    assert fake.closed
    assert any(getattr(r, "event", None) == "relocate_configure_fail" for r in caplog.records)


# ---------------------------------------------------------------------------
# tagmon_direction: fewest-hop wraparound direction with deterministic tie-break
# ---------------------------------------------------------------------------

def test_tagmon_direction_already_on_target_is_none():
    assert tagmon_direction(0, 0, 2) is None


def test_tagmon_direction_single_hop_two_monitors():
    assert tagmon_direction(0, 1, 2) in (1, -1)


def test_tagmon_direction_backward_wrap_shorter():
    # 0 -> 3 on 4 monitors: forward 3 hops vs backward 1 hop -> -1.
    assert tagmon_direction(0, 3, 4) == -1


def test_tagmon_direction_tie_breaks_positive():
    # 1 -> 3 on 4 monitors: forward 2 == backward 2 -> deterministic +1.
    assert tagmon_direction(1, 3, 4) == 1


def test_tagmon_direction_out_of_range_or_too_few_monitors():
    assert tagmon_direction(0, 5, 4) is None       # target out of range
    assert tagmon_direction(0, -1, 4) is None       # target out of range
    assert tagmon_direction(0, 1, 1) is None        # < 2 monitors
    assert tagmon_direction(0, 0, 0) is None


# ---------------------------------------------------------------------------
# plan_restore: ordered restore deltas + the tiled-vs-floating guarantee
# ---------------------------------------------------------------------------

def _live(*, target_monitor, current_monitor, current_floating, n_monitors=2):
    return SimpleNamespace(target_monitor=target_monitor, current_monitor=current_monitor,
                           current_floating=current_floating, n_monitors=n_monitors)


def _rec(*, tags=4, is_floating=False, geometry=None):
    return SimpleNamespace(tags=tags, is_floating=is_floating,
                           geometry=geometry or {"x": 10, "y": 20, "width": 800, "height": 600})


def test_plan_restore_floating_moved_matching_float():
    rec = _rec(tags=4, is_floating=True, geometry={"x": 1, "y": 2, "width": 3, "height": 4})
    live = _live(target_monitor=1, current_monitor=0, current_floating=True, n_monitors=2)
    actions = plan_restore(rec, live)
    assert actions == [
        Action("tagmon", 1),
        Action("tag", 4),
        Action("configure", {"x": 1, "y": 2, "width": 3, "height": 4}),
    ]
    # No togglefloating because current_floating already matches record.is_floating.
    assert not any(a.verb == "togglefloating" for a in actions)


def test_plan_restore_tiled_on_correct_monitor_tag_only():
    rec = _rec(tags=2, is_floating=False)
    live = _live(target_monitor=0, current_monitor=0, current_floating=False)
    actions = plan_restore(rec, live)
    assert actions == [Action("tag", 2)]
    # THE tiled guarantee: never a geometry write, never a floating conversion.
    assert not any(a.verb == "configure" for a in actions)
    assert not any(a.verb == "togglefloating" for a in actions)


def test_plan_restore_togglefloating_on_state_mismatch():
    # Record is tiled but the window is currently floating -> one togglefloating.
    rec = _rec(is_floating=False)
    live = _live(target_monitor=0, current_monitor=0, current_floating=True)
    actions = plan_restore(rec, live)
    assert Action("togglefloating", None) in actions
    # Still tiled -> no configure.
    assert not any(a.verb == "configure" for a in actions)


def test_plan_restore_configure_iff_floating_regardless_of_geometry():
    rec = _rec(is_floating=True, geometry={"x": 5, "y": 6, "width": 7, "height": 8})
    live = _live(target_monitor=0, current_monitor=0, current_floating=True)
    actions = plan_restore(rec, live)
    assert Action("configure", {"x": 5, "y": 6, "width": 7, "height": 8}) in actions


def test_plan_restore_omits_tagmon_when_no_target_or_same_monitor():
    rec = _rec(tags=1, is_floating=False)
    # target_monitor None (unmatched output) -> no tagmon.
    assert not any(a.verb == "tagmon"
                   for a in plan_restore(rec, _live(target_monitor=None, current_monitor=0,
                                                    current_floating=False)))
    # target == current -> no tagmon.
    assert not any(a.verb == "tagmon"
                   for a in plan_restore(rec, _live(target_monitor=1, current_monitor=1,
                                                    current_floating=False)))
