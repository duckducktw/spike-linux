#!/usr/bin/env python3
"""Extract the SPIKE web app from the official Windows MSI.

  python3 extract.py SPIKE_APP_3_Win10__3.6.1_Global.msi [outdir]

Needs: msitools (msiextract) and the python package dnfile.
The web app lives as ~10k manifest resources inside "LEGO Education SPIKE.dll";
Com.Lego.Flipper.Windows.platform.map.json maps resource GUIDs back to file paths.
"""
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PREFIX = "Com.Lego.Flipper.Windows.platform."


def extract(msi, out, delete_msi=False, progress=None):
    import dnfile

    progress = progress or (lambda pct, text: None)
    msi, out = Path(msi).resolve(), Path(out).resolve()
    if not shutil.which("msiextract"):
        raise RuntimeError("找不到 msiextract：sudo apt install msitools")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".spike3-msi-") as tmp:
        progress(0, "解開安裝檔 ...")
        r = subprocess.run(["msiextract", "-C", tmp, str(msi)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"msiextract 失敗：{r.stderr.strip()[:300]}")
        if delete_msi:
            msi.unlink(missing_ok=True)  # free 1.3 GB before writing the output
        dll = next(Path(tmp).rglob("LEGO Education SPIKE.dll"), None)
        if dll is None:
            raise RuntimeError("安裝檔裡找不到 LEGO Education SPIKE.dll（版本不相容？）")
        progress(30, "讀取資源 ...")
        pe = dnfile.dnPE(str(dll), fast_load=False)
        base = pe.get_offset_from_rva(pe.net.struct.ResourcesRva)
        res = {}
        for row in pe.net.mdtables.ManifestResource.rows:
            res[str(row.Name)] = base + row.Offset
        pe.close()

        staging = Path(tmp) / "app"
        with open(dll, "rb") as f:
            def read(name):
                f.seek(res[name])
                ln = struct.unpack("<I", f.read(4))[0]
                return f.read(ln)

            mapping = json.loads(read(PREFIX + "map.json"))
            n = write_tree(staging, mapping, read, progress)
            (staging / "versions.json").write_bytes(read("Com.Lego.Flipper.Windows.PlatformPrepper.versions.json"))
            (staging / "appconfig.json").write_bytes(read("Com.Lego.Flipper.Windows.PlatformPrepper.appconfig.json"))
        if out.exists():
            shutil.rmtree(out)
        staging.rename(out)
    progress(100, "完成")
    return n


def write_tree(staging, mapping, read, progress=None):
    """Write the platform map's files under `staging`.

    The map comes from the (possibly user-supplied) MSI, so every target is resolved first and
    must stay inside `staging`; nothing is written if any entry escapes.
    """
    progress = progress or (lambda pct, text: None)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    root = staging.resolve()

    def target(e):
        p = (staging / str(e["dir"]).lstrip("/") / str(e["name"])).resolve()
        if p == root or root not in p.parents:
            raise RuntimeError(f"安裝檔內容不合法（路徑跑出目錄）：{e['dir']}/{e['name']}")
        return p

    plan = [(e, target(e)) for e in mapping]  # validate everything before writing anything
    for e, p in plan:
        if e["isdir"]:
            p.mkdir(parents=True, exist_ok=True)
    files = [(e, p) for e, p in plan if not e["isdir"]]
    for i, (e, p) in enumerate(files):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(read(PREFIX + e["resource"]))
        if i % 200 == 0:
            progress(30 + 70 * i / len(files), f"寫出 web app {i}/{len(files)}")
    return len(files)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "app"
    n = extract(sys.argv[1], out, progress=lambda pct, text: print(f"\r[{int(pct):3d}%] {text}".ljust(60), end="", flush=True))
    print(f"\nextracted {n} files to {out}")


if __name__ == "__main__":
    main()
