"""Hook-timing + existing-behavior-preservation tests for the Phase-10 watch hook.

Mirrors the ``_drive`` harness in tests/test_watch.py (FakeDisplay, monkeypatched
``watch.display.Display`` / ``watch.topology_hash`` / ``watch.apply_once`` and the
``select`` seam) and proves the additive coordinator hook: it fires exactly once
post-apply on a real change, never on the wakeup/unchanged paths, survives a
coordinator fault, and leaves the no-coordinator path byte-for-byte identical.

The second half of this file (14-08) carries the LIVE replug-bounce trace end to
end over the REAL ``apply_once`` and the REAL ``scrub_stale``; see the banner
comment there.
"""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace

import pytest

import xrandrw.apply as apply_mod
import xrandrw.relocate as relocate
import xrandrw.watch as watch
import xrandrw.windows as win_mod
from xrandrw import dwmipc
from xrandrw.xrandr import Output, topology_hash_from_outputs


class FakeDisplay:
    def __init__(self, version=(1, 5)):
        self.version = version
        self.pending = 0
        self.selected_mask = None
        self.closed = False

    def screen(self):
        def _select(mask):
            self.selected_mask = mask
        return SimpleNamespace(root=SimpleNamespace(xrandr_select_input=_select))

    def flush(self):
        pass

    def fileno(self):
        return 77

    def xrandr_query_version(self):
        return SimpleNamespace(major_version=self.version[0], minor_version=self.version[1])

    def pending_events(self):
        return self.pending

    def next_event(self):
        self.pending -= 1
        return None

    def close(self):
        self.closed = True


class SpyCoordinator:
    """Records on_settled invocations into a shared event log (for ordering)."""

    def __init__(self, events, *, raises=False):
        self.events = events
        self.raises = raises
        self.calls = 0
        self.stop_evts = []

    def on_settled(self, env, logger, stop_evt=None):
        self.calls += 1
        self.stop_evts.append(stop_evt)
        self.events.append(("settle",))
        if self.raises:
            raise RuntimeError("coordinator boom")


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw")
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    watch.stop_evt.clear()
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    yield
    watch.stop_evt.clear()


def _env():
    return {"POLL_INTERVAL": "45", "EXCESS_WINDOW_SEC": "10", "EXCESS_THRESHOLD": "5"}


def _drive(monkeypatch, fake, script, topo, events):
    """Wire the module seams; apply_once appends ('apply', src) to the shared log."""
    monkeypatch.setattr(watch.display, "Display", lambda: fake)
    monkeypatch.setattr(watch, "topology_hash", lambda logger=None: topo["hash"])

    def _apply(env, logger, event_source):
        events.append(("apply", event_source))
        # BL-01: apply_once's contract is now `-> bool` (True == a full apply
        # completed). The fake must honour it, or these hook-timing tests would
        # silently exercise the new "apply bailed" branch (no settle, no hash
        # absorption) instead of the normal post-apply path they mean to cover.
        return True
    monkeypatch.setattr(watch, "apply_once", _apply)

    pipe = {}
    real_pipe = os.pipe

    def _capture_pipe():
        r, w = real_pipe()
        pipe["r"], pipe["w"] = r, w
        return r, w
    monkeypatch.setattr(watch.os, "pipe", _capture_pipe)

    it = iter(script)

    def _select(rfds, wfds, xfds, timeout):
        try:
            token, mutate = next(it)
        except StopIteration:
            watch.stop_evt.set()
            return ([], [], [])
        if mutate:
            mutate()
        xfd, rpipe = rfds[0], rfds[1]
        if token == "wake":
            os.write(pipe["w"], b"\x00")
            return ([rpipe], [], [])
        if token == "event":
            return ([xfd], [], [])
        return ([], [], [])  # timeout
    monkeypatch.setattr(watch.select, "select", _select)


def test_hook_fires_once_after_apply_on_change(monkeypatch, logger):
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)
    coord = SpyCoordinator(events)

    watch.watch_loop(_env(), logger, coordinator=coord)

    assert coord.calls == 1, "coordinator runs exactly once per applied change"
    # Ordering: the settle hook fires AFTER apply_once.
    assert events == [("apply", "randr_event"), ("settle",)]
    # WR-01: the watch loop threads its shutdown flag into the hook.
    assert coord.stop_evts == [watch.stop_evt]


