"""Headless tests for Switch2Bridge logic (no BLE, no real key presses)."""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import Switch2Bridge as S2B

# --- record key presses instead of sending them ---
events = []
S2B.keyboard.press = lambda k: events.append(("press", k))
S2B.keyboard.release = lambda k: events.append(("release", k))

FAILURES = []

def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)

# ============ Mappings._parse_key ============
print("== _parse_key ==")
M = S2B.Mappings
check("null unmapped", M._parse_key(None) is None)
check("<none> unmapped", M._parse_key("<none>") is None)
check("single char", M._parse_key("a") == "a")
check("uppercase normalized", M._parse_key("Z") == "z")
check("f-key", M._parse_key("<f12>") == S2B.Key.f12)
check("arrow", M._parse_key("<up>") == S2B.Key.up)
try:
    M._parse_key("ctrl")
    check("multi-char rejected", False)
except ValueError:
    check("multi-char rejected", True)
try:
    M._parse_key(3)
    check("int rejected", False)
except ValueError:
    check("int rejected", True)

# ============ Mappings._apply validation ============
print("== _apply ==")
m = M()
cfg = json.loads(json.dumps(M.DEFAULT))
cfg["buttons"]["TYPO_BTN"] = "y"
cfg["sticks"]["threshold"] = 5.0
cfg["sticks"]["left"]["upp"] = "t"
m._apply(cfg)
check("unknown button warned", "TYPO_BTN" in (m.last_warning or ""), m.last_warning)
check("threshold clamped", m.stick_threshold == 0.9, m.stick_threshold)
check("unknown stick dir warned", "upp" in (m.last_warning or ""))
check("typo button not applied", "TYPO_BTN" not in m.buttons)
check("C default unmapped", m.buttons.get("C") is None)

# load() with corrupt file
print("== load ==")
tmpdir = Path(tempfile.mkdtemp())
S2B.CONFIG_DIR = tmpdir
S2B.MAPPINGS_FILE = tmpdir / "mappings.json"
(tmpdir / "mappings.json").write_text("{not json")
m2 = M()
ok = m2.load()
check("corrupt file -> ok False", ok is False)
check("corrupt file -> error set", m2.last_error is not None)
check("corrupt file -> defaults applied", m2.buttons.get("A") == "z")

S2B.MAPPINGS_FILE.unlink()
m3 = M()
ok = m3.load()
check("first launch writes default + ok True", ok is True and S2B.MAPPINGS_FILE.exists())

# ============ key ref-counting ============
print("== _set_key ref counting ==")
mm = M()
mm._apply(json.loads(json.dumps(M.DEFAULT)))
br = S2B.ControllerBridge(mm)
events.clear()

# two sources sharing key "z"
br._set_key("A", "z", True)
br._set_key("B", "z", True)
br._set_key("A", "z", False)
check("shared key still held", ("release", "z") not in events, events)
br._set_key("B", "z", False)
check("shared key released once both up", events == [("press", "z"), ("release", "z")], events)

# mapping change while held
events.clear()
br._set_key("A", "z", True)
br._set_key("A", "q", True)  # remapped mid-hold
check("remap mid-hold swaps keys", events == [("press", "z"), ("release", "z"), ("press", "q")], events)
br._set_key("A", None, True)  # unmapped mid-hold
check("unmap mid-hold releases", events[-1] == ("release", "q"), events)

# release_all
events.clear()
br._set_key("A", "z", True)
br._set_key("ls_up", "w", True)
br.release_all_keys()
check("release_all releases everything",
      sorted(e for e in events if e[0] == "release") == [("release", "w"), ("release", "z")], events)
check("release_all clears state", not br._key_refs and not br._source_keys)

# ============ hysteresis ============
print("== stick hysteresis ==")
events.clear()
br._set_stick_key("ls_up", "w", 0.45)   # below 0.5 -> no press
check("below threshold no press", events == [])
br._set_stick_key("ls_up", "w", 0.55)   # above -> press
check("above threshold press", events == [("press", "w")])
br._set_stick_key("ls_up", "w", 0.45)   # above 0.4 (=0.8*0.5) -> stays held
check("hysteresis holds", events == [("press", "w")])
br._set_stick_key("ls_up", "w", 0.35)   # below 0.4 -> release
check("below release threshold releases", events[-1] == ("release", "w"))

