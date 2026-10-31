#!/usr/bin/env bash
# Install LEGO Education SPIKE 3 for Linux (per-user, no root needed except for apt deps).
#   ./install.sh [path/to/SPIKE_APP_3_Win10__3.6.1_Global.msi]
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local/share/spike3-linux}"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
MSI="${1:-}"
URL="https://assets.education.lego.com/_/downloads/SPIKE_APP_3_Win10__3.6.1_Global.msi"

need() { command -v "$1" >/dev/null 2>&1; }
if ! need msiextract; then
  echo "需要 msitools：sudo apt install msitools"; exit 1
fi
if ! need google-chrome && ! need google-chrome-stable && ! need chromium && ! need chromium-browser && ! need microsoft-edge; then
  echo "需要 Chrome / Chromium / Edge 其中之一"; exit 1
fi

mkdir -p "$PREFIX" "$BIN" "$APPS"
cp -r "$SRC/host" "$PREFIX/"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q -r "$SRC/requirements.txt"

if [ ! -f "$PREFIX/app/platform/web/index.html" ]; then
  if [ -z "$MSI" ]; then
    MSI="$PREFIX/SPIKE.msi"
    [ -f "$MSI" ] || { echo "下載官方安裝檔 (1.3 GB) ..."; curl -fL -o "$MSI.part" "$URL" && mv "$MSI.part" "$MSI"; }
  fi
  "$PREFIX/venv/bin/python" "$PREFIX/host/extract.py" "$MSI" "$PREFIX/app"
  [ "$MSI" = "$PREFIX/SPIKE.msi" ] && rm -f "$MSI"
fi

install -Dm644 "$SRC/packaging/icon.png" "$HOME/.local/share/icons/hicolor/256x256/apps/lego-spike3.png"

cat > "$BIN/spike3" <<EOF
#!/bin/sh
exec "$PREFIX/venv/bin/python" "$PREFIX/host/spike_host.py" "\$@"
EOF
chmod +x "$BIN/spike3"

cat > "$APPS/chrome-spike.localhost__-Default.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=LEGO Education SPIKE
Comment=SPIKE Prime / Essential
Exec=$BIN/spike3 %F
Icon=lego-spike3
Terminal=false
Categories=Education;Development;
MimeType=application/x-lego-spike3;application/x-lego-spike;
StartupWMClass=chrome-spike.localhost__-Default
EOF

mkdir -p "$HOME/.local/share/mime/packages"
cat > "$HOME/.local/share/mime/packages/lego-spike3.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/x-lego-spike3"><comment>SPIKE Project</comment><glob pattern="*.llsp3"/></mime-type>
  <mime-type type="application/x-lego-spike"><comment>SPIKE Legacy Project</comment><glob pattern="*.llsp"/></mime-type>
</mime-info>
EOF
update-mime-database "$HOME/.local/share/mime" >/dev/null 2>&1 || true
update-desktop-database "$APPS" >/dev/null 2>&1 || true
xdg-mime default chrome-spike.localhost__-Default.desktop application/x-lego-spike3 application/x-lego-spike 2>/dev/null || true

echo
echo "安裝完成：選單搜尋「LEGO Education SPIKE」或執行 spike3"
if ! id -nG | grep -qw dialout; then
  echo "USB 連 Hub 需要 dialout 權限：sudo usermod -aG dialout \$USER  (之後重新登入)"
fi
