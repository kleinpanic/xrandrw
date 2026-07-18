from __future__ import annotations
import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from Xlib.ext import randr

import xrandrw.xrandr as xr_mod
from xrandrw.xrandr import (
    RandRReader,
    edid_bytes_to_sha1,
    edid_sysfs_read,
    randr_resources_to_outputs,
    read_edid_native,
    read_edids,
)

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


# ---------------- RandRReader / EDID integration (WR-04) ----------------
# Fake Display objects standing in for python-xlib; no live X server.

class FakeReadDisplay:
    def __init__(self, output_infos=OUTPUT_INFOS, crtc_infos=CRTC_INFOS,
                 modes=MODES, primary=PRIMARY_ID, version=(1, 5)):
        self._outputs = {oi.oid: oi for oi in output_infos}
        self._crtcs = crtc_infos
        self._modes = modes
        self._primary = primary
        self._version = version
        self.closed = False

    def screen(self):
        res = SimpleNamespace(config_timestamp=1234, outputs=list(self._outputs), modes=self._modes)
        root = SimpleNamespace(
            xrandr_get_screen_resources_current=lambda: res,
            xrandr_get_output_primary=lambda: SimpleNamespace(output=self._primary),
        )
        return SimpleNamespace(root=root)

    def xrandr_get_output_info(self, oid, ct):
        assert ct == 1234, "config_timestamp must be threaded through"
        return self._outputs[oid]

    def xrandr_get_crtc_info(self, crtc, ct):
        return self._crtcs[crtc]

    def xrandr_query_version(self):
        return SimpleNamespace(major_version=self._version[0], minor_version=self._version[1])

    def close(self):
        self.closed = True


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_native_read")
    lg.setLevel(logging.DEBUG)
    return lg


def test_reader_read_maps_and_closes(monkeypatch, logger):
    fake = FakeReadDisplay()
    monkeypatch.setattr(xr_mod.display, "Display", lambda: fake)

    outs = RandRReader().read(logger)

    # The live read must produce the same mapping the pure mapper is verified for.
    assert outs == randr_resources_to_outputs(OUTPUT_INFOS, CRTC_INFOS, MODES, PRIMARY_ID)
    assert fake.closed, "Display must be closed after a read"


def test_reader_read_closes_on_error(monkeypatch, logger):
    fake = FakeReadDisplay()

    def boom(oid, ct):
        raise RuntimeError("X error")
    fake.xrandr_get_output_info = boom
    monkeypatch.setattr(xr_mod.display, "Display", lambda: fake)

    with pytest.raises(RuntimeError):
        RandRReader().read(logger)
    assert fake.closed, "Display must be closed even when the read raises"


def test_reader_open_fail_logged(monkeypatch, logger, caplog):
    def no_display():
        raise ConnectionError("no X server")
    monkeypatch.setattr(xr_mod.display, "Display", no_display)

    with caplog.at_level(logging.ERROR, logger="xrandrw.test_native_read"):
        with pytest.raises(ConnectionError):
            RandRReader().read(logger)

    fails = [r for r in caplog.records if getattr(r, "event", None) == "xlib_connect_fail"]
    assert fails and fails[0].levelno == logging.ERROR


def test_reader_version_and_events_supported(monkeypatch, logger):
    for version, expected in (((1, 5), True), ((1, 6), True), ((1, 4), False)):
        fake = FakeReadDisplay(version=version)
        monkeypatch.setattr(xr_mod.display, "Display", lambda f=fake: f)
        assert RandRReader().version(logger) == version
        assert RandRReader().events_supported(logger) is expected
        assert fake.closed


def test_read_edid_native_paths():
    d = SimpleNamespace(
        xrandr_get_output_property=lambda oid, atom, t, off, ln: SimpleNamespace(value=b"abc")
    )
    assert read_edid_native(d, 100, 99) == b"abc"

    d_empty = SimpleNamespace(
        xrandr_get_output_property=lambda oid, atom, t, off, ln: SimpleNamespace(value=b"")
    )
    assert read_edid_native(d_empty, 100, 99) is None

    def boom(*a):
        raise RuntimeError("BadOutput")
    assert read_edid_native(SimpleNamespace(xrandr_get_output_property=boom), 100, 99) is None