# ============ _on_data packet parsing ============
print("== _on_data ==")
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

br2 = S2B.ControllerBridge(mm)
events.clear()
br2._on_data(None, packet(b2=0x02))          # A pressed
check("A press -> z", ("press", "z") in events, events)
br2._on_data(None, packet(b2=0x00))          # A released
check("A release -> z up", ("release", "z") in events, events)

events.clear()
br2._on_data(None, packet(b4=0x02))          # C pressed, unmapped by default
check("unmapped C -> nothing", events == [], events)

events.clear()
br2._on_data(None, packet(ly=4000))          # left stick up
check("stick up -> w", ("press", "w") in events, events)
br2._on_data(None, packet(ly=100))           # left stick down
check("stick flip: w released", ("release", "w") in events, events)
check("stick flip: s pressed", ("press", "s") in events, events)

events.clear()
br2._on_data(None, b"\x00\x01\x02")          # short packet
check("short packet ignored", events == [] and br2.packet_count == 5)

# dpad + stick sharing same key
mm.left_stick["up"] = S2B.Key.up  # same as DUP
events.clear()
br2._on_data(None, packet(b3=0x08, ly=4000))  # DUP + stick up together
br2._on_data(None, packet(b3=0x00, ly=4000))  # DUP released, stick still up
check("dpad/stick shared key not stolen", ("release", S2B.Key.up) not in events, events)
br2._on_data(None, packet())
check("shared key released at rest", ("release", S2B.Key.up) in events)

# ============ connect/cancel lifecycle (mock BLE) ============
print("== lifecycle ==")

class MockScanner:
    delay = 0.3
    result = {}
    queue = []  # optional per-round results, popped first
    @staticmethod
    async def discover(timeout=None, return_adv=False):
        await asyncio.sleep(MockScanner.delay)
        if MockScanner.queue:
            return MockScanner.queue.pop(0)
        return MockScanner.result

S2B.BleakScanner = MockScanner

# not found path (shrink the 30 s initial window for the test)
S2B.INITIAL_SCAN_WINDOW = 0.0
br3 = S2B.ControllerBridge(mm)
br3.connect()
br3._thread.join(3)
check("not found -> error", br3.last_error and "not found" in br3.last_error, br3.last_error)
check("not found -> thread exits", not br3._thread.is_alive())

# cancel during scan
MockScanner.delay = 5.0
br4 = S2B.ControllerBridge(mm)
br4.connect()
time.sleep(0.5)
t0 = time.time()
br4.disconnect(wait=True, timeout=4.0)
dt = time.time() - t0
check("cancel interrupts scan quickly", dt < 1.5 and not br4._thread.is_alive(), f"dt={dt:.2f}")
check("cancel -> no error surfaced", br4.last_error is None, br4.last_error)

# connect while thread alive is a no-op; reconnect after cancel works
MockScanner.delay = 0.1
br4.connect()
br4._thread.join(3)
check("reconnect after cancel runs", br4.last_error is not None)

# mock full session: device found, client streams then "drops"
class MockChar:
    def __init__(self, uuid, properties=("read", "notify")):
        self.uuid = uuid
        self.properties = list(properties)

class MockService:
    def __init__(self, uuid, characteristics):
        self.uuid = uuid
        self.characteristics = characteristics

def known_gatt():
    """GATT table of a controller exposing the documented input characteristic."""
    return [MockService("7492866c-ec3e-4619-8258-32755ffcc0f0", [
        MockChar(S2B.INPUT_CHAR_UUID),
        MockChar("7492866c-ec3e-4619-8258-32755ffcc0f8", ("write",)),
    ])]

