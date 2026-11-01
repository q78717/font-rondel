#!/usr/bin/env python3
"""Proof sheet straight from the glyph definitions (no font build): glyph, circle, baseline."""
import os
import sys
from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_rondel as B  # noqa: E402
from quick import Flat  # noqa: E402

NAVY, OFF, RED, GUIDE = (20, 30, 56), (246, 242, 233), (196, 48, 43), (205, 214, 228)


def sheet(names, out, px=120, cols=12):
    rows = (len(names) + cols - 1) // cols
    ch = int(px * 1.45)
    img = Image.new("RGB", (cols * px, rows * ch), OFF)
    d = ImageDraw.Draw(img)
    sc = px / 3.3
    for i, n in enumerate(names):
        gx, gy = (i % cols) * px, (i // cols) * ch
        cx, cy = gx + px / 2, gy + ch * 0.53
        r = B.D * sc
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=GUIDE)
        base = cy + B.XB * sc
        d.line([(gx + 4, base), (gx + px - 4, base)], fill=(232, 200, 196))
        g = B.G()
        B.GLYPHS[n][1](g)
        if g.empty():
            continue
        pen = Flat()
        g.outline().draw(pen)
        mask = Image.new("1", img.size, 0)
        for c in pen.contours:
            pts = [(cx + x * sc, cy - y * sc) for x, y in c]
            if len(pts) > 2:
                t = Image.new("1", img.size, 0)
                ImageDraw.Draw(t).polygon(pts, fill=1)
                mask = ImageChops.logical_xor(mask, t)
        img.paste(NAVY, (0, 0), mask.convert("L"))
        u = B.GLYPHS[n][0]
        d.text((gx + 3, gy + 2), chr(u) if u else n[:10], fill=RED)
    img.save(out)
    print(out)


if __name__ == "__main__":
    sheet(sys.argv[2:], sys.argv[1])
