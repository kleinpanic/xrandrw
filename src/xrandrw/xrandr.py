from __future__ import annotations
import hashlib
import logging
from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path

from Xlib import X, display
from Xlib.ext import randr

from xrandrw.logging_utils import logev

@dataclass
class Output:
    name: str
    connected: bool
    primary: bool = False
    current_mode: tuple[int, int] | None = None
    # CRTC origin (x, y); None when the output has no CRTC. Additive (Phase 9,
    # WM-04) so dwm monitor_geometry can be matched by full position+size.
    position: tuple[int, int] | None = None
    modes: list[tuple[int, int, float, str]] = field(default_factory=list)  # (w,h,rate,flags "*+")
    edid_sha1: str | None = None

# Plain struct the live RandRReader hands to the pure mapper (oid is the loop var, not on oi).
_RROutput = namedtuple("_RROutput", "oid name connection crtc modes num_preferred")

def randr_resources_to_outputs(output_infos, crtc_infos, modes, primary_id) -> dict[str, Output]:
    modemap = {m.id: m for m in modes}
    outs: dict[str, Output] = {}
    for oi in output_infos:
        ci = crtc_infos.get(oi.crtc) if oi.crtc else None
        current_mode = (ci.width, ci.height) if ci else None
        position = (ci.x, ci.y) if ci else None
        cur_mode_id = ci.mode if ci else 0
        mode_tuples: list[tuple[int, int, float, str]] = []
        for i, mid in enumerate(oi.modes):
            # WR-02: a hotplug between the two RandR round-trips can leave the output
            # referencing a mode id absent from this snapshot; skip it rather than crash.
            m = modemap.get(mid)
            if m is None:
                continue
            rate = m.dot_clock / (m.h_total * m.v_total) if m.h_total and m.v_total else 0.0
            flags = ("*" if mid == cur_mode_id else "") + ("+" if i < oi.num_preferred else "")
            mode_tuples.append((m.width, m.height, round(rate, 2), flags))
        name = oi.name if isinstance(oi.name, str) else oi.name.decode()
        outs[name] = Output(
            name=name,
            connected=(oi.connection == randr.Connected),
            primary=(oi.oid == primary_id),
            current_mode=current_mode,
            position=position,
            modes=mode_tuples,
        )
    return outs

def edid_bytes_to_sha1(raw: bytes) -> str | None:
    if not raw:
        return None
    return hashlib.sha1(raw).hexdigest()

class RandRReader:
    """Thin main-thread-only live seam: opens its own Display per read, shares nothing.

    Xlib's Display is not thread-safe, so every method opens and closes its own
    connection on the calling thread (Pitfall 4).
    """

    def read(self, logger: logging.Logger | None = None) -> dict[str, Output]:
        d = self._open(logger)
        try:
            root = d.screen().root
            res = root.xrandr_get_screen_resources_current()
            ct = res.config_timestamp
            primary_id = root.xrandr_get_output_primary().output
            output_infos = []
            crtc_infos = {}
            for oid in res.outputs:
                oi = d.xrandr_get_output_info(oid, ct)
                output_infos.append(
                    _RROutput(oid, oi.name, oi.connection, oi.crtc, list(oi.modes), oi.num_preferred)
                )
                if oi.crtc and oi.crtc not in crtc_infos:
                    crtc_infos[oi.crtc] = d.xrandr_get_crtc_info(oi.crtc, ct)
            return randr_resources_to_outputs(output_infos, crtc_infos, res.modes, primary_id)
        finally:
            d.close()

    def version(self, logger: logging.Logger | None = None) -> tuple[int, int]:
        d = self._open(logger)
        try:
            v = d.xrandr_query_version()
            return (v.major_version, v.minor_version)
        finally:
            d.close()

    def events_supported(self, logger: logging.Logger | None = None) -> bool:
        # RandR < 1.5 never registers RRNotify subevents (randr.init gate) -> slow-poll only.
        return self.version(logger) >= (1, 5)

    def _open(self, logger: logging.Logger | None):
        try:
            return display.Display()
        except Exception as e:
            logev(logger, logging.ERROR, "xlib_connect_fail", "cannot open X display", error=str(e))
            raise

def read_xrandr(logger: logging.Logger) -> dict[str, Output]:
    return RandRReader().read(logger)

def edid_sysfs_read(name: str) -> bytes | None:
    base = Path("/sys/class/drm")
    for p in base.glob(f"card*-{name}/edid"):
        try:
            return p.read_bytes()
        except Exception:
            pass
    return None

def read_edid_native(d, oid, atom) -> bytes | None:
    # long_length is in 32-bit units: 128 units = 512 bytes covers 128/256-byte EDIDs.
    try:
        prop = d.xrandr_get_output_property(oid, atom, X.AnyPropertyType, 0, 128)
        raw = bytes(prop.value)
    except Exception:
        return None
    return raw or None

def read_edids(outs: dict[str, Output], logger: logging.Logger) -> None:
    # sysfs first, then native RandR EDID output-property
    for n, o in outs.items():
        if not o.connected:
            continue
        raw = edid_sysfs_read(n)
        if raw:
            o.edid_sha1 = edid_bytes_to_sha1(raw)
            logev(logger, logging.DEBUG, "edid_sysfs", "EDID via sysfs", output=n, sha1=o.edid_sha1)
    need = [n for n, o in outs.items() if o.connected and not o.edid_sha1]
    if not need:
        return
    try:
        d = display.Display()
    except Exception as e:
        logev(logger, logging.DEBUG, "edid_native_fail", "cannot open X display for EDID", error=str(e))
        return
    try:
        root = d.screen().root
        atom = d.get_atom("EDID")
        res = root.xrandr_get_screen_resources_current()
        ct = res.config_timestamp
        name_to_oid = {}
        for oid in res.outputs:
            oi = d.xrandr_get_output_info(oid, ct)
            nm = oi.name if isinstance(oi.name, str) else oi.name.decode()
            name_to_oid[nm] = oid
        for n in need:
            oid = name_to_oid.get(n)
            if oid is None:
                continue
            sha1 = edid_bytes_to_sha1(read_edid_native(d, oid, atom))
            if sha1:
                outs[n].edid_sha1 = sha1
                logev(logger, logging.DEBUG, "edid_native", "EDID via RandR property", output=n, sha1=sha1)
    finally:
        d.close()

def topology_hash(logger: logging.Logger | None = None) -> str:
    outs = RandRReader().read(logger)
    parts = []
    for name in sorted(outs):
        o = outs[name]
        # A disconnected head whose CRTC is still lit (unplug leaves it driving pixels) must
        # be visible to change detection or the daemon never heals it. Idle disconnected
        # connectors (no CRTC) stay out of the hash to avoid churn noise.
        if o.connected or o.current_mode is not None:
            parts.append(f"{o.name}|{o.connected}|{o.current_mode}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()
