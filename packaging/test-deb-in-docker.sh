#!/usr/bin/env bash
# Runs inside a clean ubuntu:24.04 container: install the deb, extract from /msi/SPIKE.msi,
# start the host headless and check that the web app and RPC respond.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq /dist/*.deb curl >/dev/null
spike3 --version
useradd -m kid
su kid -c 'spike3 --setup /msi/SPIKE.msi'
su kid -c 'spike3 --headless --port 3046 > /tmp/host.log 2>&1 &'
for i in $(seq 30); do curl -sf -o /dev/null http://127.0.0.1:3046/ && break; sleep 1; done
curl -sf http://127.0.0.1:3046/ | grep -q 'Linux host shim' && echo "INDEX OK (shim injected)"
curl -sf -o /dev/null -w "js %{http_code} %{size_download}\n" http://127.0.0.1:3046/static/js/index.1e09a763.js
curl -sf -o /dev/null -w "blocks js %{http_code} %{size_download}\n" http://127.0.0.1:3046/static/js/scratch-vendor.c8a4a5e0.js
python3 - <<'EOF'
import asyncio, json, aiohttp
async def m():
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect("ws://127.0.0.1:3046")
        await ws.send_str(json.dumps({"id": "1", "method": "/configuration/request", "params": {"values": {}, "version": "18.0.1"}, "jsonrpc": "2.0"}))
        for _ in range(2):
            print("RPC", (await ws.receive()).data[:160])
asyncio.run(m())
EOF
du -sh /home/kid/.local/share/spike3-linux/app
ls -la /home/kid/.local/share/spike3-linux/
cat /tmp/host.log
apt-get remove -y -qq spike3-linux >/dev/null && echo REMOVE OK
