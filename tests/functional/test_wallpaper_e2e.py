"""L1 real-X E2E: the wallpaper actually LANDS on the root window (WP-03).

Every unit test in ``tests/test_wallpaper.py`` asserts we RAN a command. None of
them can tell "the backend was invoked" from "the wallpaper is on screen" -- which
is exactly the gap that let WP-01 (discarded returncode) ship. This file closes it
with the only assertion that proves the effect: the X root pixmap property
``_XROOTPMAP_ID`` CHANGES, and the pixels behind it are the image we asked for.

Backend: ``native`` (Xlib+Pillow), forced via ``WALLPAPER_ENGINE`` so the test is
self-contained and needs no feh/fehbg/xwallpaper binary in the harness.

ISOLATION CONTRACT: runs on the session ``x_display`` (nested Xephyr >= :99) and
asserts it is never ``:0`` -- this must never touch the developer's live desktop.
"""
from __future__ import annotations

import logging
import os
from subprocess import CompletedProcess

import pytest
from Xlib import X, Xatom, display

import xrandrw.wallpaper as wp
from conftest import _unavailable  # session fixtures/helpers live in the same dir

pytestmark = pytest.mark.functional

_LOG = logging.getLogger("xrandrw.functional-test")


def _root_pixmap_id(d, root) -> int | None:
    prop = root.get_full_property(d.get_atom("_XROOTPMAP_ID"), Xatom.PIXMAP)
    return prop.value[0] if prop and len(prop.value) else None


def _pixel_rgb(d, pmid: int) -> tuple[int, int, int]:
    # One pixel out of the root pixmap. The native tier resizes to full screen, so a
    # solid-colour source means any pixel is representative.
    pm = d.create_resource_object("pixmap", pmid)
    data = pm.get_image(0, 0, 1, 1, X.ZPixmap, 0xFFFFFFFF).data
    b, g, r = data[0], data[1], data[2]   # depth-24 ZPixmap on little-endian TrueColor
    return (r, g, b)


def _write_solid(path, rgb) -> str:
    wp.Image.new("RGB", (64, 64), rgb).save(path)
    return str(path)


@pytest.fixture
def x_conn(x_display):
    d = display.Display(x_display)
    try:
        yield d, d.screen().root
    finally:
        d.close()


def test_native_wallpaper_changes_root_pixmap(x_display, x_conn, tmp_path):
    if not wp._HAVE_PIL:
        _unavailable("Pillow missing: pip install -e '.[wallpaper]'")
    assert x_display != ":0", "refusing to touch the developer's live session"
    assert os.environ.get("DISPLAY") == x_display, "native tier must target the harness server"
    d, root = x_conn

    before = _root_pixmap_id(d, root)

    red = _write_solid(tmp_path / "red.png", (255, 0, 0))
    wp.apply_wallpaper({"WALL": red, "WALLPAPER_ENGINE": "native"}, _LOG)
    d.sync()

    first = _root_pixmap_id(d, root)
    assert first is not None, "_XROOTPMAP_ID was never set: no wallpaper landed"
    assert first != before, "_XROOTPMAP_ID did not change: no wallpaper landed"
    r, g, b = _pixel_rgb(d, first)
    assert (r, g, b) == (255, 0, 0), f"root pixmap holds {r},{g},{b}, not the red image"

    # A SECOND, different image must land too -- proves the property tracks each apply
    # rather than merely having been set once.
    blue = _write_solid(tmp_path / "blue.png", (0, 0, 255))
    wp.apply_wallpaper({"WALL": blue, "WALLPAPER_ENGINE": "native"}, _LOG)
    d.sync()

    second = _root_pixmap_id(d, root)
    assert second != first, "_XROOTPMAP_ID unchanged across a second apply"
    assert _pixel_rgb(d, second) == (0, 0, 255), "root pixmap did not update to the blue image"


def test_failed_backend_falls_through_to_native_on_real_x(x_display, x_conn, tmp_path, monkeypatch):
    # WP-01 end-to-end: a backend that exits non-zero must not leave the root
    # untouched -- the chain falls through and a real wallpaper still lands.
    if not wp._HAVE_PIL:
        _unavailable("Pillow missing: pip install -e '.[wallpaper]'")
    d, root = x_conn
    before = _root_pixmap_id(d, root)

    green = _write_solid(tmp_path / "green.png", (0, 255, 0))
    # Present a feh that always fails; native is the next (and last) link in the chain.
    monkeypatch.setattr(wp.shutil, "which", lambda n: "/nonexistent/feh" if n == "feh" else None)
    monkeypatch.setattr(wp, "run", lambda cmd, logger=None, **kw: CompletedProcess(cmd, 1))
    wp.apply_wallpaper({"WALL": green}, _LOG)
    d.sync()

    after = _root_pixmap_id(d, root)
    assert after is not None and after != before, "fallthrough did not land a wallpaper"
    assert _pixel_rgb(d, after) == (0, 255, 0), "native fallthrough did not paint the image"
