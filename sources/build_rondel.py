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

Character set
  Google Fonts Latin Core (accented Latin, punctuation and symbols) plus the monograms.

Build
  This script writes the UFO sources; gftools builder compiles them into fonts.
  Run sources/build.sh to do both.
  Usage:  python3 sources/build_rondel.py [--ufo sources/ufo] [--styles Regular Ring Coin]
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
VERSION = "1.600"
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

    def __init__(self, k=1.0, sx=1, sy=1, prims=None, skel=None, o=(0.0, 0.0), w=None, extras=None):
        self.k, self.sx, self.sy = k, sx, sy
        self.o = o                                      # centre of this glyph's circle
        self.w = W if w is None else w                  # stroke width
        self.prims = prims if prims is not None else []
        self.skel = skel if skel is not None else []   # centre-line record, for specimens
        self.extras = extras if extras is not None else []   # parts outside this circle

    def flipped(self, sx=1, sy=1):
        return G(self.k, self.sx * sx, self.sy * sy, self.prims, self.skel, self.o, self.w, self.extras)

    def part(self, dx=0.0, dy=0.0, k_mul=1.0, w=None):
        """A separate drawing whose own circle is centred at (dx, dy) of this one.
        Its strokes are not trimmed by this glyph's circle (used for accents and
        small inset letters)."""
        p = G(self.k * k_mul, 1, 1, None, self.skel,
              (self.o[0] + self.k * dx, self.o[1] + self.k * dy),
              self.w if w is None else w)
        self.extras.append(p)
        return p

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

    def _ext(self, c):
        if c == "f":
            return 0.0
        if c == "s":
            return self.w / 2
        if c == "c":
            return CUT
        return float(c)

    def _add(self, path, clip):
        self.prims.append((path, self._clip(clip)))

    # primitives ----------------------------------------------------------------------
    def poly(self, pts, c0="f", c1="f", clip=None):
        """Straight polyline with mitre joins. Caps: f=flat, s=square, c=run out to circle."""
        P = [self.T(p) for p in pts]
        d = []
        for i in range(len(P) - 1):
            dx, dy = P[i + 1][0] - P[i][0], P[i + 1][1] - P[i][1]
            L = math.hypot(dx, dy)
            d.append((dx / L, dy / L))
        e0, e1 = self._ext(c0), self._ext(c1)
        P[0] = (P[0][0] - d[0][0] * e0, P[0][1] - d[0][1] * e0)
        P[-1] = (P[-1][0] + d[-1][0] * e1, P[-1][1] + d[-1][1] * e1)
        self.skel.append(("l", list(P)))
        h = self.w / 2

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
        ro, ri = rr + self.w / 2, rr - self.w / 2
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
        h = self.w / 2
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
        for rad_, ccw in ((rr + self.w / 2, True), (rr - self.w / 2, False)):
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
        for p in self.extras:
            if p.prims or p.extras:
                out = union(out, p.outline())
        return out

    def empty(self):
        return not (self.prims or self.extras)


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
    left_stem(g)                      # bowed stems, like B D E F K L P R
    left_stem(g.flipped(-1, 1))
    x = stem_x(0)
    g.line((x, 0), (-x, 0))


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
    g.line((0, 1.2), (0, -1.2), "c", "c")


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
    g.arc((0, 0), ro, a0 - 8, 290)              # overlaps the hook so the join is clean


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
    g.poly([(-0.55, -0.78), (0.55, -0.78), (0.55, 0.78), (-0.55, 0.78), (-0.55, -0.78)], "s", "s")


# --------------------------------------------------------------------------------------
# Extended Latin (Google Fonts Latin Core)
#
# Accents are drawn from arcs and lines like everything else, a little lighter than the
# letters. On lowercase they sit inside the circle; on capitals and ascenders there is
# no room, so they sit just outside it.
# --------------------------------------------------------------------------------------
W_ACC = 0.85 * W                  # accent stroke width
LC_TOP = XB + 0.065               # attachment height of accents on x-height letters
CAP_TOP = D + 0.07                # ... on capitals and ascenders (outside the circle)
LC_BOT = -XB - 0.01               # attachment of marks below the baseline
CAP_BOT = -D - 0.03               # ... below capitals and descenders
TALL = 0.7                        # ink above this means "capital or ascender"
MARK_CENTER = 0.7                 # combining marks: ink centred this far left of origin

MARKS = {}        # name -> dict(draw, kind, uni, spacing=(name, uni))


def mark(name, kind, uni=None, spacing=None):
    def deco(f):
        MARKS[name] = dict(draw=f, kind=kind, uni=uni, spacing=spacing)
        return f
    return deco


# ---- the marks, each drawn with its attachment point at (0, 0) ------------------------
@mark("acutecomb", "top", 0x0301, ("acute", 0x00B4))
def m_acute(g):
    arc3(g, (-0.07, 0.0), (0.10, 0.24), 0.03)


@mark("gravecomb", "top", 0x0300, ("grave_", None))
def m_grave(g):
    arc3(g, (0.07, 0.0), (-0.10, 0.24), -0.03)


@mark("circumflexcomb", "top", 0x0302, ("circumflex", 0x02C6))
def m_circumflex(g):
    pointed(g, (-0.20, 0.0), (0.20, 0.0), (0.0, 0.21), 0.035, "f")


@mark("caroncomb", "top", 0x030C, ("caron", 0x02C7))
def m_caron(g):
    pointed(g, (-0.20, 0.21), (0.20, 0.21), (0.0, 0.0), -0.035, "f")


@mark("tildecomb", "top", 0x0303, ("tilde", 0x02DC))
def m_tilde(g):
    rr, y = 0.11, 0.09
    g.arc((-rr, y), rr, 208, 0)
    g.arc((rr, y), rr, 180, 388)


@mark("macroncomb", "top", 0x0304, ("macron", 0x00AF))
def m_macron(g):
    arc3(g, (-0.22, 0.07), (0.22, 0.07), -0.025)


@mark("brevecomb", "top", 0x0306, ("breve", 0x02D8))
def m_breve(g):
    g.arc((0, 0.21), 0.19, 195, 345)


@mark("dotaccentcomb", "top", 0x0307, ("dotaccent", 0x02D9))
def m_dot(g):
    g.dot((0, DOT * 0.9), DOT * 0.9)


