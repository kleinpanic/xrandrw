from __future__ import annotations
from typing import Callable

import pytest

from xrandrw.xrandr import Output


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
def state_path(tmp_path):
    return tmp_path / "state.json"


@pytest.fixture
def lock_dir(tmp_path):
    return tmp_path
