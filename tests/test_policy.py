from __future__ import annotations

from xrandrw.policy import (
    SIDES,
    assign_placements,
    current_or_preferred_mode,
    is_internal_lcd,
)


def test_internal_lcd_recognizes_all_panel_types():
    # Internal panels across systems must always win primary over externals:
    # eDP (laptops), LVDS (old laptops), DSI (Pi/embedded), DPI (GPIO panels).
    for name in ("eDP-1", "eDP1", "LVDS-1", "LVDS1", "DSI-1", "DSI1", "DPI-1"):
        assert is_internal_lcd(name), name


def test_external_connectors_are_not_internal():
    for name in ("HDMI-1", "HDMI-A-1", "DP-1", "DisplayPort-0", "VGA-1", "DVI-D-1"):
        assert not is_internal_lcd(name), name


# ---------------- GAP-C: current_or_preferred_mode (THE HOTPLUG PATH) ----------------
#
# Deleting the '+' (preferred) fallback left the whole suite green. That branch is
# exactly what runs on hotplug: a freshly connected head has no CRTC yet, so xrandr
# marks no mode '*' (current) -- only '+' (preferred). Without the fallback the
# function drops through to o.current_mode, which is None for a new head, and the
# HiDPI/scale decision downstream (apply.py:288) is then made on missing data.


def test_current_mode_flag_wins(output_factory):
    o = output_factory("HDMI-1", modes=[(1920, 1080, 60.0, "*"), (1280, 720, 60.0, "+")])
    assert current_or_preferred_mode(o) == (1920, 1080)


def test_starred_mode_wins_even_when_a_preferred_mode_is_listed_first(output_factory):
    # xrandr lists the preferred mode first, so '+' routinely PRECEDES '*'. A
    # first-match-either-flag implementation would pick the wrong mode here.
    o = output_factory("HDMI-1", modes=[(3840, 2160, 30.0, "+"), (1920, 1080, 60.0, "*")])
    assert current_or_preferred_mode(o) == (1920, 1080)


def test_scan_continues_past_unflagged_modes_to_reach_the_current_one(output_factory):
    # Pins the 15->14 loop-back edge: the '*' mode is neither first nor second.
    o = output_factory("HDMI-1", modes=[
        (640, 480, 60.0, ""), (1280, 720, 60.0, ""), (1920, 1080, 60.0, "*")])
    assert current_or_preferred_mode(o) == (1920, 1080)


def test_freshly_connected_head_falls_back_to_the_preferred_mode(output_factory):
    # THE hotplug case: no CRTC => current_mode None and no '*' anywhere.
    o = output_factory("HDMI-1", current_mode=None,
                       modes=[(1920, 1080, 60.0, "+"), (1280, 720, 60.0, "")])
    assert current_or_preferred_mode(o) == (1920, 1080), \
        "a just-plugged head has only '+'; without this fallback its size is unknown"


def test_preferred_fallback_scans_past_unflagged_modes(output_factory):
    # The 18->17 loop-back edge, and the realistic ordering where the preferred
    # mode is not the first line of the mode list.
    o = output_factory("HDMI-1", current_mode=None,
                       modes=[(640, 480, 60.0, ""), (2560, 1440, 60.0, "+")])
    assert current_or_preferred_mode(o) == (2560, 1440)


def test_combined_star_plus_flags_are_recognised(output_factory):
    # xrandr's most common rendering: "1920x1080  60.00*+" -- current AND preferred.
    o = output_factory("HDMI-1", modes=[(1920, 1080, 60.0, "*+")])
    assert current_or_preferred_mode(o) == (1920, 1080)


def test_unflagged_mode_list_falls_back_to_current_mode(output_factory):
    o = output_factory("HDMI-1", current_mode=(1024, 768),
                       modes=[(640, 480, 60.0, ""), (800, 600, 60.0, "")])
    assert current_or_preferred_mode(o) == (1024, 768)


def test_no_modes_and_no_current_mode_is_none(output_factory):
    # Both callers guard on None (apply.py:28 `if m:`, :288 `cur[0] if cur else 0`),
    # so returning None is the contract, not an oversight.
    assert current_or_preferred_mode(output_factory("HDMI-1", modes=[])) is None


def _pref(items, side="right-of"):
    return [(i, side) for i in items]


def test_assign_placements_single():
    assert assign_placements([("a", "right-of")], "PRIM") == [("a", "right-of", "PRIM")]


def test_assign_placements_honors_preferred_side():
    # The whole point of set-pref: an item lands on its STORED side, not an index default.
    assert assign_placements([("a", "left-of")], "PRIM") == [("a", "left-of", "PRIM")]
    assert assign_placements([("a", "above")], "PRIM") == [("a", "above", "PRIM")]
    assert assign_placements([("a", "below")], "PRIM") == [("a", "below", "PRIM")]


def test_assign_placements_collision_falls_back_to_free_side():
    # Two items want left-of; the first (newest) wins it, the other takes the next free side.
    result = assign_placements([("a", "left-of"), ("b", "left-of")], "PRIM")
    assert result[0] == ("a", "left-of", "PRIM")
    assert result[1][0] == "b" and result[1][1] != "left-of" and result[1][2] == "PRIM"


def test_assign_placements_four_distinct_prefs_all_anchor_primary():
    result = assign_placements(
        [("a", "right-of"), ("b", "left-of"), ("c", "above"), ("d", "below")], "PRIM")
    assert result == [
        ("a", "right-of", "PRIM"), ("b", "left-of", "PRIM"),
        ("c", "above", "PRIM"), ("d", "below", "PRIM"),
    ]


def test_assign_placements_same_pref_fills_free_sides_then_chains():
    # All want right-of: first takes it, the rest fall back through the free sides,
    # and the 5th (all four sides taken) chains off the previously placed item.
    result = assign_placements(_pref(["a", "b", "c", "d", "e"]), "PRIM")
    assert [r[1] for r in result[:4]] == list(SIDES)
    assert all(r[2] == "PRIM" for r in result[:4])
    assert result[4] == ("e", "right-of", "d")


def test_assign_placements_chain_beyond_four_off_previous():
    result = assign_placements(_pref(["a", "b", "c", "d", "e", "f", "g"]), "PRIM")
    for i in (4, 5, 6):
        assert result[i][1] == "right-of" and result[i][2] == result[i - 1][0]


def test_assign_placements_no_collision_among_primary_anchored():
    result = assign_placements(_pref(["a", "b", "c", "d"]), "PRIM")
    sides = [r[1] for r in result]
    assert len(set(sides)) == 4


def test_assign_placements_chain_side_override():
    result = assign_placements(_pref(["a", "b", "c", "d", "e"]), "PRIM", chain_side="below")
    assert result[4] == ("e", "below", "d")