@mark("dieresiscomb", "top", 0x0308, ("dieresis", 0x00A8))
def m_dieresis(g):
    for x in (-0.17, 0.17):
        g.dot((x, DOT * 0.9), DOT * 0.9)


@mark("ringcomb", "top", 0x030A, ("ring", 0x02DA))
def m_ring(g):
    g.ring((0, 0.13 + W_ACC / 2), 0.13)


@mark("hungarumlautcomb", "top", 0x030B, ("hungarumlaut", 0x02DD))
def m_hungarumlaut(g):
    for x in (-0.12, 0.14):
        arc3(g, (x - 0.06, 0.0), (x + 0.08, 0.22), 0.025)


@mark("commaturnedabovecomb", "top", 0x0312)
def m_commaturned(g):
    g.dot((0, 0.10), 0.085)
    arc3(g, (-0.03, 0.10), (0.09, 0.30), -0.035)


@mark("caroncomb.alt", "caronalt")
def m_caronalt(g):
    arc3(g, (0.03, 0.25), (-0.03, 0.0), -0.035)


@mark("cedillacomb", "bottom", 0x0327, ("cedilla", 0x00B8))
def m_cedilla(g):
    g.arc((-0.02, -0.16), 0.13, 100, -150)


@mark("commaaccentcomb", "bottom", 0x0326)
def m_commabelow(g):
    g.dot((0, -0.13), 0.085)
    arc3(g, (0.03, -0.13), (-0.09, -0.34), -0.035)


@mark("ogonekcomb", "ogonek", 0x0328, ("ogonek", 0x02DB))
def m_ogonek(g):
    g.arc((0.10, -0.10), 0.14, 135, 300)


def mark_attach_y(kind):
    """Where a mark sits in its own combining glyph (the lowercase position)."""
    return {"top": LC_TOP, "caronalt": LC_TOP, "bottom": LC_BOT, "ogonek": LC_BOT}[kind]


def draw_mark(g, name, x, y):
    MARKS[name]["draw"](g.part(x, y, w=W_ACC))


def _register_marks():
    for name, m in MARKS.items():
        y = mark_attach_y(m["kind"])
        fn = (lambda nm, yy: (lambda g: draw_mark(g, nm, 0.0, yy)))(name, y)
        GLYPHS[name] = (m["uni"], fn)
        if m["spacing"]:
            sname, suni = m["spacing"]
            if suni is not None:
                GLYPHS[sname] = (suni, fn)


# ---- special letters -----------------------------------------------------------------
@glyph("dotlessi", 0x0131)
def dotlessi(g):
    g.line((0, -XB), (0, XB))


@glyph("dotlessj", 0x0237)
def dotlessj(g):
    x, cy, r = 0.15, -0.5, 0.35
    g.line((x, XB), (x, cy))
    g.arc((x - r, cy), r, 0, -180)


@glyph("AE", 0x00C6)
def AE(g):
    fl, ap = pc(-124), (0.0, 1.0)
    cl, rl, _, _ = arc3(g, fl, ap, LANCET, "c", 0.2)
    g.line((0, 1.2), (0, -1.2), "c", "c")
    g.arc((0, 0), 1, 90, 40)
    g.arc((0, 0), 1, 270, 320)
    g.line((0, 0), (0.48, 0))
    y = -0.3
    g.line((x_on(cl, rl, y, -1), y), (0, y))


@glyph("OE", 0x0152)
def OE(g):
    g.arc((0, 0), 1, 90, 270)
    g.line((0, 1.2), (0, -1.2), "c", "c")
    g.arc((0, 0), 1, 90, 40)
    g.arc((0, 0), 1, 270, 320)
    g.line((0, 0), (0.48, 0))


@glyph("ae", 0x00E6)
def ae(g):
    g.ring((-0.48, 0), XR)
    g.line((0.02, -XB), (0.02, XB))
    g.arc((0.5, 0), XR, 0, 315)
    g.line((0.0, 0), (1.2, 0), "f", "c")


@glyph("oe", 0x0153)
def oe(g):
    g.ring((-0.48, 0), XR)
    g.arc((0.5, 0), XR, 0, 315)
    g.line((0.0, 0), (1.2, 0), "f", "c")


@glyph("Oslash", 0x00D8)
def Oslash(g):
    g.ring((0, 0), 1)
    g.line(pc(232), pc(52), "c", "c")


@glyph("oslash", 0x00F8)
def oslash(g):
    g.ring((0, 0), XR)
    g.line((-0.5, -0.62), (0.5, 0.62))


@glyph("Eth", 0x00D0)
def Eth(g):
    GLYPHS["D"][1](g)
    g.line((-0.82, 0), (-0.22, 0))


GLYPHS["Dcroat"] = (0x0110, Eth)


@glyph("dcroat", 0x0111)
def dcroat(g):
    GLYPHS["d"][1](g)
    g.line((0.22, 0.80), (1.0, 0.80), "f", "c")


@glyph("eth", 0x00F0)
def eth(g):
    g.ring((0, 0), XR)
    tarc(g, pc(15, XR), arc_dir(15, False), (-0.26, 0.98))
    g.line((-0.20, 0.60), (0.30, 0.88))


@glyph("Hbar", 0x0126)
def Hbar(g):
    H(g)
    g.line((-0.76, 0.5), (0.76, 0.5))


@glyph("hbar", 0x0127)
def hbar(g):
    GLYPHS["h"][1](g)
    g.line((-0.78, 0.80), (-0.20, 0.80))


@glyph("Lslash", 0x0141)
def Lslash(g):
    GLYPHS["L"][1](g)
    g.line((-0.82, -0.22), (-0.24, 0.22))


@glyph("lslash", 0x0142)
def lslash(g):
    GLYPHS["l"][1](g)
    g.line((-0.36, 0.12), (0.14, 0.52))


def thorn_bowl(g, y_top, y_low, reach):
    p0, p1 = (stem_x(y_top), y_top), (stem_x(y_low), y_low)
    lo, hi = 0.2, 1.4
    for _ in range(50):
        mid = (lo + hi) / 2
        c, r = arc_circle(p0, p1, mid)
        if c[0] + r > reach:
            hi = mid
        else:
            lo = mid
    arc3(g, p0, p1, (lo + hi) / 2)


