"""TEST-07: main() flag dispatch + set_pref/list_state/window_state exit codes.

Drives ``xrandrw.cli.main()`` with a monkeypatched ``sys.argv`` and stubs every
side-effecting entry point (apply_once/watch_loop/_seeded_coordinator/wait_for_x,
subprocess.run, dwmipc.available), so the console-script dispatch paths are
exercised with NO real X server, dwm, or xrandr. cli.py itself is never modified.
"""
from __future__ import annotations

import json
import logging

import pytest

import xrandrw.cli as cli


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_cli_dispatch")
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture(autouse=True)
def _quiet_boot(monkeypatch, logger):
    # main() calls the real load_config()/_setup_logging()/_install_signals() at the
    # top; keep those side-effect-light so repeated main() calls in this module don't
    # accumulate handlers or install real signal handlers. load_config is left REAL
    # (config.py is already covered) but logging/signal wiring is stubbed.
    monkeypatch.setattr(cli, "_setup_logging", lambda env: logger)
    monkeypatch.setattr(cli, "_install_signals", lambda logger: None)
    cli.stop_evt.clear()
    yield
    cli.stop_evt.clear()


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(cli.sys, "argv", ["xrandrw", *argv])
    return cli.main()


def test_main_print_runs_xrandr_query(monkeypatch):
    ran = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, *a, **k: ran.append(cmd))
    rc = _run_main(monkeypatch, ["--print"])
    assert rc == 0
    assert ran == [["xrandr", "--query"]]


