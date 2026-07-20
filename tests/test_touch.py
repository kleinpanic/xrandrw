from __future__ import annotations

import logging
from subprocess import CompletedProcess

import pytest

import xrandrw.touch as touch
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


# ---------------- GAP-B: the ACTING half (xinput I/O), end to end ----------------
#
# parse_touch_map/resolve_touch_remaps above are pure and well covered, but
# _list_input_devices and the acting half of remap_touch were not exercised at
# all. Three separately injected defects survived the whole suite: dropping the
# box-drawing-glyph strip, ignoring xinput's exit status, and NEVER ISSUING
# `map-to-output`. Each is silent -- the user sets TOUCH_MAP, sees no error, and
# touch input stays on the wrong panel. The tests below drive a fake `run` and
# assert the ARGV actually issued, not merely that nothing raised.

# A realistic `xinput list --short` block: master devices carry ⎡/⎣, their slaves
# are indented under ⎜ or spaces and prefixed with ↳, and columns are tab-separated.
_XINPUT_SHORT = (
    "⎡ Virtual core pointer                     \tid=2\t[master pointer  (3)]\n"
    "⎜   ↳ Virtual core XTEST pointer           \tid=4\t[slave  pointer  (2)]\n"
    "⎜   ↳ generic ft5x06 (79)                  \tid=8\t[slave  pointer  (2)]\n"
    "⎜   ↳ Logitech Wireless Mouse              \tid=11\t[slave  pointer  (2)]\n"
    "⎣ Virtual core keyboard                    \tid=3\t[master keyboard (2)]\n"
    "    ↳ Virtual core XTEST keyboard          \tid=5\t[slave  keyboard (3)]\n"
    "    ↳ ELAN Touchscreen                     \tid=13\t[slave  keyboard (3)]\n"
)

_EXPECTED_DEVICES = [
    (2, "Virtual core pointer"),
    (4, "Virtual core XTEST pointer"),
    (8, "generic ft5x06 (79)"),
    (11, "Logitech Wireless Mouse"),
    (3, "Virtual core keyboard"),
    (5, "Virtual core XTEST keyboard"),
    (13, "ELAN Touchscreen"),
]


class _FakeRun:
    # Records every argv issued and replays a caller-chosen (rc, stdout), so a test
    # can assert what xinput was ACTUALLY asked to do.
    def __init__(self, stdout: str = "", rc: int = 0):
        self.stdout = stdout
        self.rc = rc
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, logger=None, **kw):
        self.cmds.append(list(cmd))
        return CompletedProcess(cmd, self.rc, stdout=self.stdout)


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_touch")
    lg.setLevel(logging.DEBUG)
    return lg


def test_list_input_devices_parses_ids_and_strips_tree_glyphs(monkeypatch, logger):
    fake = _FakeRun(_XINPUT_SHORT)
    monkeypatch.setattr(touch, "run", fake)

    devices = touch._list_input_devices(logger)

    assert devices == _EXPECTED_DEVICES
    # Explicit: no box-drawing glyph may leak into a device name. A name of
    # "⎜   ↳ generic ft5x06 (79)" would still substring-match today, so the
    # equality above is what actually pins the strip -- this states the intent.
    assert not any(ch in name for _id, name in devices for ch in "⎡⎜⎣↳")
    assert fake.cmds == [["xinput", "list", "--short"]]


def test_list_input_devices_ignores_lines_without_an_id(monkeypatch, logger):
    monkeypatch.setattr(touch, "run", _FakeRun(
        "Virtual core pointer\n"
        "⎜   ↳ generic ft5x06                       \tid=8\t[slave  pointer  (2)]\n"
        "\n"
        "unable to find device foo\n"
    ))
    assert touch._list_input_devices(logger) == [(8, "generic ft5x06")]


def test_list_input_devices_honours_a_nonzero_exit(monkeypatch, logger):
    # xinput failing (no X, no such display, binary error) must yield NO devices.
    # Parsing its stdout anyway would let us map onto ids scraped from an error
    # stream -- so the payload here is deliberately a VALID device list.
    monkeypatch.setattr(touch, "run", _FakeRun(_XINPUT_SHORT, rc=1))
    assert touch._list_input_devices(logger) == []


def test_remap_touch_actually_issues_map_to_output(monkeypatch, logger, caplog):
    # THE regression: remap_touch resolved the right (id, output) pair and then
    # never told xinput about it. Assert the exact argv sequence.
    fake = _FakeRun(_XINPUT_SHORT)
    monkeypatch.setattr(touch, "run", fake)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_touch"):
        touch.remap_touch({"TOUCH_MAP": "ft5x06:DSI-1"}, {"DSI-1"}, logger)

    assert fake.cmds == [
        ["xinput", "list", "--short"],
        ["xinput", "map-to-output", "8", "DSI-1"],
    ], "the resolved mapping must be pushed to xinput, not just computed"

    remaps = [r for r in caplog.records if getattr(r, "event", None) == "touch_remap"]
    assert len(remaps) == 1
    assert remaps[0].device == 8 and remaps[0].output == "DSI-1"


def test_remap_touch_maps_every_mapping_from_one_device_listing(monkeypatch, logger):
    fake = _FakeRun(_XINPUT_SHORT)
    monkeypatch.setattr(touch, "run", fake)

    touch.remap_touch({"TOUCH_MAP": "ft5x06:DSI-1;ELAN Touchscreen:eDP-1"},
                      {"DSI-1", "eDP-1"}, logger)

    assert fake.cmds == [
        ["xinput", "list", "--short"],
        ["xinput", "map-to-output", "8", "DSI-1"],
        ["xinput", "map-to-output", "13", "eDP-1"],
    ], "one listing, then one map-to-output per resolved mapping, in TOUCH_MAP order"


def test_remap_touch_without_config_never_touches_xinput(monkeypatch, logger):
    # The documented no-op: unset TOUCH_MAP means xinput is not even a dependency.
    fake = _FakeRun(_XINPUT_SHORT)
    monkeypatch.setattr(touch, "run", fake)

    touch.remap_touch({}, {"DSI-1"}, logger)
    touch.remap_touch({"TOUCH_MAP": ""}, {"DSI-1"}, logger)

    assert fake.cmds == [], "no TOUCH_MAP must not shell out to xinput at all"


def test_remap_touch_does_not_map_onto_a_disconnected_output(monkeypatch, logger):
    fake = _FakeRun(_XINPUT_SHORT)
    monkeypatch.setattr(touch, "run", fake)

    touch.remap_touch({"TOUCH_MAP": "ft5x06:HDMI-1"}, {"DSI-1"}, logger)

    assert fake.cmds == [["xinput", "list", "--short"]], \
        "mapping onto an absent head would strand touch input in a dead region"


def test_remap_touch_issues_nothing_when_the_listing_failed(monkeypatch, logger):
    # Chain consequence of the exit-status check: a failed listing must not lead
    # to a map-to-output against a stale or imagined device id.
    fake = _FakeRun(_XINPUT_SHORT, rc=1)
    monkeypatch.setattr(touch, "run", fake)

    touch.remap_touch({"TOUCH_MAP": "ft5x06:DSI-1"}, {"DSI-1"}, logger)

    assert fake.cmds == [["xinput", "list", "--short"]]


def test_remap_touch_skips_a_mapping_with_no_matching_device(monkeypatch, logger):
    fake = _FakeRun(_XINPUT_SHORT)
    monkeypatch.setattr(touch, "run", fake)

    touch.remap_touch({"TOUCH_MAP": "no-such-panel:DSI-1"}, {"DSI-1"}, logger)

    assert fake.cmds == [["xinput", "list", "--short"]]
