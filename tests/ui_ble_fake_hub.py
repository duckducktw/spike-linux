"""Real web app + simulated BLE hub: the SPIKE UI itself scans, lists, connects and talks to the hub.

    .venv/bin/python tests/ui_ble_fake_hub.py      (needs sudo for the fake hub, and the extracted app)

Prints the hub-side protocol log (what the web app actually sent over BLE) and saves screenshots.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UDF  # noqa: E402
from test_ble_fake_hub import start_fake_hub, stop, hub_log  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 3194
SHOTS = Path(tempfile.mkdtemp(prefix="spike-ble-ui-"))
NAMES = {0x00: "InfoRequest", 0x01: "InfoResponse", 0x18: "GetHubName", 0x19: "HubName", 0x1A: "DeviceUuid",
         0x1B: "DeviceUuidResp", 0x28: "DeviceNotificationReq", 0x29: "DeviceNotificationResp",
         0x3C: "DeviceNotification", 0x46: "ClearSlot", 0x47: "ClearSlotResp", 0x0C: "StartFileUpload",
         0x0D: "StartFileUploadResp", 0x10: "TransferChunk", 0x11: "TransferChunkResp", 0x1E: "ProgramFlow",
         0x1F: "ProgramFlowResp", 0x20: "ProgramFlowNotif", 0x21: "Console", 0x16: "SetHubName", 0x17: "SetHubNameResp"}


def chrome():
    import shutil
    for b in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(b):
            return shutil.which(b)
    return None


async def click_testid(pg, t, timeout=4000):
    try:
        await pg.locator(f"[data-testid={t}]").first.click(force=True, timeout=timeout)
        await pg.wait_for_timeout(1500)
        return True
    except Exception:
        return False


async def main():
    log = tempfile.mktemp(suffix=".jsonl", prefix="fakehub-ui-")
    fh, adapter = start_fake_hub(log)
    tmp = Path(tempfile.mkdtemp(prefix="spike-ble-ui-home-"))
    env = dict(os.environ, HOME=str(tmp), XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_DOCUMENTS_DIR=str(tmp / "Documents"),
               SPIKE_BLE_ADAPTER=adapter, SPIKE_APP_ROOT=os.environ.get("SPIKE_APP_ROOT", str(ROOT / "app")))
    env.pop("SPIKE_SESSION_TOKEN", None)
    host = subprocess.Popen([sys.executable, str(ROOT / "host/spike_host.py"), "--headless", "--port", str(PORT)],
                            env=env, stdout=subprocess.DEVNULL, stderr=open(tmp / "host.err", "w"))
    login = tmp / "cfg" / UDF / "login-url.txt"
    rpc = []
    try:
        for _ in range(100):
            if login.exists():
                break
            time.sleep(0.1)
        async with async_playwright() as p:
            kw = {"executable_path": chrome()} if chrome() else {}
            b = await p.chromium.launch(headless=True, **kw)
            pg = await b.new_page(viewport={"width": 1440, "height": 900})
            pg.on("websocket", lambda w: (w.on("framesent", lambda f: rpc.append(">" + str(f)[:300])),
                                          w.on("framereceived", lambda f: rpc.append("<" + str(f)[:300]))))
            await pg.goto(login.read_text().strip())
            await pg.wait_for_timeout(12000)
            await click_testid(pg, "introduction-got-it")
            await pg.get_by_text("Prime", exact=True).first.click(force=True)
            await pg.wait_for_timeout(3000)
            await click_testid(pg, "introduction-got-it")
            for t in ["create-project-lobby-button", "python", "new-project-modal__create"]:
                await click_testid(pg, t)
            await pg.wait_for_timeout(4000)
            # write a real program in the Monaco editor
            try:
                await pg.locator(".monaco-editor").first.click(timeout=4000)
                await pg.keyboard.press("Control+A")
                await pg.keyboard.press("Delete")
                await pg.keyboard.type("print('hello from linux')\n")
                await pg.wait_for_timeout(1500)
                print("typed python program")
            except Exception as e:
                print("editor not found:", e)
            await pg.screenshot(path=str(SHOTS / "1-project.png"))
            if os.environ.get("DUMP_PROJECT"):
                print("PROJ BUTTONS:", await pg.evaluate(
                    "[...document.querySelectorAll('button,[role=button],[data-testid]')].filter(e=>e.offsetParent)"
                    ".map(e=>(e.dataset.testid||'')+'|'+(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,30))"
                    ".filter(s=>s!=='|').slice(0,80)"))

            # open the connect panel (hub selector / connect button in the hardware header)
            opened = False
            for sel in ["[data-testid=python-hub__button]", "[data-testid=scratch-hub__button]",
                        "[data-testid=hub-selector]", "[data-testid=button-hub-selector-new]",
                        "[data-testid=lls-connect-open-button]", "text=Connect", "text=連線"]:
                try:
                    await pg.locator(sel).first.click(force=True, timeout=2500)
                    await pg.wait_for_timeout(2000)
                    opened = True
                    print("clicked", sel)
                    if await pg.locator("[data-testid=connect-hub-list]").count():
                        break
                except Exception:
                    pass
            await pg.screenshot(path=str(SHOTS / "2-connect-panel.png"))
            # "Which hub do you have?" -> Green / Already updated (current firmware, connects over BLE)
            if await click_testid(pg, "button-hub-selector-new", 3000):
                print("chose: hub-selector-new (already updated)")
                await pg.wait_for_timeout(3000)
                await pg.screenshot(path=str(SHOTS / "2b-after-selector.png"))
            if os.environ.get("DUMP_PANEL"):
                print("PANEL TEXT:", (await pg.evaluate("document.body.innerText"))[-1500:])
                print("PANEL TESTIDS:", await pg.evaluate(
                    "[...new Set([...document.querySelectorAll('[data-testid]')].map(e=>e.dataset.testid))]"
                    ".filter(t=>/conn|hub|ble|blue|usb|wireless|guide|lls/i.test(t))"))
                print("PANEL BUTTONS:", await pg.evaluate(
                    "[...document.querySelectorAll('button,[role=button]')].map(b=>(b.innerText||b.getAttribute('aria-label')||'').trim())"
                    ".filter(Boolean).slice(-40)"))

            # wait for the fake hub to appear in the list, click it
            found = False
            for _ in range(30):
                btn = pg.locator("[data-testid=connect-hub-list__item__connect-button]")
                if await btn.count():
                    txt = await pg.locator("[data-testid=connect-hub-list]").inner_text()
                    if "Fake SPIKE Prime" in txt:
                        found = True
                        break
                await pg.wait_for_timeout(1000)
            print("hub listed in UI:", found)
            await pg.screenshot(path=str(SHOTS / "3-hub-list.png"))
            if found:
                # the list re-renders on every devicefound (~1/s), so click it from inside the page
                clicked = await pg.evaluate("""() => new Promise(res => {
                    let n = 0; const t = setInterval(() => {
                        const b = document.querySelector('[data-testid=connect-hub-list__item__connect-button]');
                        if (b) { b.click(); clearInterval(t); res(true); }
                        else if (++n > 50) { clearInterval(t); res(false); }
                    }, 100); })""")
                print("clicked CONNECT", clicked)
            for _ in range(30):
                if any("/hub/connected" in r for r in rpc):
                    break
                await pg.wait_for_timeout(500)
            await pg.wait_for_timeout(6000)
            await pg.screenshot(path=str(SHOTS / "4-connected.png"))
            # run the (empty) program from the UI: Play -> the web app compiles + uploads + starts it over BLE
            try:
                await click_testid(pg, "connection-guide-close-button", 1500)
                await pg.keyboard.press("Escape")
                info = await pg.evaluate("""() => { const b = document.querySelector('[data-testid=play-button]');
                    if (!b) return 'none'; const r = b.getBoundingClientRect();
                    const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
                    return JSON.stringify({rect: [r.x, r.y, r.width, r.height], disp: getComputedStyle(b).display,
                        vis: getComputedStyle(b).visibility, cover: top && (top.dataset.testid || top.className || top.tagName)}); }""")
                print("play button:", info)
                ok = await pg.evaluate("() => { const b = document.querySelector('[data-testid=play-button]'); if (b) { b.click(); return true } return false }")
                if not ok:
                    raise RuntimeError("no play button")
                print("clicked Play")
                await pg.wait_for_timeout(10000)
                await pg.screenshot(path=str(SHOTS / "5-after-play.png"))
            except Exception as e:
                print("play not clicked:", e)
            body = await pg.evaluate("document.body.innerText")
            if os.environ.get("DUMP_BODY"):
                print("BODY:", body[:2500].replace("\n", " | "))
                print("DIALOGS:", await pg.evaluate(
                    "[...document.querySelectorAll('[role=dialog],[class*=modal],[class*=toast],[class*=notification]')]"
                    ".map(e=>e.innerText.trim().slice(0,200)).filter(Boolean)"))
            await b.close()
    finally:
        host.terminate()
        host.wait(10)
        stop(fh)

    ev = hub_log(log)
    seq = [("→hub " if e["dir"] == "in" else "←hub ") + NAMES.get(e["type"], hex(e["type"]))
           for e in ev if e.get("dir") in ("in", "out")]
    compact = []
    for s in seq:
        if compact and compact[-1][0] == s:
            compact[-1][1] += 1
        else:
            compact.append([s, 1])
    print("hub-side protocol log:")
    for s, n in compact[:40]:
        print("  ", s, f"x{n}" if n > 1 else "")
    print("notes:", [e for e in ev if e.get("dir") == "note"][:6])
    print("ws rpc (hub):", [r for r in rpc if "/hub/" in r or "hub/devicefound" in r][:14])
    print("screenshots:", SHOTS)
    print("ui text mentions:", [w for w in ("Fake SPIKE Prime", "Connected", "hello from fake hub", "Disconnect", "Update", "firmware") if w in body])


if __name__ == "__main__":
    asyncio.run(main())
