"""Headless tests for the DSU (cemuhook) server — real UDP round-trips."""
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dsu_server as D
from dsu_server import DSUServer

FAILURES = []

def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)

def client_packet(msg_type, payload=b""):
    data = struct.pack("<I", msg_type) + payload
    header = struct.pack("<4sHHII", b"DSUC", 1001, len(data), 0, 0xCAFE)
    pkt = bytearray(header + data)
    struct.pack_into("<I", pkt, 8, zlib.crc32(bytes(pkt)) & 0xFFFFFFFF)
    return bytes(pkt)

def crc_ok(pkt):
    stored = struct.unpack_from("<I", pkt, 8)[0]
    z = bytearray(pkt)
    struct.pack_into("<I", z, 8, 0)
    return stored == (zlib.crc32(bytes(z)) & 0xFFFFFFFF)

# ============ lifecycle ============
print("== lifecycle ==")
srv = DSUServer(port=0)  # ephemeral port
check("start ok", srv.start() is True)
check("running", srv.running)
check("port resolved", srv.port != 0, srv.port)

srv2 = DSUServer(port=srv.port)
check("port conflict -> start False", srv2.start() is False)
check("port conflict -> error set", srv2.last_error and str(srv.port) in srv2.last_error,
      srv2.last_error)

cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
cli.settimeout(2.0)
cli.bind(("127.0.0.1", 0))
addr = ("127.0.0.1", srv.port)

# ============ version request ============
print("== version ==")
cli.sendto(client_packet(D.MSG_VERSION), addr)
pkt, _ = cli.recvfrom(1024)
check("version: magic", pkt[:4] == b"DSUS")
check("version: crc", crc_ok(pkt))
check("version: msg type", struct.unpack_from("<I", pkt, 16)[0] == D.MSG_VERSION)
check("version: value", struct.unpack_from("<H", pkt, 20)[0] == 1001)
check("version: length field", struct.unpack_from("<H", pkt, 6)[0] == len(pkt) - 16)

# ============ ports request ============
print("== ports ==")
cli.sendto(client_packet(D.MSG_PORTS, struct.pack("<i", 2) + bytes([0, 1])), addr)
replies = [cli.recvfrom(1024)[0] for _ in range(2)]
p0 = next(p for p in replies if p[20] == 0)
p1 = next(p for p in replies if p[20] == 1)
check("ports: crc", all(crc_ok(p) for p in replies))
check("ports: slot0 disconnected before pad", p0[21] == 0)
check("ports: slot0 model full gyro", p0[22] == 2)
check("ports: slot0 mac", p0[24:30] == D.PAD_MAC)
check("ports: slot1 empty", p1[21] == 0 and p1[24:30] == b"\x00" * 6)
check("ports: reply length", len(p0) == 32, len(p0))

srv.set_connected(True)
cli.sendto(client_packet(D.MSG_PORTS, struct.pack("<i", 1) + bytes([0])), addr)
p0, _ = cli.recvfrom(1024)
check("ports: slot0 connected after pad", p0[21] == 2)

# ============ subscribe + data ============
print("== data ==")
cli.sendto(client_packet(D.MSG_DATA, bytes([0, 0]) + b"\x00" * 6), addr)
time.sleep(0.1)  # let the server register the client
check("client registered", srv.client_count() == 1)

buttons = {'A': 1, 'ZL': 1, 'DUP': 1, '-': 1, 'HOME': 1, 'CAPT': 0}
srv.push(buttons, 0.5, -1.0, 0.0, 1.0)
pkt, _ = cli.recvfrom(1024)
check("data: 100 bytes", len(pkt) == 100, len(pkt))
check("data: crc", crc_ok(pkt))
check("data: length field", struct.unpack_from("<H", pkt, 6)[0] == 84)
check("data: msg type", struct.unpack_from("<I", pkt, 16)[0] == D.MSG_DATA)
check("data: slot state connected", pkt[21] == 2)
check("data: connected byte", pkt[31] == 1)
c1 = struct.unpack_from("<I", pkt, 32)[0]