def _fake_drm(monkeypatch, tmp_path):
    drm = tmp_path / "drm"
    monkeypatch.setattr(xr_mod, "Path",
                        lambda p: drm if p == "/sys/class/drm" else Path(p))
    return drm


def test_edid_sysfs_read(monkeypatch, tmp_path):
    drm = _fake_drm(monkeypatch, tmp_path)
    raw = bytes(range(128))
    (drm / "card1-HDMI-1").mkdir(parents=True)
    (drm / "card1-HDMI-1" / "edid").write_bytes(raw)

    assert edid_sysfs_read("HDMI-1") == raw
    assert edid_sysfs_read("DP-1") is None          # no matching connector dir
    # Unreadable edid node degrades to None (read_bytes raises on a directory).
    (drm / "card1-DSI-1" / "edid").mkdir(parents=True)
    assert edid_sysfs_read("DSI-1") is None


class FakeEdidDisplay:
    def __init__(self, edids):  # name -> raw bytes
        self._by_oid = {200 + i: kv for i, kv in enumerate(sorted(edids.items()))}
        self.closed = False

    def get_atom(self, name):
        assert name == "EDID"
        return 99

    def screen(self):
        res = SimpleNamespace(outputs=list(self._by_oid), config_timestamp=7)
        return SimpleNamespace(root=SimpleNamespace(xrandr_get_screen_resources_current=lambda: res))

    def xrandr_get_output_info(self, oid, ct):
        return SimpleNamespace(name=self._by_oid[oid][0])

    def xrandr_get_output_property(self, oid, atom, t, off, ln):
        return SimpleNamespace(value=self._by_oid[oid][1])

    def close(self):
        self.closed = True


def test_read_edids_sysfs_then_native(monkeypatch, output_factory, logger):
    sysfs_raw = bytes(range(128))
    native_raw = bytes(reversed(range(128)))
    outs = {
        "DSI-1": output_factory("DSI-1", connected=True),
        "HDMI-1": output_factory("HDMI-1", connected=True),
        "HDMI-2": output_factory("HDMI-2", connected=False),
    }
    monkeypatch.setattr(xr_mod, "edid_sysfs_read",
                        lambda n: sysfs_raw if n == "DSI-1" else None)
    fake = FakeEdidDisplay({"HDMI-1": native_raw})
    monkeypatch.setattr(xr_mod.display, "Display", lambda: fake)

    read_edids(outs, logger)

    assert outs["DSI-1"].edid_sha1 == hashlib.sha1(sysfs_raw).hexdigest()
    assert outs["HDMI-1"].edid_sha1 == hashlib.sha1(native_raw).hexdigest()
    assert outs["HDMI-2"].edid_sha1 is None, "disconnected outputs must be skipped"
    assert fake.closed


def test_read_edids_all_sysfs_skips_native(monkeypatch, output_factory, logger):
    outs = {"DSI-1": output_factory("DSI-1", connected=True)}
    monkeypatch.setattr(xr_mod, "edid_sysfs_read", lambda n: bytes(range(128)))

    def no_display():
        raise AssertionError("native path must not open a Display when sysfs satisfied all")
    monkeypatch.setattr(xr_mod.display, "Display", no_display)

    read_edids(outs, logger)
    assert outs["DSI-1"].edid_sha1 is not None


def test_read_edids_display_fail_degrades(monkeypatch, output_factory, logger, caplog):
    outs = {"DP-1": output_factory("DP-1", connected=True)}
    monkeypatch.setattr(xr_mod, "edid_sysfs_read", lambda n: None)

    def no_display():
        raise ConnectionError("no X server")
    monkeypatch.setattr(xr_mod.display, "Display", no_display)

    with caplog.at_level(logging.DEBUG, logger="xrandrw.test_native_read"):
        read_edids(outs, logger)  # must not raise

    assert outs["DP-1"].edid_sha1 is None
    fails = [r for r in caplog.records if getattr(r, "event", None) == "edid_native_fail"]
    assert fails


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