def test_hook_not_called_on_wakeup_or_unchanged(monkeypatch, logger):
    # Wakeup-pipe exit: no apply, no settle.
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []
    _drive(monkeypatch, fake, [("wake", watch.stop_evt.set)], topo, events)
    coord = SpyCoordinator(events)
    watch.watch_loop(_env(), logger, coordinator=coord)
    assert coord.calls == 0 and events == []

    # Unchanged hash on a slow-poll timeout: no apply, no settle.
    watch.stop_evt.clear()
    fake2 = FakeDisplay()
    topo2 = {"hash": "h0"}
    events2 = []
    _drive(monkeypatch, fake2, [("timeout", watch.stop_evt.set)], topo2, events2)
    coord2 = SpyCoordinator(events2)
    watch.watch_loop(_env(), logger, coordinator=coord2)
    assert coord2.calls == 0 and events2 == []


def test_coordinator_fault_never_breaks_loop(monkeypatch, logger, caplog):
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)
    coord = SpyCoordinator(events, raises=True)

    with caplog.at_level(logging.WARNING, logger="xrandrw"):
        watch.watch_loop(_env(), logger, coordinator=coord)

    # Apply still happened exactly once; loop exited cleanly; Display closed.
    assert [e for e in events if e[0] == "apply"] == [("apply", "randr_event")]
    assert fake.closed
    assert any(getattr(r, "event", None) == "relocate_hook_fail" for r in caplog.records)


def test_no_coordinator_path_is_identical(monkeypatch, logger):
    # Regression guard: omitting the coordinator applies exactly as before.
    fake = FakeDisplay()
    topo = {"hash": "h0"}
    events = []

    def _burst():
        fake.pending = 3
        topo["hash"] = "h1"
    script = [("event", _burst), ("timeout", watch.stop_evt.set)]
    _drive(monkeypatch, fake, script, topo, events)

    watch.watch_loop(_env(), logger)   # no coordinator

    assert events == [("apply", "randr_event")]
    assert fake.closed


# ==========================================================================
# 14-08: the LIVE replug-bounce path, spanning apply.py + relocate.py
# ==========================================================================
#
# Every other apply-level suite in this repo monkeypatches ``scrub_stale`` to a
# no-op (test_apply.py:33, test_apply_branches.py:47,
# test_apply_place_externals_golden.py:51, test_regression_rpi4_core.py:51), so
# nothing here may be built on those fixtures -- an apply-level test inheriting
# them passes VACUOUSLY. These tests drive the REAL ``apply_once`` and the REAL
# ``scrub_stale`` by faking only apply_once's own seams (read_xrandr, wait_for_x,
# read_edids, the wallpaper/touch delegators and the ApplyBackend) over a single
# MUTABLE world model that both the apply layer and the relocation layer observe.
#
# Trace: .planning/debug/relocate-replug-bounce.md (+ evidence/newdaemon2.log).
# Isolation: the autouse ``block_live_dwm`` fixture applies (this file is
# unmarked) and every coordinator here is wired with explicit
# control=/reader=/xreader=/proc_root=/sock_path= -- a bare
# ``RelocationCoordinator()`` defaults to live X and a live dwm socket.

# Live connectors and geometry from the log: eDP-1 @ (0,0) 1920x1200 primary,
# HDMI-1 @ (1920,0) 1600x900 placed --right-of eDP-1.
_LIVE_GEOMETRY = {
    "eDP-1": ((0, 0), (1920, 1200)),
    "HDMI-1": ((1920, 0), (1600, 900)),
}

# The three throwaway xterms of the live test (LIVE-A/B/C) plus a co-resident
# ANCHOR on the surviving monitor. Without the anchor the crash-safety gate
# (_tagmon_would_crash_dwm) refuses the restore hop and the test would fail for
# the wrong reason. The anchor is deliberately absent from the xreader pid map so
# capture never resolves or records it.
_LIVE_XIDS = (0x2000001, 0x2000002, 0x2000003)
_ANCHOR_XID = 0x2000009
_LIVE_PROCS = {0x2000001: (926962, 5001), 0x2000002: (926986, 5002),
               0x2000003: (927000, 5003)}


