"""Session fixtures for the L1 real-dwm/X functional harness (TEST-05).

These fixtures stand up, ONCE per test session, a throwaway headless X server
(plain ``Xvfb`` -- no root, no ``Xorg``, no ``xf86-video-dummy``, no tty) carved
into two dwm monitors with ``xrandr --setmonitor`` (RandR 1.5 -> Xinerama), and a
REAL patched dwm (built from the vendored ``dwm/dwm-ipc.diff`` + ``dwm/config.h``)
listening on a PRIVATE ``$DWM_SOCKET`` (never ``/tmp/dwm.sock`` -- T-14-01). The
tests then drive the entire ``xrandrw.dwmipc`` control+capture path against
reality with zero mocked dwm/Xlib.

ISOLATION CONTRACT: this harness NEVER touches ``DISPLAY=:0``, the developer's
running dwm, or ``/tmp/dwm.sock``. It picks a free display >= :99 and a private
socket under ``$RUNNER_TEMP`` / a short temp dir, and asserts the socket is not
``/tmp/dwm.sock`` before use.

SKIP-vs-FAIL policy (M2): on CI (``$GITHUB_ACTIONS`` set) a missing ``Xvfb`` /
``dwm-build-dep`` / a launch failure is an ERROR/FAILURE -- NEVER a skip -- so the
gating ``functional`` job can never report vacuous green. Locally (no CI) the same
condition ``pytest.skip``s cleanly so unit dev/CI stays fast. On a box that DOES
have Xvfb + the dwm build toolchain (like the maintainer's), the suite actually
RUNS GREEN rather than skipping -- it builds its own dwm and needs no live X.

Deterministic waits only: X/socket/selection readiness is confirmed by bounded
POLLING of a real condition (``xrandr --query`` rc, ``dwmipc.available``,
``get_monitors``), mirroring ``.planning/spikes/003.../probe_003_live.py``'s
bounded ``range(40)`` loop -- never a single fixed "hope it's ready" sleep.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

# Ensure the vendored src/ is importable even outside an editable install.
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from xrandrw import dwmipc  # noqa: E402  (after the sys.path shim above)

pytestmark = pytest.mark.functional

# --- pinned dwm provenance (must match dwm/dwm-ipc.diff header, T-14-02) ------
DWM_VERSION = "6.5"
_DWM_TARBALL = f"https://dl.suckless.org/dwm/dwm-{DWM_VERSION}.tar.gz"
_DWM_GIT = "https://git.suckless.org/dwm"
_HERE = Path(__file__).resolve().parent
_DIFF = _HERE / "dwm" / "dwm-ipc.diff"
_CONFIG_H = _HERE / "dwm" / "config.h"

# AF_UNIX sun_path hard limit; a socket path at/above this silently fails to bind.
_SUN_PATH_MAX = 108

_ON_CI = bool(os.environ.get("GITHUB_ACTIONS"))


# --- skip-vs-fail helper (M2) -------------------------------------------------

def _unavailable(reason: str) -> None:
    """FAIL on CI (no vacuous green), SKIP locally (fast unit dev/CI)."""
    if _ON_CI:
        pytest.fail(f"functional harness unavailable on CI (must not skip): {reason}")
    pytest.skip(f"functional harness unavailable locally: {reason}")


def _require_bins(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        _unavailable(f"missing binaries: {', '.join(missing)}")


def _poll(predicate, *, tries: int = 50, backoff: float = 0.1):
    """Bounded poll of a real readiness condition (mirrors probe_003_live).

    Returns the first truthy ``predicate()`` value, or None on timeout. The small
    inter-attempt ``backoff`` is poll pacing, NOT a fixed readiness sleep -- the
    loop exits the instant the real condition holds.
    """
    for _ in range(tries):
        try:
            val = predicate()
        except Exception:
            val = None
        if val:
            return val
        time.sleep(backoff)  # poll-backoff only; the condition above is the gate
    return None


def _reap(proc: subprocess.Popen | None) -> None:
    """SIGTERM then (if needed) SIGKILL a child, never raising."""
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            proc.send_signal(sig)
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return


def _free_display() -> int:
    """Return a free X display number >= 99, avoiding the developer's :0."""
    for n in range(99, 160):
        if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return n
    _unavailable("no free X display >= :99")
    raise RuntimeError("unreachable")  # pragma: no cover


