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
    f = PLATFORM / "hosts" / "appconfig.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.exit(f"web app not found or incomplete at {APP_ROOT} ({e}); run `spike3 --setup`")


CFG = load_appconfig()
APP_NAME = CFG.get("name", "LEGO Education SPIKE")
USER_DATA = XDG_CONFIG / CFG.get("userDataFolder", "SPIKE3")
CONTENT_DIR = XDG_DATA / CFG.get("userDataFolder", "SPIKE3") / CFG.get("contentFolder", "Content")
PROJECT_DIR = documents_dir() / CFG.get("projectFolder", APP_NAME)
PROJECT_EXTS = [fa["ext"] for fa in CFG.get("fileAssociations", [])] or ["llsp3", "llsp"]
FULL_VERSION = CFG.get("fullVersion", "3.6.1")
# MenuBarItemEnum of the Windows host (values sent in /menu/itemClicked)
MENU_ITEMS = {"newProject", "openProject", "saveAs", "moveTo", "share", "settings", "help", "checkForUpdate"}

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
    rel = (rel or "").replace("\\", "/").lstrip("/")
    p = (base / rel).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p


class BadPath(ValueError):
    pass


def must_join(base: Path, rel: str) -> Path:
    p = safe_join(base, rel)
    if p is None:
        raise BadPath("invalid path")
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
        fake = os.environ.get("SPIKE_FAKE_DIALOG")
        if fake is not None:  # tests: every dialog answers with this path ("" = cancelled)
            return fake or None
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
        # every read-modify-write of self.projects + availableProjects.json goes through this lock
        self._lock = asyncio.Lock()

    async def locked(self, fn, *args):
        """Run a blocking project operation in a worker thread, one at a time."""
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

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

    def add_files(self, paths):
        return [i for i in (self.add_file(p) for p in paths) if i]

    def store_dropped(self, name: str, data: bytes) -> Path:
        """Copy a project dropped onto the window into the project folder (dedupe identical files)."""
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        dst = PROJECT_DIR / Path(name).name
        if dst.exists() and dst.read_bytes() != data:
            dst = self.unique_path(Path(name).stem).with_suffix(Path(name).suffix)
        if not dst.exists():
            dst.write_bytes(data)
        return dst

    async def open_files(self, paths):
        ids = await self.locked(self.add_files, list(paths))
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


def ble_adapter_kw() -> dict:
    """SPIKE_BLE_ADAPTER=hciN picks the BlueZ adapter (tests use a virtual one); default = system default."""
    a = os.environ.get("SPIKE_BLE_ADAPTER")
    return {"bluez": {"adapter": a}} if a else {}


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
                self.client = BleakClient(dev, disconnected_callback=lambda c: asyncio.create_task(self.on_lost()), timeout=15,
                                          **ble_adapter_kw())
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
            self.ble_scanner = BleakScanner(detection_callback=cb, **ble_adapter_kw())
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

    def write_tmp(self, tmp_id, rel, data):
        dst = must_join(CONTENT_DIR, f"tmp/{tmp_id}/{rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)

    def write_resource(self, rel, text):
        dst = must_join(CONTENT_DIR, rel)
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
    if (!(ev.ctrlKey || ev.metaKey) || !window.__spikeMenu) return;
    const k = ev.key.toLowerCase(); let item = null;
    if (k === 'o' && !ev.shiftKey && !ev.altKey) item = 'openProject';
    else if (k === 'n' && ev.altKey) item = 'newProject';
    else if (k === 's' && ev.shiftKey) item = 'saveAs';
    if (item && window.__spikeMenu.enabled(item)) { ev.preventDefault(); ev.stopPropagation(); window.__spikeMenu.click(item); }
  }, true);
})();
</script>
<script>
/* Linux host: application menu bar (File / Edit / View / Help).
   Windows draws this natively in WPF (UIMenuBuilder) and sends /menu/itemClicked to the web app;
   here the same items live in a thin HTML bar above the app and go through the same RPC. */
