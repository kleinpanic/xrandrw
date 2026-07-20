"""TEST-07: apply.py branch coverage — primitives, backends, and apply_once edges.

Covers the low-level xrandr_* primitives (otherwise always mocked away), both
apply backends' delegation, and the apply_once early-exit branches (symlink-refuse
non-ELOOP re-raise, apply-skip, first-read failure, no-connected), plus the
place-chain internal-primary branch and the _sd_notify/_watchdog_thread helpers.
Behavior-level assertions only (log events + recorded argv), no src changes.
"""
from __future__ import annotations

import errno
import logging
import socket
import threading

import pytest

import xrandrw.apply as apply_mod


def _env(tmp_path):
    return {
        "LOCKFILE": str(tmp_path / "xrandrw.lock"),
        "STATE_LOCKFILE": str(tmp_path / "xrandrw.state.lock"),
        "PREF_DEFAULT_SIDE": "right-of",
        "HIDPI_WIDTH": "3840",
        "WALL": str(tmp_path / "wall.png"),
        "USE_XWALLPAPER": "0",
        "APPLY_BACKEND": "subprocess",
    }


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_apply_branches")
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture
def mock_x(monkeypatch, output_factory):
    # Same seam as tests/test_apply.py: stub every X/side-effect entry point; auto_pos
    # calls are recorded as (connector, rel_opt, anchor).
    calls = []
    monkeypatch.setattr(apply_mod, "wait_for_x", lambda logger: None)
    monkeypatch.setattr(apply_mod, "read_edids", lambda outs, logger: None)
    monkeypatch.setattr(apply_mod, "scrub_stale", lambda outs, logger, backend=None: None)
    monkeypatch.setattr(apply_mod, "reapply_wallpaper", lambda env, logger: None)
    monkeypatch.setattr(apply_mod, "xrandr_auto_primary_scale", lambda c, s, logger: None)
    monkeypatch.setattr(apply_mod, "xrandr_rotate_left_if_portrait", lambda c, o, logger: None)
    monkeypatch.setattr(apply_mod, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        apply_mod, "xrandr_auto_pos",
        lambda connector, rel_opt, anchor, logger: calls.append((connector, rel_opt, anchor)),
    )

    def set_outputs(specs):
        # specs: iterable of (name, connected) or plain names (assumed connected)
        outs = {}
        for s in specs:
            if isinstance(s, tuple):
                name, connected = s
            else:
                name, connected = s, True
            outs[name] = output_factory(name=name, connected=connected)
        monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: outs)
        return outs

    return calls, set_outputs


def _isolate_state(monkeypatch):
    monkeypatch.setattr(apply_mod, "load_state", lambda: {"profiles": {}, "identity_map": {}})
    monkeypatch.setattr(apply_mod, "save_state", lambda st: None)


# ---------------- xrandr_* primitives (always mocked elsewhere) ----------------

def test_primitive_output_off_builds_argv(monkeypatch, logger):
    argvs = []
    monkeypatch.setattr(apply_mod, "run", lambda cmd, **k: argvs.append(cmd))
    apply_mod.xrandr_output_off("HDMI-1", logger)
    assert argvs == [["xrandr", "--output", "HDMI-1", "--off"]]


def test_primitive_primary_scale_builds_argv(monkeypatch, logger):
    argvs = []
    monkeypatch.setattr(apply_mod, "run", lambda cmd, **k: argvs.append(cmd))
    apply_mod.xrandr_auto_primary_scale("DSI-1", "0.5x0.5", logger)
    assert argvs == [["xrandr", "--output", "DSI-1", "--auto", "--scale", "0.5x0.5",
                      "--panning", "0x0", "--primary"]]


def test_primitive_auto_pos_builds_argv(monkeypatch, logger):
    argvs = []
    monkeypatch.setattr(apply_mod, "run", lambda cmd, **k: argvs.append(cmd))
    apply_mod.xrandr_auto_pos("DP-2", "right-of", "DP-1", logger)
    assert argvs == [["xrandr", "--output", "DP-2", "--auto", "--scale", "1x1",
                      "--panning", "0x0", "--right-of", "DP-1"]]


def test_primitive_rotate_only_when_portrait(monkeypatch, logger, output_factory):
    argvs = []
    monkeypatch.setattr(apply_mod, "run", lambda cmd, **k: argvs.append(cmd))
    portrait = output_factory("DP-3", connected=True, current_mode=(1080, 1920))
    apply_mod.xrandr_rotate_left_if_portrait("DP-3", portrait, logger)
    assert argvs == [["xrandr", "--output", "DP-3", "--rotate", "left"]]

    argvs.clear()
    landscape = output_factory("DP-4", connected=True, current_mode=(1920, 1080))
    apply_mod.xrandr_rotate_left_if_portrait("DP-4", landscape, logger)
    assert argvs == [], "landscape output is never rotated"

    argvs.clear()
    no_mode = output_factory("DP-5", connected=True, current_mode=None)
    apply_mod.xrandr_rotate_left_if_portrait("DP-5", no_mode, logger)
    assert argvs == [], "no current/preferred mode => no rotate"


