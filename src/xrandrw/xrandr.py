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

    @property
    def is_lit(self) -> bool:
        """True iff this output has a LIVE CRTC -- it is driving pixels right now.

        THE single definition of CRTC liveness (WR-03, 14-08). Before this there
        were four divergent spellings of the same idea:

          relocate.py    `o.current_mode is not None`                    (edge predicate)
          apply.py       `o.position is None and o.current_mode is None` (scrub skip)
          xrandr.py      `o.connected or o.current_mode is not None`     (hash inclusion)
          test harness   `position is not None and current_mode is not None`

        They agreed only because :func:`randr_resources_to_outputs` derives BOTH
        fields from one ``ci``. That is an accident of one producer, and the test
        model had ALREADY drifted to a different predicate from production. The
        relocation edge predicate and the apply scrub predicate are now
        load-bearing AGAINST each other -- that pairing IS the architecture of the
        replug-bounce fix -- so a future ``Output`` producer that populates one
        field but not the other would desynchronise them SILENTLY. Routing all
        four through this property makes that impossible.

        Both fields are required: a dark output has ``crtc=None`` hence BOTH
        ``position`` and ``current_mode`` None, so the half-populated state is not
        reachable from the current producer, and defining liveness over both means
        any producer that ever creates it is treated as dark (the conservative
        direction -- we never hand a mutating verb a head we are unsure about).
        """
        return self.current_mode is not None and self.position is not None

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

def topology_hash_from_outputs(outs: dict[str, Output]) -> str:
    # PURE half of topology_hash (extracted Phase 14, 14-08): the digest reasons only over an
    # output map, so a headless test can drive the watch loop's change detection with the
    # PRODUCTION predicate instead of a hand-scripted string. No behavior change.
    parts = []
    for name in sorted(outs):
        o = outs[name]
        # A disconnected head whose CRTC is still lit (unplug leaves it driving pixels) must
        # be visible to change detection or the daemon never heals it. Idle disconnected
        # connectors (no CRTC) stay out of the hash to avoid churn noise.
        if o.connected or o.is_lit:
            parts.append(f"{o.name}|{o.connected}|{o.current_mode}")
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()

def unhealed_outputs(outs: dict[str, Output]) -> list[str]:
    """Connectors that are CONNECTED but have NO live CRTC -- a KNOWN-BAD state.

    The mirror image of the Phase-4.1 lesson above. ``topology_hash_from_outputs``
    deliberately makes *disconnected-and-lit* visible to change detection so the
    daemon heals it. The converse -- *connected-and-dark* -- became a reachable
    RESTING state when the scrub moved below read #2 (14-08): if both reads see a
    head disconnected, the apply issues ``--off`` and placement (which filters on
    the same read) issues no matching ``--auto``, so the CRTC stays dark. If HPD
    then returns DURING that apply -- the live ordering, a ~2.3 s window -- the
    post-apply ``settled`` hash absorbs ``HDMI-1|True|None``. That digest is
    STABLE, so the watch loop's ``cur == last_hash`` short-circuit fires forever:
    no further apply, no ``returned`` edge, no restore, and the external monitor
    stays BLACK. That is strictly worse than the stranded-windows bug being fixed
    (pre-14-08 the same apply re-lit the head, so the monitor at least worked).

    A hash is the wrong tool for a liveness invariant: hash STABILITY is exactly
    the wrong signal when the state is known-bad. So this is an explicit
    change-EQUIVALENT condition rather than an attempt to make the digest express
    it. PURE: no I/O.
    """
    return sorted(name for name, o in outs.items() if o.connected and not o.is_lit)


def topology_hash(logger: logging.Logger | None = None) -> str:
    return topology_hash_from_outputs(RandRReader().read(logger))
