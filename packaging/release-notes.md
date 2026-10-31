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
