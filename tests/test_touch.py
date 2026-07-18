from __future__ import annotations

from xrandrw.touch import parse_touch_map, resolve_touch_remaps


def test_parse_empty_is_no_op():
    assert parse_touch_map({}) == []
    assert parse_touch_map({"TOUCH_MAP": ""}) == []
    assert parse_touch_map({"TOUCH_MAP": "   "}) == []


def test_parse_single_and_multiple():
    assert parse_touch_map({"TOUCH_MAP": "ft5x06:DSI-1"}) == [("ft5x06", "DSI-1")]
    assert parse_touch_map({"TOUCH_MAP": "ft5x06:DSI-1;ELAN Touchscreen:eDP-1"}) == [
        ("ft5x06", "DSI-1"),
        ("ELAN Touchscreen", "eDP-1"),
    ]


def test_parse_ignores_malformed_chunks():
    assert parse_touch_map({"TOUCH_MAP": "ft5x06:DSI-1;garbage;:DSI-2;name:"}) == [
        ("ft5x06", "DSI-1")
    ]


def test_resolve_matches_case_insensitive_substring():
    devices = [(6, "Power Button"), (8, "generic ft5x06 10-0038"), (10, "Logitech Mouse")]
    remaps = resolve_touch_remaps([("ft5x06", "DSI-1")], devices, {"DSI-1"})
    assert remaps == [(8, "DSI-1")]


def test_resolve_skips_when_output_not_connected():
    # Never map onto a head that isn't present — that reintroduces the dead-region bug.
    devices = [(8, "generic ft5x06")]
    assert resolve_touch_remaps([("ft5x06", "HDMI-1")], devices, {"DSI-1"}) == []


def test_resolve_skips_when_no_device_matches():
    devices = [(8, "Logitech K400")]
    assert resolve_touch_remaps([("ft5x06", "DSI-1")], devices, {"DSI-1"}) == []


def test_resolve_multiple_devices_first_match_wins():
    devices = [(8, "ELAN Touchscreen"), (9, "ELAN Touchpad")]
    remaps = resolve_touch_remaps([("ELAN Touchscreen", "eDP-1")], devices, {"eDP-1"})
    assert remaps == [(8, "eDP-1")]
