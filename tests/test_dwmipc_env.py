"""Env-coercion hardening tests for dwmipc (SEC-01 robustness).

Non-finite env values (``inf`` / ``-inf`` / ``nan`` / an over-range literal like
``1e400`` that ``float()`` rounds to ``inf``) must NOT crash the daemon. Before
the fix ``int(float("1e400"))`` raised ``OverflowError`` at import time (taking
down ``import xrandrw.dwmipc`` on startup) and an infinite ``DWMIPC_TIMEOUT``
both crashed ``sock.settimeout(inf)`` and silently defeated the hang guard. These
tests prove every hostile value degrades to the documented default and that the
resulting timeout is a finite value ``settimeout`` accepts.
"""
from __future__ import annotations

import math
import os
import socket
import subprocess
import sys

import pytest

from xrandrw import dwmipc


_NON_FINITE = ["1e400", "inf", "Inf", "Infinity", "-inf", "-Infinity", "nan", "NaN"]


@pytest.mark.parametrize("raw", _NON_FINITE)
def test_env_float_non_finite_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("DWMIPC_TIMEOUT", raw)
    v = dwmipc._env_float("DWMIPC_TIMEOUT", 1.0, 0.001)
    assert v == 1.0
    assert math.isfinite(v)
    # The fallback value must be one settimeout() accepts (inf raises OverflowError).
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(v)  # must not raise
    finally:
        s.close()


@pytest.mark.parametrize("raw", _NON_FINITE)
def test_env_int_non_finite_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("DWMIPC_MAX_REPLY", raw)
    v = dwmipc._env_int("DWMIPC_MAX_REPLY", 8 * 1024 * 1024, 1)
    assert v == 8 * 1024 * 1024
    assert isinstance(v, int)


def test_env_still_coerces_normal_values(monkeypatch):
    monkeypatch.setenv("DWMIPC_TIMEOUT", "2.5")
    monkeypatch.setenv("DWMIPC_MAX_REPLY", "1024")
    assert dwmipc._env_float("DWMIPC_TIMEOUT", 1.0, 0.001) == 2.5
    assert dwmipc._env_int("DWMIPC_MAX_REPLY", 8 * 1024 * 1024, 1) == 1024


@pytest.mark.parametrize("raw", ["1e400", "inf", "Infinity", "-inf", "nan"])
def test_module_import_with_non_finite_env_does_not_crash(raw):
    # Import the module in a fresh subprocess with the hostile env in place: the
    # module-level DEFAULT_TIMEOUT / MAX_REPLY_SIZE constants are computed at
    # import, so a non-finite value must not raise at import (which would take
    # down the whole daemon at startup). Isolated in a child so it cannot mutate
    # this process's already-imported module state.
    env = dict(os.environ, DWMIPC_TIMEOUT=raw, DWMIPC_MAX_REPLY=raw)
    src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import math, xrandrw.dwmipc as d\n"
        "assert math.isfinite(d.DEFAULT_TIMEOUT), d.DEFAULT_TIMEOUT\n"
        "assert d.DEFAULT_TIMEOUT == 1.0, d.DEFAULT_TIMEOUT\n"
        "assert d.MAX_REPLY_SIZE == 8 * 1024 * 1024, d.MAX_REPLY_SIZE\n"
        "import socket\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "s.settimeout(d.DEFAULT_TIMEOUT)\n"  # must not raise OverflowError
        "s.close()\n"
    )
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