# ---------------- backend delegation ----------------

def test_subprocess_backend_delegates_all_ops(monkeypatch, logger, output_factory):
    seen = []
    monkeypatch.setattr(apply_mod, "xrandr_output_off", lambda c, logger: seen.append(("off", c)))
    monkeypatch.setattr(apply_mod, "xrandr_auto_primary_scale", lambda c, s, logger: seen.append(("scale", c, s)))
    monkeypatch.setattr(apply_mod, "xrandr_auto_pos", lambda c, r, a, logger: seen.append(("pos", c, r, a)))
    monkeypatch.setattr(apply_mod, "xrandr_rotate_left_if_portrait", lambda c, o, logger: seen.append(("rot", c)))
    b = apply_mod.SubprocessBackend()
    b.output_off("HDMI-1", logger)
    b.primary_scale("DSI-1", "1x1", logger)
    b.auto_pos("DP-2", "left-of", "DP-1", logger)
    b.rotate_left_if_portrait("DP-3", output_factory("DP-3"), logger)
    assert seen == [("off", "HDMI-1"), ("scale", "DSI-1", "1x1"),
                    ("pos", "DP-2", "left-of", "DP-1"), ("rot", "DP-3")]


def test_native_backend_warns_and_delegates_all_ops(monkeypatch, logger, output_factory, caplog):
    seen = []
    monkeypatch.setattr(apply_mod, "xrandr_output_off", lambda c, logger: seen.append(("off", c)))
    monkeypatch.setattr(apply_mod, "xrandr_auto_primary_scale", lambda c, s, logger: seen.append(("scale", c)))
    monkeypatch.setattr(apply_mod, "xrandr_auto_pos", lambda c, r, a, logger: seen.append(("pos", c)))
    monkeypatch.setattr(apply_mod, "xrandr_rotate_left_if_portrait", lambda c, o, logger: seen.append(("rot", c)))
    nat = apply_mod.NativeRandRBackend()
    with caplog.at_level(logging.WARNING, logger="xrandrw.test_apply_branches"):
        nat.output_off("HDMI-1", logger)
        nat.primary_scale("DSI-1", "1x1", logger)
        nat.auto_pos("DP-2", "right-of", "DP-1", logger)
        nat.rotate_left_if_portrait("DP-3", output_factory("DP-3"), logger)
    assert [x[0] for x in seen] == ["off", "scale", "pos", "rot"]
    warns = [r for r in caplog.records if getattr(r, "event", None) == "apply_backend"]
    assert len(warns) == 4 and all(r.levelno == logging.WARNING for r in warns)


def test_get_apply_backend_selects_native(monkeypatch):
    assert isinstance(apply_mod.get_apply_backend({"APPLY_BACKEND": "native"}),
                      apply_mod.NativeRandRBackend)


def test_reapply_wallpaper_delegates(monkeypatch, logger):
    seen = []
    monkeypatch.setattr(apply_mod, "apply_wallpaper", lambda env, logger: seen.append(env))
    apply_mod.reapply_wallpaper({"WALL": "x"}, logger)
    assert seen == [{"WALL": "x"}]


# ---------------- apply_once early-exit branches ----------------

def test_apply_once_reraises_non_eloop_lock_error(monkeypatch, logger, tmp_path):
    # A non-ELOOP OSError from opening the apply-lock is NOT a symlink refusal; it
    # must propagate rather than being silently treated as a symlink case.
    def boom(path):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(apply_mod, "_open_lock_fd", boom)
    with pytest.raises(OSError) as ei:
        apply_mod.apply_once(_env(tmp_path), logger)
    assert ei.value.errno == errno.EACCES


def test_apply_once_skips_when_another_apply_holds_lock(monkeypatch, mock_x, logger, tmp_path, caplog):
    calls, set_outputs = mock_x
    set_outputs(["DP-1", "DP-2"])

    def busy(fd, op):
        raise OSError(errno.EWOULDBLOCK, "locked")

    monkeypatch.setattr(apply_mod.fcntl, "flock", busy)
    with caplog.at_level(logging.INFO, logger="xrandrw.test_apply_branches"):
        apply_mod.apply_once(_env(tmp_path), logger)
    assert calls == [], "no placement runs while another apply holds the lock"
    assert any(getattr(r, "event", None) == "apply_skip" for r in caplog.records)


