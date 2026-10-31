"""End-to-end test of the host's WS JSON-RPC + USB serial bridge using a pty pair as a fake hub.

Run: .venv/bin/python tests/test_rpc_serial.py
"""
import asyncio
import base64
import json
import os
import pty
import subprocess
import sys
import tempfile
import tty
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UDF  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 3099


async def main():
    master, slave = pty.openpty()
    tty.setraw(slave)
    slave_name = os.ttyname(slave)
    tmp = Path(tempfile.mkdtemp(prefix="spike-serial-"))
    env = dict(os.environ, SPIKE_FAKE_USB=f"{slave_name}:0x0694:0x0009", HOME=str(tmp),
               XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_DOCUMENTS_DIR=str(tmp / "Documents"))
    env.pop("SPIKE_SESSION_TOKEN", None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "host/spike_host.py"), "--headless", "--port", str(PORT)], env=env)
    try:
        state = tmp / "cfg" / UDF / "linux-host.json"
        for _ in range(100):
            if state.exists():
                break
            await asyncio.sleep(0.1)
        token = json.loads(state.read_text())["token"]
        await asyncio.sleep(0.5)
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"ws://127.0.0.1:{PORT}",
                                    headers={"Host": f"spike.localhost:{PORT}", "Cookie": f"spike_token={token}"})
            n = 0
            pending = {}
            notes = asyncio.Queue()

            async def reader():
                async for m in ws:
                    d = json.loads(m.data)
                    if "id" in d and d["id"] in pending:
                        pending.pop(d["id"]).set_result(d)
                    else:
                        await notes.put(d)
            asyncio.create_task(reader())

            async def call(method, values):
                nonlocal n
                n += 1
                f = asyncio.get_running_loop().create_future()
                pending[str(n)] = f
                await ws.send_str(json.dumps({"id": str(n), "method": method, "params": {"values": values, "version": "18.0.1"}, "jsonrpc": "2.0"}))
                return await asyncio.wait_for(f, 10)

            async def note(method, timeout=5):
                while True:
                    d = await asyncio.wait_for(notes.get(), timeout)
                    if d.get("method") == method:
                        return d

            r = await call("/configuration/request", {})
            assert "result" in r, r
            c = await note("/configuration/changed")
            assert c["params"]["configuration"]["host"]["hasUSB"] is True
            print("config ok:", c["params"]["configuration"]["host"]["platform"])

            await call("/hub/startscan", {"type": "usb"})
            df = await note("hub/devicefound")
            dev = df["params"]["device"]
            assert dev["id"] == slave_name and dev["serial"]["vendorId"] == 0x0694, dev
            print("devicefound ok:", dev)

            r = await call("/hub/connect", {"device": dev})
            assert r.get("result") == "Successfully connected", r
            await note("/hub/connected")
            print("connect ok")

            payload = bytes(range(1, 40))
            r = await call("/hub/send", {"id": dev["id"], "message": base64.b64encode(payload).decode()})
            assert r.get("result") == len(payload), r
            got = b""
            loop = asyncio.get_running_loop()
            while len(got) < len(payload):
                got += await loop.run_in_executor(None, os.read, master, 4096)
            assert got == payload, got
            print("web->hub ok", len(got), "bytes")

            os.write(master, b"\x01\x02hello-from-hub\x02")
            buf = b""
            while buf != b"\x01\x02hello-from-hub\x02":
                d = await note("/hub/didReceiveMessage")
                assert d["params"]["id"] == dev["id"]
                buf += base64.b64decode(d["params"]["message"])
            print("hub->web ok:", buf)

            r = await call("/hub/connections", {})
            assert r["result"]["devices"][0]["id"] == dev["id"]
            await call("/hub/disconnect", {"id": dev["id"]})
            await note("/hub/disconnected")
            print("disconnect ok")
            await ws.close()
        print("ALL PASS")
    finally:
        proc.terminate()
        proc.wait(5)


asyncio.run(main())
