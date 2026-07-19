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


def test_match_unplugged_but_lit_output_is_named(logger):
    # INVERTED at 14-08 (was test_match_disconnected_output_never_matched, which
    # asserted {0: None}). An output whose HPD has dropped but whose CRTC is STILL
    # LIT is exactly the live 04:21:43,109 state: it is still driving pixels and dwm
    # still has that monitor, so it MUST be nameable. Refusing to name it captured
    # all three windows with output=None, and _record_displaced can never move a
    # record whose output is None -> the windows were stranded.
    #
    # Decision D of 09-CONTEXT ("never guess-associate") is NARROWED, not dropped:
    # `connected` is now a TIE-BREAK applied only to a MULTI-match, so "guess" means
    # an ambiguity that survives the tie-break. See the docstring on
    # match_dwm_monitor_to_output and .planning/debug/relocate-replug-bounce.md.
    outs = {"DP-1": _out("DP-1", connected=False, mode=(1920, 1080), position=(0, 0))}
    mons = [_mon(0, 0, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: "DP-1"}


def test_match_dark_output_never_matched(logger):
    # The DARK output is the one that must never be a tagmon target -- and it needs
    # no special case: no CRTC means position is None, which can never equal a dwm
    # monitor's integer origin.
    outs = {"DP-1": _out("DP-1", connected=False, mode=None, position=None)}
    mons = [_mon(0, 0, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: None}


def test_match_mirror_pair_resolves_to_the_connected_output(logger):
    # NON-REGRESSION for the tie-break, and its whole justification. A mirrored /
    # cloned pair or a dock-port handover presents two outputs at the SAME origin
    # and mode with one connected and one unplugged-but-lit. Today that binds to the
    # connected one; a bare deletion of the `o.connected` conjunct would have made it
    # candidates=2 -> None, WIDENING the mirroring failure from "both connected" to
    # "either connected" on a MUTATING path (this feeds _tagmon_to_target).
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0)),
        "HDMI-1": _out("HDMI-1", connected=False, mode=(1920, 1080), position=(0, 0)),
    }
    mons = [_mon(0, 0, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: "DP-1"}


def test_match_non_integer_geometry_is_never_coerced(logger):
    # ASVS V5: monitor geometry crosses the untrusted dwm.sock boundary and Python's
    # True == 1 would let a boolean coerce into a position match. Shape-check first.
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(1, 0))}
    mons = [_mon(0, True, 0, 1920, 1080)]
    assert match_dwm_monitor_to_output(mons, outs, logger=logger) == {0: None}


def test_unmatched_event_carries_per_output_diagnostics(logger, caplog):
    # The live failure had to be reconstructed from the ABSENCE of an EDID log line
    # because this event carried only a candidate COUNT. Name and level are
    # unchanged (existing assertions depend on both); the payload is enriched.
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0))}
    mons = [_mon(0, 500, 500, 1024, 768)]
    with caplog.at_level(logging.INFO, logger="xrandrw.test_windows_capture"):
        match_dwm_monitor_to_output(mons, outs, logger=logger)
    rec = next(r for r in caplog.records
               if getattr(r, "event", None) == "window_monitor_unmatched")
    assert rec.levelno == logging.INFO
    assert "DP-1" in rec.outputs and "conn" in rec.outputs
    assert "(0, 0)" in rec.outputs and "(1920, 1080)" in rec.outputs
    # WR-02: a true zero-geometry-match reports zero on BOTH counters.
    assert rec.candidates == 0 and rec.candidates_after_tiebreak == 0


def test_unmatched_event_distinguishes_tiebreak_elimination_from_zero_match(logger, caplog):
    """WR-02: `candidates=0` must no longer be ambiguous.

    A mirror pair that are BOTH unplugged-but-lit match the geometry, then the
    connected-preferring tie-break eliminates both. Pre-fix that logged
    `candidates=0` -- identical to "nothing matched the geometry at all", which is
    exactly the ambiguity that made the live 04:21:43,109 incident so hard to read.
    """
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), connected=False),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(0, 0), connected=False),
    }
    mons = [_mon(0, 0, 0, 1920, 1080)]
    with caplog.at_level(logging.INFO, logger="xrandrw.test_windows_capture"):
        got = match_dwm_monitor_to_output(mons, outs, logger=logger)
    assert got == {0: None}, "still a refusal -- the tie-break resolved nothing"
    rec = next(r for r in caplog.records
               if getattr(r, "event", None) == "window_monitor_unmatched")
    assert rec.candidates == 2, "two outputs DID match on geometry"
    assert rec.candidates_after_tiebreak == 0, "and the tie-break eliminated both"


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
    rec = build_record(0xABC, (1234, 555, "app", "app --flag"),
                       _client(nested=True), "DP-2", "sha1edid")
    assert rec.geometry == {"x": 1, "y": 2, "width": 300, "height": 400}
    assert (rec.monitor_number, rec.tags) == (1, 5)
    assert rec.is_floating is True and rec.is_fullscreen is False
    assert rec.output == "DP-2" and rec.edid == "sha1edid"
    assert rec.pid == 1234 and rec.starttime == 555 and rec.comm == "app"
    assert rec.cmdline == "app --flag"


