from __future__ import annotations
from typing import Callable

import pytest

from xrandrw.xrandr import Output


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    # Hard guarantee: no test may ever read or write the real user state at
    # ~/.local/share/xrandrw/. state_dir()/state_path() honor XDG_DATA_HOME, so
    # redirecting it per-test sandboxes all state I/O — even for tests that call
    # apply_once/set_pref without patching load_state/save_state directly.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


@pytest.fixture
def output_factory() -> Callable[..., Output]:
    def make(name, connected=True, primary=False, current_mode=None, modes=None, edid_sha1=None):
        return Output(
            name=name,
            connected=connected,
            primary=primary,
            current_mode=current_mode,
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
def state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def lock_dir(tmp_path):
    return tmp_path
