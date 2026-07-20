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


# ---------------- UX-02: replug bounce hold-down ----------------

def _benv(hold="3000", suspect="5000"):
    e = _env()
    e["BOUNCE_HOLDDOWN_MS"] = hold
    e["BOUNCE_SUSPECT_MS"] = suspect
    return e


def _churn(env, present, age_s=0.0):
    return {
        "times": [], "backoff": 0,
        "window": int(env["EXCESS_WINDOW_SEC"]), "threshold": int(env["EXCESS_THRESHOLD"]),
        "apply_done_at": watch.time.monotonic() - age_s,
        "present": set(present),
    }


def test_bounce_returning_connector_is_suppressed(monkeypatch, logger):
    # The whole point: a disconnect that resolves back to the pre-drop topology
    # inside the hold-down must NOT reach apply_once, so scrub_stale never --off's
    # the head and dwm never evacuates the windows it just restored.
    env = _benv()
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: {"eDP-1"})
    seq = iter(["h_dark", "h0"])  # first re-read still dark, second: bounced back
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: next(seq))
    monkeypatch.setattr(watch.stop_evt, "wait", lambda t: False)

    assert watch._bounce_settled(env, logger, "h0", _churn(env, {"eDP-1", "HDMI-1"})) is True


def test_bounce_holddown_expires_when_head_really_gone(monkeypatch, logger):
    # A genuine unplug that merely happens to land inside the suspect window must
    # still be applied — delayed, never dropped.
    env = _benv(hold="60")
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: {"eDP-1"})
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: "h_dark")
    monkeypatch.setattr(watch.stop_evt, "wait", lambda t: False)

    assert watch._bounce_settled(env, logger, "h0", _churn(env, {"eDP-1", "HDMI-1"})) is False


def test_connect_edge_is_never_held(monkeypatch, logger):
    # Gate 2. The genuine replug at 04:21 landed 3.1 s after apply_done — inside the
    # 5 s suspect window. Holding it would delay every real replug.
    env = _benv()
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: {"eDP-1", "HDMI-1"})
    monkeypatch.setattr(watch, "topology_hash", _unreachable_hash)

    assert watch._bounce_settled(env, logger, "h0", _churn(env, {"eDP-1"}, age_s=3.1)) is False


def test_genuine_unplug_outside_suspect_window_is_not_delayed(monkeypatch, logger):
    # Gate 1. Measured genuine unplugs sit 47–248 s after the previous apply; they
    # must pay ZERO added latency, so we must not even read the topology.
    env = _benv()
    monkeypatch.setattr(watch, "present_connectors", _unreachable_present)
    monkeypatch.setattr(watch, "topology_hash", _unreachable_hash)

    assert watch._bounce_settled(env, logger, "h0", _churn(env, {"eDP-1", "HDMI-1"}, age_s=47.4)) is False


def test_holddown_zero_disables_feature(monkeypatch, logger):
    env = _benv(hold="0")
    monkeypatch.setattr(watch, "present_connectors", _unreachable_present)

    assert watch._bounce_settled(env, logger, "h0", _churn(env, {"eDP-1", "HDMI-1"})) is False


def test_unreadable_topology_never_holds_down(monkeypatch, logger):
    # present_connectors swallows to set(); an UNKNOWN edge direction must not be
    # guessed at, so no baseline (or an unreadable one) means no hold-down.
    env = _benv()
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: set())

    assert watch._bounce_settled(env, logger, "h0", _churn(env, set())) is False


def test_holddown_bails_promptly_on_shutdown(monkeypatch, logger):
    # WR-01: a multi-second hold must not add multi-second SIGTERM latency. It waits
    # on stop_evt, never time.sleep, and a set flag aborts without suppressing.
    env = _benv(hold="30000")
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: {"eDP-1"})
    monkeypatch.setattr(watch, "topology_hash", _unreachable_hash)
    monkeypatch.setattr(watch.stop_evt, "wait", lambda t: True)  # signalled

    assert watch._bounce_settled(env, logger, "h0", _churn(env, {"eDP-1", "HDMI-1"})) is False


