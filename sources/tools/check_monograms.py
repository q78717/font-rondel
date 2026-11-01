#!/usr/bin/env python3
"""Smoke test: monogram substitution works in Regular and Ring, and leaves other text alone."""
import os
import sys

import uharfbuzz as hb

CASES = {
    "<RV>": ["monogram.ring", "R.mono2l", "V.mono2r", "monogram.end"],
    "<jfk>": ["monogram.ring", "J.mono3l", "F.mono3c", "K.mono3r", "monogram.end"],
    "<Q>": ["monogram.ring", "Q.mono1", "monogram.end"],
    "<ABCD>": ["less", "A", "B", "C", "D", "greater"],
    "a < b > c": ["a", "space", "less", "space", "b", "space", "greater", "space", "c"],
}


def shape(path, text):
    font = hb.Font(hb.Face(hb.Blob.from_file_path(path)))
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf)
    return [font.glyph_to_string(i.codepoint) for i in buf.glyph_infos]


def main(folder):
    bad = 0
    for fn in ("Rondel-Regular.ttf", "RondelRing-Regular.ttf"):
        for text, want in CASES.items():
            got = shape(os.path.join(folder, fn), text)
            if got != want:
                bad += 1
                print(f"FAIL {fn} {text!r}: {got}")
    print("monograms ok" if not bad else f"{bad} monogram check(s) failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "fonts/ttf"))
