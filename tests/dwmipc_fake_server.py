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

_VALID_MODES = {"auto", "monitors", "client", "run_command", "subscribe"}
_HOSTILE_MODES = {
    "truncated_header",
    "oversized",
    "wrong_magic",
    "size_zero",
    "non_json",
    "wrong_schema",
    "close_mid_message",
    "hang",
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
                 run_result=None, oversized_size: int = 256 * 1024 * 1024):
        if mode not in _VALID_MODES and mode not in _HOSTILE_MODES:
            raise ValueError(f"unknown fake-server mode {mode!r}")
        self.path = str(path)
        self.mode = mode
        self.monitors = monitors if monitors is not None else VALID_MONITORS
        self.client = client if client is not None else VALID_CLIENT
        self.run_result = run_result if run_result is not None else VALID_RUN_COMMAND
        self.oversized_size = oversized_size
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

    def _valid_reply(self, rtype: int) -> bytes:
        if self.mode == "monitors" or (self.mode == "auto" and rtype == GET_MONITORS):
            return _frame(GET_MONITORS, self.monitors)
        if self.mode == "client" or (self.mode == "auto" and rtype == GET_DWM_CLIENT):
            return _frame(GET_DWM_CLIENT, self.client)
        if self.mode == "run_command" or (self.mode == "auto" and rtype == RUN_COMMAND):
            return _frame(RUN_COMMAND, self.run_result)
        if self.mode == "subscribe" or (self.mode == "auto" and rtype == SUBSCRIBE):
            return _frame(SUBSCRIBE, VALID_SUBSCRIBE)
        # Fallback: echo a monitors list.
        return _frame(GET_MONITORS, self.monitors)

    def _handle(self, conn: socket.socket) -> None:
        mode = self.mode

        if mode == "hang":
            # Accept + read the request, then never reply until shutdown, to
            # exercise the client's socket timeout (no unbounded block).
            self._read_request(conn)
            self._stop.wait(timeout=10.0)
            return

        req = self._read_request(conn)
        rtype = req[0] if req is not None else GET_MONITORS

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
        conn.sendall(self._valid_reply(rtype))


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