class BounceWorld:
    """Single mutable source of truth for outputs + dwm clients.

    HPD (``Output.connected``) is PHYSICAL and only the scenario mutates it. The
    CRTC (``position``/``current_mode``) is the DAEMON's, and only the apply
    backend mutates it. dwm monitors are DERIVED: one per LIT output, numbered by
    ascending x origin, so eDP-1 is monitor 0 and HDMI-1 is monitor 1.
    """

    def __init__(self):
        self.outs = {
            name: Output(name=name, connected=True, position=pos, current_mode=mode,
                         modes=[(mode[0], mode[1], 60.0, "*+")])
            for name, (pos, mode) in _LIVE_GEOMETRY.items()
        }
        self.clients = {xid: {"tags": 1, "monitor_number": 1, "is_floating": False}
                        for xid in _LIVE_XIDS}
        self.clients[_ANCHOR_XID] = {"tags": 1, "monitor_number": 0, "is_floating": False}
        self.selected = None
        self.ops = []

    def hpd(self, name, up):
        self.outs[name].connected = up

    def lit(self):
        # WR-03: the model uses the PRODUCTION liveness predicate. It previously
        # spelled its own (`position is not None and current_mode is not None`),
        # which is how the four-way divergence started.
        return sorted((o for o in self.outs.values() if o.is_lit),
                      key=lambda o: o.position[0])

    def monitor_of(self, name):
        for num, o in enumerate(self.lit()):
            if o.name == name:
                return num
        return None

    def monitors_of_live_windows(self):
        return [self.clients[xid]["monitor_number"] for xid in _LIVE_XIDS]

    def snapshot(self):
        # A read returns a COPY: apply_once must not be able to mutate the model
        # except through the backend, which is where the semantics are encoded.
        return {n: Output(name=o.name, connected=o.connected, primary=o.primary,
                          current_mode=o.current_mode, position=o.position,
                          modes=list(o.modes))
                for n, o in self.outs.items()}


class ScenarioBackend:
    """The two xrandr/dwm semantics the live log demonstrates, encoded explicitly.

    ``output_off(name)`` -- log 04:21:43,291 ``xrandr --output HDMI-1 --off``.
    Darkens the CRTC AND EVACUATES the monitor's clients: dwm collapses the
    vanished monitor and reassigns every client on it to monitor 0. This is the
    step that actually strands the windows; without it the headline assertion
    would pass at HEAD and the RED gate would prove nothing.

    ``auto_pos`` / ``primary_scale`` -- log 04:21:44,851
    ``xrandr --output HDMI-1 --auto --right-of eDP-1``. Re-lights the CRTC at the
    output's desired geometry so the dwm monitor reappears, but moves NO client:
    the live log shows all three windows still on eDP-1 afterwards. Windows return
    ONLY via an explicit tagmon from the coordinator.
    """

    def __init__(self, world):
        self.world = world

    def _light(self, connector):
        pos, mode = _LIVE_GEOMETRY[connector]
        self.world.outs[connector].position = pos
        self.world.outs[connector].current_mode = mode

    def output_off(self, connector, logger):
        w = self.world
        w.ops.append(("output_off", connector))
        num = w.monitor_of(connector)
        w.outs[connector].position = None
        w.outs[connector].current_mode = None
        if num is None:
            return
        for client in w.clients.values():
            if client["monitor_number"] == num:
                client["monitor_number"] = 0

    def primary_scale(self, connector, scale, logger):
        self.world.ops.append(("primary_scale", connector))
        self._light(connector)

    def auto_pos(self, connector, rel_opt, anchor, logger):
        self.world.ops.append(("auto_pos", connector))
        self._light(connector)

    def rotate_left_if_portrait(self, connector, o, logger):
        self.world.ops.append(("rotate", connector))


