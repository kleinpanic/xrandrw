"""CI-04 behavior-lock: golden place/place_chain sequence for the shared external-placement path.

This test pins the EXACT ordered sequence of `place`/`place_chain` log events (output, side,
anchor, profile) that apply_once emits over a >=2-external CHAINED-placement scenario that
exercises three things simultaneously:
  - newest-first reversal (attach_stack is reversed before assign_placements),
  - the WR-03 connector-expansion path (two identical-EDID heads collapse to ONE profile id but
    still expand to two connectors), and
  - place_chain (once all four sides are occupied the next connector chains off the
    previously-placed external, NOT the primary).

It is recorded against the CURRENT (pre-refactor) code and MUST stay byte-identical after the
`_place_externals` extraction in plan 14-03 — that is what proves the extraction is
behavior-preserving. Behavior-level assertions only (log events), no src coupling.
"""
from __future__ import annotations
import logging

import pytest

import xrandrw.apply as apply_mod
from xrandrw.state import _new_profile_id


def _env(tmp_path):
    return {
        "LOCKFILE": str(tmp_path / "xrandrw.lock"),
        "STATE_LOCKFILE": str(tmp_path / "xrandrw.state.lock"),
        "PREF_DEFAULT_SIDE": "right-of",
        "HIDPI_WIDTH": "3840",
        "WALL": str(tmp_path / "wall.png"),
        "USE_XWALLPAPER": "0",
        "APPLY_BACKEND": "subprocess",
    }


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_place_golden")
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture
def mock_x(monkeypatch):
    # Same seam as tests/test_apply.py: stub every X/side-effect entry point so apply_once
    # needs no live server; auto_pos calls are recorded as (connector, rel_opt, anchor).
    calls = []
    monkeypatch.setattr(apply_mod, "wait_for_x", lambda logger: None)
    monkeypatch.setattr(apply_mod, "read_edids", lambda outs, logger: None)
    monkeypatch.setattr(apply_mod, "scrub_stale", lambda outs, logger, backend=None: None)
    monkeypatch.setattr(apply_mod, "reapply_wallpaper", lambda env, logger: None)
    monkeypatch.setattr(apply_mod, "xrandr_auto_primary_scale", lambda c, s, logger: None)
    monkeypatch.setattr(apply_mod, "xrandr_rotate_left_if_portrait", lambda c, o, logger: None)
    monkeypatch.setattr(apply_mod, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        apply_mod, "xrandr_auto_pos",
        lambda connector, rel_opt, anchor, logger: calls.append((connector, rel_opt, anchor)),
    )
    return calls


def _isolate_state(monkeypatch):
    monkeypatch.setattr(apply_mod, "load_state", lambda: {"profiles": {}, "identity_map": {}})
    monkeypatch.setattr(apply_mod, "save_state", lambda st, path=None: None)


def _place_sequence(records):
    seq = []
    for r in records:
        ev = getattr(r, "event", None)
        if ev in ("place", "place_chain"):
            seq.append((ev, r.output, r.side, r.anchor, r.profile))
    return seq


def test_internal_primary_place_chain_golden(tmp_path, mock_x, logger, monkeypatch, output_factory, caplog):
    # DSI-1 internal primary; five externals where DP-A/DP-B share an EDID (one profile id,
    # two connectors -> WR-03 expansion). Five connectors fill all four sides, so the fifth
    # (DP-B) chains off the previously-placed DP-A via place_chain. attach_stack is reversed,
    # so DP-E (last-seen) is placed first.
    calls = mock_x
    outs = {
        "DSI-1": output_factory("DSI-1", connected=True),
        "DP-A": output_factory("DP-A", connected=True, edid_sha1="cafe1234"),
        "DP-B": output_factory("DP-B", connected=True, edid_sha1="cafe1234"),
        "DP-C": output_factory("DP-C", connected=True),
        "DP-D": output_factory("DP-D", connected=True),
        "DP-E": output_factory("DP-E", connected=True),
    }
    monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: outs)
    _isolate_state(monkeypatch)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_place_golden"):
        apply_mod.apply_once(_env(tmp_path), logger)

    dupe = _new_profile_id("cafe1234")
    pid_c = _new_profile_id("DP-C")
    pid_d = _new_profile_id("DP-D")
    pid_e = _new_profile_id("DP-E")

    golden = [
        ("place", "DP-E", "right-of", "DSI-1", pid_e),
        ("place", "DP-D", "left-of", "DSI-1", pid_d),
        ("place", "DP-C", "above", "DSI-1", pid_c),
        ("place", "DP-A", "below", "DSI-1", dupe),
        ("place_chain", "DP-B", "right-of", "DP-A", dupe),
    ]
    assert _place_sequence(caplog.records) == golden
    # The recorded auto_pos argv order mirrors the golden placement order exactly.
    assert calls == [(o, s, a) for _ev, o, s, a, _p in golden]


def test_no_internal_place_chain_golden(tmp_path, mock_x, logger, monkeypatch, output_factory, caplog):
    # Same chained/reversed/expansion shape in the NO-internal branch: DP-0 (lexicographically
    # first) becomes primary; DP-A/DP-B share an EDID; DP-C/DP-D/DP-E fill the sides so DP-B chains.
    calls = mock_x
    outs = {
        "DP-0": output_factory("DP-0", connected=True),
        "DP-A": output_factory("DP-A", connected=True, edid_sha1="feed5678"),
        "DP-B": output_factory("DP-B", connected=True, edid_sha1="feed5678"),
        "DP-C": output_factory("DP-C", connected=True),
        "DP-D": output_factory("DP-D", connected=True),
        "DP-E": output_factory("DP-E", connected=True),
    }
    monkeypatch.setattr(apply_mod, "read_xrandr", lambda logger: outs)
    _isolate_state(monkeypatch)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_place_golden"):
        apply_mod.apply_once(_env(tmp_path), logger)

    dupe = _new_profile_id("feed5678")
    pid_c = _new_profile_id("DP-C")
    pid_d = _new_profile_id("DP-D")
    pid_e = _new_profile_id("DP-E")

    golden = [
        ("place", "DP-E", "right-of", "DP-0", pid_e),
        ("place", "DP-D", "left-of", "DP-0", pid_d),
        ("place", "DP-C", "above", "DP-0", pid_c),
        ("place", "DP-A", "below", "DP-0", dupe),
        ("place_chain", "DP-B", "right-of", "DP-A", dupe),
    ]
    assert _place_sequence(caplog.records) == golden
    assert calls == [(o, s, a) for _ev, o, s, a, _p in golden]
