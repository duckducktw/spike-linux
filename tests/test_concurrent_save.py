"""Concurrent saves must not lose projects from availableProjects.json (audit Q-1).

Run: .venv/bin/python tests/test_concurrent_save.py
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UDF  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 3198
HOST = f"spike.localhost:{PORT}"
N = 40


def project_zip(pid: str) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("manifest.json", json.dumps({"id": pid, "name": pid, "type": "word-blocks"}))
        z.writestr("scratch.sb3", b"")
    return b.getvalue()


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="spike-race-"))
    env = dict(os.environ, HOME=str(tmp), XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_DATA_HOME=str(tmp / "data"),
               XDG_DOCUMENTS_DIR=str(tmp / "Documents"))
    proc = subprocess.Popen([sys.executable, str(ROOT / "host/spike_host.py"), "--headless", "--port", str(PORT)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state = tmp / "cfg" / UDF / "linux-host.json"
    try:
        for _ in range(100):
            if state.exists():
                break
            time.sleep(0.1)
        token = json.loads(state.read_text()).get("token", "")
        hdr = {"Host": HOST, "Origin": f"http://{HOST}", "Cookie": f"spike_token={token}"}
        async with aiohttp.ClientSession() as s:
            async def save(i):
                fd = aiohttp.FormData()
                fd.add_field("name", f"P{i}")
                fd.add_field("file", project_zip(f"id{i}"), filename="p.llsp3")
                async with s.post(f"http://127.0.0.1:{PORT}/projects/id{i}", data=fd, headers=hdr) as r:
                    return r.status
            codes = await asyncio.gather(*(save(i) for i in range(N)))
        listed = json.loads((tmp / "cfg" / UDF / "availableProjects.json").read_text())
        ok = all(c == 200 for c in codes) and len(listed) == N
        print(f"{'PASS' if ok else 'FAIL'} {N} concurrent saves -> {len(listed)} listed (status {sorted(set(codes))})")
        sys.exit(0 if ok else 1)
    finally:
        proc.terminate()
        proc.wait(5)


if __name__ == "__main__":
    asyncio.run(main())
