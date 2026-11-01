#!/usr/bin/env python3
"""Render the README images in documentation/images/ from the built fonts (`make images`)."""
import os
import sys
import unicodedata

from fontTools.ttLib import TTFont
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid  # noqa: E402
from shape_proof import line, stack  # noqa: E402

OFF, RED, DENIM = (246, 242, 233), (196, 48, 43), (62, 97, 148)
R = "fonts/ttf/Rondel-Regular.ttf"
RING = "fonts/ttf/RondelRing-Regular.ttf"
COIN = "fonts/ttf/RondelCoin-Regular.ttf"
OUT = "documentation/images"


def pad(img, w):
    out = Image.new("RGB", (w, img.height), OFF)
    out.paste(img, (0, 0))
    return out


def save(rows, name):
    w = max(i.width for i in rows)
    stack([pad(i, w) for i in rows]).save(os.path.join(OUT, name))
    print(os.path.join(OUT, name))


def main():
    os.makedirs(OUT, exist_ok=True)
    save([line(R, "Rondel", 220, pad=40), line(R, "Every glyph in one circle, from arcs and lines.", 46, pad=40),
          line(R, "<RV> <OC> <JFK>", 120, color=RED, pad=40)], "rondel-banner.png")
    save([line(R, "Rondel  Round & round 2026", 84, pad=30), line(RING, "RING 360", 84, color=DENIM, pad=30),
          line(COIN, "Coin @ 50%", 84, color=RED, pad=30)], "styles.png")
    save([line(R, "<A> <RV> <AB> <OC> <JD>", 130, pad=30), line(R, "<LBJ> <SPQ> <JFK> <MK> <Q>", 130, pad=30)],
         "monograms.png")
    save([line(R, "Žluťoučký kůň úpěl ďábelské ódy", 64, pad=30),
          line(R, "Zażółć gęślą jaźń · Árvíztűrő tükörfúrógép", 64, pad=30),
          line(R, "Çok güzel İstanbul · Ærøskøbing · Œuvre", 64, pad=30),
          line(R, "«Quotes» “curly” — £12 €34 ¥56 © ®", 64, pad=30)], "languages.png")
    cmap = TTFont(R).getBestCmap()
    chars = "".join(chr(c) for c in sorted(cmap)
                    if c > 32 and c != 0xA0 and c != 0x25CC and unicodedata.category(chr(c)) != "Mn")
    grid.grid(R, chars, os.path.join(OUT, "glyphs.png"), px=90, cols=20)


if __name__ == "__main__":
    main()
