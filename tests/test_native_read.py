from __future__ import annotations
import hashlib

import pytest
from Xlib.ext import randr

import xrandrw.xrandr as xr_mod
from xrandrw.xrandr import edid_bytes_to_sha1, randr_resources_to_outputs

from randr_fixtures import CRTC_INFOS, MODES, OUTPUT_INFOS, OutputInfo, PRIMARY_ID


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


def test_unknown_mode_id_skipped_not_keyerror():
    # WR-02: output references a mode id (999) missing from the modes snapshot (hotplug race)
    oi = OutputInfo(oid=100, name="DSI-1", connection=randr.Connected, crtc=64,
                    modes=[1, 999, 2], num_preferred=1)
    outs = randr_resources_to_outputs([oi], CRTC_INFOS, MODES, PRIMARY_ID)
    modes = outs["DSI-1"].modes
    assert [(w, h) for w, h, _r, _f in modes] == [(800, 480), (640, 480)]
    assert modes[0][3] == "*+"  # current + preferred flags survive the skip
    assert modes[1][3] == ""


def _hash_for(monkeypatch, outs):
    monkeypatch.setattr(xr_mod.RandRReader, "read", lambda self, logger=None: outs)
    return xr_mod.topology_hash()


def test_topology_hash_sees_lingering_disconnected_crtc(monkeypatch, output_factory):
    # Regression for the reported bug: unplugged HDMI-1 still driving 1600x900 was
    # invisible to change detection, so the daemon never powered it off.
    def dsi():
        return output_factory("DSI-1", connected=True, current_mode=(800, 480))
    lit = {"DSI-1": dsi(),
           "HDMI-1": output_factory("HDMI-1", connected=False, current_mode=(1600, 900))}
    off = {"DSI-1": dsi(),
           "HDMI-1": output_factory("HDMI-1", connected=False, current_mode=None)}
    solo = {"DSI-1": dsi()}
    plugged = {"DSI-1": dsi(),
               "HDMI-1": output_factory("HDMI-1", connected=True, current_mode=(1600, 900))}

    h_lit = _hash_for(monkeypatch, lit)
    h_off = _hash_for(monkeypatch, off)
    h_solo = _hash_for(monkeypatch, solo)
    h_plugged = _hash_for(monkeypatch, plugged)

    assert h_lit != h_off, "lingering CRTC on a disconnected head must change the hash"
    assert h_lit != h_solo
    assert h_lit != h_plugged, "connected flag must be part of the fingerprint"
    # Idle disconnected connectors do not contribute (no hash noise from dark heads).
    assert h_off == h_solo
