#!/usr/bin/env python3
"""spike3 launcher.

On first run, downloads the official LEGO Education SPIKE Windows installer,
verifies its SHA-256 and extracts the web app into the user's data dir.
LEGO's files are never redistributed by this project.

  spike3 [files...]            start the app
  spike3 --setup [MSI]         (re)extract the web app, optionally from a local MSI
  spike3 --version
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "vendor"))
sys.path.insert(0, str(HERE))

VERSION = "0.1.1"
SPIKE_VERSION = "3.6.1"
MSI_URL = "https://assets.education.lego.com/_/downloads/SPIKE_APP_3_Win10__3.6.1_Global.msi"
MSI_SHA256 = "f2b4bd158d3e948e65e9ce00366e6afa6d69f4fe546e1f8b4bad2e6fdf18d31a"
MSI_SIZE = 1395183616

XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))


def app_root() -> Path:
    env = os.environ.get("SPIKE_APP_ROOT")
    if env:
        return Path(env)
    local = HERE.parent / "app"  # source checkout / install.sh layout
    if (local / "platform/web/index.html").is_file():
        return local
    return XDG_DATA / "spike3-linux" / "app"


class Progress:
    """zenity progress dialog when running from the desktop, plain text otherwise."""

    def __init__(self, title):
        self.proc = None
        self.last = -1
        if not sys.stdout.isatty() and os.environ.get("DISPLAY", os.environ.get("WAYLAND_DISPLAY")) and shutil.which("zenity"):
            self.proc = subprocess.Popen(["zenity", "--progress", "--title", title, "--width", "460",
                                          "--auto-close", "--no-cancel", "--text", title],
                                         stdin=subprocess.PIPE, text=True)

    def update(self, pct, text):
        pct = int(pct)
        if self.proc:
            try:
                self.proc.stdin.write(f"{pct}\n#{text}\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                pass
        elif pct != self.last:
            print(f"\r[{pct:3d}%] {text}".ljust(70), end="", flush=True)
        self.last = pct

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.write("100\n")
                self.proc.stdin.close()
            except BrokenPipeError:
                pass
            self.proc.wait()
        else:
            print()


def error(msg):
    print(msg, file=sys.stderr)
    if not sys.stdout.isatty() and shutil.which("zenity"):
        subprocess.run(["zenity", "--error", "--width", "420", "--title", "LEGO Education SPIKE", "--text", msg])
    sys.exit(1)


def download(dst: Path, prog: Progress):
    part = dst.with_suffix(".part")
    h = hashlib.sha256()
    req = urllib.request.Request(MSI_URL, headers={"User-Agent": f"spike3-linux/{VERSION}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or MSI_SIZE)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            prog.update(done * 80 / total, f"下載官方安裝檔 {done >> 20} / {total >> 20} MB")
    if h.hexdigest() != MSI_SHA256:
        part.unlink(missing_ok=True)
        raise RuntimeError("下載的安裝檔 SHA-256 不符，已刪除（可能是 LEGO 更新了版本或下載損毀）")
    part.rename(dst)


def setup(msi: str | None = None):
    out = app_root()
    out.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(out.parent).free
    need = (3 if msi else 4.5) * (1 << 30)
    if free < need:
        error(f"磁碟空間不足：需要約 {need / (1 << 30):.1f} GB，{out.parent} 只剩 {free / (1 << 30):.1f} GB")
    prog = Progress("LEGO Education SPIKE 首次設定")
    try:
        with tempfile.TemporaryDirectory(dir=out.parent, prefix=".spike3-setup-") as tmp:
            if msi:
                src = Path(msi).resolve()
            else:
                src = Path(tmp) / "SPIKE.msi"
                download(src, prog)
            import extract
            extract.extract(src, out, delete_msi=not msi,
                            progress=lambda pct, text: prog.update(80 + pct * 0.2, text))
    except Exception as e:
        prog.close()
        error(f"設定失敗：{e}")
    prog.close()
    print(f"web app ready at {out}")


def main():
    args = sys.argv[1:]
    if args[:1] == ["--version"]:
        print(f"spike3-linux {VERSION} (LEGO Education SPIKE {SPIKE_VERSION})")
        return
    if args[:1] == ["--setup"]:
        setup(args[1] if len(args) > 1 else None)
        return
    root = app_root()
    if not (root / "platform/web/index.html").is_file():
        setup()
    os.environ["SPIKE_APP_ROOT"] = str(root)
    import asyncio
    import spike_host
    asyncio.run(spike_host.main())


if __name__ == "__main__":
    main()
