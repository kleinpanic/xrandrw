"""Precision of the tagmon crash-safety guard (Phase 14, relocate-guard-precision).

The Phase-10 guard refused ANY tagmon that would EMPTY the source monitor --
sufficient to avoid the dwm single-window-center SIGSEGV, but not NECESSARY. The
real crash needs BOTH (a) the source emptied (``selmon->sel`` -> NULL) AND (b)
some monitor at exactly ``n == 1`` during the post-move ``arrange`` (the buggy
``if (n == 1 && selmon->sel->CenterThisWindow)`` derefs the NULL). Blocking the
sufficient-only condition wrongly refused the last-window-on-external restore
where the destination absorbs the window and NO monitor lands at ``n == 1``.

These tests pin the REFINED guard :meth:`RelocationCoordinator._tagmon_would_crash_dwm`
to the EXACT post-move condition. The decision layer is exercised directly with a
MONKEYPATCHED ``get_monitors`` (fabricated per-monitor client lists -- no live X,
no real dwm-ipc socket; the ``block_live_dwm`` autouse guard stays in force), and
one end-to-end pass over the REAL stateful ``FakeDwmServer`` proves the wiring in
``_tagmon_to_target`` now ISSUES the previously-regressed last-window tagmon.
"""
from __future__ import annotations

import logging
import time
from collections import namedtuple
from types import SimpleNamespace

import pytest

import xrandrw.relocate as relocate
import xrandrw.windows as win_mod
from xrandrw import dwmipc
from xrandrw.xrandr import Output
from dwmipc_fake_server import FakeDwmServer


XID = 0x1400001


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


def _coord():
    # sock_path is never dialed: every test monkeypatches get_monitors, so no real
    # socket is opened. Defaults for control/reader/xreader are inert here.
    return relocate.RelocationCoordinator(sock_path="/nonexistent/dwm.sock")


# dwm's 9-tag "viewing everything" mask.
ALL_TAGS = 0x1FF

# A client as the fixtures describe it. B-1: `clients.all` alone is NOT enough to
# decide either half of the crash predicate -- dwm's focus(NULL) needs
# ISVISIBLE (tags & the monitor's tagset) and tile()'s `n` needs
# nexttiled (visible AND not floating) -- so every fixture client carries its
# tags and floating state, and _patch_monitors serves them over get_dwm_client
# exactly as dwm would.
Cl = namedtuple("Cl", "xid tags floating")


def _c(xid, tags=ALL_TAGS, floating=False):
    """A VISIBLE, TILED client by default -- the shape the old fixtures implied."""
    return Cl(xid, tags, floating)


def _mon(num, clients, tagset=ALL_TAGS):
    """One get_monitors monitor dict. ``clients`` may be bare xids or :func:`_c`."""
    cls = [c if isinstance(c, Cl) else _c(c) for c in clients]
    return {
        "num": num,
        "monitor_geometry": {"x": num * 1920, "y": 0, "width": 1920, "height": 1080},
        "layout": {"symbol": "[]="},
        "is_selected": False,
        # dwm dumps mon->tagset[mon->seltags] here.
        "tagset": {"current": tagset, "old": tagset},
        "clients": {"all": [c.xid for c in cls], "selected": None},
        "_clients": cls,          # fixture-only sidecar, stripped from the reply
    }


def _patch_monitors(monkeypatch, monitors):
    """Serve these monitors over get_monitors AND their clients over get_dwm_client."""
    by_xid = {c.xid: c for m in monitors for c in m.get("_clients", [])}

    def _get_monitors(path=None, **kw):
        return [{k: v for k, v in m.items() if k != "_clients"} for m in monitors]

    def _get_dwm_client(win, path=None, **kw):
        c = by_xid.get(int(win))
        if c is None:
            raise dwmipc.DwmIpcUnavailable(f"no such window {win}")
        return {"name": f"w{c.xid}", "tags": c.tags, "monitor_number": 0,
                "geometry": {"current": {"x": 0, "y": 0, "width": 9, "height": 9}},
                "states": {"is_floating": c.floating, "is_fullscreen": False}}

    monkeypatch.setattr(relocate.dwmipc, "get_monitors", _get_monitors)
    monkeypatch.setattr(relocate.dwmipc, "get_dwm_client", _get_dwm_client)


