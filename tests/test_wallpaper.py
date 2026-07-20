from __future__ import annotations
import logging
from unittest.mock import MagicMock

import pytest

import xrandrw.wallpaper as wp
from xrandrw.wallpaper import select_wallpaper_backend, wallpaper_backend_chain


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_wallpaper")
    lg.setLevel(logging.DEBUG)
    return lg


# ---------------- backend-failure fallthrough (WP-01) ----------------

class _FakeRun:
    # Records every argv and returns a caller-chosen rc per binary, so a test can make a
    # backend FAIL for real instead of assuming "we ran a command" == "it worked".
    def __init__(self, rc_by_binary: dict[str, int]):
        self.rc_by_binary = rc_by_binary
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, logger=None, **kw):
        self.cmds.append(list(cmd))
        from subprocess import CompletedProcess
        return CompletedProcess(cmd, self.rc_by_binary.get(cmd[0], 0))

    @property
    def binaries(self) -> list[str]:
        return [c[0] for c in self.cmds]


def _events(caplog) -> list[str]:
    return [getattr(r, "event", None) for r in caplog.records]


def _only(caplog, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == event]


@pytest.fixture
def wall_file(tmp_path):
    p = tmp_path / "wall.png"
    p.write_bytes(b"x")
    return str(p)


def _present(monkeypatch, *names: str):
    # Only `names` exist on PATH, for both select/chain and the per-backend guards.
    monkeypatch.setattr(wp.shutil, "which", lambda n: f"/usr/bin/{n}" if n in names else None)


def test_chain_order():
    # Auto-detect builds a full fallthrough chain, always terminated by native.
    assert wallpaper_backend_chain({}, True, True, True) == ["fehbg", "feh", "native"]
    assert wallpaper_backend_chain({"USE_XWALLPAPER": "1"}, True, True, True) == [
        "xwallpaper", "fehbg", "feh", "native"]
    assert wallpaper_backend_chain({}, False, False, False) == ["native"]

    # An explicitly configured engine is a ONE-entry chain: no silent substitution.
    assert wallpaper_backend_chain({"WALLPAPER_ENGINE": " FEH "}, True, True, True) == ["feh"]

    # The chain head always agrees with the documented single-backend selector.
    for env in ({}, {"USE_XWALLPAPER": "1"}, {"WALLPAPER_ENGINE": "native"}):
        assert wallpaper_backend_chain(env, True, True, True)[0] == \
            select_wallpaper_backend(env, True, True, True)


def test_nonzero_returncode_is_detected(monkeypatch, logger, caplog, wall_file):
    # The core WP-01 hole: a failing backend used to log "wallpaper" as though it worked.
    _present(monkeypatch, "feh")
    fake = _FakeRun({"feh": 1})
    monkeypatch.setattr(wp, "run", fake)
    monkeypatch.setattr(wp, "_native_wallpaper", lambda env, lg: False)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": wall_file}, logger)

    failed = _only(caplog, "wallpaper_failed")
    assert failed, "a non-zero backend returncode must log wallpaper_failed"
    assert failed[0].levelno == logging.WARNING
    assert failed[0].backend == "feh" and failed[0].rc == 1
    assert "wallpaper" not in _events(caplog), "a failed backend must not log success"


def test_failed_backend_falls_through_to_next(monkeypatch, logger, caplog, wall_file):
    # fehbg fails -> feh must actually be TRIED, and its success ends the chain.
    _present(monkeypatch, "fehbg", "feh")
    fake = _FakeRun({"fehbg": 1, "feh": 0})
    monkeypatch.setattr(wp, "run", fake)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": wall_file}, logger)

    assert fake.binaries == ["fehbg", "feh"], "a failed backend must fall through to the next"
    assert _only(caplog, "wallpaper_failed")[0].backend == "fehbg"
    assert [r.msg for r in _only(caplog, "wallpaper")][0].startswith("feh")
    assert "wallpaper_exhausted" not in _events(caplog)


def test_configured_engine_does_not_fall_through(monkeypatch, logger, caplog, wall_file):
    # WP-01: an explicitly named engine is respected -- warn, never substitute another.
    _present(monkeypatch, "fehbg", "feh")
    fake = _FakeRun({"feh": 1, "fehbg": 0})
    monkeypatch.setattr(wp, "run", fake)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": wall_file, "WALLPAPER_ENGINE": "feh"}, logger)

    assert fake.binaries == ["feh"], "a configured engine must never fall through to another"
    assert _only(caplog, "wallpaper_failed")[0].backend == "feh"


def test_all_backends_failing_logs_exhausted(monkeypatch, logger, caplog, wall_file):
    _present(monkeypatch, "fehbg", "feh")
    fake = _FakeRun({"fehbg": 1, "feh": 3})
    monkeypatch.setattr(wp, "run", fake)
    monkeypatch.setattr(wp, "_native_wallpaper", lambda env, lg: False)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": wall_file}, logger)  # must never raise

    assert fake.binaries == ["fehbg", "feh"]
    assert [r.backend for r in _only(caplog, "wallpaper_failed")] == ["fehbg", "feh"]
    exhausted = _only(caplog, "wallpaper_exhausted")
    assert exhausted, "every backend failing must log wallpaper_exhausted"
    assert exhausted[0].levelno == logging.WARNING


def test_missing_binary_still_skips_without_failing(monkeypatch, logger, caplog, wall_file):
    # The pre-existing, already-correct case: nothing ran, so nothing "failed".
    _present(monkeypatch)
    monkeypatch.setattr(wp, "run", _FakeRun({}))
    monkeypatch.setattr(wp, "_HAVE_PIL", False)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": wall_file}, logger)

    assert _only(caplog, "wallpaper_native_skip")
    assert "wallpaper_failed" not in _events(caplog)
    assert "wallpaper_exhausted" not in _events(caplog)


