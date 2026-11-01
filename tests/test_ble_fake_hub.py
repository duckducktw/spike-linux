"""Bluetooth LE end-to-end against a simulated SPIKE Prime hub (real BlueZ + bleak, virtual radio).

    sudo -v   # the fake hub needs /dev/vhci (root)
    .venv/bin/python tests/test_ble_fake_hub.py

Starts tests/fake_ble_hub.py as root (new virtual adapter hciN), then an isolated host with
SPIKE_BLE_ADAPTER=hciN, and drives the host over its WebSocket JSON-RPC exactly like the web app:
scan -> devicefound (with LEGO manufacturer data) -> connect -> InfoRequest/InfoResponse ->
hub name -> device notifications -> program start + console output -> large upload in chunks ->
disconnect; then a hub-side link drop must surface as /hub/disconnected.
"""
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UDF  # noqa: E402
from fake_ble_hub import pack, unpack, SERVICE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 3195
BT = {"serviceUUID": SERVICE.lower(),
      "transmitCharacteristicUUID": "0000fd02-0001-1000-8000-00805f9b34fb",
      "receiveCharacteristicUUID": "0000fd02-0002-1000-8000-00805f9b34fb",
      "writeMethod": "without_response", "desiredMtu": 512}
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def sudo(args, **kw):
    pw = Path.home() / "sudo.pwd"
    if pw.exists():
        return subprocess.Popen(["sudo", "-S", "-p", ""] + args, stdin=open(pw), **kw)
    return subprocess.Popen(["sudo", "-n"] + args, **kw)


def start_fake_hub(log, extra=()):
    out = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".out")
    p = sudo([sys.executable, str(ROOT / "tests/fake_ble_hub.py"), "--log", log, *extra], stdout=out, stderr=subprocess.STDOUT)
    for _ in range(100):
        txt = Path(out.name).read_text()
        if "ADAPTER " in txt:
            return p, txt.split("ADAPTER ")[1].split()[0]
        if p.poll() is not None:
            break
        time.sleep(0.1)
    raise SystemExit("fake hub did not start:\n" + Path(out.name).read_text())


def stop(p):
    # p is the sudo process; kill its python child as root, then sudo itself
    kids = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
    for k in kids:
        sudo(["kill", k]).wait()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        sudo(["kill", "-9", *kids]).wait()


class Rpc:
    def __init__(self, ws):
        self.ws, self.n, self.pending, self.notes = ws, 0, {}, asyncio.Queue()
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        async for m in self.ws:
            d = json.loads(m.data)
            if "id" in d and d["id"] in self.pending:
                self.pending.pop(d["id"]).set_result(d)
            elif "method" in d:
                await self.notes.put(d)

    async def call(self, method, params, timeout=60):
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[str(self.n)] = fut
        await self.ws.send_str(json.dumps({"id": str(self.n), "method": method, "params": params, "jsonrpc": "2.0"}))
        return await asyncio.wait_for(fut, timeout)

    async def note(self, method, timeout=20, pred=lambda d: True):
        end = time.time() + timeout
        while True:
            d = await asyncio.wait_for(self.notes.get(), max(0.1, end - time.time()))
            if d["method"] == method and pred(d):
                return d


class HubStream:
    """Reassembles SPIKE frames from /hub/didReceiveMessage chunks."""
    def __init__(self, rpc, hub_id):
        self.rpc, self.id, self.buf = rpc, hub_id, bytearray()

    async def msg(self, want_type=None, timeout=15):
        end = time.time() + timeout
        while True:
            while b"\x02" in self.buf:
                i = self.buf.index(b"\x02")
                frame, self.buf = bytes(self.buf[:i + 1]), self.buf[i + 1:]
                if len(frame) > 1:
                    m = unpack(frame)
                    if want_type is None or m[0] == want_type:
                        return m
            d = await self.rpc.note("/hub/didReceiveMessage", max(0.1, end - time.time()),
                                    lambda d: d["params"]["id"] == self.id)
            self.buf += base64.b64decode(d["params"]["message"])

    async def send(self, msg):
        r = await self.rpc.call("/hub/send", {"id": self.id, "message": base64.b64encode(pack(msg)).decode()})
        assert "result" in r, r


def hub_log(path):
    return [json.loads(line) for line in open(path) if line.strip()]


