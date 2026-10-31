#!/usr/bin/env bash
# Runs inside a clean container (Ubuntu/Debian or Fedora): install the package from /dist,
# optionally extract /msi/SPIKE.msi, start the host headless and check web app + RPC.
#   docker run --rm -v $PWD/dist:/dist:ro [-v dir_with_SPIKE.msi:/msi:ro] -v $PWD/packaging/test-pkg-in-docker.sh:/t.sh:ro IMAGE bash /t.sh
set -euxo pipefail
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq /dist/*.deb curl >/dev/null
  REMOVE="apt-get remove -y -qq spike3-linux"
else
  dnf -y -q install /dist/*.rpm curl shadow-utils >/dev/null
  REMOVE="dnf -y -q remove spike3-linux"
fi
spike3 --version
python3 -c 'import sys; sys.path.insert(0, "/usr/lib/spike3-linux/vendor"); import dnfile, aiohttp, serial, bleak; print("imports ok")'
test -f /usr/lib/udev/rules.d/70-lego-spike.rules
test -f /usr/share/applications/chrome-spike.localhost__-Default.desktop
test -f /usr/share/icons/hicolor/256x256/apps/lego-spike3.png
python3 -c "import sys; sys.path.insert(0, \"/usr/lib/spike3-linux/vendor\"); from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus; print(\"bluez backend ok\")"

if [ -f /msi/SPIKE.msi ]; then
  useradd -m kid
  su kid -c 'spike3 --setup /msi/SPIKE.msi' | tail -1
  su kid -c 'spike3 --headless --port 3046 > /tmp/host.log 2>&1 &'
  for i in $(seq 30); do curl -sf -o /dev/null http://127.0.0.1:3046/ && break; sleep 1; done
  curl -sf http://127.0.0.1:3046/ | grep -q 'Linux host shim' && echo "INDEX OK"
  curl -sf -o /dev/null -w "blocks js %{http_code} %{size_download}\n" http://127.0.0.1:3046/static/js/scratch-vendor.c8a4a5e0.js
  python3 - <<'EOF'
import asyncio, json, aiohttp
async def m():
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect("ws://127.0.0.1:3046")
        await ws.send_str(json.dumps({"id": "1", "method": "/configuration/request", "params": {"values": {}, "version": "18.0.1"}, "jsonrpc": "2.0"}))
        for _ in range(2):
            print("RPC", (await ws.receive()).data[:120])
asyncio.run(m())
EOF
fi
$REMOVE >/dev/null && echo REMOVE OK
