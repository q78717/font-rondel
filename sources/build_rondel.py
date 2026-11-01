#!/usr/bin/env python3
"""
Rondel — a typeface in which every glyph lives inside one circle of fixed radius.

Construction rules
  * Every glyph is drawn from only two primitives: arcs of circles and straight lines.
  * All glyphs share the same bounding circle (same radius, same centre).
  * Strokes that run to the edge are trimmed by that circle, so every outer terminal
    is a curved cut lying on the shared circle (the family's signature motif).
  * Capitals and figures fill the circle. Lowercase is built on a concentric
    "x-circle" of half the radius; ascenders and descenders run out to the outer circle.

Styles
  Rondel        open glyphs, proportional spacing, kerned, with monograms
  Rondel Ring   glyph set inside a visible ring, monospaced cells, with monograms
  Rondel Coin   glyph knocked out of a solid disc, monospaced cells

Monograms
  Typing 1-3 letters between < and >, e.g. <RV>, sets them as one medallion
  (contextual alternates / standard ligatures; Regular and Ring only).

Requires: see requirements.txt (fonttools, skia-pathops, numpy, brotli, ttfautohint-py)
Usage:    python3 build_rondel.py [--ttf DIR] [--woff2 DIR]      (both default to ./fonts)
          RONDEL_NO_HINT=1 skips ttfautohint.
"""
import math
import os
import sys

import pathops
from pathops import Path, PathOp, FillType
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.ttLib import TTFont

# --------------------------------------------------------------------------------------
# Design constants (unit space: centre-line radius of the bounding circle = 1)
# --------------------------------------------------------------------------------------
W = 0.17            # stroke width
R = 1.0             # bounding circle centre-line radius
D = R + W / 2       # ink radius of the bounding circle
XR = 0.5            # lowercase x-circle radius (concentric)
XB = XR + W / 2     # half height of the lowercase band (ink)
DOT = 0.7 * W       # dot radius (i, j, punctuation)
CUT = 3.0           # extension for strokes that run out and are trimmed by the circle
MITRE_LIMIT = 12.0
SEG = math.radians(22.5)   # max sweep per quadratic arc segment
EPS = 0.004         # clip disc is a hair larger than the outer ring to avoid coincident edges

S = 400             # font units per design unit
UPM = 1000
BASE = XB           # design y that maps to the baseline (bottom of lowercase band)
SB = 0.12           # side bearing (open style), design units

FAMILY = "Rondel"
VERSION = "1.500"
REPO_URL = "https://github.com/q78717/font-rondel"
COPYRIGHT = f"Copyright 2026 The Rondel Project Authors ({REPO_URL})"


def rad(a):
    return math.radians(a)


def pc(deg, r=1.0, c=(0.0, 0.0)):
    """Point on circle (c, r) at angle deg."""
    return (c[0] + r * math.cos(rad(deg)), c[1] + r * math.sin(rad(deg)))


def xat(p0, p1, y):
    """x on the line p0-p1 at height y."""
    t = (y - p0[1]) / (p1[1] - p0[1])
    return p0[0] + t * (p1[0] - p0[0])


def lerp(p0, p1, t):
    return (p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t)


def tangent_deg(c, r, q, cw):
    """Angle on circle (c, r) where travelling cw/ccw heads straight at external point q."""
    dx, dy = q[0] - c[0], q[1] - c[1]
    d = math.hypot(dx, dy)
    phi = math.atan2(dy, dx)
    alpha = math.acos(r / d)
    for th in (phi + alpha, phi - alpha):
        p = (c[0] + r * math.cos(th), c[1] + r * math.sin(th))
        v = (math.sin(th), -math.cos(th)) if cw else (-math.sin(th), math.cos(th))
        if v[0] * (q[0] - p[0]) + v[1] * (q[1] - p[1]) > 0:
            return math.degrees(th)
    raise ValueError("no tangent")


# --------------------------------------------------------------------------------------
# Path helpers (pathops / quadratic arcs)
# --------------------------------------------------------------------------------------
def _arc_to(path, cx, cy, r, a0, a1):
    """Append quadratic arc from angle a0 to a1 (radians) — pen must sit at the start."""
    sweep = a1 - a0
    n = max(1, int(math.ceil(abs(sweep) / SEG - 1e-9)))
    da = sweep / n
    rc = r / math.cos(da / 2)
    for i in range(n):
        t0 = a0 + i * da
        tm = t0 + da / 2
        t1 = t0 + da
        path.quadTo(cx + rc * math.cos(tm), cy + rc * math.sin(tm),
                    cx + r * math.cos(t1), cy + r * math.sin(t1))


def circle_path(cx, cy, r, ccw=True):
    p = Path()
    p.moveTo(cx + r, cy)
    _arc_to(p, cx, cy, r, 0, 2 * math.pi if ccw else -2 * math.pi)
    p.close()
    return p


def polygon_path(pts):
    p = Path()
    p.moveTo(*pts[0])
    for q in pts[1:]:
        p.lineTo(*q)
    p.close()
    return p


def union(a, b):
    return pathops.op(a, b, PathOp.UNION)


def intersect(a, b):
    return pathops.op(a, b, PathOp.INTERSECTION)


def difference(a, b):
    return pathops.op(a, b, PathOp.DIFFERENCE)