# ---------------- UX-02: the hold-down is actually WIRED to the apply path ----------------
# Everything above hand-builds churn via _churn() and calls _bounce_settled directly, so
# NOTHING proved _apply_if_changed populates that dict. Three defects survived the suite
# with it green: deleting the baseline record, freezing apply_done_at, and unhooking
# _bounce_settled from the loop entirely. An empty `prev` makes _bounce_settled return
# False at watch.py:114, so the fix goes silently inert and the four-window replug returns
# — indistinguishable from the bug coming back. These drive _apply_if_changed instead.


def _fresh_churn(env):
    # EXACTLY what watch_loop builds — no bounce keys. _apply_if_changed must add them.
    return {
        "times": [], "backoff": 0,
        "window": int(env["EXCESS_WINDOW_SEC"]), "threshold": int(env["EXCESS_THRESHOLD"]),
    }


def _hash_script(monkeypatch, seq):
    # Exact-length: an unexpected extra topology read raises StopIteration rather than
    # quietly returning a stale hash.
    it = iter(seq)
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: next(it))


def _record_applies(monkeypatch, applies):
    def _apply(env, logger, event_source):
        applies.append(event_source)
        return True  # BL-01: a completed apply
    monkeypatch.setattr(watch, "apply_once", _apply)
    return applies


def test_completed_apply_records_the_bounce_baseline(monkeypatch, logger):
    # The wiring itself: without both of these keys the hold-down can never fire.
    env = _benv()
    churn = _fresh_churn(env)
    applies = _record_applies(monkeypatch, [])
    _hash_script(monkeypatch, ["h1", "h1", "h2"])  # cur, post-debounce verify, settled
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: {"eDP-1", "HDMI-1"})
    monkeypatch.setattr(watch, "unhealed_connectors", lambda logger=None: [])

    assert watch._apply_if_changed(env, logger, "h0", churn, False) == "h2"
    assert applies == ["slow_poll"]
    assert churn.get("present") == {"eDP-1", "HDMI-1"}, "no baseline -> hold-down is inert"
    assert isinstance(churn.get("apply_done_at"), float), "no timestamp -> gate 1 never opens"


def test_bounce_after_a_real_apply_never_reaches_apply_once_again(monkeypatch, logger):
    # End-to-end consequence, driven only through _apply_if_changed: apply, then drop
    # HDMI-1 and have it return. The second apply must NOT happen — that apply is the
    # one whose scrub_stale --off makes dwm evacuate the just-restored windows.
    env = _benv()
    churn = _fresh_churn(env)
    applies = _record_applies(monkeypatch, [])
    present = {"now": {"eDP-1", "HDMI-1"}}
    monkeypatch.setattr(watch, "present_connectors", lambda logger=None: set(present["now"]))
    monkeypatch.setattr(watch, "unhealed_connectors", lambda logger=None: [])
    monkeypatch.setattr(watch.stop_evt, "wait", lambda t: False)
    _hash_script(monkeypatch, ["h1", "h1", "h2",        # pass 1: the genuine apply
                               "h_dark", "h_dark",      # pass 2: the bounce's dark read
                               "h2"])                   # ...which resolves back inside the hold

    last = watch._apply_if_changed(env, logger, "h0", churn, False)
    assert applies == ["slow_poll"] and last == "h2"

    present["now"] = {"eDP-1"}  # HDMI-1 drops
    assert watch._apply_if_changed(env, logger, last, churn, True) == "h2"
    assert applies == ["slow_poll"], "the bounce must be absorbed, not applied"


def test_holddown_zero_records_no_baseline_and_costs_no_read(monkeypatch, logger):
    # Kill-switch: the documented off state must leave the apply path byte-identical to
    # pre-UX-02 — no baseline stored and not one extra RandR read paid for.
    env = _benv(hold="0")
    churn = _fresh_churn(env)
    applies = _record_applies(monkeypatch, [])
    _hash_script(monkeypatch, ["h1", "h1", "h2"])
    monkeypatch.setattr(watch, "present_connectors", _unreachable_present)
    monkeypatch.setattr(watch, "unhealed_connectors", lambda logger=None: [])

    assert watch._apply_if_changed(env, logger, "h0", churn, False) == "h2"
    assert applies == ["slow_poll"]
    assert "present" not in churn and "apply_done_at" not in churn


def _unreachable_hash(logger=None):
    raise AssertionError("topology must not be re-read on this path")


def _unreachable_present(logger=None):
    raise AssertionError("topology must not be read on this path")