b1, b2_, home, touch = pkt[36], pkt[37], pkt[38], pkt[39]
check("data: minus->Share", b1 & 0x01)
check("data: DUP bit", b1 & 0x10)
check("data: no other b1 bits", b1 == 0x11, hex(b1))
check("data: A->Circle", b2_ & 0x20)
check("data: ZL->L2", b2_ & 0x01)
check("data: no other b2 bits", b2_ == 0x21, hex(b2_))
check("data: HOME->PS", home == 0xFF)
check("data: CAPT off", touch == 0x00)

lx, ly, rx, ry = pkt[40], pkt[41], pkt[42], pkt[43]
check("data: lx 0.5 -> ~191", lx == 191, lx)
check("data: ly -1.0 -> 0", ly == 0)
check("data: rx 0.0 -> 128", rx == 128)
check("data: ry 1.0 -> 255", ry == 255)

# analog button bytes: DLEFT,DDOWN,DRIGHT,DUP, Y,B,A,X, R,L,ZR,ZL
analog = pkt[44:56]
check("data: analog DUP", analog[3] == 0xFF)
check("data: analog A", analog[6] == 0xFF)
check("data: analog ZL", analog[11] == 0xFF)
check("data: analog others zero", analog[0] == 0 and analog[4] == 0)

ts = struct.unpack_from("<Q", pkt, 68)[0]
check("data: motion timestamp set", ts > 0)
floats = struct.unpack_from("<6f", pkt, 76)
check("data: motion zeroed", all(f == 0.0 for f in floats))

srv.push(buttons, 0, 0, 0, 0)
pkt2, _ = cli.recvfrom(1024)
c2 = struct.unpack_from("<I", pkt2, 32)[0]
check("data: counter increments", c2 == c1 + 1, (c1, c2))

# ============ client expiry ============
print("== expiry ==")
with srv._lock:
    for a in srv._clients:
        srv._clients[a] -= (D.CLIENT_TIMEOUT + 1)
srv.push(buttons, 0, 0, 0, 0)
try:
    cli.settimeout(0.5)
    cli.recvfrom(1024)
    check("stale client pruned", False, "still receiving")
except socket.timeout:
    check("stale client pruned", True)
check("client_count zero", srv.client_count() == 0)

# ============ robustness ============
print("== robustness ==")
cli.sendto(b"garbage", addr)
cli.sendto(b"DSUC" + b"\x00" * 10, addr)          # too short
cli.sendto(client_packet(0xDEADBEEF), addr)        # unknown type
cli.sendto(client_packet(D.MSG_PORTS, struct.pack("<i", 99)), addr)  # bad count
time.sleep(0.2)
check("server survives garbage", srv.running)

# ============ bridge integration ============
print("== bridge integration ==")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import Switch2Bridge as S2B
S2B.keyboard.press = lambda k: None
S2B.keyboard.release = lambda k: None

mm = S2B.Mappings()
mm._apply(mm.DEFAULT)
check("mappings: dsu defaults", mm.dsu_enabled and mm.dsu_port == 26760)

br = S2B.ControllerBridge(mm, srv)
cli.settimeout(2.0)
cli.sendto(client_packet(D.MSG_DATA, bytes([0, 0]) + b"\x00" * 6), addr)
time.sleep(0.1)

def packet(b2=0, b3=0, b4=0, lx=2048, ly=2048, rx=2048, ry=2048):
    d = bytearray(11)
    d[2], d[3], d[4] = b2, b3, b4
    d[5] = lx & 0xFF
    d[6] = ((lx >> 8) & 0x0F) | ((ly & 0x0F) << 4)
    d[7] = (ly >> 4) & 0xFF
    d[8] = rx & 0xFF
    d[9] = ((rx >> 8) & 0x0F) | ((ry & 0x0F) << 4)
    d[10] = (ry >> 4) & 0xFF
    return bytes(d)

br._on_data(None, packet(b2=0x02, ly=4095))  # A + left stick full up
pkt, _ = cli.recvfrom(1024)
check("bridge->dsu: A->Circle", pkt[37] & 0x20)
check("bridge->dsu: analog ly high", pkt[41] >= 254, pkt[41])
check("bridge->dsu: rx centered", 127 <= pkt[42] <= 129, pkt[42])

# stop
srv.stop()
check("stop: not running", not srv.running)
srv.push(buttons, 0, 0, 0, 0)  # must not raise after stop
check("push after stop harmless", True)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    sys.exit(1)
print("ALL DSU TESTS PASSED")