# --------------------------------------------------------------------------------------
# Glyph construction kit
# --------------------------------------------------------------------------------------
class G:
    """Collects stroked primitives in unit space. k scales the skeleton (not the stroke);
    sx/sy reflect it (used for mirrored and rotated glyphs)."""

    def __init__(self, k=1.0, sx=1, sy=1, prims=None, skel=None, o=(0.0, 0.0)):
        self.k, self.sx, self.sy = k, sx, sy
        self.o = o                                      # centre of this glyph's circle
        self.prims = prims if prims is not None else []
        self.skel = skel if skel is not None else []   # centre-line record, for specimens

    def flipped(self, sx=1, sy=1):
        return G(self.k, self.sx * sx, self.sy * sy, self.prims, self.skel, self.o)

    # transforms --------------------------------------------------------------------
    def T(self, p):
        return (self.o[0] + self.sx * self.k * p[0], self.o[1] + self.sy * self.k * p[1])

    def _ang(self, deg):
        th = rad(deg)
        return math.atan2(self.sy * math.sin(th), self.sx * math.cos(th))

    def _clip(self, clip):
        if clip is None:
            return None
        x0, y0, x1, y1 = clip
        a, b = self.T((x0, y0)), self.T((x1, y1))
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))

    @staticmethod
    def _ext(c):
        if c == "f":
            return 0.0
        if c == "s":
            return W / 2
        if c == "c":
            return CUT
        return float(c)

    def _add(self, path, clip):
        self.prims.append((path, self._clip(clip)))

    # primitives ----------------------------------------------------------------------
    def poly(self, pts, c0="f", c1="f", clip=None):
        """Straight polyline with mitre joins. Caps: f=flat, s=square, c=run out to circle."""
        P = [self.T(p) for p in pts]
        h = W / 2
        d = []
        for i in range(len(P) - 1):
            dx, dy = P[i + 1][0] - P[i][0], P[i + 1][1] - P[i][1]
            L = math.hypot(dx, dy)
            d.append((dx / L, dy / L))
        e0, e1 = self._ext(c0), self._ext(c1)
        P[0] = (P[0][0] - d[0][0] * e0, P[0][1] - d[0][1] * e0)
        P[-1] = (P[-1][0] + d[-1][0] * e1, P[-1][1] + d[-1][1] * e1)
        self.skel.append(("l", list(P)))

        def nrm(v):
            return (-v[1], v[0])

        left, right = [], []
        n0 = nrm(d[0])
        left.append((P[0][0] + n0[0] * h, P[0][1] + n0[1] * h))
        right.append((P[0][0] - n0[0] * h, P[0][1] - n0[1] * h))
        for i in range(1, len(P) - 1):
            na, nb = nrm(d[i - 1]), nrm(d[i])
            mx, my = na[0] + nb[0], na[1] + nb[1]
            ml = math.hypot(mx, my)
            mx, my = mx / ml, my / ml
            cosv = mx * nb[0] + my * nb[1]
            ln = h / cosv
            p = P[i]
            if 1 / cosv > MITRE_LIMIT:
                turn = d[i - 1][0] * d[i][1] - d[i - 1][1] * d[i][0]
                if turn > 0:
                    left.append((p[0] + mx * ln, p[1] + my * ln))
                    right += [(p[0] - na[0] * h, p[1] - na[1] * h), (p[0] - nb[0] * h, p[1] - nb[1] * h)]
                else:
                    left += [(p[0] + na[0] * h, p[1] + na[1] * h), (p[0] + nb[0] * h, p[1] + nb[1] * h)]
                    right.append((p[0] - mx * ln, p[1] - my * ln))
            else:
                left.append((p[0] + mx * ln, p[1] + my * ln))
                right.append((p[0] - mx * ln, p[1] - my * ln))
        nl = nrm(d[-1])
        left.append((P[-1][0] + nl[0] * h, P[-1][1] + nl[1] * h))
        right.append((P[-1][0] - nl[0] * h, P[-1][1] - nl[1] * h))
        self._add(polygon_path(left + right[::-1]), clip)

    def line(self, p0, p1, c0="f", c1="f", clip=None):
        self.poly([p0, p1], c0, c1, clip)

    def arc(self, c, r, a0, a1, c0="f", c1="f", clip=None):
        """Arc of circle (c, r) from angle a0 to a1 in degrees (a1 > a0 = counter-clockwise)."""
        cx, cy = self.T(c)
        rr = r * self.k
        s0 = self._ang(a0)
        sweep = rad(a1 - a0) * self.sx * self.sy
        sg = 1 if sweep > 0 else -1
        e0 = rr * rad(25) if c0 == "c" else self._ext(c0)
        e1 = rr * rad(25) if c1 == "c" else self._ext(c1)
        s0 -= sg * e0 / rr
        s1 = s0 + sweep + sg * (e0 + e1) / rr
        ro, ri = rr + W / 2, rr - W / 2
        self.skel.append(("a", cx, cy, rr, s0, s1))
        p = Path()
        p.moveTo(cx + ro * math.cos(s0), cy + ro * math.sin(s0))
        _arc_to(p, cx, cy, ro, s0, s1)
        p.lineTo(cx + ri * math.cos(s1), cy + ri * math.sin(s1))
        _arc_to(p, cx, cy, ri, s1, s0)
        p.close()
        self._add(p, clip)

    def corner(self, p, u, v):
        """Mitre-fill the outside of a corner at p: one stroke arrives along u, the next leaves along v."""
        P = self.T(p)

        def tv(d):
            x, y = self.sx * d[0], self.sy * d[1]
            L = math.hypot(x, y)
            return (x / L, y / L)

        u, v = tv(u), tv(v)
        h = W / 2
        cr = u[0] * v[1] - u[1] * v[0]
        s = -1 if cr > 0 else 1
        nu, nv = (-u[1] * s, u[0] * s), (-v[1] * s, v[0] * s)
        a = (P[0] + nu[0] * h, P[1] + nu[1] * h)
        b = (P[0] + nv[0] * h, P[1] + nv[1] * h)
        t = ((b[0] - a[0]) * v[1] - (b[1] - a[1]) * v[0]) / cr
        M = (a[0] + u[0] * t, a[1] + u[1] * t)
        self._add(polygon_path([P, a, M, b]), None)

    def vlimit(self, h):
        """Trim the whole glyph to |y| <= h (scaled with the skeleton)."""
        self.prims.append((None, ("V", h)))

    def ring(self, c, r, clip=None):
        cx, cy = self.T(c)
        rr = r * self.k
        self.skel.append(("c", cx, cy, rr))
        p = Path(fillType=FillType.EVEN_ODD)
        for rad_, ccw in ((rr + W / 2, True), (rr - W / 2, False)):
            p.moveTo(cx + rad_, cy)
            _arc_to(p, cx, cy, rad_, 0, 2 * math.pi if ccw else -2 * math.pi)
            p.close()
        self._add(p, clip)

    def dot(self, c, r=DOT):
        cx, cy = self.T(c)
        self.skel.append(("d", cx, cy, r))
        self._add(circle_path(cx, cy, r), None)

    # assemble ------------------------------------------------------------------------
    def outline(self):
        out = Path()
        vlim = None
        for path, clip in self.prims:
            if path is None:
                vlim = clip[1] * self.k + (W / 2) * (1 - self.k)
                continue
            if clip is not None:
                x0, y0, x1, y1 = clip
                path = intersect(path, polygon_path([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
            out = union(out, path)
        disc = circle_path(self.o[0], self.o[1], self.k * R + W / 2 + EPS)
        out = intersect(out, disc)
        if vlim is not None:
            y0, y1 = self.o[1] - vlim, self.o[1] + vlim
            out = intersect(out, polygon_path([(-3, y0), (3, y0), (3, y1), (-3, y1)]))
        return out


# --------------------------------------------------------------------------------------
# Glyph definitions
# --------------------------------------------------------------------------------------
GLYPHS = {}   # name -> (codepoint, function)


def glyph(name, uni=None):
    def deco(f):
        GLYPHS[name] = (uni, f)
        return f
    return deco


def mirror(fn, sx=-1, sy=1):
    return lambda g: fn(g.flipped(sx, sy))


# ---- arc helpers ---------------------------------------------------------------------
def ang(p, c=(0.0, 0.0)):
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0]))


def arc_circle(p0, p1, sag):
    """Centre and radius of the arc from p0 to p1 bulging by `sag` left of travel."""
    mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    L = math.hypot(dx, dy)
    nx, ny = -dy / L, dx / L
    h, s_ = L / 2, abs(sag)
    r = (h * h + s_ * s_) / (2 * s_)
    sg = 1 if sag > 0 else -1
    return (mx - nx * (r - s_) * sg, my - ny * (r - s_) * sg), r


def _sweep(a0, a1, cw):
    if cw:
        while a1 > a0:
            a1 -= 360
        while a1 <= a0 - 360:
            a1 += 360
    else:
        while a1 < a0:
            a1 += 360
        while a1 >= a0 + 360:
            a1 -= 360
    return a1


def arc3(g, p0, p1, sag, c0="f", c1="f", clip=None):
    """Arc from p0 to p1 bulging by `sag` to the left of travel (negative = right).
    Returns (centre, radius, a0, a1)."""
    c, r = arc_circle(p0, p1, sag)
    a0 = ang(p0, c)
    a1 = _sweep(a0, ang(p1, c), cw=sag > 0)
    g.arc(c, r, a0, a1, c0, c1, clip)
    return c, r, a0, a1


def arc_dir(a, cw):
    """Unit travel direction along a circle at angle a (degrees)."""
    t = rad(a)
    return (math.sin(t), -math.cos(t)) if cw else (-math.sin(t), math.cos(t))


def end_dir(c, a0, a1):
    return arc_dir(a1, a1 < a0)


def start_dir(c, a0, a1):
    return arc_dir(a0, a1 < a0)