class ScenarioReader:
    """Coordinator-side RandR seam over the world, with a per-read scenario clock.

    ``arm(after, fn)`` runs ``fn`` immediately BEFORE the ``after``-th subsequent
    read. That is how the live INTRA-SETTLE race is reproduced: ``on_settled``'s
    edge read (relocate.py:338) and ``capture_windows``' own read (windows.py:438)
    are two SEPARATE round-trips, and at 04:21:43,105 -> 43,109 the physical link
    state changed between them. The bounce is injected HERE, in the reader -- the
    coordinator is never handed a scripted connected/disconnected pair.
    """

    def __init__(self, world):
        self.world = world
        self.reads = 0
        self._armed = []

    def arm(self, after, fn):
        self._armed.append([after, fn])

    def read(self, logger=None):
        for slot in list(self._armed):
            if slot[0] == 0:
                self._armed.remove(slot)
                slot[1]()
            else:
                slot[0] -= 1
        self.reads += 1
        return self.world.snapshot()


class FakeDwm:
    """In-process dwm-ipc stand-in; monitors DERIVE from the world's lit outputs."""

    def __init__(self, world):
        self.world = world
        self.commands = []

    def get_monitors(self, path=None, timeout=None):
        w = self.world
        mons = []
        for num, o in enumerate(w.lit()):
            x, y = o.position
            width, height = o.current_mode
            allc = [xid for xid, c in w.clients.items() if c["monitor_number"] == num]
            sel = w.selected if w.selected in allc else None
            mons.append({"num": num,
                         "monitor_geometry": {"x": x, "y": y, "width": width, "height": height},
                         "is_selected": sel is not None,
                         "clients": {"all": allc, "selected": sel}})
        return mons

    def get_dwm_client(self, xid, path=None, timeout=None):
        client = self.world.clients.get(int(xid))
        if client is None:
            raise dwmipc.DwmIpcUnavailable(f"no such window {xid}")
        return {"xid": int(xid), "tags": client["tags"],
                "monitor_number": client["monitor_number"],
                "geometry": {"current": {"x": 0, "y": 0, "width": 800, "height": 600}},
                "states": {"is_floating": client["is_floating"], "is_fullscreen": False}}

    def run_command(self, name, *args, path=None, timeout=None):
        self.commands.append((name, args))
        w = self.world
        client = w.clients.get(w.selected)
        if client is None:
            return {"result": "success"}
        if name == "tagmon":
            n = len(w.lit())
            if n:
                step = 1 if args[0] > 0 else -1
                client["monitor_number"] = (client["monitor_number"] + step) % n
        elif name == "tag":
            client["tags"] = args[0]
        elif name == "togglefloating":
            client["is_floating"] = not client["is_floating"]
        return {"result": "success"}


class ScenarioControl:
    def __init__(self, world):
        self.world = world

    def focus(self, xid):
        self.world.selected = int(xid)
        return True

    def configure_geometry(self, xid, geometry):
        return True


def _bounce_proc_root(tmp_path):
    root = tmp_path / "bounce-proc"
    for pid, starttime in _LIVE_PROCS.values():
        d = root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        tail = ["S"] + [str(i) for i in range(1, 19)] + [str(starttime), "0", "0"]
        (d / "stat").write_text(f"{pid} (xterm) " + " ".join(tail) + "\n")
        (d / "comm").write_text("xterm\n")
        (d / "cmdline").write_bytes(b"xterm\x00")
    return str(root)


def _bounce_env(tmp_path):
    return {
        "LOCKFILE": str(tmp_path / "bounce.lock"),
        "STATE_LOCKFILE": str(tmp_path / "bounce.state.lock"),
        "PREF_DEFAULT_SIDE": "right-of",
        "HIDPI_WIDTH": "3840",
        "APPLY_BACKEND": "subprocess",
    }


def _wire_apply_seams(monkeypatch, world, backend):
    """Fake apply_once's OWN seams -- never apply_once itself, never scrub_stale."""
    monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: world.snapshot())
    monkeypatch.setattr(apply_mod, "wait_for_x", lambda logger: None)
    monkeypatch.setattr(apply_mod, "read_edids", lambda outs, logger: None)
    monkeypatch.setattr(apply_mod, "reapply_wallpaper", lambda env, logger: None)
    monkeypatch.setattr(apply_mod, "remap_touch", lambda env, names, logger: None)
    monkeypatch.setattr(apply_mod, "run", lambda *a, **k: None)
    monkeypatch.setattr(apply_mod, "get_apply_backend", lambda env: backend)