def test_apply_once_first_read_failure_logged_not_propagated(monkeypatch, mock_x, logger, tmp_path, caplog):
    calls, _set = mock_x

    def boom(logger):
        raise RuntimeError("xrandr gone")

    monkeypatch.setattr(apply_mod, "read_xrandr", boom)
    with caplog.at_level(logging.ERROR, logger="xrandrw.test_apply_branches"):
        apply_mod.apply_once(_env(tmp_path), logger)  # must return, not raise
    assert calls == []
    assert any(getattr(r, "event", None) == "xrandr_unavail" for r in caplog.records)


def test_apply_once_no_connected_reapplies_wallpaper_and_returns(monkeypatch, mock_x, logger, tmp_path, caplog):
    calls, set_outputs = mock_x
    set_outputs([("HDMI-1", False), ("HDMI-2", False)])  # all disconnected
    walls = []
    monkeypatch.setattr(apply_mod, "reapply_wallpaper", lambda env, logger: walls.append(True))

    with caplog.at_level(logging.INFO, logger="xrandrw.test_apply_branches"):
        apply_mod.apply_once(_env(tmp_path), logger)

    assert calls == [], "no placements when nothing is connected"
    assert walls == [True], "wallpaper is reapplied even with no connected outputs"
    events = {getattr(r, "event", None) for r in caplog.records}
    assert "apply_none" in events and "apply_done" in events


def test_apply_once_no_internal_picks_lexicographically_first_primary(monkeypatch, mock_x, logger, tmp_path, caplog):
    # No internal LCD => the lexicographically-first connector becomes primary and the
    # rest are placed relative to it.
    calls, set_outputs = mock_x
    set_outputs(["DP-9", "DP-1", "DP-5"])  # first == DP-1
    _isolate_state(monkeypatch)
    primaries = []
    monkeypatch.setattr(apply_mod, "run", lambda cmd, **k: primaries.append(cmd))

    with caplog.at_level(logging.INFO, logger="xrandrw.test_apply_branches"):
        apply_mod.apply_once(_env(tmp_path), logger)

    assert any(cmd[:5] == ["xrandr", "--output", "DP-1", "--auto", "--primary"] for cmd in primaries)
    assert {c for c, _r, _a in calls} == {"DP-5", "DP-9"}


def test_apply_once_internal_primary_chains_beyond_four(monkeypatch, mock_x, logger, tmp_path, caplog):
    # Internal DSI-1 primary + 5 externals: the 5th chains off a placed external
    # (place_chain), not the primary — exercising the internal-branch chain log.
    calls, set_outputs = mock_x
    set_outputs(["DSI-1", "DP-1", "DP-2", "DP-3", "DP-4", "DP-5"])
    _isolate_state(monkeypatch)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_apply_branches"):
        apply_mod.apply_once(_env(tmp_path), logger)

    assert len(calls) == 5, "all five externals placed"
    chained = [r for r in caplog.records if getattr(r, "event", None) == "place_chain"]
    assert chained, "the 5th external chains off a non-primary anchor (place_chain)"


# ---------------- _sd_notify / _watchdog_thread ----------------

def test_sd_notify_noop_without_socket_env(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    # No socket configured => silent no-op (must not raise).
    apply_mod._sd_notify("READY=1")


def test_sd_notify_sends_to_unix_socket(monkeypatch, tmp_path):
    sock_path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    srv.settimeout(2.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        apply_mod._sd_notify("READY=1")
        data, _ = srv.recvfrom(64)
        assert data == b"READY=1"
    finally:
        srv.close()


def test_sd_notify_abstract_socket_prefix(monkeypatch, tmp_path):
    # A leading '@' selects the Linux abstract namespace (translated to a NUL byte).
    sent = {}

    class FakeSock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def connect(self, addr):
            sent["addr"] = addr

        def send(self, data):
            sent["data"] = data

    monkeypatch.setenv("NOTIFY_SOCKET", "@app/notify")
    monkeypatch.setattr(apply_mod.socket, "socket", lambda *a, **k: FakeSock())
    apply_mod._sd_notify("WATCHDOG=1")
    assert sent["addr"].startswith("\0"), "abstract-namespace addr begins with NUL"
    assert sent["data"] == b"WATCHDOG=1"


def test_watchdog_thread_noop_without_usec(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    evt = threading.Event()
    # No WATCHDOG_USEC => returns immediately without pinging.
    apply_mod._watchdog_thread(evt, logging.getLogger("xrandrw.test_apply_branches"))


def test_watchdog_thread_pings_once_then_stops(monkeypatch, logger):
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")
    pings = []
    monkeypatch.setattr(apply_mod, "_sd_notify", lambda msg: pings.append(msg))

    class OneShotEvent:
        # wait() returns False once (run the loop body), then True (stop).
        def __init__(self):
            self.n = 0

        def wait(self, timeout=None):
            self.n += 1
            return self.n > 1

    apply_mod._watchdog_thread(OneShotEvent(), logger)
    assert pings == ["WATCHDOG=1"], "exactly one watchdog ping before the stop event"
