import asyncio, sys
from playwright.async_api import async_playwright

JS = """async()=>{
 const svg=document.querySelector('.blocklySvg'); const host=document.querySelector('.injectionDiv').parentElement;
 const w0=svg.getAttribute('width');
 host.style.width='0px'; window.dispatchEvent(new Event('resize'));  // Blockly measures 0 (window not laid out yet)
 await new Promise(r=>setTimeout(r,300));
 const w1=svg.getAttribute('width');
 host.style.width='';                                                 // container grows, no window resize event
 await new Promise(r=>setTimeout(r,800));
 return {before:w0, collapsed:w1, after:svg.getAttribute('width')};
}"""


async def main(port):
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True)
        pg = await b.new_page(viewport={"width": 1400, "height": 900})
        await pg.goto(f"http://localhost:{port}/debug")
        await pg.wait_for_timeout(14000)
        await pg.get_by_text("Prime", exact=True).first.click(force=True)
        await pg.wait_for_timeout(3000)
        for t in ["introduction-got-it", "create-project-lobby-button", "word-blocks", "new-project-modal__create"]:
            try:
                await pg.locator(f"[data-testid={t}]").first.click(force=True, timeout=4000)
                await pg.wait_for_timeout(2500)
            except Exception:
                pass
        await pg.wait_for_timeout(3000)
        print(port, await pg.evaluate(JS))
        await b.close()

asyncio.run(main(int(sys.argv[1])))
