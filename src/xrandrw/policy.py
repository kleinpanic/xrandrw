from __future__ import annotations
from typing import Dict, Optional, Tuple

from xrandrw.xrandr import Output
from xrandrw.state import get_profile

SIDES = ("right-of", "left-of", "above", "below")

def is_internal_lcd(name: str) -> bool:
    return name.startswith("eDP") or name.startswith("LVDS")

def current_or_preferred_mode(o: Output) -> Optional[Tuple[int, int]]:
    for w, h, rate, flags in o.modes:
        if "*" in flags:
            return (w, h)
    for w, h, rate, flags in o.modes:
        if "+" in flags:
            return (w, h)
    return o.current_mode

def pick_side_for(pid: str, st: Dict[str, dict], occupied: Dict[str, str], default_side: str) -> str:
    prof = get_profile(st, pid)
    pref = prof.get("preferred_side") or default_side
    chosen = pref if pref not in occupied else next((s for s in SIDES if s not in occupied), default_side)
    prof["last_side"] = chosen
    return chosen
