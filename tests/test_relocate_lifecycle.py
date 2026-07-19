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


def _make_outputs():
    # W1 geometry alignment: positions/modes MUST line up with the fake server's
    # per-monitor origins (num*1920, 1920x1080) or match_dwm_monitor_to_output
    # resolves to None and the test could pass VACUOUSLY.
    return {
        "DP-1": Output(name="DP-1", connected=True, current_mode=(1920, 1080),
                       position=(0, 0), edid_sha1="edA"),
        "DP-2": Output(name="DP-2", connected=True, current_mode=(1920, 1080),
                       position=(1920, 0), edid_sha1="edB"),
    }


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

        # 1. BOOT SEED (all connected): seeds _prev_connected + _snapshot only.
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._snapshot
        assert coord._snapshot[(PID_A, ST_A)].output == "DP-2"
        assert not coord._displaced

        # 2. FIRST unplug DP-2: dwm evacuates A to monitor 0; A recorded displaced.
        _evacuate(orig_run, srv, sock, A)
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # 3. FIRST replug DP-2: A restored to monitor 1, tags 4, floating, geometry.
        outs["DP-2"].connected = True
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
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)
        outs["DP-2"].connected = True
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
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # PID reused: starttime moved -> identity no longer matches the record.
        _rewrite_starttime(tmp_path, PID_A, "a", ST_A + 999)
        events.clear()
        outs["DP-2"].connected = True
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
        assert coord._prev_connected is None
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
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced and (PID_B, ST_B) in coord._displaced

        # B's live client read raises for one window only (per-window DwmIpcUnavailable).
        orig_get = dwmipc.get_dwm_client

        def get_spy(win, path=None, **kw):
            if int(win) == B:
                raise dwmipc.DwmIpcUnavailable("bad window")
            return orig_get(win, path=path, **kw)
        monkeypatch.setattr(relocate.dwmipc, "get_dwm_client", get_spy)

        outs["DP-2"].connected = True
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
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)

        # Make tagmon a no-op so A can never reach its target monitor -> giveup.
        real_run = dwmipc.run_command

        def run_noop_tagmon(name, *args, **kw):
            events.append(("cmd", name, args))
            if name == "tagmon":
                return {"result": "success"}   # no movement
            return real_run(name, *args, **kw)
        monkeypatch.setattr(relocate.dwmipc, "run_command", run_noop_tagmon)

        outs["DP-2"].connected = True
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
    clients = [_client(A, 4, 1, GA, True)]
    with FakeDwmServer(sock, mode="stateful", clients=clients) as srv:
        outs = _make_outputs()
        control = MockControl(srv, events)
        coord = relocate.RelocationCoordinator(
            control=control, reader=FakeRandr(outs),
            xreader=_fake_xreader({A: PID_A}),
            sock_path=str(sock), proc_root=proc_root)

        coord.on_settled({}, logger)
        _evacuate(orig_run, srv, sock, A)   # A now the ONLY client on monitor 0
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)
        events.clear()
        outs["DP-2"].connected = True
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
        outs["DP-2"].connected = False
        coord.on_settled({}, logger)
        assert (PID_A, ST_A) in coord._displaced

        # dwm's selected client is now the WRONG window (B), and selection LAGS by
        # one poll: a verb issued without confirming would act on B, not A.
        srv.select(B)
        srv.set_select_lag(1)
        events.clear()

        outs["DP-2"].connected = True
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
        outs["DP-2"].connected = False
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

        outs["DP-2"].connected = True
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
        outs["DP-2"].connected = False           # DP-2 stays disconnected
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