# ---------------------------------------------------------------------------
# The four required cases, asserting the code's ACTUAL computed decision.
# The guard returns True == UNSAFE (block), False == safe (allow). XID sits on
# the SOURCE monitor (num 0); direction +1 -> dirtomon = monitor 1 (dest).
# ---------------------------------------------------------------------------

def test_case1_source1_dest0_blocks(monkeypatch, logger):
    # Required case 1: source has 1 (only XID), dest has 0 -> single-window-total
    # move (source->0, dest->1). BLOCK (still crash-safe).
    _patch_monitors(monkeypatch, [_mon(0, [XID]), _mon(1, [])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_case2_source1_dest2_allows(monkeypatch, logger):
    # Required case 2 (the exact live-test regression): source has 1, dest has 2.
    # Post-move source->0, dest->3, no monitor at 1 -> ALLOW.
    _patch_monitors(monkeypatch, [_mon(0, [XID]), _mon(1, [0x2001, 0x2002])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is False


def test_case3_source2_never_emptied_allows(monkeypatch, logger):
    # Required case 3: source has 2 (XID + a co-resident) -> source post = 1,
    # never emptied -> ALLOW regardless of any monitor landing at 1.
    _patch_monitors(monkeypatch, [_mon(0, [XID, 0x2009]), _mon(1, [])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is False


def test_case4_source1_dest0_third_monitor_at_1_blocks(monkeypatch, logger):
    # Required case 4: source has 1, dest has 0, a THIRD monitor has 1 client.
    # Post-move source->0, dest->1, third stays at 1 -> BLOCK (a monitor at n==1
    # while the source is emptied is the exact crash).
    _patch_monitors(monkeypatch, [_mon(0, [XID]), _mon(1, []), _mon(2, [0x2003])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_case5_source1_dest1_allows(monkeypatch, logger):
    # Bonus (debug-file case 5): source has 1, dest has 1. Post-move source->0,
    # dest->2, no monitor at 1 -> ALLOW. Asserts the exact computed decision.
    _patch_monitors(monkeypatch, [_mon(0, [XID]), _mon(1, [0x2004])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is False


def test_backward_direction_wraps_to_last_monitor(monkeypatch, logger):
    # dirtomon(source, -1) wraps to the last monitor. Source (num 0) has 1, the
    # wrap destination (num 2) has 2 -> post source->0, dest->3, monitor 1 holds 1
    # -> BLOCK: some monitor is at n==1 while the source is emptied.
    _patch_monitors(monkeypatch,
                    [_mon(0, [XID]), _mon(1, [0x2005]), _mon(2, [0x2006, 0x2007])])
    assert _coord()._tagmon_would_crash_dwm(XID, -1, logger) is True


# ---------------------------------------------------------------------------
# B-1: TAG- AND FLOAT-AWARENESS.
#
# Every case above passes IDENTICALLY before and after B-1, because they use
# visible, tiled clients -- for which `len(clients.all)` happens to equal both
# dwm's focus(NULL) visible count AND tile()'s nexttiled `n`. The cases below are
# where those three numbers DIVERGE, and they are the cases the old guard got
# WRONG. Two of them are FALSE NEGATIVES: the old guard said "safe" and let
# through a move that SIGSEGVs dwm.
#
# dwm reference: focus(NULL) walks selmon->stack for an ISVISIBLE client
# (`c->tags & c->mon->tagset[c->mon->seltags]`) -- floating clients ARE visible.
# tile() computes n via nexttiled, which skips `c->isfloating || !ISVISIBLE(c)`.
# ---------------------------------------------------------------------------

OFF_TAG = 0x004        # a tag the monitor below is NOT viewing
VIEWING = 0x001        # the monitor's current tagset


def test_offtag_clients_do_not_keep_the_source_alive(monkeypatch, logger):
    """FALSE NEGATIVE #1 (source_emptied half): off-tag clients are NOT focusable.

    The source holds XID plus TWO clients on a tag the monitor is not viewing.
    The old guard read `len(clients.all) == 3`, computed post == 2, concluded
    "source not emptied" and ALLOWED the move. But dwm's focus(NULL) skips both
    invisible clients, so `selmon->sel` still goes NULL -- and the destination
    lands at exactly one tiled client. That is the crash, permitted.
    """
    mons = [_mon(0, [_c(XID, tags=VIEWING),
                     _c(0x3001, tags=OFF_TAG), _c(0x3002, tags=OFF_TAG)],
                 tagset=VIEWING),
            _mon(1, [], tagset=VIEWING)]
    _patch_monitors(monkeypatch, mons)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_floating_coresident_does_not_hide_a_tiled_singleton(monkeypatch, logger):
    """FALSE NEGATIVE #2 (any_singleton half): a floating client is not tiled.

    Monitor 2 holds one TILED and one FLOATING client. The old guard counted 2
    and saw no singleton, so with the source emptied it ALLOWED the move. dwm's
    tile() skips the floating one, so monitor 2's `n` is 1 -- and the buggy
    `if (n == 1 && selmon->sel->CenterThisWindow)` dereferences the NULL
    selmon->sel during the very same arrange(NULL). That is the crash, permitted.
    """
    mons = [_mon(0, [XID]),
            _mon(1, [0x2001, 0x2002]),                    # dest absorbs -> 3 tiled
            _mon(2, [_c(0x2003), _c(0x2004, floating=True)])]
    _patch_monitors(monkeypatch, mons)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_lone_floating_window_to_empty_monitor_is_allowed(monkeypatch, logger):
    """A FLOATING client can never create a tiled singleton -> genuinely safe.

    Source holds only XID (floating); the destination is empty. Post-move the
    source has 0 clients and the destination has one FLOATING client, so
    tile() sees n == 0 on BOTH -- the `n == 1` branch is never taken and
    selmon->sel is never dereferenced. The old all-clients guard counted the
    destination as 1 and refused this move; it is safe and must be allowed.
    """
    _patch_monitors(monkeypatch,
                    [_mon(0, [_c(XID, floating=True)]), _mon(1, [])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is False


def test_lone_tiled_window_to_empty_monitor_still_blocks(monkeypatch, logger):
    """The float-aware twin of the case above: TILED lands at n == 1 -> BLOCK."""
    _patch_monitors(monkeypatch,
                    [_mon(0, [_c(XID, floating=False)]), _mon(1, [])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_offtag_client_on_destination_does_not_count_toward_its_n(monkeypatch, logger):
    """The destination's OFF-TAG resident cannot save it from being a singleton.

    Dest holds one client on a tag it is not viewing. Old guard: dest post == 2,
    no singleton -> ALLOW. dwm: that client is invisible, so after the move dest
    has exactly ONE tiled visible client while the source is emptied -> crash.
    """
    mons = [_mon(0, [_c(XID, tags=VIEWING)], tagset=VIEWING),
            _mon(1, [_c(0x4001, tags=OFF_TAG)], tagset=VIEWING)]
    _patch_monitors(monkeypatch, mons)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_moved_client_takes_the_destination_tagset(monkeypatch, logger):
    """sendmon sets ``c->tags = m->tagset[m->seltags]`` -- the move is always visible.

    XID sits on an OFF_TAG that the DESTINATION is not viewing either. It still
    becomes visible (and tiled) on arrival, so the destination lands at n == 1
    while the source empties -> BLOCK. Modelling the moved client as keeping its
    old tags would wrongly compute dest n == 0 and allow the crash.
    """
    mons = [_mon(0, [_c(XID, tags=OFF_TAG)], tagset=OFF_TAG),
            _mon(1, [], tagset=VIEWING)]
    _patch_monitors(monkeypatch, mons)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


# ---------------------------------------------------------------------------
# FAIL-SAFE: any IPC failure / unresolved window / malformed reply / single
# monitor / missing tagset / over-budget client count -> refuse the move
# (return True). Never weaken to allow-by-default.
# ---------------------------------------------------------------------------

def test_missing_tagset_fails_safe_blocks(monkeypatch, logger):
    # Without mon->tagset there is no way to know which clients are ISVISIBLE,
    # so neither half of the predicate is computable -> refuse.
    mons = [_mon(0, [XID]), _mon(1, [0x2001, 0x2002])]
    del mons[1]["tagset"]
    _patch_monitors(monkeypatch, mons)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_unresolvable_client_fails_safe_blocks(monkeypatch, logger):
    # A client listed on a monitor whose get_dwm_client raises: its visibility
    # and floating state are unknown, so the counts cannot be trusted -> refuse.
    # (The whole point of B-1 is that a guard which guesses errs UNSAFE.)
    mons = [_mon(0, [XID]), _mon(1, [0x2001])]
    mons[1]["clients"]["all"].append(0x9999)      # listed, but unresolvable
    _patch_monitors(monkeypatch, mons)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_over_client_budget_fails_safe_blocks(monkeypatch, logger):
    # More clients than _GUARD_CLIENT_BUDGET: resolving them all would put an
    # unbounded number of IPC round-trips on the single-threaded watch loop, so
    # the guard refuses rather than either stalling or guessing.
    many = [_c(0x5000 + i) for i in range(relocate._GUARD_CLIENT_BUDGET + 1)]
    _patch_monitors(monkeypatch, [_mon(0, [XID]), _mon(1, many)])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True



def test_ipc_unavailable_fails_safe_blocks(monkeypatch, logger):
    def boom(path=None, **kw):
        raise dwmipc.DwmIpcUnavailable("no socket")
    monkeypatch.setattr(relocate.dwmipc, "get_monitors", boom)
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_window_not_found_fails_safe_blocks(monkeypatch, logger):
    # XID is on no monitor -> source unresolved -> block.
    _patch_monitors(monkeypatch, [_mon(0, [0x9998]), _mon(1, [0x9999])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_single_monitor_fails_safe_blocks(monkeypatch, logger):
    # Fewer than two monitors -> no destination -> block.
    _patch_monitors(monkeypatch, [_mon(0, [XID])])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


def test_malformed_monitor_reply_fails_safe_blocks(monkeypatch, logger):
    # A monitor with a non-list clients.all -> malformed -> block.
    bad = {"num": 1, "monitor_geometry": {"x": 1920, "y": 0}, "clients": {"all": None}}
    _patch_monitors(monkeypatch, [_mon(0, [XID]), bad])
    assert _coord()._tagmon_would_crash_dwm(XID, +1, logger) is True


# ---------------------------------------------------------------------------
# END-TO-END wiring over the REAL stateful FakeDwmServer: the exact regressed
# live case (all windows on the external, restoring the LAST one whose source
# monitor then empties but whose destination already holds 2 clients) now ISSUES
# the tagmon instead of skipping it, and never logs relocate_tagmon_unsafe.
# ---------------------------------------------------------------------------

PID_A, ST_A = 1001, 5000
GA = {"x": 1930, "y": 40, "width": 300, "height": 400}
ANCHOR1 = 0x14000A1     # two co-residents already restored to the destination
ANCHOR2 = 0x14000A2


def _make_proc(tmp_path, procs):
    root = tmp_path / "proc"
    for pid, comm, starttime in procs:
        d = root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        after = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime)] + ["0", "0"]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after) + "\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(comm.encode() + b"\x00")
    return str(root)


def _make_outputs():
    return {
        "DP-1": Output(name="DP-1", connected=True, current_mode=(1920, 1080),
                       position=(0, 0), edid_sha1="edA"),
        "DP-2": Output(name="DP-2", connected=True, current_mode=(1920, 1080),
                       position=(1920, 0), edid_sha1="edB"),
    }


class _FakeRandr:
    def __init__(self, outs):
        self.outs = outs

    def read(self, logger=None):
        return dict(self.outs)


def _fake_xreader(pid_for):
    return SimpleNamespace(
        net_wm_pid=lambda xid: pid_for.get(xid),
        client_machine=lambda xid: None,
        xres_pid=lambda xid: None,
        has_xres=lambda: True,
    )


class _MockControl:
    def __init__(self, srv, events):
        self.srv = srv
        self.events = events

    def focus(self, xid):
        self.srv.select(xid)
        self.events.append(("focus", xid))
        return True

    def configure_geometry(self, xid, geom):
        self.srv.set_geometry(xid, geom)
        self.events.append(("configure", xid))
        return True


def _client(xid, tags, mon, geom, floating):
    return {"xid": xid, "name": f"w{xid}", "tags": tags, "monitor_number": mon,
            "geometry": {"current": dict(geom)},
            "states": {"is_floating": floating, "is_fullscreen": False}}


def _wait_for(pred, deadline=2.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_last_window_on_external_now_restores_when_dest_absorbs(tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    monkeypatch.setattr(relocate.dwmipc, "available", lambda path=None, **kw: True)
    orig_run = dwmipc.run_command

    def run_spy(name, *args, **kw):
        events.append(("cmd", name, args))
        return orig_run(name, *args, **kw)
    monkeypatch.setattr(relocate.dwmipc, "run_command", run_spy)

    sock = tmp_path / "dwm.sock"
    # A on the external (monitor 1) alongside TWO co-residents. On unplug A is
    # evacuated to monitor 0 ALONE; the two anchors stay on monitor 1. Restoring A
    # to monitor 1 empties monitor 0 (source->0) BUT monitor 1 lands at 3 -> no
    # monitor at n==1 -> the refined guard ALLOWS the move (pre-fix it blocked).
    clients = [_client(A := XID, 4, 1, GA, True),
               _client(ANCHOR1, 1, 1, {"x": 1, "y": 2, "width": 9, "height": 9}, False),
               _client(ANCHOR2, 1, 1, {"x": 3, "y": 4, "width": 9, "height": 9}, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = _MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=_FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)                 # boot seed
        srv.select(A)
        orig_run("tagmon", 1, path=str(sock))        # dwm evacuates A: mon1 -> mon0
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 0)
        # A REAL unplug darkens the CRTC as well as dropping HPD -- that is what
        # apply_once's scrub_stale does to a disconnected head, and since 14-08 the
        # coordinator's presence predicate is CRTC liveness, not HPD `connected`.
        outs["DP-2"].connected = False
        outs["DP-2"].position = None
        outs["DP-2"].current_mode = None
        coord.on_settled({}, logger)                 # record A displaced
        assert (PID_A, ST_A) in coord._displaced

        events.clear()
        outs["DP-2"].connected = True
        outs["DP-2"].position = (1920, 0)
        outs["DP-2"].current_mode = (1920, 1080)
        with caplog.at_level(logging.WARNING, logger="xrandrw"):
            coord.on_settled({}, logger)             # restore A

        # A moved back to monitor 1 -- the tagmon was ISSUED, not skipped.
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert any(e[0] == "cmd" and e[1] == "tagmon" for e in events)
        # The refined guard did NOT log the unsafe-skip for this safe move.
        assert not any(getattr(r, "event", None) == "relocate_tagmon_unsafe"
                       for r in caplog.records)
        assert (PID_A, ST_A) not in coord._displaced
