#!/bin/sh
# electron-builder afterInstall (deb postinst / rpm %post). ${executable} etc. are templated by electron-builder.
set -e
APPDIR='/opt/${sanitizedProductName}'

# Chromium sandbox: Ubuntu 24.04+ restricts unprivileged user namespaces via AppArmor,
# so the setuid helper is what makes the sandbox work there. Keep it root:4755.
if [ -f "$APPDIR/chrome-sandbox" ]; then
  chown root:root "$APPDIR/chrome-sandbox" || true
  chmod 4755 "$APPDIR/chrome-sandbox" || true
fi

# AppArmor profile so the bundled Chromium may create user namespaces (same approach as Chrome/VS Code/Slack)
if [ -d /etc/apparmor.d ] && command -v apparmor_parser >/dev/null 2>&1; then
  cat > /etc/apparmor.d/spike3-linux <<EOF
abi <abi/4.0>,
include <tunables/global>

profile spike3-linux $APPDIR/spike3 flags=(unconfined) {
  userns,
  include if exists <local/spike3-linux>
}
EOF
  apparmor_parser -r -T -W /etc/apparmor.d/spike3-linux 2>/dev/null || true
fi

ln -sf "$APPDIR/spike3" /usr/bin/spike3

# LEGO hubs over USB: give the logged-in desktop user access (no dialout group needed)
mkdir -p /usr/lib/udev/rules.d
cat > /usr/lib/udev/rules.d/70-lego-spike.rules <<'EOF'
# LEGO Education hubs (SPIKE Prime/Essential, MINDSTORMS Robot Inventor) - installed by spike3-linux
SUBSYSTEM=="tty", ATTRS{idVendor}=="0694", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0694", TAG+="uaccess"
EOF
if command -v udevadm >/dev/null 2>&1; then
  udevadm control --reload-rules 2>/dev/null || true
  udevadm trigger --subsystem-match=tty 2>/dev/null || true
fi

command -v update-mime-database >/dev/null 2>&1 && update-mime-database /usr/share/mime >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