class MockClient:
    instances = []
    services_template = None   # set per test; None -> known_gatt()
    streaming = {}             # uuid -> payload emitted while subscribed
    def __init__(self, address, timeout=None):
        self.address = address
        self._connected = False
        self.notify_cb = None
        self.subscribed = []
        self.services = (
            known_gatt() if MockClient.services_template is None
            else MockClient.services_template
        )
        self._feeds = {}
        MockClient.instances.append(self)
    @property
    def is_connected(self):
        return self._connected
    async def connect(self):
        self._connected = True
    async def disconnect(self):
        self._connected = False
    async def start_notify(self, uuid, cb):
        self.notify_cb = cb
        self.subscribed.append(uuid)
        payload = MockClient.streaming.get(uuid)
        if payload is not None:
            self._feeds[uuid] = asyncio.ensure_future(self._feed(uuid, cb, payload))
    async def stop_notify(self, uuid):
        feed = self._feeds.pop(uuid, None)
        if feed is not None:
            feed.cancel()
    @staticmethod
    async def _feed(uuid, cb, payload):
        while True:
            await asyncio.sleep(0.02)
            cb(uuid, payload)

S2B.BleakClient = MockClient

class Adv:
    manufacturer_data = {0x057E: b"\x01\x69\x20\xff"}
class Dev:
    name = "Pro Controller (S2)"

MockScanner.result = {"AA:BB": (Dev(), Adv())}
MockScanner.delay = 0.05

br5 = S2B.ControllerBridge(mm)
br5.connect()
time.sleep(0.6)
check("session connected", br5.is_connected and br5.controller_name == "Pro Controller (S2)")
client = MockClient.instances[-1]
client.notify_cb(None, packet(b2=0x02))
check("notify parsed while connected", br5.packet_count == 1)

# unexpected drop -> reconnect
client._connected = False
time.sleep(0.4)
check("drop -> reconnecting notice", br5.last_notice and "reconnect" in br5.last_notice.lower(), br5.last_notice)
time.sleep(0.6)
check("auto-reconnected", br5.is_connected)
check("reconnected notice", br5.last_notice and "Reconnected" in br5.last_notice, br5.last_notice)

# user disconnect -> clean stop, no reconnect
br5.disconnect(wait=True, timeout=3.0)
check("user disconnect stops thread", not br5._thread.is_alive())
check("no error after user disconnect", br5.last_error is None, br5.last_error)
check("client disconnected", not MockClient.instances[-1].is_connected)

# initial search keeps scanning: nothing on round 1, found on round 2
print("== initial scan retry ==")
S2B.INITIAL_SCAN_WINDOW = 10.0
MockScanner.queue = [{}]
br6 = S2B.ControllerBridge(mm)
br6.connect()
time.sleep(2.0)  # scan (0.05) + 1 s pause + scan + connect
check("found on second scan round", br6.is_connected)
check("no error during retry", br6.last_error is None, br6.last_error)
br6.disconnect(wait=True, timeout=3.0)

# scan error -> actionable messages
print("== scan error messages ==")
msg = S2B.ControllerBridge._scan_error_message(Exception("CBCentralManager is not authorized"))
check("unauthorized -> permission hint", "Privacy & Security" in msg and "Terminal" in msg, msg)
msg = S2B.ControllerBridge._scan_error_message(Exception("Bluetooth device is turned off"))
check("powered off -> enable hint", "turned off" in msg, msg)
msg = S2B.ControllerBridge._scan_error_message(Exception("boom"))
check("generic passthrough", "boom" in msg, msg)

# discovery filters: both Nintendo company IDs + name fallback
print("== discovery filters ==")
class AdvSIG:
    manufacturer_data = {0x0553: b"\x01\x69\x20\x00"}  # BT SIG assigned ID
class AdvVID:
    manufacturer_data = {0x057E: b"\x01\x69\x20\x00"}  # Nintendo USB VID
class AdvOther:
    manufacturer_data = {0x004C: b"\x10\x05"}          # random Apple device
class NoName:
    name = None
class Named:
    name = "Pro Controller (S2)"

