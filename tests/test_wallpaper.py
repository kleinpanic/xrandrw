from __future__ import annotations
import logging
from unittest.mock import MagicMock

import pytest

import xrandrw.wallpaper as wp
from xrandrw.wallpaper import select_wallpaper_backend


@pytest.fixture
def logger():
    lg = logging.getLogger("xrandrw.test_wallpaper")
    lg.setLevel(logging.DEBUG)
    return lg


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