(function(){
  const ITEMS = {
    file: [['newProject','menu.file.new_project','Ctrl+Alt+N'], ['openProject','menu.file.open','Ctrl+O'], '-',
           ['saveAs','menu.file.save_as','Ctrl+Shift+S'], ['moveTo','menu.file.move_to',''], ['share','menu.file.share',''], '-',
           ['@quit','menu.mac_app.quit','Ctrl+Q']],
    edit: [['@undo','menu.edit.undo','Ctrl+Z'], ['@redo','menu.edit.redo','Ctrl+Shift+Z'], '-',
           ['@cut','menu.edit.cut','Ctrl+X'], ['@copy','menu.edit.copy','Ctrl+C'], ['@paste','menu.edit.paste','Ctrl+V'], '-',
           ['@selectAll','menu.edit.select_all','Ctrl+A']],
    view: [['@reload','menu.view.reload','Ctrl+R'], '-', ['@fullscreen','menu.view.fullscreen','F11']],
    help: [['help','menu.help.item',''], ['settings','menu.settings','']]
  };
  const ORDER = ['file','edit','view','help'];
  const PROJECT_ITEMS = ['saveAs','moveTo','share'];
  let labels = {}, state = {initial: true, project: false};
  let ws = null, seq = 0, pending = {};
  const BASE = location.origin.replace(/^http/, 'ws');
  const tr = (k) => {
    const v = (labels[k] || (/\./.test(k) ? k.split('.').pop().replace(/_/g,' ') : k));
    return v.replace('{{name}}', 'SPIKE').replace(/\.{3,}|…+/g, '…');
  };
  function rpc(method, params){
    return new Promise((res) => {
      if (!ws || ws.readyState !== 1) return res(null);
      const id = 'm' + (++seq); pending[id] = res;
      ws.send(JSON.stringify({jsonrpc:'2.0', id, method, params: params || {}}));
      setTimeout(() => { if (pending[id]) { delete pending[id]; res(null); } }, 5000);
    });
  }
  function connect(){
    ws = new WebSocket(BASE + '/');
    ws.onopen = async () => { const r = await rpc('/linux/menuInfo'); if (r) { labels = r.labels || {}; state = r.state || state; render(); } };
    ws.onmessage = (m) => {
      let d; try { d = JSON.parse(m.data); } catch(e) { return; }
      if (d.id && pending[d.id]) { const f = pending[d.id]; delete pending[d.id]; f(d.result || null); return; }
      if (d.method === '/linux/menuState') { state = d.params || state; render(); }
      else if (d.method === '/linux/menuLabels') { labels = d.params || labels; render(); }
    };
    ws.onclose = () => setTimeout(connect, 1500);
  }
  function enabled(id){
    if (PROJECT_ITEMS.includes(id)) return !!state.project;
    return true;
  }
  // Electron: the bar is a real native menu (electron/main.js); this page only reports labels + state.
  const shell = (window.spikeShell && window.spikeShell.setMenu) ? window.spikeShell : null;
  function local(id){
    const ae = document.activeElement;
    switch (id) {
      case '@undo': case '@redo': case '@cut': case '@copy': case '@selectAll':
        if (ae && ae.focus) ae.focus();
        return document.execCommand(id === '@selectAll' ? 'selectAll' : id.slice(1));
      case '@paste':
        if (navigator.clipboard && navigator.clipboard.readText)
          return navigator.clipboard.readText().then(t => { if (ae && ae.focus) ae.focus(); document.execCommand('insertText', false, t); }).catch(() => {});
        return;
      case '@reload': return location.reload();
      case '@fullscreen':
        return document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
      case '@quit': return window.close();
    }
  }
  function click(id){
    close();
    if (id.startsWith('@')) return local(id);
    if (!enabled(id)) return;
    rpc('/linux/menu', {item: id});
  }
  window.__spikeMenu = {click, enabled};

  let bar = null, openMenu = null;
  function close(){
    if (openMenu) { openMenu.remove(); openMenu = null; }
    bar && bar.querySelectorAll('.lsm-top').forEach(b => b.classList.remove('lsm-open'));
  }
  function open(key, btn){
    close();
    btn.classList.add('lsm-open');
    const m = document.createElement('div'); m.className = 'lsm-pop'; m.setAttribute('role','menu');
    for (const it of ITEMS[key]) {
      if (it === '-') { const s = document.createElement('div'); s.className = 'lsm-sep'; m.appendChild(s); continue; }
      const [id, label, acc] = it;
      const b = document.createElement('button'); b.type = 'button'; b.className = 'lsm-item'; b.setAttribute('role','menuitem');
      b.dataset.item = id;
      b.disabled = !enabled(id);
      const l = document.createElement('span'); l.textContent = tr(label);
      const a = document.createElement('span'); a.className = 'lsm-acc'; a.textContent = acc;
      b.append(l, a);
      // keep focus (and the text selection) in the app so Edit actions apply to it
      b.addEventListener('mousedown', e => e.preventDefault());
      b.addEventListener('click', e => { e.stopPropagation(); click(id); });
      m.appendChild(b);
    }
    const r = btn.getBoundingClientRect();
    m.style.left = r.left + 'px'; m.style.top = r.bottom + 'px';
    document.body.appendChild(m); openMenu = m;
  }
  function render(){
    if (shell) { shell.setMenu({labels, state}); return; }
    if (!document.body) return;
    if (!bar) {
      const css = document.createElement('style');
      css.textContent = `
        #linux-spike-menubar{position:fixed;top:0;left:0;right:0;height:26px;z-index:2147483646;display:flex;align-items:stretch;
          background:#f3f3f3;border-bottom:1px solid #d6d6d6;font:13px/1 system-ui,-apple-system,"Noto Sans",sans-serif;color:#222;user-select:none}
        #linux-spike-menubar .lsm-top{all:unset;padding:0 10px;display:flex;align-items:center;cursor:default}
        #linux-spike-menubar .lsm-top:hover,#linux-spike-menubar .lsm-top.lsm-open{background:#dcdcdc}
        .lsm-pop{position:fixed;z-index:2147483647;min-width:230px;background:#fff;border:1px solid #c8c8c8;border-radius:4px;
          box-shadow:0 4px 14px rgba(0,0,0,.18);padding:4px 0;font:13px/1 system-ui,-apple-system,"Noto Sans",sans-serif;color:#222}
        .lsm-item{all:unset;box-sizing:border-box;width:100%;display:flex;justify-content:space-between;gap:24px;padding:7px 14px;cursor:default}
        .lsm-item:hover:not(:disabled),.lsm-item:focus-visible{background:#e8f0fe}
        .lsm-item:disabled{color:#aaa}
        .lsm-acc{color:#888}
        .lsm-sep{height:1px;background:#e3e3e3;margin:4px 0}
        html.linux-spike-has-menubar #app{position:fixed!important;top:26px!important;left:0;right:0;bottom:0;height:auto!important}`;
      document.head.appendChild(css);
      bar = document.createElement('div'); bar.id = 'linux-spike-menubar'; bar.setAttribute('role','menubar');
      document.body.appendChild(bar);
      document.documentElement.classList.add('linux-spike-has-menubar');
      document.addEventListener('mousedown', e => { if (openMenu && !openMenu.contains(e.target) && !bar.contains(e.target)) close(); }, true);
      document.addEventListener('keydown', e => { if (e.key === 'Escape' && openMenu) { close(); e.stopPropagation(); } }, true);
      window.addEventListener('blur', close);
      window.addEventListener('resize', close);
    }
    bar.textContent = '';
    for (const key of ORDER) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'lsm-top'; b.dataset.menu = key;
      b.textContent = tr(key === 'file' ? 'menu.file.title' : key === 'edit' ? 'menu.edit.title' : key === 'view' ? 'menu.view.title' : 'menu.help.title');
      b.addEventListener('mousedown', e => { e.preventDefault(); openMenu && b.classList.contains('lsm-open') ? close() : open(key, b); });
      b.addEventListener('mouseenter', () => { if (openMenu && !b.classList.contains('lsm-open')) open(key, b); });
      bar.appendChild(b);
    }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => { render(); connect(); });
  else { render(); connect(); }
})();
</script>
"""


class Host:
    MAX_INFLIGHT_RPC = 256  # per WebSocket connection

    def __init__(self, port: int, token: str | None = None, embedded: bool = False):
        self.port = port
        self.token = token or None
        self.embedded = embedded
        self.allowed_hosts = {f"{HOSTNAME}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}
        self.saveas_approved: set[str] = set()   # paths the user picked in the native Save As dialog
        self.menu_state = {"project": False, "initial": False}
        self.clients: set[web.WebSocketResponse] = set()
        self.settings = Settings()
        self.projects = Projects(self)
        self.hubs = Hubs(self)
        self.content = Content()
        self.region = (locale.getlocale()[0] or "en_US").split("_")[-1].upper() or "US"
        self.app = web.Application(client_max_size=128 * 1024 * 1024, middlewares=[self.guard])
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
        r.add_get("/linux/login", self.h_login)
        r.add_get("/debug", self.h_debug)
        r.add_route("*", "/{tail:.*}", self.h_static)

    # ------------------------------------------------------------------ urls
    def inject_js(self) -> str:
        return INJECT_JS

    def menu_labels(self) -> dict:
        """Flattened menu.* strings from the Windows host's own translations (hosts/translations)."""
        lang = self.settings.get("ui.language") or CFG.get("defaultLanguage", "en-US")
        cache = getattr(self, "_label_cache", {})
        if lang in cache:
            return cache[lang]
        tdir = PLATFORM / "hosts" / "translations"
        cands = [tdir / f"hostapp.{lang}.json", *sorted(tdir.glob(f"hostapp.{lang.split('-')[0]}-*.json")),
                 tdir / "hostapp.en-US.json"]
        out = {}
        for f in cands:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

            def walk(o, p):
                if isinstance(o, dict):
                    for k, v in o.items():
                        walk(v, f"{p}.{k}" if p else k)
                elif isinstance(o, str):
                    out[p] = o
            walk(data.get("menu", {}), "menu")
            break
        cache[lang] = out
        self._label_cache = cache
        return out

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
    async def guard(self, request, handler):
        """Only the app's own window may talk to the host.

        The server listens on loopback, but any web page in the user's normal browser can still send
        requests to 127.0.0.1 / *.localhost, and other local users can connect directly. So:
          * Host header must be ours (defeats DNS rebinding),
          * a browser-supplied Origin must be ours, and Sec-Fetch-Site must not be cross-site
            (blocks cross-site fetch/form/img/WebSocket, which carry these headers),
          * when a session token is set (Electron), every request must carry it
            (cookie set by the shell, or X-Spike-Token for the shell's own calls).
        No CORS headers are ever granted: the web app is same-origin.
        """
        reason = None
        if request.headers.get("Host", "") not in self.allowed_hosts:
            reason = "host"
        elif "Origin" in request.headers and request.headers["Origin"] not in self.allowed_origins:
            reason = "origin"
        elif request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            reason = "cross-site"
        elif request.path == "/linux/login":
            return await handler(request)  # checks the one-time token itself
        elif self.token:
            got = request.headers.get("X-Spike-Token") or request.cookies.get("spike_token") or ""
            if not secrets.compare_digest(got.encode(), self.token.encode()):
                reason = "token"
        if reason:
            log.warning("rejected %s %s (%s)", request.method, request.path, reason)
            return web.Response(status=403, text="forbidden")
        if request.method == "OPTIONS":
            return web.Response(status=204)
        return await handler(request)

    # ------------------------------------------------------------------ http
    def login_url(self):
        return f"{self.base}/linux/login?t={self.token}"

    async def h_login(self, request):
        """Browser modes: exchange the token from the launch URL for an HttpOnly same-site cookie."""
        got = request.query.get("t", "")
        if not (self.token and secrets.compare_digest(got.encode(), self.token.encode())):
            return web.Response(status=403, text="forbidden")
        resp = web.HTTPFound(self.boot_url())
        resp.set_cookie("spike_token", self.token, httponly=True, samesite="Strict", path="/")
        raise resp

    async def h_debug(self, request):
        raise web.HTTPFound(self.boot_url())

    async def h_sentry(self, request):
        return web.Response()

    async def h_activate(self, request):
        argv = await request.json()
        if not isinstance(argv, list):
            return web.Response(status=400)
        files = [str(a) for a in argv if isinstance(a, str) and Path(a).suffix[1:].lower() in PROJECT_EXTS]
        await self.projects.open_files(files)
        return web.Response()

    async def h_drop(self, request):
        form = await request.post()
        f = form.get("file")
        if f is None or not getattr(f, "filename", None):
            return web.Response(status=400)
        name = Path(f.filename).name
        if Path(name).suffix[1:].lower() not in PROJECT_EXTS:
            return web.Response(status=400)
        data = await asyncio.to_thread(f.file.read)
        dst = await asyncio.to_thread(self.projects.store_dropped, name, data)
        await self.projects.open_files([str(dst)])
        return web.Response()

    async def h_menu(self, request):
        item = request.query.get("item")
        if item not in MENU_ITEMS:
            return web.Response(status=400)
        await self.menu_click(item)
        return web.Response()

    async def menu_click(self, item):
        await self.broadcast("/menu/itemClicked", {"item": item})

    async def h_tmp_save(self, request):
        try:
            form = await request.post()
            data = await asyncio.to_thread(form["file"].file.read)
            await asyncio.to_thread(self.content.write_tmp, request.match_info["tmp_id"], request.match_info["path"], data)
            return web.Response()
        except BadPath:
            return web.Response(status=400, text="invalid path")
        except Exception:
            log.exception("tmp save failed")
            return web.Response(status=500)

    async def h_files_saveas(self, request):
        form = await request.post()
        name = Path(str(form.get("name") or "data")).name
        data = await asyncio.to_thread(form["file"].file.read)
        path = await DIALOGS.save("Save As", documents_dir() / name, ["csv"])
        if path:
            await asyncio.to_thread(Path(path).write_bytes, data)
        return web.Response()

    async def h_files_load(self, request):
        exts = [e.strip(". ") for e in request.query.get("exts", "").split(",") if e.strip()]
        sel = await DIALOGS.open("Open", exts or ["*"], False, documents_dir())
        if not sel:
            return web.json_response(None)
        p = sel[0]
        text = await asyncio.to_thread(Path(p).read_text, encoding="utf-8", errors="replace")
        return web.json_response({"file": p, "content": text})

    @staticmethod
    async def _list_dir(base: Path, rel: str):
        d = safe_join(base, rel)
        items = await asyncio.to_thread(lambda: sorted(os.listdir(d)) if d and d.is_dir() else [])
        return web.json_response(items)

    @staticmethod
    async def _text_file(base: Path, rel: str):
        f = safe_join(base, rel)
        if not (f and f.is_file()):
            raise web.HTTPNotFound()
        resp = web.FileResponse(f)  # streamed (sendfile), not read into memory
        resp.content_type = "text/plain"
        resp.charset = "utf-8"
        return resp

    async def h_bundled_dir(self, request):
        return await self._list_dir(PLATFORM, request.match_info["path"])

    async def h_content_dir(self, request):
        return await self._list_dir(CONTENT_DIR, request.match_info["path"])

    async def h_bundled_file(self, request):
        return await self._text_file(PLATFORM, request.match_info["path"])

    async def h_content_file(self, request):
        return await self._text_file(CONTENT_DIR, request.match_info["path"])

    async def h_project_save(self, request):
        try:
            form = await request.post()
            key = request.match_info["key"]
            data = await asyncio.to_thread(form["file"].file.read)
            path = form.get("path")
            if path:
                # Save As: the path must be one the user just chose in the native dialog (/projects/saveAs)
                path = str(Path(path).resolve())
                if path not in self.saveas_approved or Path(path).suffix[1:].lower() not in PROJECT_EXTS:
                    log.warning("save-as to unapproved path refused: %s", path)
                    return web.Response(status=403, text="path not chosen by user")
                self.saveas_approved.discard(path)
                deleted = await self.projects.locked(self.projects.save_as, key, path, data)
                return web.json_response({"deletedProjectId": deleted})
            await self.projects.locked(self.projects.save, key, form.get("name") or "Project", data)
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
            return web.Response(body=await asyncio.to_thread(self.projects.program, key) or b"", content_type="application/zip")
        return web.Response(body=await asyncio.to_thread(self.projects.full_project, key) or b"",
                            content_type="application/octet-stream")

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
            f, override = await asyncio.to_thread(self.content.resolve_web_content, path)
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
            html = f.read_text(encoding="utf-8").replace("</head>", self.inject_js() + "</head>", 1)
            return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})
        resp = web.FileResponse(f)
        resp.content_type = ctype.split(";")[0]
        return resp

    # ------------------------------------------------------------------ websocket rpc
    async def ws_handler(self, request):
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        log.info("web connected (%d clients)", len(self.clients))
        inflight = asyncio.Semaphore(self.MAX_INFLIGHT_RPC)
        tasks: set[asyncio.Task] = set()

        async def run(data):
            try:
                await self.handle_rpc(ws, data)
            finally:
                inflight.release()

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await inflight.acquire()  # backpressure: stop reading while too many calls are pending
                    t = asyncio.create_task(run(msg.data))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
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

    async def shutdown(self, browser_proc=None):
        """Fast exit: close page sockets ourselves, then hubs, browser, state file."""
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.close(code=1001), 1)
            except Exception:
                pass
        self.clients.clear()
        try:
            await asyncio.wait_for(self.hubs.disconnect_all(), 3)
        except Exception:
            log.warning("hub disconnect timed out on shutdown")
        if browser_proc and browser_proc.returncode is None:
            browser_proc.terminate()
        try:
            STATE.unlink()
        except OSError:
            pass

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
            h = self._rpc.get(method)
            if h is None:
                log.debug("unhandled rpc %s", method)
                return await err("Not implemented")
            await h(a0, ok, err, ws)
        except BadPath:
            await err("Invalid path")
        except Exception as e:
            log.exception("rpc %s failed", method)
            await err(type(e).__name__)

    @property
    def _rpc(self):
        t = getattr(self, "_rpc_cache", None)
        if t is None:
            t = self._rpc_cache = self.rpc_table()
        return t

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
            await asyncio.to_thread(H.settings.set_many, vals)
            await ok({})
            if "ui.language" in vals:
                await H.broadcast("/linux/menuLabels", H.menu_labels())

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

        # menu: same state machine as the Windows host (ClientRpcMenuRequestHandler / DefaultUIService):
        #   enableInitialItems -> File/Help usable; show/hideProjectItems -> Save as / Move to / Share
        def menu_rpc(field, value):
            async def h(a, ok, err, ws):
                H.menu_state[field] = value
                await ok({})
                await H.broadcast("/linux/menuState", dict(H.menu_state))
            return h
        S["/menu/enableInitialItems"] = menu_rpc("initial", True)
        S["/menu/showProjectItems"] = menu_rpc("project", True)
        S["/menu/hideProjectItems"] = menu_rpc("project", False)

        @reg("/linux/menu")
        async def _(a, ok, err, ws):
            item = a.get("item")
            if item not in MENU_ITEMS:
                return await err("Unknown menu item")
            await ok({})
            await H.menu_click(item)

        @reg("/linux/menuInfo")
        async def _(a, ok, err, ws):
            await ok({"labels": H.menu_labels(), "state": dict(H.menu_state)})

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
                    await H.projects.locked(H.projects.move_to, pid, d)

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
            if path:
                path = str(Path(path).resolve())
                H.saveas_approved.add(path)  # the follow-up POST /projects/<id> may write exactly here
            await ok({"filePath": path})

        @reg("/projects/delete")
        async def _(a, ok, err, ws):
            pid = a.get("projectId")
            if not pid:
                return await err("No project id")
            await H.projects.locked(H.projects.delete, pid)
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
            async with s.post(f"http://{HOSTNAME}:{st['port']}/activate", json=[str(Path(f).resolve()) for f in files],
                              headers={"Host": f"{HOSTNAME}:{st['port']}", "X-Spike-Token": st.get("token", "")},
                              timeout=3) as r:
                return r.status == 200
    except Exception:
        return False


