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


def test_assign_placements_single():
    assert assign_placements(["a"], "PRIM") == [("a", "right-of", "PRIM")]


def test_assign_placements_first_four_anchor_to_primary():
    result = assign_placements(["a", "b", "c", "d"], "PRIM")
    assert result == [
        ("a", SIDES[0], "PRIM"),
        ("b", SIDES[1], "PRIM"),
        ("c", SIDES[2], "PRIM"),
        ("d", SIDES[3], "PRIM"),
    ]


def test_assign_placements_chains_beyond_four():
    result = assign_placements(["a", "b", "c", "d", "e"], "PRIM")
    assert result[4] == ("e", "right-of", "d")


def test_assign_placements_seven_chain_off_previous():
    pids = ["a", "b", "c", "d", "e", "f", "g"]
    result = assign_placements(pids, "PRIM")
    for i in (4, 5, 6):
        assert result[i] == (pids[i], "right-of", pids[i - 1])


def test_assign_placements_no_collision_among_primary_anchored():
    pids = ["a", "b", "c", "d", "e", "f", "g"]
    result = assign_placements(pids, "PRIM")
    primary_anchored = [(rel, ref) for _, rel, ref in result[:4]]
    assert len(set(primary_anchored)) == 4


def test_assign_placements_chain_side_override():
    result = assign_placements(["a", "b", "c", "d", "e"], "PRIM", chain_side="below")
    assert result[4] == ("e", "below", "d")


def test_assign_placements_invariant():
    pids = ["a", "b", "c", "d", "e", "f"]
    anchor = "PRIM"
    chain_side = "right-of"
    result = assign_placements(pids, anchor, chain_side=chain_side)
    for i, entry in enumerate(result):
        if i < len(SIDES):
            assert entry == (pids[i], SIDES[i], anchor)
        else:
            assert entry == (pids[i], chain_side, pids[i - 1])
