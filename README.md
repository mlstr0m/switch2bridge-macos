# 🎮 Switch 2 Pro Controller — macOS BLE Bridge

**The first working Bluetooth LE client for the Nintendo Switch 2 Pro Controller on macOS.**

A Python menubar app that connects to the Switch 2 Pro Controller via BLE and translates inputs to keyboard presses for use with emulators like Ryujinx.

[![CI](https://github.com/mlstr0m/switch2bridge-macos/actions/workflows/ci.yml/badge.svg)](https://github.com/mlstr0m/switch2bridge-macos/actions/workflows/ci.yml)
[![macOS](https://img.shields.io/badge/macOS-Ventura%2B-blue?logo=apple)](https://www.apple.com/macos)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green?logo=python)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

---

## ⚠️ What This Is (and Isn't)

**This is NOT a system driver.** It won't make your controller appear in System Preferences or work natively with games.

**This IS:**
- ✅ A BLE client that reads controller inputs via Bluetooth Low Energy
- ✅ A keyboard bridge that converts inputs to key presses for Ryujinx
- ✅ A reference implementation for the Switch 2 Pro Controller BLE protocol

## 🚀 Features

- ✅ **Full button mapping** — all buttons, triggers, D-pad working
- ✅ **Analog sticks** — read with 12-bit precision (converted to 8 directions, see Limitations)
- ✅ **Grip buttons** — Switch 2 exclusive GL/GR buttons supported
- ✅ **Ryujinx compatible** — keyboard bridge for emulator support
- ✅ **No pairing required** — bypasses macOS Bluetooth limitations
- ✅ **Auto-reconnect** — if the controller sleeps or drops, the bridge retries for 60 s
- ✅ **C button** — the Switch 2's new C button can be mapped (experimental)
- ✅ **DSU server (cemuhook)** — true **analog sticks** in Dolphin, Cemu & other DSU clients, no driver needed
- ✅ **Start at Login** — one click in the menubar (bundled .app, macOS 13+)

## 🤔 Why This Exists

The Nintendo Switch 2 Pro Controller (Product ID: `0x2069`) doesn't work with macOS natively:

| Method | Status | Problem |
|--------|--------|---------|
| USB | ❌ | Firmware blocks non-Switch connections |
| Bluetooth Classic | ❌ | macOS can't discover/pair with it |
| Bluetooth LE | ✅ | Works with custom BLE client (this project) |

This bridge connects via BLE using the `bleak` library, reads the raw input data, and converts it to keyboard presses that Ryujinx can use.

## 📋 Requirements

- macOS Ventura (13.0) or later
- Python 3.9+
- Nintendo Switch 2 Pro Controller

## 🔧 Run from source

```bash
git clone https://github.com/mlstr0m/switch2bridge-macos.git
cd switch2bridge-macos

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python Switch2Bridge.py
```

macOS will ask for two permissions:
1. **Accessibility** — prompted at first launch (to simulate keyboard input)
2. **Bluetooth** — prompted the first time you click **Connect Controller** (not at launch!)

⚠️ **When running from source, the Bluetooth permission belongs to _Terminal_ (or your Python interpreter), not to the app.** If no prompt ever appears, add/enable Terminal manually in `System Settings → Privacy & Security → Bluetooth`, then relaunch. The app detects a denied permission and offers to open the right settings pane.

## 📦 Build a standalone .app + DMG

```bash
chmod +x build_dmg.sh
./build_dmg.sh
```

The installer lands at `dist/Switch2Bridge-Installer.dmg`. Manual build:

```bash
python setup_app.py py2app
# → dist/Switch2 Bridge.app
```

## 🎯 Usage

1. Launch the app — a 🎮 appears in the menu bar
2. Click → **Connect Controller**
3. Wait for 🟢 (connected)
4. Open Ryujinx → Options → Settings → Input
   - Input Device: **Keyboard**
   - Controller Type: **Pro Controller**
   - Map keys using the table below

## 🎮 Button Mapping

Default mapping:

| Button | Key | | Button | Key |
|--------|-----|-|--------|-----|
| A | Z | | L | Q |
| B | X | | R | E |
| X | C | | ZL | 1 |
| Y | V | | ZR | 3 |
| + | P | | LS (click) | F |
| - | M | | RS (click) | G |
| Home | H | | GL (grip) | 9 |
| Capture | O | | GR (grip) | 0 |

| D-Pad | Key | | Stick | Keys |
|-------|-----|-|-------|------|
| Up | ↑ | | Left Stick | WASD |
| Down | ↓ | | Right Stick | IJKL |
| Left | ← | | | |
| Right | → | | | |

### Custom mappings

The first time you launch the app it writes a JSON config to:

```
~/Library/Application Support/Switch2Bridge/mappings.json
```

Edit it to remap any button or stick direction, then **Reload mappings** from the menubar (or restart the app). The menubar also has **Edit mappings file…** which reveals the file in Finder.

Each value is either a single character (`"a"`, `"5"`, `"."`), `null` to leave a button unmapped, or a named key in angle brackets: `<up>`, `<down>`, `<left>`, `<right>`, `<space>`, `<enter>`, `<esc>`, `<tab>`, `<backspace>`, `<delete>`, `<home>`, `<end>`, `<pageup>`, `<pagedown>`, `<shift>`, `<ctrl>`, `<alt>`, `<cmd>`, and `<f1>` … `<f20>`.

The Switch 2's new **C button** is supported as `"C"` (unmapped by default — set it to any key to use it).

The `"ble"` section holds one advanced setting, `input_char`: leave it `null` to auto-detect the controller's input characteristic (the bridge fills it in itself once detected), or set a 128-bit UUID to force one. See [BLE Characteristics](#ble-characteristics).

Invalid JSON falls back to defaults and the menubar surfaces the parse error. Unknown button names or stick directions (typos) are reported via a notification instead of being silently ignored. Two inputs may share the same key: the key is only released once both are released.

## 🕹️ DSU server — true analog sticks (no driver)

The app runs a [cemuhook/DSU](https://github.com/v1993/cemuhook-protocol) server (default `127.0.0.1:26760`), which exposes the controller as a full gamepad over UDP — **analog sticks included**, bypassing the keyboard bridge's 8-direction limitation.

- **Dolphin** — Options → Controller Settings → *Alternate Input Sources* → enable *DSU Client*, add `127.0.0.1:26760`. The pad then appears as an input device with analog axes.
- **Cemu** — Input settings → add a *DSUController* with the same address.
- **Ryujinx** — uses DSU for **motion only** (Settings → Input → enable *Motion* → *Use CemuHook compatible motion*). Buttons/sticks still go through the keyboard bridge. Motion data itself is not decoded yet (sent as zeros).

Configure in `mappings.json`:

```json
"dsu": { "enabled": true, "host": "127.0.0.1", "port": 26760 }
```

or toggle it from the menubar (**DSU server** item — the checkmark shows it's listening). Button mapping on the DSU side is positional: A→Circle, B→Cross, X→Triangle, Y→Square, −→Share, +→Options, Home→PS, Capture→Touch. GL/GR/C have no DSU equivalent.

## 📁 Project Structure

```
switch2bridge-macos/
├── Switch2Bridge.py    # Menubar app (BLE client + keyboard bridge)
├── dsu_server.py       # DSU (cemuhook) server — analog output for emulators
├── setup_app.py        # py2app configuration
├── build_dmg.sh        # Automated build script (.app + DMG)
├── requirements.txt    # Python dependencies
├── tests/
│   └── test_bridge.py  # Headless tests (mappings, key dispatch, BLE lifecycle)
├── AppIcon.icns        # Application icon (used by py2app)
├── LICENSE
└── README.md
```

## 🔬 Technical Details

### How It Works

```
┌─────────────────┐     BLE      ┌─────────────────┐    pynput    ┌─────────────────┐
│  Switch 2 Pro   │ ──────────▶  │  Python Bridge  │ ──────────▶  │    Ryujinx      │
│   Controller    │   (bleak)    │                 │  (keyboard)  │   (Keyboard)    │
└─────────────────┘              └─────────────────┘              └─────────────────┘
```

1. **BLE Connection** — uses `bleak` to connect directly via Bluetooth LE
2. **Input Parsing** — decodes the proprietary Nintendo protocol
3. **Keyboard Simulation** — uses `pynput` to simulate key presses
4. **Ryujinx** — reads keyboard input as if from a physical keyboard

### BLE Characteristics

| UUID | Purpose |
|------|---------|
| `7492866c-ec3e-4619-8258-32755ffcc0f9` | Input reports (notifications) |
| `7492866c-ec3e-4619-8258-32755ffcc0f8` | Output (LED, rumble — not working) |

Not every controller exposes that input UUID (see [#15](https://github.com/mlstr0m/switch2bridge-macos/issues/15)), so the bridge treats it as a first guess only:

1. the UUID pinned in `mappings.json` (`ble.input_char`), if it is present on the device;
2. otherwise the documented UUID above;
3. otherwise it **probes** every other notifiable characteristic — vendor UUIDs first, SIG-assigned ones last — subscribing for 3 s each and keeping the first one that streams reports of at least 11 bytes (enough to decode buttons and both sticks). The winner is written back to `ble.input_char`, so later connections skip the probing.

Every connection also logs the full GATT table (`GATT: N characteristic(s): …`) to `~/Library/Logs/Switch2Bridge/bridge.log` — that line is what a bug report needs when a controller can't be identified.

## 🩺 Troubleshooting

- **The controller never appears in System Settings → Bluetooth** — that's **expected**, and not a failure. This bridge is a BLE client: there is no system-level pairing, so macOS will never list the controller. The only place to watch is the app's menubar icon (🔍 → 🟢).
- **"Controller not found"** — make sure the controller is **not paired with a console nearby** (unpair it or put the console to sleep far away). Click **Connect Controller** *first* — the search now runs for 30 s — *then* hold the small pair button on the back until the LEDs sweep back and forth.
- **No Bluetooth prompt ever appeared (run-from-source)** — the permission belongs to Terminal/Python, not the app. Check `System Settings → Privacy & Security → Bluetooth` and enable Terminal, then relaunch. Without it, scans silently find nothing.
- **"Characteristic … was not found" / "no readable input characteristic"** — the controller connected but its input-report characteristic isn't where the bridge expects it. Since v1.2.4 the bridge probes the alternatives automatically and remembers what worked, so retry once — and move the sticks while it says it is identifying the controller, in case that revision only reports on change. If it still gives up, the log now contains a `GATT:` line listing every service and characteristic of your controller — attach it to an issue. You can also pin a UUID yourself: `"ble": { "input_char": "…" }` in `mappings.json`.
- **Menubar says 🟢 connected but inputs don't reach the emulator** — macOS Accessibility permission is missing. Grant it in *System Settings → Privacy & Security → Accessibility*, then relaunch the app. (The app should also pop an alert about this on first launch.)
- **Logs** — written to `~/Library/Logs/Switch2Bridge/bridge.log`. Open a terminal and `tail -f` it to watch what's happening in real time.

## 🚧 Limitations

| Feature | Status | Notes |
|---------|--------|-------|
| Buttons | ✅ Working | All buttons mapped |
| C button | 🧪 Experimental | Parsed as byte 4, bit `0x02` — please report if it doesn't fire |
| Analog Sticks | ✅ Analog via DSU | Full 12-bit analog through the DSU server (Dolphin/Cemu). The keyboard bridge remains digital: thresholded (with hysteresis) to 8 directions (WASD/IJKL). |
| LED Control | ❌ Not working | Output characteristic doesn't respond |
| Rumble | ❌ Not working | Same issue |
| Motion/Gyro | ⚠️ Plumbing ready | DSU motion fields are sent (as zeros) — the gyro bytes in the BLE report are not decoded yet |
| Native HID | ❌ Not possible | Would require DriverKit (kernel-level) |

## 🤝 Contributing

Contributions welcome! Areas that need work:

1. **LED/Rumble** — figure out the output protocol (likely a Joy-Con-style handshake)
2. **Motion controls** — decode gyro/accelerometer data
3. **True analog** — virtual HID device via DriverKit
4. **Cross-platform** — Linux/Windows ports

## 📜 Credits

- **Aurélien Desert** — reverse engineering & implementation
- **Claude (Anthropic)** — development assistance
- Inspired by [SPro2Win](https://github.com/SquareDonut1/SPro2Win) (Windows)
- Protocol reference from [Nintendo Switch Reverse Engineering](https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering)

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <b>⭐ Star this repo if it helped you!</b><br>
  <i>First macOS BLE bridge for Switch 2 Pro Controller — January 2026</i>
</p>