@glyph("Thorn", 0x00DE)
def Thorn(g):
    left_stem(g)
    thorn_bowl(g, 0.55, -0.45, 0.5)


@glyph("thorn", 0x00FE)
def thorn(g):
    g.ring((0, 0), XR)
    g.line((-XR, 2), (-XR, -2), "c", "c")


@glyph("germandbls", 0x00DF)
def germandbls(g):
    x0, c1, r1 = -0.42, (-0.07, 0.55), 0.35
    g.line((x0, -XB), (x0, c1[1]))
    g.arc(c1, r1, 180, -40)
    p = pc(-40, r1, c1)
    biarc(g, p, arc_dir(-40, True), (0.46, -0.10), (0, -1))
    g.arc((0.0, -0.10), 0.46, 0, -140)


@glyph("Germandbls", 0x1E9E)
def Germandbls(g):
    left_stem(g)
    g.arc((0, 0), 1, STEM_A, 52)
    cb, rb = (0.02, -0.46), 0.50
    start = pc(100, rb, cb)
    arc3(g, pc(52), start, 0.06, "c", "f")
    g.arc(cb, rb, 100, -148)


@glyph("dottedCircle", 0x25CC)
def dotted_circle(g):
    for i_ in range(12):
        g.dot(pc(90 + 30 * i_, 0.42), 0.055)


# ---- punctuation and symbols ----------------------------------------------------------
GLYPHS["exclamdown"] = (0x00A1, mirror(exclam, -1, -1))
GLYPHS["questiondown"] = (0x00BF, mirror(question, -1, -1))


@glyph("cent", 0x00A2)
def cent(g):
    g.arc((0, 0), XR, 45, 315)
    g.line((0.04, -0.80), (0.04, 0.80))


@glyph("sterling", 0x00A3)
def sterling(g):
    g.vlimit(FIG_H)
    ch, rh = (0.12, FIG_R - 0.32), 0.32
    g.arc(ch, rh, 20, 180)
    top = pc(180, rh, ch)
    q = pc(232, FIG_R)
    res = tarc(g, top, (0, -1), q)
    g.arc((0, 0), FIG_R, 232, 312)
    g.corner(q, end_dir(res[0], res[2], res[3]), arc_dir(232, False))
    g.line((-0.52, -0.04), (0.20, -0.04))


@glyph("yen", 0x00A5)
def yen(g):
    g.vlimit(FIG_H)
    j = (0, 0.02)
    arc3(g, pc(127), j, -LANCET, "c", 0.06)
    arc3(g, pc(53), j, LANCET, "c", 0.06)
    g.line(j, (0, -1.2), "f", "c")
    for y in (-0.16, -0.44):
        g.line((-0.40, y), (0.40, y))


@glyph("Euro", 0x20AC)
def Euro(g):
    g.vlimit(FIG_H)
    g.arc((0.08, 0), FIG_R - 0.06, 48, 312)
    for y, x1 in ((0.16, 0.26), (-0.16, 0.16)):
        g.line((-0.90, y), (x1, y))


@glyph("section", 0x00A7)
def section(g):
    r = 0.27
    g.ring((0, 0), r)
    g.arc((0, 2 * r), r, 20, 270)
    g.arc((0, -2 * r), r, 200, 450)


@glyph("paragraph", 0x00B6)
def paragraph(g):
    y = 0.86
    g.arc((0.06, y - 0.38), 0.38, 90, 270)
    g.line((0.06, y), (0.34, y))
    g.line((0.06, y), (0.06, -0.86))
    g.line((0.34, y), (0.34, -0.86))


def inset(g, letter, k_mul, dx=0.0, dy=0.0, w=None):
    GLYPHS[letter][1](g.part(dx, dy, k_mul, w))


@glyph("copyright", 0x00A9)
def copyright_(g):
    g.ring((0, 0), 0.86)
    inset(g, "C", 0.46, w=W * 0.85)


@glyph("registered", 0x00AE)
def registered(g):
    g.ring((0, 0), 0.86)
    inset(g, "R", 0.46, w=W * 0.85)


@glyph("trademark", 0x2122)
def trademark(g):
    inset(g, "T", 0.33, -0.37, 0.62, W * 0.75)
    inset(g, "M", 0.33, 0.37, 0.62, W * 0.75)


@glyph("ordfeminine", 0x00AA)
def ordfeminine(g):
    p = g.part(0, 0.72, 0.5, W * 0.85)
    a(p)
    g.line((-0.30, 0.24), (0.30, 0.24))


@glyph("ordmasculine", 0x00BA)
def ordmasculine(g):
    p = g.part(0, 0.72, 0.5, W * 0.85)
    o(p)
    g.line((-0.30, 0.24), (0.30, 0.24))


@glyph("degree", 0x00B0)
def degree(g):
    g.part(0, 0.78, w=W_ACC).ring((0, 0), 0.17)


@glyph("periodcentered", 0x00B7)
def periodcentered(g):
    g.dot((0, 0))


@glyph("bullet", 0x2022)
def bullet(g):
    g.dot((0, 0), 0.2)


@glyph("ellipsis", 0x2026)
def ellipsis(g):
    for x in (-0.46, 0.0, 0.46):
        g.dot((x, BASE_DOT[1]))


@glyph("endash", 0x2013)
def endash(g):
    g.line((-0.60, 0), (0.60, 0))


@glyph("emdash", 0x2014)
def emdash(g):
    g.line((-1.04, 0), (1.04, 0))


@glyph("minus", 0x2212)
def minus(g):
    g.line((-0.45, 0), (0.45, 0))


@glyph("multiply", 0x00D7)
def multiply(g):
    g.line((-0.32, -0.32), (0.32, 0.32))
    g.line((-0.32, 0.32), (0.32, -0.32))


@glyph("divide", 0x00F7)
def divide(g):
    g.line((-0.45, 0), (0.45, 0))
    g.dot((0, 0.30))
    g.dot((0, -0.30))


def comma_shape(g):
    g.dot(BASE_DOT)
    arc3(g, (0.04, BASE_DOT[1]), (-0.16, -0.84), -0.06)


QUOTE_DY = 0.86 - BASE_DOT[1]     # lifts a comma so its dot sits near the top of the circle


def quote_right(g, x):
    comma_shape(g.part(x, QUOTE_DY))


def quote_left(g, x):
    comma_shape(g.part(x, 0.10).flipped(-1, -1))


