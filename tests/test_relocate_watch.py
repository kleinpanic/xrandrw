"""Hook-timing + existing-behavior-preservation tests for the Phase-10 watch hook.

Mirrors the ``_drive`` harness in tests/test_watch.py (FakeDisplay, monkeypatched
``watch.display.Display`` / ``watch.topology_hash`` / ``watch.apply_once`` and the
``select`` seam) and proves the additive coordinator hook: it fires exactly once
post-apply on a real change, never on the wakeup/unchanged paths, survives a
coordinator fault, and leaves the no-coordinator path byte-for-byte identical.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

import xrandrw.watch as watch


class FakeDisplay:
    def __init__(self, version=(1, 5)):
        self.version = version
        self.pending = 0
        self.selected_mask = None
        self.closed = False

    def screen(self):
        def _select(mask):
            self.selected_mask = mask
        return SimpleNamespace(root=SimpleNamespace(xrandr_select_input=_select))

    def flush(self):
        pass

    def fileno(self):
        return 77

    def xrandr_query_version(self):
        return SimpleNamespace(major_version=self.version[0], minor_version=self.version[1])

    def pending_events(self):
        return self.pending

    def next_event(self):
        self.pending -= 1
        return None

    def close(self):
        self.closed = True


class SpyCoordinator:
    """Records on_settled invocations into a shared event log (for ordering)."""

    def __init__(self, events, *, raises=False):
        self.events = events
        self.raises = raises
        self.calls = 0

    def on_settled(self, env, logger):
        self.calls += 1
        self.events.append(("settle",))
        if self.raises:
            raise RuntimeError("coordinator boom")


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    watch.stop_evt.clear()
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    yield
    watch.stop_evt.clear()


def _env():
    return {"POLL_INTERVAL": "45", "EXCESS_WINDOW_SEC": "10", "EXCESS_THRESHOLD": "5"}


def _drive(monkeypatch, fake, script, topo, events):
    """Wire the module seams; apply_once appends ('apply', src) to the shared log."""
    monkeypatch.setattr(watch.display, "Display", lambda: fake)
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: topo["hash"])

    def _apply(env, logger, event_source):
        events.append(("apply", event_source))
    monkeypatch.setattr(watch, "apply_once", _apply)

    pipe = {}
    real_pipe = os.pipe

    def _capture_pipe():
        r, w = real_pipe()
        pipe["r"], pipe["w"] = r, w
        return r, w
    monkeypatch.setattr(watch.os, "pipe", _capture_pipe)

    it = iter(script)

    def _select(rfds, wfds, xfds, timeout):
        try:
            token, mutate = next(it)
        except StopIteration:
            watch.stop_evt.set()
            return ([], [], [])
        if mutate:
            mutate()
        xfd, rpipe = rfds[0], rfds[1]
        if token == "wake":
            os.write(pipe["w"], b"\x00")
            return ([rpipe], [], [])
        if token == "event":
            return ([xfd], [], [])
        return ([], [], [])  # timeout
    monkeypatch.setattr(watch.select, "select", _select)


def test_hook_fires_once_after_apply_on_change(monkeypatch, logger):
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)
    coord = SpyCoordinator(events)

    watch.watch_loop(_env(), logger, coordinator=coord)

    assert coord.calls == 1, "coordinator runs exactly once per applied change"
    # Ordering: the settle hook fires AFTER apply_once.
    assert events == [("apply", "randr_event"), ("settle",)]


def test_hook_not_called_on_wakeup_or_unchanged(monkeypatch, logger):
    # Wakeup-pipe exit: no apply, no settle.
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []
    _drive(monkeypatch, fake, [("wake", watch.stop_evt.set)], topo, events)
    coord = SpyCoordinator(events)
    watch.watch_loop(_env(), logger, coordinator=coord)
    assert coord.calls == 0 and events == []

    # Unchanged hash on a slow-poll timeout: no apply, no settle.
    watch.stop_evt.clear()
    fake2 = FakeDisplay()
    topo2 = {"hash": "h0"}
    events2 = []
    _drive(monkeypatch, fake2, [("timeout", watch.stop_evt.set)], topo2, events2)
    coord2 = SpyCoordinator(events2)
    watch.watch_loop(_env(), logger, coordinator=coord2)
    assert coord2.calls == 0 and events2 == []


def test_coordinator_fault_never_breaks_loop(monkeypatch, logger, caplog):
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)
    coord = SpyCoordinator(events, raises=True)

    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        watch.watch_loop(_env(), logger, coordinator=coord)

    # Apply still happened exactly once; loop exited cleanly; Display closed.
    assert [e for e in events if e[0] == "apply"] == [("apply", "randr_event")]
    assert fake.closed
    assert any(getattr(r, "event", None) == "relocate_hook_fail" for r in caplog.records)


def test_no_coordinator_path_is_identical(monkeypatch, logger):
    # Regression guard: omitting the coordinator applies exactly as before.
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)

    watch.watch_loop(_env(), logger)   # no coordinator

    assert events == [("apply", "randr_event")]
    assert fake.closed
