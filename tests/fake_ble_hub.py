"""Fake SPIKE Prime hub over Bluetooth LE, on a virtual adapter (no hardware needed).

    sudo .venv/bin/python tests/fake_ble_hub.py [--log /tmp/fakehub.jsonl] [--drop-after SECONDS]

Creates two software Bluetooth controllers (bumble) joined by an in-memory radio link:
  * one is handed to the Linux kernel through /dev/vhci  -> BlueZ sees a new adapter (hciN)
  * the other runs the fake hub: advertises like a SPIKE Prime hub and serves its GATT service
So the host under test goes through the real BlueZ + bleak stack, exactly as with a real hub;
only the radio is simulated.

Protocol: https://lego.github.io/spike-prime-docs/ (COBS + XOR 0x03 framing, 0x02 delimiter).
Prints "ADAPTER hciN" once the kernel adapter is up. Every message received/sent is logged as JSON lines.
"""
import argparse
import asyncio
import json
import os
import struct
import subprocess
import sys
import time

from bumble.controller import Controller
from bumble.core import AdvertisingData, UUID
from bumble.device import Device
from bumble.gatt import Characteristic, CharacteristicValue, Service
from bumble.hci import Address
from bumble.link import LocalLink
from bumble.transport import open_transport

SERVICE = "0000FD02-0000-1000-8000-00805F9B34FB"
HUB_RX = "0000FD02-0001-1000-8000-00805F9B34FB"   # client writes here (write without response)
HUB_TX = "0000FD02-0002-1000-8000-00805F9B34FB"   # hub notifies here
LEGO_COMPANY_ID = 0x0397                           # 919, what the web app looks for
NAME = "Fake SPIKE Prime"
MAX_PACKET, MAX_MESSAGE, MAX_CHUNK = 509, 8192, 444


# ---------------------------------------------------------------- framing (official spec)
def cobs_encode(data: bytes) -> bytes:
    buf = bytearray()
    code_index = block = 0

    def begin():
        nonlocal code_index, block
        code_index = len(buf)
        buf.append(0xFF)
        block = 1

    begin()
    for b in data:
        if b > 2:
            buf.append(b)
            block += 1
        if b <= 2 or block > 84:
            if b <= 2:
                buf[code_index] = b * 84 + block + 2   # delimiter * MAX_BLOCK + block size + 3 - 1
            begin()
    buf[code_index] = block + 2
    return bytes(buf)


def cobs_decode(data: bytes) -> bytes:
    out = bytearray()

    def unescape(code):
        if code == 0xFF:
            return None, 84 + 1
        value, block = divmod(code - 3, 84)
        block += 1
        if block == 85:
            block = 1
            value -= 1
        return value, block

    value, block = unescape(data[0])
    for b in data[1:]:
        block -= 1
        if block > 0:
            out.append(b)
            continue
        if value is not None:
            out.append(value)
        value, block = unescape(b)
    return bytes(out)


def pack(msg: bytes) -> bytes:
    return bytes(b ^ 3 for b in cobs_encode(msg)) + b"\x02"


def unpack(frame: bytes) -> bytes:
    start = 1 if frame[0] == 0x01 else 0
    return cobs_decode(bytes(b ^ 3 for b in frame[start:-1]))


# ---------------------------------------------------------------- hub behaviour
class FakeHub:
    def __init__(self, device: Device, log_path: str):
        self.device = device
        self.log = open(log_path, "a", buffering=1)
        self.buf = bytearray()
        self.notify_task = None
        self.uploads = {}
        self.tx = Characteristic(HUB_TX, Characteristic.Properties.NOTIFY, Characteristic.READABLE, b"")
        self.rx = Characteristic(HUB_RX, Characteristic.Properties.WRITE_WITHOUT_RESPONSE | Characteristic.Properties.WRITE,
                                 Characteristic.WRITEABLE, CharacteristicValue(write=self.on_write))
        device.add_service(Service(SERVICE, [self.rx, self.tx]))

    def event(self, **kw):
        kw["t"] = round(time.time(), 3)
        self.log.write(json.dumps(kw) + "\n")

    def on_write(self, connection, value: bytes):
        self.buf += value
        while b"\x02" in self.buf:
            i = self.buf.index(b"\x02")
            frame, self.buf = bytes(self.buf[: i + 1]), self.buf[i + 1:]
            if len(frame) <= 1:
                continue
            try:
                msg = unpack(frame)
            except Exception as e:
                self.event(dir="in", error=f"bad frame: {e}", raw=frame.hex())
                continue
            self.event(dir="in", type=msg[0], hex=msg.hex())
            asyncio.get_running_loop().create_task(self.handle(msg))

    async def send(self, msg: bytes):
        self.event(dir="out", type=msg[0], hex=msg.hex())
        data = pack(msg)
        for i in range(0, len(data), MAX_PACKET):
            await self.device.notify_subscribers(self.tx, data[i:i + MAX_PACKET])

    async def handle(self, m: bytes):
        try:
            await self._handle(m)
        except Exception as e:   # never die silently: a real hub would answer, so make the failure visible
            self.event(dir="note", handler_error=f"{type(e).__name__}: {e}", type=m[0], hex=m.hex())

    async def _handle(self, m: bytes):
        t = m[0]
        if t == 0x00:      # InfoRequest -> InfoResponse (RPC 1.0.0, firmware 1.8.149 = what SPIKE 3.6.1 ships, Prime = 0x0000)
            await self.send(struct.pack("<BBBHBBHHHHH", 0x01, 1, 0, 0, 1, 8, 149, MAX_PACKET, MAX_MESSAGE, MAX_CHUNK, 0x0000))
        elif t == 0x18:    # GetHubNameRequest
            await self.send(bytes([0x19]) + NAME.encode().ljust(30, b"\0")[:30])
        elif t == 0x16:    # SetHubNameRequest
            await self.send(bytes([0x17, 0]))
        elif t == 0x1A:    # DeviceUuidRequest
            await self.send(bytes([0x1B]) + bytes(range(16)))
        elif t == 0x28:    # DeviceNotificationRequest(interval)
            (interval,) = struct.unpack_from("<H", m, 1)
            await self.send(bytes([0x29, 0]))
            if self.notify_task:
                self.notify_task.cancel()
            if interval:
                self.notify_task = asyncio.get_running_loop().create_task(self.notify_loop(interval / 1000))
        elif t == 0x46:    # ClearSlotRequest
            await self.send(bytes([0x47, 0]))
        elif t == 0x0C:    # StartFileUploadRequest(name\0, slot, crc32) - the app sends the name null-terminated, not padded
            end = m.index(0, 1)
            name = m[1:end].decode(errors="replace")
            slot, crc = struct.unpack_from("<BI", m, end + 1)
            self.uploads = {"name": name, "slot": slot, "crc": crc, "bytes": 0}
            self.event(dir="note", upload=name, slot=slot)
            await self.send(bytes([0x0D, 0]))
        elif t == 0x10:    # TransferChunkRequest(crc32, size, payload)
            _crc, size = struct.unpack_from("<IH", m, 1)
            self.uploads["bytes"] = self.uploads.get("bytes", 0) + size
            await self.send(bytes([0x11, 0]))
        elif t == 0x1E:    # ProgramFlowRequest(action, slot): 0 start, 1 stop
            action = m[1]
            await self.send(bytes([0x1F, 0]))
            await self.send(bytes([0x20, action]))
            if action == 0:
                await self.send(bytes([0x21]) + b"hello from fake hub\0")
        else:
            self.event(dir="note", unhandled=t)

    async def notify_loop(self, period):
        # DeviceNotification: battery 88 %, motor on port A at 90 degrees
        while True:
            payload = bytes([0x00, 88]) + struct.pack("<BBBhhbi", 0x0A, 0, 48, 90, 0, 0, 90)
            await self.send(struct.pack("<BH", 0x3C, len(payload)) + payload)
            await asyncio.sleep(max(period, 0.05))


