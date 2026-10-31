"""Host must exit promptly when Electron goes away (stdin closed) even with the page still connected.

Run: .venv/bin/python tests/test_shutdown.py
"""
import subprocess, sys, time, os, json, tempfile, asyncio, aiohttp
from pathlib import Path
tmp = Path(tempfile.mkdtemp())
env = dict(os.environ, HOME=str(tmp), XDG_CONFIG_HOME=str(tmp/"cfg"), XDG_DOCUMENTS_DIR=str(tmp/"Documents"), SPIKE_SESSION_TOKEN="tok123")
p = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / "host/spike_host.py"), "--embedded", "-v"], env=env,
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
line = p.stdout.readline()
while not line.startswith("READY"): line = p.stdout.readline()
port = json.loads(line[6:])["port"]
async def connect():
    s = aiohttp.ClientSession()
    ws = await s.ws_connect(f"ws://127.0.0.1:{port}/", headers={"Host": f"spike.localhost:{port}", "Cookie": "spike_token=tok123"})
    return s, ws
loop = asyncio.new_event_loop(); s, ws = loop.run_until_complete(connect())
time.sleep(1)
t0 = time.time(); p.stdin.close()
p.wait(90)
el = time.time() - t0
ok = el < 5
print(("PASS" if ok else "FAIL") + f" host exited {el:.1f}s after stdin closed (page still connected)")
loop.run_until_complete(s.close())
sys.exit(0 if ok else 1)