def tarc(g, p, d, q, c0="f", c1="f", clip=None):
    """Arc leaving p in direction d (tangent) and ending at q. Returns (centre, r, a0, a1)."""
    L = math.hypot(*d)
    d = (d[0] / L, d[1] / L)
    n = (-d[1], d[0])
    v = (q[0] - p[0], q[1] - p[1])
    den = 2 * (n[0] * v[0] + n[1] * v[1])
    if abs(den) < 1e-9:
        g.line(p, q, c0, c1, clip)
        return None
    s_ = (v[0] ** 2 + v[1] ** 2) / den
    c = (p[0] + n[0] * s_, p[1] + n[1] * s_)
    r = abs(s_)
    a0 = ang(p, c)
    a1 = _sweep(a0, ang(q, c), cw=s_ < 0)
    g.arc(c, r, a0, a1, c0, c1, clip)
    return c, r, a0, a1


def _unit(v):
    L = math.hypot(*v)
    return (v[0] / L, v[1] / L)


def biarc(g, p0, t0, p1, t1, c0="f", c1="f", clip=None):
    """Two tangent arcs: leave p0 heading t0, arrive at p1 heading t1."""
    t0, t1 = _unit(t0), _unit(t1)
    v = sub(p1, p0)
    t = (t0[0] + t1[0], t0[1] + t1[1])
    vt = v[0] * t[0] + v[1] * t[1]
    vv = v[0] ** 2 + v[1] ** 2
    den = 2 * (1 - (t0[0] * t1[0] + t0[1] * t1[1]))
    if abs(den) < 1e-6:
        d = vv / (4 * (v[0] * t1[0] + v[1] * t1[1]))
    else:
        d = (-vt + math.sqrt(vt * vt + den * vv)) / den
    pm = ((p0[0] + d * t0[0] + p1[0] - d * t1[0]) / 2, (p0[1] + d * t0[1] + p1[1] - d * t1[1]) / 2)
    r1 = tarc(g, p0, t0, pm, c0, "f", clip)
    dm = end_dir(r1[0], r1[2], r1[3]) if r1 else _unit(sub(pm, p0))
    return r1, tarc(g, pm, dm, p1, "f", c1, clip)


def s_shape(g, r, cy, t_end=25, spine=-35):
    """S: a bowl of radius r round to its leftmost point, then a biarc down to the centre,
    arriving on a diagonal; the lower half is the point reflection, so the curve never breaks."""
    th = rad(spine)
    for h in (g, g.flipped(-1, -1)):
        cu = (0, cy)
        h.arc(cu, r, t_end, 180)
        biarc(h, pc(180, r, cu), (0, -1), (0.0, 0.0), (math.cos(th), math.sin(th)))


def pointed(g, p_left, p_right, apex, sag, c_out):
    """Two bowed strokes meeting in a mitred point (lancet arch / inverted arch)."""
    _, _, a0l, a1l = arc3(g, p_left, apex, sag, c_out, "f")
    _, _, a0r, a1r = arc3(g, p_right, apex, -sag, c_out, "f")
    ul = arc_dir(a1l, a1l < a0l)
    ur = arc_dir(a1r, a1r < a0r)
    g.corner(apex, ul, (-ur[0], -ur[1]))


def circles_meet(c0, r0, c1, r1):
    dx, dy = c1[0] - c0[0], c1[1] - c0[1]
    d = math.hypot(dx, dy)
    a = (r0 * r0 - r1 * r1 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, r0 * r0 - a * a))
    mx, my = c0[0] + a * dx / d, c0[1] + a * dy / d
    return ((mx + h * dy / d, my - h * dx / d), (mx - h * dy / d, my + h * dx / d))


def x_on(c, r, y, side):
    return c[0] + side * math.sqrt(r * r - (y - c[1]) ** 2)


def neg(v):
    return (-v[0], -v[1])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


# ---- the bowed left stem (B D E F K L P R) ---------------------------------------------
STEM_X = -0.55          # leftmost point of the stem
STEM_R = 3.0            # radius of the circle the stem is cut from
STEM_C = (STEM_X + STEM_R, 0.0)
_sx = (1 + STEM_C[0] ** 2 - STEM_R ** 2) / (2 * STEM_C[0])
STEM_TOP = (_sx, math.sqrt(1 - _sx * _sx))      # where the stem meets the circle
STEM_A = ang(STEM_TOP)
_sa = math.degrees(math.asin(STEM_TOP[1] / STEM_R))
LANCET = 0.14
LC_LANCET = 0.085


def stem_x(y):
    return STEM_C[0] - math.sqrt(STEM_R ** 2 - y * y)


def left_stem(g):
    g.arc(STEM_C, STEM_R, 180 - _sa, 180 + _sa, "c", "c")


# ---- figures: one height for all ------------------------------------------------------
FIG_H = 0.985           # ink half-height of every figure
FIG_R = FIG_H - W / 2   # figures' own concentric circle (centre line)


# ---- shared skeletons ----------------------------------------------------------------
def s_curve(g, r):
    g.arc((0, r), r, 30, 270)
    g.arc((0, -r), r, 90, -150)


def six(g):
    g.vlimit(FIG_H)
    c, r = (0, -FIG_R + 0.5), 0.5
    g.ring(c, r)
    R6 = 1.35
    g.arc((c[0] - r + R6, c[1]), R6, 180, 100, "f", "c")


def lc_n(g):
    g.line((-XR, -XB), (-XR, XB))
    g.arc((0, 0), XR, 180, 0)
    g.line((XR, 0), (XR, -XB))


def lc_b(g):
    g.ring((0, 0), XR)
    g.line((-XR, -XB), (-XR, 2), "f", "c")


def lc_p(g):
    g.ring((0, 0), XR)
    g.line((-XR, XB), (-XR, -2), "f", "c")


def paren(g):
    c_, r_ = (0.78, 0.0), 1.15
    t = ang(max(circles_meet(c_, r_, (0, 0), 1), key=lambda q: q[1]), c_)
    g.arc(c_, r_, t, 360 - t, "c", "c")



def brace(g):
    for h in (g, g.flipped(1, -1)):
        tip, mid, nib = (0.25, 0.9), (0.0, 0.46), (-0.3, 0.0)
        biarc(h, tip, (-1, 0), mid, (0, -1))
        biarc(h, mid, (0, -1), nib, (-1, 0))



def bracket(g):
    xm, rs = -0.24, 3.4                    # bowed stem: leftmost x, radius
    sc = (xm + rs, 0.0)
    top = max(circles_meet(sc, rs, (0, 0), 1), key=lambda q: q[1])
    a = math.degrees(math.asin(top[1] / rs))
    g.arc(sc, rs, 180 - a, 180 + a, "c", "c")
    at = ang(top)
    g.arc((0, 0), 1, at, 60)
    g.arc((0, 0), 1, -at, -60)



def angle_lt(g):
    pointed(g, (0.45, 0.42), (0.45, -0.42), (-0.45, 0.0), -0.1, "f")


def tail_l(g, x, top, c0):
    """Stem with a right-turning tail that lands on the baseline."""
    r = 0.3
    yb = -XR + r
    g.line((x, top), (x, yb), c0, "f")
    g.arc((x + r, yb), r, 180, 270)


def xbar(g, xc):
    """Lowercase crossbar: a short arc of an x-circle-sized circle, top at x-height."""
    g.arc((xc, 0), XR, 138, 42)


# ---- capitals ------------------------------------------------------------------------
@glyph("A", 0x41)
def A(g):
    fl, fr, ap = pc(-124), pc(-56), (0, 1.0)
    pointed(g, fl, fr, ap, LANCET, "c")
    # crossbar swung from the apex
    cl, rl = arc_circle(fl, ap, LANCET)
    rb = 1.3
    p = min(circles_meet(ap, rb, cl, rl), key=lambda q: abs(q[1] + 0.25) + (q[0] > 0))
    a0 = ang(p, ap) % 360
    g.arc(ap, rb, a0, 540 - a0)


