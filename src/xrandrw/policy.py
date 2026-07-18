from __future__ import annotations
from typing import List, Optional, Tuple

from xrandrw.xrandr import Output

SIDES = ("right-of", "left-of", "above", "below")

def is_internal_lcd(name: str) -> bool:
    # Internal-panel connector prefixes across systems: eDP (modern laptops),
    # LVDS (older laptops), DSI (Raspberry Pi ribbon panels / embedded), DPI
    # (GPIO parallel panels). These must always win primary over externals.
    return name.startswith(("eDP", "LVDS", "DSI", "DPI"))

def current_or_preferred_mode(o: Output) -> Optional[Tuple[int, int]]:
    for w, h, rate, flags in o.modes:
        if "*" in flags:
            return (w, h)
    for w, h, rate, flags in o.modes:
        if "+" in flags:
            return (w, h)
    return o.current_mode

def assign_placements(ordered: List[Tuple[str, str]], anchor: str,
                      chain_side: str = "right-of") -> List[Tuple[str, str, str]]:
    # `ordered` is (item, preferred_side) pairs, newest first. Each item takes its
    # preferred side relative to `anchor` if free; on collision it takes the next free
    # side; once all four sides are occupied, further items chain off the previously
    # placed item (HARD-04, uncapped). Honoring preferred_side is what makes set-pref
    # persist — placing purely by index would silently ignore the stored side.
    placements: List[Tuple[str, str, str]] = []
    occupied: dict = {}
    last_item: Optional[str] = None
    for item, pref in ordered:
        if len(occupied) >= len(SIDES):
            placements.append((item, chain_side, last_item))
        else:
            side = pref if pref in SIDES else chain_side
            if side in occupied:
                side = next(s for s in SIDES if s not in occupied)
            occupied[side] = item
            placements.append((item, side, anchor))
        last_item = item
    return placements