def write_state(port: int, token: str):
    """Port + session token for `spike3 file.llsp3` to hand files to the running instance (owner-only file)."""
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"port": port, "pid": os.getpid(), "token": token}, f)
    os.chmod(STATE, 0o600)


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
    # Session token: Electron passes one in (it sets it as a cookie before the page loads);
    # the plain-browser / headless modes generate one and hand it over once via the boot URL.
    token = os.environ.pop("SPIKE_SESSION_TOKEN", "") or secrets.token_urlsafe(32)
    host = Host(port, token=token, embedded=args.embedded)
    await host.projects.locked(host.projects.initialize)
    host.projects.initial = await host.projects.locked(host.projects.add_files, args.files)

    runner = web.AppRunner(host.app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    write_state(port, token)
    log.info("SPIKE host %s listening on %s", HOST_VERSION, host.base)
    log.info("boot url: %s", host.boot_url())  # token is not part of it
    if args.headless and not args.embedded:
        # headless/dev: the login URL (with token) goes into an owner-only file, never into the log
        login_file = USER_DATA / "login-url.txt"
        fd = os.open(login_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(host.login_url() + "\n")
        log.info("login url written to %s", login_file)

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
        cmd = [b, f"--app={host.login_url()}", f"--user-data-dir={prof}", "--class=lego-spike",
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
    log.info("shutting down")
    await host.shutdown(browser_proc)
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