@glyph("quoteright", 0x2019)
def quoteright(g):
    quote_right(g, 0)


@glyph("quoteleft", 0x2018)
def quoteleft(g):
    quote_left(g, 0)


@glyph("quotedblright", 0x201D)
def quotedblright(g):
    for x in (-0.2, 0.2):
        quote_right(g, x)


@glyph("quotedblleft", 0x201C)
def quotedblleft(g):
    for x in (-0.2, 0.2):
        quote_left(g, x)


@glyph("quotesinglbase", 0x201A)
def quotesinglbase(g):
    comma_shape(g)


@glyph("quotedblbase", 0x201E)
def quotedblbase(g):
    for x in (-0.2, 0.2):
        comma_shape(g.part(x, 0))


def guil(g, x):
    pointed(g, (x + 0.17, 0.24), (x + 0.17, -0.24), (x - 0.17, 0.0), -0.05, "f")


GLYPHS["guilsinglleft"] = (0x2039, lambda g: guil(g, 0))
GLYPHS["guilsinglright"] = (0x203A, lambda g: guil(g.flipped(-1, 1), 0))
GLYPHS["guillemetleft"] = (0x00AB, lambda g: (guil(g, -0.17), guil(g, 0.17)))
GLYPHS["guillemetright"] = (0x00BB, lambda g: (guil(g.flipped(-1, 1), -0.17), guil(g.flipped(-1, 1), 0.17)))


# ---- accented letters -----------------------------------------------------------------
# Each is the base letter plus marks placed at anchors; in the Regular UFO they are
# stored as components, in Ring and Coin as outlines.
TOP_X = {"c": 0.0, "C": 0.0, "G": 0.0, "L": -0.30, "l": -0.12, "t": -0.10, "r": -0.05,
         "J": 0.10, "j": 0.15, "dotlessj": 0.15}
BOTTOM_X = {"t": -0.02, "L": 0.10, "l": 0.0, "T": 0.0, "g": 0.0, "G": 0.08}
OGONEK_AT = {"A": (0.60, -0.84), "a": (0.50, -XB + 0.03), "E": (0.40, -0.94), "e": (0.20, -XB + 0.04),
             "I": (0.0, -0.98), "i": (0.0, -XB + 0.03), "dotlessi": (0.0, -XB + 0.03),
             "U": (0.32, -0.97), "u": (0.50, -XB + 0.03), "dottedCircle": (0.26, -0.40)}
CARONALT_AT = {"d": (0.76, 0.66), "l": (0.18, 0.66), "t": (0.22, 0.60), "L": (-0.20, 0.60)}

COMB = {0x0300: "gravecomb", 0x0301: "acutecomb", 0x0302: "circumflexcomb", 0x0303: "tildecomb",
        0x0304: "macroncomb", 0x0306: "brevecomb", 0x0307: "dotaccentcomb", 0x0308: "dieresiscomb",
        0x030A: "ringcomb", 0x030B: "hungarumlautcomb", 0x030C: "caroncomb", 0x0326: "commaaccentcomb",
        0x0327: "cedillacomb", 0x0328: "ogonekcomb"}
COMMA_BELOW_LETTERS = set("GKLNRgklnr")      # Latvian: U+0327 is drawn as a comma below
CARONALT_LETTERS = set("dltL")               # ď ľ ť Ľ use the vertical caron

COMPOSITES = {}     # name -> (codepoint, base, [(mark, anchor_name)])

_ANCHOR_CACHE = {}


def base_bounds(base):
    if base not in _ANCHOR_CACHE:
        g = G()
        GLYPHS[base][1](g)
        _ANCHOR_CACHE[base] = path_bounds(g.outline())
    return _ANCHOR_CACHE[base]


def anchor(base, kind):
    """Design-unit attachment point on a base glyph for a mark of this kind."""
    x0, y0, x1, y1 = base_bounds(base)
    if kind == "top":
        return (TOP_X.get(base, (x0 + x1) / 2), CAP_TOP if y1 > TALL else LC_TOP)
    if kind == "bottom":
        return (BOTTOM_X.get(base, (x0 + x1) / 2), CAP_BOT if y0 < -TALL else LC_BOT)
    if kind == "ogonek":
        return OGONEK_AT[base]
    if kind == "caronalt":
        return CARONALT_AT[base]
    raise KeyError(kind)


def glyph_name_for(cp):
    from fontTools.agl import UV2AGL
    special = {0x0110: "Dcroat", 0x0111: "dcroat", 0x0122: "Gcommaaccent", 0x0123: "gcommaaccent",
               0x0136: "Kcommaaccent", 0x0137: "kcommaaccent", 0x013B: "Lcommaaccent",
               0x013C: "lcommaaccent", 0x0145: "Ncommaaccent", 0x0146: "ncommaaccent",
               0x0156: "Rcommaaccent", 0x0157: "rcommaaccent", 0x0218: "Scommaaccent",
               0x0219: "scommaaccent", 0x021A: "Tcommaaccent", 0x021B: "tcommaaccent",
               0x0130: "Idotaccent", 0x1E80: "Wgrave", 0x1E81: "wgrave", 0x1E82: "Wacute",
               0x1E83: "wacute", 0x1E84: "Wdieresis", 0x1E85: "wdieresis", 0x1EF2: "Ygrave",
               0x1EF3: "ygrave", 0x0150: "Ohungarumlaut", 0x0151: "ohungarumlaut",
               0x0170: "Uhungarumlaut", 0x0171: "uhungarumlaut"}
    return special.get(cp) or UV2AGL.get(cp) or f"uni{cp:04X}"


def _add_composites():
    import unicodedata
    for cp in LATIN_CORE_COMPOSITES:
        dec = unicodedata.decomposition(chr(cp)).split()
        base_ch, marks = chr(int(dec[0], 16)), [int(m, 16) for m in dec[1:]]
        base = glyph_name_for(ord(base_ch))
        out = []
        for m in marks:
            mname = COMB[m]
            kind = MARKS[mname]["kind"]
            if m == 0x0327 and base_ch in COMMA_BELOW_LETTERS:
                if base_ch == "g":
                    mname, kind = "commaturnedabovecomb", "top"
                else:
                    mname, kind = "commaaccentcomb", "bottom"
            if m == 0x030C and base_ch in CARONALT_LETTERS:
                mname, kind = "caroncomb.alt", "caronalt"
            if kind == "top" and base_ch in "ij":
                base = "dotlessi" if base_ch == "i" else "dotlessj"
            out.append((mname, kind))
        COMPOSITES[glyph_name_for(cp)] = (cp, base, out)