MockScanner.queue = []
MockScanner.delay = 0.01
br7 = S2B.ControllerBridge(mm)

MockScanner.result = {"S1": (NoName(), AdvSIG())}
a, n = asyncio.run(br7._find_controller())
check("SIG id 0x0553 matched (no name)", a == "S1" and n == "Switch 2 Pro Controller", (a, n))

MockScanner.result = {"S2": (NoName(), AdvVID())}
a, n = asyncio.run(br7._find_controller())
check("USB VID 0x057e matched", a == "S2")

MockScanner.result = {"S3": (Named(), AdvOther())}
a, n = asyncio.run(br7._find_controller())
check("name fallback", a == "S3" and "Pro Controller" in n)

MockScanner.result = {"S4": (NoName(), AdvOther())}
a, n = asyncio.run(br7._find_controller())
check("unrelated device ignored", a is None)

# ============ input characteristic resolution (issue #15) ============
print("== input characteristic ==")
S2B.INPUT_PROBE_TIMEOUT = 0.3   # keep the probing tests fast

BATTERY_CHAR = "00002a19" + S2B.BLE_BASE_UUID_SUFFIX
ALT_INPUT_CHAR = "7492866c-ec3e-4619-8258-32755ffcc0fa"
ODD_VENDOR_CHAR = "ab0828b1-198e-4351-b779-901fa0e0371e"

def resolve(services, pinned=None, streaming=None):
    """Run _resolve_input_char against a fabricated GATT table."""
    MockClient.services_template = services
    MockClient.streaming = streaming or {}
    mres = M()
    mres._apply(json.loads(json.dumps(M.DEFAULT)))
    mres.input_char = pinned
    brr = S2B.ControllerBridge(mres)
    client = MockClient("XX")
    async def run():
        await client.connect()
        return await brr._resolve_input_char(client)
    try:
        return asyncio.run(run()), brr, client
    finally:
        MockClient.services_template = None
        MockClient.streaming = {}

report = packet(b2=0x02)

# known UUID present -> used directly, nothing probed
uuid, brr, client = resolve(known_gatt())
check("known char used", uuid == S2B.INPUT_CHAR_UUID, uuid)
check("known char not probed", client.subscribed == [], client.subscribed)

# pinned UUID wins when present, is ignored when absent
uuid, _, _ = resolve(
    [MockService("svc", [MockChar(S2B.INPUT_CHAR_UUID), MockChar(ALT_INPUT_CHAR)])],
    pinned=ALT_INPUT_CHAR,
)
check("pinned char used", uuid == ALT_INPUT_CHAR, uuid)
uuid, _, _ = resolve(known_gatt(), pinned=ALT_INPUT_CHAR)
check("stale pin falls back to known char", uuid == S2B.INPUT_CHAR_UUID, uuid)

# renumbered characteristic: probe adopts the one that streams real reports
uuid, brr, client = resolve(
    [MockService("svc", [MockChar(BATTERY_CHAR), MockChar(ALT_INPUT_CHAR)])],
    streaming={ALT_INPUT_CHAR: report, BATTERY_CHAR: b"\x63"},
)
check("renumbered char adopted", uuid == ALT_INPUT_CHAR, uuid)
check("vendor char probed before battery", client.subscribed[0] == ALT_INPUT_CHAR,
      client.subscribed)
check("adopted char persisted", brr.mappings.input_char == ALT_INPUT_CHAR)
check("adoption noticed in UI", brr.last_notice and "revision" in brr.last_notice,
      brr.last_notice)
check("no error on success", brr.last_error is None, brr.last_error)

# short reports must not be mistaken for input (battery streams 1 byte)
uuid, brr, _ = resolve(
    [MockService("svc", [MockChar(BATTERY_CHAR)])],
    streaming={BATTERY_CHAR: b"\x63"},
)
check("short reports rejected", uuid is None, uuid)
check("rejection explains next step",
      brr.last_error and "bridge.log" in brr.last_error and "issues" in brr.last_error,
      brr.last_error)

