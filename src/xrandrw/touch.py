from __future__ import annotations
import logging
import re

from xrandrw.logging_utils import run, logev

# Remap touchscreen input devices onto their display outputs after a layout change.
# xrandrw moves outputs, and `xinput map-to-output` bakes the output geometry in at
# call time, so a fixed login-time mapping goes stale on every apply. Recomputing it
# here — right after placement — keeps absolute touch coordinates aligned to the panel
# wherever it lands. Driven by the TOUCH_MAP config key; a no-op (no xinput dependency)
# when unset.

def parse_touch_map(env: dict[str, str]) -> list[tuple[str, str]]:
    # TOUCH_MAP="ft5x06:DSI-1;ELAN Touchscreen:eDP-1" -> [("ft5x06","DSI-1"), ...]
    # rpartition on ':' so device names may themselves contain a colon.
    pairs: list[tuple[str, str]] = []
    for chunk in (env.get("TOUCH_MAP", "") or "").split(";"):
        chunk = chunk.strip()
        if ":" not in chunk:
            continue
        name, _, output = chunk.rpartition(":")
        if name.strip() and output.strip():
            pairs.append((name.strip(), output.strip()))
    return pairs

def resolve_touch_remaps(mappings: list[tuple[str, str]],
                         devices: list[tuple[int, str]],
                         connected: set) -> list[tuple[int, str]]:
    # Pure: for each (name-substr, output) whose output is currently connected, match the
    # first input device whose name contains the substring (case-insensitive). One device
    # per mapping. Skips mappings whose output is absent so we never map onto a dead head.
    out: list[tuple[int, str]] = []
    for substr, output in mappings:
        if output not in connected:
            continue
        for did, name in devices:
            if substr.lower() in name.lower():
                out.append((did, output))
                break
    return out

def _list_input_devices(logger: logging.Logger) -> list[tuple[int, str]]:
    cp = run(["xinput", "list", "--short"], logger=logger)
    devices: list[tuple[int, str]] = []
    if cp.returncode != 0:
        return devices
    for line in cp.stdout.splitlines():
        m = re.search(r"\bid=(\d+)", line)
        if not m:
            continue
        name = re.sub(r"[⎡⎜⎣↳~|]+", " ", line[: m.start()]).strip()
        devices.append((int(m.group(1)), name))
    return devices

def remap_touch(env: dict[str, str], connected: set, logger: logging.Logger) -> None:
    mappings = parse_touch_map(env)
    if not mappings:
        return
    devices = _list_input_devices(logger)
    for did, output in resolve_touch_remaps(mappings, devices, connected):
        logev(logger, logging.INFO, "touch_remap", "map touch device to output",
              device=did, output=output)
        run(["xinput", "map-to-output", str(did), output], logger=logger)
