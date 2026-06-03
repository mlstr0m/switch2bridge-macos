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
import subprocess
import sys
import threading
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


# ============================================================
# CONSTANTS
# ============================================================

APP_NAME = "Switch2 Bridge"
INPUT_CHAR_UUID = "7492866c-ec3e-4619-8258-32755ffcc0f9"

# Bluetooth SIG company identifier for Nintendo
NINTENDO_COMPANY_ID = 0x057e
# Switch 2 Pro Controller product ID 0x2069, little-endian as it appears on the wire
SWITCH2_PRO_PID_LE = b'\x69\x20'

CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Switch2Bridge"
MAPPINGS_FILE = CONFIG_DIR / "mappings.json"

LOG_DIR = Path.home() / "Library" / "Logs" / "Switch2Bridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "bridge.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ============================================================
# MAPPINGS — load/save user-editable JSON
# ============================================================

class Mappings:
    """User-editable button + stick mappings.

    Loaded from ~/Library/Application Support/Switch2Bridge/mappings.json.
    On first launch, the default mapping is written there so users can edit it.
    """

    DEFAULT = {
        "version": 1,
        "buttons": {
            "A": "z", "B": "x", "X": "c", "Y": "v",
            "L": "q", "R": "e", "ZL": "1", "ZR": "3",
            "+": "p", "-": "m", "HOME": "h", "CAPT": "o",
            "LS": "f", "RS": "g", "GL": "9", "GR": "0",
            "DUP": "<up>", "DDOWN": "<down>",
            "DLEFT": "<left>", "DRIGHT": "<right>",
        },
        "sticks": {
            "threshold": 0.5,
            "left":  {"up": "w", "down": "s", "left": "a", "right": "d"},
            "right": {"up": "i", "down": "k", "left": "j", "right": "l"},
        },
    }

    SPECIAL_KEYS = {
        "<up>": Key.up, "<down>": Key.down,
        "<left>": Key.left, "<right>": Key.right,
        "<space>": Key.space, "<enter>": Key.enter,
        "<esc>": Key.esc, "<tab>": Key.tab,
        "<backspace>": Key.backspace,
        "<shift>": Key.shift, "<ctrl>": Key.ctrl,
        "<alt>": Key.alt, "<cmd>": Key.cmd,
    }

    def __init__(self):
        self.buttons = {}
        self.stick_threshold = 0.5
        self.left_stick = {}
        self.right_stick = {}
        self.last_error = None  # consumed by the UI tick

    # --- IO ---

    @classmethod
    def ensure_default_file(cls):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not MAPPINGS_FILE.exists():
            with open(MAPPINGS_FILE, "w") as f:
                json.dump(cls.DEFAULT, f, indent=2)
            log.info("wrote default mappings to %s", MAPPINGS_FILE)

    def load(self):
        """Load mappings from disk, falling back to defaults on error."""
        self.last_error = None
        cfg = self.DEFAULT
        try:
            self.ensure_default_file()
            with open(MAPPINGS_FILE) as f:
                cfg = json.load(f)
        except Exception as e:
            log.exception("failed to read mappings.json")
            self.last_error = f"Could not read mappings.json: {e}\nUsing defaults."

        try:
            self._apply(cfg)
            log.info("mappings loaded from %s", MAPPINGS_FILE)
            return True
        except Exception as e:
            log.exception("invalid mappings.json")
            self.last_error = f"Invalid mappings.json: {e}\nUsing defaults."
            self._apply(self.DEFAULT)
            return False

    # --- internals ---

    def _apply(self, cfg):
        buttons = cfg.get("buttons", {})
        sticks = cfg.get("sticks", {})

        self.buttons = {name: self._parse_key(v) for name, v in buttons.items()}
        self.stick_threshold = float(sticks.get("threshold", 0.5))
        self.left_stick = {d: self._parse_key(k) for d, k in sticks.get("left", {}).items()}
        self.right_stick = {d: self._parse_key(k) for d, k in sticks.get("right", {}).items()}

    @classmethod
    def _parse_key(cls, value):
        if not isinstance(value, str):
            raise ValueError(f"key must be a string, got {type(value).__name__}")
        if value in cls.SPECIAL_KEYS:
            return cls.SPECIAL_KEYS[value]
        if len(value) == 1:
            return value
        raise ValueError(f"unknown key {value!r} (use a single character or one of {sorted(cls.SPECIAL_KEYS)})")


# ============================================================
# CONTROLLER BRIDGE (BLE + keyboard, runs in worker thread)
# ============================================================