async def session(adapter, log, drop=False):
    tmp = Path(tempfile.mkdtemp(prefix="spike-ble-"))
    env = dict(os.environ, HOME=str(tmp), XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_DOCUMENTS_DIR=str(tmp / "Documents"),
               SPIKE_BLE_ADAPTER=adapter)
    env.pop("SPIKE_SESSION_TOKEN", None)
    host = subprocess.Popen([sys.executable, str(ROOT / "host/spike_host.py"), "--headless", "--port", str(PORT)],
                            env=env, stdout=subprocess.DEVNULL, stderr=open(tmp / "host.err", "w"))
    state = tmp / "cfg" / UDF / "linux-host.json"
    try:
        for _ in range(100):
            if state.exists():
                break
            await asyncio.sleep(0.1)
        token = json.loads(state.read_text())["token"]
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"ws://127.0.0.1:{PORT}/",
                                    headers={"Host": f"spike.localhost:{PORT}", "Cookie": f"spike_token={token}"},
                                    max_msg_size=0)
            rpc = Rpc(ws)
            # --- scan
            await rpc.call("/hub/startscan", {"type": "bluetooth-lowenergy", "bleService": BT})
            t0 = time.time()
            df = await rpc.note("hub/devicefound", 25,
                                lambda d: d["params"]["device"]["type"] == "bluetooth-lowenergy")
            dev = df["params"]["device"]
            if drop:
                await rpc.call("/hub/stopscan", {"type": "bluetooth-lowenergy"})
                r = await rpc.call("/hub/connect", {"device": dev, "bleService": BT})
                check("[drop] connect", "result" in r, r)
                d = await rpc.note("/hub/disconnected", 20)
                check("[drop] hub-side link loss reported as /hub/disconnected", d["params"]["id"] == dev["id"], d)
                r = await rpc.call("/hub/connections", {})
                check("[drop] connection list empty after loss", r["result"]["devices"] == [], r)
                return
            check(f"scan finds the hub ({time.time() - t0:.1f}s)", dev["id"].upper() == "F0:F1:F2:F3:F4:F5", dev)
            md = base64.b64decode(dev["ble"]["manufacturerData"].get("919", ""))
            check("advert carries LEGO manufacturer data (919 = SPIKE/Prime)", md[:2] == b"\x01\x01", dev["ble"])
            await rpc.call("/hub/stopscan", {"type": "bluetooth-lowenergy"})
            await rpc.note("/hub/scanningstopped", 5)

            # --- connect
            t0 = time.time()
            r = await rpc.call("/hub/connect", {"device": dev, "bleService": BT})
            check(f"connect ({time.time() - t0:.1f}s)", r.get("result") == "Successfully connected", r)
            await rpc.note("/hub/connected", 5)
            r = await rpc.call("/hub/connections", {})
            check("connection listed", [d["id"] for d in r["result"]["devices"]] == [dev["id"]], r)
            hub = HubStream(rpc, dev["id"])

            # --- handshake (what the web app does first)
            await hub.send(b"\x00")
            m = await hub.msg(0x01)
            rpc_maj, rpc_min, _, fw_maj, fw_min, fw_build, max_packet, max_msg, max_chunk, product = \
                struct.unpack_from("<BBHBBHHHHH", m, 1)
            check("InfoRequest -> InfoResponse", (fw_maj, fw_min, fw_build, max_chunk) == (1, 8, 149, 444), m.hex())

            await hub.send(b"\x18")
            m = await hub.msg(0x19)
            check("hub name over BLE", m[1:].split(b"\0")[0] == b"Fake SPIKE Prime", m)

            # --- streamed sensor data (hub -> app notifications)
            await hub.send(struct.pack("<BH", 0x28, 50))
            await hub.msg(0x29)
            got = [await hub.msg(0x3C) for _ in range(5)]
            battery = got[-1][4] if got[-1][3] == 0 else None
            check("device notifications stream (5 received)", len(got) == 5 and battery == 88, got[-1].hex())
            await hub.send(struct.pack("<BH", 0x28, 0))
            await hub.msg(0x29)

            # --- upload a program bigger than one BLE packet, in chunks, then run it
            program = (b"import motor\n" * 400)[:max_chunk * 3 + 17]
            await hub.send(b"\x46\x00")
            await hub.msg(0x47)
            await hub.send(b"\x0C" + b"program.py\0" + b"\x00" + struct.pack("<I", 0))   # same layout the web app sends
            m = await hub.msg(0x0D)
            ok = m[1] == 0
            for i in range(0, len(program), max_chunk):
                c = program[i:i + max_chunk]
                await hub.send(struct.pack("<BIH", 0x10, 0, len(c)) + c)
                ok &= (await hub.msg(0x11))[1] == 0
            check(f"upload {len(program)} bytes in {-(-len(program) // max_chunk)} chunks", ok)
            await hub.send(b"\x1E\x00\x00")
            await hub.msg(0x1F)
            m = await hub.msg(0x21)
            check("program start -> console output from hub", b"hello from fake hub" in m, m)

            # --- hub log agrees with what the host sent
            ev = hub_log(log)
            ins = [e for e in ev if e.get("dir") == "in"]
            up = sum(len(bytes.fromhex(e["hex"])) - 7 for e in ins if e["type"] == 0x10)
            check("hub received every chunk byte", up == len(program), (up, len(program)))
            check("no framing errors on the hub", not any("error" in e for e in ev))

            # --- disconnect
            await rpc.call("/hub/disconnect", {"id": dev["id"]})
            await rpc.note("/hub/disconnected", 10)
            r = await rpc.call("/hub/connections", {})
            check("disconnect", r["result"]["devices"] == [], r)
            await asyncio.sleep(1)
            check("hub saw the link go down", any("disconnected" in e for e in hub_log(log)))
            await ws.close()
    finally:
        host.terminate()
        host.wait(10)


async def main():
    if not Path("/dev/vhci").exists():
        print("SKIP: /dev/vhci not available (modprobe hci_vhci)")
        return
    log = tempfile.mktemp(suffix=".jsonl", prefix="fakehub-")
    p, adapter = start_fake_hub(log)
    print("fake hub on", adapter, flush=True)
    try:
        await asyncio.wait_for(session(adapter, log), 180)
    finally:
        stop(p)

    log2 = tempfile.mktemp(suffix=".jsonl", prefix="fakehub-")
    p, adapter = start_fake_hub(log2, ["--drop-after", "3"])
    try:
        await asyncio.wait_for(session(adapter, log2, drop=True), 120)
    finally:
        stop(p)
    print("\n" + (f"{len(FAILS)} failed" if FAILS else "ALL PASS"))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