# probe order: same vendor block, then other vendor UUIDs, then SIG-assigned
MockClient.services_template = [MockService("svc", [
    MockChar(BATTERY_CHAR), MockChar(ODD_VENDOR_CHAR), MockChar(ALT_INPUT_CHAR),
    MockChar("write-only-char", ("write",)),
])]
order = S2B.ControllerBridge._probe_candidates(MockClient("XX"))
check("probe order", order == [ALT_INPUT_CHAR, ODD_VENDOR_CHAR, BATTERY_CHAR], order)
check("non-notifiable skipped", "write-only-char" not in order, order)
MockClient.services_template = None

# a client whose services never got discovered must not crash the resolver
uuid, brr, _ = resolve([])
check("empty GATT -> actionable error", uuid is None and brr.last_error, brr.last_error)

# a failed resolution must not spam stop_notify for a subscription we never made
MockClient.services_template = []
br_nochar = S2B.ControllerBridge(mm)
streamed = asyncio.run(br_nochar._session("XX", "Pro Controller"))
check("session fails cleanly without input char", streamed is False)
check("no stop_notify attempted", MockClient.instances[-1].subscribed == [],
      MockClient.instances[-1].subscribed)
MockClient.services_template = None

# ============ ble.input_char config ============
print("== ble.input_char config ==")
warns = []
check("null -> auto", M._parse_uuid(None, warns) is None and not warns)
check("uuid normalized",
      M._parse_uuid("7492866C-EC3E-4619-8258-32755FFCC0FA", warns) == ALT_INPUT_CHAR)
check("garbage rejected with warning",
      M._parse_uuid("nope", warns) is None and any("input_char" in w for w in warns),
      warns)

mcfg = M()
cfg2 = json.loads(json.dumps(M.DEFAULT))
cfg2["ble"]["input_char"] = ALT_INPUT_CHAR
mcfg._apply(cfg2)
check("config pin applied", mcfg.input_char == ALT_INPUT_CHAR)

S2B.MAPPINGS_FILE.write_text(json.dumps(M.DEFAULT))
mcfg.set_input_char(ALT_INPUT_CHAR)
saved = json.loads(S2B.MAPPINGS_FILE.read_text())
check("input_char persisted", saved["ble"]["input_char"] == ALT_INPUT_CHAR, saved.get("ble"))
check("persist keeps other sections", saved["buttons"]["A"] == "z" and "dsu" in saved)

# failing reconnect attempts must not spam last_error; only the final
# give-up message surfaces
print("== reconnect error spam ==")
S2B.INITIAL_SCAN_WINDOW = 10.0
S2B.RECONNECT_WINDOW = 2.0
MockScanner.delay = 0.05
MockScanner.queue = []
MockScanner.result = {"AA:BB": (Dev(), Adv())}
br8 = S2B.ControllerBridge(mm)
br8.connect()
time.sleep(0.6)
check("spam test: connected", br8.is_connected)

orig_connect = MockClient.connect
async def _failing_connect(self):
    raise RuntimeError("nope")
MockClient.connect = _failing_connect
MockClient.instances[-1]._connected = False  # unexpected drop
time.sleep(1.2)  # several failing reconnect attempts happen here
check("no error surfaced while retrying", br8.last_error is None, br8.last_error)
time.sleep(2.5)  # past the (shrunk) reconnect window
check("final give-up error surfaced",
      br8.last_error and "reconnect" in br8.last_error.lower(), br8.last_error)
check("spam test: thread ended", not br8._thread.is_alive())
MockClient.connect = orig_connect

# a corrupt mappings.json must never be clobbered by the DSU toggle
print("== dsu toggle vs corrupt file ==")
S2B.MAPPINGS_FILE.write_text("{broken json")
m4 = S2B.Mappings()
m4.set_dsu_enabled(False)
check("corrupt file untouched", S2B.MAPPINGS_FILE.read_text() == "{broken json")
check("in-memory toggle still applied", m4.dsu_enabled is False)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
    sys.exit(1)
print("ALL TESTS PASSED")
