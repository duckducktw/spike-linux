#!/usr/bin/env bash
# Build spike3-linux-<version>-1.noarch.rpm (needs rpmbuild; CI runs this in a Fedora container)
#   packaging/build-rpm.sh [version]      -> dist/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$ROOT/host/spike3.py")}"
VERSION="${VERSION#v}"
RPMVER="${VERSION//\~/.}"; RPMVER="${RPMVER//-/.}"   # rpm Version may not contain '-'
PKG=spike3-linux
DESKTOP=chrome-spike.localhost__-Default.desktop
TOP="$(mktemp -d)"
trap 'rm -rf "$TOP"' EXIT
mkdir -p "$TOP"/{SOURCES,SPECS,BUILD,RPMS,SRPMS} "$ROOT/dist"

STAGE="$TOP/SOURCES/$PKG-$RPMVER"
mkdir -p "$STAGE/vendor"
cp "$ROOT"/host/{spike_host.py,extract.py,spike3.py} "$STAGE/"
sed -i "s/^VERSION = .*/VERSION = \"$VERSION\"/" "$STAGE/spike3.py"
cp "$ROOT"/packaging/{"$DESKTOP",lego-spike3-mime.xml,icon.png,70-lego-spike.rules} "$ROOT/LICENSE" "$STAGE/"
# not packaged in Fedora: vendor dnfile + bleak (both MIT, pure python)
python3 -m pip install -q --no-deps --target "$TOP/vendor" "dnfile==0.18.0" "bleak==1.1.1"
cp -r "$TOP/vendor/dnfile" "$TOP/vendor/bleak" "$STAGE/vendor/"
cp "$TOP"/vendor/bleak-*.dist-info/licenses/LICENSE "$STAGE/bleak.LICENSE" 2>/dev/null \
  || cp "$TOP"/vendor/bleak-*.dist-info/LICENSE* "$STAGE/bleak.LICENSE"
rm -rf "$STAGE"/vendor/bleak/backends/{corebluetooth,winrt,p4android,pythonista}
cp "$TOP"/vendor/dnfile-*.dist-info/licenses/LICENSE "$STAGE/dnfile.LICENSE" 2>/dev/null \
  || cp "$TOP"/vendor/dnfile-*.dist-info/LICENSE* "$STAGE/dnfile.LICENSE"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
tar -C "$TOP/SOURCES" -czf "$TOP/SOURCES/$PKG-$RPMVER.tar.gz" "$PKG-$RPMVER"

cat > "$TOP/SPECS/$PKG.spec" <<EOF
Name:           $PKG
Version:        $RPMVER
Release:        1
Summary:        LEGO Education SPIKE 3 app for Linux (unofficial)
License:        MIT
URL:            https://github.com/duckducktw/spike-linux
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch
Requires:       python3 >= 3.10
Requires:       python3-aiohttp
Requires:       python3-pyserial
Requires:       python3-dbus-fast
Requires:       python3-pefile
Requires:       msitools
Requires:       (zenity or kdialog)
Recommends:     (chromium or google-chrome-stable or microsoft-edge-stable or brave-browser)
Recommends:     bluez
%global debug_package %{nil}
%global __brp_mangle_shebangs %{nil}
%global __requires_exclude_from ^/usr/lib/%{name}/.*$

%description
Runs the official LEGO Education SPIKE 3 web app on Linux by reimplementing
the Windows app's native host layer (USB/BLE hub bridge, project files,
offline content) in Python. On first launch the official Windows installer
is downloaded from LEGO, verified and extracted for the current user; no
LEGO files are shipped in this package.

Not affiliated with or endorsed by the LEGO Group.

%prep
%setup -q

%build

%install
install -d %{buildroot}/usr/lib/%{name}/vendor %{buildroot}%{_bindir}
install -m644 spike_host.py extract.py %{buildroot}/usr/lib/%{name}/
install -m755 spike3.py %{buildroot}/usr/lib/%{name}/
cp -r vendor/dnfile vendor/bleak %{buildroot}/usr/lib/%{name}/vendor/
printf '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/%{name}/spike3.py "\$@"\n' > %{buildroot}%{_bindir}/spike3
chmod 755 %{buildroot}%{_bindir}/spike3
install -Dm644 $DESKTOP %{buildroot}%{_datadir}/applications/$DESKTOP
install -Dm644 lego-spike3-mime.xml %{buildroot}%{_datadir}/mime/packages/lego-spike3.xml
install -Dm644 icon.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/lego-spike3.png
install -Dm644 70-lego-spike.rules %{buildroot}/usr/lib/udev/rules.d/70-lego-spike.rules

%post
update-mime-database %{_datadir}/mime >/dev/null 2>&1 || :
update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :
udevadm control --reload-rules >/dev/null 2>&1 || :
udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || :

%postun
update-mime-database %{_datadir}/mime >/dev/null 2>&1 || :
update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :

%files
%license LICENSE dnfile.LICENSE bleak.LICENSE
/usr/lib/%{name}
%{_bindir}/spike3
%{_datadir}/applications/$DESKTOP
%{_datadir}/mime/packages/lego-spike3.xml
%{_datadir}/icons/hicolor/256x256/apps/lego-spike3.png
/usr/lib/udev/rules.d/70-lego-spike.rules
EOF

rpmbuild --define "_topdir $TOP" -bb "$TOP/SPECS/$PKG.spec" >/dev/null
cp "$TOP"/RPMS/noarch/*.rpm "$ROOT/dist/"
ls "$ROOT"/dist/*.rpm