def test_main_list_state_prints_json_and_returns_zero(monkeypatch, capsys):
    # load_state is isolated to an empty tmp XDG dir by the conftest fixture.
    rc = _run_main(monkeypatch, ["--list-state"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == {"profiles": {}, "identity_map": {}}


def test_main_window_state_returns_int_and_prints_diagnostic(monkeypatch, capsys):
    # Feature default-off => window_state returns 0 and prints the stable schema.
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: False)
    rc = _run_main(monkeypatch, ["--window-state"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["enabled"] is False
    assert parsed["captured"] == [] and parsed["displaced"] == []
    assert "reason" in parsed


def test_main_set_pref_dispatches_and_returns_zero(monkeypatch):
    seen = {}

    def fake_set_pref(env, out_or_id, side, logger):
        seen["args"] = (out_or_id, side)

    monkeypatch.setattr(cli, "set_pref", fake_set_pref)
    rc = _run_main(monkeypatch, ["--set-pref", "DP-1", "right-of"])
    assert rc == 0
    assert seen["args"] == ("DP-1", "right-of")


def test_main_set_pref_invalid_side_raises_systemexit(monkeypatch):
    # Real set_pref validates the side BEFORE any X read, so an invalid side must
    # surface as SystemExit through main() with no monitor access.
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, ["--set-pref", "DP-1", "bogus-side"])


def test_main_daemon_wires_boot_apply_seed_and_watch(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "wait_for_x", lambda logger: events.append("wait_x"))
    monkeypatch.setattr(cli, "_watchdog_thread", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_sd_notify", lambda msg: events.append(("notify", msg)))
    monkeypatch.setattr(cli, "apply_once",
                        lambda env, logger, event_source="manual": events.append(("apply", event_source)))
    sentinel = object()
    monkeypatch.setattr(cli, "_seeded_coordinator", lambda env, logger: sentinel)
    monkeypatch.setattr(cli, "watch_loop",
                        lambda env, logger, coordinator=None: events.append(("watch", coordinator)))

    rc = _run_main(monkeypatch, ["--daemon"])
    assert rc == 0
    assert ("apply", "daemon_boot") in events
    assert ("watch", sentinel) in events
    # finally-block sets the stop event so watchdog/loop shut down cleanly.
    assert cli.stop_evt.is_set()


def test_main_watch_wires_boot_apply_seed_and_watch(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "wait_for_x", lambda logger: None)
    monkeypatch.setattr(cli, "_watchdog_thread", lambda *a, **k: None)
    monkeypatch.setattr(cli, "apply_once",
                        lambda env, logger, event_source="manual": events.append(("apply", event_source)))
    sentinel = object()
    monkeypatch.setattr(cli, "_seeded_coordinator", lambda env, logger: sentinel)
    monkeypatch.setattr(cli, "watch_loop",
                        lambda env, logger, coordinator=None: events.append(("watch", coordinator)))

    rc = _run_main(monkeypatch, ["--watch"])
    assert rc == 0
    assert ("apply", "watch_boot") in events
    assert ("watch", sentinel) in events
    assert cli.stop_evt.is_set()


def test_main_default_applies_once_with_env_label(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "apply_once",
                        lambda env, logger, event_source="manual": events.append(event_source))
    # No ACTION/OUTPUT in env => the manual label.
    monkeypatch.delenv("ACTION", raising=False)
    monkeypatch.delenv("OUTPUT", raising=False)
    rc = _run_main(monkeypatch, [])
    assert rc == 0
    assert events == ["manual"]


def test_main_default_labels_xplugd_from_env(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "apply_once",
                        lambda env, logger, event_source="manual": events.append(event_source))
    monkeypatch.setenv("ACTION", "add")
    rc = _run_main(monkeypatch, [])
    assert rc == 0
    assert events == ["xplugd"]


def test_main_ignores_extra_unknown_args(monkeypatch):
    events = []
    monkeypatch.setattr(cli, "apply_once",
                        lambda env, logger, event_source="manual": events.append(event_source))
    monkeypatch.delenv("ACTION", raising=False)
    monkeypatch.delenv("OUTPUT", raising=False)
    rc = _run_main(monkeypatch, ["--totally-unknown-flag"])
    assert rc == 0
    assert events == ["manual"]


def test_event_source_from_env_toggles(monkeypatch):
    monkeypatch.delenv("ACTION", raising=False)
    monkeypatch.delenv("OUTPUT", raising=False)
    assert cli._event_source_from_env() == "manual"
    monkeypatch.setenv("OUTPUT", "HDMI-1")
    assert cli._event_source_from_env() == "xplugd"


# ---------------- _seeded_coordinator branch coverage ----------------

def test_seeded_coordinator_seed_fail_is_swallowed(monkeypatch, logger, caplog):
    # on_settled raising must be caught (relocate_seed_fail) and not propagate; the
    # returned coordinator still logs relocate_seed_deferred (_prev_connected None).
    class BoomCoordinator:
        def __init__(self, *, config_enabled=None, ipc_timeout=None):
            self._prev_connected = None

        def on_settled(self, env, logger):
            raise RuntimeError("ipc blew up")

    monkeypatch.setattr(cli, "RelocationCoordinator", BoomCoordinator)
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: True)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_cli_dispatch"):
        coord = cli._seeded_coordinator({"WINDOW_MANAGEMENT": "1"}, logger, retries=1, delay=0)

    assert coord._prev_connected is None
    events = {getattr(r, "event", None) for r in caplog.records}
    assert "relocate_seed_fail" in events
    assert "relocate_seed_deferred" in events


def test_seeded_coordinator_wait_breaks_on_stop_event(monkeypatch, logger):
    # If the daemon is asked to stop while waiting for dwm-ipc, the boot-wait loop
    # breaks immediately (no further sleeps) rather than burning the full budget.
    class Coord:
        def __init__(self, *, config_enabled=None, ipc_timeout=None):
            self._prev_connected = None

        def on_settled(self, env, logger):
            pass

    sleeps = []
    monkeypatch.setattr(cli, "RelocationCoordinator", Coord)
    monkeypatch.setattr(cli.dwmipc, "available", lambda *a, **k: False)
    monkeypatch.setattr(cli.time, "sleep", lambda s: sleeps.append(s))
    cli.stop_evt.set()

    cli._seeded_coordinator({"WINDOW_MANAGEMENT": "1"}, logger, retries=5, delay=0.5)
    # stop_evt set before the first sleep => the loop breaks with zero sleeps.
    assert sleeps == []