# --- vendored dwm build (cached; keyed on diff+config.h+tag, M?/T-14-02) ------

def _cache_key() -> str:
    h = hashlib.sha1()
    h.update(DWM_VERSION.encode())
    h.update(_DIFF.read_bytes())
    h.update(_CONFIG_H.read_bytes())
    return h.hexdigest()[:16]


def _fetch_dwm_source(dest: Path) -> None:
    """Fetch pinned dwm ``DWM_VERSION`` source into ``dest`` (tarball, git fallback)."""
    try:
        with urllib.request.urlopen(_DWM_TARBALL, timeout=60) as r:  # noqa: S310 (pinned suckless URL)
            blob = r.read()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            root = tf.getnames()[0].split("/", 1)[0]
            tf.extractall(dest.parent)  # noqa: S202 (trusted pinned suckless tarball)
        (dest.parent / root).rename(dest)
        return
    except Exception:
        pass
    subprocess.run(["git", "clone", "--depth", "1", "--branch", DWM_VERSION, _DWM_GIT, str(dest)],
                   check=True, capture_output=True, timeout=120)


def _build_harness_dwm() -> str:
    """Build (or reuse a cached) patched dwm and return the binary path.

    Clones/downloads pinned dwm ``DWM_VERSION``, applies the vendored
    ``dwm-ipc.diff`` (which must apply with NO fuzz -- T-14-02) + the vendored
    ``config.h``, and ``make dwm``. Cached under ``$DWM_BUILD_CACHE`` /
    ``$RUNNER_TEMP`` / tempdir keyed on hash(diff+config.h+tag) so CI can cache it.
    """
    _require_bins("git", "make", "cc", "patch")
    base = Path(os.environ.get("DWM_BUILD_CACHE")
                or os.environ.get("RUNNER_TEMP")
                or tempfile.gettempdir())
    build = base / f"xrw-dwm-{_cache_key()}"
    binary = build / "dwm"
    if binary.is_file() and os.access(binary, os.X_OK):
        return str(binary)

    src = build / "src"
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    try:
        _fetch_dwm_source(src)
        # Patch must apply cleanly (no fuzz) to the pinned tree.
        applied = subprocess.run(["patch", "-p1", "--fuzz=0", "-i", str(_DIFF)],
                                 cwd=src, capture_output=True, text=True)
        if applied.returncode != 0:
            _unavailable(f"dwm-ipc.diff failed to apply cleanly: {applied.stdout}\n{applied.stderr}")
        shutil.copyfile(_CONFIG_H, src / "config.h")
        built = subprocess.run(["make", "dwm"], cwd=src, capture_output=True, text=True)
        if built.returncode != 0:
            _unavailable(f"dwm build failed (libyajl-dev/X11 headers?): {built.stderr[-800:]}")
        shutil.copyfile(src / "dwm", binary)
        binary.chmod(0o755)
    except subprocess.TimeoutExpired:
        _unavailable("dwm source fetch/build timed out")
    return str(binary)


# --- fixtures -----------------------------------------------------------------

