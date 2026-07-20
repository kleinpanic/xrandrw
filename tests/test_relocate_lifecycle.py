"""Full unplug->record->replug->restore lifecycle over the stateful fake server.

Wires a REAL AF_UNIX stateful :class:`FakeDwmServer` + a MOCKED
:class:`RelocationControl` whose ``focus`` bridges to the server's ``select`` and
whose ``configure_geometry`` bridges to ``set_geometry`` + a fake ``RandRReader``
+ a fake ``WindowXReader`` + a tmp_path fake ``/proc``. Drives the coordinator
headless (no live X, no real dwm) and asserts the record/restore contract,
the tiled-vs-floating guarantee, reused/dead-PID safety, unavailable no-op,
one-window-failure isolation, bounded tagmon giveup, and the boot-seed baseline.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

import xrandrw.relocate as relocate
import xrandrw.windows as win_mod
from xrandrw import dwmipc
from xrandrw.xrandr import Output
from dwmipc_fake_server import FakeDwmServer


A = 0x1400001      # floating window
B = 0x1400002      # tiled (or second) window
# Co-resident anchor on the SURVIVING monitor (monitor 0). It is NOT in any
# xreader map, so capture never resolves/records it -- it exists only in the fake
# server's client list to keep the source monitor non-empty. This lets the
# coordinator's crash-safety gate (_tagmon_would_crash_dwm: refuses a tagmon that
# would EMPTY the source AND leave a monitor at n==1, SIGSEGVing single-window-
# center dwm builds) permit a lone displaced window's restore hop -- the source
# keeps 2 clients so it is never emptied -- mirroring a real evacuation, where the
# surviving monitor holds the user's other windows.
ANCHOR = 0x1400009
PID_A, ST_A = 1001, 5000
PID_B, ST_B = 1002, 6000
GA = {"x": 1930, "y": 40, "width": 300, "height": 400}
GB = {"x": 5, "y": 6, "width": 100, "height": 200}


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


def _wait_for(pred, deadline=2.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


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


def _rewrite_starttime(tmp_path, pid, comm, starttime):
    d = tmp_path / "proc" / str(pid)
    after = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime)] + ["0", "0"]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after) + "\n")


# W1 geometry alignment: positions/modes MUST line up with the fake server's
# per-monitor origins (num*1920, 1920x1080) or match_dwm_monitor_to_output
# resolves to None and the tests could pass VACUOUSLY.
_LIVE_GEOMETRY = {"DP-1": ((0, 0), (1920, 1080)),
                  "DP-2": ((1920, 0), (1920, 1080)),
                  "DP-3": ((3840, 0), (1920, 1080))}
_EDIDS = {"DP-1": "edA", "DP-2": "edB", "DP-3": "edC"}
# Default head set. DP-3 exists in the tables above only for the UX-03 3-monitor
# test; every other test stays dual-head, so it must be opted into explicitly.
_DUAL = ("DP-1", "DP-2")
_TRIPLE = ("DP-1", "DP-2", "DP-3")


def _make_outputs(names=_DUAL):
    return {
        name: Output(name=name, connected=True, position=_LIVE_GEOMETRY[name][0],
                     current_mode=_LIVE_GEOMETRY[name][1], edid_sha1=_EDIDS[name])
        for name in names
    }


def _unplug(outs, name):
    """Model a REAL unplug: HPD drops AND the apply's scrub darkens the CRTC.

    Since 14-08 the coordinator's presence predicate is CRTC LIVENESS, not HPD
    ``connected`` -- on the live replug-bounce trace HPD had already returned by
    the settle while the CRTC was still dark, so no HPD edge existed to act on. A
    fixture that clears ``connected`` but leaves ``current_mode`` set describes a
    head that is STILL driving pixels, which dwm has not evacuated; that is not an
    unplug. Darkening the CRTC is exactly what ``apply_once``'s ``scrub_stale``
    does to a disconnected head, so this is the faithful post-apply state.
    """
    o = outs[name]
    o.connected = False
    o.position = None
    o.current_mode = None


def _replug(outs, name):
    o = outs[name]
    o.connected = True
    o.position, o.current_mode = _LIVE_GEOMETRY[name]


class FakeRandr:
    def __init__(self, outs):
        self.outs = outs

    def read(self, logger=None):
        return {k: v for k, v in self.outs.items()}


def _fake_xreader(pid_for):
    return SimpleNamespace(
        net_wm_pid=lambda xid: pid_for.get(xid),
        client_machine=lambda xid: None,   # None -> resolve_pid skips the non-local check
        xres_pid=lambda xid: None,
        has_xres=lambda: True,
    )


class MockControl:
    """Focus bridges X-focus -> dwm selection; configure bridges to set_geometry."""

    def __init__(self, srv, events):
        self.srv = srv
        self.events = events
        self.configured = []

    def focus(self, xid):
        self.srv.select(xid)
        self.events.append(("focus", xid))
        return True

    def configure_geometry(self, xid, geom):
        self.srv.set_geometry(xid, geom)
        self.configured.append((xid, dict(geom)))
        self.events.append(("configure", xid))
        return True


def _client(xid, tags, mon, geom, floating):
    return {"xid": xid, "name": f"w{xid}", "tags": tags, "monitor_number": mon,
            "geometry": {"current": dict(geom)},
            "states": {"is_floating": floating, "is_fullscreen": False}}


def _install_common(monkeypatch, events):
    """Patch read_edids off, available True, and a run_command spy into events."""
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    monkeypatch.setattr(relocate.dwmipc, "available", lambda path=None, **kw: True)
    orig_run = dwmipc.run_command

    def run_spy(name, *args, **kw):
        events.append(("cmd", name, args))
        return orig_run(name, *args, **kw)
    monkeypatch.setattr(relocate.dwmipc, "run_command", run_spy)
    return orig_run


def _evacuate(orig_run, srv, sock, xid):
    """Simulate dwm auto-evacuating a client off the removed monitor (1 -> 0)."""
    srv.select(xid)
    orig_run("tagmon", 1, path=str(sock))
    assert _wait_for(lambda: srv.state(xid)["monitor_number"] == 0)


# ---------------------------------------------------------------------------
# Main: single boot seed -> FIRST unplug records -> FIRST replug restores (B2)
# ---------------------------------------------------------------------------

def test_full_lifecycle_records_and_restores_floating(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A), (PID_B, "b", ST_B)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    clients = [_client(A, 4, 1, GA, True), _client(B, 2, 0, GB, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A, B: PID_B}),
            sock_path=str(sock), proc_root=proc_root)

        # 1. BOOT SEED (all connected): seeds _prev_present + _snapshot only.
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._snapshot
        assert coord._snapshot[(PID_A, ST_A)].output == "DP-2"
        assert not coord._displaced

        # 2. FIRST unplug DP-2: dwm evacuates A to monitor 0; A recorded displaced.
        _evacuate(orig_run, srv, sock, A)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # 3. FIRST replug DP-2: A restored to monitor 1, tags 4, floating, geometry.
        _replug(outs, "DP-2")
        tagmon_before = sum(1 for e in events if e[0] == "cmd" and e[1] == "tagmon")
        coord.on_settled({}, logger)

        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        st = srv.state(A)
        assert st["tags"] == 4
        assert st["is_floating"] is True
        # Geometry is restored MONITOR-RELATIVE: dwm's configurerequest is
        # c->x = c->mon->mx + ev->x, so the coordinator subtracts the TARGET
        # monitor origin (DP-2 == dwm monitor 1 @ (1920,0)) from the captured
        # ABSOLUTE geometry (GA.x=1930) before ConfigureWindow. ev->x=10 makes dwm
        # recompute c->x = 1920+10 = 1930 (the saved absolute pos). Pre-fix it sent
        # absolute 1930 and dwm double-shifted it to 1920+1930 -> off-screen/centered.
        assert (A, {"x": GA["x"] - 1920, "y": GA["y"] - 0,
                    "width": GA["width"], "height": GA["height"]}) in control.configured
        # W1 non-vacuous: a real tagmon verb was issued during restore.
        tagmon_after = sum(1 for e in events if e[0] == "cmd" and e[1] == "tagmon")
        assert tagmon_after > tagmon_before
        assert (PID_A, ST_A) not in coord._displaced

        # focus-then-act: every dwm verb (cmd) is immediately preceded by a focus.
        for i, e in enumerate(events):
            if e[0] == "cmd":
                assert events[i - 1][0] == "focus"


# ---------------------------------------------------------------------------
# Tiled window: restored monitor+tag, NO geometry write, never converted
# ---------------------------------------------------------------------------

def test_tiled_window_no_geometry_and_never_converted(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_B, "b", ST_B)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    clients = [_client(B, 2, 1, GB, False),          # tiled on DP-2 (monitor 1)
               _client(ANCHOR, 1, 0, GB, False)]      # anchor keeps monitor 0 non-empty
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({B: PID_B}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, B)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        _replug(outs, "DP-2")
        coord.on_settled({}, logger)

        assert _wait_for(lambda: srv.state(B)["monitor_number"] == 1)
        assert srv.state(B)["tags"] == 2
        assert srv.state(B)["is_floating"] is False           # never converted
        assert control.configured == []                       # NO geometry write
        assert not any(e[0] == "cmd" and e[1] == "togglefloating" for e in events)


# ---------------------------------------------------------------------------
# Identity: a reused/dead PID (starttime moved) is left untouched and dropped
# ---------------------------------------------------------------------------

def test_identity_mismatch_left_untouched(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    clients = [_client(A, 4, 1, GA, True)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # PID reused: starttime moved -> identity no longer matches the record.
        _rewrite_starttime(tmp_path, PID_A, "a", ST_A + 999)
        events.clear()
        _replug(outs, "DP-2")
        coord.on_settled({}, logger)

        # Left where dwm put it (monitor 0), dropped, and NO control call issued.
        assert srv.state(A)["monitor_number"] == 0
        assert (PID_A, ST_A) not in coord._displaced
        assert not any(e[0] == "focus" for e in events)
        assert not any(e[0] == "cmd" for e in events)


# ---------------------------------------------------------------------------
# Gate: dwmipc unavailable -> on_settled is a complete no-op (no seed/restore)
# ---------------------------------------------------------------------------

def test_gate_unavailable_is_complete_noop(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    monkeypatch.setattr(relocate.dwmipc, "available", lambda path=None, **kw: False)
    sock = tmp_path / "dwm.sock"
    clients = [_client(A, 4, 1, GA, True)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = relocate.RelocationCoordinator(
            control=MockControl(srv, []), reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)
        coord.on_settled({}, logger)
        assert coord._prev_present is None
        assert coord._snapshot == {}
        assert coord._displaced == {}


# ---------------------------------------------------------------------------
# Isolation: one window's get_dwm_client failure never blocks the other
# ---------------------------------------------------------------------------

def test_one_window_failure_isolated(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A), (PID_B, "b", ST_B)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    # Both floating on DP-2 (monitor 1) so both get displaced by the DP-2 unplug.
    clients = [_client(A, 4, 1, GA, True), _client(B, 8, 1, GB, True)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A, B: PID_B}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _evacuate(orig_run, srv, sock, B)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced and (PID_B, ST_B) in coord._displaced

        # B's live client read raises for one window only (per-window
        # DwmIpcUnavailable) -- and B is gone from dwm's client list too, which is
        # what an unreadable window actually means. B-1: the crash guard now
        # resolves EVERY listed client to learn its tags/floating state, so a
        # window that is both listed AND unresolvable makes the guard fail safe
        # for the whole monitor (covered by
        # test_unresolvable_client_fails_safe_blocks). Removing B from the server
        # keeps this test on its actual subject: one window's failure must not
        # stop the OTHER window from being restored.
        srv.remove(B)
        orig_get = dwmipc.get_dwm_client

        def get_spy(win, path=None, **kw):
            if int(win) == B:
                raise dwmipc.DwmIpcUnavailable("bad window")
            return orig_get(win, path=path, **kw)
        monkeypatch.setattr(relocate.dwmipc, "get_dwm_client", get_spy)

        _replug(outs, "DP-2")
        coord.on_settled({}, logger)   # must not raise

        assert (PID_A, ST_A) not in coord._displaced   # A restored + dropped
        assert (PID_B, ST_B) in coord._displaced        # B skipped, still displaced


# ---------------------------------------------------------------------------
# Bounded tagmon: never-reached target logs giveup and leaves the window as-is
# ---------------------------------------------------------------------------

def test_tagmon_giveup_when_target_never_reached(tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    clients = [_client(A, 4, 1, GA, True),
               _client(ANCHOR, 1, 0, GB, False)]      # anchor keeps monitor 0 non-empty
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)   # A now on monitor 0 (alongside the anchor)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)

        # Make tagmon a no-op so A can never reach its target monitor -> giveup.
        real_run = dwmipc.run_command

        def run_noop_tagmon(name, *args, **kw):
            events.append(("cmd", name, args))
            if name == "tagmon":
                return {"result": "success"}   # no movement
            return real_run(name, *args, **kw)
        monkeypatch.setattr(relocate.dwmipc, "run_command", run_noop_tagmon)

        _replug(outs, "DP-2")
        with caplog.at_level(logging.WARNING, logger="xrandrw"):
            coord.on_settled({}, logger)

        assert any(getattr(r, "event", None) == "relocate_monitor_giveup" for r in caplog.records)
        # Bounded: exactly n_monitors (2) tagmon attempts, then giveup.
        assert sum(1 for e in events if e[0] == "cmd" and e[1] == "tagmon") == 2


# ---------------------------------------------------------------------------
# CRASH-SAFETY (WM-08): a tagmon that would EMPTY the source monitor is SKIPPED,
# never issued. On dwm builds with the single-window-center layout patch such a
# move sets selmon->sel=NULL and the next arrange()->tile() dereferences it ->
# SIGSEGV (root cause: .planning/debug/resolved/tagmon-sigsegv-dwm.md, fault at
# tile+384 `cmpl $0x0,0x170(%rdi)`). Guarding OUR code keeps selmon->sel valid on
# ALL dwm builds. This is the unit mirror of the isolated real-dwm repro.
# ---------------------------------------------------------------------------

def test_tagmon_skipped_when_move_would_empty_source_monitor(tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    # ONLY the displaced window exists on the surviving monitor -> a restore
    # tagmon would empty it. NO anchor here on purpose: this is the crash case.
    #
    # B-1: the window must be TILED for this to BE the crash case. dwm's tile()
    # counts `n` via nexttiled, which skips floating clients -- so a lone FLOATING
    # window landing on an empty monitor gives n == 0 there and n == 0 on the
    # emptied source, the `if (n == 1 && selmon->sel->CenterThisWindow)` branch is
    # never taken, and nothing is dereferenced. That move is SAFE and the
    # float-aware guard now correctly allows it (see
    # test_lone_floating_window_to_empty_monitor_is_allowed). A TILED window
    # lands at n == 1 on the destination while selmon->sel is NULL: the SIGSEGV.
    clients = [_client(A, 4, 1, GA, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)   # A now the ONLY client on monitor 0
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        events.clear()
        _replug(outs, "DP-2")
        with caplog.at_level(logging.WARNING, logger="xrandrw"):
            coord.on_settled({}, logger)

        # The unsafe move is refused: NO tagmon command was ever issued...
        assert not any(e[0] == "cmd" and e[1] == "tagmon" for e in events)
        # ...the guard logged the skip...
        assert any(getattr(r, "event", None) == "relocate_tagmon_unsafe" for r in caplog.records)
        # ...and the window stayed on monitor 0 (left where dwm evacuated it).
        assert srv.state(A)["monitor_number"] == 0


# ---------------------------------------------------------------------------
# CR-01: the coordinator confirms dwm's SELECTED client caught up to the focus
# (polls get_monitors) BEFORE issuing a verb -- a lagged select must not let a
# verb land on the previously-selected client.
# ---------------------------------------------------------------------------

def test_focus_confirmed_before_verb_under_lagged_select(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A), (PID_B, "b", ST_B)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    # Record every get_monitors as a "monitors" event so we can prove the
    # confirm-poll happens BETWEEN focus and the first verb.
    orig_get_mons = dwmipc.get_monitors

    def mons_spy(*args, **kw):
        events.append(("monitors",))
        return orig_get_mons(*args, **kw)
    monkeypatch.setattr(relocate.dwmipc, "get_monitors", mons_spy)

    sock = tmp_path / "dwm.sock"
    # A floating on DP-2 (monitor 1); B tiled on DP-1 (monitor 0), never displaced.
    clients = [_client(A, 4, 1, GA, True), _client(B, 1, 0, GB, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A, B: PID_B}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)          # A now on monitor 0
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # dwm's selected client is now the WRONG window (B), and selection LAGS by
        # one poll: a verb issued without confirming would act on B, not A.
        srv.select(B)
        srv.set_select_lag(1)
        events.clear()

        _replug(outs, "DP-2")
        coord.on_settled({}, logger)

        # Restore landed on A (confirmed target), not the stale selection B.
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert srv.state(A)["tags"] == 4
        # B was never the verb target: it stayed put and unmodified.
        assert srv.state(B)["monitor_number"] == 0
        assert srv.state(B)["tags"] == 1
        assert srv.state(B)["is_floating"] is False

        # A get_monitors selection-confirm read occurs between the focus and the
        # first verb -- proving the coordinator polls rather than firing blind.
        cmd_idx = next(i for i, e in enumerate(events) if e[0] == "cmd")
        focus_before = [i for i in range(cmd_idx) if events[i][0] == "focus"]
        assert focus_before, "a focus precedes the first verb"
        between = events[focus_before[-1] + 1:cmd_idx]
        assert any(e[0] == "monitors" for e in between), \
            "coordinator confirms selection (get_monitors) before issuing the verb"


# ---------------------------------------------------------------------------
# WR-01: a SIGTERM (stop_evt set) mid-cycle bails after the CURRENT window
# instead of restoring the whole batch (prompt shutdown, no watchdog masking).
# ---------------------------------------------------------------------------

def test_stop_evt_bails_restore_after_current_window(tmp_path, monkeypatch, logger):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A), (PID_B, "b", ST_B)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    # Two floating windows on DP-2 (monitor 1) -> both displaced by the unplug.
    clients = [_client(A, 4, 1, GA, True), _client(B, 8, 1, GB, True)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A, B: PID_B}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _evacuate(orig_run, srv, sock, B)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced and (PID_B, ST_B) in coord._displaced

        # Simulate SIGTERM arriving DURING the first window's restore: the moment
        # a window is restored, stop_evt is set, so the loop must stop before the
        # second window rather than draining the whole batch.
        stop_evt = threading.Event()
        restored = []
        orig_restore_one = coord._restore_one

        def counting_restore_one(rec, monitors, conn_to_mon, lg):
            restored.append((rec.pid, rec.starttime))
            stop_evt.set()
            return orig_restore_one(rec, monitors, conn_to_mon, lg)
        monkeypatch.setattr(coord, "_restore_one", counting_restore_one)

        _replug(outs, "DP-2")
        coord.on_settled({}, logger, stop_evt=stop_evt)

        # Exactly ONE window was restored; the other stays displaced for later.
        assert len(restored) == 1
        still_displaced = [k for k in ((PID_A, ST_A), (PID_B, ST_B)) if k in coord._displaced]
        assert len(still_displaced) == 1


# ---------------------------------------------------------------------------
# WR-02/AUDIT-A: a displaced record whose process has exited is swept on the
# next steady settle so _displaced cannot leak forever in a long-lived daemon.
# ---------------------------------------------------------------------------

def test_dead_displaced_record_evicted_on_steady_settle(tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    clients = [_client(A, 4, 1, GA, True)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _unplug(outs, "DP-2")           # DP-2 stays disconnected
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # The displaced window's process exits (its /proc entry disappears).
        shutil.rmtree(tmp_path / "proc" / str(PID_A))

        # A steady settle (no removal, no return) sweeps the dead record.
        with caplog.at_level(logging.INFO, logger="xrandrw"):
            coord.on_settled({}, logger)

        assert (PID_A, ST_A) not in coord._displaced
        assert any(getattr(r, "event", None) == "relocate_displaced_evict"
                   for r in caplog.records)


# ---------------------------------------------------------------------------
# UX-01 (focus theft): dwm's verbs act on the SELECTED client, so the coordinator
# focuses every window it touches. Before this fix the user's own focus was never
# captured and never given back -- with 3 windows over 2 bounce cycles that was
# 6+ focus yanks in a few seconds, ending wherever the last verb landed.
#
# The first attempt captured the selection at RESTORE-CYCLE ENTRY and live
# testing refuted it: when a monitor dies dwm evacuates its clients and FOCUSES
# one of them, which happens before the daemon even records the displacement. An
# out-of-band sampler caught the steal ~2 s after the unplug and ~9 s before the
# restore, so cycle-entry capture captured DWM'S choice and dutifully restored the
# wrong window. The focus must therefore come from the STEADY-STATE snapshot --
# the only sample taken before dwm gets a say.
#
# ``_evacuate`` models that faithfully: it selects the client it moves, exactly
# as dwm does. Tests below rely on that; a fake that silently left the selection
# alone could not reproduce the live failure and would pass vacuously.
# ---------------------------------------------------------------------------

USER = 0x140000A       # the user's own window, never displaced
PID_U, ST_U = 1003, 7000
USER2 = 0x140000B      # a second surviving window, for the snapshot-refresh test
PID_U2, ST_U2 = 1004, 7100


def _selection(sock):
    """dwm's selected client xid, read from the RAW get_monitors reply.

    Deliberately does NOT go through relocate._selected_focus: asserting the
    production reader against itself could pass vacuously.
    """
    for m in dwmipc.get_monitors(path=str(sock)):
        if m.get("is_selected"):
            return m["clients"]["selected"]
    return None


def _focus_fixture(tmp_path, monkeypatch, extra_clients=()):
    """Two displaced windows on DP-2 + the user's window(s) on DP-1, all seeded."""
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A), (PID_B, "b", ST_B),
                                      (PID_U, "u", ST_U), (PID_U2, "u2", ST_U2)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    clients = [_client(A, 4, 1, GA, True), _client(B, 8, 1, GB, True),
               _client(USER, 1, 0, GB, False), _client(USER2, 1, 0, GB, False),
               *extra_clients]
    return proc_root, events, orig_run, clients


