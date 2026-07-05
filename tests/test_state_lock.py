from __future__ import annotations
import json
import logging
import threading
import time

import xrandrw.cli as cli
import xrandrw.state as state
from xrandrw.state import state_lock


def _env(tmp_path):
    return {
        "LOCKFILE": str(tmp_path / "xrandrw.lock"),
        "STATE_LOCKFILE": str(tmp_path / "xrandrw.state.lock"),
        "PREF_DEFAULT_SIDE": "right-of",
    }


def _mock_x(monkeypatch, output_factory, tmp_path):
    outs = {"DP-1": output_factory(name="DP-1", connected=True)}
    monkeypatch.setattr(cli, "read_xrandr", lambda logger: outs)
    monkeypatch.setattr(cli, "read_edids", lambda outs, logger: None)
    state_file = tmp_path / "state.json"

    def _load():
        if state_file.is_file():
            return json.loads(state_file.read_text())
        return {"profiles": {}, "identity_map": {}}

    monkeypatch.setattr(cli, "load_state", _load)
    monkeypatch.setattr(cli, "save_state", lambda st: state.save_state(st, path=state_file))
    return state_file


def _logger():
    lg = logging.getLogger("xrandrw.test_state_lock")
    lg.setLevel(logging.DEBUG)
    return lg


def test_concurrent_setpref_apply(tmp_path, monkeypatch, output_factory):
    env = _env(tmp_path)
    state_file = _mock_x(monkeypatch, output_factory, tmp_path)

    holder_acquired = threading.Event()
    release_ts = {}

    def holder():
        with state_lock(env["STATE_LOCKFILE"]):
            holder_acquired.set()
            time.sleep(0.4)
            release_ts["t"] = time.monotonic()

    t = threading.Thread(target=holder)
    t.start()
    assert holder_acquired.wait(timeout=2.0), "holder failed to acquire the state-lock"

    # set_pref must block on the held state-lock until the holder releases.
    cli.set_pref(env, "DP-1", "left-of", _logger())
    ret_ts = time.monotonic()

    t.join(timeout=2.0)
    assert not t.is_alive(), "holder thread did not finish"
    assert ret_ts >= release_ts["t"], "set_pref returned before the state-lock was released"

    st = json.loads(state_file.read_text())
    sides = [p.get("preferred_side") for p in st["profiles"].values()]
    assert "left-of" in sides, "set_pref write was lost"


def test_setpref_takes_only_state_lock(tmp_path, monkeypatch, output_factory):
    env = _env(tmp_path)
    _mock_x(monkeypatch, output_factory, tmp_path)

    opened = []
    real_open_lock = state._open_lock_fd

    def spy(path):
        opened.append(str(path))
        return real_open_lock(path)

    monkeypatch.setattr(state, "_open_lock_fd", spy)

    cli.set_pref(env, "DP-1", "left-of", _logger())

    # D-03a deadlock-freedom proof: set_pref opens the state-lock and NEVER the apply-lock.
    assert env["STATE_LOCKFILE"] in opened, "set_pref must acquire the state-lock"
    assert env["LOCKFILE"] not in opened, "set_pref must NEVER acquire the apply-lock (D-03a)"