@glyph("B", 0x42)
def B(g):
    left_stem(g)
    cu = -0.05
    ru = (1 - cu * cu) / 2
    tu = ang((cu, ru))
    g.arc((0, 0), 1, STEM_A, tu)
    g.arc((cu, ru), ru, tu, -90)
    g.arc((0.0, -0.5), 0.5, 90, -90)
    g.arc((0, 0), 1, -90, -STEM_A)
    g.line((STEM_X, 0), (0.0, 0))


@glyph("C", 0x43)
def C(g):
    g.arc((0, 0), 1, 45, 315)


@glyph("D", 0x44)
def D_(g):
    left_stem(g)
    g.arc((0, 0), 1, STEM_A, -STEM_A)


@glyph("E", 0x45)
def E(g):
    left_stem(g)
    g.arc((0, 0), 1, STEM_A, 40)
    g.arc((0, 0), 1, -STEM_A, -40)
    g.line((STEM_X, 0.0), (0.42, 0.0))


@glyph("F", 0x46)
def F(g):
    left_stem(g)
    g.arc((0, 0), 1, STEM_A, 40)
    g.line((STEM_X, 0.0), (0.42, 0.0))


@glyph("G", 0x47)
def G_(g):
    g.arc((0, 0), 1, 38, 360)
    g.line((0.1, 0), (1, 0), "f", "c")


@glyph("H", 0x48)
def H(g):
    g.arc((0, 0), 1, 180 - 58, 180 + 58)
    g.arc((0, 0), 1, -58, 58)
    g.line((-1, 0), (1, 0))


@glyph("I", 0x49)
def I(g):
    g.line((0, -1.2), (0, 1.2), "c", "c")
    g.arc((0, 0), 1, 72, 108)
    g.arc((0, 0), 1, 252, 288)


@glyph("J", 0x4A)
def J(g):
    x, cy = 0.52, -0.38
    rs = 6.0
    sc = (x - rs, cy)
    top = max(circles_meet(sc, rs, (0, 0), 1), key=lambda q: q[1])
    a_top = ang(top, sc)
    g.arc(sc, rs, a_top, 0, "c", "f")
    g.arc((0, cy), x, 0, -180)
    g.arc((0, 0), 1, ang(top), 106)


@glyph("K", 0x4B)
def K(g):
    left_stem(g)
    y0 = -0.1
    c, r, a0, a1 = arc3(g, (stem_x(y0), y0), pc(50), -0.09, "f", "c")
    j = pc(a0 + 0.36 * (a1 - a0), r, c)
    arc3(g, j, pc(-50), 0.09, "f", "c")


@glyph("L", 0x4C)
def L(g):
    left_stem(g)
    g.arc((0, 0), 1, -STEM_A, -40)


@glyph("M", 0x4D)
def M(g):
    t = 125
    g.arc((0, 0), 1, 360 - t, t)
    pointed(g, pc(t), pc(180 - t), (0, -0.3), -LANCET, "c")
    g.arc((0, 0), 1, 180 - t, t - 180)


@glyph("N", 0x4E)
def N(g):
    t = 123
    g.arc((0, 0), 1, 360 - t, t)
    arc3(g, pc(t), pc(t + 180), 0.17, "c", "c")
    g.arc((0, 0), 1, t + 180 - 360, 180 - t)


@glyph("O", 0x4F)
def O(g):
    g.ring((0, 0), 1)


def p_bowl(g, y_low):
    """P/R bowl: one arc from the stem top round to the stem at y_low."""
    p0, p1 = STEM_TOP, (stem_x(y_low), y_low)
    # solve the sag that makes the bowl reach x = 0.55
    lo, hi = 0.3, 1.4
    for _ in range(50):
        mid = (lo + hi) / 2
        c, r = arc_circle(p0, p1, mid)
        if c[0] + r > 0.55:
            hi = mid
        else:
            lo = mid
    return arc3(g, p0, p1, (lo + hi) / 2)


@glyph("P", 0x50)
def P_(g):
    left_stem(g)
    return p_bowl(g, -0.12)


@glyph("Q", 0x51)
def Q(g):
    g.ring((0, 0), 1)
    arc3(g, (0.12, -0.3), pc(-50), 0.12, "f", "c")


@glyph("R", 0x52)
def R_(g):
    c, r, _, _ = P_(g)
    start = (c[0], c[1] - r)
    arc3(g, start, pc(-58), 0.08, "f", "c")


@glyph("S", 0x53)
def S_(g):
    s_shape(g, 0.55, 1 - 0.55)


@glyph("T", 0x54)
def T_(g):
    g.arc((0, 0), 1, 132, 48)
    g.line((0, 1.2), (0, -1.2), "c", "c")


@glyph("U", 0x55)
def U(g):
    g.arc((0, 0), 1, 132, 408)


@glyph("V", 0x56)
def V(g):
    pointed(g, pc(127), pc(53), (0, -1.0), -LANCET, "c")


@glyph("W", 0x57)
def W_(g):
    tl, bl = 143, 246
    g.arc((0, 0), 1, tl, bl)
    pointed(g, pc(bl), pc(540 - bl), (0, 0.35), LANCET, "c")
    g.arc((0, 0), 1, 540 - bl, 540 - tl)


@glyph("X", 0x58)
def X(g):
    t = 125
    arc3(g, pc(t), pc(360 - t), 0.58, "c", "c")
    arc3(g, pc(180 - t), pc(t + 180), -0.58, "c", "c")


@glyph("Y", 0x59)
def Y(g):
    j = (0, 0.0)
    arc3(g, pc(127), j, -LANCET, "c", 0.06)
    arc3(g, pc(53), j, LANCET, "c", 0.06)
    g.line(j, (0, -1.2), "f", "c")


@glyph("Z", 0x5A)
def Z(g):
    t = 127
    g.arc((0, 0), 1, t, 180 - t)
    arc3(g, pc(180 - t), pc(360 - t), -0.12, "c", "c")
    g.arc((0, 0), 1, 360 - t, t + 180)


# ---- figures (all exactly FIG_H tall) ---------------------------------------------------
@glyph("zero", 0x30)
def zero(g):
    g.vlimit(FIG_H)
    hw, hh, re = 0.5, FIG_R, 0.38
    b = hh - re
    k_ = hw - re
    a = (b * b - k_ * k_) / (2 * k_)
    rs = a + hw
    t = ang((0, b), (a, 0))
    g.arc((a, 0), rs, t, 360 - t)
    g.arc((-a, 0), rs, t - 180, 180 - t)
    g.arc((0, b), re, t, 180 - t)
    g.arc((0, -b), re, 360 - t, 180 + t)


@glyph("one", 0x31)
def one(g):
    g.vlimit(FIG_H)
    x = 0.12
    g.line((x, 1.2), (x, -1.2), "c", "c")
    g.arc((0, 0), FIG_R, math.degrees(math.acos(x / FIG_R)), 145)


@glyph("two", 0x32)
def two(g):
    g.vlimit(FIG_H)
    c, r = (0, FIG_R - 0.5), 0.5
    q = pc(233.13, FIG_R)
    t = -12
    g.arc(c, r, 158, t)
    j = pc(t, r, c)
    res = tarc(g, j, arc_dir(t, True), q)
    d_in = end_dir(*([res[0]] + list(res[2:]))) if res else sub(q, j)
    g.arc((0, 0), FIG_R, 233.13, 318)
    g.corner(q, d_in, arc_dir(233.13, False))


@glyph("three", 0x33)
def three(g):
    g.vlimit(FIG_H)
    ru, rl = 0.43, 0.47
    cu, cl = (0, FIG_R - ru), (0, -FIG_R + rl)
    ym = FIG_R - 2 * ru
    g.arc(cu, ru, 150, -90)
    g.arc(cl, rl, 90, -150)
    arc3(g, (-0.2, ym + 0.03), (0, ym), 0.03)