def _focus_coord(srv, outs, events, proc_root, sock, control=None):
    return relocate.RelocationCoordinator(
        control=control if control is not None else MockControl(srv, events),
        reader=FakeRandr(outs),
        xreader=_fake_xreader({A: PID_A, B: PID_B, USER: PID_U, USER2: PID_U2}),
        sock_path=str(sock), proc_root=proc_root)


def test_steady_state_focus_survives_dwm_refocus_during_evacuation(
        tmp_path, monkeypatch, logger, caplog):
    """THE live failure: dwm steals focus on the unplug, before any restore runs.

    Reproduces the sampler trace exactly -- user focused on the surviving
    monitor, monitor dies, dwm evacuates its clients and focuses one of them,
    THEN the daemon restores. Capturing the focus at restore-cycle entry (the
    pre-fix behaviour) captures dwm's pick and hands the user the wrong window;
    only the steady-state snapshot holds the right answer.
    """
    proc_root, events, orig_run, clients = _focus_fixture(tmp_path, monkeypatch)
    sock = tmp_path / "dwm.sock"
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = _focus_coord(srv, outs, events, proc_root, sock)

        # STEADY STATE: the user is working in THEIR window, all heads up. This
        # is the only moment the user's real intent is observable.
        srv.select(USER)
        assert _selection(sock) == USER
        coord.on_settled({}, logger)

        # UNPLUG: dwm evacuates DP-2's clients and focuses one of them (B) --
        # this lands BEFORE the daemon records anything, exactly as live.
        _evacuate(orig_run, srv, sock, A)
        _evacuate(orig_run, srv, sock, B)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced and (PID_B, ST_B) in coord._displaced
        # NON-VACUOUS: the focus really is stolen before the restore cycle opens.
        # Without this the test could pass against cycle-entry capture.
        assert _selection(sock) == B
        assert _selection(sock) != USER

        _replug(outs, "DP-2")
        with caplog.at_level(logging.DEBUG, logger="xrandrw"):
            coord.on_settled({}, logger)

        # Non-vacuous: BOTH windows really were restored, so focus really was
        # stolen repeatedly during the cycle...
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert _wait_for(lambda: srv.state(B)["monitor_number"] == 1)
        assert sum(1 for e in events if e[0] == "focus") > 2
        # ...and the user's selection is back where THEY left it -- not where dwm
        # moved it on the evacuation.
        assert _selection(sock) == USER
        assert any(getattr(r, "event", None) == "relocate_focus_restored"
                   for r in caplog.records)
        # ONCE per cycle, not per window: the give-back is the LAST focus event.
        focus_xids = [e[1] for e in events if e[0] == "focus"]
        assert focus_xids[-1] == USER
        assert focus_xids.count(USER) == 1


