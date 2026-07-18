"""Headless tests for state capture + output/EDID association (WM-04).

No live X server and no real dwm socket: RandR is a fake ``{connector: Output}``
map, the dwm IPC verbs are monkeypatched, identity is a fake reader, and
``/proc`` is a ``tmp_path`` directory.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import xrandrw.windows as win_mod
from xrandrw.xrandr import Output
from xrandrw.dwmipc import DwmIpcUnavailable
from xrandrw.windows import (
    WindowRecord,
    build_record,
    capture_windows,
    match_dwm_monitor_to_output,
)

from randr_fixtures import CRTC_INFOS, MODES, OUTPUT_INFOS, PRIMARY_ID


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_windows_capture")
    lg.setLevel(logging.DEBUG)
    return lg


def _out(name, *, connected=True, mode=None, position=None, edid=None):
    return Output(name=name, connected=connected, current_mode=mode,
                  position=position, edid_sha1=edid)


def _mon(num, x, y, w, h, clients=None):
    d = {"num": num, "monitor_geometry": {"x": x, "y": y, "width": w, "height": h}}
    if clients is not None:
        d["clients"] = {"all": clients}
    return d


# --- Output.position via the pure mapper ------------------------------------

def test_mapper_sets_position_from_crtc():
    from xrandrw.xrandr import randr_resources_to_outputs
    outs = randr_resources_to_outputs(OUTPUT_INFOS, CRTC_INFOS, MODES, PRIMARY_ID)
    # DSI-1 is driven by CRTC 64 at (0,0)
    assert outs["DSI-1"].position == (0, 0)
    # HDMI-2 has no CRTC (crtc=0) -> position None
    assert outs["HDMI-2"].position is None


# --- match_dwm_monitor_to_output --------------------------------------------

def test_match_single_monitor_to_output(logger):
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0))}
    mons = [_mon(0, 0, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: "DP-1"}


def test_match_two_identical_size_monitors_by_position(logger):
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0)),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(1920, 0)),
    }
    mons = [_mon(0, 0, 0, 1920, 1080), _mon(1, 1920, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: "DP-1", 1: "DP-2"}


def test_match_no_geometry_match_is_none(logger, caplog):
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0))}
    mons = [_mon(0, 500, 500, 1024, 768)]
    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_windows_capture"):
        got = match_dwm_monitor_to_output(mons, outs, logger=logger)
    assert got == {0: None}
    assert any(getattr(r, "event", None) == "window_monitor_unmatched" for r in caplog.records)


def test_match_disconnected_output_never_matched(logger):
    outs = {"DP-1": _out("DP-1", connected=False, mode=(1920, 1080), position=(0, 0))}
    mons = [_mon(0, 0, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: None}


def test_match_ambiguous_identical_geometry_is_none(logger, caplog):
    # Warning 2: two connected outputs share identical position+mode for one dwm
    # monitor geometry (mirror/clone or identical-EDID risk) -> None + logged.
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0)),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(0, 0)),
    }
    mons = [_mon(0, 0, 0, 1920, 1080)]
    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_windows_capture"):
        got = match_dwm_monitor_to_output(mons, outs, logger=logger)
    assert got == {0: None}, "ambiguous multi-match must not guess-associate"
    events = [r for r in caplog.records if getattr(r, "event", None) == "window_monitor_unmatched"]
    assert events, "ambiguous match must log window_monitor_unmatched"


# --- WindowRecord round-trip ------------------------------------------------

def test_window_record_round_trip():
    rec = WindowRecord(
        xid=0x1400001, pid=1234, starttime=999, comm="term", cmdline="term --x",
        output="DP-1", edid="deadbeef", monitor_number=0, tags=1,
        is_floating=False, is_fullscreen=True,
        geometry={"x": 0, "y": 0, "width": 800, "height": 600},
    )
    d = rec.to_dict()
    assert isinstance(d, dict)
    assert WindowRecord.from_dict(d) == rec


# --- build_record ------------------------------------------------------------

def _client(nested=True):
    geom = {"x": 1, "y": 2, "width": 300, "height": 400}
    c = {
        "name": "app", "tags": 5, "monitor_number": 1,
        "states": {"is_floating": True, "is_fullscreen": False},
    }
    c["geometry"] = {"current": geom} if nested else geom
    return c


def test_build_record_nested_geometry():
    rec = build_record(0xABC, (1234, 555, "app"), _client(nested=True),
                       "DP-2", "sha1edid", cmdline="app --flag")
    assert rec.geometry == {"x": 1, "y": 2, "width": 300, "height": 400}
    assert (rec.monitor_number, rec.tags) == (1, 5)
    assert rec.is_floating is True and rec.is_fullscreen is False
    assert rec.output == "DP-2" and rec.edid == "sha1edid"
    assert rec.pid == 1234 and rec.starttime == 555 and rec.comm == "app"
    assert rec.cmdline == "app --flag"


def test_build_record_flat_geometry_fallback():
    rec = build_record(0xABC, (1, 2, "c"), _client(nested=False), None, None)
    assert rec.geometry == {"x": 1, "y": 2, "width": 300, "height": 400}
    assert rec.output is None and rec.edid is None


# --- capture_windows orchestrator -------------------------------------------

def _fake_reader(outs):
    return SimpleNamespace(read=lambda logger=None: dict(outs))


def _fake_xreader(pid, machine="localhost"):
    return SimpleNamespace(
        net_wm_pid=lambda xid: pid,
        client_machine=lambda xid: machine,
        xres_pid=lambda xid: None,
        has_xres=lambda: True,
    )


def _make_proc(tmp_path, pid, comm="app", starttime=42, cmdline=b"app\x00-x\x00"):
    d = tmp_path / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    after = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime)] + ["0", "0"]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after) + "\n")
    (d / "comm").write_text(comm + "\n")
    (d / "cmdline").write_bytes(cmdline)
    return tmp_path


def test_capture_two_clients_two_monitors(tmp_path, monkeypatch, logger):
    _make_proc(tmp_path, 1234)
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA"),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(1920, 0), edid="edidB"),
    }
    mons = [
        _mon(0, 0, 0, 1920, 1080, clients=[0x11]),
        _mon(1, 1920, 0, 1920, 1080, clients=[0x22]),
    ]

    def client_for(xid, path=None, **kw):
        mnum = 0 if xid == 0x11 else 1
        return {"name": "app", "tags": 1, "monitor_number": mnum,
                "geometry": {"current": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "states": {"is_floating": False, "is_fullscreen": False}}

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    recs = capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                           proc_root=str(tmp_path), hostname="localhost",
                           sock_path="/x", logger=logger)
    assert len(recs) == 2
    by_out = {r.output: r for r in recs}
    assert by_out["DP-1"].edid == "edidA"
    assert by_out["DP-2"].edid == "edidB"
    assert all(r.cmdline == "app -x" for r in recs)


def test_capture_skips_unresolved_identity(tmp_path, monkeypatch, logger, caplog):
    # xreader yields no pid -> resolve_pid returns None -> window skipped.
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="e")}
    mons = [_mon(0, 0, 0, 1920, 1080, clients=[0x11])]
    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client",
                        lambda xid, path=None, **kw: _client())
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    xreader = SimpleNamespace(net_wm_pid=lambda xid: None, client_machine=lambda xid: "localhost",
                              xres_pid=lambda xid: None, has_xres=lambda: True)
    with caplog.at_level(logging.DEBUG, logger="xrandrw"):
        recs = capture_windows(reader=_fake_reader(outs), xreader=xreader,
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    assert recs == []


def test_capture_get_monitors_unavailable_returns_empty(tmp_path, monkeypatch, logger, caplog):
    def boom(path=None, **kw):
        raise DwmIpcUnavailable("server gone")
    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", boom)
    with caplog.at_level(logging.DEBUG, logger="xrandrw"):
        recs = capture_windows(reader=_fake_reader({}), xreader=_fake_xreader(1),
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    assert recs == []
    assert any(getattr(r, "event", None) == "window_capture_unavailable"
               for r in caplog.records)


def test_capture_malformed_clients_shape_skips_monitor_not_capture(tmp_path, monkeypatch, logger, caplog):
    # BLOCKER 1: a clients that isn't a dict (['not','a','dict']) and a clients
    # whose 'all' isn't a list ({'all': 12345}) must each yield a graceful
    # per-monitor skip -- other monitors are still captured, no exception.
    _make_proc(tmp_path, 1234)
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA"),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(1920, 0), edid="edidB"),
        "DP-3": _out("DP-3", mode=(1280, 720), position=(0, 1080), edid="edidC"),
    }
    mons = [
        # monitor 0: clients is a list, not a dict -> skip this monitor only
        {"num": 0, "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
         "clients": ["not", "a", "dict"]},
        # monitor 1: clients.all is an int, not a list -> skip this monitor only
        {"num": 1, "monitor_geometry": {"x": 1920, "y": 0, "width": 1920, "height": 1080},
         "clients": {"all": 12345}},
        # monitor 2: well-formed -> its window is still captured
        _mon(2, 0, 1080, 1280, 720, clients=[0x33]),
    ]

    def client_for(xid, path=None, **kw):
        return {"name": "app", "tags": 1, "monitor_number": 2,
                "geometry": {"current": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "states": {"is_floating": False, "is_fullscreen": False}}

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    with caplog.at_level(logging.DEBUG, logger="xrandrw"):
        recs = capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    # The well-formed monitor's window survives; malformed ones did not abort it.
    assert len(recs) == 1
    assert recs[0].output == "DP-3" and recs[0].edid == "edidC"
    skips = [r for r in caplog.records
             if getattr(r, "event", None) == "window_capture_skip"]
    assert len(skips) == 2, "both malformed monitors must log a graceful skip"


@pytest.mark.parametrize("bad_geom", [None, ["x", "y"], {"x": 0, "y": 0}])
def test_build_record_malformed_geometry_raises(bad_geom):
    # WARNING 3: geometry that is None, a list, or a dict missing keys must raise
    # (ValueError) so the per-window catch skips the record.
    client = {"name": "app", "tags": 1, "monitor_number": 0,
              "states": {"is_floating": False, "is_fullscreen": False},
              "geometry": bad_geom}
    with pytest.raises((ValueError, KeyError)):
        build_record(0xABC, (1, 2, "c"), client, None, None)


def test_capture_malformed_geometry_skips_that_window_only(tmp_path, monkeypatch, logger, caplog):
    # WARNING 3 end-to-end: one client with bad geometry is skipped, the other
    # (well-formed) is still captured.
    _make_proc(tmp_path, 1234)
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA"),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(1920, 0), edid="edidB"),
    }
    mons = [
        _mon(0, 0, 0, 1920, 1080, clients=[0x11]),
        _mon(1, 1920, 0, 1920, 1080, clients=[0x22]),
    ]

    def client_for(xid, path=None, **kw):
        mnum = 0 if xid == 0x11 else 1
        base = {"name": "app", "tags": 1, "monitor_number": mnum,
                "states": {"is_floating": False, "is_fullscreen": False}}
        # xid 0x11 has malformed geometry (a list); 0x22 is well-formed.
        base["geometry"] = ["bad"] if xid == 0x11 else {"current": {"x": 0, "y": 0, "width": 10, "height": 10}}
        return base

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    with caplog.at_level(logging.DEBUG, logger="xrandrw"):
        recs = capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    assert len(recs) == 1
    assert recs[0].output == "DP-2"
    assert any(getattr(r, "event", None) == "window_capture_skip" for r in caplog.records)


def test_capture_x_read_raises_returns_empty(tmp_path, monkeypatch, logger, caplog):
    # BLOCKER 2 (WR-01): a reader whose read() raises on a hotplug/X-restart race
    # must degrade to [] (like dwmipc-unavailable), never propagate out.
    mons = [_mon(0, 0, 0, 1920, 1080, clients=[0x11])]
    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    def boom(logger=None):
        raise ConnectionError("X server went away")

    reader = SimpleNamespace(read=boom)
    with caplog.at_level(logging.DEBUG, logger="xrandrw"):
        recs = capture_windows(reader=reader, xreader=_fake_xreader(1234),
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    assert recs == []
    assert any(getattr(r, "event", None) == "window_capture_unavailable"
               for r in caplog.records)


def test_capture_get_dwm_client_unavailable_skips_one(tmp_path, monkeypatch, logger):
    _make_proc(tmp_path, 1234)
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA"),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(1920, 0), edid="edidB"),
    }
    mons = [
        _mon(0, 0, 0, 1920, 1080, clients=[0x11]),
        _mon(1, 1920, 0, 1920, 1080, clients=[0x22]),
    ]

    def client_for(xid, path=None, **kw):
        if xid == 0x11:
            raise DwmIpcUnavailable("client fetch failed")
        return {"name": "app", "tags": 1, "monitor_number": 1,
                "geometry": {"current": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "states": {"is_floating": False, "is_fullscreen": False}}

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    recs = capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                           proc_root=str(tmp_path), hostname="localhost",
                           sock_path="/x", logger=logger)
    assert len(recs) == 1
    assert recs[0].output == "DP-2"
