"""End-to-end functional test of the Phase-9 capture pipeline, headless.

Wires the whole pipeline together with NO live X and NO real dwm:
  * a REAL ``AF_UNIX`` :class:`FakeDwmServer` speaking the DWM-IPC protocol,
  * a mocked Xlib ``WindowXReader``/``RandRReader`` seam,
  * a ``tmp_path`` fake ``/proc`` directory,
driving ``capture_windows`` and asserting resolved identity + captured state +
output/EDID association together (WM-03 + WM-04). Also closes coverage gaps on
``windows.py`` toward the Phase-12 >=90% gate (TEST-03).
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

import xrandrw.windows as win_mod
from xrandrw.windows import WindowXReader, capture_windows
from xrandrw.xrandr import Output
from dwmipc_fake_server import FakeDwmServer


HOST = "func-test-host"


@pytest.fixture
def sock_path(tmp_path):
    return tmp_path / "dwm.sock"


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")  # module logger, so seam events are captured
    lg.setLevel(logging.DEBUG)
    return lg


def _wait_for(pred, deadline=2.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# Two monitors, each owning one distinct client xid.
_MONITORS = [
    {"num": 0, "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
     "layout": {"symbol": "[]="}, "clients": {"all": [0x1400001]}},
    {"num": 1, "monitor_geometry": {"x": 1920, "y": 0, "width": 1920, "height": 1080},
     "layout": {"symbol": "[]="}, "clients": {"all": [0x1400002]}},
]

# Real nested geometry.current shape (spike 001) so build_record's nested path runs.
_CLIENT = {
    "name": "terminal", "tags": 7, "monitor_number": 0,
    "geometry": {"current": {"x": 10, "y": 20, "width": 800, "height": 600}},
    "states": {"is_floating": True, "is_fullscreen": False},
}


def _client_for_xid(xid):
    """Per-window client whose monitor_number matches the monitor it lives on.

    WARNING-5 contract: output/EDID association keys on the client's OWN
    monitor_number, so the fixture must report the true owning monitor
    (0x1400001 -> monitor 0 / DP-1, 0x1400002 -> monitor 1 / DP-2).
    """
    mnum = 0 if xid == 0x1400001 else 1
    c = dict(_CLIENT)
    c["monitor_number"] = mnum
    return c


def _outputs():
    return {
        "DP-1": Output(name="DP-1", connected=True, current_mode=(1920, 1080),
                       position=(0, 0), edid_sha1="edidAAA"),
        "DP-2": Output(name="DP-2", connected=True, current_mode=(1920, 1080),
                       position=(1920, 0), edid_sha1="edidBBB"),
    }


def _fake_randr(outs):
    return SimpleNamespace(read=lambda logger=None: dict(outs))


def _fake_xreader(machine_for):
    """machine_for: callable xid -> WM_CLIENT_MACHINE string (local == HOST)."""
    return SimpleNamespace(
        net_wm_pid=lambda xid: 1234,
        client_machine=machine_for,
        xres_pid=lambda xid: None,
        has_xres=lambda: True,
    )


def _make_proc(tmp_path, pid=1234, comm="terminal app", starttime=765):
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    after = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime)] + ["0", "0"]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after) + "\n")
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(b"terminal\x00--login\x00")
    return str(tmp_path / "proc")


def test_functional_happy_path(sock_path, tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path)
    # read_edids would open a live Display; edids are already set on the outputs.
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    with FakeDwmServer(sock_path, mode="auto", monitors=_MONITORS, client=_client_for_xid):
        recs = capture_windows(
            reader=_fake_randr(_outputs()),
            xreader=_fake_xreader(lambda xid: HOST),
            proc_root=proc_root, hostname=HOST,
            sock_path=str(sock_path), logger=logger,
        )

    assert len(recs) == 2
    by_out = {r.output: r for r in recs}
    assert set(by_out) == {"DP-1", "DP-2"}

    r = by_out["DP-1"]
    # WM-03: resolved local identity from the fake /proc
    assert (r.pid, r.starttime, r.comm) == (1234, 765, "terminal app")
    assert r.cmdline == "terminal --login"
    # WM-04: captured state
    assert r.tags == 7
    assert r.is_floating is True and r.is_fullscreen is False
    assert r.geometry == {"x": 10, "y": 20, "width": 800, "height": 600}
    # WM-04: association to the connector + EDID for the monitor it sits on
    assert r.edid == "edidAAA"
    assert by_out["DP-2"].edid == "edidBBB"


def test_functional_nonlocal_skip(sock_path, tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    # xid 0x1400001 (monitor 0 / DP-1) is remote; 0x1400002 (DP-2) is local.
    def machine_for(xid):
        return "remote.example.net" if xid == 0x1400001 else HOST

    with FakeDwmServer(sock_path, mode="auto", monitors=_MONITORS, client=_client_for_xid):
        with caplog.at_level(logging.DEBUG, logger="xrandrw"):
            recs = capture_windows(
                reader=_fake_randr(_outputs()),
                xreader=_fake_xreader(machine_for),
                proc_root=proc_root, hostname=HOST,
                sock_path=str(sock_path), logger=logger,
            )

    assert len(recs) == 1
    assert recs[0].output == "DP-2"
    assert any(getattr(r, "event", None) == "window_skip_nonlocal" for r in caplog.records)


def test_functional_degrade_to_empty(sock_path, tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    # A server that RSTs on accept makes get_monitors raise DwmIpcUnavailable.
    with FakeDwmServer(sock_path, mode="rst_on_accept"):
        recs = capture_windows(
            reader=_fake_randr(_outputs()),
            xreader=_fake_xreader(lambda xid: HOST),
            proc_root=proc_root, hostname=HOST,
            sock_path=str(sock_path), logger=logger,
        )
    assert recs == []


# ---------------------------------------------------------------------------
# Coverage aim (Task 2): exercise the live WindowXReader seam + remaining
# branches with a fake Display (mirrors FakeReadDisplay in test_native_read).
# ---------------------------------------------------------------------------

class _FakeWin:
    def __init__(self, props):
        self._props = props  # atom(int) -> prop object or absent

    def get_full_property(self, atom, typ):
        return self._props.get(atom)


class _FakeXDisplay:
    """Configurable stand-in for python-xlib Display for the seam tests."""

    def __init__(self, *, atoms=None, props=None, has_ext=True,
                 ids_reply=None, fail=frozenset()):
        self._atoms = atoms or {"_NET_WM_PID": 1, "WM_CLIENT_MACHINE": 2}
        self._props = props or {}
        self._has_ext = has_ext
        self._ids_reply = ids_reply
        self._fail = fail
        self.closed = False

    def get_atom(self, name):
        if "get_atom" in self._fail:
            raise RuntimeError("atom boom")
        return self._atoms[name]

    def create_resource_object(self, kind, xid):
        return _FakeWin(self._props)

    def has_extension(self, name):
        if "has_extension" in self._fail:
            raise RuntimeError("ext query boom")
        return self._has_ext

    def res_query_client_ids(self, specs):
        if "res" in self._fail:
            raise RuntimeError("res boom")
        return self._ids_reply

    def close(self):
        self.closed = True


def _patch_display(monkeypatch, fake):
    monkeypatch.setattr(win_mod.display, "Display", lambda: fake)
    return fake


def test_seam_net_wm_pid_value_missing_and_error(monkeypatch, caplog):
    # value present
    fake = _patch_display(monkeypatch, _FakeXDisplay(
        props={1: SimpleNamespace(value=[4321])}))
    assert WindowXReader().net_wm_pid(0x1) == 4321
    assert fake.closed
    # property absent -> None
    _patch_display(monkeypatch, _FakeXDisplay(props={}))
    assert WindowXReader().net_wm_pid(0x1) is None
    # non-positive -> None
    _patch_display(monkeypatch, _FakeXDisplay(props={1: SimpleNamespace(value=[0])}))
    assert WindowXReader().net_wm_pid(0x1) is None
    # raised Xlib error -> None (logged)
    _patch_display(monkeypatch, _FakeXDisplay(fail={"get_atom"}))
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        assert WindowXReader().net_wm_pid(0x1) is None
    assert any(getattr(r, "event", None) == "window_pid_prop_fail" for r in caplog.records)


def test_seam_client_machine_bytes_intarray_and_error(monkeypatch, caplog):
    # bytes value with trailing NUL
    _patch_display(monkeypatch, _FakeXDisplay(props={2: SimpleNamespace(value=b"myhost\x00")}))
    assert WindowXReader().client_machine(0x1) == "myhost"
    # int-array value (8-bit prop handed back as ints) -> "hi"
    _patch_display(monkeypatch, _FakeXDisplay(props={2: SimpleNamespace(value=[104, 105])}))
    assert WindowXReader().client_machine(0x1) == "hi"
    # absent -> None
    _patch_display(monkeypatch, _FakeXDisplay(props={}))
    assert WindowXReader().client_machine(0x1) is None
    # error -> None (logged)
    _patch_display(monkeypatch, _FakeXDisplay(fail={"get_atom"}))
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        assert WindowXReader().client_machine(0x1) is None
    assert any(getattr(r, "event", None) == "window_machine_fail" for r in caplog.records)


def test_seam_has_xres_present_absent_and_error(monkeypatch, caplog):
    _patch_display(monkeypatch, _FakeXDisplay(has_ext=True))
    assert WindowXReader().has_xres() is True
    _patch_display(monkeypatch, _FakeXDisplay(has_ext=False))
    assert WindowXReader().has_xres() is False
    # error path logs the degrade ONCE per reader
    _patch_display(monkeypatch, _FakeXDisplay(fail={"has_extension"}))
    rdr = WindowXReader()
    with caplog.at_level(logging.INFO, logger="xrandrw"):
        assert rdr.has_xres() is False
        assert rdr.has_xres() is False  # second call must NOT log again
    absents = [r for r in caplog.records if getattr(r, "event", None) == "window_xres_absent"]
    assert len(absents) == 1


def test_seam_xres_pid_present_guarded_and_error(monkeypatch, caplog):
    reply = SimpleNamespace(ids=[SimpleNamespace(spec=SimpleNamespace(mask=2), value=[9090])])
    _patch_display(monkeypatch, _FakeXDisplay(has_ext=True, ids_reply=reply))
    assert WindowXReader().xres_pid(0x1) == 9090
    # XRes absent -> guard returns None
    _patch_display(monkeypatch, _FakeXDisplay(has_ext=False))
    assert WindowXReader().xres_pid(0x1) is None
    # empty ids -> None
    _patch_display(monkeypatch, _FakeXDisplay(has_ext=True, ids_reply=SimpleNamespace(ids=[])))
    assert WindowXReader().xres_pid(0x1) is None
    # query raises -> None (logged)
    _patch_display(monkeypatch, _FakeXDisplay(has_ext=True, fail={"res"}))
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        assert WindowXReader().xres_pid(0x1) is None
    assert any(getattr(r, "event", None) == "window_xres_degrade" for r in caplog.records)


# --- remaining pure-branch gaps ---------------------------------------------

def test_parse_starttime_short_line_raises():
    from xrandrw.windows import parse_starttime_from_stat
    with pytest.raises(ValueError):
        parse_starttime_from_stat("1 (c) S 2 3")  # has ')' but < 20 trailing fields


def test_read_proc_identity_comm_missing(tmp_path, caplog):
    from xrandrw.windows import read_proc_identity
    d = tmp_path / "77"
    d.mkdir()
    after = ["S"] + [str(i) for i in range(1, 19)] + ["500", "0", "0"]
    (d / "stat").write_text("77 (x) " + " ".join(after) + "\n")  # no comm file
    with caplog.at_level(logging.DEBUG, logger="xrandrw"):
        assert read_proc_identity(77, proc_root=str(tmp_path)) is None
    assert any(getattr(r, "event", None) == "window_proc_missing" for r in caplog.records)


def test_resolve_pid_reader_raises_is_caught(logger, caplog):
    from xrandrw.windows import resolve_pid

    def boom(xid):
        raise RuntimeError("reader exploded")

    reader = SimpleNamespace(client_machine=boom, net_wm_pid=lambda xid: None,
                             xres_pid=lambda xid: None, has_xres=lambda: True)
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        assert resolve_pid(0x1, reader, hostname=HOST, logger=logger) is None
    assert any(getattr(r, "event", None) == "window_resolve_fail" for r in caplog.records)


def test_capture_default_seams_with_unavailable_ipc(tmp_path, monkeypatch, logger):
    # reader=None/xreader=None construct the real seams (no X connection yet);
    # get_monitors raising DwmIpcUnavailable returns [] before any live X read.
    from xrandrw.dwmipc import DwmIpcUnavailable

    def boom(path=None, **kw):
        raise DwmIpcUnavailable("no socket")

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", boom)
    recs = capture_windows(proc_root=str(tmp_path), hostname=HOST,
                           sock_path="/nope", logger=logger)
    assert recs == []


def test_capture_malformed_client_skipped(tmp_path, monkeypatch, logger, caplog):
    from xrandrw.dwmipc import DwmIpcUnavailable  # noqa: F401 (import parity)
    proc_root = _make_proc(tmp_path)
    outs = _outputs()
    mons = [{"num": 0, "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
             "clients": {"all": [0x1400001]}}]
    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    # malformed client: missing 'states' -> build_record raises KeyError -> skip
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client",
                        lambda xid, path=None, **kw: {"name": "x", "tags": 1,
                                                      "monitor_number": 0,
                                                      "geometry": {"x": 0, "y": 0,
                                                                   "width": 1, "height": 1}})
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        recs = capture_windows(reader=_fake_randr(outs), xreader=_fake_xreader(lambda xid: HOST),
                               proc_root=proc_root, hostname=HOST,
                               sock_path="/x", logger=logger)
    assert recs == []
    assert any(getattr(r, "event", None) == "window_capture_skip" for r in caplog.records)


def test_capture_unmatched_monitor_pipeline(tmp_path, monkeypatch, logger):
    # A monitor whose geometry matches no output -> record with output=None/edid=None.
    proc_root = _make_proc(tmp_path)
    outs = _outputs()
    mons = [{"num": 5, "monitor_geometry": {"x": 700, "y": 700, "width": 640, "height": 480},
             "clients": {"all": [0x1400001]}}]
    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client",
                        lambda xid, path=None, **kw: _CLIENT)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    recs = capture_windows(reader=_fake_randr(outs), xreader=_fake_xreader(lambda xid: HOST),
                           proc_root=proc_root, hostname=HOST,
                           sock_path="/x", logger=logger)
    assert len(recs) == 1
    assert recs[0].output is None and recs[0].edid is None
