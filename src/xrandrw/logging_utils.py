from __future__ import annotations
import json
import logging
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

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