@glyph("four", 0x34)
def four(g):
    g.vlimit(FIG_H)
    x = 0.3
    top = (x, FIG_R)
    g.line((x, -1.2), top, "c", "f")
    fc, fr = (-0.42, -0.1), 0.22          # rounded lower-left corner
    g.arc(fc, fr, 165, 270)
    f0, f1 = pc(165, fr, fc), pc(270, fr, fc)
    c, r, a0, a1 = tarc(g, f0, neg(arc_dir(165, False)), top)
    g.corner(top, (0, 1), neg(end_dir(c, a0, a1)))
    tarc(g, f1, (1, 0), (0.66, -0.25))



@glyph("five", 0x35)
def five(g):
    g.vlimit(FIG_H)
    x = -0.42
    a = math.degrees(math.acos(x / FIG_R))
    pt = pc(a, FIG_R)
    rb = 0.52
    cb = (0.02, -FIG_R + rb)
    ab = 145
    pb = pc(ab, rb, cb)
    g.arc((0, 0), FIG_R, a, 38)
    arc3(g, pt, pb, -0.07)
    g.arc(cb, rb, ab, -148)
    g.dot(pt, W / 2)
    g.dot(pb, W / 2)



@glyph("six", 0x36)
def six_(g):
    six(g)


@glyph("seven", 0x37)
def seven(g):
    g.vlimit(FIG_H)
    t = 127
    p = pc(180 - t, FIG_R)
    g.arc((0, 0), FIG_R, t, 180 - t)
    c, r, a0, a1 = arc3(g, p, (-0.22, -1.1), 0.1, "f", "c")
    g.corner(p, arc_dir(180 - t, True), start_dir(c, a0, a1))


@glyph("eight", 0x38)
def eight(g):
    g.vlimit(FIG_H)
    ru, rl = 0.42, 0.48
    g.ring((0, FIG_R - ru), ru)
    g.ring((0, -FIG_R + rl), rl)


GLYPHS["nine"] = (0x39, mirror(six, -1, -1))


# ---- lowercase -----------------------------------------------------------------------
@glyph("a", 0x61)
def a(g):
    g.ring((0, 0), XR)
    g.line((XR, -XB), (XR, XB))


GLYPHS["b"] = (0x62, lc_b)


@glyph("c", 0x63)
def c(g):
    g.arc((0, 0), XR, 45, 315)


GLYPHS["d"] = (0x64, mirror(lc_b))


@glyph("e", 0x65)
def e(g):
    g.line((-XR, 0), (XR + W / 2, 0))
    g.arc((0, 0), XR, 0, 315)


@glyph("f", 0x66)
def f(g):
    x = -0.15
    g.line((x, -XB), (x, 0.55))
    g.arc((x + 0.35, 0.55), 0.35, 180, 40)
    xbar(g, x + 0.04)


@glyph("g", 0x67)
def g_(g):
    g.ring((0, 0), XR)
    g.line((XR, XB), (XR, -0.45))
    g.arc((0, -0.45), XR, 0, -160)


@glyph("h", 0x68)
def h(g):
    g.line((-XR, -XB), (-XR, 2), "f", "c")
    g.arc((0, 0), XR, 180, 0)
    g.line((XR, 0), (XR, -XB))


@glyph("i", 0x69)
def i(g):
    g.line((0, -XB), (0, XB))
    g.dot((0, 0.82))


@glyph("j", 0x6A)
def j(g):
    x, cy, r = 0.15, -0.5, 0.35
    g.line((x, XB), (x, cy))
    g.arc((x - r, cy), r, 0, -180)
    g.dot((x, 0.82))


@glyph("k", 0x6B)
def k(g):
    band = (-2, -XB, 2, XB)
    g.line((-0.45, -XB), (-0.45, 2), "f", "c")
    c, r, a0, a1 = arc3(g, (-0.45, -0.12), (0.52, 0.78), -0.07, "f", "f", clip=band)
    jn = pc(a0 + 0.4 * (a1 - a0), r, c)
    arc3(g, jn, (0.58, -0.82), 0.07, "f", "f", clip=band)


@glyph("l", 0x6C)
def l(g):
    tail_l(g, -0.12, 2, "c")


@glyph("m", 0x6D)
def m(g):
    r, cy = 0.3, XB - W / 2 - 0.3
    g.line((-0.6, -XB), (-0.6, XB))
    g.arc((-0.3, cy), r, 180, 0)
    g.line((0, cy), (0, -XB))
    g.arc((0.3, cy), r, 180, 0)
    g.line((0.6, cy), (0.6, -XB))


GLYPHS["n"] = (0x6E, lc_n)


@glyph("o", 0x6F)
def o(g):
    g.ring((0, 0), XR)


GLYPHS["p"] = (0x70, lc_p)
GLYPHS["q"] = (0x71, mirror(lc_p))


@glyph("r", 0x72)
def r(g):
    x = -0.35
    g.line((x, -XB), (x, XB))
    g.arc((x + XR, 0), XR, 180, 55)


@glyph("s", 0x73)
def s(g):
    s_shape(g, 0.29, XR - 0.29, 28, -40)


@glyph("t", 0x74)
def t(g):
    x = -0.1
    tail_l(g, x, 0.86, "f")
    xbar(g, x + 0.03)


GLYPHS["u"] = (0x75, mirror(lc_n, -1, -1))


@glyph("v", 0x76)
def v(g):
    band = (-2, -XB, 2, XB)
    ap = (0, -XB)
    arc3(g, (-0.5, XB), ap, -LC_LANCET, 0.3, 0.2, clip=band)
    arc3(g, (0.5, XB), ap, LC_LANCET, 0.3, 0.2, clip=band)


@glyph("w", 0x77)
def w(g):
    band = (-2, -XB, 2, XB)
    rw = 0.75
    xb = math.sqrt(rw * rw - XB * XB)
    bl = ang((-xb, -XB)) + 360
    tl = 360 - bl
    g.arc((0, 0), rw, tl, bl, 0.3, 0.2, clip=band)
    arc3(g, (-xb, -XB - 0.1), (0, 0.32), -LC_LANCET, "f", 0.15, clip=band)
    arc3(g, (xb, -XB - 0.1), (0, 0.32), LC_LANCET, "f", 0.15, clip=band)
    g.arc((0, 0), rw, 540 - bl, 540 - tl, 0.2, 0.3, clip=band)


@glyph("x", 0x78)
def x(g):
    band = (-2, -XB, 2, XB)
    arc3(g, (-0.5, XB - 0.02), (-0.5, -XB + 0.02), 0.47, "f", "f", clip=band)
    arc3(g, (0.5, XB - 0.02), (0.5, -XB + 0.02), -0.47, "f", "f", clip=band)


@glyph("y", 0x79)
def y(g):
    top = (-2, -2, 2, XB)
    ap = (0, -XB)
    arc3(g, (-0.5, XB), ap, -LC_LANCET, 0.3, "f", clip=top)
    arc3(g, (0.5, XB), ap, LC_LANCET, 0.3, 1.3, clip=top)


@glyph("z", 0x7A)
def z(g):
    yb = XB - W / 2
    r = 0.8
    t = 128
    ct, cb = (0, yb - r), (0, r - yb)
    g.arc(ct, r, t, 180 - t)
    p0, p1 = pc(180 - t, r, ct), pc(360 - t, r, cb)
    c, rr, a0, a1 = arc3(g, p0, p1, 0.1)
    g.arc(cb, r, 360 - t, 180 + t)
    g.corner(p0, arc_dir(180 - t, True), start_dir(c, a0, a1))
    g.corner(p1, end_dir(c, a0, a1), arc_dir(360 - t, False))