class ControllerBridge:
    """BLE connection + keyboard input simulation."""

    def __init__(self, mappings: Mappings):
        self.mappings = mappings
        self.is_connected = False
        self.is_searching = False
        self.controller_name = None
        self.packet_count = 0
        # Set by worker, read & cleared by the main-thread UI tick
        self.last_error = None
        self.pressed_keys = set()
        self._client = None
        self._stop_event = threading.Event()
        self._thread = None

    # --- key dispatch ---

    def _set_key(self, key, active):
        if key is None:
            return
        if active:
            if key not in self.pressed_keys:
                self.pressed_keys.add(key)
                try:
                    keyboard.press(key)
                except Exception as e:
                    log.warning("keyboard.press failed: %s", e)
        else:
            if key in self.pressed_keys:
                self.pressed_keys.discard(key)
                try:
                    keyboard.release(key)
                except Exception as e:
                    log.warning("keyboard.release failed: %s", e)

    def release_all_keys(self):
        for key in list(self.pressed_keys):
            try:
                keyboard.release(key)
            except Exception as e:
                log.warning("keyboard.release on cleanup failed: %s", e)
        self.pressed_keys.clear()

    # --- BLE input parser ---

    def _on_data(self, sender, data: bytes):
        if len(data) < 11:
            return

        self.packet_count += 1

        b = self.mappings.buttons
        b2, b3, b4 = data[2], data[3], data[4]

        # face buttons (byte 2)
        self._set_key(b.get('B'), b2 & 0x01)
        self._set_key(b.get('A'), b2 & 0x02)
        self._set_key(b.get('Y'), b2 & 0x04)
        self._set_key(b.get('X'), b2 & 0x08)
        self._set_key(b.get('R'), b2 & 0x10)
        self._set_key(b.get('ZR'), b2 & 0x20)
        self._set_key(b.get('+'), b2 & 0x40)
        self._set_key(b.get('RS'), b2 & 0x80)

        # d-pad + left shoulder/trigger (byte 3)
        self._set_key(b.get('DDOWN'), b3 & 0x01)
        self._set_key(b.get('DRIGHT'), b3 & 0x02)
        self._set_key(b.get('DLEFT'), b3 & 0x04)
        self._set_key(b.get('DUP'), b3 & 0x08)
        self._set_key(b.get('L'), b3 & 0x10)
        self._set_key(b.get('ZL'), b3 & 0x20)
        self._set_key(b.get('-'), b3 & 0x40)
        self._set_key(b.get('LS'), b3 & 0x80)

        # special (byte 4)
        self._set_key(b.get('HOME'), b4 & 0x01)
        self._set_key(b.get('GR'), b4 & 0x04)
        self._set_key(b.get('GL'), b4 & 0x08)
        self._set_key(b.get('CAPT'), b4 & 0x10)

        # sticks: 12-bit packed across bytes 5-10
        lx_raw = data[5] | ((data[6] & 0x0F) << 8)
        ly_raw = ((data[6] & 0xF0) >> 4) | (data[7] << 4)
        rx_raw = data[8] | ((data[9] & 0x0F) << 8)
        ry_raw = ((data[9] & 0xF0) >> 4) | (data[10] << 4)

        lx = (lx_raw - 2048) / 2048.0
        ly = (ly_raw - 2048) / 2048.0
        rx = (rx_raw - 2048) / 2048.0
        ry = (ry_raw - 2048) / 2048.0

        t = self.mappings.stick_threshold
        ls = self.mappings.left_stick
        rs = self.mappings.right_stick

        self._set_key(ls.get('up'),    ly > t)
        self._set_key(ls.get('down'),  ly < -t)
        self._set_key(ls.get('left'),  lx < -t)
        self._set_key(ls.get('right'), lx > t)

        self._set_key(rs.get('up'),    ry > t)
        self._set_key(rs.get('down'),  ry < -t)
        self._set_key(rs.get('left'),  rx < -t)
        self._set_key(rs.get('right'), rx > t)

    # --- discovery ---

    async def _find_controller(self, timeout=5.0):
        """Returns (address, name) or (None, None)."""
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for address, (device, adv) in devices.items():
            # Primary: Nintendo company ID is the dict key in bleak's manufacturer_data
            payload = adv.manufacturer_data.get(NINTENDO_COMPANY_ID)
            if payload and SWITCH2_PRO_PID_LE in payload:
                return address, device.name or "Switch 2 Pro Controller"
            # Fallback: some macOS BLE stacks expose name without manufacturer data
            if device.name and "Pro Controller" in device.name:
                return address, device.name
        return None, None

    # --- main async routine ---

    async def _connect_async(self):
        self.is_searching = True
        self.packet_count = 0

        try:
            address, name = await self._find_controller()
        except Exception as e:
            log.exception("BLE scan failed")
            self.last_error = f"Bluetooth scan failed: {e}"
            self.is_searching = False
            return

        self.is_searching = False

        if self._stop_event.is_set():
            return

        if not address:
            self.last_error = "Controller not found. Make sure it's on and in pairing mode."
            return

        try:
            self._client = BleakClient(address, timeout=15.0)
            await self._client.connect()

            if not self._client.is_connected:
                self.last_error = "Failed to connect to controller."
                return

            self.controller_name = name
            self.is_connected = True
            log.info("connected to %s @ %s", name, address)

            await self._client.start_notify(INPUT_CHAR_UUID, self._on_data)

            while not self._stop_event.is_set() and self._client.is_connected:
                await asyncio.sleep(0.1)

            try:
                if self._client.is_connected:
                    await self._client.stop_notify(INPUT_CHAR_UUID)
                    await self._client.disconnect()
            except Exception as e:
                log.warning("BLE cleanup error: %s", e)

        except Exception as e:
            log.exception("connection error")
            self.last_error = f"Connection error: {e}"
        finally:
            self.release_all_keys()
            self.is_connected = False
            self.controller_name = None

    # --- public API ---

    def connect(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._connect_async())
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def disconnect(self, wait=False, timeout=2.0):
        self._stop_event.set()
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
        self.bridge = ControllerBridge(self.mappings)

        # Long-lived menu items
        self._status_item = rumps.MenuItem("○ Not connected")
        self._detail_item = rumps.MenuItem(" ")
        self._action_item = rumps.MenuItem("Connect Controller", callback=self._on_action)
        self._mapping_item = rumps.MenuItem("Button Mapping…", callback=self._show_mapping)
        self._reveal_item = rumps.MenuItem("Edit mappings file…", callback=self._reveal_mappings)
        self._reload_item = rumps.MenuItem("Reload mappings", callback=self._reload_mappings)
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
            self._quit_item,
        ]

        self._last_state = None
        self._accessibility_checked = False
        self._apply_state('idle')

        self._timer = rumps.Timer(self._tick, self.REFRESH_INTERVAL)
        self._timer.start()

    # --- state machine ---

    def _current_state(self):
        if self.bridge.is_searching:
            return 'searching'
        if self.bridge.is_connected:
            return 'connected'
        return 'idle'

    def _apply_state(self, state):
        if state == 'searching':
            self.title = "🔍"
            self._status_item.title = "Searching…"
            self._detail_item.title = " "
            self._action_item.title = "Cancel"
        elif state == 'connected':
            self.title = "🟢"
            self._status_item.title = f"✓ {self.bridge.controller_name or 'Controller'}"
            self._detail_item.title = f"   {self.bridge.packet_count} packets"
            self._action_item.title = "Disconnect"
        else:  # idle
            self.title = "🎮"
            self._status_item.title = "○ Not connected"
            self._detail_item.title = " "
            self._action_item.title = "Connect Controller"
        self._last_state = state

    def _tick(self, _):
        """Runs every REFRESH_INTERVAL on the main thread."""
        if not self._accessibility_checked:
            self._accessibility_checked = True
            self._check_accessibility()
            self._surface_mappings_error()

        if self.bridge.last_error:
            err = self.bridge.last_error
            self.bridge.last_error = None
            log.warning("user-visible bridge error: %s", err)
            self._notify("Connection failed", err)

        state = self._current_state()
        if state != self._last_state:
            self._apply_state(state)
        elif state == 'connected':
            # in-place title update — no menu rebuild
            self._detail_item.title = f"   {self.bridge.packet_count} packets"

    # --- actions ---

    def _on_action(self, _):
        state = self._current_state()
        if state == 'idle':
            self.bridge.connect()
        else:
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
        self._surface_mappings_error()
        if ok:
            self._notify("Mappings reloaded", f"Loaded from {MAPPINGS_FILE.name}")

    def _on_quit(self, _):
        log.info("quitting")
        self.bridge.disconnect(wait=True, timeout=2.0)
        rumps.quit_application()

    # --- startup checks ---

    def _check_accessibility(self):
        if AXIsProcessTrusted is None:
            return
        if not AXIsProcessTrusted():
            log.warning("Accessibility permission not granted")
            rumps.alert(
                title="Accessibility Required",
                message=(
                    f"{APP_NAME} needs Accessibility access to simulate keyboard "
                    "input.\n\n"
                    "Grant access in:\n"
                    "System Settings → Privacy & Security → Accessibility\n\n"
                    "You may need to quit and relaunch after granting access."
                ),
                ok="OK",
            )

    def _surface_mappings_error(self):
        if self.mappings.last_error:
            err = self.mappings.last_error
            self.mappings.last_error = None
            log.warning("user-visible mappings error: %s", err)
            rumps.alert(title="Mappings", message=err, ok="OK")

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
