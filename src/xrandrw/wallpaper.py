from __future__ import annotations
import logging
import shutil
from pathlib import Path
from typing import Dict

from Xlib import X, Xatom, display

from xrandrw.logging_utils import run, loge, logev

# Optional-Pillow guard (D-06): the native tier needs Pillow, declared as the `wallpaper`
# extra. Absent Pillow degrades gracefully (see _native_wallpaper). Image is kept as a
# module attribute either way so the native path stays trivially mockable in tests.
try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:  # pragma: no cover
    Image = None
    _HAVE_PIL = False


def select_wallpaper_backend(env: Dict[str, str], has_xwallpaper: bool, has_fehbg: bool, has_feh: bool) -> str:
    # Pure: configured engine wins, else auto-detect order, else native fallback (D-04/D-05).
    eng = env.get("WALLPAPER_ENGINE", "").strip().lower()
    if eng in ("feh", "fehbg", "xwallpaper", "native"):
        return eng
    if env.get("USE_XWALLPAPER") == "1" and has_xwallpaper:
        return "xwallpaper"
    if has_fehbg:
        return "fehbg"
    if has_feh:
        return "feh"
    return "native"


def apply_wallpaper(env: Dict[str, str], logger: logging.Logger):
    wall = env["WALL"]
    backend = select_wallpaper_backend(
        env,
        has_xwallpaper=shutil.which("xwallpaper") is not None,
        has_fehbg=shutil.which("fehbg") is not None,
        has_feh=shutil.which("feh") is not None,
    )
    logev(logger, logging.INFO, "wallpaper_backend", "wallpaper backend selected", backend=backend)

    if backend == "xwallpaper":
        if Path(wall).is_file() and shutil.which("xwallpaper"):
            run(["xwallpaper", "--zoom", wall], logger=logger)
            logev(logger, logging.INFO, "wallpaper", "xwallpaper", file=wall)
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "xwallpaper missing or file not found", file=wall)
    elif backend == "fehbg":
        if shutil.which("fehbg"):
            run(["fehbg"], logger=logger)
            loge(logger, logging.INFO, "wallpaper", "fehbg")
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "fehbg missing", file=wall)
    elif backend == "feh":
        if shutil.which("feh") and Path(wall).is_file():
            run(["feh", "--no-fehbg", "--bg-fill", wall], logger=logger)
            logev(logger, logging.INFO, "wallpaper", "feh", file=wall)
        else:
            logev(logger, logging.INFO, "wallpaper_skip", "feh missing or file not found", file=wall)
    else:
        _native_wallpaper(env, logger)


def _native_wallpaper(env: Dict[str, str], logger: logging.Logger):
    if not _HAVE_PIL:
        logev(logger, logging.INFO, "wallpaper_native_skip",
              "native wallpaper needs the 'wallpaper' extra: pip install xrandrw[wallpaper]")
        return
    wall = env["WALL"]
    if not Path(wall).is_file():
        logev(logger, logging.INFO, "wallpaper_skip", "wallpaper file not found", file=wall)
        return
    try:
        d = display.Display()                      # per-call, main apply thread only (Xlib not thread-safe)
        s = d.screen()
        root, depth = s.root, s.root_depth
        sw, sh = s.width_in_pixels, s.height_in_pixels
        img = Image.open(wall).convert("RGB").resize((sw, sh))  # RGB required by put_pil_image
        pm = root.create_pixmap(sw, sh, depth)
        gc = root.create_gc()
        pm.put_pil_image(gc, 0, 0, img)
        root.change_attributes(background_pixmap=pm.id)
        root.clear_area(0, 0, sw, sh)
        for name in ("_XROOTPMAP_ID", "ESETROOT_PMAP_ID"):
            root.change_property(d.get_atom(name), Xatom.PIXMAP, 32, [pm.id])
        d.set_close_down_mode(X.RetainPermanent)   # MANDATORY — else the pixmap is freed on close -> black root
        gc.free()
        d.flush()
        d.sync()
        d.close()
        logev(logger, logging.INFO, "wallpaper", "native", file=wall, size=f"{sw}x{sh}")
    except Exception as e:                          # best-effort; a wallpaper failure must never break layout apply
        logev(logger, logging.WARNING, "wallpaper_native_fail", "native wallpaper failed", error=str(e))
