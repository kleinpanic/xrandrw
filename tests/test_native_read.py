from __future__ import annotations
import hashlib

import pytest

from xrandrw.xrandr import edid_bytes_to_sha1, randr_resources_to_outputs

from randr_fixtures import CRTC_INFOS, MODES, OUTPUT_INFOS, PRIMARY_ID


@pytest.fixture
def mapped():
    return randr_resources_to_outputs(OUTPUT_INFOS, CRTC_INFOS, MODES, PRIMARY_ID)


def test_connected_primary_with_crtc(mapped):
    dsi = mapped["DSI-1"]
    assert dsi.connected is True
    assert dsi.primary is True
    assert dsi.current_mode == (800, 480)


def test_disconnected_stale_crtc_is_not_connected(mapped):
    # THE gotcha: HDMI-1 keeps a stale CRTC (1600x900) but connection != Connected
    assert mapped["HDMI-1"].connected is False
    assert mapped["HDMI-1"].primary is False


def test_no_crtc_output_has_no_current_mode(mapped):
    hdmi2 = mapped["HDMI-2"]
    assert hdmi2.connected is False
    assert hdmi2.current_mode is None


def test_mode_flags_and_refresh(mapped):
    modes = mapped["DSI-1"].modes
    assert modes[0] == (800, 480, 60.0, "*+")   # current + preferred
    assert modes[1] == (640, 480, 59.52, "")    # neither
    assert modes[2][:2] == (1024, 768)
    assert modes[2][2] == 0.0                    # div-by-zero guarded


def test_bytes_name_normalized(mapped):
    assert "HDMI-2" in mapped
    assert isinstance(mapped["HDMI-2"].name, str)


def test_edid_sha1_known():
    raw = bytes(range(128))
    assert edid_bytes_to_sha1(raw) == hashlib.sha1(raw).hexdigest()


def test_edid_sha1_empty_is_none():
    assert edid_bytes_to_sha1(b"") is None
