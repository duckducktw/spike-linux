# SPIKE 3 for Linux

English | [繁體中文](README.zh-TW.md)

Run the **LEGO Education SPIKE 3** app (v3.6.1) natively on Linux: program SPIKE Prime and SPIKE Essential hubs with Word Blocks, Icon Blocks or Python, over USB or Bluetooth.

> Unofficial community project. Not affiliated with, endorsed by, or supported by the LEGO Group.
> This repository contains no LEGO code or content — the app is extracted on your machine from LEGO's own Windows installer.

## Why this exists

LEGO ships SPIKE 3 for Windows, macOS, iPadOS, Android and ChromeOS, plus a browser version — but no Linux desktop app.
The Windows app turns out to be a web app inside a thin Windows shell (WPF + CefSharp + a local Kestrel server that bridges USB/Bluetooth to the page). This project:

- **extracts** the original web app from the official Windows MSI, unchanged;
- **reimplements the native shell in Python** (the local server, the JSON-RPC protocol the page speaks, USB serial + BLE hub bridge, project files, content downloads), following the decompiled C# behaviour;
- **wraps it in Electron**, so it runs as a normal desktop app with its own window, taskbar icon, menu bar and file associations.

Compared with the browser version, you get offline use, projects saved as normal `.llsp3` files in `~/Documents`, double-click to open, and no dependency on Web Serial / Web Bluetooth support in your browser.

## Install

Download the package for your distro from [Releases](https://github.com/duckducktw/spike-linux/releases):

```bash
# Ubuntu 24.04+ / Debian 13+
sudo apt install ./spike3-linux_*_amd64.deb

# Fedora 41+
sudo dnf install ./spike3-linux_*_x86_64.rpm
```

Then open **LEGO Education SPIKE** from your app menu, or run `spike3`.

**First launch** downloads the official installer from LEGO (~1.4 GB), verifies its SHA-256 and extracts the app to `~/.local/share/spike3-linux/`. This needs about 4.5 GB free temporarily and ~1.4 GB afterwards. It happens once per user.

Already have the MSI? `spike3 --setup /path/to/SPIKE_APP_3_Win10__3.6.1_Global.msi` (it must match the pinned SHA-256).

**Requirements:** x86_64, Python 3.10+, `msitools` (pulled in automatically by the package). Chromium is bundled; no system browser is needed.

### Connecting a hub

- **USB:** plug it in. The package installs a udev rule that gives the logged-in user access to LEGO hubs (vendor `0694`); no need to join `dialout`.
- **Bluetooth:** needs BlueZ (standard on desktop distros). SPIKE Prime / Essential with current firmware use Bluetooth Low Energy.

## Status

| Feature | Status |
|---|---|
| Home, lessons, choosing Prime / Essential | ✅ tested |
| Word Blocks / Icon Blocks / Python (Monaco) editors | ✅ tested |
| New, autosave, reopen from My Projects, open `.llsp3` by double-click | ✅ tested |
| Menu bar (File / Edit / View / Help) | ✅ tested — native Electron menu; Save as / Move to / Share enable only inside a project |
| Save as, export, delete, move to folder | implemented, not tested end-to-end |
| USB hub connection | ✅ tested against an emulated hub (pty): detect, connect, two-way SPIKE packets. **Not yet tested with a real hub.** |
| Bluetooth LE hub connection | ✅ tested end-to-end against a **simulated** SPIKE Prime on a virtual BlueZ adapter (scan, connect, handshake, sensor stream, program upload + run, console output, link loss), driven by the real web app UI; **not yet tested with a real hub** |
| Firmware update, sound recording | implemented, not tested |
| Classic Bluetooth (BTC, very old hub firmware) | ❌ not implemented |
| Gamepads | ❌ not implemented |

If you try it with a real hub, please open an issue with the result and `~/.config/SPIKE3/log.txt`.

## Files

| What | Where |
|---|---|
| Your projects | `~/Documents/LEGO Education SPIKE/` |
| Settings and log | `~/.config/SPIKE3/` (`log.txt`) |
| Downloaded lesson content | `~/.local/share/SPIKE3/` |
| Extracted app | `~/.local/share/spike3-linux/app/` |

To uninstall completely: remove the package, then delete the folders above you no longer need.

## Security model

The Python host serves the app on `127.0.0.1` and can read/write project files and talk to hubs, so it only answers the app's own window:

- every request must carry a per-launch random token (an HttpOnly cookie set by Electron; never in URLs, logs or the page's JavaScript);
- the `Host` and `Origin` headers must be the app's own, and cross-site requests are refused — other websites in your browser cannot reach it (no CORS, no DNS rebinding);
- *Save as* only writes to the exact path you picked in the file dialog; every other file path is confined to the project/content folders;
- the downloaded installer is SHA-256 pinned, and extraction refuses any entry that would land outside its target folder.

The Electron window runs with `contextIsolation`, `sandbox` and no Node integration. The preload exposes one function (reporting menu labels/state). Links that leave the app open in your normal browser.

Found a problem? Please report it privately via GitHub's *Report a vulnerability*.

## Build from source

```bash
sudo apt install nodejs npm python3-pip msitools rpm
packaging/build.sh            # -> dist/*.deb dist/*.rpm  (version from electron/package.json)
```

Run without packaging:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python host/extract.py SPIKE_APP_3_Win10__3.6.1_Global.msi    # -> ./app
cd electron && npm ci && npx electron .
```

### Layout

```
host/spike3.py        launcher: first-run download + verify + extract, then starts the host
host/extract.py       pulls the web app out of the MSI (~10k .NET manifest resources)
host/spike_host.py    the native layer: HTTP + WebSocket JSON-RPC, USB/BLE bridge, projects, content
electron/             window, native menu bar, single-instance + file open handling
packaging/            build script, deb/rpm post-install (sandbox, AppArmor, udev), container test
tests/                host security, concurrency, menu bar (real Chromium), serial bridge, extraction
```

### Tests

```bash
.venv/bin/python tests/test_host_security.py      # foreign sites / processes can't drive the host; Save as is confined
.venv/bin/python tests/test_concurrent_save.py    # concurrent saves never drop projects
.venv/bin/python tests/test_extract_traversal.py  # a tampered MSI can't write outside the target folder
.venv/bin/python tests/test_menubar.py            # menu bar with real clicks in Chromium (needs the extracted app)
.venv/bin/python tests/test_rpc_serial.py         # full USB path against an emulated hub
.venv/bin/python tests/test_ble_fake_hub.py       # full BLE path against a simulated hub (needs sudo: /dev/vhci)
.venv/bin/python tests/ui_ble_fake_hub.py         # same, but the real web app UI clicks Connect and Play
.venv/bin/python tests/test_shutdown.py           # host exits within seconds when the window closes
```

### Releases

Pushing a `v*` tag runs CI: tests → build `.deb` + `.rpm` → install, launch and check the blocks editor in clean Ubuntu 24.04, Debian 13 and Fedora 42 containers → publish the release with SHA-256 sums.

## License

Code in this repository: [MIT](LICENSE). Vendored `dnfile` and `bleak` keep their own MIT licenses.

"LEGO", "SPIKE" and the SPIKE app and content are trademarks and property of the LEGO Group. You obtain the app directly from LEGO; this project only makes it run on Linux.
