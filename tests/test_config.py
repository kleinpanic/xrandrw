from __future__ import annotations
from pathlib import Path

import pytest

from xrandrw import config
from xrandrw.config import _coerce_int, load_config, resolve_lock_dir


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONF_SYS", tmp_path / "nonexistent-sys.conf")
    monkeypatch.setattr(config, "CONF_USER", tmp_path / "nonexistent-user.conf")
    for k in config.ENV_DEFAULTS:
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_resolve_lock_dir_fallbacks(monkeypatch, tmp_path):
    xrd = tmp_path / "runtime"
    xrd.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xrd))
    assert resolve_lock_dir() == xrd

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(config.os, "getuid", lambda: 999999999)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    result = resolve_lock_dir()
    assert result == home / ".local/share/xrandrw"
    assert result.is_dir()
    assert result != Path("/tmp")


@pytest.mark.parametrize(
    "raw, default, minimum, use_float, expected, expect_warn",
    [
        ("3200", "3200", 0, False, 3200, False),
        ("3200px", "3200", 0, False, 3200, True),
        ("", "3200", 0, False, 3200, True),
        ("0.5", "1", 1, True, 1, False),
        ("2", "4", 4, False, 4, False),
    ],
)
def test_coerce_int_fallback(raw, default, minimum, use_float, expected, expect_warn):
    value, warning = _coerce_int(raw, default, minimum, use_float)
    assert value == expected
    assert (warning is not None) == expect_warn


def test_load_config_returns_warnings(monkeypatch, isolated_config):
    monkeypatch.setenv("HIDPI_WIDTH", "bad")
    env, warnings = load_config()
    assert any("HIDPI_WIDTH" in w for w in warnings)
    assert env["HIDPI_WIDTH"] == config.ENV_DEFAULTS["HIDPI_WIDTH"]


def test_load_config_lockfile_resolution(monkeypatch, isolated_config):
    monkeypatch.delenv("LOCKFILE", raising=False)
    env, _ = load_config()
    assert env["LOCKFILE"] == str(resolve_lock_dir() / "xrandrw.lock")
    assert not env["LOCKFILE"].startswith("/tmp")
    assert env["STATE_LOCKFILE"].endswith("xrandrw.state.lock")

    monkeypatch.setenv("LOCKFILE", "/custom/path/my.lock")
    env2, _ = load_config()
    assert env2["LOCKFILE"] == "/custom/path/my.lock"
    assert env2["STATE_LOCKFILE"].endswith("xrandrw.state.lock")
