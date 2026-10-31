#!/usr/bin/env bash
# Build spike3-linux_<version>_all.deb
#   packaging/build-deb.sh [version]      -> dist/
# Contains only this project's code (+ vendored MIT-licensed dnfile). The LEGO web app is
# downloaded from LEGO and extracted per user on first launch.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/host/spike3.py")}"
VERSION="${VERSION#v}"
PKG=spike3-linux
OUT="$ROOT/dist"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
D="$BUILD/${PKG}_${VERSION}_all"

install -d "$D/DEBIAN" "$D/usr/lib/$PKG/vendor" "$D/usr/bin" \
  "$D/usr/share/applications" "$D/usr/share/mime/packages" "$D/usr/share/icons/hicolor/256x256/apps" \
  "$D/usr/share/doc/$PKG" "$D/usr/lib/udev/rules.d"

install -m644 "$ROOT"/host/spike_host.py "$ROOT"/host/extract.py "$D/usr/lib/$PKG/"
install -m755 "$ROOT"/host/spike3.py "$D/usr/lib/$PKG/"
sed -i "s/^VERSION = .*/VERSION = \"$VERSION\"/" "$D/usr/lib/$PKG/spike3.py"

# dnfile is not packaged in Debian/Ubuntu: vendor it (MIT)
python3 -m pip install -q --no-deps --target "$BUILD/vendor" "dnfile==0.18.0"
cp -r "$BUILD/vendor/dnfile" "$D/usr/lib/$PKG/vendor/"
cp "$BUILD"/vendor/dnfile-*.dist-info/licenses/LICENSE "$D/usr/share/doc/$PKG/dnfile.LICENSE" 2>/dev/null \
  || cp "$BUILD"/vendor/dnfile-*.dist-info/LICENSE* "$D/usr/share/doc/$PKG/dnfile.LICENSE"
find "$D/usr/lib/$PKG" -name __pycache__ -prune -exec rm -rf {} +

cat > "$D/usr/bin/spike3" <<EOF
#!/bin/sh
exec /usr/bin/python3 /usr/lib/$PKG/spike3.py "\$@"
EOF
chmod 755 "$D/usr/bin/spike3"

install -m644 "$ROOT/packaging/lego-spike3.desktop" "$D/usr/share/applications/"
install -m644 "$ROOT/packaging/lego-spike3-mime.xml" "$D/usr/share/mime/packages/lego-spike3.xml"
install -m644 "$ROOT/packaging/icon.png" "$D/usr/share/icons/hicolor/256x256/apps/lego-spike3.png"
install -m644 "$ROOT/packaging/70-lego-spike.rules" "$D/usr/lib/udev/rules.d/"
install -m644 "$ROOT/LICENSE" "$D/usr/share/doc/$PKG/copyright"
gzip -9n -c > "$D/usr/share/doc/$PKG/changelog.gz" <<EOF
$PKG ($VERSION) stable; urgency=medium

  * Release $VERSION. See https://github.com/duckducktw/spike-linux/releases

 -- spike-linux <noreply@github.com>  $(date -R)
EOF

SIZE=$(du -sk "$D" | cut -f1)
cat > "$D/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: education
Priority: optional
Architecture: all
Depends: python3 (>= 3.10), python3-aiohttp (>= 3.8), python3-serial, python3-bleak (>= 0.20), python3-pefile, msitools, zenity | kdialog
Recommends: chromium | chromium-browser | google-chrome-stable | microsoft-edge-stable | brave-browser, bluez
Installed-Size: $SIZE
Maintainer: spike-linux <noreply@github.com>
Homepage: https://github.com/duckducktw/spike-linux
Description: LEGO Education SPIKE 3 app for Linux (unofficial)
 Runs the official LEGO Education SPIKE 3 web app on Linux by reimplementing
 the Windows app's native host layer (USB/BLE hub bridge, project files,
 offline content) in Python. On first launch the official Windows installer
 is downloaded from LEGO, verified and extracted for the current user; no
 LEGO files are shipped in this package.
 .
 Not affiliated with or endorsed by the LEGO Group.
EOF

cat > "$D/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true
  command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
  command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
  if command -v udevadm >/dev/null; then udevadm control --reload-rules 2>/dev/null || true; udevadm trigger --subsystem-match=tty 2>/dev/null || true; fi
fi
EOF
cat > "$D/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
command -v update-mime-database >/dev/null && update-mime-database /usr/share/mime || true
command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
if [ "$1" = purge ]; then
  echo "spike3-linux: per-user data (~/.local/share/spike3-linux, ~/.config/SPIKE3) is left in place." >&2
fi
EOF
chmod 755 "$D/DEBIAN/postinst" "$D/DEBIAN/postrm"

mkdir -p "$OUT"
fakeroot dpkg-deb --build -Zxz "$D" "$OUT/" >/dev/null
echo "$OUT/${PKG}_${VERSION}_all.deb"
