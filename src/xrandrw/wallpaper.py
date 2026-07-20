from __future__ import annotations
import logging
import shutil
from pathlib import Path

from Xlib import X, Xatom, display

from xrandrw.logging_utils import run, logev

# Optional-Pillow guard (D-06): the native tier needs Pillow, declared as the `wallpaper`
# extra. Absent Pillow degrades gracefully (see _native_wallpaper). Image is kept as a
# module attribute either way so the native path stays trivially mockable in tests.
try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:  # pragma: no cover
    Image = None
    _HAVE_PIL = False


def select_wallpaper_backend(env: dict[str, str], has_xwallpaper: bool, has_fehbg: bool, has_feh: bool) -> str:
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


def wallpaper_backend_chain(env: dict[str, str], has_xwallpaper: bool, has_fehbg: bool, has_feh: bool) -> list[str]:
    # Pure: the ordered list of backends apply_wallpaper may try. An explicitly configured
    # engine is a single-entry chain -- if the user named an engine we respect it and warn
    # rather than silently substituting another one (WP-01).
    eng = env.get("WALLPAPER_ENGINE", "").strip().lower()
    if eng in ("feh", "fehbg", "xwallpaper", "native"):
        return [eng]
    chain = []
    if env.get("USE_XWALLPAPER") == "1" and has_xwallpaper:
        chain.append("xwallpaper")
    if has_fehbg:
        chain.append("fehbg")
    if has_feh:
        chain.append("feh")
    chain.append("native")
    return chain


def apply_wallpaper(env: dict[str, str], logger: logging.Logger):
    chain = wallpaper_backend_chain(
        env,
        has_xwallpaper=shutil.which("xwallpaper") is not None,
        has_fehbg=shutil.which("fehbg") is not None,
        has_feh=shutil.which("feh") is not None,
    )
    logev(logger, logging.INFO, "wallpaper_backend", "wallpaper backend selected", backend=chain[0])

    for backend in chain:
        applied = _try_backend(backend, env, logger)
        if applied is None:
            return          # binary missing / file not found: terminal skip, already logged
        if applied:
            return
    logev(logger, logging.WARNING, "wallpaper_exhausted", "every wallpaper backend failed",
          tried=",".join(chain))


def _run_backend(cmd: list[str], backend: str, logger: logging.Logger, **fields) -> bool:
    # WP-01: the returncode is the ONLY evidence the backend did anything. Discarding it
    # let a failed apply log "wallpaper" as though it had worked.
    cp = run(cmd, logger=logger)
    if cp.returncode != 0:
        logev(logger, logging.WARNING, "wallpaper_failed", "wallpaper backend failed",
              backend=backend, rc=cp.returncode)
        return False
    logev(logger, logging.INFO, "wallpaper", backend, **fields)
    return True


def _try_backend(backend: str, env: dict[str, str], logger: logging.Logger):
    # True = applied, False = backend failed (try the next one), None = skip (stop).
    wall = env.get("WALL", "")
    if backend == "xwallpaper":
        if not (Path(wall).is_file() and shutil.which("xwallpaper")):
            logev(logger, logging.INFO, "wallpaper_skip", "xwallpaper missing or file not found", file=wall)
            return None
        return _run_backend(["xwallpaper", "--zoom", wall], "xwallpaper", logger, file=wall)
    if backend == "fehbg":
        if not shutil.which("fehbg"):
            logev(logger, logging.INFO, "wallpaper_skip", "fehbg missing", file=wall)
            return None
        return _run_backend(["fehbg"], "fehbg", logger)
    if backend == "feh":
        if not (shutil.which("feh") and Path(wall).is_file()):
            logev(logger, logging.INFO, "wallpaper_skip", "feh missing or file not found", file=wall)
            return None
        return _run_backend(["feh", "--no-fehbg", "--bg-fill", wall], "feh", logger, file=wall)
    return _native_wallpaper(env, logger)


def _native_wallpaper(env: dict[str, str], logger: logging.Logger):
    if not _HAVE_PIL:
        logev(logger, logging.INFO, "wallpaper_native_skip",
              "native wallpaper needs the 'wallpaper' extra: pip install xrandrw[wallpaper]")
        return None
    wall = env.get("WALL", "")
    if not Path(wall).is_file():
        logev(logger, logging.INFO, "wallpaper_skip", "wallpaper file not found", file=wall)
        return None
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
        return True
    except Exception as e:                          # best-effort; a wallpaper failure must never break layout apply
        logev(logger, logging.WARNING, "wallpaper_native_fail", "native wallpaper failed", error=str(e))
        return False
