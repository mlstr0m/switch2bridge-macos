"""
DSU (cemuhook) server for Switch2 Bridge
========================================

Exposes the controller as a DSU pad on UDP so emulators that speak the
cemuhook protocol (Dolphin, Cemu, and Ryujinx for motion) can read the
sticks as true analog inputs — no driver needed.

Protocol reference: https://github.com/v1993/cemuhook-protocol
"""

import logging
import socket
import struct
import threading
import time
import zlib

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1001
SERVER_ID = 0x53324252  # "S2BR"

MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_DATA = 0x100002

# Locally-administered, made-up MAC so clients can identify the pad
PAD_MAC = b"\x02S2BRG"
PAD_SLOT = 0
MODEL_FULL_GYRO = 2
CONN_BLUETOOTH = 2
BATTERY_NA = 0x00

# Clients must re-send a data request at least this often to keep receiving
CLIENT_TIMEOUT = 5.0

# DSU buttons, mapped positionally from the Switch layout
# (A→Circle, B→Cross, X→Triangle, Y→Square, -→Share, +→Options,
#  HOME→PS, CAPT→Touch; GL/GR/C have no DSU equivalent)
_BUTTONS1 = (  # payload byte 36: (dsu bit, switch button name)
    (0x01, '-'), (0x02, 'LS'), (0x04, 'RS'), (0x08, '+'),
    (0x10, 'DUP'), (0x20, 'DRIGHT'), (0x40, 'DDOWN'), (0x80, 'DLEFT'),
)
_BUTTONS2 = (  # payload byte 37
    (0x01, 'ZL'), (0x02, 'ZR'), (0x04, 'L'), (0x08, 'R'),
    (0x10, 'X'), (0x20, 'A'), (0x40, 'B'), (0x80, 'Y'),
)
# Analog button bytes 44..55, in protocol order
_ANALOG_ORDER = (
    'DLEFT', 'DDOWN', 'DRIGHT', 'DUP',
    'Y', 'B', 'A', 'X',
    'R', 'L', 'ZR', 'ZL',
)


def _axis_to_byte(value):
    """[-1.0, 1.0] → [0, 255] with 128 center (255 = right/up)."""
    return max(0, min(255, int(round((value + 1.0) * 127.5))))


class DSUServer:
    """Threaded UDP server. `push()` may be called from any thread."""

    def __init__(self, host="127.0.0.1", port=26760):
        self.host = host
        self.port = port
        self.last_error = None  # consumed by the UI tick
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients = {}  # addr -> last request time
        self._counter = 0
        self._connected = False

    # --- lifecycle ---

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return True
        self._stop.clear()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.settimeout(1.0)
        except OSError as e:
            log.warning("DSU server could not bind %s:%s: %s", self.host, self.port, e)
            self.last_error = (
                f"DSU server could not listen on {self.host}:{self.port}: {e}"
            )
            return False
        self._sock = sock
        self.port = sock.getsockname()[1]  # resolve port 0 → real port
        self._thread = threading.Thread(
            target=self._serve, name="dsu-server", daemon=True
        )
        self._thread.start()
        log.info("DSU server listening on %s:%s", self.host, self.port)
        return True

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(2.0)
        self._thread = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        with self._lock:
            self._clients.clear()

    def set_connected(self, connected):
        self._connected = bool(connected)

    def client_count(self):
        now = time.monotonic()
        with self._lock:
            return sum(1 for t in self._clients.values() if now - t < CLIENT_TIMEOUT)

    # --- packet builders ---

    def _packet(self, msg_type, payload):
        data = struct.pack("<I", msg_type) + payload
        header = struct.pack(
            "<4sHHII", b"DSUS", PROTOCOL_VERSION, len(data), 0, SERVER_ID
        )
        pkt = bytearray(header + data)
        struct.pack_into("<I", pkt, 8, zlib.crc32(bytes(pkt)) & 0xFFFFFFFF)
        return bytes(pkt)

    def _port_info(self, slot):
        if slot == PAD_SLOT:
            state = 2 if self._connected else 0
            return struct.pack(
                "<BBBB6sB", slot, state, MODEL_FULL_GYRO, CONN_BLUETOOTH,
                PAD_MAC, BATTERY_NA,
            )
        return struct.pack("<BBBB6sB", slot, 0, 0, 0, b"\x00" * 6, 0)

    def _data_packet(self, buttons, lx, ly, rx, ry):
        b1 = 0
        for bit, name in _BUTTONS1:
            if buttons.get(name):
                b1 |= bit
        b2 = 0
        for bit, name in _BUTTONS2:
            if buttons.get(name):
                b2 |= bit

        self._counter = (self._counter + 1) & 0xFFFFFFFF
        payload = self._port_info(PAD_SLOT)
        payload += struct.pack("<BI", 1, self._counter)  # connected + counter
        payload += struct.pack(
            "<BBBB", b1, b2,
            0xFF if buttons.get('HOME') else 0,
            0xFF if buttons.get('CAPT') else 0,
        )
        payload += struct.pack(
            "<BBBB",
            _axis_to_byte(lx), _axis_to_byte(ly),
            _axis_to_byte(rx), _axis_to_byte(ry),
        )
        payload += bytes(
            0xFF if buttons.get(name) else 0 for name in _ANALOG_ORDER
        )
        payload += b"\x00" * 12  # two (inactive) touch structs
        payload += struct.pack("<Q", time.monotonic_ns() // 1000)
        payload += struct.pack("<6f", 0, 0, 0, 0, 0, 0)  # accel + gyro (not decoded yet)
        return self._packet(MSG_DATA, payload)

    # --- server loop ---

    def _serve(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed
            try:
                self._handle(data, addr)
            except Exception:
                log.exception("DSU: failed to handle request from %s", addr)
        log.info("DSU server stopped")

    def _handle(self, data, addr):
        if len(data) < 20 or data[:4] != b"DSUC":
            return
        (msg_type,) = struct.unpack_from("<I", data, 16)

        if msg_type == MSG_VERSION:
            reply = self._packet(
                MSG_VERSION, struct.pack("<H", PROTOCOL_VERSION) + b"\x00\x00"
            )
            self._sock.sendto(reply, addr)

        elif msg_type == MSG_PORTS:
            if len(data) < 24:
                return
            (count,) = struct.unpack_from("<i", data, 20)
            slots = data[24:24 + max(0, min(count, 4))]
            for slot in slots:
                reply = self._packet(MSG_PORTS, self._port_info(slot) + b"\x00")
                self._sock.sendto(reply, addr)

        elif msg_type == MSG_DATA:
            # Registration request: keep streaming to this client until timeout
            with self._lock:
                self._clients[addr] = time.monotonic()

    # --- input feed (called from the BLE thread) ---

    def push(self, buttons, lx, ly, rx, ry):
        sock = self._sock
        if sock is None or self._stop.is_set():
            return
        now = time.monotonic()
        with self._lock:
            stale = [a for a, t in self._clients.items() if now - t > CLIENT_TIMEOUT]
            for a in stale:
                del self._clients[a]
            targets = list(self._clients)
        if not targets:
            return
        pkt = self._data_packet(buttons, lx, ly, rx, ry)
        for addr in targets:
            try:
                sock.sendto(pkt, addr)
            except OSError as e:
                log.warning("DSU send to %s failed: %s", addr, e)
