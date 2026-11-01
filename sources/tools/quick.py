#!/usr/bin/env python3
"""Fast preview of glyph functions straight from the construction kit (no font build)."""
from PIL import Image, ImageDraw
from fontTools.pens.basePen import BasePen
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_rondel as B


class Flat(BasePen):
    def __init__(self):
        super().__init__(None)
        self.contours, self.cur = [], []

    def _moveTo(self, p):
        self.cur = [p]

    def _lineTo(self, p):
        self.cur.append(p)

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, 9):
            t = i / 8
            a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))

    def _closePath(self):
        self.contours.append(self.cur)
        self.cur = []

    _endPath = _closePath


def render(fns, out, px=220, labels=None):
    n = len(fns)
    img = Image.new("RGB", (n * px, px + 20), (246, 242, 233))
    d = ImageDraw.Draw(img)
    for i, fn in enumerate(fns):
        g = B.G()
        fn(g)
        path = g.outline()
        pen = Flat()
        path.draw(pen)
        sc = px / 2.4
        ox, oy = i * px + px / 2, 10 + px / 2
        r = B.D * sc
        d.ellipse([ox - r, oy - r, ox + r, oy + r], outline=(205, 214, 228))
        mask = Image.new("1", img.size, 0)
        md = ImageDraw.Draw(mask)
        for c in pen.contours:
            pts = [(ox + x * sc, oy - y * sc) for x, y in c]
            if len(pts) > 2:
                tmp = Image.new("1", img.size, 0)
                ImageDraw.Draw(tmp).polygon(pts, fill=1)
                from PIL import ImageChops
                mask = ImageChops.logical_xor(mask, tmp)
        img.paste((20, 30, 56), (0, 0), mask.convert("L"))
        if labels:
            d.text((i * px + 4, 2), labels[i], fill=(196, 48, 43))
    img.save(out)
    print(out)
