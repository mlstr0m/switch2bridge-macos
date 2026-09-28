# 🎮 Switch 2 Pro Controller — macOS BLE Bridge

**The first working Bluetooth LE client for the Nintendo Switch 2 Pro Controller on macOS.**

A Python menubar app that connects to the Switch 2 Pro Controller via BLE and translates inputs to keyboard presses for use with emulators like Ryujinx.

[![CI](https://github.com/mlstr0m/switch2bridge-macos/actions/workflows/ci.yml/badge.svg)](https://github.com/mlstr0m/switch2bridge-macos/actions/workflows/ci.yml)
[![macOS](https://img.shields.io/badge/macOS-Ventura%2B-blue?logo=apple)](https://www.apple.com/macos)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green?logo=python)](https://python.org)
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
- ✅ **Factory stick calibration** — read from the controller at connect, so sticks reach full deflection without drift
- ✅ **Grip buttons** — Switch 2 exclusive GL/GR buttons supported
- ✅ **Ryujinx compatible** — keyboard bridge for emulator support
- ✅ **No pairing required** — bypasses macOS Bluetooth limitations
- ✅ **Auto-reconnect** — if the controller sleeps or drops, the bridge retries for 60 s
- ✅ **C button** — the Switch 2's new C button can be mapped
- ✅ **Player LED** — player 1 light is set on connect (best effort)
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
- Python 3.10+ (bleak 3 requires it)
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
pip install -r requirements-build.txt   # adds py2app
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

The `"ble"` section holds two advanced settings:
- `input_char`: leave it `null` to auto-detect the controller's input characteristic (the bridge fills it in itself once detected), or set a 128-bit UUID to force one. See [BLE Characteristics](#ble-characteristics).
- `address`: leave it `null` to connect to any available Switch 2 Pro Controller, or set a Bluetooth address/UUID to pin this instance to a specific controller (useful for multi-controller setups).

Invalid JSON falls back to defaults and the menubar surfaces the parse error. Unknown button names or stick directions (typos) are reported via a notification instead of being silently ignored. Two inputs may share the same key: the key is only released once both are released.

### Multi-controller and multiple instances

To run multiple controllers simultaneously, launch separate instances of the app with `--config` pointing to different mapping files:

- **From a bundled .app:**
  ```bash
  open -n -a "Switch2 Bridge" --args --config ~/Library/Application\ Support/Switch2Bridge/pad2.json
  ```
- **From source:**
  ```bash
  python Switch2Bridge.py --config ~/Library/Application\ Support/Switch2Bridge/pad2.json
  ```

If the specified config file does not exist, it is created with defaults.

> [!IMPORTANT]
> **Avoid port and controller collisions when running multiple instances:**
> 1. **Per-instance DSU ports:** Every fresh configuration file defaults to DSU port `26760`. Two instances cannot bind to the same UDP port. Edit the second configuration file to use another port (e.g., `"port": 26761`) or disable DSU (`"enabled": false`) on that instance.
> 2. **Controller assignment (`ble.address`):** By default, an instance connects to the first controller it discovers. To deterministically bind each instance to its own gamepad, pin each controller's Bluetooth address/UUID in its config file:
>    ```json
>    "ble": {
>      "input_char": null,
>      "address": "B9EA5233-37EF-4DD6-8A31-9EEAE20F78F8"
>    }
>    ```
>    The controller's address is printed in the connection log (`connected to Switch 2 Pro Controller @ <address>`).
> 3. **Per-instance logs:** Each instance names its log file after the configuration file (e.g. `pad2.log`), so instances do not interfere with each other's logs.

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
├── requirements.txt    # Runtime dependencies
├── requirements-build.txt  # + py2app, for the .app / DMG
├── tests/
│   ├── test_bridge.py  # Headless tests (mappings, key dispatch, BLE lifecycle, calibration)
│   └── test_dsu.py     # DSU server over real UDP
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
| `7492866c-ec3e-4619-8258-32755ffcc0f9` | Input reports (notifications) — what the bridge reads |
| `7492866c-ec3e-4619-8258-32755ffcc0f8` | Also notify-only (read, notify) — it can't be written, which is why LED/rumble never worked through it |
| `649d4ac9-8eb7-4e6c-af44-1ea54fe5f005` | Command channel (write without response) |
| `c765a961-d9d8-4d36-a20a-5315b111836a` | Command replies (notifications) |

UUIDs from [ndeadly/switch2_controller_research](https://github.com/ndeadly/switch2_controller_research) (`bluetooth_interface.md`) and the hardware findings in [#14](https://github.com/mlstr0m/switch2bridge-macos/issues/14).

### Input report

| Byte | Content |
|------|---------|
| 2 | `0x01` B, `0x02` A, `0x04` Y, `0x08` X, `0x10` R, `0x20` ZR, `0x40` +, `0x80` RS |
| 3 | `0x01` Down, `0x02` Right, `0x04` Left, `0x08` Up, `0x10` L, `0x20` ZL, `0x40` −, `0x80` LS |
| 4 | `0x01` Home, `0x02` Capture, `0x04` GR, `0x08` GL, `0x10` C |
| 5–7 / 8–10 | Left / right stick, two packed 12-bit values each |

Byte 4 follows ndeadly's `hid_reports.md`, [espp's Pro Controller 2 report](https://github.com/esp-cpp/espp/pull/765) and the capture in #14 — up to v1.2.4 the bridge had C and Capture swapped.

### Stick calibration

At connect the bridge reads the controller's factory calibration over the command channel (SPI reads of `0x130A8` for the left stick and `0x130E8` for the right, 9 bytes each: centre, +travel, −travel as packed 12-bit pairs), then lights the player 1 LED. Real travel is only ~1500–1770 counts rather than the nominal 2048, so without it a fully pushed stick tops out around 0.8 in DSU. If the controller doesn't answer, the bridge silently keeps the nominal range; the menubar shows *sticks calibrated* when it worked.

Frame format and addresses come from [kennethreitz's fork](https://github.com/kennethreitz/switch2bridge-macos/blob/975f329592a4ef56bd8ac2947d4d148eaf808fe0/controller_commands.py) (issue #14, MIT), itself based on [BlueRetro #1249](https://github.com/darthcloud/BlueRetro/issues/1249); the addresses match [SDL's Switch 2 driver](https://github.com/libsdl-org/SDL/blob/main/src/joystick/hidapi/SDL_hidapi_switch2.c).

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
- **Logs** — written to `~/Library/Logs/Switch2Bridge/bridge.log` (or `<config_name>.log` when running with `--config`). Open a terminal and `tail -f` it to watch what's happening in real time.

## 🚧 Limitations

| Feature | Status | Notes |
|---------|--------|-------|
| Buttons | ✅ Working | All buttons mapped |
| C button | ✅ Fixed in v1.3.0 | Byte 4, bit `0x10` (was wrongly read as `0x02`, i.e. Capture, up to v1.2.4) |
| Analog Sticks | ✅ Analog via DSU | Full 12-bit analog through the DSU server (Dolphin/Cemu), scaled with the controller's factory calibration. The keyboard bridge remains digital: thresholded (with hysteresis) to 8 directions (WASD/IJKL). |
| LED Control | 🧪 Player 1 only | Set once on connect through the command channel |
| Rumble | ❌ Not implemented | Goes through the command/vibration channels, not `…c0f8` |
| Motion/Gyro | ⚠️ Plumbing ready | DSU motion fields are sent (as zeros) — the gyro bytes in the BLE report are not decoded yet |
| Native HID | ❌ Not possible | Would require DriverKit (kernel-level) |

## 🤝 Contributing

Contributions welcome! Areas that need work:

1. **Rumble** — the command channel is now used for calibration and LEDs; rumble is the next step (see #14)
2. **Motion controls** — decode gyro/accelerometer data
3. **True analog** — virtual HID device via DriverKit
4. **Cross-platform** — Linux/Windows ports

## 📜 Credits

- **Aurélien Desert** — reverse engineering & implementation
- **Claude (Anthropic)** — development assistance
- Inspired by [SPro2Win](https://github.com/SquareDonut1/SPro2Win) (Windows)
- Protocol reference from [Nintendo Switch Reverse Engineering](https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering)
- Switch 2 BLE protocol: [ndeadly/switch2_controller_research](https://github.com/ndeadly/switch2_controller_research), [darthcloud & german77 (BlueRetro #1249)](https://github.com/darthcloud/BlueRetro/issues/1249)
- C/Capture fix, stick calibration and command channel findings: [issue #14](https://github.com/mlstr0m/switch2bridge-macos/issues/14) and [kennethreitz's fork](https://github.com/kennethreitz/switch2bridge-macos/tree/real-gamepad-output)

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

<p align="center">
  <b>⭐ Star this repo if it helped you!</b><br>
  <i>First macOS BLE bridge for Switch 2 Pro Controller — January 2026</i>
</p>