# ---- punctuation & symbols ------------------------------------------------------------
BASE_DOT = (0, -XB + DOT)


@glyph("space", 0x20)
def space(g):
    pass


@glyph("exclam", 0x21)
def exclam(g):
    arc3(g, (0.06, 1.15), (0, -0.18), 0.07, "f", "f")
    g.dot(BASE_DOT)


def tick(g, x):
    arc3(g, (x + 0.05, 1.12), (x - 0.04, 0.56), 0.09)


@glyph("quotedbl", 0x22)
def quotedbl(g):
    tick(g, -0.17)
    tick(g, 0.17)


@glyph("numbersign", 0x23)
def numbersign(g):
    for v_ in (-0.3, 0.3):
        y_ = math.sqrt(1 - v_ * v_)
        sg = 1 if v_ > 0 else -1
        arc3(g, (v_, y_), (v_, -y_), sg * 0.09, "c", "c")       # verticals bow outward
        arc3(g, (-y_, v_), (y_, v_), sg * 0.09, "c", "c")       # horizontals bow outward


@glyph("dollar", 0x24)
def dollar(g):
    s_shape(g, 0.47, 0.9 - 0.47)
    g.line((0, -1.2), (0, 1.2), "c", "c")


@glyph("percent", 0x25)
def percent(g):
    g.ring((-0.42, 0.42), 0.24)
    g.ring((0.42, -0.42), 0.24)
    arc3(g, pc(225), pc(45), 0.12, "c", "c")


@glyph("ampersand", 0x26)
def ampersand(g):
    lc, lr = (-0.06, 0.5), 0.3
    g.arc(lc, lr, -32, 214)
    p2 = pc(214, lr, lc)
    tarc(g, p2, arc_dir(214, False), (0.84, -0.98), "f", "c")
    cb, rb = (-0.08, -0.4), 0.46
    b0, b1 = 128, 368
    g.arc(cb, rb, b0, b1)
    biarc(g, pc(-32, lr, lc), arc_dir(-32, True), pc(b0, rb, cb), arc_dir(b0, False))
    tarc(g, pc(b1, rb, cb), arc_dir(b1, False), (0.64, 0.04))



@glyph("quotesingle", 0x27)
def quotesingle(g):
    tick(g, 0)


GLYPHS["parenleft"] = (0x28, paren)
GLYPHS["parenright"] = (0x29, mirror(paren))


@glyph("asterisk", 0x2A)
def asterisk(g):
    c_, rr = (0, 0.42), 0.37
    for k_ in range(6):
        arc3(g, c_, pc(90 + 60 * k_, rr, c_), 0.085)
    g.dot(c_, W * 0.62)



@glyph("plus", 0x2B)
def plus(g):
    g.line((-0.45, 0), (0.45, 0))
    g.line((0, -0.45), (0, 0.45))


@glyph("comma", 0x2C)
def comma(g):
    g.dot(BASE_DOT)
    arc3(g, (0.04, BASE_DOT[1]), (-0.16, -0.84), -0.06)


@glyph("hyphen", 0x2D)
def hyphen(g):
    g.line((-0.35, 0), (0.35, 0))


@glyph("period", 0x2E)
def period(g):
    g.dot(BASE_DOT)


@glyph("slash", 0x2F)
def slash(g):
    arc3(g, pc(245), pc(65), 0.12, "c", "c")


@glyph("colon", 0x3A)
def colon(g):
    g.dot(BASE_DOT)
    g.dot((0, XB - DOT))


@glyph("semicolon", 0x3B)
def semicolon(g):
    comma(g)
    g.dot((0, XB - DOT))


GLYPHS["less"] = (0x3C, angle_lt)


@glyph("equal", 0x3D)
def equal(g):
    g.line((-0.45, 0.2), (0.45, 0.2))
    g.line((-0.45, -0.2), (0.45, -0.2))


GLYPHS["greater"] = (0x3E, mirror(angle_lt))


@glyph("question", 0x3F)
def question(g):
    c_, rr = (0, 0.5), 0.4
    t = -42
    g.arc(c_, rr, 168, t)
    biarc(g, pc(t, rr, c_), arc_dir(t, True), (0, -0.18), (0, -1))
    g.dot(BASE_DOT)



@glyph("at", 0x40)
def at(g):
    rb = 0.36
    g.ring((0, 0), rb)
    yb = -0.15
    g.line((rb, rb + W / 2), (rb, yb))
    ro = 0.9
    xo = math.sqrt(ro * ro - yb * yb)
    rh = (xo - rb) / 2
    g.arc((rb + rh, yb), rh, 180, 360)
    a0 = math.degrees(math.atan2(yb, xo))
    g.arc((0, 0), ro, a0, 290)


GLYPHS["bracketleft"] = (0x5B, bracket)


@glyph("backslash", 0x5C)
def backslash(g):
    arc3(g, pc(295), pc(115), -0.12, "c", "c")


GLYPHS["bracketright"] = (0x5D, mirror(bracket))


@glyph("asciicircum", 0x5E)
def asciicircum(g):
    pointed(g, (-0.4, 0.38), (0.4, 0.38), (0, 0.86), 0.06, "f")


@glyph("underscore", 0x5F)
def underscore(g):
    g.arc((0, 0), 1, 244, 296)


@glyph("grave", 0x60)
def grave(g):
    arc3(g, (-0.2, 0.92), (0.14, 0.66), 0.06)


GLYPHS["braceleft"] = (0x7B, brace)


@glyph("bar", 0x7C)
def bar(g):
    g.line((0, -1.2), (0, 1.2), "c", "c")


GLYPHS["braceright"] = (0x7D, mirror(brace))


@glyph("asciitilde", 0x7E)
def asciitilde(g):
    rr = 0.27
    g.arc((-rr, 0), rr, 208, 0)
    g.arc((rr, 0), rr, 180, 388)



@glyph(".notdef")
def notdef(g):
    g.ring((0, 0), 1)
    g.line(pc(225), pc(45), "c", "c")


# --------------------------------------------------------------------------------------
# Styles and font assembly
# --------------------------------------------------------------------------------------
STYLES = {
    # name: (k, mode)
    "Regular": dict(k=1.0, mode="open"),
    "Ring": dict(k=0.70, mode="ring"),
    "Coin": dict(k=0.74, mode="coin"),
}
CELL_SB = 0.08


def glyph_outline(name, k, mode):
    _, fn = GLYPHS[name]
    g = G(k=k)
    fn(g)
    body = g.outline() if g.prims else Path()
    if mode == "ring":
        frame = Path(fillType=FillType.EVEN_ODD)
        for rad_, ccw in ((D, True), (D - W, False)):
            frame.moveTo(rad_, 0)
            _arc_to(frame, 0, 0, rad_, 0, 2 * math.pi if ccw else -2 * math.pi)
            frame.close()
        body = union(frame, body) if g.prims else pathops.op(frame, Path(), PathOp.UNION)
    elif mode == "coin":
        disc = circle_path(0, 0, D)
        body = difference(disc, body) if g.prims else pathops.op(disc, Path(), PathOp.UNION)
    return body


def path_bounds(path):
    bp = BoundsPen(None)
    path.draw(bp)
    return bp.bounds


def draw_ttglyph(path, dx, dy):
    pen = TTGlyphPen(None)
    tp = TransformPen(Cu2QuPen(pen, 0.5, reverse_direction=False), (S, 0, 0, S, dx, dy))
    path.draw(tp)
    return pen.glyph()


def signed_area(path):
    from fontTools.pens.areaPen import AreaPen
    ap = AreaPen()
    path.draw(ap)
    return ap.value