def test_build_record_flat_geometry_fallback():
    rec = build_record(0xABC, (1, 2, "c", None), _client(nested=False), None, None)
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


def test_capture_threads_timeout_into_every_dwmipc_call(tmp_path, monkeypatch, logger):
    # AUDIT-B: an explicit timeout must reach BOTH get_monitors and
    # get_dwm_client so every dwm-ipc round-trip honours the caller's bound.
    _make_proc(tmp_path, 1234)
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="e")}
    mons = [_mon(0, 0, 0, 1920, 1080, clients=[0x11])]
    seen = {"monitors": [], "client": []}

    def mons_for(path=None, timeout=None, **kw):
        seen["monitors"].append(timeout)
        return mons

    def client_for(xid, path=None, timeout=None, **kw):
        seen["client"].append(timeout)
        return {"name": "app", "tags": 1, "monitor_number": 0,
                "geometry": {"current": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "states": {"is_floating": False, "is_fullscreen": False}}

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", mons_for)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                    proc_root=str(tmp_path), hostname="localhost",
                    sock_path="/x", timeout=0.25, logger=logger)
    assert seen["monitors"] == [0.25]
    assert seen["client"] == [0.25]


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
        build_record(0xABC, (1, 2, "c", None), client, None, None)


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


def test_capture_associates_output_by_client_monitor_not_stale_mnum(tmp_path, monkeypatch, logger):
    # WARNING 5: a window enumerated under mnum=0 (DP-1) whose get_dwm_client
    # reports monitor_number=1 (it moved between round-trips) must associate to
    # DP-1's neighbour DP-2 -- the client's OWN monitor -- never the stale mnum.
    _make_proc(tmp_path, 1234)
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA"),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(1920, 0), edid="edidB"),
    }
    mons = [
        _mon(0, 0, 0, 1920, 1080, clients=[0x11]),
        _mon(1, 1920, 0, 1920, 1080, clients=[]),
    ]

    def client_for(xid, path=None, **kw):
        # enumerated under monitor 0, but the client now lives on monitor 1.
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
    # output/edid follow the client's OWN monitor_number (1 -> DP-2), not mnum 0.
    assert recs[0].monitor_number == 1
    assert recs[0].output == "DP-2" and recs[0].edid == "edidB"


def test_capture_client_monitor_not_in_mapping_leaves_output_none(tmp_path, monkeypatch, logger, caplog):
    # WARNING 5: when the client's monitor_number isn't a known mapping key,
    # leave output/edid None + log rather than mis-associate to the stale mnum.
    _make_proc(tmp_path, 1234)
    outs = {"DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA")}
    mons = [_mon(0, 0, 0, 1920, 1080, clients=[0x11])]

    def client_for(xid, path=None, **kw):
        return {"name": "app", "tags": 1, "monitor_number": 7,  # unknown monitor
                "geometry": {"current": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "states": {"is_floating": False, "is_fullscreen": False}}

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    with caplog.at_level(logging.INFO, logger="xrandrw"):
        recs = capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    assert len(recs) == 1
    assert recs[0].monitor_number == 7
    assert recs[0].output is None and recs[0].edid is None
    assert any(getattr(r, "event", None) == "window_monitor_unmapped"
               for r in caplog.records)


def test_capture_null_mapping_value_emits_an_event(tmp_path, monkeypatch, logger, caplog):
    # 14-08: the `unmapped` branch above guards a MISSING key. A key that is PRESENT
    # but holds a NULL value -- the exact shape of all three poisoned records in the
    # live 04:21:43,109 capture -- used to be committed with output=None logging
    # NOTHING at all, which is why the failure had to be reconstructed forensically.
    _make_proc(tmp_path, 1234)
    # Two CONNECTED outputs at identical geometry: a genuine mirror, unresolvable
    # even after the tie-break, so the mapping holds {0: None}.
    outs = {
        "DP-1": _out("DP-1", mode=(1920, 1080), position=(0, 0), edid="edidA"),
        "DP-2": _out("DP-2", mode=(1920, 1080), position=(0, 0), edid="edidB"),
    }
    mons = [_mon(0, 0, 0, 1920, 1080, clients=[0x11])]

    def client_for(xid, path=None, **kw):
        return {"name": "app", "tags": 1, "monitor_number": 0,
                "geometry": {"current": {"x": 0, "y": 0, "width": 10, "height": 10}},
                "states": {"is_floating": False, "is_fullscreen": False}}

    monkeypatch.setattr(win_mod.dwmipc, "get_monitors", lambda path=None, **kw: mons)
    monkeypatch.setattr(win_mod.dwmipc, "get_dwm_client", client_for)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)

    with caplog.at_level(logging.INFO, logger="xrandrw"):
        recs = capture_windows(reader=_fake_reader(outs), xreader=_fake_xreader(1234),
                               proc_root=str(tmp_path), hostname="localhost",
                               sock_path="/x", logger=logger)
    assert len(recs) == 1 and recs[0].output is None
    assert any(getattr(r, "event", None) == "window_monitor_null_mapping"
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
