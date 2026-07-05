from __future__ import annotations
from collections import namedtuple

from Xlib.ext import randr

# Plain structs mirroring the python-xlib RandR reply objects the pure mapper reads.
OutputInfo = namedtuple("OutputInfo", "oid name connection crtc modes num_preferred")
CrtcInfo = namedtuple("CrtcInfo", "width height mode")
ModeInfo = namedtuple("ModeInfo", "id width height dot_clock h_total v_total")

MODES = [
    # 800x480 @ 60.0 (current + preferred on DSI-1)
    ModeInfo(id=1, width=800, height=480, dot_clock=30000000, h_total=1000, v_total=500),
    # 640x480 @ 59.52 (neither current nor preferred)
    ModeInfo(id=2, width=640, height=480, dot_clock=25000000, h_total=800, v_total=525),
    # 1024x768 with h_total=0 to exercise the div-by-zero guard -> rate 0.0
    ModeInfo(id=3, width=1024, height=768, dot_clock=65000000, h_total=0, v_total=806),
]

CRTC_INFOS = {
    64: CrtcInfo(width=800, height=480, mode=1),
    # Stale CRTC left on a Disconnected output (live: HDMI-1 connection=1 but crtc=65, 1600x900)
    65: CrtcInfo(width=1600, height=900, mode=0),
}

PRIMARY_ID = 100

OUTPUT_INFOS = [
    OutputInfo(oid=100, name="DSI-1", connection=randr.Connected, crtc=64, modes=[1, 2, 3], num_preferred=1),
    OutputInfo(oid=101, name="HDMI-1", connection=randr.Disconnected, crtc=65, modes=[], num_preferred=0),
    # bytes name + no CRTC (oi.crtc == 0)
    OutputInfo(oid=102, name=b"HDMI-2", connection=randr.Disconnected, crtc=0, modes=[], num_preferred=0),
]
