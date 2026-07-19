from __future__ import annotations
from pathlib import Path

import pytest

from xrandrw import config
from xrandrw.config import _coerce_int, _load_env_file, load_config, resolve_lock_dir


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


def test_load_config_lean_poll_default(monkeypatch, isolated_config):
    env, _ = load_config()
    assert int(env["POLL_INTERVAL"]) >= 30


def test_load_config_apply_backend_default(monkeypatch, isolated_config):
    env, _ = load_config()
    assert env["APPLY_BACKEND"] == "subprocess"


def test_load_config_apply_backend_bogus_fallback(monkeypatch, isolated_config):
    monkeypatch.setenv("APPLY_BACKEND", "bogus")
    env, _ = load_config()
    assert env["APPLY_BACKEND"] == "subprocess"


def test_load_config_apply_backend_native_preserved(monkeypatch, isolated_config):
    monkeypatch.setenv("APPLY_BACKEND", "native")
    env, _ = load_config()
    assert env["APPLY_BACKEND"] == "native"


def test_load_env_file_parsing(tmp_path):
    conf = tmp_path / "x.conf"
    conf.write_text(
        "# comment\n"
        "\n"
        "not-a-kv-line\n"
        "KEY1=plain\n"
        "KEY2='single quoted'\n"
        'KEY3="double quoted"\n'
        "KEY4 = spaced \n"
    )
    assert _load_env_file(conf) == {
        "KEY1": "plain",
        "KEY2": "single quoted",
        "KEY3": "double quoted",
        "KEY4": "spaced",
    }


def test_load_env_file_missing(tmp_path):
    assert _load_env_file(tmp_path / "nope.conf") == {}


def test_missing_conf_files_yield_defaults(isolated_config):
    env, warnings = load_config()
    assert warnings == []
    assert env["PREF_DEFAULT_SIDE"] == config.ENV_DEFAULTS["PREF_DEFAULT_SIDE"]
    assert env["HIDPI_WIDTH"] == config.ENV_DEFAULTS["HIDPI_WIDTH"]
    assert env["WALLPAPER_ENGINE"] == ""


def test_conf_file_and_env_precedence(monkeypatch, isolated_config, tmp_path):
    sys_conf = tmp_path / "sys.conf"
    user_conf = tmp_path / "user.conf"
    sys_conf.write_text("HIDPI_WIDTH=1000\nPOLL_INTERVAL=50\n")
    user_conf.write_text('HIDPI_WIDTH="1500"\n')
    monkeypatch.setattr(config, "CONF_SYS", sys_conf)
    monkeypatch.setattr(config, "CONF_USER", user_conf)

    env, warnings = load_config()
    assert warnings == []
    assert env["HIDPI_WIDTH"] == "1500", "user conf must beat sys conf"
    assert env["POLL_INTERVAL"] == "50", "sys conf value survives where user conf is silent"

    monkeypatch.setenv("HIDPI_WIDTH", "2000")
    env2, _ = load_config()
    assert env2["HIDPI_WIDTH"] == "2000", "process env must beat both conf files"


def test_conf_file_malformed_numeric_warns(monkeypatch, isolated_config, tmp_path):
    user_conf = tmp_path / "user.conf"
    user_conf.write_text("POLL_INTERVAL=fast\n")
    monkeypatch.setattr(config, "CONF_USER", user_conf)

    env, warnings = load_config()
    assert env["POLL_INTERVAL"] == config.ENV_DEFAULTS["POLL_INTERVAL"]
    assert any("POLL_INTERVAL" in w for w in warnings)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", "1"),
        ("true", "1"),
        ("yes", "1"),
        ("0", "0"),
        ("bogus", "0"),
        ("", "0"),
    ],
)
def test_window_management_coerce(monkeypatch, isolated_config, raw, expected):
    monkeypatch.setenv("WINDOW_MANAGEMENT", raw)
    env, _ = load_config()
    assert env["WINDOW_MANAGEMENT"] == expected


def test_window_management_default_off(isolated_config):
    env, _ = load_config()
    assert env["WINDOW_MANAGEMENT"] == "0"


def test_window_management_precedence(monkeypatch, isolated_config, tmp_path):
    sys_conf = tmp_path / "sys.conf"
    user_conf = tmp_path / "user.conf"
    sys_conf.write_text("WINDOW_MANAGEMENT=1\n")  # user conf silent
    user_conf.write_text("HIDPI_WIDTH=1500\n")
    monkeypatch.setattr(config, "CONF_SYS", sys_conf)
    monkeypatch.setattr(config, "CONF_USER", user_conf)

    env, _ = load_config()
    assert env["WINDOW_MANAGEMENT"] == "1", "sys conf value survives where user conf is silent"

    monkeypatch.setenv("WINDOW_MANAGEMENT", "0")
    env2, _ = load_config()
    assert env2["WINDOW_MANAGEMENT"] == "0", "process env must beat conf files"


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
