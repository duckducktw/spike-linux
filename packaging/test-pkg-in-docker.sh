#!/usr/bin/env bash
# Runs inside a clean container (Ubuntu/Debian or Fedora): install the Electron package from /dist,
# check files/permissions, start the bundled app under Xvfb, and (if /msi/SPIKE.msi is mounted)
# run first-time setup and verify that the window loads SPIKE with the blocks editor working.
#   docker run --rm -v $PWD/dist:/dist:ro [-v dir_with_SPIKE.msi:/msi:ro] -v $PWD/packaging/test-pkg-in-docker.sh:/t.sh:ro IMAGE bash /t.sh
set -euo pipefail
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq /dist/*.deb xvfb xauth curl python3-pip >/dev/null
  REMOVE="apt-get remove -y -qq spike3-linux"
else
  dnf -y -q install /dist/*.rpm xorg-x11-server-Xvfb xauth curl shadow-utils python3-pip >/dev/null
  REMOVE="dnf -y -q remove spike3-linux"
fi
echo "== files"
test -x /usr/bin/spike3 && echo "/usr/bin/spike3 -> $(readlink -f /usr/bin/spike3)"
stat -c '%U %a %n' /opt/spike3-linux/chrome-sandbox
test -f /usr/lib/udev/rules.d/70-lego-spike.rules && echo "udev rule ok"
grep -E '^(Exec|StartupWMClass)=' /usr/share/applications/lego-spike3.desktop
P=/opt/spike3-linux/resources/host
python3 "$P/spike3.py" --version
python3 -c "import sys; sys.path.insert(0, '$P/vendor'); import dnfile, aiohttp, serial, bleak; from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus; print('python deps ok')"

if [ -f /msi/SPIKE.msi ]; then
  useradd -m kid
  su kid -c "python3 $P/spike3.py --setup /msi/SPIKE.msi" 2>&1 | tail -1
  # docker blocks the namespaces Chromium sandboxes with; --no-sandbox is for this container test only
  # playwright only as a CDP client to inspect the Electron window
  python3 -m pip install -q --break-system-packages playwright==1.55.0 2>/dev/null || python3 -m pip install -q playwright==1.55.0
  cat > /tmp/check.py <<'EOF'
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        for _ in range(60):
            try:
                b = await p.chromium.connect_over_cdp("http://127.0.0.1:9333"); break
            except Exception:
                await asyncio.sleep(1)
        pg = None
        for _ in range(60):
            pages = [x for c in b.contexts for x in c.pages if "spike" in x.url]
            if pages: pg = pages[0]; break
            await asyncio.sleep(1)
        await pg.wait_for_timeout(15000)
        print("ua:", (await pg.evaluate("navigator.userAgent")).split(") ")[-1][:80])
        async def click(sel=None, text=None):
            try:
                loc = pg.get_by_text(text, exact=True).first if text else pg.locator(sel).first
                await loc.click(force=True, timeout=4000); await pg.wait_for_timeout(2500)
            except Exception: pass
        await click(text="Prime")
        for t in ["introduction-got-it", "create-project-lobby-button", "word-blocks", "new-project-modal__create"]:
            await click(f"[data-testid={t}]")
        await pg.wait_for_timeout(4000)
        print("blocks:", await pg.evaluate("""() => ({svgWidth: document.querySelector('.blocklySvg')?.getAttribute('width'),
            flyoutBlocks: document.querySelectorAll('.blocklyFlyout .blocklyDraggable').length})"""))
asyncio.run(main())
EOF
  su kid -c "cd ~ && xvfb-run -a -s '-screen 0 1600x1000x24' spike3 --no-sandbox --remote-debugging-port=9333 > /tmp/app.log 2>&1 &"
  python3 /tmp/check.py || { echo "--- app.log"; tail -30 /tmp/app.log; }
  pkill -f /opt/spike3-linux/spike3 || true; sleep 3
  pgrep -f 'spike3.py --embedded' >/dev/null && echo "host still running after app exit (BAD)" || echo "host stopped with app"
  grep -iE "error|fatal" /tmp/app.log | grep -viE "dbus|gpu|viz|vaapi|ozone|libva|gles|egl|sharedimage|bus.cc|cert_verify" | head -5 || true
fi
$REMOVE >/dev/null
test ! -e /usr/bin/spike3 && test ! -e /usr/lib/udev/rules.d/70-lego-spike.rules && echo "REMOVE OK (cleaned up)"
