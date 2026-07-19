"""Headless fake AF_UNIX dwm-ipc server for testing (real socket, canned + hostile replies).

This is the test double that lets every dwm-ipc test run with NO real dwm and NO
X server. It binds a real ``socket.AF_UNIX`` ``SOCK_STREAM`` on a background
daemon thread and speaks the raw DWM-IPC wire protocol itself:

    magic[7]="DWM-IPC" | uint32 size (LE) | uint8 type | null-terminated JSON body

It deliberately does NOT import ``xrandrw.dwmipc`` -- it speaks bytes directly so
it can also validate the client's framing independently, and so the parse-side
hardening cannot accidentally be "tested against itself".

Reply modes
-----------
Valid: ``auto`` (reply keyed off the request type), ``monitors``, ``client``,
``run_command``, ``subscribe``.

Hostile (SEC-01 negative matrix, driven by 08-04): ``truncated_header``,
``oversized``, ``wrong_magic``, ``size_zero``, ``non_json``, ``wrong_schema``,
``close_mid_message``, ``hang``.

It captures every request it receives in :attr:`received` as
``(rtype, size, payload_bytes)`` so a test can assert on the framing the client
actually sent (e.g. run_command int-typed args).

Also runnable standalone for a self round-trip smoke check::

    python tests/dwmipc_fake_server.py
"""
from __future__ import annotations

import copy
import json
import socket
import struct
import threading
from typing import List, Optional, Tuple

MAGIC = b"DWM-IPC"
_HDR = struct.Struct("<7sIB")  # 7 + 4 + 1 = 12 bytes, packed

RUN_COMMAND = 0
GET_MONITORS = 1
GET_TAGS = 2
GET_LAYOUTS = 3
GET_DWM_CLIENT = 4
SUBSCRIBE = 5
EVENT = 6

# Canned valid payloads (shapes match the 08-01 validators).
VALID_MONITORS = [
    {
        "num": 0,
        "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        "layout": {"symbol": "[]="},
        "clients": {"all": [0x1400001]},
    }
]
VALID_CLIENT = {
    "name": "terminal",
    "tags": 1,
    "monitor_number": 0,
    "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
    "states": {"is_floating": False, "is_fullscreen": False},
}
VALID_RUN_COMMAND = {"result": "success"}
VALID_SUBSCRIBE = {"result": "success"}

_VALID_MODES = {"auto", "monitors", "client", "run_command", "subscribe", "stateful"}
_HOSTILE_MODES = {
    "truncated_header",
    "oversized",
    "wrong_magic",
    "size_zero",
    "non_json",
    "wrong_schema",
    "close_mid_message",
    "hang",
    "rst_on_accept",
    "slow_trickle",
}


def _frame(rtype: int, obj) -> bytes:
    """Frame ``obj`` as a well-formed, null-terminated DWM-IPC reply."""
    body = json.dumps(obj).encode() + b"\x00"  # size INCLUDES the terminator
    return _HDR.pack(MAGIC, len(body), rtype) + body