@pytest.fixture
def bounce(tmp_path, monkeypatch):
    world = BounceWorld()
    backend = ScenarioBackend(world)
    reader = ScenarioReader(world)
    dwm = FakeDwm(world)

    _wire_apply_seams(monkeypatch, world, backend)
    monkeypatch.setattr(win_mod, "read_edids", lambda outs, logger=None: None)
    monkeypatch.setattr(dwmipc, "available", lambda path=None, **kw: True)
    monkeypatch.setattr(dwmipc, "get_monitors", dwm.get_monitors)
    monkeypatch.setattr(dwmipc, "get_dwm_client", dwm.get_dwm_client)
    monkeypatch.setattr(dwmipc, "run_command", dwm.run_command)
    monkeypatch.setattr(watch, "topology_hash",
                        lambda logger=None: topology_hash_from_outputs(world.outs))

    xreader = SimpleNamespace(
        net_wm_pid=lambda xid: _LIVE_PROCS.get(int(xid), (None, None))[0],
        client_machine=lambda xid: None,
        xres_pid=lambda xid: None,
        has_xres=lambda: True,
    )
    coord = relocate.RelocationCoordinator(
        control=ScenarioControl(world), reader=reader, xreader=xreader,
        sock_path=str(tmp_path / "bounce-dwm.sock"),
        proc_root=_bounce_proc_root(tmp_path))

    settles = {"n": 0}
    real_on_settled = coord.on_settled

    def counting_on_settled(env, logger, stop_evt=None):
        settles["n"] += 1
        return real_on_settled(env, logger, stop_evt=stop_evt)
    monkeypatch.setattr(coord, "on_settled", counting_on_settled)

    return SimpleNamespace(world=world, coord=coord, reader=reader, dwm=dwm,
                           settles=settles, env=_bounce_env(tmp_path))


def _churn():
    return {"times": [], "backoff": 0, "window": 10, "threshold": 5}


def test_replug_bounce_live_path_recovers_windows(bounce, logger):
    """THE headline test: the logged trace, carried end to end.

    HDMI-1 reads disconnected on BOTH reads of the bounce apply, IS powered off,
    dwm evacuates the three windows to monitor 0, and they must be back on
    monitor 1 after the subsequent replug apply.
    """
    world, coord, reader, env = bounce.world, bounce.coord, bounce.reader, bounce.env
    churn = _churn()
    obs = {}

    # STEP 1 -- boot seed at steady state (log L29 `seeded steady-state baseline
    # connected=2 windows=3`). All three windows live on HDMI-1 / dwm monitor 1.
    coord.on_settled(env, logger)
    obs["seed_outputs"] = sorted(str(r.output) for r in coord._snapshot.values())

    # STEP 2 -- the replug apply (log 40,72x..42,917) and its re-seed (43,109).
    # The HPD drop lands BETWEEN on_settled's edge read and capture_windows' own
    # read: live, the restore completed at 43,105 with HDMI-1 present, and 4 ms
    # later the capture logged NO HDMI-1 EDID and `candidates=0`. HDMI-1 is then
    # disconnected but STILL LIT at (1920,0)/1600x900 from the 42,917 --auto.
    reader.arm(1, lambda: world.hpd("HDMI-1", False))
    last = watch._apply_if_changed(env, logger, "topology-before-the-replug",
                                   churn, True, coord)
    obs["reseed_outputs"] = sorted(str(r.output) for r in coord._snapshot.values())

    # STEP 3 -- the bounce apply (log 43,127 hash change -> 43,291 `--off`).
    # `last` was frozen BEFORE the settle-time HPD drop, so the loop observes the
    # change exactly as it did live. Both reads of this apply see HDMI-1
    # disconnected, so the real scrub_stale powers it off and dwm evacuates.
    #
    # FIDELITY GUARD: HPD comes back UP before this apply's SETTLE (live 44,79x /
    # 47,095 reads a valid EDID again) while the CRTC stays dark. An HPD-based
    # `cur` therefore equals `_prev_connected` and yields NO removal edge -- only
    # the dark CRTC distinguishes the state. Without this the test could pass for
    # the wrong reason.
    reader.arm(0, lambda: world.hpd("HDMI-1", True))
    last = watch._apply_if_changed(env, logger, last, churn, True, coord)
    obs["monitors_after_bounce"] = world.monitors_of_live_windows()

    # STEP 4 -- the replug apply (log 44,851 `--auto --right-of eDP-1`): the CRTC
    # is re-lit and dwm monitor 1 reappears, but NO client moves. The windows come
    # back only via the coordinator's tagmon at this apply's settle.
    watch._apply_if_changed(env, logger, last, churn, True, coord)

    # --- headline assertion: behavioural, so it survives any fix shape ---------
    final = {hex(xid): world.clients[xid]["monitor_number"] for xid in _LIVE_XIDS}
    assert set(final.values()) == {1}, (
        "all three windows must be back on dwm monitor 1 (HDMI-1) after the replug; "
        f"got {final} (0 == stranded on eDP-1, the live failure)")

    # --- mechanism guard: the eviction really happened, it is now RECOVERABLE ---
    assert ("output_off", "HDMI-1") in world.ops, (
        "the bounce apply must still issue the HDMI-1 --off; the fix makes the "
        "eviction recoverable, NOT absent")
    assert obs["monitors_after_bounce"] == [0, 0, 0], (
        "dwm must have evacuated all three windows to monitor 0 mid-scenario")

    # --- the snapshot the bounce recorded from must not have been poisoned -----
    assert obs["seed_outputs"] == ["HDMI-1"] * 3
    assert obs["reseed_outputs"] == ["HDMI-1"] * 3, (
        "the unplugged-but-still-lit HDMI-1 must still be nameable (live 43,109 "
        "logged candidates=0 and captured all three records with output=None)")

    # --- the number of coordinator observations is an OUTPUT, not an input -----
    assert bounce.settles["n"] == 4