def composite_fn(base, marks):
    def fn(g):
        GLYPHS[base][1](g)
        for mname, kind in marks:
            x, y = anchor(base, kind)
            draw_mark(g, mname, x, y)
    return fn


LATIN_CORE_COMPOSITES = [
    0x00C0, 0x00C1, 0x00C2, 0x00C3, 0x00C4, 0x00C5, 0x00C7, 0x00C8, 0x00C9, 0x00CA, 0x00CB, 0x00CC,
    0x00CD, 0x00CE, 0x00CF, 0x00D1, 0x00D2, 0x00D3, 0x00D4, 0x00D5, 0x00D6, 0x00D9, 0x00DA, 0x00DB,
    0x00DC, 0x00DD, 0x00E0, 0x00E1, 0x00E2, 0x00E3, 0x00E4, 0x00E5, 0x00E7, 0x00E8, 0x00E9, 0x00EA,
    0x00EB, 0x00EC, 0x00ED, 0x00EE, 0x00EF, 0x00F1, 0x00F2, 0x00F3, 0x00F4, 0x00F5, 0x00F6, 0x00F9,
    0x00FA, 0x00FB, 0x00FC, 0x00FD, 0x00FF, 0x0100, 0x0101, 0x0102, 0x0103, 0x0104, 0x0105, 0x0106,
    0x0107, 0x010A, 0x010B, 0x010C, 0x010D, 0x010E, 0x010F, 0x0112, 0x0113, 0x0116, 0x0117, 0x0118,
    0x0119, 0x011A, 0x011B, 0x011E, 0x011F, 0x0120, 0x0121, 0x0122, 0x0123, 0x012A, 0x012B, 0x012E,
    0x012F, 0x0130, 0x0136, 0x0137, 0x0139, 0x013A, 0x013B, 0x013C, 0x013D, 0x013E, 0x0143, 0x0144,
    0x0145, 0x0146, 0x0147, 0x0148, 0x0150, 0x0151, 0x0154, 0x0155, 0x0158, 0x0159, 0x015A, 0x015B,
    0x015E, 0x015F, 0x0160, 0x0161, 0x0164, 0x0165, 0x016A, 0x016B, 0x016E, 0x016F, 0x0170, 0x0171,
    0x0172, 0x0173, 0x0174, 0x0175, 0x0176, 0x0177, 0x0178, 0x0179, 0x017A, 0x017B, 0x017C, 0x017D,
    0x017E, 0x0218, 0x0219, 0x021A, 0x021B, 0x1E80, 0x1E81, 0x1E82, 0x1E83, 0x1E84, 0x1E85, 0x1EF2,
    0x1EF3,
]

_register_marks()
_add_composites()
COMPOSITES["iogonek.dotless"] = (None, "dotlessi", [("ogonekcomb", "ogonek")])   # į under accents
for _n, (_cp, _b, _m) in COMPOSITES.items():
    GLYPHS[_n] = (_cp, composite_fn(_b, _m))


# --------------------------------------------------------------------------------------
# Styles and font assembly
#
# This script writes UFO sources (sources/ufo/*.ufo); gftools builder compiles them
# (see sources/config*.yaml and sources/build.sh).
# --------------------------------------------------------------------------------------
STYLES = {
    "Regular": dict(k=1.0, mode="open", family=FAMILY),
    "Ring": dict(k=0.70, mode="ring", family=f"{FAMILY} Ring"),
    "Coin": dict(k=0.74, mode="coin", family=f"{FAMILY} Coin"),
}
CELL_SB = 0.08
FIT = {"ring": D - W - 0.10, "coin": D - 0.24}   # glyphs that reach past this radius are shrunk


def is_mark(name):
    return name in MARKS and MARKS[name]["uni"] is not None


def _max_radius(path):
    from fontTools.pens.recordingPen import RecordingPen
    rp = RecordingPen()
    path.draw(rp)
    r = 0.0
    for _, pts in rp.value:
        for pt in pts:
            if pt is not None:
                r = max(r, math.hypot(*pt))
    return r


def _draw(name, k):
    g = G(k=k)
    GLYPHS[name][1](g)
    return Path() if g.empty() else g.outline()


def glyph_outline(name, k, mode):
    """Final outline in design units (ring added / letter knocked out for Ring and Coin)."""
    body = _draw(name, k)
    if mode in FIT and not is_mark(name) and path_bounds(body) is not None:
        rmax = _max_radius(body)
        if rmax > FIT[mode]:                 # e.g. accented capitals: shrink to fit the ring
            k2 = k * (FIT[mode] - W / 2) / (rmax - W / 2)
            body = _draw(name, k2)
    if is_mark(name) or mode == "open":
        return body
    if mode == "ring":
        frame = Path(fillType=FillType.EVEN_ODD)
        for rad_, ccw in ((D, True), (D - W, False)):
            frame.moveTo(rad_, 0)
            _arc_to(frame, 0, 0, rad_, 0, 2 * math.pi if ccw else -2 * math.pi)
            frame.close()
        return union(frame, body)
    disc = circle_path(0, 0, D)
    return difference(disc, body)


def path_bounds(path):
    bp = BoundsPen(None)
    path.draw(bp)
    return bp.bounds


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
    """-> list of (name, design path, dx in font units, advance)."""
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


def ccmp_fea():
    """i and j (and i with ogonek) lose their dots under accents typed as combining marks,
    including when a mark below comes between."""
    tops = " ".join(n for n, m in MARKS.items() if m["uni"] and m["kind"] == "top")
    bottoms = " ".join(n for n, m in MARKS.items() if m["uni"] and m["kind"] in ("bottom", "ogonek"))
    return (f"@TOP_MARKS = [{tops}];\n"
            f"@BOTTOM_MARKS = [{bottoms}];\n"
            "lookup DOTLESS { sub i by dotlessi; sub j by dotlessj; sub iogonek by iogonek.dotless; } DOTLESS;\n"
            "feature ccmp {\n"
            "  sub [i j iogonek]' lookup DOTLESS @TOP_MARKS;\n"
            "  sub [i j iogonek]' lookup DOTLESS @BOTTOM_MARKS @TOP_MARKS;\n"
            "  sub [i j iogonek]' lookup DOTLESS @BOTTOM_MARKS @BOTTOM_MARKS @TOP_MARKS;\n"
            "} ccmp;\n")


