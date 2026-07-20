"""GAP-A: logging sink installation, especially the stderr fallback.

The fallback at ``logging_utils.py:60-64`` is the ONLY sink on a machine with no
``systemd-python`` and no ``LOG_FILE`` -- i.e. a stock ``pip install xrandrw``.
Deleting it produced a COMPLETELY SILENT daemon and the whole suite stayed green,
because nothing called ``_setup_logging`` at all. These tests call it for real and
assert a record actually lands on stderr, plus that the fallback is correctly
SUPPRESSED when a real sink (journald or file) was installed -- both directions,
so neither "never install it" nor "always install it" can survive.
"""
from __future__ import annotations

import json
import logging
import sys
import types

import pytest

from xrandrw.logging_utils import (
    JsonFormatter,
    _kv,
    _sanitize_extra,
    _setup_logging,
    loge,
    logev,
    run,
    wait_for_x,
)


@pytest.fixture
def xrandrw_logger():
    # ``_setup_logging`` mutates the PROCESS-WIDE logging.getLogger("xrandrw")
    # singleton, so every call here must be sandboxed: start from zero handlers
    # and restore the original handler list + level afterwards. Without this the
    # handlers leak into unrelated tests (and into each other).
    lg = logging.getLogger("xrandrw")
    saved_handlers, saved_level, saved_propagate = list(lg.handlers), lg.level, lg.propagate
    lg.handlers = []
    yield lg
    lg.handlers = saved_handlers
    lg.setLevel(saved_level)
    lg.propagate = saved_propagate


@pytest.fixture
def no_journald(monkeypatch):
    # Force the journald import to fail regardless of whether systemd-python is
    # installed in the running venv, so this test asserts the same thing on a
    # bare dev box and on a CI runner with the `journald` extra.
    monkeypatch.setitem(sys.modules, "systemd", None)
    monkeypatch.setitem(sys.modules, "systemd.journal", None)


@pytest.fixture
def fake_journald(monkeypatch):
    # A stand-in `systemd.journal` whose JournalHandler is a plain in-memory
    # handler, so the "journald present" branch can be exercised with no journal.
    class FakeJournalHandler(logging.Handler):
        def __init__(self, **kw):
            super().__init__()
            self.kw = kw
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    pkg = types.ModuleType("systemd")
    mod = types.ModuleType("systemd.journal")
    mod.JournalHandler = FakeJournalHandler
    pkg.journal = mod
    monkeypatch.setitem(sys.modules, "systemd", pkg)
    monkeypatch.setitem(sys.modules, "systemd.journal", mod)
    return FakeJournalHandler


# ---------------- GAP-A: the stderr fallback ----------------

def test_stderr_fallback_is_installed_when_no_journald_and_no_log_file(
        no_journald, xrandrw_logger, capsys):
    # THE regression: no journald + no LOG_FILE must still leave the daemon able
    # to speak. A background service that fails silently is the worst outcome.
    lg = _setup_logging({"LOG_LEVEL": "info"})

    assert lg is xrandrw_logger
    assert len(lg.handlers) == 1, "exactly one fallback sink expected"
    handler = lg.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr, "the fallback must write to stderr"
    assert handler.level == logging.INFO, "the fallback must honour LOG_LEVEL"


def test_stderr_fallback_actually_delivers_a_record(no_journald, xrandrw_logger, capsys):
    # Installing a handler is not enough -- prove a real log call reaches stderr.
    lg = _setup_logging({"LOG_LEVEL": "info"})
    logev(lg, logging.INFO, "apply_start", "daemon is alive", outputs=2)

    captured = capsys.readouterr()
    assert "daemon is alive" in captured.err, \
        "with no journald and no LOG_FILE the daemon must still speak on stderr"
    assert "INFO" in captured.err
    assert "outputs=2" in captured.err, "logev must append k=v for journalctl readability"
    assert captured.out == "", "log output must not pollute stdout (--list-state parses it)"


def test_stderr_fallback_respects_a_higher_log_level(no_journald, xrandrw_logger, capsys):
    lg = _setup_logging({"LOG_LEVEL": "err"})
    loge(lg, logging.INFO, "noise", "should not appear")
    loge(lg, logging.ERROR, "boom", "should appear")

    err = capsys.readouterr().err
    assert "should not appear" not in err
    assert "should appear" in err


