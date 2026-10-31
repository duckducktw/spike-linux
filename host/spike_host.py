#!/usr/bin/env python3
"""LEGO Education SPIKE 3 - Linux host.

Re-implementation of the native layer of the Windows app
(Com.Lego.Flipper.Windows: WPF + CefSharp + Kestrel) so the unmodified
bundled web app runs on Linux:

  * HTTP: static web app + REST controllers (/projects, /files, /content, ...)
  * WebSocket JSON-RPC 2.0 ("flipper" wrapped params {values, version})
  * Hub transport: USB CDC serial (pyserial) and BLE (bleak / BlueZ)
  * Window: Chromium/Chrome in --app mode
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import json
import locale
import logging
import mimetypes
import os
import platform as pyplatform
import re
import secrets
import shutil
import signal
import socket
import string
import subprocess
import sys
import zipfile
from pathlib import Path

from aiohttp import web, WSMsgType, ClientSession

log = logging.getLogger("spike")

HERE = Path(__file__).resolve().parent
APP_ROOT = Path(os.environ.get("SPIKE_APP_ROOT", HERE.parent / "app")).resolve()
PLATFORM = APP_ROOT / "platform"
WEB = PLATFORM / "web"

HOSTNAME = "spike.localhost"  # Chrome resolves *.localhost to loopback; gives the window its own app id
HOST_VERSION = "3.6.1.22676"
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def documents_dir() -> Path:
    try:
        out = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True, timeout=3).stdout.strip()
        if out and out != str(Path.home()):
            return Path(out)
    except Exception:
        pass
    return Path.home() / "Documents"


def load_appconfig() -> dict:
    return json.loads((PLATFORM / "hosts" / "appconfig.json").read_text(encoding="utf-8"))


CFG = load_appconfig()
APP_NAME = CFG.get("name", "LEGO Education SPIKE")
USER_DATA = XDG_CONFIG / CFG.get("userDataFolder", "SPIKE3")
CONTENT_DIR = XDG_DATA / CFG.get("userDataFolder", "SPIKE3") / CFG.get("contentFolder", "Content")
PROJECT_DIR = documents_dir() / CFG.get("projectFolder", APP_NAME)
PROJECT_EXTS = [fa["ext"] for fa in CFG.get("fileAssociations", [])] or ["llsp3", "llsp"]
FULL_VERSION = CFG.get("fullVersion", "3.6.1")

mimetypes.add_type("text/plain; charset=utf-8", ".websrt")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("text/plain; charset=utf-8", ".py")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/json", ".json")


def iso(t: dt.datetime | None) -> str:
    t = t or dt.datetime(1, 1, 1, tzinfo=dt.timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.isoformat()


def parse_dt(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


def safe_join(base: Path, rel: str) -> Path | None:
    rel = rel.replace("\\", "/").lstrip("/")
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p


# --------------------------------------------------------------------------- dialogs
class Dialogs:
    """Native file dialogs via kdialog (KDE) or zenity (GTK)."""

    def __init__(self):
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
        self.tool = None
        order = ["kdialog", "zenity"] if "KDE" in de else ["zenity", "kdialog"]
        for t in order:
            if shutil.which(t):
                self.tool = t
                break

    async def _run(self, args):
        if not self.tool:
            log.warning("no kdialog/zenity available for file dialogs")
            return None
        proc = await asyncio.create_subprocess_exec(*args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return out.decode().strip() or None

    async def save(self, title, initial: Path, exts):
        if self.tool == "kdialog":
            flt = " ".join(f"*.{e}" for e in exts)
            r = await self._run(["kdialog", "--title", title, "--getsavefilename", str(initial), flt])
        else:
            args = ["zenity", "--file-selection", "--save", "--confirm-overwrite", "--title", title,
                    f"--filename={initial}"]
            args += [f"--file-filter={' '.join('*.' + e for e in exts)}"]
            r = await self._run(args)
        if r and exts and not any(r.lower().endswith("." + e) for e in exts):
            r += "." + exts[0]
        return r

    async def open(self, title, exts, multiple=False, start: Path | None = None):
        start = start or PROJECT_DIR
        if self.tool == "kdialog":
            flt = " ".join(f"*.{e}" for e in exts)
            args = ["kdialog", "--title", title, "--getopenfilename", str(start), flt]
            if multiple:
                args += ["--multiple", "--separate-output"]
            r = await self._run(args)
            return r.splitlines() if r else None
        args = ["zenity", "--file-selection", "--title", title, f"--filename={start}/",
                f"--file-filter={' '.join('*.' + e for e in exts)}"]
        if multiple:
            args += ["--multiple", "--separator=\n"]
        r = await self._run(args)
        return r.splitlines() if r else None

    async def folder(self, title, start: Path):
        if self.tool == "kdialog":
            return await self._run(["kdialog", "--title", title, "--getexistingdirectory", str(start)])
        return await self._run(["zenity", "--file-selection", "--directory", "--title", title, f"--filename={start}/"])

    async def message(self, title, text):
        if self.tool == "kdialog":
            await self._run(["kdialog", "--title", title, "--sorry", text])
        elif self.tool:
            await self._run(["zenity", "--warning", "--title", title, "--text", text])


DIALOGS = Dialogs()


async def clipboard_text() -> str:
    for cmd in (["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"], ["xsel", "-ob"]):
        if shutil.which(cmd[0]):
            try:
                p = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                out, _ = await asyncio.wait_for(p.communicate(), 3)
                if p.returncode == 0:
                    return out.decode("utf-8", "replace")
            except Exception:
                pass
    return ""


# --------------------------------------------------------------------------- settings
class Settings:
    def __init__(self):
        self.path = USER_DATA / "settings.json"
        try:
            self.values = json.loads(self.path.read_text())
        except Exception:
            self.values = {}
        self.values.setdefault("ui.language", self.default_language())
        self.save()

    @staticmethod
    def default_language():
        avail = CFG.get("availableLanguages", [])
        lang = (os.environ.get("LANGUAGE", "").split(":")[0] or os.environ.get("LC_ALL") or os.environ.get("LANG") or "")
        lang = lang.split(".")[0].replace("_", "-")
        cands = [lang]
        if lang.lower() in ("zh-tw", "zh-hk", "zh-mo"):
            cands.append("zh-CN")  # no zh-TW translation shipped; nearest is zh-CN
        if "-" in lang:
            base = lang.split("-")[0]
            cands += [a for a in avail if a.split("-")[0] == base]
        for c in cands:
            for a in avail:
                if a.lower() == c.lower():
                    return a
        return CFG.get("defaultLanguage", "en-US")

    def save(self):
        USER_DATA.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, indent=1))
        tmp.replace(self.path)

    def get(self, k, d=None):
        return self.values.get(k, d)

    def set_many(self, d):
        self.values.update(d)
        self.save()


# --------------------------------------------------------------------------- projects
class Projects:
    def __init__(self, server: "Host"):
        self.server = server
        self.list_path = USER_DATA / "availableProjects.json"
        self.projects: dict[str, dict] = {}  # id -> {path, name, created, lastsaved, meta}
        self.initial: list[str] = []

    # metadata ---------------------------------------------------------
    @staticmethod
    def load_meta(path: Path):
        meta = {"icon": None, "manifest": None, "monitors": None}
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                ln = n.lower()
                if ln == "icon.svg":
                    meta["icon"] = z.read(n).decode("utf-8", "replace")
                elif ln == "manifest.json":
                    meta["manifest"] = z.read(n).decode("utf-8-sig", "replace")
                elif ln == "monitors.json":
                    meta["monitors"] = z.read(n).decode("utf-8", "replace")
        return meta

    def _apply_manifest(self, rec):
        try:
            m = json.loads(rec["meta"]["manifest"] or "{}")
        except Exception:
            m = {}
        for k_src, k_dst in (("id", "id"), ("name", "name"), ("created", "created"), ("lastsaved", "lastsaved")):
            if k_src in m and m[k_src] is not None:
                rec[k_dst] = m[k_src]

    def initialize(self):
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        entries = []
        for p in (self.list_path, self.list_path.with_suffix(".json.tmp")):
            try:
                entries = list(json.loads(p.read_text()).values())
                break
            except Exception:
                continue
        for e in entries:
            path = Path(e.get("filePath", ""))
            if not path.is_file():
                continue
            try:
                meta = self.load_meta(path)
            except Exception:
                continue
            rec = {"id": e.get("id"), "name": e.get("name"), "path": str(path),
                   "created": e.get("created"), "lastsaved": e.get("lastSaved"), "meta": meta}
            if rec["id"]:
                self.projects[rec["id"]] = rec

    def save_list(self):
        out = {}
        i = 0
        for r in self.projects.values():
            if Path(r["path"]).is_file():
                out[f"I{i}"] = {"id": r["id"], "name": r["name"], "created": r.get("created"),
                                "lastSaved": r.get("lastsaved"), "filePath": r["path"]}
                i += 1
        USER_DATA.mkdir(parents=True, exist_ok=True)
        tmp = self.list_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=1))
        tmp.replace(self.list_path)

    def prune(self):
        for k in [k for k, r in self.projects.items() if not Path(r["path"]).is_file()]:
            self.projects.pop(k, None)

    def list_items(self):
        self.prune()
        out = []
        for r in self.projects.values():
            p = Path(r["path"])
            st = p.stat()
            created = parse_dt(r.get("created")) or dt.datetime.fromtimestamp(st.st_ctime, dt.timezone.utc)
            saved = parse_dt(r.get("lastsaved")) or dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc)
            out.append({"created": iso(created), "id": r["id"], "lastsaved": iso(saved),
                        "name": r["name"], "size": st.st_size})
        return out

    @staticmethod
    def clean_name(name):
        bad = set('/\0')
        return "".join(" " if c in bad else c for c in (name or "Project")).strip() or "Project"

    def unique_path(self, name, folder: Path | None = None) -> Path:
        folder = Path(folder or PROJECT_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        name = self.clean_name(name)
        ext = PROJECT_EXTS[0]
        p = folder / f"{name}.{ext}"
        n = 0
        while p.exists():
            p = folder / f"{name}-{n}.{ext}"
            n += 1
        return p

    def _do_save(self, rec, data: bytes):
        path = Path(rec["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        rec["meta"] = self.load_meta(path)
        self._apply_manifest(rec)
        self.projects[rec["id"]] = rec
        self.save_list()

    def save(self, key, name, data: bytes):
        rec = self.projects.get(key)
        if rec is None:
            rec = {"id": key, "name": name, "path": str(self.unique_path(name))}
        else:
            old = Path(rec["path"])
            folder = old.parent
            if old.exists():
                old.unlink()
            rec["path"] = str(self.unique_path(name, folder))
            rec["name"] = name
        self._do_save(rec, data)

    def save_as(self, key, path, data: bytes):
        deleted = None
        rp = str(Path(path).resolve())
        for k, r in list(self.projects.items()):
            if str(Path(r["path"]).resolve()) == rp:
                self.projects.pop(k)
                deleted = k
        rec = {"id": key, "name": Path(path).stem, "path": path}
        self._do_save(rec, data)
        return deleted

    def delete(self, key):
        rec = self.projects.pop(key, None)
        if rec and Path(rec["path"]).exists():
            Path(rec["path"]).unlink()
        self.save_list()

    def move_to(self, key, newdir):
        rec = self.projects.get(key)
        if not rec:
            return
        data = Path(rec["path"]).read_bytes()
        Path(rec["path"]).unlink(missing_ok=True)
        rec["path"] = str(self.unique_path(Path(rec["path"]).stem, Path(newdir)))
        self._do_save(rec, data)

    def manifest(self, key):
        r = self.projects.get(key)
        if not r:
            return None
        try:
            m = json.loads(r["meta"]["manifest"] or "{}")
        except Exception:
            m = {}
        m.update({"id": r["id"], "name": r["name"], "size": Path(r["path"]).stat().st_size})
        return json.dumps(m)

    def program(self, key) -> bytes | None:
        r = self.projects.get(key)
        if not r:
            return None
        with zipfile.ZipFile(r["path"]) as z:
            for n in z.namelist():
                if n.lower() in ("scratch.sb3", "projectbody.json"):
                    return z.read(n)
        return None

    def full_project(self, key) -> bytes | None:
        r = self.projects.get(key)
        if not r:
            return None
        buf = io.BytesIO()
        with zipfile.ZipFile(r["path"]) as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zout:
            for info in zin.infolist():
                data = zin.read(info)
                if info.filename == "manifest.json":
                    try:
                        m = json.loads(data.decode("utf-8-sig"))
                        m.update({"id": r["id"], "name": r["name"], "size": Path(r["path"]).stat().st_size})
                        data = json.dumps(m, indent=2).encode()
                    except Exception:
                        pass
                zout.writestr(Path(info.filename).name, data)
        return buf.getvalue()

    @staticmethod
    def nanoid():
        alpha = "_-" + string.digits + string.ascii_lowercase + string.ascii_uppercase
        return "".join(secrets.choice(alpha) for _ in range(12))

    def _name_from_file(self, path: Path):
        base = path.stem
        n = 1
        name = base
        while any(r["name"] == name for r in self.projects.values()):
            name = f"{base}-{n}"
            n += 1
        return name

    def add_file(self, path: str):
        p = Path(path)
        if not p.is_file() or p.suffix[1:].lower() not in PROJECT_EXTS:
            return None
        self.prune()
        try:
            meta = self.load_meta(p)
        except Exception as e:
            log.warning("cannot read project %s: %s", p, e)
            return None
        rec = {"id": None, "name": None, "path": str(p.resolve()), "meta": meta}
        self._apply_manifest(rec)
        if not rec["id"]:
            rec["id"] = self.nanoid()
        existing = self.projects.get(rec["id"])
        if existing and Path(existing["path"]).resolve() == p.resolve():
            pass
        elif existing and Path(existing["path"]).exists():
            rec["id"] = self.nanoid()
            rec["name"] = self._name_from_file(p)
            self.projects[rec["id"]] = rec
        else:
            rec["name"] = self._name_from_file(p)
            self.projects[rec["id"]] = rec
        self.save_list()
        return rec["id"]

    async def open_files(self, paths):
        ids = [i for i in (self.add_file(p) for p in paths) if i]
        if ids:
            await self.server.broadcast("/projects/opennotification", {"ids": ids})
        return ids


# --------------------------------------------------------------------------- hubs
class HubConnection:
    def __init__(self, host: "Host", info: dict):
        self.host = host
        self.info = info
        self.id = info["id"]
        self.disconnected = False

    async def connect(self, params) -> bool: ...
    async def send(self, data: bytes): ...
    async def close(self): ...

    async def on_data(self, data: bytes):
        if not self.disconnected:
            await self.host.broadcast("/hub/didReceiveMessage",
                                      {"id": self.id, "message": base64.b64encode(data).decode()})

    async def on_lost(self):
        if self.disconnected:
            return
        self.disconnected = True
        await self.host.hubs.connection_lost(self)


class SerialHub(HubConnection):
    async def connect(self, params):
        import serial
        err = None
        try:
            self.ser = serial.Serial(None, 115200, timeout=0, write_timeout=5)
            self.ser.port = self.id
            self.ser.dtr = True  # Windows host opens with DtrEnable/RtsEnable
            self.ser.rts = True
            self.ser.open()
        except Exception as e:
            err = e
            if "Inappropriate ioctl" in str(e):  # ptys (tests) have no modem lines
                try:
                    self.ser = serial.Serial(None, 115200, timeout=0, write_timeout=5)
                    self.ser.port = self.id
                    self.ser.open()
                    err = None
                except Exception as e2:
                    err = e2
        if err is not None:
            log.error("serial open %s failed: %s", self.id, err)
            if "Permission denied" in str(err):
                asyncio.create_task(DIALOGS.message(APP_NAME, f"無法開啟 {self.id}：權限不足。\n請把使用者加入 dialout 群組後重新登入：\nsudo usermod -aG dialout $USER"))
            return False
        loop = asyncio.get_running_loop()
        loop.add_reader(self.ser.fileno(), self._readable)
        return True

    def _readable(self):
        try:
            data = self.ser.read(self.ser.in_waiting or 1)
        except Exception as e:
            log.info("serial read failed: %s", e)
            self._drop_reader()
            asyncio.create_task(self.on_lost())
            return
        if data:
            asyncio.create_task(self.on_data(data))

    def _drop_reader(self):
        try:
            asyncio.get_running_loop().remove_reader(self.ser.fileno())
        except Exception:
            pass

    async def send(self, data):
        if self.disconnected:
            raise RuntimeError("SerialPort is not open")
        try:
            await asyncio.to_thread(self.ser.write, data)
        except Exception as e:
            await self.on_lost()
            raise RuntimeError(f"SerialPort send failed: {e}")

    async def close(self):
        self._drop_reader()
        try:
            self.ser.close()
        except Exception:
            pass
        await self.on_lost()


class BleHub(HubConnection):
    async def connect(self, params):
        from bleak import BleakClient
        svc = params.get("bleService") or {}
        self.rx_uuid = svc.get("receiveCharacteristicUUID")
        self.tx_uuid = svc.get("transmitCharacteristicUUID")
        self.with_response = svc.get("writeMethod", "with_response") == "with_response"
        dev = self.host.hubs.ble_devices.get(self.id, self.id)
        for attempt in range(3):
            try:
                self.client = BleakClient(dev, disconnected_callback=lambda c: asyncio.create_task(self.on_lost()), timeout=15)
                await self.client.connect()
                self.tx_char = self.client.services.get_characteristic(self.tx_uuid)
                await self.client.start_notify(self.rx_uuid, self._notify)
                return True
            except Exception as e:
                log.warning("BLE connect attempt %d failed: %s", attempt + 1, e)
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
        return False

    def _notify(self, _char, data: bytearray):
        asyncio.create_task(self.on_data(bytes(data)))

    async def send(self, data):
        if self.disconnected:
            raise RuntimeError("Device not connected")
        try:
            await self.client.write_gatt_char(self.tx_char or self.tx_uuid, data, response=self.with_response)
        except Exception as e:
            await self.on_lost()
            raise RuntimeError(f"BLE send failed: {e}")

    async def close(self):
        try:
            await self.client.disconnect()
        except Exception:
            pass
        await self.on_lost()


class Hubs:
    def __init__(self, host: "Host"):
        self.host = host
        self.active: dict[str, HubConnection] = {}
        self.scanning: dict[str, asyncio.Task] = {}
        self.ble_devices = {}
        self.ble_scanner = None

    @property
    def paused(self):
        return bool(self.active)

    async def found(self, dev):
        if self.paused and dev["type"] not in ("usb", "usb-hid"):
            return
        await self.host.broadcast("hub/devicefound", {"device": dev})

    # USB --------------------------------------------------------------
    async def _usb_loop(self):
        from serial.tools import list_ports
        fake = os.environ.get("SPIKE_FAKE_USB")  # test hook: "/dev/pts/N:0x0694:0x0009"
        while True:
            ports = [(p.device, p.vid, p.pid, p.description) for p in list_ports.comports()]
            if fake:
                dev, vid, pid = fake.split(":")
                ports.append((dev, int(vid, 0), int(pid, 0), "Fake LEGO Hub"))
            for device, vid, pid, desc in ports:
                if not vid:
                    continue
                await self.found({"id": device, "name": desc or os.path.basename(device),
                                  "type": "usb", "serial": {"vendorId": vid, "productId": pid}})
            await asyncio.sleep(1)

    # BLE --------------------------------------------------------------
    async def _ble_loop(self, params):
        from bleak import BleakScanner
        svc = (params.get("bleService") or {}).get("serviceUUID")

        def cb(device, adv):
            if svc and svc.lower() not in [u.lower() for u in adv.service_uuids]:
                return
            if adv.rssi is not None and adv.rssi < -90:
                return
            self.ble_devices[device.address] = device
            md = {str(k): base64.b64encode(bytes(v)).decode() for k, v in adv.manufacturer_data.items()}
            dev = {"id": device.address, "name": adv.local_name or device.name or "", "type": "bluetooth-lowenergy",
                   "ble": {"rssi": adv.rssi, "manufacturerData": md}}
            asyncio.create_task(self.found(dev))

        try:
            self.ble_scanner = BleakScanner(detection_callback=cb)
            await self.ble_scanner.start()
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("BLE scan failed: %s", e)
            await self.host.broadcast("/hub/scanningstopped", {"type": "bluetooth-lowenergy"})
            self.scanning.pop("bluetooth-lowenergy", None)
            asyncio.create_task(DIALOGS.message(APP_NAME, f"藍牙掃描失敗：{e}\n請確認藍牙已開啟（bluetoothctl power on）。"))
        finally:
            if self.ble_scanner:
                try:
                    await self.ble_scanner.stop()
                except Exception:
                    pass
                self.ble_scanner = None

    async def start_scan(self, params):
        t = params.get("type")
        if t in self.scanning:
            return
        if t == "usb":
            self.scanning[t] = asyncio.create_task(self._usb_loop())
        elif t == "bluetooth-lowenergy":
            self.scanning[t] = asyncio.create_task(self._ble_loop(params))
        else:
            log.info("scan type %s not supported on Linux", t)

    async def stop_scan(self, params):
        t = params.get("type")
        task = self.scanning.pop(t, None)
        if task:
            task.cancel()
            await self.host.broadcast("/hub/scanningstopped", {"type": t})

    async def connect(self, params):
        info = params["device"]
        if info["id"] in self.active:
            await self.host.broadcast("/hub/connected", {"id": info["id"]})
            return True
        cls = {"usb": SerialHub, "bluetooth-lowenergy": BleHub}.get(info.get("type"))
        if not cls:
            await self.host.broadcast("/hub/connectionfailed")
            return False
        conn = cls(self.host, info)
        try:
            ok = await asyncio.wait_for(conn.connect(params), 45)
        except Exception as e:
            log.error("connect failed: %s", e)
            ok = False
        if ok:
            self.active[info["id"]] = conn
            await self.host.broadcast("/hub/connected", {"id": info["id"]})
            return True
        await self.host.broadcast("/hub/connectionfailed")
        return False

    async def connection_lost(self, conn):
        if self.active.get(conn.id) is conn:
            self.active.pop(conn.id)
            await self.host.broadcast("/hub/disconnected", {"id": conn.id})

    async def disconnect(self, hub_id):
        c = self.active.get(hub_id)
        if c:
            await c.close()

    async def disconnect_all(self):
        for k in list(self.active):
            await self.disconnect(k)

    async def send(self, hub_id, data):
        c = self.active.get(hub_id)
        if not c:
            raise RuntimeError("Device not connected")
        await c.send(data)


# --------------------------------------------------------------------------- content
class Content:
    def __init__(self):
        CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    def p(self, *parts) -> Path:
        return CONTENT_DIR.joinpath(*[x.replace("\\", "/") for x in parts])

    async def write_tmp(self, tmp_id, rel, data):
        dst = safe_join(CONTENT_DIR, f"tmp/{tmp_id}/{rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)

    def write_resource(self, rel, text):
        dst = safe_join(CONTENT_DIR, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")

    def confirm(self, tmp_id):
        src = safe_join(CONTENT_DIR, f"tmp/{tmp_id}")
        if not src or not src.is_dir():
            return
        for root, dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            (CONTENT_DIR / rel).mkdir(parents=True, exist_ok=True)
            for f in files:
                os.replace(Path(root) / f, CONTENT_DIR / rel / f)
        shutil.rmtree(src, ignore_errors=True)

    def clean_tmp(self, tmp_id):
        src = safe_join(CONTENT_DIR, f"tmp/{tmp_id}")
        if src and src.is_dir():
            shutil.rmtree(src, ignore_errors=True)

    def delete_all(self):
        for c in CONTENT_DIR.iterdir():
            if c.is_dir():
                shutil.rmtree(c, ignore_errors=True)
            else:
                c.unlink(missing_ok=True)

    def delete(self, paths):
        for rel in paths or []:
            t = safe_join(CONTENT_DIR, rel)
            if not t:
                continue
            if t.is_file():
                t.unlink(missing_ok=True)
            elif t.is_dir():
                shutil.rmtree(t, ignore_errors=True)
        for root, dirs, files in os.walk(CONTENT_DIR, topdown=False):
            if Path(root) != CONTENT_DIR and not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass

    def resolve_web_content(self, path: str):
        """FlipperContentWebContentProvider: /content/<lang>/... with default-language fallback."""
        parts = [x for x in path.split("/") if x][1:]
        if not parts:
            return None, None
        lang = parts[0]
        f = safe_join(CONTENT_DIR, "/".join(parts))
        if not (f and f.is_file()):
            lang = CFG.get("defaultLanguage", "en-US")
            f = safe_join(CONTENT_DIR, "/".join([lang] + parts[1:]))
        if not (f and f.is_file()):
            return None, None
        if len(parts) == 2 and parts[1] == "manifest.json":
            try:
                m = json.loads(f.read_text(encoding="utf-8-sig"))
                for unit in m.get("units", []):
                    acts = unit.get("activities", [])
                    ok = all((CONTENT_DIR / lang / str(unit.get("id")) / "activities" / str(a.get("id")) / "manifest.json").is_file() for a in acts)
                    unit["unavailableOffline"] = bool(acts) and not ok
                return f, json.dumps(m).encode()
            except Exception:
                pass
        return f, None


# --------------------------------------------------------------------------- host
INJECT_JS = r"""
<script>
(function(){
  // assets.education.lego.cn is NXDOMAIN outside China but LEGO's remote config hands it to some regions (e.g. TW)
  const _fetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    try {
      if (typeof input === 'string' && input.indexOf('assets.education.lego.cn') >= 0) input = input.replace('assets.education.lego.cn', 'assets.education.lego.com');
      else if (input && input.url && input.url.indexOf('assets.education.lego.cn') >= 0) input = new Request(input.url.replace('assets.education.lego.cn', 'assets.education.lego.com'), input);
    } catch(e) {}
    return _fetch(input, init);
  };
  // Blockly only re-measures on window 'resize'. If the workspace was injected while its container
  // was still 0-sized (window not yet laid out / mapped), blocks stay invisible until the user resizes.
  // Watch every Blockly container and nudge a resize whenever its box changes.
  (function(){
    if (!window.ResizeObserver) return;
    let pending = false;
    const kick = () => { if (pending) return; pending = true;
      requestAnimationFrame(() => { pending = false; window.dispatchEvent(new Event('resize')); }); };
    const ro = new ResizeObserver(kick);
    const seen = new WeakSet();
    const scan = () => document.querySelectorAll('.injectionDiv, .blocklySvg').forEach(el => {
      const host = el.classList.contains('injectionDiv') ? el.parentElement || el : el.parentElement;
      if (host && !seen.has(host)) { seen.add(host); ro.observe(host); kick(); }
    });
    new MutationObserver(scan).observe(document.documentElement, {childList: true, subtree: true});
    document.addEventListener('visibilitychange', () => { if (!document.hidden) kick(); });
  })();
  // Linux host shim: file drop + native menu replacement (Windows uses CefSharp/WPF for these)
  document.addEventListener('dragover', function(ev){ ev.preventDefault(); }, true);
  document.addEventListener('drop', async function(ev){
    if (!ev.dataTransfer || !ev.dataTransfer.files || !ev.dataTransfer.files.length) return;
    ev.preventDefault(); ev.stopPropagation();
    for (const f of ev.dataTransfer.files) {
      if (!/\.(llsp3|llsp)$/i.test(f.name)) continue;
      const fd = new FormData(); fd.append('file', f, f.name);
      try { await fetch('/linux/drop', {method:'POST', body: fd}); } catch(e) {}
    }
  }, true);
  window.addEventListener('keydown', function(ev){
    if (!(ev.ctrlKey || ev.metaKey)) return;
    const k = ev.key.toLowerCase(); let item = null;
    if (k === 'o') item = 'openProject';
    else if (k === 'n' && ev.altKey) item = 'newProject';
    else if (k === 's' && ev.shiftKey) item = 'saveAs';
    if (item) { ev.preventDefault(); fetch('/linux/menu?item=' + item, {method:'POST'}); }
  }, true);
})();
</script>
"""


class Host:
    def __init__(self, port: int):
        self.port = port
        self.clients: set[web.WebSocketResponse] = set()
        self.settings = Settings()
        self.projects = Projects(self)
        self.hubs = Hubs(self)
        self.content = Content()
        self.region = (locale.getlocale()[0] or "en_US").split("_")[-1].upper() or "US"
        self.app = web.Application(client_max_size=128 * 1024 * 1024)
        r = self.app.router
        r.add_post("/v1/sentryEnabled", self.h_sentry)
        r.add_post("/activate", self.h_activate)
        r.add_post("/content/tmp/{tmp_id}/{path:.*}", self.h_tmp_save)
        r.add_post("/files/saveAs", self.h_files_saveas)
        r.add_get("/files/load", self.h_files_load)
        r.add_get(r"/files/bundled/dir{path:(/.*)?}", self.h_bundled_dir)
        r.add_get(r"/files/content/dir{path:(/.*)?}", self.h_content_dir)
        r.add_get("/files/bundled/file/{path:.*}", self.h_bundled_file)
        r.add_get("/files/content/file/{path:.*}", self.h_content_file)
        r.add_post("/projects/{key}", self.h_project_save)
        r.add_get("/projects/{key}", self.h_project_get)
        r.add_get("/projects/{key}/{file}", self.h_project_get)
        r.add_post("/linux/drop", self.h_drop)
        r.add_post("/linux/menu", self.h_menu)
        r.add_get("/debug", self.h_debug)
        r.add_route("*", "/{tail:.*}", self.h_static)
        self.app.middlewares.append(self.cors)

    # ------------------------------------------------------------------ urls
    @property
    def base(self):
        return f"http://{HOSTNAME}:{self.port}"

    def boot_url(self):
        from urllib.parse import urlencode
        q = {"appId": CFG.get("appId", "spike"), "debugUrl": f"{self.base}/debug",
             "defaultLanguage": CFG.get("defaultLanguage", "en-US"), "fullVersion": FULL_VERSION,
             "sentryEnabled": "0", "webUrl": self.base, "wsUrl": f"ws://{HOSTNAME}:{self.port}"}
        return f"{self.base}/?{urlencode(q)}"

    @web.middleware
    async def cors(self, request, handler):
        if request.method == "OPTIONS":
            resp = web.Response()
        else:
            resp = await handler(request)
        if not isinstance(resp, web.WebSocketResponse) and not resp.prepared:
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Methods"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

    # ------------------------------------------------------------------ http
    async def h_debug(self, request):
        raise web.HTTPFound(self.boot_url())

    async def h_sentry(self, request):
        return web.Response()

    async def h_activate(self, request):
        argv = await request.json()
        await self.projects.open_files([a for a in argv if Path(a).suffix[1:].lower() in PROJECT_EXTS])
        return web.Response()

    async def h_drop(self, request):
        form = await request.post()
        f = form.get("file")
        if f is None:
            return web.Response(status=400)
        data = f.file.read()
        dst = PROJECT_DIR / Path(f.filename).name
        if dst.exists() and dst.read_bytes() != data:
            dst = self.projects.unique_path(Path(f.filename).stem)
            dst = dst.with_suffix(Path(f.filename).suffix)
        if not dst.exists():
            dst.write_bytes(data)
        await self.projects.open_files([str(dst)])
        return web.Response()

    async def h_menu(self, request):
        await self.broadcast("/menu/itemClicked", {"item": request.query.get("item")})
        return web.Response()

    async def h_tmp_save(self, request):
        try:
            form = await request.post()
            await self.content.write_tmp(request.match_info["tmp_id"], request.match_info["path"], form["file"].file.read())
            return web.Response()
        except Exception:
            log.exception("tmp save failed")
            return web.Response(status=500)

    async def h_files_saveas(self, request):
        form = await request.post()
        name = form.get("name") or "data"
        data = form["file"].file.read()
        path = await DIALOGS.save("Save As", documents_dir() / name, ["csv"])
        if path:
            Path(path).write_bytes(data)
        return web.Response()

    async def h_files_load(self, request):
        exts = [e.strip(". ") for e in request.query.get("exts", "").split(",") if e.strip()]
        sel = await DIALOGS.open("Open", exts or ["*"], False, documents_dir())
        if not sel:
            return web.json_response(None)
        p = sel[0]
        return web.json_response({"file": p, "content": Path(p).read_text(encoding="utf-8", errors="replace")})

    async def h_bundled_dir(self, request):
        d = safe_join(PLATFORM, request.match_info["path"])
        return web.json_response(sorted(os.listdir(d)) if d and d.is_dir() else [])

    async def h_content_dir(self, request):
        d = safe_join(CONTENT_DIR, request.match_info["path"])
        return web.json_response(sorted(os.listdir(d)) if d and d.is_dir() else [])

    async def h_bundled_file(self, request):
        f = safe_join(PLATFORM, request.match_info["path"])
        if f and f.is_file():
            return web.Response(text=f.read_text(encoding="utf-8", errors="replace"), content_type="text/plain")
        raise web.HTTPNotFound()

    async def h_content_file(self, request):
        f = safe_join(CONTENT_DIR, request.match_info["path"])
        if f and f.is_file():
            return web.Response(text=f.read_text(encoding="utf-8", errors="replace"), content_type="text/plain")
        raise web.HTTPNotFound()

    async def h_project_save(self, request):
        try:
            form = await request.post()
            key = request.match_info["key"]
            data = form["file"].file.read()
            path = form.get("path")
            if path:
                deleted = await asyncio.to_thread(self.projects.save_as, key, path, data)
                return web.json_response({"deletedProjectId": deleted})
            await asyncio.to_thread(self.projects.save, key, form.get("name") or "Project", data)
            return web.Response()
        except Exception:
            log.exception("project save failed")
            return web.Response(status=500)

    async def h_project_get(self, request):
        key = request.match_info["key"]
        file = request.match_info.get("file", "") or ""
        if key not in self.projects.projects:
            raise web.HTTPNotFound()
        if file.startswith("icon"):
            return web.Response(text=self.projects.projects[key]["meta"]["icon"] or "", content_type="image/svg+xml")
        if file.startswith("manifest"):
            return web.Response(text=self.projects.manifest(key) or "", content_type="application/json")
        if file.startswith("monitors"):
            return web.Response(text=self.projects.projects[key]["meta"]["monitors"] or "", content_type="application/json")
        if file.startswith("scratch") or file.startswith("projectbody"):
            return web.Response(body=self.projects.program(key) or b"", content_type="application/zip")
        return web.Response(body=self.projects.full_project(key) or b"", content_type="application/octet-stream")

    async def h_static(self, request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.ws_handler(request)
        path = request.match_info["tail"]
        path = "/" + path
        if path.endswith("/"):
            path += "index.html"
        override = None
        f = None
        if path.startswith("/content/"):
            f, override = self.content.resolve_web_content(path)
        if f is None:
            for base in (WEB, PLATFORM):
                c = safe_join(base, path)
                if c and c.is_file():
                    f = c
                    break
        if f is None:
            raise web.HTTPNotFound()
        ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        if override is not None:
            return web.Response(body=override, content_type="application/json")
        if f == WEB / "index.html":
            html = f.read_text(encoding="utf-8").replace("</head>", INJECT_JS + "</head>", 1)
            return web.Response(text=html, content_type="text/html")
        resp = web.FileResponse(f)
        resp.content_type = ctype.split(";")[0]
        return resp

    # ------------------------------------------------------------------ websocket rpc
    async def ws_handler(self, request):
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=None)
        await ws.prepare(request)
        self.clients.add(ws)
        log.info("web connected (%d clients)", len(self.clients))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    asyncio.create_task(self.handle_rpc(ws, msg.data))
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            log.info("web disconnected (%d clients)", len(self.clients))
            if not self.clients:
                await self.hubs.disconnect_all()
        return ws

    async def _send(self, ws, obj):
        if ws.closed:
            return
        try:
            await ws.send_str(json.dumps(obj, separators=(",", ":")))
        except Exception:
            pass

    async def broadcast(self, method, params=None):
        obj = {"method": method, "params": params, "jsonrpc": "2.0"}
        await asyncio.gather(*(self._send(ws, obj) for ws in list(self.clients)))

    async def handle_rpc(self, ws, raw):
        try:
            pkt = json.loads(raw)
        except Exception:
            return
        method = pkt.get("method")
        rid = pkt.get("id")
        if method is None:
            return  # responses to host->web requests: none issued
        params = pkt.get("params")
        values = params.get("values") if isinstance(params, dict) and "values" in params else params
        args = values if isinstance(values, list) else [values]
        a0 = args[0] if args else None
        a0 = a0 if isinstance(a0, dict) else {}

        async def ok(result=None):
            if rid is not None:
                await self._send(ws, {"id": rid, "result": result if result is not None else {}, "jsonrpc": "2.0"})

        async def err(message):
            if rid is not None:
                await self._send(ws, {"id": rid, "error": {"code": 0, "message": message}, "jsonrpc": "2.0"})

        try:
            h = self.rpc_table().get(method)
            if h is None:
                log.debug("unhandled rpc %s", method)
                return await err("Not implemented")
            await h(a0, ok, err, ws)
        except Exception as e:
            log.exception("rpc %s failed", method)
            await err(str(e))

    def host_config(self):
        return {
            "currentLanguage": self.settings.get("ui.language", CFG.get("defaultLanguage")),
            "deviceModelDescription": f"{pyplatform.node()} (linux)",
            "deviceType": "desktop",
            "hasUSB": True,
            "hostVersion": ".".join(HOST_VERSION.split(".")[:3]),
            "hostBuild": HOST_VERSION.split(".")[-1],
            "operatingSystemDescription": f"{pyplatform.system()} {pyplatform.release()}",
            "platform": "win10i",
            "supportsReleaseChannels": False,
            "region": self.region,
        }

    def rpc_table(self):
        H = self
        S = {}

        def reg(name):
            def d(fn):
                S[name] = fn
                return fn
            return d

        @reg("/configuration/request")
        async def _(a, ok, err, ws):
            await ok({})
            await H._send(ws, {"method": "/configuration/changed", "jsonrpc": "2.0", "params": {"configuration": {
                "host": H.host_config(),
                "userSettings": {k: {"value": v} for k, v in H.settings.values.items()}}}})

        @reg("/settings/save")
        async def _(a, ok, err, ws):
            vals = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (a.get("settings") or {}).items()}
            H.settings.set_many(vals)
            await ok({})

        @reg("/ping")
        async def _(a, ok, err, ws):
            await ok({})

        # content
        @reg("/content/cleantemp")
        async def _(a, ok, err, ws):
            await asyncio.to_thread(H.content.clean_tmp, a.get("tmpId"))
            await ok({})

        @reg("/content/confirmdownloadresource")
        async def _(a, ok, err, ws):
            await asyncio.to_thread(H.content.confirm, a.get("tmpId"))
            await ok({})

        @reg("/content/deleteresources")
        async def _(a, ok, err, ws):
            await asyncio.to_thread(H.content.delete, a.get("localPaths"))
            await ok({})

        @reg("/content/writeresource")
        async def _(a, ok, err, ws):
            await asyncio.to_thread(H.content.write_resource, a.get("localPath"), a.get("resource") or "")
            await ok({})

        @reg("/content/openlink")
        async def _(a, ok, err, ws):
            link = a.get("link")
            if link and re.match(r"^(https?|mailto):", link):
                subprocess.Popen(["xdg-open", link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            await ok({})

        @reg("/content/deletecontent")
        async def _(a, ok, err, ws):
            await asyncio.to_thread(H.content.delete_all)
            await H._send(ws, {"method": "/content/contentdeleted", "params": None, "jsonrpc": "2.0"})
            await ok({})

        # hub
        @reg("/hub/connect")
        async def _(a, ok, err, ws):
            if await H.hubs.connect(a):
                await ok("Successfully connected")
            else:
                await err("Connection failed")

        @reg("/hub/disconnect")
        async def _(a, ok, err, ws):
            await H.hubs.disconnect(a.get("id"))
            await ok({})

        @reg("/hub/send")
        async def _(a, ok, err, ws):
            hid = a.get("id")
            try:
                data = base64.b64decode(a.get("message") or "", validate=True)
            except Exception:
                return await err(f"Hub send failed ({hid}): invalid payload")
            try:
                await H.hubs.send(hid, data)
                await ok(len(data))
            except Exception as e:
                await err(f"Hub send failed ({hid}): {e}")

        @reg("/hub/connections")
        async def _(a, ok, err, ws):
            await ok({"devices": [c.info for c in H.hubs.active.values()]})

        @reg("/hub/startscan")
        async def _(a, ok, err, ws):
            await H.hubs.start_scan(a)
            await ok({})

        @reg("/hub/stopscan")
        async def _(a, ok, err, ws):
            await H.hubs.stop_scan(a)
            await ok({})

        # menu (no native menubar on Linux; acknowledge)
        for m in ("/menu/showProjectItems", "/menu/hideProjectItems", "/menu/enableInitialItems"):
            S[m] = (lambda a, ok, err, ws: ok({}))

        # projects
        @reg("/projects/initialprojects")
        async def _(a, ok, err, ws):
            await ok({"projectIds": H.projects.initial})

        @reg("/projects/list")
        async def _(a, ok, err, ws):
            await ok({"projects": await asyncio.to_thread(H.projects.list_items)})

        @reg("/projects/move")
        async def _(a, ok, err, ws):
            pid = a.get("projectId")
            if not pid:
                return await err("No project id")
            await ok({})
            rec = H.projects.projects.get(pid)
            if rec:
                d = await DIALOGS.folder("Move To", Path(rec["path"]).parent)
                if d:
                    await asyncio.to_thread(H.projects.move_to, pid, d)

        @reg("/projects/share")
        async def _(a, ok, err, ws):
            pid = a.get("projectId")
            if not pid:
                return await err("No project id")
            await ok({})
            rec = H.projects.projects.get(pid)
            if rec:  # no share sheet on Linux: reveal the file in the file manager
                subprocess.Popen(["xdg-open", str(Path(rec["path"]).parent)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        @reg("/projects/saveAs")
        async def _(a, ok, err, ws):
            pid = a.get("projectId")
            if not pid:
                return await err("No project id")
            rec = H.projects.projects.get(pid)
            init = Path(rec["path"]) if rec else PROJECT_DIR / "Project.llsp3"
            path = await DIALOGS.save("Save As", init, [PROJECT_EXTS[0]])
            await ok({"filePath": path})

        @reg("/projects/delete")
        async def _(a, ok, err, ws):
            pid = a.get("projectId")
            if not pid:
                return await err("No project id")
            await asyncio.to_thread(H.projects.delete, pid)
            await ok({})

        @reg("/projects/showOpenDialog")
        async def _(a, ok, err, ws):
            await ok({})
            sel = await DIALOGS.open("Open", PROJECT_EXTS, True, PROJECT_DIR)
            if sel:
                await H.projects.open_files(sel)

        @reg("/python/triggerpaste")
        async def _(a, ok, err, ws):
            await H._send(ws, {"method": "/python/paste", "params": {"text": await clipboard_text()}, "jsonrpc": "2.0"})

        @reg("/navigation/request")
        async def _(a, ok, err, ws):
            await ok({})

        @reg("/misc/requestrating")
        async def _(a, ok, err, ws):
            await ok(None)

        return S


# --------------------------------------------------------------------------- single instance + launch
STATE = USER_DATA / "linux-host.json"


def find_browser():
    env = os.environ.get("SPIKE_BROWSER")
    if env:
        return env
    for b in ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave-browser", "microsoft-edge"):
        p = shutil.which(b)
        if p:
            return p
    snap = "/snap/bin/chromium"
    return snap if os.path.exists(snap) else None


async def try_activate(files) -> bool:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        return False
    try:
        async with ClientSession() as s:
            async with s.post(f"http://127.0.0.1:{st['port']}/activate", json=[str(Path(f).resolve()) for f in files], timeout=3) as r:
                return r.status == 200
    except Exception:
        return False


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


async def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--headless", action="store_true", help="server only, do not open a window")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--embedded", action="store_true",
                    help="run under the Electron shell: print 'READY <json>' on stdout, exit when stdin closes")
    args = ap.parse_args()
    if args.embedded:
        args.headless = True

    USER_DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(USER_DATA / "log.txt")])
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if not args.embedded and not args.headless and await try_activate(args.files):
        log.info("forwarded to running instance")
        return

    if not WEB.is_dir():
        sys.exit(f"web app not found at {WEB}; run extract.py first")

    port = args.port or (3046 if port_free(3046) else free_port())  # stable origin keeps web storage
    host = Host(port)
    host.projects.initialize()
    host.projects.initial = [i for i in (host.projects.add_file(f) for f in args.files) if i]

    runner = web.AppRunner(host.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    STATE.write_text(json.dumps({"port": port, "pid": os.getpid()}))
    log.info("SPIKE host %s listening on %s", HOST_VERSION, host.base)
    log.info("boot url: %s", host.boot_url())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    if args.embedded:
        print("READY " + json.dumps({"url": host.boot_url(), "base": host.base, "port": port}), flush=True)

        def parent_gone():  # Electron closed our stdin (exit or crash) -> shut down
            try:
                if not os.read(0, 4096):
                    loop.remove_reader(0)
                    stop.set()
            except OSError:
                loop.remove_reader(0)
                stop.set()
        loop.add_reader(0, parent_gone)

    browser_proc = None
    if not args.headless:
        b = find_browser()
        if not b:
            sys.exit("no Chromium-based browser found (set SPIKE_BROWSER)")
        prof = USER_DATA / "browser-profile"
        cmd = [b, f"--app={host.boot_url()}", f"--user-data-dir={prof}", "--class=lego-spike",
               "--no-first-run", "--no-default-browser-check", "--disable-features=HardwareMediaKeyHandling,Translate",
               "--autoplay-policy=no-user-gesture-required", "--disable-pinch", "--window-size=1440,900"]
        browser_proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        async def watch():
            await browser_proc.wait()
            # chrome may hand off to an existing process with the same profile and exit at once;
            # then the window lives on in that process -> keep serving while a page is connected
            for _ in range(30):
                if host.clients:
                    break
                await asyncio.sleep(1)
            while host.clients:
                await asyncio.sleep(1)
            stop.set()
        asyncio.create_task(watch())

    await stop.wait()
    await host.hubs.disconnect_all()
    if browser_proc and browser_proc.returncode is None:
        browser_proc.terminate()
    try:
        STATE.unlink()
    except Exception:
        pass
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
