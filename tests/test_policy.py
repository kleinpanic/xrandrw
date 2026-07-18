from __future__ import annotations

from xrandrw.policy import SIDES, assign_placements, is_internal_lcd


def test_internal_lcd_recognizes_all_panel_types():
    # Internal panels across systems must always win primary over externals:
    # eDP (laptops), LVDS (old laptops), DSI (Pi/embedded), DPI (GPIO panels).
    for name in ("eDP-1", "eDP1", "LVDS-1", "LVDS1", "DSI-1", "DSI1", "DPI-1"):
        assert is_internal_lcd(name), name


def test_external_connectors_are_not_internal():
    for name in ("HDMI-1", "HDMI-A-1", "DP-1", "DisplayPort-0", "VGA-1", "DVI-D-1"):
        assert not is_internal_lcd(name), name


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
