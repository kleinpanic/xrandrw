from __future__ import annotations
from dataclasses import dataclass

# ---------------- grammar ----------------

SIDES = ("left-of", "right-of", "above", "below")

@dataclass
class OutputSpec:
    connector: str
    mode: str
    primary: bool
    pos: tuple[int, int] | None
    rel: tuple[str, str] | None
    scale: str = "1x1"
    rotate: str | None = None

@dataclass
class Profile:
    name: str
    specs: list[OutputSpec]

    @property
    def connectors(self) -> frozenset:
        return frozenset(s.connector for s in self.specs)

# ---------------- parse ----------------

def _parse_position(field: str) -> tuple[tuple[int, int] | None, tuple[str, str] | None]:
    if "=" in field:
        side, _, anchor = field.partition("=")
        if side not in SIDES or not anchor:
            raise ValueError(f"bad relative position {field!r}")
        return None, (side, anchor)
    x, sep, y = field.partition("x")
    if not sep:
        raise ValueError(f"bad position {field!r}")
    return (int(x), int(y)), None

def _parse_spec(field: str) -> OutputSpec:
    parts = field.split(":")
    if len(parts) < 4:
        raise ValueError(f"spec needs connector:mode:role:position, got {field!r}")
    connector, mode, role, position = parts[0], parts[1], parts[2], parts[3]
    if not connector or not mode:
        raise ValueError(f"empty connector/mode in {field!r}")
    pos, rel = _parse_position(position)
    spec = OutputSpec(
        connector=connector,
        mode=mode,
        primary=(role == "primary"),
        pos=pos,
        rel=rel,
    )
    for transform in parts[4:]:
        key, sep, val = transform.partition("=")
        if not sep:
            raise ValueError(f"bad transform {transform!r}")
        if key == "scale":
            spec.scale = val
        elif key == "rotate":
            spec.rotate = val
        else:
            raise ValueError(f"unknown transform {key!r}")
    return spec

# Degrade-to-None on any malformed spec (mirrors config._coerce_int degrade-to-default):
# a bad conf line is skipped, never crashes the daemon.
def parse_profile(name: str, spec_string: str) -> Profile | None:
    try:
        specs = [_parse_spec(s) for s in spec_string.split(";") if s]
    except ValueError:
        return None
    if not specs:
        return None
    return Profile(name=name, specs=specs)

def parse_all_profiles(env: dict[str, str]) -> list[Profile]:
    profiles: list[Profile] = []
    for key, value in env.items():
        if not key.startswith("LAYOUT_"):
            continue
        prof = parse_profile(key[len("LAYOUT_"):], value)
        if prof is not None:
            profiles.append(prof)
    return profiles

# ---------------- match ----------------

# Exact match: a profile fires only when its connector set EQUALS the connected set.
# Subset matching (WR-05) let a {DSI-1} profile fire with {DSI-1, HDMI-1} connected and
# apply_once early-returned with HDMI-1 never configured. Tie-break on identical sets:
# alphabetically-first profile name wins.
def match_profile(connected: frozenset, profiles: list[Profile]) -> Profile | None:
    candidates = [p for p in profiles if p.connectors == connected]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.name)[0]

# ---------------- argv ----------------

def build_xrandr_argv(profile: Profile) -> list[str]:
    argv = ["xrandr"]
    for s in profile.specs:
        argv += ["--output", s.connector]
        if s.primary:
            argv += ["--primary"]
        argv += ["--mode", s.mode] if s.mode != "auto" else ["--auto"]
        if s.pos is not None:
            argv += ["--pos", f"{s.pos[0]}x{s.pos[1]}"]
        elif s.rel is not None:
            argv += [f"--{s.rel[0]}", s.rel[1]]
        argv += ["--scale", s.scale]
        if s.rotate:
            argv += ["--rotate", s.rotate]
    return argv
