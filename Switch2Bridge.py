#!/usr/bin/env python3
"""
Switch2 Bridge - macOS Menubar App
==================================

A clean menubar app to connect your Switch 2 Pro Controller
and use it with Ryujinx (or any other emulator that reads the keyboard).

Author: Aurélien Desert
License: MIT
"""

import asyncio
import json
import logging
import re
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ============================================================
# DEPENDENCY CHECK
# ============================================================

try:
    import rumps
except ImportError:
    print("❌ rumps not installed — run: pip install rumps")
    sys.exit(1)

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("❌ bleak not installed — run: pip install bleak")
    sys.exit(1)

try:
    from pynput.keyboard import Controller, Key
    keyboard = Controller()
except ImportError:
    print("❌ pynput not installed — run: pip install pynput")
    sys.exit(1)

AXIsProcessTrusted = None
try:
    from ApplicationServices import AXIsProcessTrusted
except ImportError:
    try:
        from HIServices import AXIsProcessTrusted  # older pyobjc layout
    except ImportError:
        pass

from dsu_server import DSUServer

SMAppService = None
try:
    from ServiceManagement import SMAppService
except ImportError:
    pass  # "Start at Login" simply won't be offered


# ============================================================
# CONSTANTS
# ============================================================

APP_NAME = "Switch2 Bridge"
APP_VERSION = "1.2.4"  # single source of truth — read by setup_app.py & build_dmg.sh
INPUT_CHAR_UUID = "7492866c-ec3e-4619-8258-32755ffcc0f9"
# …f8 is the *output* (LED/rumble) characteristic on the unit this was
# reverse-engineered on, but an AU-market controller streams its input reports
# from it and has no …f9 at all (issue #15) — the two roles are swapped there.
# Trusted first, probe-validated after, since we know less about its role.
KNOWN_INPUT_CHAR_UUIDS = (
    INPUT_CHAR_UUID,
    "7492866c-ec3e-4619-8258-32755ffcc0f8",
)

# Not every controller revision exposes that UUID (issue #15), so it is only the
# first candidate: when it is absent we probe the other notifiable
# characteristics and keep the one that actually streams input reports.
# Characteristics ending with the Bluetooth base UUID are SIG-assigned
# (battery level, device info…) and are probed last.
BLE_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"
# Vendor UUIDs sharing this prefix belong to the same Nintendo block as
# INPUT_CHAR_UUID — likeliest place for a renumbered input characteristic.
VENDOR_UUID_PREFIX = "7492866c"
INPUT_PROBE_TIMEOUT = 3.0   # per candidate: how long to wait for a report
MAX_INPUT_PROBES = 8        # cap the worst-case identification time
MIN_REPORT_LEN = 11         # bytes needed to decode buttons + both sticks

ISSUES_URL = "https://github.com/mlstr0m/switch2bridge-macos/issues"

# Nintendo company identifiers seen in BLE advertisements:
# 0x0553 is the Bluetooth SIG assigned ID, 0x057E is Nintendo's USB VID
# (both observed in the wild depending on firmware)
NINTENDO_COMPANY_IDS = (0x0553, 0x057e)
# Switch 2 Pro Controller product ID 0x2069, little-endian as it appears on the wire
SWITCH2_PRO_PID_LE = b'\x69\x20'

SCAN_TIMEOUT = 5.0
CONNECT_TIMEOUT = 15.0
# Keep scanning this long on the first search — pairing-mode advertising is
# easy to miss with a single short window
INITIAL_SCAN_WINDOW = 30.0
# After an unexpected drop, keep trying to reconnect for this long
RECONNECT_WINDOW = 60.0

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Switch2Bridge"
MAPPINGS_FILE = CONFIG_DIR / "mappings.json"

LOG_DIR = Path.home() / "Library" / "Logs" / "Switch2Bridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_handler = RotatingFileHandler(
    LOG_DIR / "bridge.log", maxBytes=1_000_000, backupCount=2
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler])
log = logging.getLogger(__name__)


# ============================================================
# MAPPINGS — load/save user-editable JSON
# ============================================================

