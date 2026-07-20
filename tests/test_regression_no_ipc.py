"""REG-01 capability-gate-OFF no-op proof + the zero-interaction regression guard.

Proves the window-mgmt lifecycle is a COMPLETE no-op whenever the capability gate
is off -- the "RPi4 / vanilla-dwm / i3 can never silently break" guard. A REAL
``RelocationCoordinator`` is driven through a simulated unplug->replug cycle with
counting-spy seams on every dwm-ipc verb, the live-X reader, the capture, and the
window-control seam. Both gate-off legs are covered:

  (A) NO SOCKET   -- ``config_enabled=True`` but ``dwmipc.available()`` False;
  (B) CONFIG OFF  -- socket present (``available()`` would be True) but
                     ``config_enabled=False`` short-circuits ``_enabled()`` so the
                     socket is never even probed (lean-boot guarantee).

A named regression guard (``test_gate_off_never_touches_dwm_or_windows``) asserts
ZERO dwm-ipc calls + ZERO window control while ``_enabled()`` is False -- it MUST
fail if a future change makes the gated-off window-mgmt path run. A final test
runs the REAL config-off coordinator through ``watch.watch_loop`` and proves the
post-apply hook stays inert while the display-layout apply still fires once.

TEST/DOC ONLY: no src/ change; no runtime window-mgmt behavior is exercised.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

import xrandrw.relocate as relocate
import xrandrw.watch as watch


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


class _CountingReader:
    """A RandRReader stand-in: any ``read`` is a regression (gate should block it).

    If it DID run it would flip HDMI-1's connected state across calls, so a leaked
    gate would produce removed/returned sets (and thus dwm-ipc traffic) -- making
    the leak observable. While the gate is off ``read`` must never be called.
    """

    def __init__(self):
        self.reads = 0

    def read(self, logger=None):
        self.reads += 1
        # Odd calls = HDMI unplugged, even = replugged (would drive record/restore).
        hdmi_connected = (self.reads % 2 == 0)
        return {
            "DSI-1": SimpleNamespace(name="DSI-1", connected=True),
            "HDMI-1": SimpleNamespace(name="HDMI-1", connected=hdmi_connected),
        }


class _CountingControl:
    """Live-X window-control seam stand-in; every call is a regression."""

    def __init__(self):
        self.focus_calls = 0
        self.configure_calls = 0

    def focus(self, xid):
        self.focus_calls += 1
        return True

    def configure_geometry(self, xid, geometry):
        self.configure_calls += 1
        return True


class _CountingCapture:
    def __init__(self):
        self.calls = 0

    def __call__(self, **kw):
        self.calls += 1
        return []


def _make_coord(monkeypatch, *, config_enabled, available_returns, calls, avail_counter):
    """Build a REAL coordinator with counting seams; monkeypatch every dwm-ipc verb.

    ``calls`` collects any dwm-ipc verb invocation (get_monitors/get_dwm_client/
    run_command) -- it must stay EMPTY while the gate is off. ``avail_counter``
    records ``dwmipc.available`` invocations so the config-off leg can prove the
    socket is never probed.
    """
    def _sentinel(*a, **kw):
        calls.append(("ipc", a, kw))
        return []

    monkeypatch.setattr(relocate.dwmipc, "get_monitors", _sentinel)
    monkeypatch.setattr(relocate.dwmipc, "get_dwm_client", _sentinel)
    monkeypatch.setattr(relocate.dwmipc, "run_command", _sentinel)

    def _available(path=None, **kw):
        avail_counter.append(path)
        return available_returns
    monkeypatch.setattr(relocate.dwmipc, "available", _available)

    reader = _CountingReader()
    control = _CountingControl()
    capture = _CountingCapture()
    coord = relocate.RelocationCoordinator(
        control=control, reader=reader, xreader=SimpleNamespace(),
        capture=capture, sock_path="/nonexistent/dwm.sock",
        config_enabled=config_enabled, proc_root="/proc",
    )
    return coord, reader, control, capture


def _assert_zero_interaction(coord, reader, control, capture, calls):
    assert reader.reads == 0, "gate-off must never reach the live-X read"
    assert calls == [], "gate-off must issue ZERO dwm-ipc calls"
    assert control.focus_calls == 0 and control.configure_calls == 0, \
        "gate-off must drive ZERO window control"
    assert capture.calls == 0, "gate-off must never capture windows"
    assert coord._displaced == {}, "gate-off must not record any displaced window"
    assert coord._prev_present is None, "gate-off must return before seeding baseline"


def test_gate_off_no_socket_is_complete_noop(monkeypatch, logger):
    # Leg A: config on, but no dwm.sock -> dwmipc.available() False -> on_settled inert.
    calls, avail = [], []
    coord, reader, control, capture = _make_coord(
        monkeypatch, config_enabled=True, available_returns=False,
        calls=calls, avail_counter=avail)

    # Simulated unplug->replug cycle: if the gate leaked, the reader would flip
    # HDMI-1 connected across these calls and drive record/restore + dwm-ipc.
    for _ in range(4):
        coord.on_settled({}, logger)

    _assert_zero_interaction(coord, reader, control, capture, calls)
    # available() MAY be consulted here -- it IS the gate check.


def test_gate_off_config_off_never_probes_socket(monkeypatch, logger):
    # Leg B: socket present (available would be True) but WINDOW_MANAGEMENT off.
    calls, avail = [], []
    coord, reader, control, capture = _make_coord(
        monkeypatch, config_enabled=False, available_returns=True,
        calls=calls, avail_counter=avail)

    for _ in range(4):
        coord.on_settled({}, logger)

    _assert_zero_interaction(coord, reader, control, capture, calls)
    # Lean-boot guarantee: _enabled()'s `config_enabled and available(...)` short-
    # circuits, so a config-off machine never even probes the dwm-ipc socket.
    assert avail == [], "config-off must short-circuit BEFORE calling available()"


def test_gate_off_never_touches_dwm_or_windows(monkeypatch, logger):
    # REGRESSION GUARD (the "can never silently break RPi4" assertion). For an
    # _enabled()-False coordinator, a full displaced/returned sequence must produce
    # ZERO dwm-ipc calls and ZERO window-control calls.
    #
    # THIS TEST MUST FAIL if a future change makes the gated-off window-mgmt path
    # run (issues any dwm-ipc verb / moves any window on a no-dwm-ipc machine).
    calls, avail = [], []
    coord, reader, control, capture = _make_coord(
        monkeypatch, config_enabled=True, available_returns=False,
        calls=calls, avail_counter=avail)

    assert coord._enabled() is False, "precondition: the gate must be OFF"

    # Drive an unplug->replug->settle sequence several times over.
    for _ in range(6):
        coord.on_settled({"POLL_INTERVAL": "45"}, logger)

    assert calls == [], "gated-off path issued a dwm-ipc call -- RPi4 regression!"
    assert control.focus_calls == 0 and control.configure_calls == 0, \
        "gated-off path drove window control -- RPi4 regression!"
    _assert_zero_interaction(coord, reader, control, capture, calls)


# --- inert watch hook: the post-apply hook stays inert while apply still runs ---

class _FakeDisplay:
    """Minimal RandR display seam mirrored from tests/test_relocate_watch.py."""

    def __init__(self, version=(1, 5)):
        self.version = version
        self.pending = 0
        self.closed = False

    def screen(self):
        return SimpleNamespace(root=SimpleNamespace(xrandr_select_input=lambda mask: None))

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


def _drive(monkeypatch, fake, script, topo, events):
    """Wire the watch seams; apply_once appends ('apply', src) to the shared log."""
    monkeypatch.setattr(watch.display, "Display", lambda: fake)
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: topo["hash"])
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)

    def _apply(env, logger, event_source):
        events.append(("apply", event_source))
        return True  # BL-01: apply_once's contract is `-> bool`; honour it in the fake
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
        xfd = rfds[0]
        if token == "event":
            return ([xfd], [], [])
        return ([], [], [])  # timeout
    monkeypatch.setattr(watch.select, "select", _select)


def _watch_env():
    return {"POLL_INTERVAL": "45", "EXCESS_WINDOW_SEC": "10", "EXCESS_THRESHOLD": "5"}


def test_watch_hook_inert_under_gate_off_while_apply_still_runs(monkeypatch, logger):
    # A REAL config-off coordinator driven through watch_loop: its on_settled is
    # invoked post-apply yet returns inert (gate off), and the display-layout apply
    # still fires exactly once. We do NOT re-assert hook TIMING (test_relocate_watch
    # owns that) -- only gate-off inertness + that layout apply is unaffected.
    watch.stop_evt.clear()
    calls, avail = [], []
    coord, reader, control, capture = _make_coord(
        monkeypatch, config_enabled=False, available_returns=True,
        calls=calls, avail_counter=avail)

    fake = _FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)

    watch.watch_loop(_watch_env(), logger, coordinator=coord)
    watch.stop_evt.clear()

    # Display-layout apply path unchanged: exactly one apply fired.
    assert [e for e in events if e[0] == "apply"] == [("apply", "randr_event")]
    # Coordinator hook ran post-apply but was completely inert.
    _assert_zero_interaction(coord, reader, control, capture, calls)
    assert avail == [], "config-off watch hook must never probe the socket"
    assert fake.closed
