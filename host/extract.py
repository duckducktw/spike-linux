#!/usr/bin/env python3
"""Extract the SPIKE web app from the official Windows MSI.

  python3 extract.py SPIKE_APP_3_Win10__3.6.1_Global.msi [outdir]

Needs: msitools (msiextract), python package dnfile.
The web app lives as ~10k manifest resources inside "LEGO Education SPIKE.dll";
Com.Lego.Flipper.Windows.platform.map.json maps resource GUIDs back to file paths.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import dnfile

PREFIX = "Com.Lego.Flipper.Windows.platform."


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    msi = Path(sys.argv[1]).resolve()
    out = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "app").resolve()
    if not shutil.which("msiextract"):
        sys.exit("msiextract not found: sudo apt install msitools")
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        print("unpacking MSI ...")
        subprocess.run(["msiextract", "-C", tmp, str(msi)], check=True, stdout=subprocess.DEVNULL)
        dll = next(Path(tmp).rglob("LEGO Education SPIKE.dll"))
        print("reading", dll.name, f"({dll.stat().st_size >> 20} MiB)")
        pe = dnfile.dnPE(str(dll))
        base = pe.get_offset_from_rva(pe.net.struct.ResourcesRva)
        res = {}
        with open(dll, "rb") as f:
            for r in pe.net.mdtables.ManifestResource.rows:
                f.seek(base + r.Offset)
                ln = struct.unpack("<I", f.read(4))[0]
                res[str(r.Name)] = (base + r.Offset + 4, ln)

            def read(name):
                off, ln = res[name]
                f.seek(off)
                return f.read(ln)

            mapping = json.loads(read(PREFIX + "map.json"))
            if out.exists():
                shutil.rmtree(out)
            n = 0
            for e in mapping:
                p = out / e["dir"] / e["name"]
                if e["isdir"]:
                    p.mkdir(parents=True, exist_ok=True)
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(read(PREFIX + e["resource"]))
                n += 1
            (out / "versions.json").write_bytes(read("Com.Lego.Flipper.Windows.PlatformPrepper.versions.json"))
            (out / "appconfig.json").write_bytes(read("Com.Lego.Flipper.Windows.PlatformPrepper.appconfig.json"))
    print(f"extracted {n} files to {out}")


if __name__ == "__main__":
    main()
