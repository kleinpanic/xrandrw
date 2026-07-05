from __future__ import annotations
import hashlib, json, logging, time
from pathlib import Path
from typing import Dict, List, Optional

from xrandrw.xrandr import Output
from xrandrw.logging_utils import logev

STATE_DIR = Path.home() / ".local/share/xrandrw"
STATE_PATH = STATE_DIR / "state.json"

def _now() -> float:
    return time.time()

def _new_profile_id(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()[:16]

def load_state() -> Dict[str, dict]:
    try:
        if STATE_PATH.is_file():
            return json.loads(STATE_PATH.read_text())
    except Exception:
        pass
    return {"profiles": {}, "identity_map": {}}

def save_state(st: Dict[str, dict]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st, indent=2, sort_keys=True))
    except Exception:
        pass

def ident_keys(o: Output) -> List[str]:
    keys = []
    if o.edid_sha1:
        keys.append("edid:" + o.edid_sha1)
    keys.append("conn:" + o.name)
    return keys

def ensure_profile(o: Output, st: Dict[str, dict], logger: logging.Logger, default_side: str) -> str:
    imap = st.setdefault("identity_map", {})
    profs = st.setdefault("profiles", {})
    keys = ident_keys(o)
    targets = [imap[k] for k in keys if k in imap]
    target: Optional[str] = None
    if targets:
        target = targets[0]
        # merge if multiple profiles map
        for pid in targets[1:]:
            if pid != target:
                src = profs.get(pid, {})
                dst = profs.setdefault(target, {})
                dst.setdefault("names", [])
                dst["names"] = sorted(set(dst["names"]) | set(src.get("names", [])))
                if not dst.get("edid") and src.get("edid"):
                    dst["edid"] = src["edid"]
                if not dst.get("preferred_side") and src.get("preferred_side"):
                    dst["preferred_side"] = src["preferred_side"]
                profs.pop(pid, None)
                for k, v in list(imap.items()):
                    if v == pid:
                        imap[k] = target
                logev(logger, logging.INFO, "profile_merge", "Merged profiles", keep=target, merged=pid)
    if not target:
        seed = o.edid_sha1 or o.name
        target = _new_profile_id(seed)
        profs[target] = {
            "names": [o.name],
            "edid": o.edid_sha1,
            "preferred_side": default_side,
            "last_seen": _now(),
        }
        logev(logger, logging.INFO, "profile_new", "New profile", profile=target, connector=o.name, edid=o.edid_sha1)
    for k in keys:
        if imap.get(k) != target:
            imap[k] = target
            logev(logger, logging.DEBUG, "identity_link", "Identity linked", identity=k, profile=target)
    p = profs[target]
    if o.name not in p["names"]:
        p["names"].append(o.name)
    p["edid"] = p.get("edid") or o.edid_sha1
    p["last_seen"] = _now()
    return target

def get_profile(st: Dict[str, dict], pid: str) -> dict:
    return st["profiles"].setdefault(pid, {})
