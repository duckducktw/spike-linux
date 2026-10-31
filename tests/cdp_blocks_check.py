"""Drive a running SPIKE window over CDP (Electron --remote-debugging-port) and check the blocks editor.

  python3 tests/cdp_blocks_check.py [cdp_port]
"""
import asyncio
import sys

from playwright.async_api import async_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9333


async def main():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(f"http://localhost:{PORT}")
        pg = next(pg for ctx in b.contexts for pg in ctx.pages if "spike" in pg.url)
        print("ua:", (await pg.evaluate("navigator.userAgent"))[:120])
        print("viewport:", await pg.evaluate("[innerWidth, innerHeight]"))

        async def click(sel=None, text=None):
            try:
                loc = pg.get_by_text(text, exact=True).first if text else pg.locator(sel).first
                await loc.click(force=True, timeout=4000)
                await pg.wait_for_timeout(2500)
            except Exception:
                pass

        await click(text="Prime")
        await click("[data-testid=introduction-got-it]")
        await click("[data-testid=create-project-lobby-button]")
        await click("[data-testid=word-blocks]")
        await click("[data-testid=new-project-modal__create]")
        await pg.wait_for_timeout(4000)
        res = await pg.evaluate("""() => {
            const svg = document.querySelector('.blocklySvg');
            const b = document.querySelector('.blocklyFlyout .blocklyDraggable');
            return {svgWidth: svg && svg.getAttribute('width'),
                    flyoutBlocks: document.querySelectorAll('.blocklyFlyout .blocklyDraggable').length,
                    firstBlock: b && b.getBoundingClientRect().toJSON()};
        }""")
        print("blocks:", res)
        await pg.screenshot(path="/tmp/electron-blocks.png")


asyncio.run(main())
