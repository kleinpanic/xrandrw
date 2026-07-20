"""GAP-E: set_pref's match/no-match paths (cli.py:46, 48->44, 52-53, 55).

``--set-pref`` on an unknown output exited 0 with no message: the user is told
nothing and walks away believing a preference was saved. Nothing in the suite
ever ran set_pref with a CONNECTED output either, so line 46 (ensure_profile),
the 48->44 non-matching-output loop edge, and the known-profile-id fallback at
52-53 were all dark -- the whole function was one silent-success class.

Every test here drives the REAL set_pref against the real state store (sandboxed
to a tmp XDG_DATA_HOME by conftest) with only read_xrandr/read_edids faked, and
asserts what actually landed on disk.
"""
from __future__ import annotations

import logging

import pytest

import xrandrw.cli as cli
from xrandrw.state import load_state, save_state


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_cli_set_pref")
    lg.setLevel(logging.DEBUG)
    return lg


@pytest.fixture
def env(tmp_path):
    return {
        "STATE_LOCKFILE": str(tmp_path / "xrandrw.state.lock"),
        "PREF_DEFAULT_SIDE": "right-of",
    }


@pytest.fixture
def fake_outputs(monkeypatch, output_factory):
    # Seam: set_pref's only X access. Returns the connector map a test declares.
    def install(*specs):
        outs = {}
        for name, edid in specs:
            outs[name] = output_factory(name=name, connected=True, edid_sha1=edid)
        monkeypatch.setattr(cli, "read_xrandr", lambda logger: outs)
        monkeypatch.setattr(cli, "read_edids", lambda outs, logger: None)
        return outs
    return install


def _sides_by_name() -> dict[str, str]:
    # Map connector name -> stored preferred_side, straight off the persisted state.
    st = load_state()
    return {
        name: prof.get("preferred_side")
        for prof in st.get("profiles", {}).values()
        for name in prof.get("names", [])
    }


# ---------------- GAP-E: the silent-success path ----------------

def test_unknown_output_exits_nonzero_and_names_the_output(env, fake_outputs, logger):
    # THE regression: this used to exit 0, print nothing, and save nothing.
    fake_outputs(("eDP-1", "aaa111"), ("HDMI-1", None))

    with pytest.raises(SystemExit) as excinfo:
        cli.set_pref(env, "DP-9", "left-of", logger)

    code = excinfo.value.code
    assert code != 0, "an unapplied preference must not look like success"
    assert isinstance(code, str), "SystemExit(str) exits 1 and prints the message"
    assert "DP-9" in code, "the error must name the output the user actually typed"

    # The raise precedes save_state, so a rejected set-pref writes NOTHING -- not
    # even the profiles ensure_profile built in memory while scanning for a match.
    assert load_state()["profiles"] == {}, "a failed set-pref must not persist state"


def test_unknown_identity_prefixes_also_fail_loudly(env, fake_outputs, logger):
    fake_outputs(("eDP-1", "aaa111"))
    for target in ("edid:deadbeef", "conn:HDMI-3", "0123456789abcdef"):
        with pytest.raises(SystemExit) as excinfo:
            cli.set_pref(env, target, "above", logger)
        assert target in str(excinfo.value.code)


def test_invalid_side_is_rejected_before_any_x_read(env, monkeypatch, logger):
    # The side check precedes read_xrandr, so a typo never costs an X round-trip.
    def never(logger):
        raise AssertionError("read_xrandr must not run for an invalid side")

    monkeypatch.setattr(cli, "read_xrandr", never)
    with pytest.raises(SystemExit) as excinfo:
        cli.set_pref(env, "eDP-1", "sideways", logger)
    assert "sideways" in str(excinfo.value.code)
    assert "right-of" in str(excinfo.value.code), "the message must list the valid sides"


# ---------------- the matching paths (line 46 / the 48->44 loop edge) ----------------

def test_match_by_connector_name_persists_the_side(env, fake_outputs, logger, caplog):
    fake_outputs(("eDP-1", "aaa111"))

    with caplog.at_level(logging.INFO, logger="xrandrw.test_cli_set_pref"):
        cli.set_pref(env, "eDP-1", "below", logger)

    assert _sides_by_name()["eDP-1"] == "below"
    recorded = [r for r in caplog.records if getattr(r, "event", None) == "set_pref"]
    assert recorded and recorded[0].side == "below"
    assert recorded[0].profiles, "the updated profile id must be reported"