PANOSE_PROP = [2, 11, 5, 3, 0, 0, 0, 0, 0, 0]   # Latin text, sans, book weight, modern proportions
PANOSE_MONO = [2, 11, 5, 9, 0, 0, 0, 0, 0, 0]   # same, monospaced
MATH_SIGNS = ["plus", "minus", "less", "equal", "greater", "asciitilde", "multiply", "divide"]


# --------------------------------------------------------------------------------------
# Spacing and kerning
# --------------------------------------------------------------------------------------
KERN_EXTRA = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
              "period", "comma", "colon", "semicolon", "hyphen", "endash", "emdash",
              "quotesingle", "quotedbl", "quoteleft", "quoteright", "quotedblleft", "quotedblright",
              "quotesinglbase", "quotedblbase", "guilsinglleft", "guilsinglright",
              "guillemetleft", "guillemetright", "parenleft", "parenright", "slash", "exclam",
              "question", "ampersand", "ellipsis"]
KERN_ROWS_STEP = 8
KERN_WINDOW = 40       # vertical tolerance (font units) so diagonals never collide
KERN_FACTOR = 0.8
KERN_MAX = 200
KERN_MIN = 18


def _flat_contours(path, dx):
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
                a_, b_, c_ = (1 - t) ** 2, 2 * (1 - t) * t, t * t
                self.cur.append((a_ * p0[0] + b_ * p1[0] + c_ * p2[0], a_ * p0[1] + b_ * p1[1] + c_ * p2[1]))

        def _closePath(self):
            self.contours.append(self.cur)
            self.cur = []

        _endPath = _closePath

    pen = FlatPen()
    path.draw(TransformPen(pen, (S, 0, 0, S, dx, BASE * S)))
    return pen.contours


def profile(path, dx, rows):
    import numpy as np
    L = np.full(rows.shape, np.inf)
    Rr = np.full(rows.shape, -np.inf)
    for c in _flat_contours(path, dx):
        pts = np.array(c + [c[0]], dtype=float)
        for xa, ya, xb, yb in zip(pts[:-1, 0], pts[:-1, 1], pts[1:, 0], pts[1:, 1]):
            lo, hi = min(ya, yb), max(ya, yb)
            m = (rows >= lo) & (rows <= hi)
            if not m.any():
                continue
            xs = xa if ya == yb else xa + (rows[m] - ya) * (xb - xa) / (yb - ya)
            L[m] = np.minimum(L[m], xs)
            Rr[m] = np.maximum(Rr[m], xs)
    k = KERN_WINDOW // KERN_ROWS_STEP
    Lw, Rw = L.copy(), Rr.copy()
    for s_ in range(1, k + 1):
        Lw[s_:] = np.minimum(Lw[s_:], L[:-s_]); Lw[:-s_] = np.minimum(Lw[:-s_], L[s_:])
        Rw[s_:] = np.maximum(Rw[s_:], Rr[:-s_]); Rw[:-s_] = np.maximum(Rw[:-s_], Rr[s_:])
    return Lw, Rw


def kern_groups(names):
    """Accented letters kern with their base letter: group name -> members."""
    groups = {}
    for n in names:
        base = COMPOSITES[n][1] if n in COMPOSITES else n
        base = {"dotlessi": "i", "dotlessj": "j", "Dcroat": "Eth"}.get(base, base)
        groups.setdefault(base, []).append(n)
    return groups


def auto_kern(glyphs, names):
    """Class kerning from glyph profiles. Each group is measured by the envelope of all
    its members, so accents never collide. -> (groups.plist dict, kerning dict)."""
    import numpy as np
    rows = np.arange(-450, 950, KERN_ROWS_STEP, dtype=float)
    groups = kern_groups(names)
    prof = {}
    for gname, members in groups.items():
        Ls, Rs = [], []
        for n in members:
            gl = glyphs[n]
            Lp, Rp = profile(gl["path"], gl["dx"], rows)
            Ls.append(Lp)
            Rs.append(Rp)
        prof[gname] = (np.min(Ls, axis=0), np.max(Rs, axis=0), glyphs[groups[gname][0]]["adv"])
    target = 2 * SB * S
    kerning = {}
    for ga, (_, Ra, adv) in prof.items():
        for gb, (Lb, _, _) in prof.items():
            m = np.isfinite(Ra) & np.isfinite(Lb)
            if not m.any():
                continue
            dmin = float(np.min(adv - Ra[m] + Lb[m]))
            kv = -int(round(min(KERN_MAX, KERN_FACTOR * (dmin - target)) / 2.0) * 2)
            if abs(kv) >= KERN_MIN:
                left = f"public.kern1.{ga}" if len(groups[ga]) > 1 else ga
                right = f"public.kern2.{gb}" if len(groups[gb]) > 1 else gb
                kerning[(left, right)] = kv
    plist = {}
    for gname, members in groups.items():
        if len(members) > 1:
            plist[f"public.kern1.{gname}"] = list(members)
            plist[f"public.kern2.{gname}"] = list(members)
    return plist, kerning


# --------------------------------------------------------------------------------------
# UFO writer
# --------------------------------------------------------------------------------------
def glyph_order():
    names = [n for n in GLYPHS if n not in (".notdef", "space")]
    return [".notdef", "space"] + names