class Mappings:
    """User-editable button + stick mappings.

    Loaded from ~/Library/Application Support/Switch2Bridge/mappings.json.
    On first launch, the default mapping is written there so users can edit it.
    A value of null (or "<none>") leaves that button unmapped.
    """

    DEFAULT = {
        "version": 1,
        "buttons": {
            "A": "z", "B": "x", "X": "c", "Y": "v",
            "L": "q", "R": "e", "ZL": "1", "ZR": "3",
            "+": "p", "-": "m", "HOME": "h", "CAPT": "o",
            "C": None,
            "LS": "f", "RS": "g", "GL": "9", "GR": "0",
            "DUP": "<up>", "DDOWN": "<down>",
            "DLEFT": "<left>", "DRIGHT": "<right>",
        },
        "sticks": {
            "threshold": 0.5,
            "left":  {"up": "w", "down": "s", "left": "a", "right": "d"},
            "right": {"up": "i", "down": "k", "left": "j", "right": "l"},
        },
        "dsu": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 26760,
        },
        # input_char: null = auto-detect. Set a 128-bit UUID to force the
        # input-report characteristic of an unusual controller revision.
        "ble": {
            "input_char": None,
        },
    }

    BUTTON_NAMES = frozenset(DEFAULT["buttons"])
    STICK_DIRECTIONS = frozenset(("up", "down", "left", "right"))
    UUID_RE = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    SPECIAL_KEYS = {
        "<up>": Key.up, "<down>": Key.down,
        "<left>": Key.left, "<right>": Key.right,
        "<space>": Key.space, "<enter>": Key.enter,
        "<esc>": Key.esc, "<tab>": Key.tab,
        "<backspace>": Key.backspace, "<delete>": Key.delete,
        "<home>": Key.home, "<end>": Key.end,
        "<pageup>": Key.page_up, "<pagedown>": Key.page_down,
        "<shift>": Key.shift, "<ctrl>": Key.ctrl,
        "<alt>": Key.alt, "<cmd>": Key.cmd,
    }
    SPECIAL_KEYS.update({f"<f{i}>": getattr(Key, f"f{i}") for i in range(1, 21)})

    THRESHOLD_MIN, THRESHOLD_MAX = 0.1, 0.9

    def __init__(self):
        self.buttons = {}
        self.stick_threshold = 0.5
        self.left_stick = {}
        self.right_stick = {}
        self.dsu_enabled = True
        self.dsu_host = "127.0.0.1"
        self.dsu_port = 26760
        self.input_char = None  # None = auto-detect the input characteristic
        # Consumed by the UI tick: error → alert, warning → notification
        self.last_error = None
        self.last_warning = None

    # --- IO ---

    @classmethod
    def ensure_default_file(cls):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not MAPPINGS_FILE.exists():
            with open(MAPPINGS_FILE, "w") as f:
                json.dump(cls.DEFAULT, f, indent=2)
            log.info("wrote default mappings to %s", MAPPINGS_FILE)

    def load(self):
        """Load mappings from disk, falling back to defaults on error.

        Returns True only when the file was read and applied cleanly.
        """
        self.last_error = None
        self.last_warning = None
        cfg = self.DEFAULT
        ok = True
        try:
            self.ensure_default_file()
            with open(MAPPINGS_FILE) as f:
                cfg = json.load(f)
        except Exception as e:
            log.exception("failed to read mappings.json")
            self.last_error = f"Could not read mappings.json: {e}\nUsing defaults."
            cfg = self.DEFAULT
            ok = False

        try:
            self._apply(cfg)
            log.info("mappings loaded from %s", MAPPINGS_FILE)
        except Exception as e:
            log.exception("invalid mappings.json")
            self.last_error = f"Invalid mappings.json: {e}\nUsing defaults."
            self._apply(self.DEFAULT)
            ok = False
        return ok

    # --- internals ---

    def _apply(self, cfg):
        buttons = cfg.get("buttons", {})
        sticks = cfg.get("sticks", {})
        if not isinstance(buttons, dict):
            raise ValueError('"buttons" must be an object')
        if not isinstance(sticks, dict):
            raise ValueError('"sticks" must be an object')

        warnings = []

        unknown = sorted(set(buttons) - self.BUTTON_NAMES)
        if unknown:
            warnings.append(f"Unknown button name(s) ignored: {', '.join(unknown)}")

        self.buttons = {
            name: self._parse_key(v)
            for name, v in buttons.items() if name in self.BUTTON_NAMES
        }

        try:
            threshold = float(sticks.get("threshold", 0.5))
        except (TypeError, ValueError):
            raise ValueError('"sticks.threshold" must be a number')
        clamped = min(max(threshold, self.THRESHOLD_MIN), self.THRESHOLD_MAX)
        if clamped != threshold:
            warnings.append(f"Stick threshold {threshold} out of range, using {clamped}")
        self.stick_threshold = clamped

        parsed_sticks = {}
        for side in ("left", "right"):
            side_cfg = sticks.get(side, {})
            if not isinstance(side_cfg, dict):
                raise ValueError(f'"sticks.{side}" must be an object')
            unknown = sorted(set(side_cfg) - self.STICK_DIRECTIONS)
            if unknown:
                warnings.append(
                    f"Unknown {side} stick direction(s) ignored: {', '.join(unknown)}"
                )
            parsed_sticks[side] = {
                d: self._parse_key(k)
                for d, k in side_cfg.items() if d in self.STICK_DIRECTIONS
            }
        self.left_stick = parsed_sticks["left"]
        self.right_stick = parsed_sticks["right"]

        dsu = cfg.get("dsu", {})
        if not isinstance(dsu, dict):
            raise ValueError('"dsu" must be an object')
        self.dsu_enabled = bool(dsu.get("enabled", True))
        self.dsu_host = str(dsu.get("host", "127.0.0.1"))
        try:
            port = int(dsu.get("port", 26760))
        except (TypeError, ValueError):
            raise ValueError('"dsu.port" must be an integer')
        if not (1024 <= port <= 65535):
            warnings.append(f"DSU port {port} out of range, using 26760")
            port = 26760
        self.dsu_port = port

        ble = cfg.get("ble", {})
        if not isinstance(ble, dict):
            raise ValueError('"ble" must be an object')
        self.input_char = self._parse_uuid(ble.get("input_char"), warnings)

        if warnings:
            self.last_warning = "\n".join(warnings)

    def set_dsu_enabled(self, enabled):
        """Persist the DSU toggle back into mappings.json (best effort)."""
        self.dsu_enabled = bool(enabled)
        self._persist("dsu", "enabled", self.dsu_enabled, "DSU toggle")

    def set_input_char(self, uuid):
        """Remember an auto-detected input characteristic (best effort)."""
        self.input_char = uuid
        self._persist("ble", "input_char", uuid, "input characteristic")

    def _persist(self, section, key, value, what):
        """Write one setting back into mappings.json without touching the rest."""
        try:
            self.ensure_default_file()
            with open(MAPPINGS_FILE) as f:
                cfg = json.load(f)
        except Exception:
            # Never clobber a corrupt (but user-authored) file with defaults —
            # the setting just won't persist until the file is fixed.
            log.exception("mappings.json unreadable, %s not persisted", what)
            return
        target = cfg.get(section)
        if not isinstance(target, dict):
            target = cfg[section] = {}
        target[key] = value
        try:
            with open(MAPPINGS_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            log.exception("could not write mappings.json to persist %s", what)

    @classmethod
    def _parse_uuid(cls, value, warnings):
        """None/empty means auto-detect; anything not a UUID is warned about."""
        if value is None or value == "":
            return None
        text = str(value).strip().lower()
        if cls.UUID_RE.match(text):
            return text
        warnings.append(
            f'"ble.input_char" is not a 128-bit UUID, ignoring: {value!r}'
        )
        return None

    @classmethod
    def _parse_key(cls, value):
        if value is None or value == "<none>":
            return None
        if not isinstance(value, str):
            raise ValueError(f"key must be a string or null, got {type(value).__name__}")
        if value in cls.SPECIAL_KEYS:
            return cls.SPECIAL_KEYS[value]
        if len(value) == 1:
            # "Z" would be typed as shift+z by pynput; games expect the bare keycode
            if value != value.lower():
                log.info("normalizing key %r to %r", value, value.lower())
            return value.lower()
        raise ValueError(
            f"unknown key {value!r} (use a single character, null, or one of {sorted(cls.SPECIAL_KEYS)})"
        )


# ============================================================
# CONTROLLER BRIDGE (BLE + keyboard, runs in worker thread)
# ============================================================

class ControllerBridge:
    """BLE connection + keyboard input simulation."""

    def __init__(self, mappings: Mappings, dsu: DSUServer = None):
        self.mappings = mappings
        self.dsu = dsu
        self.is_connected = False
        self.is_searching = False
        self.is_connecting = False
        self.is_reconnecting = False
        self.controller_name = None
        self.packet_count = 0
        # Set by worker, read & cleared by the main-thread UI tick
        self.last_error = None
        self.last_notice = None
        # Key state: which key each input source holds, and how many sources
        # hold each key (two buttons mapped to the same key must not release
        # it while one of them is still down).
        self._key_lock = threading.Lock()
        self._source_keys = {}   # source name -> key currently held
        self._key_refs = {}      # key -> number of sources holding it
        self._client = None
        self._stop_event = threading.Event()
        self._thread = None
        self._loop = None
        self._task = None

    # --- key dispatch ---

    def _press_ref(self, key):
        n = self._key_refs.get(key, 0)
        self._key_refs[key] = n + 1
        if n == 0:
            try:
                keyboard.press(key)
            except Exception as e:
                log.warning("keyboard.press failed: %s", e)

    def _release_ref(self, key):
        n = self._key_refs.get(key, 0)
        if n <= 1:
            self._key_refs.pop(key, None)
            try:
                keyboard.release(key)
            except Exception as e:
                log.warning("keyboard.release failed: %s", e)
        else:
            self._key_refs[key] = n - 1

    def _set_key(self, source, key, active):
        """Press/release `key` on behalf of `source` (a button or stick direction)."""
        with self._key_lock:
            prev = self._source_keys.get(source)
            if active and key is not None:
                if prev == key:
                    return
                if prev is not None:
                    self._release_ref(prev)
                self._source_keys[source] = key
                self._press_ref(key)
            else:
                if prev is None:
                    return
                del self._source_keys[source]
                self._release_ref(prev)

    def release_all_keys(self):
        with self._key_lock:
            for key in list(self._key_refs):
                try:
                    keyboard.release(key)
                except Exception as e:
                    log.warning("keyboard.release on cleanup failed: %s", e)
            self._key_refs.clear()
            self._source_keys.clear()

    def _set_stick_key(self, source, key, value):
        """Threshold with hysteresis: press above t, release below 0.8*t.

        Avoids key chatter when the stick hovers right at the threshold.
        """
        t = self.mappings.stick_threshold
        held = source in self._source_keys
        self._set_key(source, key, value > (t * 0.8 if held else t))

    # --- BLE input parser ---

    def _on_data(self, sender, data: bytes):
        if len(data) < MIN_REPORT_LEN:
            return

        self.packet_count += 1

        b = self.mappings.buttons
        b2, b3, b4 = data[2], data[3], data[4]

        # face buttons (byte 2)
        self._set_key('B', b.get('B'), b2 & 0x01)
        self._set_key('A', b.get('A'), b2 & 0x02)
        self._set_key('Y', b.get('Y'), b2 & 0x04)
        self._set_key('X', b.get('X'), b2 & 0x08)
        self._set_key('R', b.get('R'), b2 & 0x10)
        self._set_key('ZR', b.get('ZR'), b2 & 0x20)
        self._set_key('+', b.get('+'), b2 & 0x40)
        self._set_key('RS', b.get('RS'), b2 & 0x80)

        # d-pad + left shoulder/trigger (byte 3)
        self._set_key('DDOWN', b.get('DDOWN'), b3 & 0x01)
        self._set_key('DRIGHT', b.get('DRIGHT'), b3 & 0x02)
        self._set_key('DLEFT', b.get('DLEFT'), b3 & 0x04)
        self._set_key('DUP', b.get('DUP'), b3 & 0x08)
        self._set_key('L', b.get('L'), b3 & 0x10)
        self._set_key('ZL', b.get('ZL'), b3 & 0x20)
        self._set_key('-', b.get('-'), b3 & 0x40)
        self._set_key('LS', b.get('LS'), b3 & 0x80)

        # special (byte 4) — 0x02 is believed to be the new C button
        self._set_key('HOME', b.get('HOME'), b4 & 0x01)
        self._set_key('C', b.get('C'), b4 & 0x02)
        self._set_key('GR', b.get('GR'), b4 & 0x04)
        self._set_key('GL', b.get('GL'), b4 & 0x08)
        self._set_key('CAPT', b.get('CAPT'), b4 & 0x10)

        # sticks: 12-bit packed across bytes 5-10
        lx_raw = data[5] | ((data[6] & 0x0F) << 8)
        ly_raw = ((data[6] & 0xF0) >> 4) | (data[7] << 4)
        rx_raw = data[8] | ((data[9] & 0x0F) << 8)
        ry_raw = ((data[9] & 0xF0) >> 4) | (data[10] << 4)

        lx = (lx_raw - 2048) / 2048.0
        ly = (ly_raw - 2048) / 2048.0
        rx = (rx_raw - 2048) / 2048.0
        ry = (ry_raw - 2048) / 2048.0

        ls = self.mappings.left_stick
        rs = self.mappings.right_stick

        self._set_stick_key('ls_up',    ls.get('up'),    ly)
        self._set_stick_key('ls_down',  ls.get('down'),  -ly)
        self._set_stick_key('ls_left',  ls.get('left'),  -lx)
        self._set_stick_key('ls_right', ls.get('right'), lx)

        self._set_stick_key('rs_up',    rs.get('up'),    ry)
        self._set_stick_key('rs_down',  rs.get('down'),  -ry)
        self._set_stick_key('rs_left',  rs.get('left'),  -rx)
        self._set_stick_key('rs_right', rs.get('right'), rx)

        # forward everything to the DSU server (true analog for emulators)
        if self.dsu is not None and self.dsu.running:
            self.dsu.push(
                {
                    'B': b2 & 0x01, 'A': b2 & 0x02, 'Y': b2 & 0x04, 'X': b2 & 0x08,
                    'R': b2 & 0x10, 'ZR': b2 & 0x20, '+': b2 & 0x40, 'RS': b2 & 0x80,
                    'DDOWN': b3 & 0x01, 'DRIGHT': b3 & 0x02, 'DLEFT': b3 & 0x04,
                    'DUP': b3 & 0x08, 'L': b3 & 0x10, 'ZL': b3 & 0x20,
                    '-': b3 & 0x40, 'LS': b3 & 0x80,
                    'HOME': b4 & 0x01, 'CAPT': b4 & 0x10,
                },
                lx, ly, rx, ry,
            )

    # --- discovery ---

    async def _find_controller(self, timeout=SCAN_TIMEOUT):
        """Returns (address, name) or (None, None)."""
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        seen = []
        for address, (device, adv) in devices.items():
            mfr = ", ".join(
                f"{cid:#06x}:{bytes(p).hex()}"
                for cid, p in adv.manufacturer_data.items()
            )
            seen.append(
                f"{device.name or '?'} rssi={getattr(adv, 'rssi', '?')} [{mfr}]"
            )
            # Primary: Nintendo company ID is the dict key in bleak's manufacturer_data
            for cid in NINTENDO_COMPANY_IDS:
                payload = adv.manufacturer_data.get(cid)
                if payload and SWITCH2_PRO_PID_LE in payload:
                    return address, device.name or "Switch 2 Pro Controller"
            # Fallback: some macOS BLE stacks expose name without manufacturer data
            if device.name and "Pro Controller" in device.name:
                return address, device.name
        # Diagnostic dump: this is what tells us why a controller wasn't matched
        log.info(
            "scan: no controller among %d device(s): %s",
            len(seen), "; ".join(seen[:20]) or "none",
        )
        return None, None

    # --- input characteristic resolution ---

    @staticmethod
    def _gatt_chars(client):
        """[(service_uuid, characteristic)] for every discovered characteristic."""
        services = getattr(client, "services", None)
        if services is None:
            return []
        raw = getattr(services, "services", None)
        iterable = raw.values() if isinstance(raw, dict) else services
        out = []
        for svc in iterable:
            svc_uuid = str(getattr(svc, "uuid", "?")).lower()
            for char in getattr(svc, "characteristics", ()) or ():
                out.append((svc_uuid, char))
        return out

    def _log_gatt(self, client):
        """Dump the GATT table — this is what identifies an unknown revision."""
        try:
            entries = [
                f"{svc}/{str(char.uuid).lower()}"
                f"[{','.join(getattr(char, 'properties', ()) or ())}]"
                for svc, char in self._gatt_chars(client)
            ]
            log.info(
                "GATT: %d characteristic(s): %s",
                len(entries), " ".join(entries) or "none",
            )
        except Exception:
            log.exception("could not dump the GATT table")

    @staticmethod
    def _can_notify(char):
        props = {str(p).lower() for p in (getattr(char, "properties", ()) or ())}
        return bool(props & {"notify", "indicate"})

    @classmethod
    def _notifiable_uuids(cls, client):
        return {
            str(char.uuid).lower()
            for _svc, char in cls._gatt_chars(client) if cls._can_notify(char)
        }

    @classmethod
    def _probe_candidates(cls, client):
        """Notifiable characteristics, likeliest input reporter first."""
        known, vendor_block, vendor, standard = [], [], [], []
        for _svc, char in cls._gatt_chars(client):
            if not cls._can_notify(char):
                continue
            uuid = str(char.uuid).lower()
            if uuid == INPUT_CHAR_UUID:
                continue  # trusted without probing, before we get here
            if uuid in KNOWN_INPUT_CHAR_UUIDS:
                known.append(uuid)
            elif uuid.endswith(BLE_BASE_UUID_SUFFIX):
                standard.append(uuid)
            elif uuid.startswith(VENDOR_UUID_PREFIX):
                vendor_block.append(uuid)
            else:
                vendor.append(uuid)
        return (known + vendor_block + vendor + standard)[:MAX_INPUT_PROBES]

    async def _probe_input_char(self, client, uuid):
        """Subscribe briefly: True if it streams reports we can decode."""
        loop = asyncio.get_event_loop()
        got_report = asyncio.Event()

        def _sniff(_sender, data):
            if len(data) >= MIN_REPORT_LEN:
                loop.call_soon_threadsafe(got_report.set)

        try:
            await client.start_notify(uuid, _sniff)
        except Exception as e:
            log.info("probe %s: start_notify refused (%s)", uuid, e)
            return False
        try:
            await asyncio.wait_for(got_report.wait(), INPUT_PROBE_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            log.info(
                "probe %s: no %d+ byte report within %.0f s",
                uuid, MIN_REPORT_LEN, INPUT_PROBE_TIMEOUT,
            )
            return False
        finally:
            try:
                await client.stop_notify(uuid)
            except Exception as e:
                log.info("probe %s: stop_notify failed (%s)", uuid, e)

    async def _resolve_input_char(self, client):
        """UUID of the characteristic streaming input reports, or None.

        Order: the UUID pinned in mappings.json, then the documented one, then
        a probe of every other notifiable characteristic (other known UUIDs
        first) — controller revisions renumber it (issue #15). Sets last_error
        when nothing works.
        """
        notifiable = self._notifiable_uuids(client)

        pinned = self.mappings.input_char
        if pinned:
            if pinned in notifiable:
                log.info("using pinned input characteristic %s", pinned)
                return pinned
            log.warning(
                "pinned input characteristic %s absent or not notifiable, "
                "re-detecting", pinned,
            )

        if INPUT_CHAR_UUID in notifiable:
            return INPUT_CHAR_UUID

        candidates = self._probe_candidates(client)
        log.info(
            "%s absent — probing %d candidate(s): %s",
            INPUT_CHAR_UUID, len(candidates), ", ".join(candidates) or "none",
        )
        if candidates:
            # Some firmwares only report on change, so ask for input while
            # we listen — an idle controller looks like a silent characteristic.
            self.last_notice = (
                "Unknown controller revision — identifying it, move the sticks…"
            )
        for uuid in candidates:
            if self._stop_event.is_set():
                return None
            if await self._probe_input_char(client, uuid):
                log.info("adopted input characteristic %s", uuid)
                # Remember it so the next connect skips the probing entirely.
                self.mappings.set_input_char(uuid)
                self.last_notice = f"New controller revision — using {uuid[:8]}…"
                return uuid

        self.last_error = (
            "Connected, but no characteristic streamed readable input.\n"
            "Retry once while moving the sticks — some revisions only report "
            "on change.\nIf it keeps failing, the controller's BLE services "
            "were listed in ~/Library/Logs/Switch2Bridge/bridge.log: please "
            f"attach that log to a report at {ISSUES_URL}."
        )
        return None

    # --- main async routine ---

    async def _session(self, address, name, reconnected=False):
        """Connect and stream input until disconnect/stop.

        Returns True once input streaming was reached (used to decide whether
        an auto-reconnect is worth attempting). On failure, last_error is set.
        """
        client = BleakClient(address, timeout=CONNECT_TIMEOUT)
        self._client = client
        self.is_connecting = True
        notifying = None  # UUID we subscribed to, for the teardown below
        try:
            try:
                await client.connect()
            except Exception as e:
                log.exception("connect failed")
                self.last_error = f"Connection error: {e}"
                return False
            if not client.is_connected:
                self.last_error = "Failed to connect to controller."
                return False

            self._log_gatt(client)
            input_char = await self._resolve_input_char(client)
            if input_char is None:
                return False  # last_error already set

            try:
                await client.start_notify(input_char, self._on_data)
                notifying = input_char
            except Exception as e:
                log.exception("start_notify failed")
                self.last_error = f"Connection error: {e}"
                return False

            self.controller_name = name
            self.is_connecting = False
            self.is_connected = True
            self.is_reconnecting = False
            if self.dsu is not None:
                self.dsu.set_connected(True)
            log.info("connected to %s @ %s", name, address)
            if reconnected:
                self.last_notice = f"Reconnected to {name}"

            while not self._stop_event.is_set() and client.is_connected:
                await asyncio.sleep(0.1)
            return True
        finally:
            self.is_connecting = False
            self.is_connected = False
            self.controller_name = None
            self._client = None
            if self.dsu is not None:
                self.dsu.set_connected(False)
            self.release_all_keys()
            try:
                if notifying and client.is_connected:
                    await client.stop_notify(notifying)
            except Exception as e:
                log.warning("BLE stop_notify error: %s", e)
            try:
                # Always disconnect: also cancels a pending CoreBluetooth
                # connection attempt if we were cancelled mid-connect.
                await client.disconnect()
            except Exception as e:
                log.warning("BLE disconnect error: %s", e)

    @staticmethod
    def _scan_error_message(exc):
        """Turn a bleak scan failure into an actionable message."""
        text = str(exc).lower()
        if "unauthorized" in text or "not authorized" in text or "denied" in text:
            return (
                "macOS refused Bluetooth access. Grant it in System Settings → "
                "Privacy & Security → Bluetooth.\nWhen running from source, the "
                "permission belongs to Terminal (or your Python), not the app."
            )
        if "turned off" in text or "powered off" in text:
            return "Bluetooth is turned off. Enable it in Control Center and retry."
        return f"Bluetooth scan failed: {exc}"

    async def _connect_async(self):
        was_connected = False
        deadline = time.monotonic() + INITIAL_SCAN_WINDOW
        try:
            while not self._stop_event.is_set():
                self.is_searching = True
                self.packet_count = 0
                try:
                    address, name = await self._find_controller()
                except Exception as e:
                    log.exception("BLE scan failed")
                    self.last_error = self._scan_error_message(e)
                    return

                if self._stop_event.is_set():
                    return

                streamed = False
                if address:
                    self.is_searching = False
                    streamed = await self._session(
                        address, name, reconnected=was_connected
                    )
                    if not streamed and was_connected:
                        # Still auto-retrying — keep the failure in the logs
                        # only; a notification per attempt would spam the user.
                        self.last_error = None

                if self._stop_event.is_set():
                    return

                if streamed:
                    # Unexpected drop (controller slept, went out of range…):
                    # retry for RECONNECT_WINDOW before giving up.
                    was_connected = True
                    deadline = time.monotonic() + RECONNECT_WINDOW
                    self.is_reconnecting = True
                    self.last_notice = "Controller disconnected — reconnecting…"
                    log.info("connection dropped, entering reconnect loop")
                    continue

                if address and not was_connected:
                    # Found it but couldn't connect — surface and stop.
                    return

                if time.monotonic() > deadline:
                    self.last_error = (
                        "Could not reconnect to the controller."
                        if was_connected else
                        "Controller not found after 30 s. Hold the small pair "
                        "button on the back until the LEDs sweep, while the "
                        "search is running.\nNote: the controller will never "
                        "appear in System Settings → Bluetooth — watch the "
                        "menubar instead."
                    )
                    return

                # keep is_searching set during the pause so the UI doesn't flicker
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            log.info("bridge task cancelled")
        finally:
            self.is_searching = False
            self.is_reconnecting = False

    # --- public API ---

    @property
    def is_stopping(self):
        return (
            self._stop_event.is_set()
            and self._thread is not None
            and self._thread.is_alive()
        )

    def connect(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.last_error = None
        self.last_notice = None

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                self._task = loop.create_task(self._connect_async())
                try:
                    loop.run_until_complete(self._task)
                except asyncio.CancelledError:
                    pass
            finally:
                self._task = None
                self._loop = None
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def disconnect(self, wait=False, timeout=2.0):
        if not self._stop_event.is_set():
            self._stop_event.set()
            # Interrupt whatever the worker is awaiting (scan, connect, stream)
            loop, task = self._loop, self._task
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # loop already closed
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout)


# ============================================================
# MENUBAR APP
# ============================================================

class Switch2BridgeApp(rumps.App):
    """
    Menu items are constructed once, then mutated in place via .title.
    We never call self.menu.clear() or reassign self.menu after init — doing
    so dismisses the open dropdown on every Timer tick.
    """

    REFRESH_INTERVAL = 1.0

    def __init__(self):
        super().__init__(APP_NAME, title="🎮", quit_button=None)

        self.mappings = Mappings()
        self.mappings.load()
        self.dsu = DSUServer(self.mappings.dsu_host, self.mappings.dsu_port)
        self.bridge = ControllerBridge(self.mappings, self.dsu)

        # Long-lived menu items
        self._status_item = rumps.MenuItem("○ Not connected")
        self._detail_item = rumps.MenuItem(" ")
        self._action_item = rumps.MenuItem("Connect Controller", callback=self._on_action)
        self._mapping_item = rumps.MenuItem("Button Mapping…", callback=self._show_mapping)
        self._reveal_item = rumps.MenuItem("Edit mappings file…", callback=self._reveal_mappings)
        self._reload_item = rumps.MenuItem("Reload mappings", callback=self._reload_mappings)
        self._dsu_item = rumps.MenuItem("DSU server", callback=self._toggle_dsu)
        self._login_item = rumps.MenuItem("Start at Login", callback=self._toggle_login)
        self._logs_item = rumps.MenuItem("Open logs…", callback=self._open_logs)
        self._version_item = rumps.MenuItem(f"{APP_NAME} v{APP_VERSION}")
        self._quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self._status_item,
            self._detail_item,
            None,
            self._action_item,
            None,
            self._mapping_item,
            self._reveal_item,
            self._reload_item,
            None,
            self._dsu_item,
            self._login_item,
            self._logs_item,
            None,
            self._version_item,
            self._quit_item,
        ]
        self._sync_dsu()
        self._sync_login_item()
        # packets/s: sampled by the UI tick
        self._rate_prev_count = 0
        self._rate_prev_time = time.monotonic()

        self._last_state = None
        self._accessibility_checked = False
        # Short error shown in the dropdown while idle — notifications are
        # unreliable when running from source, the menu is always visible
        self._idle_note = None
        self._apply_state('idle')

        self._timer = rumps.Timer(self._tick, self.REFRESH_INTERVAL)
        self._timer.start()

    # --- state machine ---

    def _current_state(self):
        if self.bridge.is_stopping:
            return 'stopping'
        if self.bridge.is_connected:
            return 'connected'
        if self.bridge.is_reconnecting:
            return 'reconnecting'
        if self.bridge.is_connecting:
            return 'connecting'
        if self.bridge.is_searching:
            return 'searching'
        return 'idle'

    def _apply_state(self, state):
        if state == 'searching':
            self.title = "🔍"
            self._status_item.title = "Searching…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
            self._action_item.set_callback(self._on_action)
        elif state == 'connecting':
            self.title = "🔗"
            self._status_item.title = "Connecting…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
            self._action_item.set_callback(self._on_action)
        elif state == 'reconnecting':
            self.title = "🔍"
            self._status_item.title = "Reconnecting…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
            self._action_item.set_callback(self._on_action)
        elif state == 'connected':
            self.title = "🟢"
            self._status_item.title = f"✓ {self.bridge.controller_name or 'Controller'}"
            self._detail_item.title = f"   {self.bridge.packet_count} pkts"
            self._action_item.title = "Disconnect"
            self._action_item.set_callback(self._on_action)
        elif state == 'stopping':
            self.title = "🎮"
            self._status_item.title = "Stopping…"
            self._detail_item.title = " "
            self._action_item.title = "Stopping…"
            self._action_item.set_callback(None)  # disabled
        else:  # idle
            self.title = "🎮"
            self._status_item.title = "○ Not connected"
            self._detail_item.title = " "
            self._action_item.title = "Connect Controller"
            self._action_item.set_callback(self._on_action)
        self._last_state = state

    def _tick(self, _):
        """Runs every REFRESH_INTERVAL on the main thread."""
        if not self._accessibility_checked:
            self._accessibility_checked = True
            self._check_accessibility()
            self._surface_mappings_messages()

        if self.bridge.last_error:
            err = self.bridge.last_error
            self.bridge.last_error = None
            log.warning("user-visible bridge error: %s", err)
            self._idle_note = err.splitlines()[0][:70]
            self._notify("Connection failed", err)

        if self.bridge.last_notice:
            notice = self.bridge.last_notice
            self.bridge.last_notice = None
            log.info("user-visible bridge notice: %s", notice)
            self._notify("Controller", notice)

        if self.dsu.last_error:
            err = self.dsu.last_error
            self.dsu.last_error = None
            log.warning("user-visible DSU error: %s", err)
            self._notify("DSU server", err)

        state = self._current_state()
        if state != self._last_state:
            self._apply_state(state)
        if state == 'idle' and self._idle_note:
            self._detail_item.title = f"   ⚠️ {self._idle_note}"
        elif state == 'connected':
            # in-place title update — no menu rebuild
            count = self.bridge.packet_count
            now = time.monotonic()
            elapsed = now - self._rate_prev_time
            rate = max(0.0, (count - self._rate_prev_count) / elapsed) if elapsed > 0 else 0.0
            self._rate_prev_count, self._rate_prev_time = count, now
            detail = f"   {count} pkts · {rate:.0f}/s"
            if self.dsu.running:
                n = self.dsu.client_count()
                if n:
                    detail += f" · DSU: {n} client{'s' if n > 1 else ''}"
            self._detail_item.title = detail

    # --- actions ---

    def _on_action(self, _):
        state = self._current_state()
        if state == 'idle':
            if not self._check_bluetooth():
                return
            self._idle_note = None
            self.bridge.connect()
        elif state != 'stopping':
            self.bridge.disconnect()
        self._tick(None)

    def _show_mapping(self, _):
        b = self.mappings.buttons
        ls = self.mappings.left_stick
        rs = self.mappings.right_stick

        def fmt(value):
            if value is None:
                return "—"
            if isinstance(value, str):
                return value
            return f"<{value.name}>"  # Key enum

        lines = [
            "Current mapping",
            "",
            f"  A→{fmt(b.get('A'))}  B→{fmt(b.get('B'))}  X→{fmt(b.get('X'))}  Y→{fmt(b.get('Y'))}",
            f"  L→{fmt(b.get('L'))}  R→{fmt(b.get('R'))}  ZL→{fmt(b.get('ZL'))}  ZR→{fmt(b.get('ZR'))}",
            f"  +→{fmt(b.get('+'))}  -→{fmt(b.get('-'))}  Home→{fmt(b.get('HOME'))}  Capture→{fmt(b.get('CAPT'))}",
            f"  GL→{fmt(b.get('GL'))}  GR→{fmt(b.get('GR'))}  LS→{fmt(b.get('LS'))}  RS→{fmt(b.get('RS'))}",
            f"  C→{fmt(b.get('C'))}",
            "",
            f"  Left stick: {fmt(ls.get('up'))}/{fmt(ls.get('left'))}/{fmt(ls.get('down'))}/{fmt(ls.get('right'))} (U/L/D/R)",
            f"  Right stick: {fmt(rs.get('up'))}/{fmt(rs.get('left'))}/{fmt(rs.get('down'))}/{fmt(rs.get('right'))} (U/L/D/R)",
            f"  D-Pad: {fmt(b.get('DUP'))}/{fmt(b.get('DLEFT'))}/{fmt(b.get('DDOWN'))}/{fmt(b.get('DRIGHT'))} (U/L/D/R)",
            f"  Stick threshold: {self.mappings.stick_threshold}",
            "",
            f"Edit: {MAPPINGS_FILE}",
        ]
        rumps.alert(title="Button Mapping", message="\n".join(lines), ok="OK")

    def _reveal_mappings(self, _):
        Mappings.ensure_default_file()
        try:
            subprocess.Popen(["open", "-R", str(MAPPINGS_FILE)])
        except Exception as e:
            log.exception("could not open Finder")
            rumps.alert(title=APP_NAME, message=f"Could not reveal file: {e}", ok="OK")

    def _reload_mappings(self, _):
        # Release everything currently held to avoid stuck keys with the new map
        self.bridge.release_all_keys()
        ok = self.mappings.load()
        self._sync_dsu()
        self._surface_mappings_messages()
        if ok:
            self._notify("Mappings reloaded", f"Loaded from {MAPPINGS_FILE.name}")

    def _toggle_dsu(self, _):
        self.mappings.set_dsu_enabled(not self.mappings.dsu_enabled)
        self._sync_dsu()

    def _sync_dsu(self):
        """Reconcile the DSU server with the current settings."""
        m = self.mappings
        settings_changed = (self.dsu.host, self.dsu.port) != (m.dsu_host, m.dsu_port)
        if self.dsu.running and (not m.dsu_enabled or settings_changed):
            self.dsu.stop()
        if settings_changed:
            self.dsu.host, self.dsu.port = m.dsu_host, m.dsu_port
        if m.dsu_enabled and not self.dsu.running:
            self.dsu.start()
        self.dsu.set_connected(self.bridge.is_connected)
        if self.dsu.running:
            self._dsu_item.title = f"DSU server ({self.dsu.host}:{self.dsu.port})"
            self._dsu_item.state = 1
        else:
            self._dsu_item.title = "DSU server"
            self._dsu_item.state = 0

    def _login_service(self):
        """SMAppService for the main app, or None when unavailable.

        Registration only works from a bundled .app (py2app sets sys.frozen),
        and needs the pyobjc ServiceManagement framework (macOS 13+).
        """
        if SMAppService is None or not getattr(sys, "frozen", False):
            return None
        try:
            return SMAppService.mainAppService()
        except Exception:
            log.exception("SMAppService unavailable")
            return None

    def _sync_login_item(self):
        svc = self._login_service()
        if svc is None:
            self._login_item.set_callback(None)  # disabled outside a bundled .app
            self._login_item.state = 0
            return
        self._login_item.set_callback(self._toggle_login)
        self._login_item.state = 1 if svc.status() == 1 else 0  # 1 = enabled

    def _toggle_login(self, _):
        svc = self._login_service()
        if svc is None:
            return
        try:
            if svc.status() == 1:
                res = svc.unregisterAndReturnError_(None)
            else:
                res = svc.registerAndReturnError_(None)
            ok, err = res if isinstance(res, tuple) else (res, None)
            if not ok:
                raise RuntimeError(err)
        except Exception as e:
            log.exception("login item toggle failed")
            rumps.alert(
                title=APP_NAME,
                message=f"Could not update the login item: {e}",
                ok="OK",
            )
        self._sync_login_item()

    def _open_logs(self, _):
        try:
            subprocess.Popen(["open", str(LOG_DIR)])
        except Exception as e:
            log.exception("could not open logs folder")
            rumps.alert(title=APP_NAME, message=f"Could not open logs: {e}", ok="OK")

    def _on_quit(self, _):
        log.info("quitting")
        self.bridge.disconnect(wait=True, timeout=2.0)
        # Belt & suspenders: never leave a key logically held after exit
        self.bridge.release_all_keys()
        self.dsu.stop()
        rumps.quit_application()

    # --- startup checks ---

    def _check_accessibility(self):
        if AXIsProcessTrusted is None:
            return
        if not AXIsProcessTrusted():
            log.warning("Accessibility permission not granted")
            clicked_ok = rumps.alert(
                title="Accessibility Required",
                message=(
                    f"{APP_NAME} needs Accessibility access to simulate keyboard "
                    "input.\n\n"
                    "Grant access in:\n"
                    "System Settings → Privacy & Security → Accessibility\n\n"
                    "You may need to quit and relaunch after granting access."
                ),
                ok="Open System Settings",
                cancel="Later",
            )
            if clicked_ok == 1:
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Accessibility",
                ])

    def _check_bluetooth(self):
        """Return False (and explain) when Bluetooth permission is denied.

        CBManagerAuthorization: 0 not determined (prompt will appear on first
        scan), 1 restricted, 2 denied, 3 allowed.
        """
        try:
            from CoreBluetooth import CBManager  # ships with bleak's backend
            auth = int(CBManager.authorization())
        except Exception:
            return True  # can't check — let the scan surface any error
        if auth in (1, 2):
            log.warning("Bluetooth permission denied (auth=%s)", auth)
            clicked_ok = rumps.alert(
                title="Bluetooth Permission Required",
                message=(
                    f"macOS is blocking Bluetooth access for {APP_NAME}.\n\n"
                    "Grant it in:\n"
                    "System Settings → Privacy & Security → Bluetooth\n\n"
                    "⚠️ When running from source, the permission belongs to "
                    "Terminal (or your Python interpreter) — enable that entry, "
                    "then relaunch."
                ),
                ok="Open System Settings",
                cancel="Later",
            )
            if clicked_ok == 1:
                subprocess.Popen([
                    "open",
                    "x-apple.systempreferences:com.apple.preference.security"
                    "?Privacy_Bluetooth",
                ])
            return False
        return True

    def _surface_mappings_messages(self):
        if self.mappings.last_error:
            err = self.mappings.last_error
            self.mappings.last_error = None
            log.warning("user-visible mappings error: %s", err)
            rumps.alert(title="Mappings", message=err, ok="OK")
        if self.mappings.last_warning:
            warn = self.mappings.last_warning
            self.mappings.last_warning = None
            log.warning("user-visible mappings warning: %s", warn)
            self._notify("Mappings", warn)

    # --- helpers ---

    def _notify(self, subtitle, message):
        try:
            rumps.notification(APP_NAME, subtitle, message)
        except Exception:
            # notifications need a bundled .app — fall back to alert
            rumps.alert(title=APP_NAME, message=f"{subtitle}\n{message}", ok="OK")


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print(f"\n🎮 {APP_NAME}")
    print("   App is running in the menu bar.")
    print(f"   Mappings: {MAPPINGS_FILE}")
    print(f"   Logs:     {LOG_DIR / 'bridge.log'}\n")
    log.info("starting %s", APP_NAME)
    Switch2BridgeApp().run()
