from __future__ import annotations
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from xrandrw.logging_utils import run, logev

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

def topology_hash(logger: Optional[logging.Logger] = None) -> str:
    cp = run(["xrandr", "--query"], logger=logger)
    lines = []
    for ln in cp.stdout.splitlines():
        if " connected" in ln or " disconnected" in ln or "*" in ln:
            lines.append(ln.strip())
    h = hashlib.sha1("\n".join(lines).encode()).hexdigest()
    return h