def test_match_by_edid_identity(env, fake_outputs, logger):
    fake_outputs(("HDMI-1", "abc123"))
    cli.set_pref(env, "edid:abc123", "above", logger)
    assert _sides_by_name()["HDMI-1"] == "above"


def test_match_by_conn_identity(env, fake_outputs, logger):
    fake_outputs(("HDMI-1", None))
    cli.set_pref(env, "conn:HDMI-1", "left-of", logger)
    assert _sides_by_name()["HDMI-1"] == "left-of"


def test_only_the_named_output_is_changed(env, fake_outputs, logger):
    # The 48->44 edge: the loop must WALK PAST non-matching connected outputs,
    # profiling each (line 46) without touching its stored side. A match-anything
    # mutation would rewrite every head's preference here.
    fake_outputs(("eDP-1", "aaa111"), ("HDMI-1", "bbb222"), ("DP-1", None))

    cli.set_pref(env, "HDMI-1", "above", logger)

    assert _sides_by_name() == {"eDP-1": "right-of", "HDMI-1": "above", "DP-1": "right-of"}


def test_disconnected_outputs_are_skipped(env, monkeypatch, output_factory, logger):
    # A dark connector is not a valid target: matching one would store a side for a
    # head that is not there, which is the state set_pref exists to keep accurate.
    outs = {"HDMI-1": output_factory(name="HDMI-1", connected=False, edid_sha1="ccc333")}
    monkeypatch.setattr(cli, "read_xrandr", lambda logger: outs)
    monkeypatch.setattr(cli, "read_edids", lambda outs, logger: None)

    with pytest.raises(SystemExit) as excinfo:
        cli.set_pref(env, "HDMI-1", "below", logger)
    assert "HDMI-1" in str(excinfo.value.code)
    assert load_state()["profiles"] == {}, "a disconnected head must not be profiled"


def test_repeated_set_pref_overwrites_rather_than_duplicating(env, fake_outputs, logger):
    fake_outputs(("eDP-1", "aaa111"))
    cli.set_pref(env, "eDP-1", "above", logger)
    cli.set_pref(env, "eDP-1", "below", logger)

    st = load_state()
    assert len(st["profiles"]) == 1, "the same panel must reuse its profile"
    assert _sides_by_name()["eDP-1"] == "below"


# ---------------- the known-profile-id fallback (cli.py:52-53) ----------------

def test_known_profile_id_matches_even_while_unplugged(env, fake_outputs, logger):
    # Setting a preference for a monitor that is currently AWAY is the whole point
    # of profile ids: seed one, unplug it, address it by id.
    fake_outputs(("HDMI-1", "abc123"))
    cli.set_pref(env, "HDMI-1", "above", logger)
    pid = next(iter(load_state()["profiles"]))

    fake_outputs(("eDP-1", "aaa111"))          # HDMI-1 now unplugged
    cli.set_pref(env, pid, "left-of", logger)

    st = load_state()
    assert st["profiles"][pid]["preferred_side"] == "left-of"
    assert "HDMI-1" in st["profiles"][pid]["names"]
    assert _sides_by_name()["eDP-1"] == "right-of", "the connected head is untouched"


def test_profile_id_fallback_does_not_fire_when_a_connector_already_matched(
        env, fake_outputs, logger):
    # Guard the precedence: a connected match must not ALSO create/patch a stray
    # profile entry keyed by the raw argument.
    fake_outputs(("eDP-1", "aaa111"))
    cli.set_pref(env, "eDP-1", "above", logger)

    st = load_state()
    assert len(st["profiles"]) == 1
    assert "eDP-1" not in st["profiles"], "the connector name must not become a profile id"


def test_orphan_profile_id_from_a_prior_boot_is_addressable(env, fake_outputs, logger):
    # A profile written by an earlier session, with nothing plugged in that matches.
    save_state({
        "profiles": {"feedfacefeedface": {"names": ["DP-2"], "edid": "zzz999",
                                          "preferred_side": "right-of"}},
        "identity_map": {"edid:zzz999": "feedfacefeedface"},
    })
    fake_outputs(("eDP-1", "aaa111"))

    cli.set_pref(env, "feedfacefeedface", "below", logger)

    assert load_state()["profiles"]["feedfacefeedface"]["preferred_side"] == "below"
