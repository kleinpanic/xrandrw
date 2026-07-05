from __future__ import annotations
import errno
import json
import logging

import pytest

import xrandrw.state as state
from xrandrw.state import _atomic_write_json, _open_lock_fd, save_state, state_lock


def _tmp_residue(directory):
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_atomic_write_replaces(tmp_path):
    target = tmp_path / "state.json"
    obj = {"a": 1, "b": [2, 3]}
    _atomic_write_json(target, obj)
    assert json.loads(target.read_text()) == obj
    assert _tmp_residue(tmp_path) == []


def test_atomic_write_cleanup_on_error(tmp_path, monkeypatch):
    target = tmp_path / "state.json"

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(state.os, "replace", boom)
    with pytest.raises(OSError):
        _atomic_write_json(target, {"a": 1})
    assert _tmp_residue(tmp_path) == []
    assert not target.exists()


def test_atomic_write_same_dir(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    seen = {}
    real_mkstemp = state.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(state.tempfile, "mkstemp", spy)
    _atomic_write_json(target, {"a": 1})
    assert seen["dir"] == str(target.parent)


def test_save_state_injected_path(tmp_path):
    target = tmp_path / "state.json"
    st = {"profiles": {"p": {"names": ["HDMI-1"]}}, "identity_map": {}}
    save_state(st, path=target)
    assert json.loads(target.read_text()) == st


def test_save_state_failure_logged(tmp_path, monkeypatch, caplog):
    target = tmp_path / "state.json"

    def boom(path, obj):
        raise OSError("disk full")

    monkeypatch.setattr(state, "_atomic_write_json", boom)
    with caplog.at_level(logging.ERROR, logger="xrandrw"):
        save_state({"a": 1}, path=target)  # must NOT raise
    assert any("state_write_fail" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)


def test_open_lock_fd_refuses_symlink(tmp_path):
    real = tmp_path / "real_target"
    real.write_text("")
    link = tmp_path / "xrandrw.state.lock"
    link.symlink_to(real)
    with pytest.raises(OSError) as ei:
        _open_lock_fd(link)
    assert ei.value.errno == errno.ELOOP


def test_state_lock_acquires_and_releases(tmp_path):
    lock = tmp_path / "xrandrw.state.lock"
    with state_lock(lock):
        assert lock.exists()
    # re-acquirable after release (fd closed on exit)
    with state_lock(lock):
        pass