# ---------------- fehbg ignores WALL (WP-02) ----------------

def test_fehbg_reports_that_wall_is_ignored(monkeypatch, logger, caplog, wall_file):
    _present(monkeypatch, "fehbg")
    fake = _FakeRun({"fehbg": 0})
    monkeypatch.setattr(wp, "run", fake)

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": wall_file}, logger)

    ignored = _only(caplog, "wallpaper_wall_ignored")
    assert ignored, "fehbg must state plainly that WALL is not applied"
    assert ignored[0].levelno == logging.INFO
    assert "WALLPAPER_ENGINE=feh" in ignored[0].msg, "must name the WALL-honouring alternative"
    # And we must NOT invent a flag for a third-party script.
    assert fake.cmds == [["fehbg"]]


def test_fehbg_silent_when_no_wall_configured(monkeypatch, logger, caplog):
    _present(monkeypatch, "fehbg")
    monkeypatch.setattr(wp, "run", _FakeRun({"fehbg": 0}))

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp.apply_wallpaper({"WALL": ""}, logger)

    assert "wallpaper_wall_ignored" not in _events(caplog)


def test_select():
    # Configured engine wins even when no binary is present; case-insensitive + stripped.
    for name in ("feh", "fehbg", "xwallpaper", "native"):
        env = {"WALLPAPER_ENGINE": f"  {name.upper()} "}
        assert select_wallpaper_backend(env, False, False, False) == name

    # Empty engine + USE_XWALLPAPER=1 + xwallpaper present -> xwallpaper.
    env = {"WALLPAPER_ENGINE": "", "USE_XWALLPAPER": "1"}
    assert select_wallpaper_backend(env, True, False, False) == "xwallpaper"

    # USE_XWALLPAPER=1 but xwallpaper absent -> falls through to fehbg.
    assert select_wallpaper_backend(env, False, True, False) == "fehbg"

    # Empty engine, no USE_XWALLPAPER: fehbg preferred over feh.
    assert select_wallpaper_backend({}, False, True, True) == "fehbg"
    assert select_wallpaper_backend({}, False, False, True) == "feh"

    # No engine, no binaries -> native final fallback.
    assert select_wallpaper_backend({}, False, False, False) == "native"

    # Unknown WALLPAPER_ENGINE value is ignored -> auto-detect path.
    assert select_wallpaper_backend({"WALLPAPER_ENGINE": "bogus"}, False, True, False) == "fehbg"


def test_native_skip_no_pillow(monkeypatch, tmp_path, logger, caplog):
    monkeypatch.setattr(wp, "_HAVE_PIL", False)
    env = {"WALL": str(tmp_path / "does-not-need-to-exist.png")}

    with caplog.at_level(logging.INFO, logger="xrandrw.test_wallpaper"):
        wp._native_wallpaper(env, logger)  # must not raise

    skips = [r for r in caplog.records if getattr(r, "event", None) == "wallpaper_native_skip"]
    assert skips, "native tier without Pillow must log wallpaper_native_skip"


def test_native_calls(monkeypatch, tmp_path, logger):
    calls = []
    atom_names = []

    pm = MagicMock()
    pm.id = 4242
    pm.put_pil_image.side_effect = lambda *a, **k: calls.append("put_pil_image")

    gc = MagicMock()
    gc.free.side_effect = lambda *a, **k: calls.append("gc_free")

    root = MagicMock()
    root.create_pixmap.side_effect = lambda *a, **k: (calls.append("create_pixmap"), pm)[1]
    root.create_gc.side_effect = lambda *a, **k: (calls.append("create_gc"), gc)[1]
    root.change_attributes.side_effect = lambda *a, **k: calls.append("change_attributes")
    root.clear_area.side_effect = lambda *a, **k: calls.append("clear_area")
    root.change_property.side_effect = lambda *a, **k: calls.append("change_property")

    screen = MagicMock()
    screen.root = root
    screen.root_depth = 24
    screen.width_in_pixels = 100
    screen.height_in_pixels = 50

    d = MagicMock()
    d.screen.return_value = screen
    d.get_atom.side_effect = lambda name: (atom_names.append(name), f"atom:{name}")[1]
    d.set_close_down_mode.side_effect = lambda mode: calls.append(("set_close_down_mode", mode))

    monkeypatch.setattr(wp, "_HAVE_PIL", True)
    monkeypatch.setattr(wp, "Image", MagicMock())
    monkeypatch.setattr(wp.display, "Display", lambda: d)

    wall = tmp_path / "wall.png"
    wall.write_bytes(b"x")
    env = {"WALL": str(wall)}

    wp._native_wallpaper(env, logger)

    # The verified call order (RESEARCH Pattern 4, python-xlib 0.33).
    labels = [c for c in calls if isinstance(c, str)]
    assert labels == [
        "create_pixmap", "create_gc", "put_pil_image",
        "change_attributes", "clear_area", "change_property", "change_property", "gc_free",
    ]

    # Both root pseudo-transparency atoms are set (change_property called exactly twice).
    assert labels.count("change_property") == 2
    assert atom_names == ["_XROOTPMAP_ID", "ESETROOT_PMAP_ID"]

    # RetainPermanent is mandatory — else the pixmap is freed on disconnect (black root).
    assert ("set_close_down_mode", wp.X.RetainPermanent) in calls
