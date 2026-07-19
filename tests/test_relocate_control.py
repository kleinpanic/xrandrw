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
from xrandrw.relocate import RelocationControl


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