@pytest.fixture(scope="session")
def x_display():
    """L1 PRIMARY: a plain Xvfb carved into two dwm monitors via ``--setmonitor``.

    Launches ``Xvfb :N -screen 0 3840x1080x24 +extension RANDR`` (N free, >= 99 --
    never :0), polls ``xrandr --query`` until the server answers, then carves two
    1920-wide RandR 1.5 monitors so dwm (Xinerama) sees >= 2 monitors. Yields the
    ``:N`` display string; SIGTERM-reaps Xvfb on teardown.
    """
    _require_bins("Xvfb", "xrandr")
    disp_n = _free_display()
    display = f":{disp_n}"
    assert display != ":0", "refusing to use the developer's live :0"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "3840x1080x24", "+extension", "RANDR"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    env = dict(os.environ, DISPLAY=display)
    try:
        ready = _poll(lambda: subprocess.run(["xrandr", "--query"], env=env,
                                             capture_output=True).returncode == 0)
        if not ready:
            _unavailable(f"Xvfb {display} never became queryable")
        # Carve two dwm monitors (RandR 1.5 -> Xinerama). On Xvfb --setmonitor
        # can emit a harmless BadValue on stderr yet still register the monitor;
        # we do NOT trust rc -- we verify via `xrandr --listmonitors` below.
        for name, geom in (("DUMMY-L", "1920/508x1080/285+0+0"),
                           ("DUMMY-R", "1920/508x1080/285+1920+0")):
            subprocess.run(["xrandr", "--setmonitor", name, geom, "none"],
                           env=env, capture_output=True)
        got = _poll(lambda: subprocess.run(["xrandr", "--listmonitors"], env=env,
                                           capture_output=True, text=True).stdout, tries=20)
        n_mon = 0 if got is None else sum(got.count(n) for n in ("DUMMY-L", "DUMMY-R"))
        if n_mon < 2:
            _unavailable("xrandr --setmonitor did not produce two monitors")
        yield display
    finally:
        _reap(proc)


@pytest.fixture(scope="session")
def dwm_ipc(x_display):
    """A REAL patched dwm on a PRIVATE ``$DWM_SOCKET`` (never /tmp/dwm.sock).

    Builds the vendored patched dwm, launches it as ``dwm -s "$DWM_SOCKET"`` on
    ``x_display`` so SERVER and CLIENT agree on the SAME private path (the client
    reads ``$DWM_SOCKET`` via ``dwmipc.DEFAULT_SOCK_PATH``), asserts the agreement
    and that it is NOT ``/tmp/dwm.sock`` (T-14-01), polls ``dwmipc.available``
    until the endpoint answers, and yields the socket path. SIGTERM-reaps dwm.
    """
    _require_bins("Xvfb")  # display already up; guards a torn-down session
    dwm_bin = _build_harness_dwm()

    # Private, SHORT socket path (AF_UNIX sun_path <= 108) under RUNNER_TEMP/tmp.
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    sockdir = tempfile.mkdtemp(prefix="xrw-dwm-", dir=base)
    sock = os.path.join(sockdir, "d.sock")
    assert sock != "/tmp/dwm.sock", "harness must never use the world-writable default socket"
    if len(sock) >= _SUN_PATH_MAX:
        _unavailable(f"socket path too long for AF_UNIX ({len(sock)} >= {_SUN_PATH_MAX})")
    # Wire BOTH client (env -> dwmipc.DEFAULT_SOCK_PATH) and server (-s) to `sock`.
    os.environ["DWM_SOCKET"] = sock

    env = dict(os.environ, DISPLAY=x_display, DWM_SOCKET=sock)
    proc = subprocess.Popen([dwm_bin, "-s", sock], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 1. Server must bind the SAME private path we asked for.
        bound = _poll(lambda: os.path.exists(sock))
        if not bound:
            _unavailable("dwm did not bind its -s socket (build/launch failure)")
        assert os.environ["DWM_SOCKET"] == sock  # client honors env (dwmipc.py:57)
        assert sock != "/tmp/dwm.sock"
        # 2. Endpoint must actually answer a real GET_MONITORS.
        live = _poll(lambda: dwmipc.available(sock))
        if not live:
            _unavailable("dwm-ipc endpoint never answered on the private socket")
        yield sock
    finally:
        _reap(proc)
        shutil.rmtree(sockdir, ignore_errors=True)


# --- no-vacuous-green floor bookkeeping (M2) ---------------------------------

def pytest_collection_modifyitems(config, items):
    """Record how many ``functional``-marked tests were collected (floor guard).

    ``test_functional_floor`` reads this to FAIL a silently-empty gating job on CI.
    Collection precedes execution, so the count is available to every test.
    """
    n = sum(1 for it in items if it.get_closest_marker("functional") is not None)
    config._xrw_functional_collected = n
