#!/usr/bin/env bash
# Build Electron packages (.deb + .rpm) into dist/
#   packaging/build.sh [version]          version defaults to electron/package.json
# Needs: node/npm, python3 + pip, rpmbuild (for .rpm).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/electron"
VERSION="${1:-$(node -p 'require("./package.json").version')}"
VERSION="${VERSION#v}"

# one version everywhere
npm pkg set version="$VERSION" >/dev/null
sed -i "s/^VERSION = .*/VERSION = \"$VERSION\"/" "$ROOT/host/spike3.py"

# python deps the distros don't ship (both MIT, pure python): vendored into resources/host/vendor
rm -rf "$ROOT/host/vendor"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
python3 -m pip install -q --no-deps --target "$TMP" "dnfile==0.18.0" "bleak==1.1.1"
mkdir -p "$ROOT/host/vendor"
cp -r "$TMP/dnfile" "$TMP/bleak" "$ROOT/host/vendor/"
rm -rf "$ROOT"/host/vendor/bleak/backends/{corebluetooth,winrt,p4android,pythonista}
find "$ROOT/host/vendor" -name __pycache__ -prune -exec rm -rf {} +
for p in dnfile bleak; do
  cp "$TMP"/$p-*.dist-info/licenses/LICENSE* "$ROOT/host/vendor/$p.LICENSE" 2>/dev/null \
    || cp "$TMP"/$p-*.dist-info/LICENSE* "$ROOT/host/vendor/$p.LICENSE"
done

npm ci --no-audit --no-fund
rm -rf "$ROOT/dist"
npx electron-builder --linux deb rpm --x64 --publish never
ls -la "$ROOT"/dist/*.deb "$ROOT"/dist/*.rpm
