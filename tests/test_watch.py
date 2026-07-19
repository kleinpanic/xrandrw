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


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_watch")
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


def _drive(monkeypatch, fake, script, topo):
    """Wire the module seams; `script` items are (token, mutate) per select() call.

    token: "wake" | "event" | "timeout"; mutate: a callable run before the return.
    """
    monkeypatch.setattr(watch.display, "Display", lambda: fake)
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: topo["hash"])
    applies = []

    def _apply(env, logger, event_source):
        # BL-01: apply_once's contract is now `-> bool` (True == a full apply
        # completed). The fake must honour it, or every _drive test would exercise
        # the new "apply bailed, do not absorb the hash" branch instead of the
        # normal path it means to cover.
        applies.append(event_source)
        return True
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
    return applies


def test_wakeup_pipe_prompt_exit(monkeypatch, logger):
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    applies = _drive(monkeypatch, fake, [("wake", watch.stop_evt.set)], topo)

    watch.watch_loop(_env(), logger)

    assert applies == [], "wakeup path must not apply"
    assert fake.closed, "Display must be closed on exit"


def test_event_burst_debounces_to_one_apply(monkeypatch, logger):
    fake = FakeDisplay()
    topo = {"hash": "h0"}

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    applies = _drive(monkeypatch, fake, script, topo)

    watch.watch_loop(_env(), logger)

    assert applies == ["randr_event"], "one plug (3 events) -> exactly one apply"


def test_slow_poll_reapplies_only_on_change(monkeypatch, logger):
    fake = FakeDisplay()
    topo = {"hash": "h0"}

    def _change():
        topo["hash"] = "h2"
    # First timeout with a real change -> safety apply; second timeout unchanged -> no apply.
    script = [("timeout", _change), ("timeout", watch.stop_evt.set)]
    applies = _drive(monkeypatch, fake, script, topo)

    watch.watch_loop(_env(), logger)

    assert applies == ["slow_poll"], "slow-poll applies once, only when topology changed"


def test_no_double_apply_from_own_mutations(monkeypatch, logger):
    # Phantom guard: the apply's own xrandr commands emit RandR events. If the loop
    # returned the PRE-apply hash it would see the settled post-apply topology as a
    # fresh change and apply a second, redundant time. It must absorb its own change.
    fake = FakeDisplay()
    topo = {"hash": "h0"}

    def _plug():
        fake.pending = 3
        topo["hash"] = "h1"          # real hotplug -> the (single) legitimate apply

    def _self_events():
        fake.pending = 3             # daemon's own events; topology already settled at h2

    script = [("event", _plug), ("event", _self_events), ("timeout", watch.stop_evt.set)]
    applies = _drive(monkeypatch, fake, script, topo)

    # Override apply_once so it mutates the topology, as a real apply does.
    def _apply(env, logger, event_source):
        applies.append(event_source)
        topo["hash"] = "h2"
        return True  # BL-01: a completed apply
    monkeypatch.setattr(watch, "apply_once", _apply)

    watch.watch_loop(_env(), logger)

    assert applies == ["randr_event"], "must not re-apply on its own post-apply events"


def test_randr_below_1_5_degrades_to_slow_poll(monkeypatch, logger):
    fake = FakeDisplay(version=(1, 4))
    topo = {"hash": "h0"}
    applies = _drive(monkeypatch, fake, [("timeout", watch.stop_evt.set)], topo)

    watch.watch_loop(_env(), logger)

    assert fake.selected_mask is None, "no event registration on RandR < 1.5"
    assert applies == []