def test_seed_captures_steady_state_focus(tmp_path, monkeypatch, logger):
    """The FIRST snapshot is the boot seed; without focus there, the first
    unplug of a session would have nothing to give back."""
    proc_root, events, _orig_run, clients = _focus_fixture(tmp_path, monkeypatch)
    sock = tmp_path / "dwm.sock"
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = _focus_coord(srv, outs, events, proc_root, sock)

        srv.select(USER)
        assert coord._snapshot_focus is None       # nothing captured pre-seed
        coord.on_settled({}, logger)               # boot seed
        assert coord._snapshot_focus == (0, USER)  # DP-1 is dwm monitor 0


def test_snapshot_focus_refreshes_on_each_steady_settle(
        tmp_path, monkeypatch, logger, caplog):
    """Refocusing during normal operation becomes the new known-good focus."""
    proc_root, events, orig_run, clients = _focus_fixture(tmp_path, monkeypatch)
    sock = tmp_path / "dwm.sock"
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = _focus_coord(srv, outs, events, proc_root, sock)

        srv.select(USER)
        coord.on_settled({}, logger)               # seed captures USER
        assert coord._snapshot_focus == (0, USER)

        # The user moves to another window; the next STEADY settle (no topology
        # change) must adopt it.
        srv.select(USER2)
        coord.on_settled({}, logger)
        assert coord._snapshot_focus == (0, USER2)

        # ...and a full cycle from here gives back USER2, not USER.
        _evacuate(orig_run, srv, sock, A)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert _selection(sock) == A               # dwm holds the evacuated window
        _replug(outs, "DP-2")
        with caplog.at_level(logging.DEBUG, logger="xrandrw"):
            coord.on_settled({}, logger)

        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert _selection(sock) == USER2


