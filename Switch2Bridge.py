#!/usr/bin/env python3
"""
Switch2 Bridge - macOS Menubar App
==================================

A clean menubar app to connect your Switch 2 Pro Controller
and use it with Ryujinx.

Author: Aurélien Desert
License: MIT
"""

import asyncio
import logging
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

BUTTON_KEYS = {
    'A': 'z', 'B': 'x', 'X': 'c', 'Y': 'v',
    'L': 'q', 'R': 'e', 'ZL': '1', 'ZR': '3',
    '+': 'p', '-': 'm', 'HOME': 'h', 'CAPT': 'o',
    'LS': 'f', 'RS': 'g', 'GL': '9', 'GR': '0',
    'DUP': Key.up, 'DDOWN': Key.down,
    'DLEFT': Key.left, 'DRIGHT': Key.right,
}

STICK_THRESHOLD = 0.5

LOG_DIR = Path.home() / "Library" / "Logs" / "Switch2Bridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "bridge.log"),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ============================================================
# CONTROLLER BRIDGE (BLE + keyboard, runs in worker thread)
# ============================================================

class ControllerBridge:
    """BLE connection + keyboard input simulation."""

    def __init__(self):
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

    def _release_all_keys(self):
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

        b2, b3, b4 = data[2], data[3], data[4]

        # face buttons (byte 2)
        self._set_key(BUTTON_KEYS['B'], b2 & 0x01)
        self._set_key(BUTTON_KEYS['A'], b2 & 0x02)
        self._set_key(BUTTON_KEYS['Y'], b2 & 0x04)
        self._set_key(BUTTON_KEYS['X'], b2 & 0x08)
        self._set_key(BUTTON_KEYS['R'], b2 & 0x10)
        self._set_key(BUTTON_KEYS['ZR'], b2 & 0x20)
        self._set_key(BUTTON_KEYS['+'], b2 & 0x40)
        self._set_key(BUTTON_KEYS['RS'], b2 & 0x80)

        # d-pad + left shoulder/trigger (byte 3)
        self._set_key(BUTTON_KEYS['DDOWN'], b3 & 0x01)
        self._set_key(BUTTON_KEYS['DRIGHT'], b3 & 0x02)
        self._set_key(BUTTON_KEYS['DLEFT'], b3 & 0x04)
        self._set_key(BUTTON_KEYS['DUP'], b3 & 0x08)
        self._set_key(BUTTON_KEYS['L'], b3 & 0x10)
        self._set_key(BUTTON_KEYS['ZL'], b3 & 0x20)
        self._set_key(BUTTON_KEYS['-'], b3 & 0x40)
        self._set_key(BUTTON_KEYS['LS'], b3 & 0x80)

        # special (byte 4)
        self._set_key(BUTTON_KEYS['HOME'], b4 & 0x01)
        self._set_key(BUTTON_KEYS['GR'], b4 & 0x04)
        self._set_key(BUTTON_KEYS['GL'], b4 & 0x08)
        self._set_key(BUTTON_KEYS['CAPT'], b4 & 0x10)

        # sticks: 12-bit packed across bytes 5-10
        lx_raw = data[5] | ((data[6] & 0x0F) << 8)
        ly_raw = ((data[6] & 0xF0) >> 4) | (data[7] << 4)
        rx_raw = data[8] | ((data[9] & 0x0F) << 8)
        ry_raw = ((data[9] & 0xF0) >> 4) | (data[10] << 4)

        lx = (lx_raw - 2048) / 2048.0
        ly = (ly_raw - 2048) / 2048.0
        rx = (rx_raw - 2048) / 2048.0
        ry = (ry_raw - 2048) / 2048.0

        # left stick → WASD
        self._set_key('w', ly > STICK_THRESHOLD)
        self._set_key('s', ly < -STICK_THRESHOLD)
        self._set_key('a', lx < -STICK_THRESHOLD)
        self._set_key('d', lx > STICK_THRESHOLD)

        # right stick → IJKL
        self._set_key('i', ry > STICK_THRESHOLD)
        self._set_key('k', ry < -STICK_THRESHOLD)
        self._set_key('j', rx < -STICK_THRESHOLD)
        self._set_key('l', rx > STICK_THRESHOLD)

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
            self._release_all_keys()
            self.is_connected = False
            self.controller_name = None

    # --- public API ---

    def connect(self):
        """Start connection in a background thread (non-blocking)."""
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
    Menu items are constructed once, then mutated in place via .title /
    .set_callback. We never call self.menu.clear() or reassign self.menu
    after init — doing so dismisses the open dropdown on every Timer tick.
    """

    REFRESH_INTERVAL = 1.0  # seconds — packet counter cadence

    def __init__(self):
        super().__init__(APP_NAME, title="🎮", quit_button=None)
        self.bridge = ControllerBridge()

        # Long-lived menu items
        self._status_item = rumps.MenuItem("○ Not connected")
        self._detail_item = rumps.MenuItem(" ")
        self._action_item = rumps.MenuItem("Connect Controller", callback=self._on_action)
        self._mapping_item = rumps.MenuItem("Button Mapping…", callback=self._show_mapping)
        self._quit_item = rumps.MenuItem("Quit", callback=self._on_quit)

        self.menu = [
            self._status_item,
            self._detail_item,
            None,
            self._action_item,
            None,
            self._mapping_item,
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
        """Update titles + callbacks for the current state — never rebuilds the menu."""
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
        # Defer accessibility check until the NSApp event loop is live
        if not self._accessibility_checked:
            self._accessibility_checked = True
            self._check_accessibility()

        # Surface any error raised by the worker
        if self.bridge.last_error:
            err = self.bridge.last_error
            self.bridge.last_error = None
            log.warning("user-visible error: %s", err)
            try:
                rumps.notification(APP_NAME, "Connection failed", err)
            except Exception:
                # notifications require a bundled .app — fall back to alert
                rumps.alert(title=APP_NAME, message=err, ok="OK")

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
        self._tick(None)  # snappy UI update

    def _show_mapping(self, _):
        rumps.alert(
            title="Ryujinx Button Mapping",
            message=(
                "In Ryujinx: Settings → Input → Keyboard\n\n"
                "BUTTONS\n"
                "  A→Z  B→X  X→C  Y→V\n"
                "  L→Q  R→E  ZL→1  ZR→3\n"
                "  +→P  -→M  Home→H  Capture→O\n\n"
                "STICKS\n"
                "  Left: WASD    Right: IJKL\n\n"
                "D-PAD: Arrow keys"
            ),
            ok="OK",
        )

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


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print(f"\n🎮 {APP_NAME}")
    print("   App is running in the menu bar.")
    print(f"   Logs: {LOG_DIR / 'bridge.log'}\n")
    log.info("starting %s", APP_NAME)
    Switch2BridgeApp().run()
