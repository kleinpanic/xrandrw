"""Regression: no non-functional test may ever reach the live dwm socket or :0.

Phase-14 test-isolation gap (P0). A Phase-14 subagent's test/harness reached the
developer's REAL ``/tmp/dwm.sock`` on the REAL ``DISPLAY=:0`` and issued a mutating
``tagmon``, SIGSEGV-crashing the live dwm. Root cause: ``dwmipc.DEFAULT_SOCK_PATH``
is frozen to ``/tmp/dwm.sock`` at import (dwmipc.py:57) and the sole autouse guard
sandboxed only ``XDG_DATA_HOME`` — so a live ``available()``/``run_command()`` in a
non-fully-mocked unit test landed on the real dwm.

These tests pin the fix: the ``block_live_dwm`` autouse fixture (tests/conftest.py)
must redirect ``dwmipc.DEFAULT_SOCK_PATH`` + ``$DWM_SOCKET`` to a dead throwaway
socket and ``$DISPLAY`` to a dead display for every test WITHOUT the ``functional``
marker, and ``dwmipc.run_command`` must hard-refuse ``/tmp/dwm.sock`` under pytest.
"""
from __future__ import annotations

import os

import pytest

from xrandrw import dwmipc


def test_default_sock_path_is_not_live():
    # The frozen import-time default (/tmp/dwm.sock) must be overridden by the guard.
    assert dwmipc.DEFAULT_SOCK_PATH != "/tmp/dwm.sock"


def test_dwm_socket_env_is_throwaway():
    assert os.environ["DWM_SOCKET"] == dwmipc.DEFAULT_SOCK_PATH
    assert os.environ["DWM_SOCKET"] != "/tmp/dwm.sock"


def test_display_env_is_dead():
    # A stray in-process Xlib connect must fail closed on a dead display, never :0.
    assert os.environ["DISPLAY"] == ":99991"


def test_available_on_default_returns_false():
    # available() on the (guarded) default points at a non-existent throwaway
    # socket: it degrades to False and NEVER touches /tmp/dwm.sock. NEVER raises.
    assert dwmipc.available(dwmipc.DEFAULT_SOCK_PATH) is False


def test_run_command_on_default_cannot_reach_dwm():
    # The mutating verb that crashed live dwm (tagmon) must dead-end: the guarded
    # default has no socket file, so the connect fails and raises DwmIpcUnavailable
    # instead of ever reaching a real dwm.
    with pytest.raises(dwmipc.DwmIpcUnavailable):
        dwmipc.run_command("tagmon", 1, path=dwmipc.DEFAULT_SOCK_PATH)


def test_backstop_rejects_live_socket_under_pytest():
    # Hard defense-in-depth even if the fixture were bypassed: run_command refuses
    # to send to the world-writable /tmp/dwm.sock while PYTEST_CURRENT_TEST is set,
    # raising loudly BEFORE any connect so it can never be silently swallowed.
    assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest sentinel must be set under pytest"
    with pytest.raises(RuntimeError):
        dwmipc.run_command("tagmon", 1, path="/tmp/dwm.sock")