def test_focus_restore_graceful_when_focused_window_is_gone(
        tmp_path, monkeypatch, logger, caplog):
    proc_root, events, orig_run, clients = _focus_fixture(tmp_path, monkeypatch)
    sock = tmp_path / "dwm.sock"
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = _focus_coord(srv, outs, events, proc_root, sock)

        srv.select(USER)
        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _evacuate(orig_run, srv, sock, B)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)

        # The user CLOSES their focused window while the restore is in flight, so
        # the captured xid is no longer a live dwm client by give-back time.
        orig_restore_one = coord._restore_one

        def closing_restore_one(rec, monitors, conn_to_mon, lg):
            srv.remove(USER)
            return orig_restore_one(rec, monitors, conn_to_mon, lg)
        monkeypatch.setattr(coord, "_restore_one", closing_restore_one)

        _replug(outs, "DP-2")
        with caplog.at_level(logging.DEBUG, logger="xrandrw"):
            coord.on_settled({}, logger)      # must not raise

        # Degrades to a logged skip; the restore cycle itself still completed.
        assert any(getattr(r, "event", None) == "relocate_focus_restore_skip"
                   and getattr(r, "reason", None) == "client_gone"
                   for r in caplog.records)
        assert any(getattr(r, "event", None) == "relocate_cycle_done"
                   for r in caplog.records)
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert (PID_A, ST_A) not in coord._displaced
        assert (PID_B, ST_B) not in coord._displaced


