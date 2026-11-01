#!/usr/bin/env python3
"""Add a STAT table to the static fonts (gftools builder only builds STAT for variable
fonts) and refresh their WOFF2 copies. Usage: add_stat.py fonts/ttf fonts/webfonts"""
import glob
import os
import sys

from fontTools.otlLib.builder import buildStatTable
from fontTools.ttLib import TTFont

ttf_dir, web_dir = sys.argv[1], sys.argv[2]
for path in sorted(glob.glob(os.path.join(ttf_dir, "*.ttf"))):
    font = TTFont(path)
    buildStatTable(font, [
        dict(tag="wght", name="Weight", values=[dict(value=400, name="Regular", flags=0x2)]),
        dict(tag="ital", name="Italic", values=[dict(value=0, name="Roman", flags=0x2, linkedValue=1)]),
    ], elidedFallbackName=2, macNames=False)
    font.save(path)
    font.flavor = "woff2"
    font.save(os.path.join(web_dir, os.path.basename(path)[:-4] + ".woff2"))
    print("STAT added:", path)