def build_glyphs(style):
    """-> dict name -> {path (design units), dx (font units), adv, unicodes}."""
    cfg = STYLES[style]
    k, mode = cfg["k"], cfg["mode"]
    cell = round((2 * D + 2 * CELL_SB) * S)
    out = {}
    for name in glyph_order():
        uni, _ = GLYPHS[name]
        if mode != "open" and name == "caroncomb.alt":
            continue                              # only used as a component in Regular
        path = Path() if name == "space" else glyph_outline(name, k, mode)
        b = path_bounds(path)
        if is_mark(name):
            dx = -MARK_CENTER * S if mode == "open" else -cell / 2
            adv = 0
        elif mode == "open":
            if b is None:
                dx, adv = 0, round(0.62 * S)
            else:
                dx = (SB - b[0]) * S
                adv = round((b[2] - b[0] + 2 * SB) * S)
        else:
            dx, adv = (D + CELL_SB) * S, cell
        out[name] = dict(path=path, dx=dx, adv=adv, unicodes=[uni] if uni is not None else [])
    out["space"]["unicodes"] = [0x20, 0xA0]
    if mode == "open":
        for name, c in COMPOSITES.items():          # accented letters keep the base advance
            base = out[c[1]]
            g = out[name]
            g["dx"] = base["dx"]
            right = path_bounds(g["path"])[2] * S + g["dx"]
            g["adv"] = max(base["adv"], round(right + SB * S))
        signs = [n for n in MATH_SIGNS if n in out]
        adv = max(out[n]["adv"] for n in signs)
        for n in signs:
            b = path_bounds(out[n]["path"])
            out[n]["dx"] = (adv / S - (b[2] - b[0])) / 2 * S - b[0] * S
            out[n]["adv"] = adv
    if mode in MONO_MODES:
        for name, path, dx, adv in mono_glyphs(mode):
            out[name] = dict(path=path, dx=dx, adv=adv, unicodes=[])
    return out


TINY = 2.5          # font units: segments shorter than this are boolean-op crumbs
COLINEAR = 4.0      # degrees: consecutive lines closer than this to straight are merged
SNAP = 1            # font units: near-vertical/horizontal lines this far off are straightened


def _clean_contours(path, dx):
    """Font-unit contours with crumbs from the boolean operations removed: tiny
    segments dropped and nearly colinear line pairs merged."""
    from fontTools.pens.recordingPen import RecordingPen
    rp = RecordingPen()
    path.draw(TransformPen(rp, (S, 0, 0, S, dx, BASE * S)))

    def rnd(pt):
        return None if pt is None else (round(pt[0]), round(pt[1]))

    def dist(p0, p1):
        return math.hypot(p0[0] - p1[0], p0[1] - p1[1])

    contours, cur = [], None
    for op, pts in rp.value:
        if op == "moveTo":
            cur = [rnd(pts[0]), []]
        elif op in ("lineTo", "qCurveTo"):
            if cur is None:                      # all-off-curve quadratic contour (no moveTo)
                cur = [None, []]
            cur[1].append((op, [rnd(q) for q in pts]))
        elif op in ("closePath", "endPath"):
            contours.append(cur)
            cur = None
    out = []
    for start, segs in contours:
        if start is None:
            out.append((None, segs))
            continue
        cleaned, here = [], start
        for op, pts in segs:
            end = pts[-1]
            if end is not None and all(q is None or dist(q, here) < TINY for q in pts):
                continue
            if op == "lineTo":
                ex, ey = end
                if 0 < abs(ex - here[0]) <= SNAP and abs(ey - here[1]) > 10:
                    pts = [(here[0], ey)]
                elif 0 < abs(ey - here[1]) <= SNAP and abs(ex - here[0]) > 10:
                    pts = [(ex, here[1])]
                end = pts[-1]
            cleaned.append([op, pts, here])
            here = end if end is not None else here
        while cleaned and cleaned[-1][0] == "lineTo" and dist(cleaned[-1][1][-1], start) < TINY:
            cleaned.pop()
        if cleaned and cleaned[-1][0] == "qCurveTo" and cleaned[-1][1][-1] is not None \
                and dist(cleaned[-1][1][-1], start) < TINY:
            cleaned[-1][1][-1] = start
        merged = []
        for seg in cleaned:
            if merged and seg[0] == "lineTo" and merged[-1][0] == "lineTo":
                a_, b_, c_ = merged[-1][2], merged[-1][1][-1], seg[1][-1]
                t1 = math.atan2(b_[1] - a_[1], b_[0] - a_[0])
                t2 = math.atan2(c_[1] - b_[1], c_[0] - b_[0])
                turn = abs((math.degrees(t2 - t1) + 180) % 360 - 180)
                if turn < COLINEAR:
                    merged[-1][1] = [c_]
                    continue
            merged.append(seg)
        if len(merged) >= 2 or any(m[0] == "qCurveTo" for m in merged):
            out.append((start, [(op, pts) for op, pts, _ in merged]))
    return out


def _draw_ufo_outline(glyph, path, dx):
    from fontTools.pens.reverseContourPen import ReverseContourPen
    if path_bounds(path) is None:
        return
    pen = glyph.getPen()
    if signed_area(path) < 0:                    # UFO wants counter-clockwise outer contours
        pen = ReverseContourPen(pen)
    for start, segs in _clean_contours(path, dx):
        if start is not None:
            pen.moveTo(start)
        for op, pts in segs:
            getattr(pen, op)(*pts)
        pen.closePath()


def vertical_metrics(glyphs):
    ys = [path_bounds(g["path"]) for g in glyphs.values() if path_bounds(g["path"]) is not None]
    ymax = max(b[3] for b in ys)
    ymin = min(b[1] for b in ys)
    asc = int(math.ceil((ymax + BASE) * S / 10.0) * 10)
    desc = int(math.floor((ymin + BASE) * S / 10.0) * 10)
    return asc, desc


