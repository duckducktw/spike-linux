#!/bin/sh
# electron-builder afterRemove (deb postrm / rpm %postun)
# rpm passes the number of remaining installs ($1 = 1 on upgrade); deb passes "upgrade" on upgrade.
case "$1" in
  1|upgrade|failed-upgrade) exit 0 ;;
esac
rm -f /usr/bin/spike3 /usr/lib/udev/rules.d/70-lego-spike.rules
if [ -f /etc/apparmor.d/spike3-linux ]; then
  command -v apparmor_parser >/dev/null 2>&1 && apparmor_parser -R /etc/apparmor.d/spike3-linux 2>/dev/null || true
  rm -f /etc/apparmor.d/spike3-linux
fi
command -v udevadm >/dev/null 2>&1 && udevadm control --reload-rules 2>/dev/null || true
command -v update-mime-database >/dev/null 2>&1 && update-mime-database /usr/share/mime >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
exit 0
