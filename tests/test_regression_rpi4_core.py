"""REG-01 core-daemon RPi4-shape proof: the display path works with WM code gated off.

Proves the CORE display path (LAYOUT_PI4 device profile, generic internal-primary
placement, TOUCH_MAP touch remap, wallpaper reapply) still works with the
window-mgmt code present-but-gated -- the Raspberry Pi 4 (DSI-1/HDMI-1, ft5x06)
shape explicitly. Every ``apply_once`` here touches ZERO dwm-ipc: ``apply.py`` does
not import the window-mgmt subsystem at all, and a regression that wired dwm-ipc
into the apply path would surface as a failing structural-independence assertion.

This is a UNIT/regression matrix over the existing mocked seams (mirrors
tests/test_apply.py's ``mock_x`` fixture + ``_env`` helper), NOT the live
functional harness (plan 14-01). Assertions are behavior-level (argv / placement
tuples / call counts) so the suite stays green before AND after plan 14-03's
behavior-preserving ``_place_externals`` extraction and ruff autofix.

TEST-ONLY: no src/ change.
"""
from __future__ import annotations

import logging

import pytest

import xrandrw.apply as apply_mod
from xrandrw.touch import resolve_touch_remaps


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
    """Mock every X/side-effect seam so apply_once needs no live server.

    Mirrors tests/test_apply.py's fixture: read_xrandr is set per-test via the
    returned setter; auto_pos calls are recorded as (connector, rel_opt, anchor).
    """
    calls = []

    monkeypatch.setattr(apply_mod, "wait_for_x", lambda logger: None)
    monkeypatch.setattr(apply_mod, "read_edids", lambda outs, logger: None)
    monkeypatch.setattr(apply_mod, "scrub_stale", lambda outs, logger, backend=None: None)
    monkeypatch.setattr(apply_mod, "reapply_wallpaper", lambda env, logger: None)
    monkeypatch.setattr(apply_mod, "xrandr_auto_primary_scale", lambda c, s, logger: None)
    monkeypatch.setattr(apply_mod, "xrandr_rotate_left_if_portrait", lambda c, o, logger: None)
    monkeypatch.setattr(apply_mod, "run", lambda *a, **k: None)
    monkeypatch.setattr(apply_mod, "remap_touch", lambda env, connected, logger: None)
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
    lg = logging.getLogger("xrandrw.test_regression_rpi4_core")
    lg.setLevel(logging.DEBUG)
    return lg


def _isolate_state(monkeypatch):
    # Keep apply_once off the real ~/.local/share/xrandrw/state.json.
    monkeypatch.setattr(apply_mod, "load_state", lambda: {"profiles": {}, "identity_map": {}})
    monkeypatch.setattr(apply_mod, "save_state", lambda st, path=None: None)


def test_layout_pi4_frozen_argv_with_wm_code_present(
        tmp_path, mock_x, logger, layout_pi4, frozen_pi4_argv, monkeypatch):
    # LAYOUT_PI4 (DSI-1/HDMI-1) assembles the frozen byte-equivalent xrandr argv,
    # with the window-mgmt code merely present. Reuses the Phase-5 byte-equivalence
    # style (frozen_pi4_argv from conftest).
    calls, set_outputs = mock_x
    set_outputs(["DSI-1", "HDMI-1"])
    env = _env(tmp_path)
    env["LAYOUT_PI4"] = layout_pi4

    captured = []
    monkeypatch.setattr(apply_mod, "run", lambda argv, **k: captured.append(argv))
    # The profile match must early-return BEFORE the state-lock path.
    monkeypatch.setattr(
        apply_mod, "load_state",
        lambda: (_ for _ in ()).throw(AssertionError("state must not be touched on profile match")),
    )

    apply_mod.apply_once(env, logger)

    assert captured == [frozen_pi4_argv], "LAYOUT_PI4 must assemble the frozen Pi4 argv"
    assert calls == [], "generic placement must not run on a profile match"


def test_apply_once_is_dwm_ipc_independent():
    # apply.py imports NO dwm-ipc: the core display path is structurally independent
    # of the window-mgmt subsystem. A regression that wired dwm-ipc into apply would
    # surface here (apply_mod would grow a `dwmipc` attribute).
    assert not hasattr(apply_mod, "dwmipc"), \
        "apply.py must not import dwm-ipc -- the core path stays WM-subsystem-independent"


def test_touch_map_ft5x06_dsi1_remap_fires_on_pi4_profile(
        tmp_path, mock_x, logger, layout_pi4, monkeypatch):
    # TOUCH_MAP ft5x06:DSI-1 remap fires for DSI-1 on the LAYOUT_PI4 profile apply:
    # remap_touch is invoked with a connected set containing "DSI-1".
    calls, set_outputs = mock_x
    set_outputs(["DSI-1", "HDMI-1"])
    env = _env(tmp_path)
    env["LAYOUT_PI4"] = layout_pi4
    env["TOUCH_MAP"] = "ft5x06:DSI-1"

    seen = []
    monkeypatch.setattr(apply_mod, "remap_touch",
                        lambda env, connected, logger: seen.append(set(connected)))

    apply_mod.apply_once(env, logger)

    assert seen, "remap_touch must be called on the profile apply"
    assert "DSI-1" in seen[0], "the connected set handed to touch remap must include DSI-1"

    # Pure-layer pin: ft5x06 substring resolves to DSI-1 when DSI-1 is connected.
    assert resolve_touch_remaps(
        [("ft5x06", "DSI-1")],
        [(8, "generic ft5x06 10-0038")],
        {"DSI-1"},
    ) == [(8, "DSI-1")]


def test_wallpaper_reapplies_on_profile_and_generic_paths(
        tmp_path, mock_x, logger, layout_pi4, monkeypatch):
    # Wallpaper reapplies once on the LAYOUT_PI4 profile apply AND once on a generic
    # (non-profile) internal-primary apply.
    calls, set_outputs = mock_x

    reapplied = {"n": 0}
    monkeypatch.setattr(apply_mod, "reapply_wallpaper",
                        lambda env, logger: reapplied.__setitem__("n", reapplied["n"] + 1))

    # (a) profile path
    set_outputs(["DSI-1", "HDMI-1"])
    env = _env(tmp_path)
    env["LAYOUT_PI4"] = layout_pi4
    apply_mod.apply_once(env, logger)
    assert reapplied["n"] == 1, "wallpaper must reapply on the profile apply"

    # (b) generic internal-primary path (no LAYOUT_* key)
    set_outputs(["DSI-1", "HDMI-1"])
    _isolate_state(monkeypatch)
    apply_mod.apply_once(_env(tmp_path), logger)
    assert reapplied["n"] == 2, "wallpaper must reapply on the generic apply"


def test_generic_internal_primary_placement_pi4_shape(tmp_path, mock_x, logger, monkeypatch):
    # Vanilla RPi4-style layout with the window-mgmt code merely present: DSI-1 is the
    # internal primary and HDMI-1 lands right-of it. No LAYOUT_* key -> generic policy.
    calls, set_outputs = mock_x
    set_outputs(["DSI-1", "HDMI-1"])
    _isolate_state(monkeypatch)

    apply_mod.apply_once(_env(tmp_path), logger)

    assert calls == [("HDMI-1", "right-of", "DSI-1")], \
        "HDMI-1 must be placed right-of the internal DSI-1 panel"