def write_ufo(style, path_out):
    import ufoLib2
    from ufoLib2.objects import Anchor
    cfg = STYLES[style]
    mode = cfg["mode"]
    glyphs = build_glyphs(style)
    ufo = ufoLib2.Font()
    info = ufo.info
    info.familyName = cfg["family"]
    info.styleName = "Regular"
    info.styleMapFamilyName = cfg["family"]
    info.styleMapStyleName = "regular"
    major, minor = VERSION.split(".")
    info.versionMajor, info.versionMinor = int(major), int(minor)
    info.copyright = COPYRIGHT
    info.openTypeNameDesigner = "RV"
    info.openTypeNameManufacturer = "The Rondel Project Authors"
    info.openTypeNameManufacturerURL = REPO_URL
    info.openTypeNameLicense = ("This Font Software is licensed under the SIL Open Font License, "
                                "Version 1.1. This license is available with a FAQ at: "
                                "https://openfontlicense.org")
    info.openTypeNameLicenseURL = "https://openfontlicense.org"
    info.openTypeNameDescription = ("Every glyph inscribed in one circle; built only from circle "
                                    "arcs and straight lines.")
    info.unitsPerEm = UPM
    asc, desc = vertical_metrics(glyphs)
    info.ascender, info.descender = asc, desc
    info.xHeight = round(2 * XB * S)
    info.capHeight = round((D + BASE) * S)
    info.openTypeHheaAscender, info.openTypeHheaDescender, info.openTypeHheaLineGap = asc, desc, 0
    info.openTypeOS2TypoAscender, info.openTypeOS2TypoDescender, info.openTypeOS2TypoLineGap = asc, desc, 0
    info.openTypeOS2WinAscent, info.openTypeOS2WinDescent = asc, -desc
    info.openTypeOS2Selection = [7]               # USE_TYPO_METRICS
    info.openTypeOS2Type = []                     # installable
    info.openTypeOS2VendorID = "RNDL"
    info.openTypeOS2WeightClass = 400
    info.openTypeOS2WidthClass = 5
    info.openTypeOS2Panose = PANOSE_PROP if mode == "open" else PANOSE_MONO
    info.openTypeOS2StrikeoutSize = round(W * S)
    info.openTypeOS2StrikeoutPosition = round(XB * S)
    info.postscriptUnderlinePosition = -120
    info.postscriptUnderlineThickness = round(W * S * 0.6)
    info.postscriptIsFixedPitch = mode != "open"
    info.postscriptFontName = f"{cfg['family'].replace(' ', '')}-Regular"

    order = list(glyphs)
    categories = {}
    for name in order:
        g = glyphs[name]
        gl = ufo.newGlyph(name)
        gl.width = g["adv"]
        gl.unicodes = g["unicodes"]
        comp = COMPOSITES.get(name) if mode == "open" else None
        if comp:
            _, base, marks = comp
            pen = gl.getPen()
            pen.addComponent(base, (1, 0, 0, 1, 0, 0))
            for mname, kind in marks:
                ax, ay = anchor(base, kind)
                mx = ax * S + glyphs[base]["dx"] - glyphs[mname]["dx"]
                my = (ay - mark_attach_y(kind)) * S
                pen.addComponent(mname, (1, 0, 0, 1, round(mx), round(my)))
        elif mode == "open" and name == "Dcroat":
            gl.getPen().addComponent("Eth", (1, 0, 0, 1, 0, 0))
        else:
            _draw_ufo_outline(gl, g["path"], g["dx"])
        if is_mark(name):
            categories[name] = "mark"
            kind = MARKS[name]["kind"]
            aname = {"top": "_top", "bottom": "_bottom", "ogonek": "_ogonek"}.get(kind)
            if aname and mode == "open":
                ay = mark_attach_y(kind)
                gl.anchors.append(Anchor(round(g["dx"]), round((ay + BASE) * S), aname))
                b = path_bounds(g["path"])
                if kind == "top":                 # lets a second accent stack on top
                    gl.anchors.append(Anchor(round(g["dx"]), round((b[3] + 0.08 + BASE) * S), "top"))
                elif kind == "bottom":
                    gl.anchors.append(Anchor(round(g["dx"]), round((b[1] - 0.06 + BASE) * S), "bottom"))
        elif g["unicodes"] or name in COMPOSITES:
            categories[name] = "base"
    if mode == "open":                            # anchors for combining marks on base letters
        bases = set(c[1] for c in COMPOSITES.values()) | set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghklmnopqrstuvwxyz")
        bases |= {"dotlessi", "dotlessj", "dottedCircle"}
        bases -= set(COMPOSITES)                  # composites inherit anchors (propagateAnchors)
        for base in sorted(bases):
            if base not in ufo:
                continue
            gl = ufo[base]
            dx = glyphs[base]["dx"]
            for kind, aname in (("top", "top"), ("bottom", "bottom")):
                ax, ay = anchor(base, kind)
                gl.anchors.append(Anchor(round(ax * S + dx), round((ay + BASE) * S), aname))
            if base in OGONEK_AT:
                ax, ay = OGONEK_AT[base]
                gl.anchors.append(Anchor(round(ax * S + dx), round((ay + BASE) * S), "ogonek"))
    ufo.lib["public.glyphOrder"] = order
    ufo.lib["public.openTypeMeta"] = {"dlng": ["Latn"], "slng": ["Latn"]}
    if mode == "open":
        ufo.lib["com.github.googlei18n.ufo2ft.filters"] = [{"name": "propagateAnchors", "pre": True}]
    ufo.lib["public.openTypeCategories"] = categories
    ufo.lib["public.postscriptNames"] = {n: production_name(n, glyphs[n]["unicodes"]) for n in order}

    fea = "languagesystem DFLT dflt;\nlanguagesystem latn dflt;\n\n" + ccmp_fea()
    if mode in MONO_MODES:
        fea += "\n" + mono_fea()
    ufo.features.text = fea
    if mode == "open":
        names = [n for n in order if n in glyphs and (
            (glyphs[n]["unicodes"] and unicodedata_category(glyphs[n]["unicodes"][0]).startswith("L"))
            or n in KERN_EXTRA)]
        groups, kerning = auto_kern(glyphs, names)
        ufo.groups.update(groups)
        ufo.kerning.update(kerning)
    import shutil
    if os.path.exists(path_out):
        shutil.rmtree(path_out)
    ufo.save(path_out)
    return path_out, len(order), len(ufo.kerning)


def unicodedata_category(cp):
    import unicodedata
    return unicodedata.category(chr(cp))


def production_name(name, unicodes):
    from fontTools.agl import UV2AGL
    if not unicodes:
        return name
    cp = unicodes[0]
    if cp in UV2AGL:
        return UV2AGL[cp]
    return f"uni{cp:04X}" if cp <= 0xFFFF else f"u{cp:05X}"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Write the Rondel UFO sources.")
    ap.add_argument("--ufo", default="sources/ufo", help="folder for the .ufo sources")
    ap.add_argument("--styles", nargs="*", default=list(STYLES))
    args = ap.parse_args()
    os.makedirs(args.ufo, exist_ok=True)
    for st in args.styles:
        fam = STYLES[st]["family"].replace(" ", "")
        p, n, nk = write_ufo(st, os.path.join(args.ufo, f"{fam}-Regular.ufo"))
        print(f"wrote {p}: {n} glyphs, {nk} kerning pairs")