def test_focus_restore_failure_does_not_abort_cycle(tmp_path, monkeypatch, logger, caplog):
    proc_root, events, orig_run, clients = _focus_fixture(tmp_path, monkeypatch)
    sock = tmp_path / "dwm.sock"

    class BoomOnUserFocus(MockControl):
        """Defence-in-depth: the seam promises never to raise -- assume it does."""

        def focus(self, xid):
            if xid == USER:
                raise RuntimeError("focus boom")
            return super().focus(xid)

    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = _focus_coord(srv, outs, events, proc_root, sock,
                             control=BoomOnUserFocus(srv, events))

        srv.select(USER)
        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _evacuate(orig_run, srv, sock, B)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)

        _replug(outs, "DP-2")
        with caplog.at_level(logging.DEBUG, logger="xrandrw"):
            coord.on_settled({}, logger)      # must not raise

        assert any(getattr(r, "event", None) == "relocate_focus_restore_skip"
                   for r in caplog.records)
        # The cycle still finished and BOTH windows were still restored+dropped.
        assert any(getattr(r, "event", None) == "relocate_cycle_done"
                   for r in caplog.records)
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert _wait_for(lambda: srv.state(B)["monitor_number"] == 1)
        assert not coord._displaced


# ---------------------------------------------------------------------------
# UX-03: dwm command rejections must be SURFACED, not silently treated as
# success, and no verb may ever be issued with a negative argument (dwm's
# dwm-ipc schema types tagmon/focusmon UNSIGNED -> "Type mismatch").
# ---------------------------------------------------------------------------