# --------------------------------------------------------------------------------------
# Monograms: type <RV> (1-3 letters between < and >) to get a medallion.
# Each letter is drawn in its own circle; all circles in one monogram share a radius and
# overlap like a Venn diagram, and the outer ring is the font's bounding circle, so
# strokes that reach a letter's circle where it touches the ring fuse into it.
# Built as zero-width overlay glyphs placed by contextual substitution (calt + liga).
# --------------------------------------------------------------------------------------
MONO_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MONO_SLOTS = {
    # slot: (scale, centre offset) in units of the bounding-circle radius
    "mono1": (0.70, (0.0, 0.0)),                    # single letter, clear of the ring
    "mono2l": (2 / 3, (-1 / 3, 0.0)),               # vesica piscis: each centre on the
    "mono2r": (2 / 3, (1 / 3, 0.0)),                # other circle, both tangent to the ring
    "mono3l": (0.5, (-0.5, 0.0)),                   # three circles, each through its
    "mono3c": (0.5, (0.0, 0.0)),                    # neighbour's centre
    "mono3r": (0.5, (0.5, 0.0)),
}
MONO_MODES = ("open", "ring")    # Coin cannot knock letters out of a disc across glyphs


def mono_ring_path():
    g = G()
    g.ring((0, 0), R)
    return g.outline()


def mono_letter_path(letter, slot):
    k, o = MONO_SLOTS[slot]
    g = G(k=k, o=o)
    GLYPHS[letter][1](g)
    return g.outline()


def mono_cell(mode):
    """(advance of the ring glyph, x of the medallion centre) in font units."""
    if mode == "open":
        return round((2 * D + 2 * SB) * S), (SB + D) * S
    cell = round((2 * D + 2 * CELL_SB) * S)
    return cell, (D + CELL_SB) * S


def mono_glyphs(mode):
    """-> list of (name, path, dx, advance)."""
    adv, cx = mono_cell(mode)
    out = [("monogram.ring", mono_ring_path(), cx, adv), ("monogram.end", Path(), 0, 0)]
    for L in MONO_LETTERS:
        for slot in MONO_SLOTS:
            out.append((f"{L}.{slot}", mono_letter_path(L, slot), cx - adv, 0))
    return out


def mono_fea():
    ins = " ".join(list(MONO_LETTERS) + list(MONO_LETTERS.lower()))
    lines = [f"@MONO_IN = [{ins}];"]
    for slot in MONO_SLOTS:
        body = " ".join(f"sub {c} by {c.upper()}.{slot};" for c in MONO_LETTERS + MONO_LETTERS.lower())
        lines.append(f"lookup MONO_{slot.upper()} {{ {body} }} MONO_{slot.upper()};")
    lines.append("lookup MONO_OPEN { sub less by monogram.ring; } MONO_OPEN;")
    lines.append("lookup MONO_CLOSE { sub greater by monogram.end; } MONO_CLOSE;")
    lt = "less' lookup MONO_OPEN"
    gt = "greater' lookup MONO_CLOSE"
    lines.append("lookup MONOGRAM {")
    lines.append(f"  sub {lt} @MONO_IN' lookup MONO_MONO3L @MONO_IN' lookup MONO_MONO3C @MONO_IN' lookup MONO_MONO3R {gt};")
    lines.append(f"  sub {lt} @MONO_IN' lookup MONO_MONO2L @MONO_IN' lookup MONO_MONO2R {gt};")
    lines.append(f"  sub {lt} @MONO_IN' lookup MONO_MONO1 {gt};")
    lines.append("} MONOGRAM;")
    lines.append("feature calt { lookup MONOGRAM; } calt;")
    lines.append("feature liga { lookup MONOGRAM; } liga;")
    return "\n".join(lines) + "\n"


def _panose(proportion):
    from fontTools.ttLib.tables.O_S_2f_2 import Panose
    p = Panose()
    p.bFamilyType, p.bSerifStyle, p.bWeight, p.bProportion = 2, 11, 5, proportion
    p.bContrast = p.bStrokeVariation = p.bArmStyle = p.bLetterForm = p.bMidline = p.bXHeight = 0
    return p


PANOSE_PROP = _panose(3)      # Latin text, sans, book weight, modern proportions
PANOSE_MONO = _panose(9)      # same, monospaced

MATH_SIGNS = ["plus", "less", "equal", "greater", "asciitilde"]


def equalize_math_signs(font):
    """Give the math signs one shared advance, each glyph centred in it."""
    hmtx, glyf = font["hmtx"], font["glyf"]
    names = [n for n in MATH_SIGNS if n in hmtx.metrics]
    adv = max(hmtx[n][0] for n in names)
    for n in names:
        g = glyf[n]
        w = g.xMax - g.xMin
        lsb = round((adv - w) / 2)
        shift = lsb - g.xMin
        if shift:
            g.coordinates.translate((shift, 0))
            g.recalcBounds(glyf)
        hmtx[n] = (adv, g.xMin)


def autohint(path):
    """Hint with ttfautohint (also turns on smart dropout control)."""
    from ttfautohint import ttfautohint
    tmp = path + ".hinted"
    ttfautohint(in_file=path, out_file=tmp)
    f = TTFont(tmp)
    f["head"].flags |= 1 << 3          # hinted fonts must round ppem to integers
    f.save(path)
    os.remove(tmp)


def drop_zero_length_segments(glyf, order):
    """Remove repeated on-curve points (zero-length segments left by coordinate rounding)."""
    from fontTools.ttLib.tables._g_l_y_f import GlyphCoordinates
    for name in order:
        g = glyf[name]
        if g.numberOfContours <= 0:
            continue
        coords, flags, ends = list(g.coordinates), list(g.flags), list(g.endPtsOfContours)
        new_c, new_f, new_e, start = [], [], [], 0
        for end in ends:
            pts = list(zip(coords[start:end + 1], flags[start:end + 1]))
            out = []
            for p, fl in pts:
                if out and fl & 1 and out[-1][1] & 1 and tuple(out[-1][0]) == tuple(p):
                    continue
                out.append((p, fl))
            while len(out) > 1 and out[0][1] & 1 and out[-1][1] & 1 and tuple(out[0][0]) == tuple(out[-1][0]):
                out.pop()
            new_c += [p for p, _ in out]
            new_f += [fl for _, fl in out]
            new_e.append(len(new_c) - 1)
            start = end + 1
        if len(new_c) != len(coords):
            g.coordinates = GlyphCoordinates(new_c)
            g.flags = bytearray(new_f)
            g.endPtsOfContours = new_e
            g.recalcBounds(glyf)


