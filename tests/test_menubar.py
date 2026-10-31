"""Menu bar end-to-end: real Chromium, real web app, real clicks.

Run: .venv/bin/python tests/test_menubar.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import UDF  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PORT = 3196
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def chrome():
    for b in ("google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(b)
        if p:
            return p
    return None


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="spike-menu-"))
    env = dict(os.environ, HOME=str(tmp), XDG_CONFIG_HOME=str(tmp / "cfg"), XDG_DOCUMENTS_DIR=str(tmp / "Documents"),
               SPIKE_FAKE_DIALOG="")
    env.pop("SPIKE_SESSION_TOKEN", None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "host/spike_host.py"), "--headless", "--port", str(PORT)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    login = tmp / "cfg" / UDF / "login-url.txt"
    try:
        for _ in range(100):
            if login.exists():
                break
            time.sleep(0.1)
        url = login.read_text().strip()
        async with async_playwright() as p:
            kw = {"executable_path": chrome()} if chrome() else {}
            b = await p.chromium.launch(headless=True, **kw)
            pg = await b.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await pg.goto(url)
            await pg.wait_for_selector("#linux-spike-menubar .lsm-top", timeout=20000)
            tops = await pg.locator("#linux-spike-menubar .lsm-top").all_inner_texts()
            check("menu bar shows File/Edit/View/Help", tops == ["File", "Edit", "View", "Help"], tops)

            # bar must not cover the app: #root starts below it
            box = await pg.evaluate("(() => { const r = document.querySelector('#app').getBoundingClientRect(); "
                                    "const m = document.querySelector('#linux-spike-menubar').getBoundingClientRect(); "
                                    "return {root: r.top, bar: m.bottom, h: r.height, vh: innerHeight}; })()")
            check("app content starts below the bar", box["root"] >= box["bar"] - 0.5 and box["root"] + box["h"] <= box["vh"] + 1, box)

            await pg.wait_for_timeout(12000)  # web app boot
            for t in ["introduction-got-it"]:
                try:
                    await pg.locator(f"[data-testid={t}]").first.click(force=True, timeout=3000)
                except Exception:
                    pass
            await pg.get_by_text("Prime", exact=True).first.click(force=True)
            await pg.wait_for_timeout(3000)
            try:
                await pg.locator("[data-testid=introduction-got-it]").first.click(force=True, timeout=3000)
            except Exception:
                pass

            # File menu: project items disabled in the lobby (no project open)
            await pg.locator("#linux-spike-menubar [data-menu=file]").dispatch_event("mousedown")
            items = await pg.locator(".lsm-pop .lsm-item").evaluate_all(
                "els => els.map(e => [e.dataset.item, e.innerText.split('\\n')[0], e.disabled])")
            d = {i[0]: i for i in items}
            check("File has New/Open/Save as/Move to/Share/Quit",
                  all(k in d for k in ["newProject", "openProject", "saveAs", "moveTo", "share", "@quit"]), items)
            check("Save as disabled in lobby", d.get("saveAs", [0, 0, False])[2] is True, d.get("saveAs"))
            check("Open enabled in lobby", d.get("openProject", [0, 0, True])[2] is False, d.get("openProject"))

            # File > New Project -> the web app's own new-project dialog opens
            await pg.locator(".lsm-pop [data-item=newProject]").click()
            await pg.wait_for_timeout(2500)
            modal = await pg.locator("[data-testid=new-project-modal__create]").count()
            check("File > New Project opens the new-project dialog", modal > 0)
            await pg.locator("[data-testid=word-blocks]").first.click(force=True)
            await pg.locator("[data-testid=new-project-modal__create]").first.click(force=True)
            await pg.wait_for_timeout(5000)

            # inside a project: Save as / Move to become enabled (web app -> /menu/showProjectItems)
            await pg.locator("#linux-spike-menubar [data-menu=file]").dispatch_event("mousedown")
            dis = await pg.locator(".lsm-pop [data-item=saveAs]").evaluate("e => e.disabled")
            check("Save as enabled inside a project", dis is False)
            await pg.keyboard.press("Escape")
            check("Escape closes the menu", await pg.locator(".lsm-pop").count() == 0)

            # Edit menu: Copy/Paste reach the focused text field
            await pg.evaluate("""(() => { const i = document.createElement('input'); i.id = 'menu-test-input';
                i.style.cssText = 'position:fixed;left:400px;top:400px;z-index:2147483647'; document.body.appendChild(i);
                i.value = 'hello'; i.focus(); i.select(); })()""")
            await pg.locator("#linux-spike-menubar [data-menu=edit]").dispatch_event("mousedown")
            labels = await pg.locator(".lsm-pop .lsm-item").all_inner_texts()
            check("Edit has Undo/Redo/Cut/Copy/Paste/Select All",
                  [x.split("\n")[0] for x in labels] == ["Undo", "Redo", "Cut", "Copy", "Paste", "Select All"], labels)
            await pg.locator(".lsm-pop [data-item='@cut']").click()
            val = await pg.evaluate("document.getElementById('menu-test-input').value")
            check("Edit > Cut acts on the focused field", val == "", val)
            await pg.locator("#linux-spike-menubar [data-menu=edit]").dispatch_event("mousedown")
            await pg.locator(".lsm-pop [data-item='@undo']").click()
            val = await pg.evaluate("document.getElementById('menu-test-input').value")
            check("Edit > Undo restores it", val == "hello", val)

            # Help > Settings opens the web app's settings
            await pg.locator("#linux-spike-menubar [data-menu=help]").dispatch_event("mousedown")
            await pg.locator(".lsm-pop [data-item=settings]").click()
            await pg.wait_for_timeout(2000)
            txt = await pg.evaluate("document.body.innerText")
            check("Help > Settings opens settings", "Settings" in txt or "設定" in txt)
            await pg.screenshot(path=str(tmp / "menubar.png"))
            print("screenshot:", tmp / "menubar.png")
            check("no page errors", not errors, errors[:3])
            await b.close()
    finally:
        proc.terminate()
        proc.wait(5)
    print(f"\n{len(FAILS)} failed" if FAILS else "\nALL PASS")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
