#!/usr/bin/env python3
"""Glyph grid proof: each glyph in a cell with the shared circle, centre and baseline drawn."""
import sys
import freetype
from PIL import Image, ImageDraw, ImageFont

NAVY = (20, 30, 56)
OFF = (246, 242, 233)
DENIM = (76, 110, 158)
RED = (196, 48, 43)
GUIDE = (205, 214, 228)

S, BASE_U, D = 400, 230, 430   # font units: scale, circle-centre height, ink radius


def grid(font, chars, out, px=150, cols=10, circle=True):
    face = freetype.Face(font)
    face.set_pixel_sizes(0, px)
    sc = px / face.units_per_EM
    cw = int(2 * D * sc + 40)
    ch = int(2 * D * sc + 50)
    rows = (len(chars) + cols - 1) // cols
    img = Image.new("RGB", (cols * cw, rows * ch), OFF)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(chars):
        gx, gy = (i % cols) * cw, (i // cols) * ch
        cx = gx + cw / 2
        cy = gy + 20 + D * sc
        if circle:
            r = D * sc
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GUIDE, width=1)
            base = cy + BASE_U * sc
            d.line([(gx + 6, base), (gx + cw - 6, base)], fill=(232, 200, 196))
        face.load_char(c, freetype.FT_LOAD_RENDER)
        g = face.glyph
        bm = g.bitmap
        adv = g.advance.x / 64
        ox = cx - adv / 2
        base = cy + BASE_U * sc
        if bm.width and bm.rows:
            buf = bytes(bytearray(b for r_ in range(bm.rows) for b in bm.buffer[r_ * bm.pitch:r_ * bm.pitch + bm.width]))
            m = Image.frombytes("L", (bm.width, bm.rows), buf)
            img.paste(Image.new("RGB", m.size, NAVY), (int(ox + g.bitmap_left), int(base - g.bitmap_top)), m)
        d.text((gx + 6, gy + 4), c if c != " " else "space", fill=RED)
    img.save(out)
    print(out)


if __name__ == "__main__":
    font, out = sys.argv[1], sys.argv[2]
    chars = sys.argv[3] if len(sys.argv) > 3 else None
    if chars is None:
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    grid(font, chars, out)
