## What's new in 0.3.0

- **Menu bar is back**: File (New Project, Open, Save as, Move to, Share, Quit), Edit (Undo, Redo, Cut, Copy, Paste, Select All), View (Refresh, zoom, full screen), Help (App help, Settings, developer tools). Native menu with the same items and keyboard shortcuts as the Windows app; labels follow the app language; Save as / Move to / Share enable only inside a project.
- **Security fix (important):** in 0.1.x–0.2.0 any website open in your normal browser could talk to the local SPIKE service while the app was running — read your projects, write files into your home folder, and send commands to a connected hub. 0.3.0 only answers the app's own window (per-launch secret, origin/host checks, no CORS) and *Save as* can only write where you chose in the dialog. **Please update.**
- A local MSI given to `spike3 --setup` must match the pinned SHA-256, and extraction can no longer write outside its folder.
- Saving several projects at once no longer drops entries from *My Projects*.
- Less stutter: file reads/writes no longer block the hub connection.

## Install

**Ubuntu 24.04+ / Debian 13+**
```bash
sudo apt install ./spike3-linux_*_amd64.deb
```

**Fedora 41+**
```bash
sudo dnf install ./spike3-linux_*_x86_64.rpm
```

Then run `spike3` or find "LEGO Education SPIKE" in the app menu.

Chromium is bundled (Electron) — no system browser needed.

On first launch the official LEGO Education SPIKE 3.6.1 Windows installer (~1.4 GB) is downloaded from LEGO, SHA-256 verified and extracted to `~/.local/share/spike3-linux/` (needs ~4.5 GB free temporarily, ~1.4 GB after). No LEGO files are included in these packages.

USB hubs work without extra setup (udev `uaccess` rule). Bluetooth needs BlueZ.

Unofficial; not affiliated with the LEGO Group.
