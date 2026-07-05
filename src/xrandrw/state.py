from __future__ import annotations
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
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

def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(obj, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())          # durability: data on disk before the rename
        os.replace(tmp, path)             # atomic swap on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)                # don't leak the temp file on failure
        except OSError:
            pass
        raise

def save_state(st: Dict[str, dict], path: Path = STATE_PATH) -> None:
    try:
        _atomic_write_json(path, st)
    except Exception as e:
        # D-01/A3: a failed atomic write must be visible, not silently swallowed;
        # log ERROR and continue (do NOT abort the caller's apply pass).
        logging.getLogger("xrandrw").error("state_write_fail: %s", e)

def _open_lock_fd(path) -> int:
    # O_NOFOLLOW refuses a symlinked final component (CWE-59 -> OSError ELOOP).
    return os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)

@contextmanager
def state_lock(lock_path):
    # Load-bearing invariant (RESEARCH Pattern 3): the state-lock is acquired INNER,
    # never while waiting for the apply-lock. apply_once holds the apply-lock from
    # outside before taking this; set_pref takes ONLY this lock. => no cycle, no deadlock (D-03a).
    # Separate, never-replaced lock file: os.replace on state.json swaps its inode,
    # so an flock on state.json itself would NOT serialize across writers.
    fd = _open_lock_fd(lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # blocking; the RMW critical section is short
        yield
    finally:
        os.close(fd)                     # closing the fd releases the flock

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