def build_style(style, outdir, webdir=None):
    webdir = webdir or outdir
    cfg = STYLES[style]
    k, mode = cfg["k"], cfg["mode"]
    order = [".notdef", "space"] + [n for n in GLYPHS if n not in (".notdef", "space")]
    glyf, metrics, cmap = {}, {}, {}
    cell = (2 * D + 2 * CELL_SB) * S
    for name in order:
        uni, _ = GLYPHS[name]
        if name == "space":
            path = Path()
        else:
            path = glyph_outline(name, k, mode)
        # TrueType wants clockwise outer contours (negative area in y-up space)
        if path_bounds(path) is not None and signed_area(path) > 0:
            path.reverse()
        dy = BASE * S
        if mode == "open":
            if name == "space":
                adv = round(0.62 * S)
                dx = 0
            else:
                b = path_bounds(path)
                dx = (SB - b[0]) * S
                adv = round((b[2] - b[0] + 2 * SB) * S)
        else:
            dx = (D + CELL_SB) * S
            adv = round(cell)
        tg = draw_ttglyph(path, dx, dy)
        glyf[name] = tg
        if uni is not None:
            cmap[uni] = name
    cmap[0xA0] = "space"

    fixed_adv = {}
    if mode in MONO_MODES:
        for name, path, dx, adv in mono_glyphs(mode):
            if path_bounds(path) is not None and signed_area(path) > 0:
                path.reverse()
            glyf[name] = draw_ttglyph(path, dx, BASE * S)
            order.append(name)
            fixed_adv[name] = adv

    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyf)
    glyf_t = fb.font["glyf"]
    drop_zero_length_segments(glyf_t, order)
    for name in order:
        g_ = glyf_t[name]
        g_.recalcBounds(glyf_t)
        lsb = getattr(g_, "xMin", 0) if g_.numberOfContours else 0
        adv = fixed_adv[name] if name in fixed_adv else adv_of(name, glyf_t, mode, cell)
        metrics[name] = (adv, lsb)
    fb.setupHorizontalMetrics(metrics)
    asc, desc = 780, -300
    fb.setupHorizontalHeader(ascent=asc, descent=desc, lineGap=0)
    fam = FAMILY if style == "Regular" else f"{FAMILY} {style}"
    ps = f"{FAMILY}{'' if style == 'Regular' else style}-Regular"
    fb.setupNameTable({
        "familyName": fam,
        "styleName": "Regular",
        "uniqueFontIdentifier": f"{VERSION};{ps}",
        "fullName": fam,
        "psName": ps,
        "version": f"Version {VERSION}",
        "copyright": COPYRIGHT,
        "licenseDescription": "This Font Software is licensed under the SIL Open Font License, Version 1.1. "
                              "This license is available with a FAQ at: https://openfontlicense.org",
        "manufacturer": "The Rondel Project Authors",
        "vendorURL": REPO_URL,
        "licenseInfoURL": "https://openfontlicense.org",
        "designer": "RV",
        "description": "Every glyph inscribed in one circle; built only from circle arcs and straight lines.",
        "typographicFamily": FAMILY,
        "typographicSubfamily": style,
    }, mac=False)
    fb.setupOS2(
        sTypoAscender=asc, sTypoDescender=desc, sTypoLineGap=0,
        usWinAscent=asc, usWinDescent=-desc,
        sxHeight=round(2 * XB * S), sCapHeight=round((D + BASE) * S),
        fsType=0, achVendID="RNDL", usWeightClass=400,
        fsSelection=0x40 | 0x80, version=4,
        ulCodePageRange1=1, ulCodePageRange2=0,          # Latin 1
        panose=PANOSE_MONO if mode != "open" else PANOSE_PROP,
    )
    fb.setupPost(isFixedPitch=0 if mode == "open" else 1,
                 underlinePosition=-120, underlineThickness=round(W * S * 0.6))
    from fontTools.misc.timeTools import timestampNow
    fb.setupHead(unitsPerEm=UPM, fontRevision=float(VERSION), created=timestampNow(), modified=timestampNow())
    if mode == "open":
        equalize_math_signs(fb.font)
    fea = "languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n"
    if mode == "open":
        fea += auto_kern(fb.font, order)
    if mode in MONO_MODES:
        fea += mono_fea()
    if mode == "open" or mode in MONO_MODES:
        from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
        addOpenTypeFeaturesFromString(fb.font, fea)
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(webdir, exist_ok=True)
    base = f"{FAMILY}{'' if style == 'Regular' else style}-Regular"
    ttf_path = os.path.join(outdir, base + ".ttf")
    fb.save(ttf_path)
    if not os.environ.get("RONDEL_NO_HINT"):
        autohint(ttf_path)
    f = TTFont(ttf_path)
    f.flavor = "woff2"
    f.save(os.path.join(webdir, base + ".woff2"))
    return ttf_path


# --------------------------------------------------------------------------------------
# Auto-kerning (open style): close up pairs whose facing profiles leave excess space
# --------------------------------------------------------------------------------------
KERN_SET = (list("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
            + ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
               "period", "comma", "colon", "semicolon", "hyphen", "quotesingle", "quotedbl",
               "parenleft", "parenright", "slash", "exclam", "question", "ampersand"])
KERN_ROWS_STEP = 8
KERN_WINDOW = 40       # vertical tolerance (font units) so diagonals never collide
KERN_FACTOR = 0.8
KERN_MAX = 200
KERN_MIN = 18


def _profiles(font, name):
    import numpy as np
    from fontTools.pens.basePen import BasePen

    class FlatPen(BasePen):
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

    glyf = font["glyf"]
    pen = FlatPen()
    glyf[name].draw(pen, glyf)
    rows = np.arange(-300, 760, KERN_ROWS_STEP, dtype=float)
    L = np.full(rows.shape, np.inf)
    Rr = np.full(rows.shape, -np.inf)
    for c in pen.contours:
        pts = np.array(c + [c[0]], dtype=float)
        x0, y0, x1, y1 = pts[:-1, 0], pts[:-1, 1], pts[1:, 0], pts[1:, 1]
        for xa, ya, xb, yb in zip(x0, y0, x1, y1):
            lo, hi = min(ya, yb), max(ya, yb)
            m = (rows >= lo) & (rows <= hi)
            if not m.any():
                continue
            xs = xa if ya == yb else xa + (rows[m] - ya) * (xb - xa) / (yb - ya)
            L[m] = np.minimum(L[m], xs)
            Rr[m] = np.maximum(Rr[m], xs)
    # widen vertically so near-diagonal neighbours keep their distance
    k = KERN_WINDOW // KERN_ROWS_STEP
    Lw, Rw = L.copy(), Rr.copy()
    for s_ in range(1, k + 1):
        Lw[s_:] = np.minimum(Lw[s_:], L[:-s_]); Lw[:-s_] = np.minimum(Lw[:-s_], L[s_:])
        Rw[s_:] = np.maximum(Rw[s_:], Rr[:-s_]); Rw[:-s_] = np.maximum(Rw[:-s_], Rr[s_:])
    return Lw, Rw


def auto_kern(font, order):
    import numpy as np
    hmtx = font["hmtx"]
    names = [n for n in KERN_SET if n in order]
    prof = {n: _profiles(font, n) for n in names}
    target = 2 * SB * S
    lines = []
    for a_ in names:
        adv = hmtx[a_][0]
        Ra = prof[a_][1]
        for b_ in names:
            Lb = prof[b_][0]
            m = np.isfinite(Ra) & np.isfinite(Lb)
            if not m.any():
                continue
            dmin = float(np.min(adv - Ra[m] + Lb[m]))
            excess = dmin - target
            kv = -int(round(min(KERN_MAX, KERN_FACTOR * excess) / 2.0) * 2)
            if abs(kv) >= KERN_MIN:
                lines.append(f"  pos {a_} {b_} {kv};")
    return "feature kern {\n" + "\n".join(lines) + "\n} kern;\n"


def adv_of(name, glyf_t, mode, cell):
    if mode != "open":
        return round(cell)
    if name == "space":
        return round(0.62 * S)
    g_ = glyf_t[name]
    return round(g_.xMax - g_.xMin + 2 * SB * S)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build the Rondel fonts.")
    ap.add_argument("outdir", nargs="?", default="fonts", help="output folder for both formats")
    ap.add_argument("--ttf", help="folder for .ttf files (default: outdir)")
    ap.add_argument("--woff2", help="folder for .woff2 files (default: outdir)")
    args = ap.parse_args()
    if os.environ.get("RONDEL_PROOF"):
        STYLES["Cell"] = dict(k=1.0, mode="cell")
    for st in STYLES:
        p = build_style(st, args.ttf or args.outdir, args.woff2 or args.outdir)
        print("built", p, len(TTFont(p).getGlyphOrder()), "glyphs")
