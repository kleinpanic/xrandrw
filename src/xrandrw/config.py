from __future__ import annotations
import os
from pathlib import Path
from typing import Dict

from xrandrw.logging_utils import _LEVEL_MAP

CONF_SYS = Path("/etc/xdg/xrandrw.conf")
CONF_USER = Path.home() / ".config/xrandrw.conf"

ENV_DEFAULTS = {
    "USE_XWALLPAPER": "0",                 # 0=feh/fehbg, 1=xwallpaper
    "WALL": str(Path.home() / ".local/share/wallpapers/space.jpg"),
    "HIDPI_WIDTH": "3200",
    "POLL_INTERVAL": "1",                  # seconds; debounced internally
    "LOG_LEVEL": "notice",                 # none|err|info|notice|debug
    "LOG_FILE": "",                        # optional file path (JSON lines)
    "LOCKFILE": "/tmp/xrandrw.lock",
    "PREF_DEFAULT_SIDE": "right-of",       # default side for unknown display
    "EXCESS_WINDOW_SEC": "20",             # burst window
    "EXCESS_THRESHOLD": "4",               # applies within window -> warn+backoff
}

def _load_env_file(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip(); v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        env[k] = v
    return env

def load_config() -> Dict[str, str]:
    env = dict(ENV_DEFAULTS)
    env.update(_load_env_file(CONF_SYS))
    env.update(_load_env_file(CONF_USER))
    for k in ENV_DEFAULTS.keys():
        if k in os.environ:
            env[k] = os.environ[k]
    env["USE_XWALLPAPER"] = "1" if env["USE_XWALLPAPER"] in ("1", "true", "yes") else "0"
    env["HIDPI_WIDTH"] = str(int(env["HIDPI_WIDTH"]))
    env["POLL_INTERVAL"] = str(max(1, int(float(env["POLL_INTERVAL"]))))
    if env["LOG_LEVEL"] not in _LEVEL_MAP:
        env["LOG_LEVEL"] = "notice"
    env["EXCESS_WINDOW_SEC"] = str(max(5, int(env["EXCESS_WINDOW_SEC"])))
    env["EXCESS_THRESHOLD"] = str(max(2, int(env["EXCESS_THRESHOLD"])))
    return env