M2 = 0x140001A         # co-resident anchors, one set per monitor, never captured
M2B = 0x140001B
M0 = 0x140002A
M0B = 0x140002B
M1 = 0x140003A
M1B = 0x140003B


def test_three_monitor_restore_reaches_target_without_negative_args(
        tmp_path, monkeypatch, logger):
    """A backward-wrap restore on 3 monitors: the OLD code emitted tagmon(-1).

    ``tagmon_direction(0, 2, 3)`` used to be -1 (1 backward hop vs 2 forward),
    which real dwm rejects with "Type mismatch" -- the window would never move
    and the daemon would believe it had. Forward-only, the bounded loop walks the
    2 hops and lands on target, which is what this asserts end-to-end.
    """
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    g = {"x": 3850, "y": 40, "width": 300, "height": 400}
    # A on DP-3 (monitor 2). Two anchors per monitor keep every hop clear of the
    # crash-safety gate (no source emptied, no monitor left at n == 1).
    clients = [_client(A, 4, 2, g, True),
               _client(M0, 1, 0, GB, False), _client(M0B, 1, 0, GB, False),
               _client(M1, 1, 1, GB, False), _client(M1B, 1, 1, GB, False),
               _client(M2, 1, 2, GB, False), _client(M2B, 1, 2, GB, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs(_TRIPLE)
        coord = relocate.RelocationCoordinator(
            control=MockControl(srv, events), reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        assert coord._snapshot[(PID_A, ST_A)].output == "DP-3"
        _evacuate(orig_run, srv, sock, A)     # dwm evacuates A: monitor 2 -> 0
        _unplug(outs, "DP-3")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        events.clear()
        _replug(outs, "DP-3")
        coord.on_settled({}, logger)

        # It ARRIVED -- 0 -> 1 -> 2, the long way round, in exactly 2 hops.
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 2)
        assert srv.state(A)["tags"] == 4
        assert (PID_A, ST_A) not in coord._displaced
        tagmons = [e for e in events if e[0] == "cmd" and e[1] == "tagmon"]
        assert len(tagmons) == 2
        # NO verb ever carried a negative argument (dwm would reject every one).
        assert all(arg >= 0 for e in events if e[0] == "cmd" for arg in e[2])


def test_dwm_rejection_is_surfaced_not_silently_swallowed(
        tmp_path, monkeypatch, logger, caplog):
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    _install_common(monkeypatch, events)
    orig_run = dwmipc.run_command
    sock = tmp_path / "dwm.sock"

    # dwm refuses `tag` exactly as it refuses an unregistered/mistyped command.
    def rejecting_run(name, *args, **kw):
        events.append(("cmd", name, args))
        if name == "tag":
            return {"result": "error", "reason": "Type mismatch"}
        return orig_run(name, *args, **kw)

    clients = [_client(A, 4, 1, GA, True), _client(ANCHOR, 1, 0, GB, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = relocate.RelocationCoordinator(
            control=MockControl(srv, events), reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)

        monkeypatch.setattr(relocate.dwmipc, "run_command", rejecting_run)
        _replug(outs, "DP-2")
        with caplog.at_level(logging.INFO, logger="xrandrw"):
            coord.on_settled({}, logger)

        rejected = [r for r in caplog.records
                    if getattr(r, "event", None) == "relocate_ipc_rejected"]
        assert rejected, "a dwm-side rejection must be logged, not pass as success"
        assert rejected[0].verb == "tag"
        assert rejected[0].reason == "Type mismatch"
        # WM-08 stands: the rejection is NOT fatal to the CYCLE -- the window still
        # reached its target monitor via the (accepted) tagmon and nothing raised.
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        # B-3: but `tag` is ESSENTIAL -- A is on the right monitor carrying the
        # WRONG TAGS, so it is NOT restored. The record must SURVIVE for a later
        # cycle to retry, and the daemon must not claim a restore it did not do.
        # (This assertion previously read `not in coord._displaced`: it encoded
        # the defect, because _restore_one discarded _run_verb's bool and dropped
        # the record while logging success.)
        assert (PID_A, ST_A) in coord._displaced
        events_logged = [getattr(r, "event", None) for r in caplog.records]
        assert "relocate_restore" not in events_logged, \
            "a window whose essential verb was rejected must not log a restore"
        assert "relocate_restore_incomplete" in events_logged


def test_all_verbs_rejected_keeps_record_and_logs_no_restore(
        tmp_path, monkeypatch, logger, caplog):
    """B-3: when dwm rejects EVERY verb, nothing was restored -- say so, keep the record.

    The pre-fix code discarded ``_run_verb``'s bool on the restore path
    (``_restore_one`` tag/togglefloating, ``_tagmon_to_target`` tagmon; only
    ``_focusmon_to`` acted on it). With every command refused it still fell
    through to ``logev(... "relocate_restore", "restored displaced window")`` and
    returned "drop", DELETING the record: the window sat exactly where dwm's
    evacuation left it, the daemon logged a success, and because the record was
    gone no later cycle ever retried. This pins the corrected outcome.
    """
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    _install_common(monkeypatch, events)
    orig_run = dwmipc.run_command
    sock = tmp_path / "dwm.sock"

    def reject_everything(name, *args, **kw):
        events.append(("cmd", name, args))
        return {"result": "error", "reason": f"Command {name} not found"}

    clients = [_client(A, 4, 1, GA, True), _client(ANCHOR, 1, 0, GB, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = relocate.RelocationCoordinator(
            control=MockControl(srv, events), reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)      # dwm evacuates A: monitor 1 -> 0
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        monkeypatch.setattr(relocate.dwmipc, "run_command", reject_everything)
        _replug(outs, "DP-2")
        with caplog.at_level(logging.INFO, logger="xrandrw"):
            coord.on_settled({}, logger)

        # Nothing took effect: A is still where dwm's evacuation left it.
        assert srv.state(A)["monitor_number"] == 0

        events_logged = [getattr(r, "event", None) for r in caplog.records]
        # THE CORE OF B-3: no success log, and the record SURVIVES so a later
        # replug cycle retries the window instead of silently losing it.
        assert "relocate_restore" not in events_logged, \
            "every verb was rejected -- the daemon must not log a restore"
        assert "relocate_restore_incomplete" in events_logged
        assert (PID_A, ST_A) in coord._displaced, \
            "a wholly-rejected restore must LEAVE the record for a later retry"

        # And a later cycle really does retry it: with dwm accepting again, the
        # same record restores and only THEN drops.
        monkeypatch.setattr(relocate.dwmipc, "run_command", orig_run)
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        _replug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert _wait_for(lambda: srv.state(A)["monitor_number"] == 1)
        assert (PID_A, ST_A) not in coord._displaced


class StockDwmControl:
    """A control whose ``focus()`` sends the ClientMessage but does NOT focus.

    This is STOCK dwm. Its ``clientmessage`` handles ``_NET_ACTIVE_WINDOW`` with
    ``seturgent(c, 1)`` -- it flags urgency and never touches the selection. Every
    other control in this suite bridges ``focus -> srv.select`` because the
    functional harness dwm carries a ``focusonnetactive`` patch
    (``tests/functional/dwm/dwm-ipc.diff``) added SPECIFICALLY so relocate's
    focus-then-act works. That patch is why B-4 went unnoticed: the whole test
    corpus modelled a patched dwm, so nothing could reproduce a PyPI user's
    stock install.
    """

    def __init__(self, srv, events):
        self.srv = srv
        self.events = events

    def focus(self, xid):
        self.events.append(("focus", xid))
        return True          # the send succeeded; dwm just ignored it

    def configure_geometry(self, xid, geom):
        self.srv.set_geometry(xid, geom)
        self.events.append(("configure", xid))
        return True


def test_unconfirmed_focus_sends_no_mutating_verb(tmp_path, monkeypatch, logger, caplog):
    """B-4: on a dwm that does not focus on _NET_ACTIVE_WINDOW, issue NOTHING.

    Pre-fix, `_focus_and_confirm` logged `relocate_focus_unconfirmed` and
    PROCEEDED BEST-EFFORT, so tag/tagmon/togglefloating were fired at whatever dwm
    actually had selected -- on a stock install, the USER'S OWN WINDOW, silently
    retagged and dragged to another monitor on every single replug.

    Here ANCHOR is the user's focused window and A is the displaced one. The
    control models stock dwm (the ClientMessage lands, the selection does not
    move), so dwm keeps reporting ANCHOR as selected. The restore must issue NO
    mutating verb at all, must not touch ANCHOR, and must keep A displaced.
    """
    proc_root = _make_proc(tmp_path, [(PID_A, "a", ST_A)])
    events = []
    orig_run = _install_common(monkeypatch, events)
    sock = tmp_path / "dwm.sock"
    clients = [_client(A, 4, 1, GA, True), _client(ANCHOR, 1, 0, GB, False)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        coord = relocate.RelocationCoordinator(
            control=StockDwmControl(srv, events), reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)        # dwm evacuates A: monitor 1 -> 0
        _unplug(outs, "DP-2")
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # THE USER refocuses their own window -- and stock dwm will keep it
        # selected no matter how many _NET_ACTIVE_WINDOW messages we send.
        srv.select(ANCHOR)
        anchor_before = srv.state(ANCHOR)
        events.clear()
        _replug(outs, "DP-2")
        with caplog.at_level(logging.INFO, logger="xrandrw"):
            coord.on_settled({}, logger)

        # 1. NOT ONE mutating verb was sent.
        mutating = [e for e in events
                    if e[0] == "cmd" and e[1] in ("tag", "tagmon", "togglefloating")]
        assert mutating == [], f"unconfirmed focus must issue no verb, sent: {mutating}"

        # 2. The user's window is exactly as they left it -- same monitor, same
        #    tags, same floating state. This is the damage the bug did.
        assert srv.state(ANCHOR) == anchor_before

        # 3. The displaced window was left alone, and the record survives (B-3),
        #    so the restore retries if the user later switches to a dwm that
        #    honours _NET_ACTIVE_WINDOW.
        assert srv.state(A)["monitor_number"] == 0
        assert (PID_A, ST_A) in coord._displaced

        # 4. And it said so, loudly.
        logged = [getattr(r, "event", None) for r in caplog.records]
        assert "relocate_focus_unconfirmed" in logged
        assert "relocate_restore" not in logged