class _RecordingBackend:
    def __init__(self):
        self.offs = []
        self.autos = []
        self.applied = None  # apply_once's completion bool (BL-01)

    def output_off(self, connector, logger):
        self.offs.append(connector)

    def primary_scale(self, connector, scale, logger):
        pass

    def auto_pos(self, connector, rel_opt, anchor, logger):
        self.autos.append(connector)

    def rotate_left_if_portrait(self, connector, o, logger):
        pass


def _apply_over_scripted_reads(monkeypatch, tmp_path, logger, reads):
    """Run the REAL apply_once (and the REAL scrub_stale) over a scripted read pair.

    A scripted item that is an ``Exception`` is RAISED instead of returned, which
    is how a transient mid-apply X failure (apply.py's read #1 / read #2 guards)
    is expressed.
    """
    backend = _RecordingBackend()
    it = iter(reads)

    def _read(logger):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    monkeypatch.setattr(apply_mod, "read_xrandr", _read)
    monkeypatch.setattr(apply_mod, "wait_for_x", lambda logger: None)
    monkeypatch.setattr(apply_mod, "read_edids", lambda outs, logger: None)
    monkeypatch.setattr(apply_mod, "reapply_wallpaper", lambda env, logger: None)
    monkeypatch.setattr(apply_mod, "remap_touch", lambda env, names, logger: None)
    monkeypatch.setattr(apply_mod, "run", lambda *a, **k: None)
    monkeypatch.setattr(apply_mod, "get_apply_backend", lambda env: backend)
    # BL-01: expose apply_once's completion bool so a caller can assert on the
    # bail/complete distinction the watch loop now depends on.
    backend.applied = apply_mod.apply_once(_bounce_env(tmp_path), logger)
    return backend


def _heads(hdmi_connected):
    return {
        "eDP-1": Output(name="eDP-1", connected=True, position=(0, 0),
                        current_mode=(1920, 1200)),
        "HDMI-1": Output(name="HDMI-1", connected=hdmi_connected, position=(1920, 0),
                         current_mode=(1600, 900)),
    }


