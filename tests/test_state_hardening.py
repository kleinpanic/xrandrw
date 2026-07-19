"""TEST-07: state.py hardening branches — load/atomic-write/lock/merge paths.

Every other suite monkeypatches load_state/save_state, so the REAL persistence
paths (load_state default-fallback, the atomic-write unlink-failure guard, the
default state_path() write, and ensure_profile's profile-merge) are otherwise
untested. These drive them directly against tmp_path. No src behavior changes.
"""
from __future__ import annotations

import errno
import json
import logging

import pytest

import xrandrw.state as state
from xrandrw.state import (
    ensure_profile,
    load_state,
    save_state,
    state_dir,
    state_path,
    _atomic_write_json,
)


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_state_hardening")
    lg.setLevel(logging.DEBUG)
    return lg


# ---------------- state_dir / state_path resolution ----------------

def test_state_dir_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert state_dir() == tmp_path / "xdg" / "xrandrw"
    assert state_path() == tmp_path / "xdg" / "xrandrw" / "state.json"


def test_state_dir_home_fallback_when_xdg_unset(monkeypatch, tmp_path):
    # The XDG_DATA_HOME-unset branch falls back to ~/.local/share; redirect HOME via
    # Path.home so this never resolves the developer's real home directory.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(state.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert state_dir() == tmp_path / "home" / ".local/share" / "xrandrw"


# ---------------- load_state (real path, not the ubiquitous monkeypatch) ----------------

def test_load_state_reads_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    payload = {"profiles": {"p": {"names": ["HDMI-1"]}}, "identity_map": {"conn:HDMI-1": "p"}}
    sp.write_text(json.dumps(payload))
    assert load_state() == payload


def test_load_state_missing_file_returns_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-xdg"))
    assert load_state() == {"profiles": {}, "identity_map": {}}


def test_load_state_malformed_json_returns_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("{ this is not json ]")
    # A corrupt state.json degrades to the safe default rather than raising.
    assert load_state() == {"profiles": {}, "identity_map": {}}


# ---------------- atomic write: unlink-failure guard + default-path save ----------------

def test_atomic_write_unlink_failure_is_swallowed(tmp_path, monkeypatch):
    # When os.replace fails AND the cleanup os.unlink ALSO fails, the inner
    # `except OSError: pass` guard must swallow the unlink error and re-raise the
    # ORIGINAL replace failure (not the unlink one).
    target = tmp_path / "state.json"

    def boom_replace(src, dst):
        raise OSError("replace failed")

    def boom_unlink(p):
        raise OSError("unlink failed too")

    monkeypatch.setattr(state.os, "replace", boom_replace)
    monkeypatch.setattr(state.os, "unlink", boom_unlink)
    with pytest.raises(OSError) as ei:
        _atomic_write_json(target, {"a": 1})
    assert "replace failed" in str(ei.value)


def test_save_state_default_path_writes_to_state_path(tmp_path, monkeypatch):
    # save_state(st) with no explicit path resolves state_path() (the default-path
    # branch), which the isolated XDG dir makes safe to exercise for real.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    st = {"profiles": {"p": {"names": ["DP-1"]}}, "identity_map": {}}
    save_state(st)
    assert json.loads(state_path().read_text()) == st


def test_save_state_failure_on_existing_does_not_corrupt(tmp_path, monkeypatch, caplog):
    # An atomic-write failure must leave the pre-existing state.json intact.
    target = tmp_path / "state.json"
    good = {"profiles": {"keep": {"names": ["DP-1"]}}, "identity_map": {}}
    save_state(good, path=target)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(state.os, "replace", boom)
    with caplog.at_level(logging.ERROR, logger="xrandrw"):
        save_state({"profiles": {"new": {}}, "identity_map": {}}, path=target)  # must not raise
    # Original content preserved; failure logged (state_write_fail).
    assert json.loads(target.read_text()) == good
    assert any("state_write_fail" in r.getMessage() for r in caplog.records)


# ---------------- state_lock O_NOFOLLOW ----------------

def test_state_lock_refuses_symlinked_path(tmp_path):
    real = tmp_path / "real_lock"
    real.write_text("")
    link = tmp_path / "sym.lock"
    link.symlink_to(real)
    with pytest.raises(OSError) as ei:
        with state.state_lock(link):
            pass
    assert ei.value.errno == errno.ELOOP


# ---------------- ensure_profile merge path ----------------

def test_ensure_profile_merges_two_profiles(logger, output_factory):
    # Two identity keys (edid + connector) that historically resolved to DIFFERENT
    # profile ids must merge into one on the next ensure_profile: names unioned,
    # edid/preferred_side back-filled, the stale pid dropped, identity_map re-pointed.
    st = {
        "profiles": {
            "pid_edid": {"names": ["DP-1"], "edid": "abc123", "preferred_side": "left-of"},
            "pid_conn": {"names": ["DP-1-alt"], "edid": None, "last_seen": 1.0},
        },
        "identity_map": {"edid:abc123": "pid_edid", "conn:DP-1": "pid_conn"},
    }
    o = output_factory(name="DP-1", connected=True, edid_sha1="abc123")

    target = ensure_profile(o, st, logger, "right-of")

    assert target == "pid_edid", "first matched target is kept"
    assert "pid_conn" not in st["profiles"], "merged-away profile removed"
    kept = st["profiles"]["pid_edid"]
    assert set(kept["names"]) >= {"DP-1", "DP-1-alt"}, "names unioned across merged profiles"
    # every identity_map entry that pointed at the stale pid now points at the target
    assert all(v == "pid_edid" for v in st["identity_map"].values())


def test_ensure_profile_merge_backfills_edid_and_side(logger, output_factory):
    # When the KEPT profile lacks edid/preferred_side but the merged-away one has
    # them, the merge back-fills both from the source before dropping it.
    st = {
        "profiles": {
            "pid_edid": {"names": ["DP-1"]},  # kept target, but missing edid + side
            "pid_conn": {"names": ["DP-1"], "edid": "abc123", "preferred_side": "above"},
        },
        "identity_map": {"edid:abc123": "pid_edid", "conn:DP-1": "pid_conn"},
    }
    o = output_factory(name="DP-1", connected=True, edid_sha1="abc123")

    target = ensure_profile(o, st, logger, "right-of")
    kept = st["profiles"][target]
    assert kept["edid"] == "abc123", "edid back-filled from the merged-away profile"
    assert kept["preferred_side"] == "above", "preferred_side back-filled from the merged profile"


def test_ensure_profile_creates_new_when_unknown(logger, output_factory):
    st = {"profiles": {}, "identity_map": {}}
    o = output_factory(name="HDMI-2", connected=True, edid_sha1=None)
    pid = ensure_profile(o, st, logger, "below")
    prof = st["profiles"][pid]
    assert prof["names"] == ["HDMI-2"]
    assert prof["preferred_side"] == "below"
    assert st["identity_map"]["conn:HDMI-2"] == pid
