"""Headless unit tests for window->process identity resolution (WM-03).

No live X server and no real dwm socket: the Xlib seam is replaced by a small
fake reader and ``/proc`` is a ``tmp_path`` directory. Every branch of
``resolve_pid`` plus the pure ``/proc`` parsers is exercised.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from xrandrw.windows import (
    parse_starttime_from_stat,
    read_proc_cmdline,
    read_proc_comm,
    read_proc_identity,
    resolve_pid,
)


# --- fixtures / helpers ------------------------------------------------------

def _stat_line(pid: int, comm: str, starttime: int) -> str:
    """Build a /proc/<pid>/stat line whose field 22 (starttime) is known.

    After the last ')', element 0 is the state char and element 19 is field 22
    (starttime), per proc(5).
    """
    after = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime)] + ["0", "0", "0"]
    return f"{pid} ({comm}) " + " ".join(after) + "\n"


def _make_proc(tmp_path, pid: int, comm: str, starttime: int,
               cmdline: bytes | None = b"prog\x00--flag\x00arg\x00"):
    d = tmp_path / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(_stat_line(pid, comm, starttime))
    (d / "comm").write_text(comm + "\n")
    if cmdline is not None:
        (d / "cmdline").write_bytes(cmdline)
    return tmp_path


def _reader(*, pid=None, machine="localhost", xres=None, has_xres=True):
    return SimpleNamespace(
        net_wm_pid=lambda xid: pid,
        client_machine=lambda xid: machine,
        xres_pid=lambda xid: xres,
        has_xres=lambda: has_xres,
    )


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_windows_identity")
    lg.setLevel(logging.DEBUG)
    return lg


# --- parse_starttime_from_stat ----------------------------------------------

def test_parse_starttime_simple():
    line = _stat_line(1234, "st", 987654)
    assert parse_starttime_from_stat(line) == 987654


def test_parse_starttime_comm_with_spaces_and_parens():
    # comm can itself contain spaces and parentheses; parse anchors on LAST ')'.
    line = _stat_line(4242, "weird ) name", 555111)
    assert parse_starttime_from_stat(line) == 555111


def test_parse_starttime_unparseable_raises():
    with pytest.raises(ValueError):
        parse_starttime_from_stat("no-paren-here at all")


# --- read_proc_comm / read_proc_cmdline / read_proc_identity ----------------

def test_read_proc_comm(tmp_path):
    _make_proc(tmp_path, 1234, "st", 100)
    assert read_proc_comm(1234, proc_root=str(tmp_path)) == "st"
    assert read_proc_comm(9999, proc_root=str(tmp_path)) is None


def test_read_proc_cmdline_replaces_nul_separators(tmp_path):
    _make_proc(tmp_path, 1234, "st", 100, cmdline=b"prog\x00--flag\x00arg\x00")
    assert read_proc_cmdline(1234, proc_root=str(tmp_path)) == "prog --flag arg"


def test_read_proc_cmdline_missing_is_none(tmp_path):
    assert read_proc_cmdline(9999, proc_root=str(tmp_path)) is None


def test_read_proc_cmdline_empty_is_none(tmp_path):
    _make_proc(tmp_path, 7, "k", 1, cmdline=b"")
    # kernel threads have an empty cmdline -> stripped to "" -> None
    assert read_proc_cmdline(7, proc_root=str(tmp_path)) is None


def test_read_proc_identity_ok(tmp_path):
    _make_proc(tmp_path, 1234, "st", 424242)
    assert read_proc_identity(1234, proc_root=str(tmp_path)) == (1234, 424242, "st")


def test_read_proc_identity_missing_is_none(tmp_path, logger, caplog):
    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_windows_identity"):
        assert read_proc_identity(9999, proc_root=str(tmp_path), logger=logger) is None
    assert any(getattr(r, "event", None) == "window_proc_missing" for r in caplog.records)


# --- resolve_pid -------------------------------------------------------------

def test_resolve_pid_uses_net_wm_pid_without_xres(tmp_path, logger, caplog):
    _make_proc(tmp_path, 1234, "st", 424242)
    calls = {"xres": 0}

    def xres(xid):
        calls["xres"] += 1
        return 5555

    reader = SimpleNamespace(
        net_wm_pid=lambda xid: 1234,
        client_machine=lambda xid: "localhost",
        xres_pid=xres,
        has_xres=lambda: True,
    )
    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_windows_identity"):
        got = resolve_pid(0xABC, reader, hostname="localhost",
                          proc_root=str(tmp_path), logger=logger)
    assert got == (1234, 424242, "st")
    assert calls["xres"] == 0, "_NET_WM_PID present -> xres must not be called"
    assert any(getattr(r, "event", None) == "window_pid_resolve" for r in caplog.records)


def test_resolve_pid_falls_back_to_xres(tmp_path, logger):
    _make_proc(tmp_path, 2200, "app", 111)
    reader = _reader(pid=None, machine="localhost", xres=2200)
    got = resolve_pid(0x1, reader, hostname="localhost",
                      proc_root=str(tmp_path), logger=logger)
    assert got == (2200, 111, "app")


def test_resolve_pid_nonlocal_machine_skipped(tmp_path, logger, caplog):
    _make_proc(tmp_path, 1234, "st", 1)
    reader = _reader(pid=1234, machine="otherhost.example")
    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_windows_identity"):
        got = resolve_pid(0x1, reader, hostname="localhost",
                          proc_root=str(tmp_path), logger=logger)
    assert got is None
    assert any(getattr(r, "event", None) == "window_skip_nonlocal" for r in caplog.records)


def test_resolve_pid_local_machine_matches_default_hostname(tmp_path, logger):
    import socket as _socket
    host = _socket.gethostname()
    _make_proc(tmp_path, 321, "loc", 77)
    reader = _reader(pid=321, machine=host)
    # hostname defaults to socket.gethostname() when None
    got = resolve_pid(0x1, reader, proc_root=str(tmp_path), logger=logger)
    assert got == (321, 77, "loc")


def test_resolve_pid_no_pid_returns_none(tmp_path, logger):
    reader = _reader(pid=None, machine="localhost", xres=None)
    assert resolve_pid(0x1, reader, hostname="localhost",
                       proc_root=str(tmp_path), logger=logger) is None


def test_resolve_pid_dead_proc_returns_none(tmp_path, logger, caplog):
    # pid resolved but /proc entry absent -> skip, never raise
    reader = _reader(pid=4040, machine="localhost")
    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_windows_identity"):
        got = resolve_pid(0x1, reader, hostname="localhost",
                          proc_root=str(tmp_path), logger=logger)
    assert got is None
    assert any(getattr(r, "event", None) == "window_proc_missing" for r in caplog.records)


def test_resolve_pid_empty_machine_is_treated_as_local(tmp_path, logger):
    # An absent WM_CLIENT_MACHINE (None/empty) must NOT skip the window.
    _make_proc(tmp_path, 88, "x", 9)
    reader = _reader(pid=88, machine=None)
    got = resolve_pid(0x1, reader, hostname="localhost",
                      proc_root=str(tmp_path), logger=logger)
    assert got == (88, 9, "x")