def test_intra_apply_straddle_never_offs_a_returning_head(tmp_path, monkeypatch, logger):
    """INVARIANT of the scrub reorder -- NOT the reproduction of the logged incident.

    A head that reads disconnected on read #1 and connected on read #2 must not be
    powered off. Post-reorder the two reads are ~10 ms apart (measured: 9 ms at
    40,728 -> 40,737), so this straddle is vanishingly unlikely in the field. It is
    pinned as the STRUCTURAL guarantee of the reorder: scrub and placement read the
    same snapshot, so one apply can never both --off and --auto a connector. The
    LOGGED incident had HDMI-1 disconnected on BOTH reads -- that is
    ``test_replug_bounce_live_path_recovers_windows``, not this test.
    """
    backend = _apply_over_scripted_reads(
        monkeypatch, tmp_path, logger, [_heads(False), _heads(True)])

    assert backend.offs == [], "a head that is back by read #2 must never be powered off"
    assert "HDMI-1" in backend.autos, "and it must still be placed"


def test_sustained_disconnect_still_powers_off(tmp_path, monkeypatch, logger):
    # Non-regression: the fix must not degenerate into "never turn anything off".
    # A head disconnected on BOTH reads with a lit CRTC is genuinely dead and is
    # still healed -- the xrandr.py:167-177 self-heal rationale depends on it.
    backend = _apply_over_scripted_reads(
        monkeypatch, tmp_path, logger, [_heads(False), _heads(False)])

    assert backend.offs == ["HDMI-1"]
    assert backend.autos == [], "a disconnected head is never placed"


def test_topology_hash_from_outputs_is_pure(output_factory):
    outs = {
        "eDP-1": output_factory("eDP-1", current_mode=(1920, 1200), position=(0, 0)),
        # Disconnected but STILL LIT: the state the whole defect turns on. It must
        # be inside the digest or change detection never sees the head at all.
        "HDMI-1": output_factory("HDMI-1", connected=False, current_mode=(1600, 900),
                                 position=(1920, 0)),
    }
    digest = topology_hash_from_outputs(outs)
    assert digest == topology_hash_from_outputs(dict(outs)), "same map -> same digest"

    dark = dict(outs)
    dark["HDMI-1"] = output_factory("HDMI-1", connected=False)
    assert topology_hash_from_outputs(dark) != digest, (
        "a disconnected-but-lit head must be distinguishable from a dark one")


# ---------------------------------------------------------------------------
# BL-01 / WR-01: apply_once's completion bool, and what the watch loop may
# absorb. These run the REAL apply_once and the REAL scrub_stale -- the natural
# home in tests/test_apply.py runs under the ``mock_x`` fixture, which no-ops
# scrub_stale, so it is STRUCTURALLY incapable of catching this regression.
# ---------------------------------------------------------------------------


class _SettleSpy:
    """Coordinator stand-in that only counts on_settled invocations (WR-01)."""

    def __init__(self):
        self.calls = 0

    def on_settled(self, env, logger, stop_evt=None):
        self.calls += 1


def _wire_apply_seams_scripted(monkeypatch, world, backend, read_hook):
    """``_wire_apply_seams`` but apply's OWN reads run through ``read_hook``.

    ``read_hook`` receives the 1-based index of the apply-level read (apply.py's
    read #1 and read #2) and may raise to simulate the transient mid-apply X
    failure the read-#2 guard was written for. The counter is CUMULATIVE across
    applies, so a hook can fail one apply and let the retry succeed.
    """
    _wire_apply_seams(monkeypatch, world, backend)
    n = {"i": 0}

    def _read(logger):
        n["i"] += 1
        read_hook(n["i"])
        return world.snapshot()
    monkeypatch.setattr(apply_mod, "read_xrandr", _read)
    return n


def test_read2_failure_bails_leaving_a_lit_head_unscrubbed(tmp_path, monkeypatch, logger):
    """BL-01 half 1: the bail is real and it skips the scrub.

    Post-reorder the read-#2 guard returns ~30 lines ABOVE ``scrub_stale``, so a
    transient failure leaves a disconnected-but-STILL-LIT head powered on. That is
    only safe if the caller can tell this happened -- hence the bool.
    """
    backend = _apply_over_scripted_reads(
        monkeypatch, tmp_path, logger,
        [_heads(False), RuntimeError("transient X error mid-apply")])

    assert backend.applied is False, "a read-#2 failure is a BAIL, not a completed apply"
    assert backend.offs == [], "the bail returns above scrub_stale; the lit head stays lit"


