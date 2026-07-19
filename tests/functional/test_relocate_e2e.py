"""L1 real-dwm/X E2E: control+capture + injected record/restore (TEST-05).

Every test here drives the REAL ``xrandrw.dwmipc`` verbs and the REAL
``RelocationControl`` / ``RelocationCoordinator`` against a REAL patched dwm on a
private socket (``dwm_ipc`` fixture) with REAL ``xterm`` windows on two real
Xinerama monitors -- zero mocked dwm, zero mocked Xlib. See ``README.md`` for the
honest boundary: L1 proves control+capture; the record->restore path is proven by
INJECTION (Xvfb/Xephyr outputs never flip ``connected``); the TRUE unplug->replug
chain is proven only by the live L3 HDMI verify (plan 14-05).
"""
from __future__ import annotations

import copy
import logging
import subprocess
import time

import pytest

from xrandrw import dwmipc
from xrandrw.relocate import RelocationCoordinator, _selected_confirmed
from xrandrw.windows import WindowRecord, WindowXReader, resolve_pid
from xrandrw.xrandr import Output

from conftest import HARNESS_MONITORS  # session fixtures live in the same dir

pytestmark = pytest.mark.functional

_LOG = logging.getLogger("xrandrw.functional-test")

# Connector names the injection tests map onto the two harness Xinerama monitors.
_LEFT = "eDP-HARNESS"    # dwm monitor 0 @ (0,0)
_RIGHT = "HDMI-HARNESS"  # dwm monitor 1 @ (1920,0)


# --- helpers (deterministic polling; mirror probe_003_live) ------------------

def _client_xids(sock: str) -> set[int]:
    xids: set[int] = set()
    for m in dwmipc.get_monitors(path=sock):
        clients = m.get("clients") or {}
        for w in clients.get("all") or []:
            xids.add(w)
    return xids


