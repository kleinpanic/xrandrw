from __future__ import annotations

import re
from pathlib import Path

import pytest

from xrandrw.profiles import (
    build_xrandr_argv,
    match_profile,
    parse_all_profiles,
    parse_profile,
)

CONF_SAMPLE = Path(__file__).resolve().parents[1] / "xrandrw.conf.sample"
_SAMPLE_LAYOUT_RE = re.compile(r'^#\s*(LAYOUT_[A-Za-z0-9_]+)="(.+)"\s*$')


def _sample_layouts() -> list[tuple[str, str]]:
    # The shipped examples live COMMENTED OUT in the sample, so _load_env_file skips
    # them by design; parse them here instead of duplicating the strings into the test.
    out = []
    for line in CONF_SAMPLE.read_text().splitlines():
        m = _SAMPLE_LAYOUT_RE.match(line.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def test_parse_pi4(layout_pi4):
    prof = parse_profile("PI4", layout_pi4)
    assert prof is not None
    assert prof.name == "PI4"
    assert [s.connector for s in prof.specs] == ["DSI-1", "HDMI-1"]

    dsi, hdmi = prof.specs
    assert dsi.primary is True
    assert dsi.mode == "800x480"
    assert dsi.pos == (1600, 0)
    assert dsi.rel is None
    assert dsi.scale == "1x1"

    assert hdmi.primary is False
    assert hdmi.mode == "1600x900"
    assert hdmi.pos == (0, 0)


def test_pi4_byte_equivalent(layout_pi4, frozen_pi4_argv):
    assert build_xrandr_argv(parse_profile("PI4", layout_pi4)) == frozen_pi4_argv


def test_match(layout_pi4):
    profiles = parse_all_profiles(
        {"LAYOUT_PI4": layout_pi4, "LAYOUT_LAPTOP": "eDP-1:auto:primary:0x0"}
    )

    pi4 = match_profile(frozenset({"DSI-1", "HDMI-1"}), profiles)
    assert pi4 is not None and pi4.name == "PI4"

    laptop = match_profile(frozenset({"eDP-1"}), profiles)
    assert laptop is not None and laptop.name == "LAPTOP"

    assert match_profile(frozenset({"DP-9"}), profiles) is None


def test_match_requires_exact_set(layout_pi4):
    # WR-05: a strict-subset profile must NOT fire — the early-return in apply_once
    # would leave the extra connected head unconfigured.
    profiles = parse_all_profiles(
        {"LAYOUT_PI4": layout_pi4, "LAYOUT_SOLO": "DSI-1:auto:primary:0x0"}
    )
    assert match_profile(frozenset({"DSI-1", "HDMI-1", "DP-3"}), profiles) is None
    assert match_profile(frozenset({"DSI-1"}), profiles).name == "SOLO"


def test_match_tiebreak_alphabetical():
    # Identical connector sets: the alphabetically-FIRST profile name wins.
    tie = parse_all_profiles(
        {
            "LAYOUT_ZED": "A-1:auto:primary:0x0;B-1:auto:secondary:0x0",
            "LAYOUT_ABLE": "A-1:auto:primary:0x0;B-1:auto:secondary:0x0",
        }
    )
    won = match_profile(frozenset({"A-1", "B-1"}), tie)
    assert won is not None and won.name == "ABLE"


# ---------------- relative anchors + transforms (the shipped-sample grammar) ----------------

def test_relative_and_transforms_byte_equivalent(layout_relative, frozen_relative_argv):
    # Byte-exact twin of test_pi4_byte_equivalent for the OTHER half of the grammar.
    # Kills, in one assert: a wrong relative flag, a swapped side/anchor, a dropped
    # rotate=, and an ignored scale= — four defects that previously survived the suite.
    assert build_xrandr_argv(parse_profile("REL", layout_relative)) == frozen_relative_argv


def test_relative_parses_side_and_anchor_in_that_order(layout_relative):
    hdmi = parse_profile("REL", layout_relative).specs[1]
    assert hdmi.pos is None
    assert hdmi.rel == ("right-of", "eDP-1"), "rel is (side, anchor) — not (anchor, side)"
    assert hdmi.mode == "auto"
    assert hdmi.rotate == "left"
    assert hdmi.scale == "2x2"


@pytest.mark.parametrize("side", ["left-of", "right-of", "above", "below"])
def test_every_side_round_trips_to_its_own_flag(side):
    prof = parse_profile("S", f"eDP-1:auto:primary:0x0;HDMI-1:auto:secondary:{side}=eDP-1")
    assert prof.specs[1].rel == (side, "eDP-1")
    argv = build_xrandr_argv(prof)
    assert argv[argv.index("HDMI-1"):] == [
        "HDMI-1", "--auto", f"--{side}", "eDP-1", "--scale", "1x1",
    ]


@pytest.mark.parametrize(
    "spec",
    [
        "eDP-1:auto:primary:0x0;HDMI-1:auto:secondary:beside=eDP-1",   # not a side
        "eDP-1:auto:primary:0x0;HDMI-1:auto:secondary:right-of=",      # empty anchor
        "eDP-1:auto:primary:0x0;HDMI-1:auto:secondary:rightof=eDP-1",  # missing hyphen
    ],
)
def test_invalid_relative_position_rejects_whole_profile(spec):
    # Must be REJECTED, not silently accepted: a bogus side that parsed would place the
    # head at the xrandr default and stack the monitors on top of each other.
    assert parse_profile("BAD", spec) is None


@pytest.mark.parametrize("transform", ["rotate", "scale"])
def test_unknown_transform_rejects_whole_profile(transform):
    assert parse_profile("BAD", f"eDP-1:auto:primary:0x0:{transform}") is None
    assert parse_profile("BAD", "eDP-1:auto:primary:0x0:tilt=45") is None


# ---------------- the examples we actually ship to users ----------------

def test_sample_conf_examples_are_all_parseable():
    # Guards against silent rot in xrandrw.conf.sample: a user copying a shipped
    # example must not get a profile that degrades to None and never fires.
    layouts = _sample_layouts()
    assert len(layouts) >= 3, "expected the sample's worked LAYOUT_ examples to be found"
    for name, value in layouts:
        assert parse_profile(name, value) is not None, f"{name} in conf.sample no longer parses"


def test_sample_relative_pi4_example_builds_expected_argv():
    value = dict(_sample_layouts())["LAYOUT_PI4"]  # last wins: the RELATIVE variant
    assert "right-of=DSI-1" in value, "sample's relative Pi4 example changed shape"
    assert build_xrandr_argv(parse_profile("PI4", value)) == [
        "xrandr",
        "--output", "DSI-1", "--primary", "--mode", "800x480", "--pos", "0x0", "--scale", "1x1",
        "--output", "HDMI-1", "--mode", "1600x900", "--right-of", "DSI-1", "--scale", "1x1",
    ]


def test_sample_desk_example_builds_expected_argv():
    value = dict(_sample_layouts())["LAYOUT_DESK"]
    assert build_xrandr_argv(parse_profile("DESK", value)) == [
        "xrandr",
        "--output", "eDP-1", "--primary", "--auto", "--pos", "0x0", "--scale", "1x1",
        "--rotate", "normal",
        "--output", "HDMI-1", "--mode", "2560x1440", "--above", "eDP-1", "--scale", "1x1",
    ]


def test_parse_malformed_skips(layout_pi4):
    profiles = parse_all_profiles(
        {"LAYOUT_BAD": "this-has-no-colons-or-fields", "LAYOUT_PI4": layout_pi4}
    )
    names = {p.name for p in profiles}
    assert "PI4" in names
    assert "BAD" not in names
