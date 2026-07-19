"""TEST-06 mutation-kill tests: sharp boundary assertions that KILL the first survivor batch.

Mutation testing found that the mock-heavy suite EXECUTES these lines but asserts weakly
enough that specific arithmetic/comparison mutants survive. Each test here pins an exact
boundary so a named mutant is provably killed:

  - Survivor A: dwmipc.parse_header size cap `>` boundary + the `size == 0` guard
    (kills `>`->`>=`, `>`->`<`, and `== 0`->`== 1`). This is the SEC-01 over-allocation
    guard, so the assertion is load-bearing, not cosmetic.
  - Survivor B: relocate.plan_restore floating/tiled guards (kills the `if record.is_floating`
    configure guard and the `!=`->`==` on the togglefloating state-diff test).
  - tagmon_direction fewest-hop tie (kills `<=`->`<` in the tie-break).

These are cheap, deterministic UNIT tests over pure helpers -- no sockets, no real dwm, no X.
"""
from __future__ import annotations

import pytest

from xrandrw import dwmipc
from xrandrw.dwmipc import DwmIpcUnavailable, GET_MONITORS, pack_header, parse_header
from xrandrw.relocate import Action, plan_restore, tagmon_direction


# --- Survivor A: parse_header size-cap `>` boundary + size==0 guard --------

def test_parse_header_size_at_cap_accepted_over_cap_rejected(monkeypatch):
    # Pin a small cap so the boundary pair is independent of the 8 MiB default.
    monkeypatch.setattr(dwmipc, "MAX_REPLY_SIZE", 16)

    # size == cap is ACCEPTED (kills `>`->`>=`: with `>=`, the at-cap header would raise).
    size, rtype = parse_header(pack_header(GET_MONITORS, 16))
    assert size == 16
    assert rtype == GET_MONITORS

    # size == cap + 1 is REJECTED (confirms `>` fires; kills `>`->`<`).
    with pytest.raises(DwmIpcUnavailable):
        parse_header(pack_header(GET_MONITORS, 17))


def test_parse_header_size_zero_rejected_size_one_accepted(monkeypatch):
    monkeypatch.setattr(dwmipc, "MAX_REPLY_SIZE", 16)

    # size == 0 is dwm's "empty message" reject (kills deletion of the `== 0` guard).
    with pytest.raises(DwmIpcUnavailable):
        parse_header(pack_header(GET_MONITORS, 0))

    # size == 1 (an empty null-terminated GET reply) is VALID (kills `== 0`->`== 1`:
    # with `== 1`, a legitimate size-1 reply would be wrongly rejected).
    size, _ = parse_header(pack_header(GET_MONITORS, 1))
    assert size == 1


# --- Survivor B: plan_restore floating/tiled guards ------------------------

def _live(*, target_monitor, current_monitor, current_floating, n_monitors=2):
    from types import SimpleNamespace
    return SimpleNamespace(target_monitor=target_monitor, current_monitor=current_monitor,
                           current_floating=current_floating, n_monitors=n_monitors)


def _rec(*, tags=4, is_floating=False, geometry=None):
    from types import SimpleNamespace
    return SimpleNamespace(tags=tags, is_floating=is_floating,
                           geometry=geometry or {"x": 1, "y": 2, "width": 3, "height": 4})


def test_plan_restore_tiled_never_configures():
    # A TILED record on its correct monitor with matching float state yields ONLY the tag
    # restore -- NO configure (kills deletion/negation of `if record.is_floating`) and NO
    # togglefloating (kills the state-diff append when states already agree).
    rec = _rec(tags=2, is_floating=False)
    live = _live(target_monitor=0, current_monitor=0, current_floating=False)
    assert plan_restore(rec, live) == [Action("tag", 2)]


def test_plan_restore_no_togglefloating_when_state_matches():
    # Floating record whose live state ALREADY matches -> NO togglefloating
    # (kills `!=`->`==`: with `==`, matching states would wrongly append togglefloating).
    rec = _rec(is_floating=True, geometry={"x": 5, "y": 6, "width": 7, "height": 8})
    live = _live(target_monitor=0, current_monitor=0, current_floating=True)
    actions = plan_restore(rec, live)
    assert not any(a.verb == "togglefloating" for a in actions)
    # It IS floating, so a configure restores the saved geometry.
    assert Action("configure", {"x": 5, "y": 6, "width": 7, "height": 8}) in actions


def test_plan_restore_exactly_one_togglefloating_when_state_differs():
    # Floating record but live is tiled -> EXACTLY one togglefloating (kills `!=`->`==`).
    rec = _rec(is_floating=True, geometry={"x": 5, "y": 6, "width": 7, "height": 8})
    live = _live(target_monitor=0, current_monitor=0, current_floating=False)
    toggles = [a for a in plan_restore(rec, live) if a.verb == "togglefloating"]
    assert toggles == [Action("togglefloating", None)]


# --- tagmon_direction fewest-hop tie-break ---------------------------------

def test_tagmon_direction_tie_breaks_positive():
    # forward hops == backward hops -> the `<=` tie-break returns +1 (kills `<=`->`<`,
    # which would flip the tie to -1).
    assert tagmon_direction(0, 1, 2) == 1
    # A 4-monitor exact tie (2 hops either way) also resolves to +1.
    assert tagmon_direction(1, 3, 4) == 1