def _spawn_xterm(sock: str):
    """Spawn a throwaway xterm and return ``(proc, xid)`` once dwm manages it."""
    before = _client_xids(sock)
    proc = subprocess.Popen(["xterm", "-e", "sleep", "600"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    xid = None
    for _ in range(80):
        new = _client_xids(sock) - before
        if new:
            xid = sorted(new)[0]
            break
        time.sleep(0.1)  # poll-backoff only
    if xid is None:
        proc.terminate()
        pytest.fail("spawned xterm never appeared in the dwm client list")
    return proc, xid


def _focus(sock: str, xid: int, ctl) -> None:
    """Focus ``xid`` via the real _NET_ACTIVE_WINDOW seam and poll until selected."""
    ctl.focus(xid)
    for _ in range(25):
        if _selected_confirmed(dwmipc.get_monitors(path=sock), xid):
            return
        time.sleep(0.03)  # poll-backoff only


def _wait_client(sock: str, xid: int, predicate, tries: int = 40):
    """Poll GET_DWM_CLIENT(xid) until ``predicate(client)`` holds; return client."""
    client = dwmipc.get_dwm_client(xid, path=sock)
    for _ in range(tries):
        if predicate(client):
            return client
        time.sleep(0.05)  # poll-backoff only
        client = dwmipc.get_dwm_client(xid, path=sock)
    return client


def _injected_outs(left_connected: bool = True, right_connected: bool = True):
    """Two Outputs whose position+mode match the two harness dwm monitors."""
    return {
        _LEFT: Output(_LEFT, connected=left_connected,
                      current_mode=(HARNESS_MONITORS[0]["width"], HARNESS_MONITORS[0]["height"]),
                      position=(HARNESS_MONITORS[0]["x"], HARNESS_MONITORS[0]["y"])),
        _RIGHT: Output(_RIGHT, connected=right_connected,
                       current_mode=(HARNESS_MONITORS[1]["width"], HARNESS_MONITORS[1]["height"]),
                       position=(HARNESS_MONITORS[1]["x"], HARNESS_MONITORS[1]["y"])),
    }


# --- L1 tests ----------------------------------------------------------------

def test_control_and_capture_against_real_dwm(dwm_ipc):
    """focus-then-act + real get_dwm_client capture + cross-monitor tagmon.

    This is the layer that would have auto-caught the Phase-10 focus race: it
    proves each dwm-ipc verb lands on the focused client, that capture reflects
    reality, and that a real ``tagmon`` moves a client between two real monitors.
    """
    from xrandrw.relocate import RelocationControl
    sock = dwm_ipc
    ctl = RelocationControl()
    monitors = dwmipc.get_monitors(path=sock)
    assert len(monitors) >= 2, "harness must present dwm >= 2 monitors"

    proc, xid = _spawn_xterm(sock)
    try:
        c0 = dwmipc.get_dwm_client(xid, path=sock)
        assert c0["monitor_number"] == 0
        base_floating = bool(c0["states"]["is_floating"])

        # 1. togglefloating flips is_floating (focus-then-act).
        _focus(sock, xid, ctl)
        dwmipc.run_command("togglefloating", path=sock)
        c1 = _wait_client(sock, xid,
                          lambda c: bool(c["states"]["is_floating"]) != base_floating)
        assert bool(c1["states"]["is_floating"]) != base_floating

        # 2. ConfigureWindow honors an absolute floating geometry. Done while the
        # window is still on monitor 0 (origin x=0): dwm's configurerequest is
        # monitor-RELATIVE (c->x = m->mx + ev->x), so absolute==relative only on
        # the origin-0 monitor (see the cross-monitor finding in test 2 / SUMMARY).
        _focus(sock, xid, ctl)
        want = {"x": 200, "y": 150, "width": 700, "height": 480}
        assert ctl.configure_geometry(xid, want) is True
        c2 = _wait_client(
            sock, xid,
            lambda c: abs(c["geometry"]["current"]["x"] - want["x"]) <= 4
            and abs(c["geometry"]["current"]["y"] - want["y"]) <= 4)
        got = c2["geometry"]["current"]
        assert abs(got["x"] - want["x"]) <= 4 and abs(got["y"] - want["y"]) <= 4
        # w/h may snap to xterm character cells; allow a one-cell tolerance.
        assert abs(got["width"] - want["width"]) <= 30
        assert abs(got["height"] - want["height"]) <= 30

        # 3. tag changes the client's tag bitmask.
        _focus(sock, xid, ctl)
        dwmipc.run_command("tag", 2, path=sock)
        c3 = _wait_client(sock, xid, lambda c: c["tags"] == 2)
        assert c3["tags"] == 2
        _focus(sock, xid, ctl)
        dwmipc.run_command("tag", int(c0["tags"]), path=sock)  # restore

        # 4. real cross-monitor tagmon moves the client between the two monitors.
        _focus(sock, xid, ctl)
        dwmipc.run_command("tagmon", 1, path=sock)
        c4 = _wait_client(sock, xid, lambda c: c["monitor_number"] == 1)
        assert c4["monitor_number"] == 1, "tagmon did not cross monitors"
    finally:
        proc.terminate()

    # 5. dwm crash-safety: survived every control op.
    assert dwmipc.available(sock) is True


def test_coordinator_record_then_restore_injection(dwm_ipc):
    """RelocationCoordinator record->restore against real dwm, sets INJECTED.

    Xvfb/Xephyr outputs never flip ``connected``, so we drive the coordinator's
    real record/restore verbs (focus-then-act tagmon/tag/togglefloating/configure)
    by INJECTING the displaced record + a crafted ``outs`` topology, then assert
    the REAL dwm client landed where the record says via get_dwm_client.
    """
    sock = dwm_ipc
    # An anchor window keeps the SOURCE monitor non-empty so the coordinator's
    # crash-safety gate (_tagmon_would_crash_dwm -- refuses a tagmon that would
    # empty the source AND leave a monitor at n==1, SIGSEGVing single-window-center
    # dwm builds) permits the cross-monitor restore. This mirrors a real
    # evacuation, where the surviving monitor holds the user's other windows.
    anchor_proc, _anchor_xid = _spawn_xterm(sock)
    proc, xid = _spawn_xterm(sock)
    try:
        # Real local identity of the spawned xterm.
        identity = resolve_pid(xid, WindowXReader(), proc_root="/proc", logger=_LOG)
        assert identity is not None, "could not resolve the xterm's local pid identity"
        pid, starttime = identity[0], identity[1]

        # The "good" placement to restore to: floating, on the RIGHT monitor (1),
        # tag 1 (which monitor 1 views by default -> stays visible so focus lands).
        rec = WindowRecord(
            xid=xid, pid=pid, starttime=starttime, comm=identity[2], cmdline=identity[3],
            output=_RIGHT, edid=None, monitor_number=1, tags=1,
            is_floating=True, is_fullscreen=False,
            geometry={"x": 2015, "y": 145, "width": 700, "height": 480})

        outs = _injected_outs()
        coord = RelocationCoordinator(sock_path=sock, config_enabled=True, ipc_timeout=2.0)

        # --- record: a snapshot entry on the removed output becomes displaced ----
        coord._snapshot = {(pid, starttime): rec}
        coord._record_displaced({_RIGHT}, _LOG)
        assert (pid, starttime) in coord._displaced

        # --- restore: the output "returns" -> put the window back ---------------
        coord._restore_returned({_RIGHT}, outs, _LOG)

        client = _wait_client(sock, xid, lambda c: c["monitor_number"] == 1)
        assert client["monitor_number"] == 1, "coordinator did not tagmon to target monitor"
        assert bool(client["states"]["is_floating"]) is True
        assert client["tags"] == 1
        geo = client["geometry"]["current"]
        # CROSS-MONITOR GEOMETRY CORRECTNESS (regression guard for the bug this L1
        # harness surfaced): against real dwm, configurerequest is monitor-RELATIVE
        # (c->x = c->mon->mx + ev->x), and the RIGHT monitor's origin is x=1920. The
        # coordinator now converts the captured ABSOLUTE geometry (x=2015) to
        # target-monitor-relative (2015-1920=95) before ConfigureWindow, so dwm
        # recomputes c->x = 1920+95 = 2015 and the floating window lands back at its
        # saved ABSOLUTE position on the non-origin monitor. Pre-fix it sent absolute
        # 2015, dwm double-shifted to 1920+2015 -> overflow-centered on monitor 1
        # (wrong). Both axes are now asserted (y origin is 0, x origin is 1920).
        assert abs(geo["x"] - rec.geometry["x"]) <= 6, (
            f"cross-monitor floating x wrong: got {geo['x']}, want ~{rec.geometry['x']} "
            f"(monitor-relative transform regression)")
        assert abs(geo["y"] - rec.geometry["y"]) <= 6
        # restored records are dropped from the displaced map.
        assert (pid, starttime) not in coord._displaced
    finally:
        proc.terminate()
        anchor_proc.terminate()

    assert dwmipc.available(sock) is True


def test_coordinator_on_settled_unplug_replug_injection(dwm_ipc):
    """Full public-entry cycle: seed -> unplug(record) -> replug(restore).

    Exercises ``on_settled`` end-to-end through a stub reader whose ``read()``
    flips an output's ``connected`` (the ONLY thing Xvfb/Xephyr cannot do for
    real), while everything else -- dwm, the socket, focus, tagmon, capture --
    is real. Proves the record/restore state machine, not just its leaves.
    """
    sock = dwm_ipc
    from xrandrw.relocate import RelocationControl

    class StubReader:
        def __init__(self, outs):
            self.outs = outs

        def read(self, logger=None):
            return {k: copy.copy(v) for k, v in self.outs.items()}

    ctl = RelocationControl()
    # Anchor on the LEFT monitor so the crash-safety gate permits the restore hop
    # back (source monitor never emptied); mirrors a real evacuation target.
    anchor_proc, _anchor_xid = _spawn_xterm(sock)
    proc, xid = _spawn_xterm(sock)
    try:
        # Move the xterm onto the RIGHT monitor (its "home" before the unplug).
        # The anchor stays on the LEFT (monitor 0).
        _focus(sock, xid, ctl)
        dwmipc.run_command("tagmon", 1, path=sock)
        _wait_client(sock, xid, lambda c: c["monitor_number"] == 1)

        outs = _injected_outs()
        coord = RelocationCoordinator(sock_path=sock, reader=StubReader(outs),
                                      config_enabled=True, ipc_timeout=2.0)

        # 1. seed the steady-state baseline (both outputs connected).
        coord.on_settled(env={}, logger=_LOG)
        assert coord._prev_present == {_LEFT, _RIGHT}
        assert coord._snapshot, "seed capture found no windows on the real socket"

        # 2. unplug the RIGHT output -> the window's record becomes displaced.
        outs[_RIGHT].connected = False
        coord.on_settled(env={}, logger=_LOG)
        assert any(r.output == _RIGHT for r in coord._displaced.values()), \
            "unplug did not record the displaced window"
        # simulate dwm's evacuation of the removed monitor's client to monitor 0.
        _focus(sock, xid, ctl)
        dwmipc.run_command("tagmon", 1, path=sock)  # relative hop back to monitor 0
        _wait_client(sock, xid, lambda c: c["monitor_number"] == 0)

        # 3. replug the RIGHT output -> the window is restored to monitor 1.
        outs[_RIGHT].connected = True
        coord.on_settled(env={}, logger=_LOG)
        client = _wait_client(sock, xid, lambda c: c["monitor_number"] == 1)
        assert client["monitor_number"] == 1, "replug did not restore the window's monitor"
    finally:
        proc.terminate()
        anchor_proc.terminate()

    assert dwmipc.available(sock) is True


def test_functional_floor(request):
    """M2 no-vacuous-green guard: on CI, > 0 functional tests must be collected.

    A gating job that silently collects zero functional tests (bad marker, broken
    conftest, wrong path) would otherwise report green. On CI that is a FAILURE.
    """
    import os
    collected = getattr(request.config, "_xrw_functional_collected", 0)
    if os.environ.get("GITHUB_ACTIONS"):
        assert collected > 0, "no @pytest.mark.functional tests were collected on CI"
    else:
        assert collected >= 0  # locally informational only