def adapters():
    out = subprocess.run(["ls", "/sys/class/bluetooth"], capture_output=True, text=True).stdout.split()
    return {a for a in out if a.startswith("hci") and ":" not in a}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/tmp/fakehub.jsonl")
    ap.add_argument("--drop-after", type=float, default=0, help="hub drops the link N s after connecting")
    ap.add_argument("--button", type=int, default=0, help="advertised button state")
    args = ap.parse_args()
    open(args.log, "w").close()
    os.chmod(args.log, 0o666)

    before = adapters()
    link = LocalLink()
    vhci = await open_transport("vhci")
    kernel_ctrl = Controller("kernel", host_source=vhci.source, host_sink=vhci.sink, link=link)
    # bumble's controller only emits the legacy LE Connection Complete event. If it advertises
    # LL privacy / extended advertising, Linux waits for LE *Enhanced* Connection Complete instead,
    # times out after ~20 s and cancels -> BlueZ never gets to GATT. Present a plain 4.2-style controller.
    from bumble import hci as _hci
    kernel_ctrl.le_features = kernel_ctrl.le_features & ~(
        _hci.LeFeatureMask.LL_PRIVACY | _hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
        | _hci.LeFeatureMask.LE_PERIODIC_ADVERTISING)

    # bumble's LocalLink tags LE ACL data with the sender's *random* address, so the hub must
    # advertise/connect with a random (static) address or its replies never reach the central.
    hub_ctrl = Controller("hub", link=link)
    device = Device.with_hci(NAME, Address("F0:F1:F2:F3:F4:F5", Address.RANDOM_DEVICE_ADDRESS), hub_ctrl, hub_ctrl)
    hub = FakeHub(device, args.log)
    await device.power_on()

    @device.on("connection")
    def on_connection(conn):
        hub.event(dir="note", connected=str(conn.peer_address))

        @conn.on("disconnection")
        def _(reason):
            hub.event(dir="note", disconnected=reason)
            if hub.notify_task:
                hub.notify_task.cancel()
            asyncio.get_running_loop().create_task(advertise())

        if args.drop_after:
            async def drop():
                await asyncio.sleep(args.drop_after)
                hub.event(dir="note", dropping=True)
                await conn.disconnect()
            asyncio.get_running_loop().create_task(drop())

    async def advertise():
        # 31-byte legacy PDU: flags(3) + 128-bit service(18) + LEGO manufacturer data(10) = 31
        adv = bytes(AdvertisingData([
            (AdvertisingData.FLAGS, bytes([0x06])),
            (AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS, UUID(SERVICE).to_bytes()),
            (AdvertisingData.MANUFACTURER_SPECIFIC_DATA,
             struct.pack("<H", LEGO_COMPANY_ID) + bytes([1, 1, args.button, 0, 0, 0])),  # group=SPIKE, device=Prime
        ]))
        scan = bytes(AdvertisingData([(AdvertisingData.COMPLETE_LOCAL_NAME, NAME.encode())]))
        await device.start_advertising(advertising_data=adv, scan_response_data=scan, auto_restart=False)

    await advertise()
    for _ in range(50):
        new = adapters() - before
        if new:
            break
        await asyncio.sleep(0.1)
    name = sorted(new)[0] if new else "?"
    print(f"ADAPTER {name}", flush=True)
    hub.event(dir="note", adapter=name)
    await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
