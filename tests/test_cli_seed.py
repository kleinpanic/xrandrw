"""WR-03: the boot seed waits (bounded) for dwm-ipc before seeding the baseline.

If dwm-ipc isn't up yet at daemon boot, ``on_settled`` is a no-op and
``_prev_connected`` stays None; a later settle would then seed off an
already-reduced topology (silently reintroducing the B2 first-unplug miss).
``_seeded_coordinator`` retries a bounded number of times waiting for
``dwmipc.available()`` before seeding, and on a permanent absence logs
``relocate_seed_deferred`` and accepts (never an infinite wait).
"""
from __future__ import annotations

import logging

import pytest

import xrandrw.cli as cli


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


class FakeCoordinator:
    """Seeds (_prev_connected set) only if dwm-ipc is available at on_settled time."""

    def __init__(self, *, config_enabled=None, ipc_timeout=None):
        self.config_enabled = config_enabled
        self.ipc_timeout = ipc_timeout
        self._prev_connected = None
        self.on_settled_calls = 0

    def on_settled(self, env, logger, stop_evt=None):
        self.on_settled_calls += 1
        if cli.dwmipc.available(cli.dwmipc.DEFAULT_SOCK_PATH, timeout=self.ipc_timeout):
            self._prev_connected = {"DP-1"}


def _patch(monkeypatch, available_fn):
    sleeps = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(cli, "RelocationCoordinator", FakeCoordinator)
    monkeypatch.setattr(cli.dwmipc, "available", available_fn)
    cli.stop_evt.clear()
    return sleeps


def test_seed_waits_then_seeds_when_dwmipc_comes_up(monkeypatch, logger):
    state = {"n": 0}

    def available(path=None, timeout=None):
        state["n"] += 1
        return state["n"] >= 3  # unavailable on the first two probes

    sleeps = _patch(monkeypatch, available)
    coord = cli._seeded_coordinator({"WINDOW_MANAGEMENT": "1"}, logger, retries=20, delay=0.5)

    assert coord._prev_connected is not None, "seeded once dwm-ipc became available"
    assert coord.on_settled_calls == 1
    assert len(sleeps) == 2, "waited exactly until the endpoint came up (bounded)"


def test_seed_deferred_and_bounded_when_dwmipc_never_up(monkeypatch, logger, caplog):
    sleeps = _patch(monkeypatch, lambda path=None, timeout=None: False)

    with caplog.at_level(logging.INFO, logger="xrandrw"):
        coord = cli._seeded_coordinator({"WINDOW_MANAGEMENT": "1"}, logger, retries=3, delay=0.5)

    assert coord._prev_connected is None
    assert len(sleeps) == 3, "bounded retries, no infinite wait"
    assert any(getattr(r, "event", None) == "relocate_seed_deferred"
               for r in caplog.records)


def test_config_enabled_passthrough_and_wait_skipped_when_disabled(monkeypatch, logger):
    # config_enabled is threaded from WINDOW_MANAGEMENT into the coordinator, and
    # the boot-wait loop is skipped entirely when disabled (lean-boot, WM-07).
    sleeps = _patch(monkeypatch, lambda path=None, timeout=None: True)

    coord_on = cli._seeded_coordinator({"WINDOW_MANAGEMENT": "1"}, logger, retries=3, delay=0.5)
    assert coord_on.config_enabled is True

    coord_off = cli._seeded_coordinator({"WINDOW_MANAGEMENT": "0"}, logger, retries=3, delay=0.5)
    assert coord_off.config_enabled is False

    coord_missing = cli._seeded_coordinator({}, logger, retries=3, delay=0.5)
    assert coord_missing.config_enabled is False

    # the two disabled calls must NOT have entered the availability wait loop
    assert sleeps == [], "disabled coordinator skips the boot-wait entirely"


def test_config_off_disables_relocation_even_with_dwmipc(monkeypatch, logger):
    # With config off, a REAL coordinator stays a no-op even when dwm-ipc is
    # available: on_settled never seeds, so _prev_connected stays None (no
    # relocation) — proving the config flag gates independently of the capability.
    from xrandrw.relocate import RelocationCoordinator

    monkeypatch.setattr("xrandrw.dwmipc.available", lambda *a, **k: True)
    coord = RelocationCoordinator(config_enabled=False)
    coord.on_settled({}, logger)
    assert coord._prev_connected is None, "config-off gates the feature off despite dwm-ipc"
