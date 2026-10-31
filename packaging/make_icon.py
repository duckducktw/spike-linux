"""Generate packaging/icon.png: a neutral brick icon (no LEGO artwork/trademarks)."""
from pathlib import Path
from PIL import Image, ImageDraw

S = 256
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((16, 16, S - 16, S - 16), radius=44, fill=(33, 41, 54, 255))
# a generic 2x2 brick seen from the front-top
d.rounded_rectangle((52, 110, 204, 196), radius=10, fill=(245, 180, 30, 255))
for cx in (90, 166):
    d.rounded_rectangle((cx - 26, 80, cx + 26, 116), radius=8, fill=(245, 180, 30, 255))
    d.ellipse((cx - 26, 70, cx + 26, 92), fill=(255, 205, 80, 255))
d.rectangle((52, 176, 204, 196), fill=(214, 150, 15, 255))
# terminal prompt glyph (Linux)
d.line((78, 134, 98, 148, 78, 162), fill=(33, 41, 54, 255), width=9, joint="curve")
d.line((110, 164, 150, 164), fill=(33, 41, 54, 255), width=9)
out = Path(__file__).resolve().parent / "icon.png"
im.save(out)
print(out)