def test_log_file_sink_suppresses_the_stderr_fallback(no_journald, xrandrw_logger, tmp_path):
    # The other direction: the fallback exists only because nothing else does.
    # A test that merely asserted "a handler is installed" would let an
    # always-install-stderr mutation live.
    log_file = tmp_path / "nested" / "xrandrw.log"
    lg = _setup_logging({"LOG_LEVEL": "info", "LOG_FILE": str(log_file)})

    assert log_file.parent.is_dir(), "LOG_FILE's parent directory must be created"
    kinds = [type(h) for h in lg.handlers]
    assert logging.FileHandler in kinds
    assert logging.StreamHandler not in kinds, \
        "a real file sink exists; the stderr fallback must NOT be added on top"


def test_journald_sink_suppresses_the_stderr_fallback(fake_journald, xrandrw_logger):
    lg = _setup_logging({"LOG_LEVEL": "debug"})

    assert len(lg.handlers) == 1
    handler = lg.handlers[0]
    assert isinstance(handler, fake_journald)
    assert handler.kw == {"SYSLOG_IDENTIFIER": "xrandrw"}
    assert handler.level == logging.DEBUG
    assert not any(type(h) is logging.StreamHandler for h in lg.handlers)


def test_broken_journald_handler_still_leaves_a_working_sink(monkeypatch, xrandrw_logger, capsys):
    # systemd-python importable but its handler blows up at construction (seen with
    # a mismatched libsystemd). The except-Exception guard must keep `added` False
    # so the stderr fallback still saves us.
    pkg = types.ModuleType("systemd")
    mod = types.ModuleType("systemd.journal")

    def _explode(**kw):
        raise OSError("libsystemd mismatch")

    mod.JournalHandler = _explode
    pkg.journal = mod
    monkeypatch.setitem(sys.modules, "systemd", pkg)
    monkeypatch.setitem(sys.modules, "systemd.journal", mod)

    lg = _setup_logging({"LOG_LEVEL": "info"})
    lg.info("still speaking")

    assert isinstance(lg.handlers[0], logging.StreamHandler)
    assert "still speaking" in capsys.readouterr().err


def test_unknown_log_level_falls_back_to_info(no_journald, xrandrw_logger):
    assert _setup_logging({"LOG_LEVEL": "not-a-level"}).level == logging.INFO


def test_log_level_none_silences_everything(no_journald, xrandrw_logger, capsys):
    lg = _setup_logging({"LOG_LEVEL": "none"})
    loge(lg, logging.ERROR, "boom", "must stay quiet")
    assert capsys.readouterr().err == ""


# ---------------- JsonFormatter / file sink ----------------

