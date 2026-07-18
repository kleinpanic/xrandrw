from __future__ import annotations
import logging
import os

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
def mock_x(monkeypatch, output_factory):
    """Mock every X/side-effect entry point so apply_once needs no live server.

    read_xrandr is set per-test via the returned setter; auto_pos calls are recorded
    as (connector, rel_opt, anchor) tuples in `calls`.
    """
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

    def set_outputs(names):
        outs = {n: output_factory(name=n, connected=True) for n in names}
        monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: outs)
        return outs

    return calls, set_outputs


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_apply")
    lg.setLevel(logging.DEBUG)
    return lg


def test_backend_select(tmp_path, mock_x, logger, caplog):
    calls, _set_outputs = mock_x
    assert isinstance(apply_mod.get_apply_backend({"APPLY_BACKEND": "subprocess"}), apply_mod.SubprocessBackend)
    assert isinstance(apply_mod.get_apply_backend({}), apply_mod.SubprocessBackend)
    nat = apply_mod.get_apply_backend({"APPLY_BACKEND": "native"})
    assert isinstance(nat, apply_mod.NativeRandRBackend)

    # native stub is warn-and-delegate: it must NOT perform a native apply — it logs an
    # apply_backend warning and delegates to the subprocess primitive (recorded via mock_x).
    with caplog.at_level(logging.WARNING, logger="xrandrw.test_apply"):
        nat.auto_pos("DP-2", "right-of", "DP-1", logger)
    assert calls == [("DP-2", "right-of", "DP-1")], "native stub must delegate to the subprocess op"
    warns = [r for r in caplog.records if getattr(r, "event", None) == "apply_backend"]
    assert warns and warns[0].levelno == logging.WARNING


def test_lock_open_refuses_symlink(tmp_path, mock_x, logger, caplog):
    calls, set_outputs = mock_x
    set_outputs(["DP-1", "DP-2"])
    env = _env(tmp_path)

    # HARD-02: pre-place a symlink at the apply-lock path (CWE-59 attack surface).
    os.symlink(tmp_path / "attacker-target", env["LOCKFILE"])

    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_apply"):
        apply_mod.apply_once(env, logger)

    assert calls == [], "apply_once must not run any placement when the lock path is a symlink"
    refusals = [r for r in caplog.records if getattr(r, "event", None) == "lock_symlink_refused"]
    assert refusals, "expected a lock_symlink_refused record"
    assert refusals[0].levelno == logging.WARNING


def test_lock_acquire_order(tmp_path, mock_x, logger, monkeypatch):
    calls, set_outputs = mock_x
    set_outputs(["DP-1", "DP-2"])
    env = _env(tmp_path)

    # Both the apply-lock and the state-lock funnel through os.open (via _open_lock_fd).
    # Patch os.open itself and record each opened path, filtering to the two lock paths.
    real_open = os.open
    order = []

    def spy_open(path, *args, **kwargs):
        p = str(path)
        if p in (env["LOCKFILE"], env["STATE_LOCKFILE"]):
            order.append(p)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    _isolate_state(monkeypatch)

    apply_mod.apply_once(env, logger)

    assert env["LOCKFILE"] in order and env["STATE_LOCKFILE"] in order
    # apply-lock OUTER, state-lock INNER (never the reverse).
    assert order.index(env["LOCKFILE"]) < order.index(env["STATE_LOCKFILE"])


def test_profile_override(tmp_path, mock_x, logger, layout_pi4, frozen_pi4_argv, monkeypatch):
    calls, set_outputs = mock_x
    set_outputs(["DSI-1", "HDMI-1"])
    env = _env(tmp_path)
    # A conf-loaded LAYOUT_* profile key surviving in env selects the device profile.
    env["LAYOUT_PI4"] = layout_pi4

    # mock_x stubs run to a no-op; replace it with a spy to capture the assembled argv.
    captured = []
    monkeypatch.setattr(apply_mod, "run", lambda argv, **k: captured.append(argv))
    # The profile-match path must early-return BEFORE the state-lock (D-03a): load_state must
    # never be reached on a match.
    monkeypatch.setattr(
        apply_mod, "load_state",
        lambda: (_ for _ in ()).throw(AssertionError("state must not be touched on profile match")),
    )

    apply_mod.apply_once(env, logger)

    # Byte-equivalence: the profile assembled EXACTLY the frozen Pi4 argv.
    assert captured == [frozen_pi4_argv]
    # No generic placement ran (early-return before the attach-stack policy).
    assert calls == []


def test_profile_subset_does_not_early_return(tmp_path, mock_x, logger, monkeypatch):
    # WR-05: a {DSI-1} profile must NOT fire when {DSI-1, HDMI-1} is connected — the
    # profile early-return would leave HDMI-1 unconfigured. Must fall through to placement.
    calls, set_outputs = mock_x
    set_outputs(["DSI-1", "HDMI-1"])
    env = _env(tmp_path)
    env["LAYOUT_SOLO"] = "DSI-1:800x480:primary:0x0"

    captured = []
    monkeypatch.setattr(apply_mod, "run", lambda argv, **k: captured.append(argv))
    _isolate_state(monkeypatch)

    apply_mod.apply_once(env, logger)

    # Generic path ran: DSI-1 primary (no internal), HDMI-1 placed relative to it.
    assert calls == [("HDMI-1", "right-of", "DSI-1")]
    # The profile argv (with --mode 800x480) was never assembled.
    assert all("800x480" not in argv for argv in captured)


