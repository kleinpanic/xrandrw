#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xrandrw — robust, zero-config X11 display policy for dwm/i3/etc.

- Journald + JSON file logging (leveled, structured)
- Debounced, low-CPU watch with excess-activity warnings
- Graceful SIGINT/SIGTERM
- Persistent profile linking: EDID <-> connector (merged)
- Persistent attach order (stack) for deterministic placement
- xwallpaper/feh fallback + reapply
"""

from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------- config (env or conf files) ----------------

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

# ---------------- logging ----------------

_LEVEL_MAP = {
    "none": logging.CRITICAL + 1,
    "err": logging.ERROR,
    "info": logging.INFO,
    "notice": logging.INFO,  # treat notice≈info
    "debug": logging.DEBUG,
}

# Reserved LogRecord keys (cannot be overridden via `extra`)
_LOGREC_RESERVED = {
    "name","msg","args","levelname","levelno","pathname","filename","module",
    "exc_info","exc_text","stack_info","lineno","funcName","created","msecs",
    "relativeCreated","thread","threadName","processName","process"
}

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "lvl": record.levelname.lower(),
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k.startswith("_") or k in _LOGREC_RESERVED:
                continue
            payload[k] = v
        return json.dumps(payload, separators=(",", ":"))

def _setup_logging(env: Dict[str, str]) -> logging.Logger:
    logger = logging.getLogger("xrandrw")
    logger.setLevel(_LEVEL_MAP.get(env["LOG_LEVEL"], logging.INFO))
    added = False
    # journald
    try:
        from systemd.journal import JournalHandler  # type: ignore
        jh = JournalHandler(SYSLOG_IDENTIFIER="xrandrw")
        jh.setLevel(logger.level)
        logger.addHandler(jh)
        added = True
    except Exception:
        pass
    # file (JSON lines)
    if env.get("LOG_FILE"):
        Path(env["LOG_FILE"]).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(env["LOG_FILE"])
        fh.setLevel(logger.level)
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)
        added = True
    # stderr fallback (human format)
    if not added:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logger.level)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        logger.addHandler(ch)
    return logger

def _sanitize_extra(fields: Dict[str, object]) -> Dict[str, object]:
    # Avoid reserved LogRecord attrs; prefix collisions with "field_"
    out = {}
    for k, v in fields.items():
        out[k if k not in _LOGREC_RESERVED else f"field_{k}"] = v
    return out

def _kv(**pairs) -> str:
    # compact " key=val" for human-readable journald while keeping structured fields
    return "".join(f" {k}={pairs[k]}" for k in pairs if pairs[k] is not None)

def loge(logger: logging.Logger, level: int, event: str, msg: str, **fields):
    logger.log(level, msg, extra={"event": event, **_sanitize_extra(fields)})

def logev(logger: logging.Logger, level: int, event: str, msg: str, **fields):
    # same as loge, but appends k=v to the message for journalctl -f readability
    msg2 = msg + _kv(**fields)
    logger.log(level, msg2, extra={"event": event, **_sanitize_extra(fields)})

# ---------------- utils ----------------

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

def run(cmd: List[str], logger: Optional[logging.Logger] = None, check=False, capture=True, env=None) -> subprocess.CompletedProcess:
    if logger and logger.isEnabledFor(logging.DEBUG):
        logger.debug("run: %s", shlex.join(cmd), extra={"event": "exec"})
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=env)

def wait_for_x(logger: logging.Logger) -> None:
    for _ in range(20):  # ~10s total
        try:
            if run(["xset", "q"]).returncode == 0:
                return
        except Exception:
            pass
        time.sleep(0.5)
    loge(logger, logging.INFO, "x_wait", "X not responsive; continuing")

# ---------------- xrandr / EDID ----------------

_MODE_RE = re.compile(r"^\s*(\d+)x(\d+)\s+([0-9.]+)([*+]*)")
_OUT_HEAD_RE = re.compile(r"^(\S+)\s+(connected|disconnected)(?:\s+primary)?(?:\s+(\d+)x(\d+)\+\d+\+\d+)?")

@dataclass
class Output:
    name: str
    connected: bool
    primary: bool = False
    current_mode: Optional[Tuple[int, int]] = None
    modes: List[Tuple[int, int, float, str]] = field(default_factory=list)  # (w,h,rate,flags "*+")
    edid_sha1: Optional[str] = None

def parse_xrandr_query(text: str) -> Dict[str, Output]:
    outs: Dict[str, Output] = {}
    cur: Optional[Output] = None
    for line in text.splitlines():
        m = _OUT_HEAD_RE.match(line)
        if m:
            name, status, cw, ch = m.groups()
            cur = Output(name=name, connected=(status == "connected"))
            cur.primary = " primary " in (line + " ")
            if cw and ch:
                try:
                    cur.current_mode = (int(cw), int(ch))
                except Exception:
                    pass
            outs[name] = cur
            continue
        if cur is None:
            continue
        mm = _MODE_RE.match(line)
        if mm:
            w, h, rate, flags = mm.groups()
            cur.modes.append((int(w), int(h), float(rate), flags))
            if "*" in flags:
                cur.current_mode = (int(w), int(h))
    return outs

def read_xrandr(logger: logging.Logger) -> Dict[str, Output]:
    cp = run(["xrandr", "--query"], logger=logger)
    if cp.returncode != 0:
        raise RuntimeError("xrandr --query failed")
    outs = parse_xrandr_query(cp.stdout)
    return outs

def edid_sysfs_read(name: str) -> Optional[bytes]:
    base = Path("/sys/class/drm")
    for p in base.glob(f"card*-{name}/edid"):
        try:
            return p.read_bytes()
        except Exception:
            pass
    return None

def read_edids(outs: Dict[str, Output], logger: logging.Logger) -> None:
    # sysfs first, then xrandr --prop
    for n, o in outs.items():
        if not o.connected:
            continue
        raw = edid_sysfs_read(n)
        if raw:
            o.edid_sha1 = hashlib.sha1(raw).hexdigest()
            logev(logger, logging.DEBUG, "edid_sysfs", "EDID via sysfs", output=n, sha1=o.edid_sha1)
    need = [n for n, o in outs.items() if o.connected and not o.edid_sha1]
    if not need:
        return
    try:
        cp = run(["xrandr", "--prop"], logger=logger)
        text = cp.stdout
    except Exception as e:
        logev(logger, logging.DEBUG, "edid_prop_fail", "xrandr --prop failed", error=str(e))
        return
    cur: Optional[str] = None
    collecting = False
    buf: List[str] = []
    for line in text.splitlines():
        m = re.match(r"^(\S+)\s+(connected|disconnected)", line)
        if m:
            # finalize previous section when we hit a new output header
            if cur and buf:
                hexstr = "".join(s.strip() for s in buf if s.strip())
                try:
                    blob = bytes.fromhex(hexstr)
                    sha1 = hashlib.sha1(blob).hexdigest()
                    if cur in outs and outs[cur].connected and not outs[cur].edid_sha1:
                        outs[cur].edid_sha1 = sha1
                        logev(logger, logging.DEBUG, "edid_xrandr", "EDID via xrandr --prop", output=cur, sha1=sha1)
                except Exception:
                    pass
            cur = m.group(1)
            collecting = False
            buf = []
            continue
        if cur and line.strip().startswith("EDID:"):
            collecting = True
            buf = []
            continue
        if collecting:
            if line.startswith("\t"):
                buf.append(line.strip())
            else:
                collecting = False
    # finalize the very last EDID block (otherwise it would be dropped)
    if cur and buf:
        hexstr = "".join(s.strip() for s in buf if s.strip())
        try:
            blob = bytes.fromhex(hexstr)
            sha1 = hashlib.sha1(blob).hexdigest()
            if cur in outs and outs[cur].connected and not outs[cur].edid_sha1:
                outs[cur].edid_sha1 = sha1
                logev(logger, logging.DEBUG, "edid_xrandr", "EDID via xrandr --prop", output=cur, sha1=sha1)
        except Exception:
            pass

# ---------------- topology hash ----------------

def topology_hash(logger: Optional[logging.Logger] = None) -> str:
    cp = run(["xrandr", "--query"], logger=logger)
    lines = []
    for ln in cp.stdout.splitlines():
        if " connected" in ln or " disconnected" in ln or "*" in ln:
            lines.append(ln.strip())
    h = hashlib.sha1("\n".join(lines).encode()).hexdigest()
    return h

# ---------------- state: persistent profile linking ----------------

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

# ---------------- policy ----------------

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

# ---------------- xrandr apply helpers ----------------

def xrandr_output_off(connector: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--off"], logger=logger)

def xrandr_reset(connector: str, logger: logging.Logger):
    # Keep for explicit callers if ever needed; no longer used pre-apply for connected outputs
    run(["xrandr", "--output", connector, "--panning", "0x0"], logger=logger)

def xrandr_auto_primary_scale(connector: str, scale: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--auto", "--scale", scale, "--panning", "0x0", "--primary"], logger=logger)

def xrandr_auto_pos(connector: str, rel_opt: str, anchor: str, logger: logging.Logger):
    run(["xrandr", "--output", connector, "--auto", "--scale", "1x1", "--panning", "0x0", f"--{rel_opt}", anchor], logger=logger)

def xrandr_rotate_left_if_portrait(connector: str, o: Output, logger: logging.Logger):
    m = current_or_preferred_mode(o)
    if m:
        w, h = m
        if h > w:
            run(["xrandr", "--output", connector, "--rotate", "left"], logger=logger)

def reapply_wallpaper(env: Dict[str, str], logger: logging.Logger):
    wall = env["WALL"]
    if env["USE_XWALLPAPER"] == "1":
        if Path(wall).is_file() and shutil.which("xwallpaper"):
            run(["xwallpaper", "--zoom", wall], logger=logger)
            logev(logger, logging.INFO, "wallpaper", "xwallpaper", file=wall)
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "xwallpaper missing or file not found", file=wall)
    else:
        if shutil.which("fehbg"):
            run(["fehbg"], logger=logger)
            loge(logger, logging.INFO, "wallpaper", "fehbg")
        elif shutil.which("feh") and Path(wall).is_file():
            run(["feh", "--no-fehbg", "--bg-fill", wall], logger=logger)
            logev(logger, logging.INFO, "wallpaper", "feh", file=wall)
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "no feh/fehbg or file", file=wall)

def scrub_stale(outs: Dict[str, Output], logger: logging.Logger):
    # Only power off disconnected heads; avoid pre-apply resets that blank active screens
    for connector, o in outs.items():
        if not o.connected:
            xrandr_output_off(connector, logger)

# ---------------- apply core ----------------

def apply_once(env: Dict[str, str], logger: logging.Logger, event_source: str = "manual") -> None:
    import fcntl
    lockfile = env["LOCKFILE"]
    with open(lockfile, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            loge(logger, logging.INFO, "apply_skip", "Another apply is running")
            return

        wait_for_x(logger)

        try:
            outs = read_xrandr(logger)
        except Exception as e:
            logev(logger, logging.ERROR, "xrandr_unavail", "xrandr not available", error=str(e))
            return

        logev(logger, logging.INFO, "apply_start", "apply: start", source=event_source)

        # Power off any newly disconnected heads only (avoid wiping transforms on active heads)
        scrub_stale(outs, logger)

        # reread + EDID
        outs = read_xrandr(logger)
        read_edids(outs, logger)

        connected = [o for o in outs.values() if o.connected]
        if not connected:
            loge(logger, logging.INFO, "apply_none", "no connected outputs found")
            reapply_wallpaper(env, logger)
            logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)
            return

        st = load_state()
        default_side = env["PREF_DEFAULT_SIDE"]
        attach_stack: List[str] = st.setdefault("attach_stack", [])  # profile ids, earliest->latest

        # prefer internal as primary
        internal = [o for o in connected if is_internal_lcd(o.name)]
        if internal:
            pnl = sorted(internal, key=lambda x: x.name)[0]
            hidpi_threshold = int(env["HIDPI_WIDTH"])
            cur = current_or_preferred_mode(pnl)
            width = (cur[0] if cur else 0)
            scale = "0.5x0.5" if width >= hidpi_threshold else "1x1"
            logev(logger, logging.INFO, "primary_set", "eDP/LVDS primary",
                  primary=pnl.name, mode=str(cur), scale=scale)
            xrandr_auto_primary_scale(pnl.name, scale, logger)

            exts = [o for o in connected if o.name != pnl.name]
            pid_by_output: Dict[str, str] = {}
            for o in exts:
                pid_by_output[o.name] = ensure_profile(o, st, logger, default_side)

            # update attach_stack: keep only currently connected pids, append new ones at end
            cur_pids = [pid_by_output[o.name] for o in exts]
            attach_stack = [pid for pid in attach_stack if pid in cur_pids]
            for pid in cur_pids:
                if pid not in attach_stack:
                    attach_stack.append(pid)
            st["attach_stack"] = attach_stack

            # assignment: newest gets right-of, previous left-of, then above, below
            desired_order = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
            sides_order = ["right-of", "left-of", "above", "below"]
            assigned_by_pid: Dict[str, str] = {}
            for pid, side in zip(desired_order, sides_order):
                assigned_by_pid[pid] = side

            # fallback for >4 externals or any remaining
            occupied: Dict[str, str] = {}
            for o in exts:
                pid = pid_by_output[o.name]
                side = assigned_by_pid.get(pid)
                if not side:
                    side = next((s for s in SIDES if s not in occupied), default_side)
                # reserve the side
                if side in occupied:
                    # pick a free one deterministically
                    side = next((s for s in SIDES if s not in occupied), side)
                occupied[side] = o.name
                logev(logger, logging.INFO, "place", "external placement (stack)",
                      output=o.name, side=side, anchor=pnl.name, profile=pid)
                xrandr_auto_pos(o.name, side, pnl.name, logger)
                xrandr_rotate_left_if_portrait(o.name, o, logger)
        else:
            # No internal; pick lexicographically first as primary
            first = sorted(connected, key=lambda x: x.name)[0]
            logev(logger, logging.INFO, "primary_set", "primary (no internal)", primary=first.name)
            run(["xrandr", "--output", first.name, "--auto", "--primary"], logger=logger)
            rest = [o for o in connected if o.name != first.name]

            pid_by_output: Dict[str, str] = {}
            for o in rest:
                pid_by_output[o.name] = ensure_profile(o, st, logger, default_side)

            # update attach_stack with current externals
            cur_pids = [pid_by_output[o.name] for o in rest]
            attach_stack = [pid for pid in st.setdefault("attach_stack", []) if pid in cur_pids]
            for pid in cur_pids:
                if pid not in attach_stack:
                    attach_stack.append(pid)
            st["attach_stack"] = attach_stack

            desired_order = list(reversed([pid for pid in attach_stack if pid in cur_pids]))
            sides_order = ["right-of", "left-of", "above", "below"]
            assigned_by_pid: Dict[str, str] = {pid: side for pid, side in zip(desired_order, sides_order)}

            occupied: Dict[str, str] = {}
            for o in rest:
                pid = pid_by_output[o.name]
                side = assigned_by_pid.get(pid)
                if not side:
                    side = next((s for s in SIDES if s not in occupied), default_side)
                if side in occupied:
                    side = next((s for s in SIDES if s not in occupied), side)
                occupied[side] = o.name
                logev(logger, logging.INFO, "place", "external placement (stack)",
                      output=o.name, side=side, anchor=first.name, profile=pid)
                xrandr_auto_pos(o.name, side, first.name, logger)
                xrandr_rotate_left_if_portrait(o.name, o, logger)

        save_state(st)
        reapply_wallpaper(env, logger)
        logev(logger, logging.INFO, "apply_done", "apply: done", source=event_source)

# ---------------- systemd notify / watchdog (harmless if Type=simple) ----------------

def _sd_notify(msg: str):
    addr = os.getenv("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":
        addr = "\0" + addr[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        try:
            s.connect(addr)
            s.send(msg.encode())
        except Exception:
            pass

def _watchdog_thread(stop_evt: threading.Event, logger: logging.Logger):
    usec = os.getenv("WATCHDOG_USEC")
    if not usec:
        return
    interval = int(int(usec) / 2 / 1_000_000) or 1
    while not stop_evt.wait(interval):
        _sd_notify("WATCHDOG=1")
        loge(logger, logging.DEBUG, "watchdog", "sd_notify WATCHDOG=1")

# ---------------- watch / daemon ----------------

stop_evt = threading.Event()

def _install_signals(logger: logging.Logger):
    def _sig(sig, frame):
        logev(logger, logging.INFO, "shutdown", "signal received", sig=sig)
        stop_evt.set()
        _sd_notify("STOPPING=1")
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

def watch_loop(env: Dict[str, str], logger: logging.Logger):
    poll = int(env["POLL_INTERVAL"])
    window = int(env["EXCESS_WINDOW_SEC"])
    threshold = int(env["EXCESS_THRESHOLD"])
    last = topology_hash(logger)
    logev(logger, logging.INFO, "watch_start", "watch: started", poll=f"{poll}s")
    change_times: List[float] = []
    backoff_add = 0
    while not stop_evt.is_set():
        cur = topology_hash(logger)
        if cur != last:
            now = time.monotonic()
            change_times.append(now)
            change_times = [t for t in change_times if now - t <= window]
            if len(change_times) > threshold:
                logev(logger, logging.WARNING, "watch_excess", "excess topology churn",
                      count=len(change_times), window=window)
                backoff_add = min(1000, backoff_add + 150)
            else:
                backoff_add = max(0, backoff_add - 50)
            logev(logger, logging.DEBUG, "watch_change", "topology hash changed",
                  debounce_ms=150+backoff_add)
            time.sleep((150 + backoff_add) / 1000.0)
            verify = topology_hash(logger)
            if verify != last:
                logev(logger, logging.INFO, "watch_apply", "apply on topology change",
                      source="watch_poll")
                apply_once(env, logger, event_source="watch_poll")
                last = verify
        stop_evt.wait(poll)

def spawn_xplugd(logger: logging.Logger):
    if shutil.which("xplugd"):
        script_path = os.path.abspath(sys.argv[0])
        logev(logger, logging.INFO, "daemon_xplugd", "launching xplugd", script=script_path)
        proc = subprocess.Popen(["xplugd", "-n", "-s", "-l", "notice", script_path])
        logev(logger, logging.INFO, "daemon_xplugd", "xplugd started", pid=proc.pid)
        return proc
    else:
        loge(logger, logging.INFO, "daemon_xplugd_skip", "xplugd not found; watcher only")
        return None

# ---------------- CLI ----------------

SIDES_VALID = ("right-of", "left-of", "above", "below")

def set_pref(env: Dict[str, str], output_or_id: str, side: str, logger: logging.Logger):
    if side not in SIDES_VALID:
        raise SystemExit(f"invalid side: {side} (valid: {', '.join(SIDES_VALID)})")
    outs = read_xrandr(logger)
    read_edids(outs, logger)
    st = load_state()
    matched: List[str] = []
    for o in outs.values():
        if not o.connected:
            continue
        pid = ensure_profile(o, st, logger, env["PREF_DEFAULT_SIDE"])
        if o.name == output_or_id or ("edid:"+o.edid_sha1 == output_or_id if o.edid_sha1 else False) or ("conn:"+o.name == output_or_id):
            get_profile(st, pid)["preferred_side"] = side
            matched.append(pid)
    if not matched and output_or_id in st.get("profiles", {}):
        get_profile(st, output_or_id)["preferred_side"] = side
        matched.append(output_or_id)
    if not matched:
        raise SystemExit(f"no such connected output or known id/profile: {output_or_id}")
    save_state(st)
    logev(logger, logging.INFO, "set_pref", "preferred side updated", side=side, profiles=",".join(matched))

def list_state():
    st = load_state()
    print(json.dumps(st, indent=2, sort_keys=True))

def _event_source_from_env() -> str:
    if os.getenv("ACTION") or os.getenv("OUTPUT"):
        return "xplugd"
    return "manual"

def main():
    env = load_config()
    logger = _setup_logging(env)
    _install_signals(logger)

    ap = argparse.ArgumentParser(description="xrandrw: robust display policy manager")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="apply once (default)")
    g.add_argument("--watch", action="store_true", help="poll topology and apply on change")
    g.add_argument("--daemon", action="store_true", help="spawn xplugd (if present) and watch")
    g.add_argument("--print", action="store_true", help="print xrandr --query and exit")
    ap.add_argument("--set-pref", nargs=2, metavar=("OUTPUT_OR_ID", "SIDE"),
                    help="set preferred side: right-of|left-of|above|below")
    ap.add_argument("--list-state", action="store_true", help="dump placement state JSON")
    args = ap.parse_args()

    if args.print:
        subprocess.run(["xrandr", "--query"])
        return 0
    if args.list_state:
        list_state()
        return 0
    if args.set_pref:
        set_pref(env, args.set_pref[0], args.set_pref[1], logger)
        return 0

    if args.daemon:
        logev(logger, logging.INFO, "daemon_start", "daemon: start",
              log_level=env["LOG_LEVEL"], wall=env["WALL"])
        wait_for_x(logger)
        child = spawn_xplugd(logger)
        wd_thread = threading.Thread(target=_watchdog_thread, args=(stop_evt, logger), daemon=True)
        wd_thread.start()
        try:
            apply_once(env, logger, event_source="daemon_boot")
            _sd_notify("READY=1")  # harmless if Type=simple
            watch_loop(env, logger)
        finally:
            if child and child.poll() is None:
                child.terminate()
            stop_evt.set()
        return 0

    if args.watch:
        wait_for_x(logger)
        # Initial apply so starting with already-plugged displays is handled
        apply_once(env, logger, event_source="watch_boot")
        wd_thread = threading.Thread(target=_watchdog_thread, args=(stop_evt, logger), daemon=True)
        wd_thread.start()
        try:
            watch_loop(env, logger)
        finally:
            stop_evt.set()
        return 0

    src = _event_source_from_env()
    apply_once(env, logger, event_source=src)
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _sd_notify("STOPPING=1")
        sys.exit(130)

