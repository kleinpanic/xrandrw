from __future__ import annotations
from typing import Callable

import pytest

from xrandrw import config, dwmipc
from xrandrw.xrandr import Output


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    # Hard guarantee: no test may ever read or write the real user state at
    # ~/.local/share/xrandrw/. state_dir()/state_path() honor XDG_DATA_HOME, so
    # redirecting it per-test sandboxes all state I/O — even for tests that call
    # apply_once/set_pref without patching load_state/save_state directly.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    # Hard guarantee, same contract as isolate_state above: no test may ever read
    # the DEVELOPER'S real /etc/xdg/xrandrw.conf or ~/.config/xrandrw.conf.
    #
    # Found the hard way. config.CONF_USER is `Path.home() / ".config/xrandrw.conf"`
    # resolved at import, so the whole suite silently inherited whatever the person
    # running it had configured. The moment WINDOW_MANAGEMENT=1 was set on this
    # machine to run the daemon for real, test_main_window_state_returns_int_...
    # started failing -- a test whose own comment says "Feature default-off" was
    # asserting the default while reading a live user override. It had only ever
    # passed because nobody who ran it also USED the tool; CI passes for the same
    # accidental reason (no user config on a runner). That is a test passing for the
    # wrong reason, which is indistinguishable from a test that does not work.
    #
    # An env-only guard is not enough: CONF_USER is Path.home()-based, not
    # XDG_CONFIG_HOME-based, so setting XDG_CONFIG_HOME does nothing. Override the
    # module attributes themselves and clear every ENV_DEFAULTS key, so both config
    # tiers AND the process-environment tier start from a known-empty state.
    #
    # Tests that WANT a config (test_config.py's isolated_config) request their own
    # fixture, which runs after this one and repoints the same attributes.
    monkeypatch.setattr(config, "CONF_SYS", tmp_path / "no-such-sys.conf")
    monkeypatch.setattr(config, "CONF_USER", tmp_path / "no-such-user.conf")
    for key in config.ENV_DEFAULTS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def block_live_dwm(request, monkeypatch, tmp_path):
    # Phase-14 test-isolation gap (P0). A non-fully-mocked unit test must NEVER be
    # able to resolve dwmipc.DEFAULT_SOCK_PATH to the real /tmp/dwm.sock or DISPLAY
    # to :0 and reach the developer's live dwm — a mutating tagmon SIGSEGV-crashed
    # it four times in one day. dwmipc freezes DEFAULT_SOCK_PATH at import
    # (dwmipc.py:57), so an env-only guard is too late for that module attribute:
    # override BOTH the attribute (every production call site — cli.py, windows.py,
    # relocate.py — re-reads it at call time) AND $DWM_SOCKET, and point
    # DISPLAY/XAUTHORITY at dead throwaways so any stray Xlib/socket connect fails
    # closed instead of landing on :0.
    #
    # functional-marked tests stand up their own session-scoped private socket +
    # display (tests/functional/conftest.py) and assert socket != /tmp/dwm.sock;
    # no-op there so this per-function guard never clobbers that session harness.
    if request.node.get_closest_marker("functional") is not None:
        return
    dead_sock = str(tmp_path / "no-dwm.sock")
    monkeypatch.setattr(dwmipc, "DEFAULT_SOCK_PATH", dead_sock)
    monkeypatch.setenv("DWM_SOCKET", dead_sock)
    monkeypatch.setenv("DISPLAY", ":99991")
    monkeypatch.setenv("XAUTHORITY", str(tmp_path / "no.Xauthority"))


@pytest.fixture
def output_factory() -> Callable[..., Output]:
    # `position` (added 14-08) defaults to None so every existing call site is unaffected;
    # without it no shared-fixture test could express a CRTC-lit output (the state the
    # replug-bounce defect turns on).
    def make(name, connected=True, primary=False, current_mode=None, modes=None, edid_sha1=None,
             position=None):
        return Output(
            name=name,
            connected=connected,
            primary=primary,
            current_mode=current_mode,
            position=position,
            modes=modes if modes is not None else [],
            edid_sha1=edid_sha1,
        )
    return make


@pytest.fixture
def layout_pi4() -> str:
    return "DSI-1:800x480:primary:1600x0;HDMI-1:1600x900:secondary:0x0"


@pytest.fixture
def frozen_pi4_argv() -> list:
    # Single source of truth shared with the Wave-2 apply test; do not redefine elsewhere.
    return [
        "xrandr",
        "--output", "DSI-1", "--primary", "--mode", "800x480", "--pos", "1600x0", "--scale", "1x1",
        "--output", "HDMI-1", "--mode", "1600x900", "--pos", "0x0", "--scale", "1x1",
    ]


@pytest.fixture
def layout_relative() -> str:
    # Exercises the half of the grammar the absolute-position fixtures never touch:
    # a relative anchor plus BOTH transforms, on an auto-mode head.
    return "eDP-1:auto:primary:0x0;HDMI-1:auto:secondary:right-of=eDP-1:rotate=left:scale=2x2"


@pytest.fixture
def frozen_relative_argv() -> list:
    # Same contract as frozen_pi4_argv: byte-exact argv, so a wrong flag, a swapped
    # side/anchor, or a silently-dropped transform cannot pass. Do not redefine elsewhere.
    return [
        "xrandr",
        "--output", "eDP-1", "--primary", "--auto", "--pos", "0x0", "--scale", "1x1",
        "--output", "HDMI-1", "--auto", "--right-of", "eDP-1", "--scale", "2x2",
        "--rotate", "left",
    ]


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def lock_dir(tmp_path):
    return tmp_path