def test_placement_chains_beyond_four(tmp_path, mock_x, logger, monkeypatch):
    calls, set_outputs = mock_x
    # DP-1 becomes primary (no internal); DP-2..DP-6 are 5 externals.
    set_outputs(["DP-1", "DP-2", "DP-3", "DP-4", "DP-5", "DP-6"])
    env = _env(tmp_path)
    _isolate_state(monkeypatch)

    apply_mod.apply_once(env, logger)

    # The 5th external must chain off the 4th external's connector, never the primary.
    placed = {connector: anchor for connector, _rel, anchor in calls}
    assert len(calls) == 5
    chained = [(c, a) for c, _r, a in calls if a != "DP-1"]
    assert len(chained) == 1, "exactly one external should chain off a non-primary anchor"
    chained_connector, chained_anchor = chained[0]
    assert chained_anchor != "DP-1"
    assert chained_anchor in placed, "chained anchor must be a previously-placed external connector"


def _isolate_state(monkeypatch):
    # Keep apply_once off the real ~/.local/share/xrandrw/state.json.
    monkeypatch.setattr(apply_mod, "load_state", lambda: {"profiles": {}, "identity_map": {}})
    monkeypatch.setattr(apply_mod, "save_state", lambda st: None)


def test_identical_edid_externals_both_placed(tmp_path, mock_x, logger, monkeypatch, output_factory):
    # WR-03: two externals with the same EDID collapse to ONE profile id; both connectors
    # must still get a placement (previously one landed overlapped at 0x0).
    calls, _set_outputs = mock_x
    outs = {
        "eDP-1": output_factory("eDP-1", connected=True),
        "DP-1": output_factory("DP-1", connected=True, edid_sha1="deadbeef"),
        "DP-2": output_factory("DP-2", connected=True, edid_sha1="deadbeef"),
    }
    monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: outs)
    _isolate_state(monkeypatch)

    apply_mod.apply_once(_env(tmp_path), logger)

    assert {c for c, _r, _a in calls} == {"DP-1", "DP-2"}, "both same-EDID heads must be placed"
    # No two placements may share the same (side, anchor) pair — that IS the overlap.
    pairs = [(r, a) for _c, r, a in calls]
    assert len(set(pairs)) == len(pairs)


def test_identical_edid_externals_no_internal(tmp_path, mock_x, logger, monkeypatch, output_factory):
    # WR-03: same collapse in the no-internal branch (DP-0 becomes primary).
    calls, _set_outputs = mock_x
    outs = {
        "DP-0": output_factory("DP-0", connected=True),
        "DP-1": output_factory("DP-1", connected=True, edid_sha1="deadbeef"),
        "DP-2": output_factory("DP-2", connected=True, edid_sha1="deadbeef"),
    }
    monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: outs)
    _isolate_state(monkeypatch)

    apply_mod.apply_once(_env(tmp_path), logger)

    assert {c for c, _r, _a in calls} == {"DP-1", "DP-2"}
    pairs = [(r, a) for _c, r, a in calls]
    assert len(set(pairs)) == len(pairs)


def test_reread_failure_logged_not_propagated(tmp_path, mock_x, logger, caplog, monkeypatch):
    # WR-01: a transient X error on the SECOND read must not escape apply_once
    calls, set_outputs = mock_x
    outs = set_outputs(["DP-1", "DP-2"])
    env = _env(tmp_path)

    reads = {"n": 0}

    def flaky_read(logger):
        reads["n"] += 1
        if reads["n"] >= 2:
            raise RuntimeError("transient X error")
        return outs
    monkeypatch.setattr(apply_mod, "read_xrandr", flaky_read)

    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_apply"):
        apply_mod.apply_once(env, logger)  # must return, not raise

    assert reads["n"] == 2
    assert calls == [], "no placement may run after a failed reread"
    errs = [r for r in caplog.records if getattr(r, "event", None) == "xrandr_unavail"]
    assert errs and errs[0].levelno == logging.ERROR


def test_scrub_stale_powers_off_lingering_head(output_factory, logger):
    # Disconnected-but-lit head (the reported bug's state) must get an output_off
    outs = {
        "DSI-1": output_factory("DSI-1", connected=True, current_mode=(800, 480)),
        "HDMI-1": output_factory("HDMI-1", connected=False, current_mode=(1600, 900)),
        "HDMI-2": output_factory("HDMI-2", connected=False, current_mode=None),
    }
    offs = []

    class FakeBackend:
        def output_off(self, connector, logger):
            offs.append(connector)

    apply_mod.scrub_stale(outs, logger, FakeBackend())

    assert sorted(offs) == ["HDMI-1", "HDMI-2"]
    assert "DSI-1" not in offs
