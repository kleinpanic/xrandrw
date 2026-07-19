from __future__ import annotations
import os
from pathlib import Path

from xrandrw.logging_utils import _LEVEL_MAP

CONF_SYS = Path("/etc/xdg/xrandrw.conf")
CONF_USER = Path.home() / ".config/xrandrw.conf"

ENV_DEFAULTS = {
    "USE_XWALLPAPER": "0",                 # 0=feh/fehbg, 1=xwallpaper
    "WALL": str(Path.home() / ".local/share/wallpapers/space.jpg"),
    "HIDPI_WIDTH": "3200",
    "POLL_INTERVAL": "45",                 # seconds; now slow safety-net timeout (D-06), not a tight loop
    "APPLY_BACKEND": "subprocess",         # subprocess (default, tested) | native (opt-in, seam-stub) — D-03
    "WALLPAPER_ENGINE": "",                # ""=auto-detect | feh | fehbg | xwallpaper | native (D-04/D-05)
    "LOG_LEVEL": "notice",                 # none|err|info|notice|debug
    "LOG_FILE": "",                        # optional file path (JSON lines)
    "LOCKFILE": "/tmp/xrandrw.lock",
    "PREF_DEFAULT_SIDE": "right-of",       # default side for unknown display
    "TOUCH_MAP": "",                       # ""=off | "devname:OUTPUT;..." remap touch after each apply
    "EXCESS_WINDOW_SEC": "20",             # burst window
    "EXCESS_THRESHOLD": "4",               # applies within window -> warn+backoff
    "WINDOW_MANAGEMENT": "0",              # 0=off (opt-in) / 1=enable dwm-ipc window relocation (WM-07)
}

def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k] = v
    return env

# Runtime lock directory: per-user, never world-writable /tmp (HARD-02).
def resolve_lock_dir() -> Path:
    xrd = os.environ.get("XDG_RUNTIME_DIR")
    if xrd and Path(xrd).is_dir():
        return Path(xrd)
    run_user = Path(f"/run/user/{os.getuid()}")
    if run_user.is_dir():
        return run_user
    d = Path.home() / ".local/share/xrandrw"
    d.mkdir(parents=True, exist_ok=True)
    return d

# Pure numeric guard: malformed config degrades to default instead of crashing (D-05).
def _coerce_int(raw: str, default: str, minimum: int, use_float: bool = False) -> tuple[int, str | None]:
    try:
        v = int(float(raw)) if use_float else int(raw)
        return max(minimum, v), None
    except (ValueError, TypeError):
        return max(minimum, int(default)), f"invalid value {raw!r}, using default {default!r}"

def load_config() -> tuple[dict[str, str], list[str]]:
    env = dict(ENV_DEFAULTS)
    env.update(_load_env_file(CONF_SYS))
    env.update(_load_env_file(CONF_USER))
    for k in ENV_DEFAULTS:
        if k in os.environ:
            env[k] = os.environ[k]
    warnings: list[str] = []

    def coerce(key: str, minimum: int, use_float: bool = False) -> str:
        v, w = _coerce_int(env[key], ENV_DEFAULTS[key], minimum, use_float)
        if w is not None:
            warnings.append(f"{key}: {w}")
        return str(v)

    env["USE_XWALLPAPER"] = "1" if env["USE_XWALLPAPER"] in ("1", "true", "yes") else "0"
    env["WINDOW_MANAGEMENT"] = "1" if env["WINDOW_MANAGEMENT"] in ("1", "true", "yes") else "0"
    env["HIDPI_WIDTH"] = coerce("HIDPI_WIDTH", 0)
    env["POLL_INTERVAL"] = coerce("POLL_INTERVAL", 5, use_float=True)
    if env["APPLY_BACKEND"] not in ("subprocess", "native"):
        env["APPLY_BACKEND"] = "subprocess"
    if env["WALLPAPER_ENGINE"].strip().lower() not in ("", "feh", "fehbg", "xwallpaper", "native"):
        env["WALLPAPER_ENGINE"] = ""
    if env["LOG_LEVEL"] not in _LEVEL_MAP:
        env["LOG_LEVEL"] = "notice"
    env["EXCESS_WINDOW_SEC"] = coerce("EXCESS_WINDOW_SEC", 5)
    env["EXCESS_THRESHOLD"] = coerce("EXCESS_THRESHOLD", 2)

    if env["LOCKFILE"] == ENV_DEFAULTS["LOCKFILE"]:
        env["LOCKFILE"] = str(resolve_lock_dir() / "xrandrw.lock")
    env["STATE_LOCKFILE"] = str(resolve_lock_dir() / "xrandrw.state.lock")
    return env, warnings