class FakeDwmServer:
    """A real AF_UNIX dwm-ipc server on a daemon thread. Context-managed and thread-safe.

    Usage::

        with FakeDwmServer(tmp_path / "dwm.sock", mode="auto") as srv:
            ...  # srv.path is the bound socket path
    """

    def __init__(self, path, mode: str = "auto", *, monitors=None, client=None,
                 run_result=None, oversized_size: int = 256 * 1024 * 1024,
                 trickle_interval: float = 0.05, clients=None, select_lag: int = 0):
        if mode not in _VALID_MODES and mode not in _HOSTILE_MODES:
            raise ValueError(f"unknown fake-server mode {mode!r}")
        self.path = str(path)
        self.mode = mode
        self.monitors = monitors if monitors is not None else VALID_MONITORS
        self.client = client if client is not None else VALID_CLIENT
        self.run_result = run_result if run_result is not None else VALID_RUN_COMMAND
        self.oversized_size = oversized_size
        self.trickle_interval = trickle_interval
        # --- stateful mode: a mutable per-window dwm model (additive; only used
        # when mode == "stateful"). Guarded by _lock because the server runs on a
        # daemon thread while the test thread drives select/set_geometry/state.
        self._lock = threading.Lock()
        self._clients: dict = {}          # int xid -> client dict (mutable)
        self._selected: Optional[int] = None
        self._n_monitors = 1
        # CR-01: model dwm's async selection settle. When select_lag > 0 a
        # select() records a PENDING selection that only becomes the effective
        # _selected after `select_lag` GET_MONITORS polls (or an explicit
        # settle()) -- mirroring the real gap where _NET_ACTIVE_WINDOW is
        # delivered over the X channel while the next run_command travels the
        # SEPARATE ipc socket and acts on the still-current selected client.
        self._select_lag = int(select_lag)
        self._pending_select: Optional[int] = None
        self._pending_lag = 0
        if clients is not None:
            for c in clients:
                self._clients[int(c["xid"])] = copy.deepcopy(c)
            if self._clients:
                self._n_monitors = max(int(c["monitor_number"])
                                       for c in self._clients.values()) + 1
            self._n_monitors = max(1, self._n_monitors)
        # Every request the server received, as (rtype, size, payload_bytes).
        self.received: List[Tuple[int, int, bytes]] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(16)
        self._sock.settimeout(0.2)  # so the accept loop can observe _stop
        self._thread = threading.Thread(target=self._serve, name="fake-dwm-ipc", daemon=True)

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeDwmServer":
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5.0)

    def __enter__(self) -> "FakeDwmServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    # --- serving -----------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._handle(conn)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    @staticmethod
    def _recvn(conn: socket.socket, n: int) -> Optional[bytes]:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _read_request(self, conn: socket.socket) -> Optional[Tuple[int, bytes]]:
        header = self._recvn(conn, _HDR.size)
        if header is None:
            return None
        _magic, size, rtype = _HDR.unpack(header)
        payload = self._recvn(conn, size) if size else b""
        if payload is None:
            payload = b""
        self.received.append((rtype, size, payload))
        return rtype, payload

    def _resolve_client(self, payload: bytes):
        """Return the client dict for a GET_DWM_CLIENT request.

        ``self.client`` may be a plain dict (returned for every window) or a
        callable ``xid -> dict`` so a test can vary the reply per window (e.g. a
        client whose ``monitor_number`` matches the monitor it lives on).
        """
        if not callable(self.client):
            return self.client
        xid = None
        try:
            # The request payload is NUL-terminated (size includes the terminator);
            # strip it before decoding or json.loads rejects the trailing \x00.
            xid = json.loads(payload.rstrip(b"\x00").decode()).get("client_window_id")
        except (ValueError, AttributeError):
            xid = None
        return self.client(xid)

    # --- stateful dwm model (mode == "stateful" only) ----------------------

    def select(self, xid) -> None:
        """Set which client the next run_command verb acts on (X-focus bridge).

        When ``select_lag`` is 0 (default) the selection is immediate. When
        ``select_lag`` > 0 the selection is PENDING and only becomes effective
        after that many GET_MONITORS polls (or an explicit :meth:`settle`),
        modelling dwm's async X-channel focus settle (CR-01).
        """
        with self._lock:
            if self._select_lag > 0:
                self._pending_select = int(xid)
                self._pending_lag = self._select_lag
            else:
                self._selected = int(xid)

    def set_select_lag(self, n: int) -> None:
        """Enable/disable the lagged-select model at runtime (CR-01 regression)."""
        with self._lock:
            self._select_lag = int(n)

    def settle(self) -> None:
        """Force any pending (lagged) selection to take effect immediately."""
        with self._lock:
            if self._pending_select is not None:
                self._selected = self._pending_select
                self._pending_select = None
                self._pending_lag = 0

    def set_geometry(self, xid, geometry) -> None:
        """Overwrite a client's current geometry (mirrors ConfigureWindow)."""
        with self._lock:
            c = self._clients.get(int(xid))
            if c is not None:
                c.setdefault("geometry", {})["current"] = dict(geometry)

    def remove(self, xid) -> None:
        """Drop a client from the model (mirrors a window being CLOSED).

        Used by the UX-01 focus-restore tests to make the pre-restore focused
        window vanish mid-cycle. If it was the selected client the selection is
        cleared too, exactly as dwm's ``unmanage`` -> ``focus(NULL)`` would.
        """
        with self._lock:
            self._clients.pop(int(xid), None)
            if self._selected == int(xid):
                self._selected = None
            if self._pending_select == int(xid):
                self._pending_select = None

    def state(self, xid) -> Optional[dict]:
        """Return a snapshot copy of one client's mutable state for assertions."""
        with self._lock:
            c = self._clients.get(int(xid))
            if c is None:
                return None
            return {
                "monitor_number": c["monitor_number"],
                "tags": c["tags"],
                "is_floating": c["states"]["is_floating"],
                "is_fullscreen": c["states"]["is_fullscreen"],
                "geometry": copy.deepcopy(c["geometry"]),
            }

    def _build_monitors_locked(self) -> list:
        # CR-01: report per-monitor selection so a client can confirm dwm's
        # selected client caught up to a focus before issuing a verb. The report
        # reflects the CURRENT _selected; a pending (lagged) select is promoted
        # AFTER building this reply, so it takes effect on a LATER poll.
        sel = self._selected
        mons = []
        for num in range(self._n_monitors):
            xids = [xid for xid, c in self._clients.items()
                    if int(c["monitor_number"]) == num]
            mon_sel = sel if (sel in xids) else None
            mons.append({
                "num": num,
                "monitor_geometry": {"x": num * 1920, "y": 0, "width": 1920, "height": 1080},
                "layout": {"symbol": "[]="},
                "is_selected": mon_sel is not None,
                "clients": {"all": xids, "selected": mon_sel},
            })
        if self._pending_select is not None:
            self._pending_lag -= 1
            if self._pending_lag <= 0:
                self._selected = self._pending_select
                self._pending_select = None
        return mons

    @staticmethod
    def _decode_xid(payload: bytes) -> Optional[int]:
        try:
            return int(json.loads(payload.rstrip(b"\x00").decode())["client_window_id"])
        except (ValueError, KeyError, AttributeError, TypeError):
            return None

    def _client_view_locked(self, client: dict) -> dict:
        view = copy.deepcopy(client)
        view.pop("xid", None)
        return view

    @staticmethod
    def _not_found_client(xid) -> dict:
        # Still shaped to satisfy validate_client (name/tags/geometry/states).
        return {
            "name": "", "tags": 0, "monitor_number": -1,
            "geometry": {"current": {"x": 0, "y": 0, "width": 0, "height": 0}},
            "states": {"is_floating": False, "is_fullscreen": False},
        }

    def _apply_run_command_locked(self, payload: bytes) -> None:
        try:
            req = json.loads(payload.rstrip(b"\x00").decode())
            command = req.get("command")
            args = req.get("args") or []
        except (ValueError, AttributeError):
            return  # malformed -> dwm no-op
        if self._selected is None:
            return  # nothing selected -> dwm no-op
        c = self._clients.get(self._selected)
        if c is None:
            return
        if command == "tagmon" and args:
            c["monitor_number"] = (int(c["monitor_number"]) + int(args[0])) % self._n_monitors
        elif command == "tag" and args:
            c["tags"] = int(args[0])
        elif command == "togglefloating":
            c["states"]["is_floating"] = not c["states"]["is_floating"]
        # unknown command -> success reply without mutation (dwm no-op)

    def _stateful_reply(self, rtype: int, payload: bytes) -> bytes:
        with self._lock:
            if rtype == GET_MONITORS:
                return _frame(GET_MONITORS, self._build_monitors_locked())
            if rtype == GET_DWM_CLIENT:
                xid = self._decode_xid(payload)
                c = self._clients.get(xid) if xid is not None else None
                if c is None:
                    return _frame(GET_DWM_CLIENT, self._not_found_client(xid))
                return _frame(GET_DWM_CLIENT, self._client_view_locked(c))
            if rtype == RUN_COMMAND:
                self._apply_run_command_locked(payload)
                return _frame(RUN_COMMAND, self.run_result)
            if rtype == SUBSCRIBE:
                return _frame(SUBSCRIBE, VALID_SUBSCRIBE)
            return _frame(GET_MONITORS, self._build_monitors_locked())

    def _valid_reply(self, rtype: int, payload: bytes = b"") -> bytes:
        if self.mode == "stateful":
            return self._stateful_reply(rtype, payload)
        if self.mode == "monitors" or (self.mode == "auto" and rtype == GET_MONITORS):
            return _frame(GET_MONITORS, self.monitors)
        if self.mode == "client" or (self.mode == "auto" and rtype == GET_DWM_CLIENT):
            return _frame(GET_DWM_CLIENT, self._resolve_client(payload))
        if self.mode == "run_command" or (self.mode == "auto" and rtype == RUN_COMMAND):
            return _frame(RUN_COMMAND, self.run_result)
        if self.mode == "subscribe" or (self.mode == "auto" and rtype == SUBSCRIBE):
            return _frame(SUBSCRIBE, VALID_SUBSCRIBE)
        # Fallback: echo a monitors list.
        return _frame(GET_MONITORS, self.monitors)

    def _handle(self, conn: socket.socket) -> None:
        mode = self.mode

        if mode == "rst_on_accept":
            # Abort the connection right after accept(), BEFORE reading anything:
            # SO_LINGER with a zero timeout makes close() send a TCP/AF_UNIX RST,
            # so the client's sendall()/recv() sees an OSError (ECONNRESET /
            # EPIPE) rather than a clean FIN. Exercises the send-side OSError path.
            try:
                linger = struct.pack("ii", 1, 0)  # l_onoff=1, l_linger=0 -> RST
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
                conn.close()
            except OSError:
                pass
            return

        if mode == "hang":
            # Accept + read the request, then never reply until shutdown, to
            # exercise the client's socket timeout (no unbounded block).
            self._read_request(conn)
            self._stop.wait(timeout=10.0)
            return

        req = self._read_request(conn)
        rtype = req[0] if req is not None else GET_MONITORS
        payload = req[1] if req is not None else b""

        if mode == "slow_trickle":
            # Drip a well-formed reply one byte at a time with a gap SHORTER than
            # the client's per-recv timeout, so no single recv() ever trips
            # socket.timeout -- only a TOTAL wall-clock deadline can bound this.
            reply = self._valid_reply(rtype, payload)
            for i in range(len(reply)):
                if self._stop.is_set():
                    return
                try:
                    conn.sendall(reply[i:i + 1])
                except OSError:
                    return
                self._stop.wait(timeout=self.trickle_interval)
            return

        if mode == "truncated_header":
            conn.sendall(b"DWM-")  # < 12 bytes, then close
            return
        if mode == "wrong_magic":
            body = json.dumps(self.monitors).encode() + b"\x00"
            conn.sendall(struct.pack("<7sIB", b"NOPE-IP", len(body), rtype) + body)
            return
        if mode == "size_zero":
            conn.sendall(struct.pack("<7sIB", MAGIC, 0, rtype))
            return
        if mode == "oversized":
            # Advertise a size far above MAX_REPLY_SIZE but send little/no body.
            conn.sendall(struct.pack("<7sIB", MAGIC, self.oversized_size, rtype))
            return
        if mode == "close_mid_message":
            body = json.dumps(self.monitors).encode() + b"\x00"
            conn.sendall(struct.pack("<7sIB", MAGIC, len(body), rtype) + body[:2])
            return  # close before the full body arrives
        if mode == "non_json":
            body = b"\xff\xfe not valid json at all \x00"
            conn.sendall(struct.pack("<7sIB", MAGIC, len(body), rtype) + body)
            return
        if mode == "wrong_schema":
            # Valid JSON, wrong shape: a bare int where a container is expected.
            body = json.dumps(12345).encode() + b"\x00"
            conn.sendall(struct.pack("<7sIB", MAGIC, len(body), rtype) + body)
            return

        # Valid modes.
        conn.sendall(self._valid_reply(rtype, payload))


def _self_smoke() -> None:
    """Standalone self round-trip: raw client speaks the protocol to the fixture.

    Directly checks the fixture frames a correct reply, independent of the
    production client (plan-checker note #1).
    """
    import os
    import tempfile

    d = tempfile.mkdtemp()
    path = os.path.join(d, "dwm.sock")
    with FakeDwmServer(path, mode="auto"):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(2.0)
        c.connect(path)
        payload = b"\x00"
        c.sendall(_HDR.pack(MAGIC, len(payload), GET_MONITORS) + payload)
        header = c.recv(_HDR.size)
        magic, size, rtype = _HDR.unpack(header)
        assert magic == MAGIC, magic
        assert rtype == GET_MONITORS, rtype
        body = b""
        while len(body) < size:
            body += c.recv(size - len(body))
        obj = json.loads(body.rstrip(b"\x00"))
        assert isinstance(obj, list) and obj and "num" in obj[0], obj
        c.close()
    print("fake-dwm-ipc self round-trip: ok")


if __name__ == "__main__":
    _self_smoke()
