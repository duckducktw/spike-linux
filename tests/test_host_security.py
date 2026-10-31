"""Security + menu regression tests for the host (no hub, no window).

Run: .venv/bin/python tests/test_host_security.py

Starts an isolated host (temp HOME/XDG, own port). Every check is a pair:
the legitimate request works AND the hostile one is refused.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UDF  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 3197
HOST = f"spike.localhost:{PORT}"
BASE = f"http://127.0.0.1:{PORT}"
OWN = {"Host": HOST, "Origin": f"http://{HOST}", "Sec-Fetch-Site": "same-origin"}
EVIL = {"Host": HOST, "Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def start_host(tmp: Path):
    env = dict(os.environ, HOME=str(tmp), XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_DATA_HOME=str(tmp / "data"),
               XDG_DOCUMENTS_DIR=str(tmp / "Documents"), SPIKE_FAKE_DIALOG=str(tmp / "Documents" / "picked.llsp3"))
    env.pop("SPIKE_SESSION_TOKEN", None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "host/spike_host.py"), "--headless", "--port", str(PORT)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    state = tmp / "cfg" / UDF / "linux-host.json"
    for _ in range(100):
        if state.exists():
            break
        time.sleep(0.1)
    st = json.loads(state.read_text())
    return proc, st["token"], state


async def ws_rpc(s, headers, method, params=None, cookie=None):
    h = dict(headers)
    if cookie:
        h["Cookie"] = f"spike_token={cookie}"
    try:
        ws = await s.ws_connect(f"ws://127.0.0.1:{PORT}/", headers=h)
    except aiohttp.WSServerHandshakeError as e:
        return ("refused", e.status)
    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}))
    try:
        while True:
            m = await asyncio.wait_for(ws.receive(), 5)
            d = json.loads(m.data)
            if d.get("id") == "1":
                await ws.close()
                return ("ok", d)
    except Exception as e:
        await ws.close()
        return ("timeout", str(e))


async def post_project(s, headers, key, path=None, cookie=None):
    fd = aiohttp.FormData()
    fd.add_field("name", "X")
    if path:
        fd.add_field("path", path)
    fd.add_field("file", b"PK\x05\x06" + b"\0" * 18, filename="p.llsp3")
    h = dict(headers)
    if cookie:
        h["Cookie"] = f"spike_token={cookie}"
    async with s.post(f"{BASE}/projects/{key}", data=fd, headers=h) as r:
        return r.status


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="spike-sec-"))
    (tmp / "Documents").mkdir()
    proc, token, state = start_host(tmp)
    try:
        await asyncio.sleep(0.5)
        target = tmp / "pwned.txt"
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
            # --- state file holding the token is owner-only
            check("state file is 0600", (state.stat().st_mode & 0o777) == 0o600, oct(state.stat().st_mode))
            login = (tmp / "cfg" / UDF / "login-url.txt")
            check("login url file is 0600", login.exists() and (login.stat().st_mode & 0o777) == 0o600)
            log = (tmp / "cfg" / UDF / "log.txt").read_text()
            check("token never written to log", token not in log)

            # --- page / WS from our own window works
            async with s.get(f"{BASE}/", headers=OWN, cookies={"spike_token": token}) as r:
                check("own page loads", r.status == 200, r.status)
                html = await r.text()
            check("menu bar injected", "linux-spike-menubar" in html)
            st, d = await ws_rpc(s, OWN, "/projects/list", cookie=token)
            check("own WS rpc works", st == "ok" and "result" in d, (st, d))

            # --- a hostile web page is refused everywhere
            async with s.options(f"{BASE}/projects/x", headers={**EVIL, "Access-Control-Request-Method": "POST"}) as r:
                check("CORS preflight from evil origin refused", r.status == 403 and "Access-Control-Allow-Origin" not in r.headers,
                      (r.status, dict(r.headers)))
            st, d = await ws_rpc(s, EVIL, "/projects/list")
            check("WS from evil origin refused", st == "refused", (st, d))
            st, d = await ws_rpc(s, EVIL, "/projects/list", cookie=token)
            check("WS from evil origin refused even with cookie", st == "refused", (st, d))
            code = await post_project(s, EVIL, "abc", path=str(target))
            check("cross-site project write refused", code == 403 and not target.exists(), code)

            # --- DNS rebinding (attacker hostname pointing at 127.0.0.1)
            async with s.get(f"{BASE}/", headers={"Host": f"evil.example:{PORT}"}) as r:
                check("foreign Host header refused", r.status == 403, r.status)

            # --- other local processes without the token
            async with s.get(f"{BASE}/", headers={"Host": HOST}) as r:
                check("no token -> refused", r.status == 403, r.status)
            st, d = await ws_rpc(s, {"Host": HOST}, "/projects/list")
            check("WS without token refused", st == "refused", (st, d))
            st, d = await ws_rpc(s, {"Host": HOST}, "/projects/list", cookie="wrong")
            check("WS with wrong token refused", st == "refused", (st, d))
            async with s.get(f"{BASE}/linux/login?t=wrong", headers={"Host": HOST}, allow_redirects=False) as r:
                check("login with wrong token refused", r.status == 403, r.status)
            async with s.get(f"{BASE}/linux/login?t={token}", headers={"Host": HOST}, allow_redirects=False) as r:
                ck = r.headers.get("Set-Cookie", "")
                check("login with right token sets HttpOnly SameSite cookie",
                      r.status == 302 and "HttpOnly" in ck and "SameSite=Strict" in ck, (r.status, ck))

            # --- Save As: only the path the user picked in the dialog
            code = await post_project(s, OWN, "abc", path=str(target), cookie=token)
            check("save-as to arbitrary path refused", code == 403 and not target.exists(), code)
            st, d = await ws_rpc(s, OWN, "/projects/saveAs", {"projectId": "abc"}, cookie=token)
            picked = d.get("result", {}).get("filePath") if st == "ok" else None
            check("saveAs dialog returns picked path", picked == str((tmp / "Documents" / "picked.llsp3").resolve()), (st, d))
            code = await post_project(s, OWN, "abc", path=picked, cookie=token)
            check("save-as to the picked path works", code == 200 and Path(picked).exists(), code)
            code = await post_project(s, OWN, "abc", path=picked, cookie=token)
            check("picked path is single-use", code == 403, code)

            # --- content tmp path traversal -> clean 400, nothing written
            fd = aiohttp.FormData()
            fd.add_field("file", b"x", filename="x")
            async with s.post(f"{BASE}/content/tmp/t1/..%2F..%2F..%2Fescape.txt", data=fd,
                              headers={**OWN, "Cookie": f"spike_token={token}"}) as r:
                check("content tmp traversal -> 400", r.status in (400, 404), r.status)
            check("content traversal wrote nothing", not any(tmp.rglob("escape.txt")))

            # --- menu
            st, d = await ws_rpc(s, OWN, "/linux/menuInfo", cookie=token)
            labels = d.get("result", {}).get("labels", {}) if st == "ok" else {}
            check("menu labels come from host translations",
                  labels.get("menu.file.title") == "File" and labels.get("menu.edit.title") == "Edit", labels)
            st, d = await ws_rpc(s, OWN, "/linux/menu", {"item": "rm -rf"}, cookie=token)
            check("unknown menu item rejected", st == "ok" and "error" in d, d)
    finally:
        proc.terminate()
        proc.wait(5)
    print(f"\n{len(FAILS)} failed" if FAILS else "\nALL PASS")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
