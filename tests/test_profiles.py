from __future__ import annotations

from xrandrw.profiles import (
    build_xrandr_argv,
    match_profile,
    parse_all_profiles,
    parse_profile,
)


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

    pi4 = match_profile(frozenset({"DSI-1", "HDMI-1", "DP-3"}), profiles)
    assert pi4 is not None and pi4.name == "PI4"

    laptop = match_profile(frozenset({"eDP-1"}), profiles)
    assert laptop is not None and laptop.name == "LAPTOP"

    assert match_profile(frozenset({"DP-9"}), profiles) is None


def test_match_tiebreak_largest_then_alphabetical():
    # BIG (3 heads) beats SMALL (2 heads) when both are subsets — largest connector set wins.
    profiles = parse_all_profiles(
        {
            "LAYOUT_BIG": "A-1:auto:primary:0x0;B-1:auto:secondary:0x0;C-1:auto:secondary:0x0",
            "LAYOUT_SMALL": "A-1:auto:primary:0x0;B-1:auto:secondary:0x0",
        }
    )
    winner = match_profile(frozenset({"A-1", "B-1", "C-1"}), profiles)
    assert winner is not None and winner.name == "BIG"

    # Equal-size subsets: the alphabetically-FIRST profile name wins (first-wins).
    tie = parse_all_profiles(
        {
            "LAYOUT_ZED": "A-1:auto:primary:0x0;B-1:auto:secondary:0x0",
            "LAYOUT_ABLE": "A-1:auto:primary:0x0;C-1:auto:secondary:0x0",
        }
    )
    won = match_profile(frozenset({"A-1", "B-1", "C-1"}), tie)
    assert won is not None and won.name == "ABLE"


def test_parse_malformed_skips(layout_pi4):
    profiles = parse_all_profiles(
        {"LAYOUT_BAD": "this-has-no-colons-or-fields", "LAYOUT_PI4": layout_pi4}
    )
    names = {p.name for p in profiles}
    assert "PI4" in names
    assert "BAD" not in names
