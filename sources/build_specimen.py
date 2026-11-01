#!/usr/bin/env python3
"""Build the self-contained specimen page (fonts and glyph data inlined).

Usage: python3 sources/build_specimen.py [--fonts fonts/webfonts] [--ttf fonts/ttf] [--out documentation/specimen/index.html]
"""
import argparse
import base64
import json
import math
import os
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_rondel as B  # noqa: E402


def f(v):
    return round(v * B.S, 1)


def svg_outline(path):
    sp = SVGPathPen(None, ntos=lambda v: ("%.1f" % v).rstrip("0").rstrip("."))
    path.draw(TransformPen(sp, (B.S, 0, 0, B.S, 0, 0)))
    return sp.getCommands()


def svg_skel(skel):
    parts, circles = [], []
    for item in skel:
        kind = item[0]
        if kind == "l":
            parts.append("M" + " L".join(f"{f(x)} {f(y)}" for x, y in item[1]))
        elif kind == "a":
            _, cx, cy, r, s0, s1 = item
            sweep = s1 - s0
            n = 2 if abs(sweep) > math.pi * 1.5 else 1
            d = f"M{f(cx + r * math.cos(s0))} {f(cy + r * math.sin(s0))}"
            for i in range(n):
                t1 = s0 + sweep * (i + 1) / n
                large = 1 if abs(sweep / n) > math.pi else 0
                sw = 1 if sweep > 0 else 0
                d += f" A{f(r)} {f(r)} 0 {large} {sw} {f(cx + r * math.cos(t1))} {f(cy + r * math.sin(t1))}"
            parts.append(d)
            circles.append((f(cx), f(cy), f(r)))
        elif kind in "cd":
            _, cx, cy, r = item
            parts.append(f"M{f(cx + r)} {f(cy)} A{f(r)} {f(r)} 0 1 1 {f(cx - r)} {f(cy)} "
                         f"A{f(r)} {f(r)} 0 1 1 {f(cx + r)} {f(cy)}")
            if kind == "c":
                circles.append((f(cx), f(cy), f(r)))
    return " ".join(parts), [list(c) for c in sorted(set(circles))]


def glyph_data():
    out = []
    for name, (uni, fn) in B.GLYPHS.items():
        if uni is None or name == "space":
            continue
        g = B.G()
        fn(g)
        skel, circles = svg_skel(g.skel)
        out.append({"n": name, "c": chr(uni), "p": svg_outline(g.outline()), "s": skel, "k": circles,
                    "lines": sum(1 for x in g.skel if x[0] == "l"),
                    "arcs": sum(1 for x in g.skel if x[0] in "ac")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fonts", default="fonts/webfonts")
    ap.add_argument("--ttf", default="fonts/ttf")
    ap.add_argument("--template", default=os.path.join(HERE, "specimen_template.html"))
    ap.add_argument("--out", default="documentation/specimen/index.html")
    a = ap.parse_args()

    import plistlib
    kplist = os.path.join(HERE, "ufo", "Rondel-Regular.ufo", "kerning.plist")
    with open(kplist, "rb") as fh:
        pairs = sum(len(v) for v in plistlib.load(fh).values())
    meta = {"S": B.S, "W": round(B.W * B.S), "R": round(B.R * B.S), "D": round(B.D * B.S),
            "XR": round(B.XR * B.S), "XB": round(B.XB * B.S),
            "glyphs": sum(1 for _, (u, _f) in B.GLYPHS.items() if u is not None),
            "kern": pairs,
            "mono": {k: [round(v[0], 4), round(v[1][0], 4), round(v[1][1], 4)] for k, v in B.MONO_SLOTS.items()}}
    data = {"meta": meta, "glyphs": glyph_data()}

    t = open(a.template, encoding="utf-8").read()
    for key, fn in (("__FONT_REGULAR__", "Rondel-Regular.woff2"),
                    ("__FONT_RING__", "RondelRing-Regular.woff2"),
                    ("__FONT_COIN__", "RondelCoin-Regular.woff2")):
        t = t.replace(key, base64.b64encode(open(os.path.join(a.fonts, fn), "rb").read()).decode())
    t = (t.replace("__GLYPH_DATA__", json.dumps(data, separators=(",", ":")))
          .replace("__VERSION__", B.VERSION)
          .replace("__REPO__", B.REPO_URL)
          .replace("__KERN__", f"{pairs:,}")
          .replace("__CHARS__", str(meta["glyphs"])))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    open(a.out, "w", encoding="utf-8").write(t)
    print(f"specimen -> {a.out} ({len(t):,} bytes, {len(data['glyphs'])} glyphs, {pairs:,} kern pairs)")


if __name__ == "__main__":
    main()