def test_log_file_receives_parseable_json_lines(no_journald, xrandrw_logger, tmp_path):
    log_file = tmp_path / "xrandrw.log"
    lg = _setup_logging({"LOG_LEVEL": "info", "LOG_FILE": str(log_file)})
    loge(lg, logging.WARNING, "wallpaper_failed", "backend failed", backend="feh", rc=1)
    for h in lg.handlers:
        h.flush()

    lines = [ln for ln in log_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["lvl"] == "warning"
    assert payload["msg"] == "backend failed"
    assert payload["event"] == "wallpaper_failed"
    assert payload["backend"] == "feh" and payload["rc"] == 1
    assert "ts" in payload


def test_json_formatter_drops_reserved_and_underscored_keys():
    rec = logging.LogRecord("xrandrw", logging.INFO, "p.py", 7, "hello", None, None)
    rec.event = "apply_start"
    rec.outputs = 3
    rec._internal = "hidden"
    payload = json.loads(JsonFormatter().format(rec))

    assert payload["msg"] == "hello" and payload["event"] == "apply_start"
    assert payload["outputs"] == 3
    assert "_internal" not in payload
    assert "levelname" not in payload and "pathname" not in payload


def test_sanitize_extra_prefixes_reserved_names():
    # Passing a reserved LogRecord attr through `extra` raises KeyError inside
    # logging; renaming it is what keeps a stray field=... from crashing a daemon.
    out = _sanitize_extra({"module": "apply", "args": 1, "backend": "feh"})
    assert out == {"field_module": "apply", "field_args": 1, "backend": "feh"}


def test_reserved_field_name_does_not_crash_the_logger(no_journald, xrandrw_logger, capsys):
    lg = _setup_logging({"LOG_LEVEL": "info"})
    logev(lg, logging.INFO, "apply_start", "renamed", module="apply")  # must not raise
    assert "renamed" in capsys.readouterr().err


def test_kv_renders_pairs_and_skips_none():
    assert _kv(a=1, b="x") == " a=1 b=x"
    assert _kv(a=1, b=None) == " a=1"
    assert _kv() == ""
    assert _kv(zero=0, empty="") == " zero=0 empty=", "falsy-but-present values must survive"


# ---------------- run() / wait_for_x() ----------------

def test_run_echoes_the_command_only_at_debug(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr("xrandrw.logging_utils.subprocess.run",
                        lambda cmd, **kw: calls.append((list(cmd), kw)))
    lg = logging.getLogger("xrandrw.test_run_echo")

    with caplog.at_level(logging.INFO, logger=lg.name):
        run(["xrandr", "--query"], logger=lg)
    assert "exec" not in [getattr(r, "event", None) for r in caplog.records]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=lg.name):
        run(["xrandr", "--output", "HDMI 1"], logger=lg)
    execs = [r for r in caplog.records if getattr(r, "event", None) == "exec"]
    assert execs, "LOG_LEVEL=debug must echo the command"
    assert "'HDMI 1'" in execs[0].getMessage(), "the echo must be shell-quoted"

    assert [c[0] for c in calls] == [["xrandr", "--query"], ["xrandr", "--output", "HDMI 1"]]
    assert all(c[1]["text"] is True and c[1]["capture_output"] is True for c in calls)


def test_run_without_a_logger_still_executes(monkeypatch):
    seen = []
    monkeypatch.setattr("xrandrw.logging_utils.subprocess.run",
                        lambda cmd, **kw: seen.append(list(cmd)))
    run(["xset", "q"])
    assert seen == [["xset", "q"]]


def test_wait_for_x_returns_as_soon_as_xset_succeeds(monkeypatch):
    from subprocess import CompletedProcess
    attempts = []

    def fake_run(cmd, **kw):
        attempts.append(list(cmd))
        return CompletedProcess(cmd, 0)

    monkeypatch.setattr("xrandrw.logging_utils.run", fake_run)
    monkeypatch.setattr("xrandrw.logging_utils.time.sleep", lambda s: pytest.fail("must not sleep"))
    wait_for_x(logging.getLogger("xrandrw.test_wait_ok"))
    assert attempts == [["xset", "q"]], "a responsive X server must be probed exactly once"


def test_wait_for_x_gives_up_after_a_bounded_number_of_probes(monkeypatch, caplog):
    from subprocess import CompletedProcess
    attempts, sleeps = [], []
    monkeypatch.setattr("xrandrw.logging_utils.run",
                        lambda cmd, **kw: (attempts.append(list(cmd)), CompletedProcess(cmd, 1))[1])
    monkeypatch.setattr("xrandrw.logging_utils.time.sleep", lambda s: sleeps.append(s))
    lg = logging.getLogger("xrandrw.test_wait_giveup")

    with caplog.at_level(logging.INFO, logger=lg.name):
        wait_for_x(lg)   # must RETURN, never block the daemon forever

    assert len(attempts) == 20, "the X wait must be bounded, not an infinite block"
    assert sum(sleeps) == pytest.approx(10.0), "~10s total budget"
    assert [getattr(r, "event", None) for r in caplog.records] == ["x_wait"]


def test_wait_for_x_survives_xset_being_absent(monkeypatch, caplog):
    # No xset binary at all -> FileNotFoundError from every probe; wait_for_x must
    # swallow it and continue rather than killing the daemon at boot.
    def boom(cmd, **kw):
        raise FileNotFoundError("xset")

    monkeypatch.setattr("xrandrw.logging_utils.run", boom)
    monkeypatch.setattr("xrandrw.logging_utils.time.sleep", lambda s: None)
    lg = logging.getLogger("xrandrw.test_wait_noxset")

    with caplog.at_level(logging.INFO, logger=lg.name):
        wait_for_x(lg)  # must not raise

    assert [getattr(r, "event", None) for r in caplog.records] == ["x_wait"]