def test_completed_applies_report_true(tmp_path, monkeypatch, logger):
    """BL-01: the non-bail paths must report True or the loop never absorbs anything."""
    backend = _apply_over_scripted_reads(
        monkeypatch, tmp_path, logger, [_heads(False), _heads(False)])

    assert backend.applied is True
    assert backend.offs == ["HDMI-1"], "sanity: this IS the completed-apply path"


def test_apply_bail_does_not_freeze_change_detection(tmp_path, monkeypatch, logger):
    """BL-01 half 2 + WR-01: a bail must not absorb the hash and must not settle.

    This is the phantom-monitor regression in full. Pre-fix, ``_apply_if_changed``
    could not distinguish a bail from a completed apply, so it unconditionally
    froze ``settled = topology_hash(...)``. The head is still lit, so
    ``topology_hash_from_outputs`` keeps including it, the digest never changes
    again, and NO further apply fires until another physical event -- a phantom
    dwm monitor that sticks forever.
    """
    world = BounceWorld()
    backend = ScenarioBackend(world)
    coord = _SettleSpy()
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    # HDMI-1 unplugged but its CRTC STILL LIT -- precisely the state the scrub exists
    # to heal, and the state that poisons the digest if it is never healed.
    world.hpd("HDMI-1", False)

    def _fail_read_2(i):
        if i == 2:
            raise RuntimeError("transient X error mid-apply")
    _wire_apply_seams_scripted(monkeypatch, world, backend, _fail_read_2)
    monkeypatch.setattr(watch, "topology_hash",
                        lambda logger=None: topology_hash_from_outputs(world.outs))

    env, churn = _bounce_env(tmp_path), _churn()
    stale = "topology-hash-from-before-the-unplug"
    got = watch._apply_if_changed(env, logger, stale, churn, True, coord)

    # (a) the head really is left disconnected-but-lit by the bail
    assert world.outs["HDMI-1"].current_mode is not None
    assert ("output_off", "HDMI-1") not in world.ops
    # (b) ... so the loop MUST NOT absorb the hash, or nothing ever heals it
    assert got == stale, "a bailed apply must not freeze the topology hash"
    assert got != topology_hash_from_outputs(world.outs), (
        "absorbing here is the phantom-monitor bug: the digest would never move again")
    # WR-01: never run the mutating settle hook on an unconfirmed observation.
    assert coord.calls == 0, "on_settled must not run when apply_once bailed"

    # (c) and the retry that the un-absorbed hash guarantees DOES heal the head
    healed = watch._apply_if_changed(env, logger, got, churn, True, coord)
    assert ("output_off", "HDMI-1") in world.ops, "the retry powers the stale head off"
    assert world.outs["HDMI-1"].current_mode is None
    assert healed == topology_hash_from_outputs(world.outs), (
        "a COMPLETED apply over a healed topology is absorbed normally")
    assert coord.calls == 1, "the COMPLETED retry apply does settle"


def test_is_lit_is_the_one_liveness_definition(output_factory):
    """WR-03: production, the apply scrub, the digest and this harness agree.

    The edge predicate (relocate) and the scrub predicate (apply) are load-bearing
    AGAINST each other, so they must not be able to drift. Pin the property AND
    the fact that all four call sites route through it.
    """
    lit = output_factory("HDMI-1", connected=False, position=(1920, 0),
                         current_mode=(1600, 900))
    dark = output_factory("HDMI-1", connected=True)
    assert lit.is_lit is True, "disconnected-but-lit is LIT (the whole 14-08 defect)"
    assert dark.is_lit is False, "connected-but-dark is NOT lit (the black-monitor state)"

    # Half-populated is unreachable from randr_resources_to_outputs, but if a
    # future producer ever creates it, it must read as DARK (conservative).
    assert output_factory("X", position=(0, 0)).is_lit is False
    assert output_factory("X", current_mode=(800, 600)).is_lit is False

    # The harness model uses the production predicate, not a private copy.
    world = BounceWorld()
    world.outs["HDMI-1"].position = None
    world.outs["HDMI-1"].current_mode = None
    assert [o.name for o in world.lit()] == ["eDP-1"]
