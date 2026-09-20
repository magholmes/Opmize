"""Make a macOS icon (.icns) from the icon photo: centre-square crop, Big Sur style rounded square inset on a transparent
1024 px canvas. usage: python make_icns.py icon_photo.jpg icon.icns"""
import sys
from PIL import Image, ImageDraw

src, out = sys.argv[1], sys.argv[2]
im = Image.open(src).convert("RGB")
w, h = im.size
s = min(w, h)
im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((1024, 1024), Image.Resampling.LANCZOS)
canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
inset = 100                                                    # macOS icons float inside the 1024 canvas
tile = im.resize((1024 - 2 * inset, 1024 - 2 * inset), Image.Resampling.LANCZOS)
mask = Image.new("L", tile.size, 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, tile.width - 1, tile.height - 1], radius=int(tile.width * 0.225), fill=255)
canvas.paste(tile, (inset, inset), mask)
canvas.save(out, format="ICNS")
print("icon written:", out)
