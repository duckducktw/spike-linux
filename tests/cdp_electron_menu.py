"""Electron (CDP) check: the native menu follows the web app, and its clicks reach the web app.

Start the app first with SPIKE_TEST_MENU=1 and --remote-debugging-port=9334, stdout to /tmp/electron-menu.log,
then:  python3 tests/cdp_electron_menu.py [cdp_port] [log]
Menu clicks are triggered with the same host call the menu item makes (POST /linux/menu with the token).
"""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9334
LOG = sys.argv[2] if len(sys.argv) > 2 else "/tmp/electron-menu.log"
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def last_menu():
    lines = [line for line in open(LOG, errors="replace") if line.startswith("MENU ")]
    return json.loads(lines[-1][5:]) if lines else None


def item(menu, top, label_start):
    for m in menu["menu"]:
        if m["label"] == top:
            for i in m["items"]:
                if i["label"].startswith(label_start):
                    return i
    return None


async def main():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        pg = [x for c in b.contexts for x in c.pages if "spike" in x.url][0]
        await pg.wait_for_timeout(8000)
        m = last_menu()
        check("native menu built", m is not None and [x["label"] for x in m["menu"]] == ["File", "Edit", "View", "Help"], m)
        check("Save as disabled in lobby", item(m, "File", "Save as")["enabled"] is False)
        check("in-page HTML bar not drawn under Electron", await pg.locator("#linux-spike-menubar").count() == 0)

        async def click(t):
            try:
                await pg.locator(f"[data-testid={t}]").first.click(force=True, timeout=3000)
                await pg.wait_for_timeout(1500)
            except Exception:
                pass
        await click("introduction-got-it")
        await pg.get_by_text("Prime", exact=True).first.click(force=True)
        await pg.wait_for_timeout(3000)
        await click("introduction-got-it")

        # File > New Project (exactly what the menu item's click handler does)
        ok = await pg.evaluate("""async () => {
            const base = location.origin;
            const r = await fetch(base + '/linux/menu?item=newProject', {method: 'POST'});
            return r.status; }""")
        await pg.wait_for_timeout(2500)
        check("File > New Project request accepted", ok == 200, ok)
        check("new-project dialog opened", await pg.locator("[data-testid=new-project-modal__create]").count() > 0)
        await click("word-blocks")
        await click("new-project-modal__create")
        await pg.wait_for_timeout(4000)
        m = last_menu()
        check("Save as enabled inside a project", item(m, "File", "Save as")["enabled"] is True, m["state"])
        check("Move to enabled inside a project", item(m, "File", "Move to")["enabled"] is True)
        print("\n" + (f"{len(FAILS)} failed" if FAILS else "ALL PASS"))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
