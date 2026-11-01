#!/usr/bin/env python3
"""Text proof with HarfBuzz shaping (applies kerning) + FreeType rasterising."""
import sys
import uharfbuzz as hb
import freetype
from PIL import Image

NAVY, OFF = (20, 30, 56), (246, 242, 233)


def line(fontpath, text, px, kern=True, color=NAVY, bg=OFF, pad=24):
    blob = hb.Blob.from_file_path(fontpath)
    face = hb.Face(blob)
    font = hb.Font(face)
    upm = face.upem
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(font, buf, {"kern": kern})
    ft = freetype.Face(fontpath)
    ft.set_pixel_sizes(0, px)
    sc = px / upm
    width = sum(p.x_advance for p in buf.glyph_positions) * sc + 2 * pad
    asc, desc = 780 * sc, 300 * sc
    img = Image.new("RGB", (int(width) + 2, int(asc + desc + 2 * pad)), bg)
    x, base = pad, pad + asc
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        ft.load_glyph(info.codepoint, freetype.FT_LOAD_RENDER)
        g = ft.glyph
        bm = g.bitmap
        if bm.width and bm.rows:
            buf_ = bytes(bytearray(b for r in range(bm.rows) for b in bm.buffer[r * bm.pitch:r * bm.pitch + bm.width]))
            m = Image.frombytes("L", (bm.width, bm.rows), buf_)
            img.paste(Image.new("RGB", m.size, color), (int(x + pos.x_offset * sc + g.bitmap_left), int(base - g.bitmap_top)), m)
        x += pos.x_advance * sc
    return img


def stack(imgs, bg=OFF):
    out = Image.new("RGB", (max(i.width for i in imgs), sum(i.height for i in imgs)), bg)
    y = 0
    for i in imgs:
        out.paste(i, (0, y)); y += i.height
    return out


if __name__ == "__main__":
    fp, out, px = sys.argv[1], sys.argv[2], int(sys.argv[3])
    texts = sys.argv[4:]
    imgs = []
    for t in texts:
        imgs.append(line(fp, t, px, kern=False))
        imgs.append(line(fp, t, px, kern=True))
    stack(imgs).save(out)
    print(out)
