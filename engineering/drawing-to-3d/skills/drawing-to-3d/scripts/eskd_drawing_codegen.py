#!/usr/bin/env python3
"""eskd_drawing_codegen.py — generate an associative ЕСКД 2D drawing script from a part spec.

KOMPAS-3D makes an "ассоциативный чертёж": the 2D views are projections of the
3D model, so they follow it. This tool gives the same workflow for the
drawing-to-3d pipeline. It emits ``<part>_drawing.py`` which, on the user's PC:

    pip install cadquery            # brings OCP (OpenCascade) and ezdxf
    python <part>_drawing.py        # -> <part>_drawing.dxf (+ .png/.svg preview, .report.json)

1. imports ``build()`` from ``<part>_cad.py`` in the same folder (by path), so the
   drawing is always re-projected from the current model;
2. projects the solid with OpenCascade hidden-line removal (HLRBRep) into the
   standard views in FIRST-ANGLE arrangement, ГОСТ 2.305: вид спереди (main view,
   looking along +Y, X right, Z up; revolved parts are laid with the axis
   horizontal), вид сверху BELOW it, вид слева to the RIGHT of it, optional
   isometric. Visible edges: сплошная основная (0.5 mm); hidden: штриховая (thin);
   axes/centre lines: штрихпунктирная тонкая (ГОСТ 2.303);
3. picks a ГОСТ 2.302 scale and an ГОСТ 2.301 sheet (A4 portrait, A3..A0
   landscape) so the views, dimensions and callouts fit around the title block;
4. draws the frame (20 mm left margin, 5 mm others), the main title block form 1
   per ГОСТ 2.104 (185 x 55, with graph 26 and the left additional graphs) and
   fills name, designation, material, mass (model volume x density), scale, sheet;
5. adds overall dimensions as real DIMENSION entities (decimal comma, the true
   size shown at any scale via DIMLFAC), hole callouts on leaders with a shelf
   ("4 отв. %%c9"), hole-pattern pitch / PCD dimensions, centre lines, and the
   технические требования block from the spec's general-note assumptions;
6. writes DXF R2010 (opens in KOMPAS-3D, AutoCAD, LibreCAD, FreeCAD), a PNG/SVG
   preview, and a self-check report (view extents vs the model bounding box x
   scale, projection alignment, frame/title-block geometry re-read from the DXF).

This tool is stdlib-only and never imports CadQuery; it emits text. It refuses a
spec that fails part_spec_validator.py (exit 2) or has open questions (exit 3).

Exit codes: 0 written · 2 spec invalid · 3 open questions · 64 usage.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from part_spec_validator import SAMPLE_SPEC, expand_positions, load_json, validate  # noqa: E402

SHEETS = ("A4", "A3", "A2", "A1", "A0")
VIEW_NAMES = ("front", "top", "left", "iso")
# ГОСТ 2.302-68 standard scales (value, label). Labels use the ЕСКД decimal comma.
SCALES = [(100, "100:1"), (50, "50:1"), (40, "40:1"), (20, "20:1"), (10, "10:1"), (5, "5:1"),
          (4, "4:1"), (2.5, "2,5:1"), (2, "2:1"), (1, "1:1"), (0.5, "1:2"), (0.4, "1:2,5"),
          (0.25, "1:4"), (0.2, "1:5"), (0.1, "1:10"), (1 / 15, "1:15"), (0.05, "1:20"),
          (0.04, "1:25"), (0.025, "1:40"), (0.02, "1:50"), (1 / 75, "1:75"), (0.01, "1:100")]

# Density, g/cm³, matched by substring of the lower-cased material (Russian grades first).
DENSITY = [
    (("12х18н10т", "08х18н10", "нерж", "stainless", "aisi 3"), 7.9),
    (("д16", "1163"), 2.78),
    (("амг", "ад31", "ад0", "ак", "алюм", "alum", "6061", "6082", "7075", "5083"), 2.7),
    (("латун", "лс59", "л63", "brass"), 8.5),
    (("бронз", "бр", "bronze"), 8.8),
    (("медь", "м1", "м2", "copper"), 8.9),
    (("титан", "вт1", "вт6", "titan"), 4.5),
    (("чугун", "сч", "вч", "cast iron"), 7.2),
    (("сталь", "ст3", "steel", "40х", "65г", "у8", "у10"), 7.85),
    (("капролон", "полиамид", "nylon", "pa6"), 1.15),
    (("фторопласт", "ptfe"), 2.2),
    (("полиацеталь", "delrin", "pom"), 1.41),
    (("abs",), 1.05),
    (("pla",), 1.24),
]

NOTE_HINTS = ("general note", "общ", "неуказан", "техн", "тт ", "примечан")


# ---------------------------------------------------------------- spec -> annotation data

def num(v):
    """ЕСКД number: decimal comma, no trailing zeros."""
    s = f"{float(v):.3f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def comma(text):
    """Decimal point between digits -> comma (M8x1.25-6H -> M8x1,25-6H)."""
    return re.sub(r"(?<=\d)\.(?=\d)", ",", str(text))


def density_for(spec):
    if spec.get("density_g_cm3"):
        return float(spec["density_g_cm3"]), "spec density_g_cm3"
    mat = str(spec.get("material") or "").lower()
    for keys, rho in DENSITY:
        if any(k in mat for k in keys):
            return rho, f"table ({keys[0]})"
    return None, None


def hole_callout(f, n, thread):
    pre = f"{n} отв. " if n > 1 else ""
    blind = f.get("depth") != "through"
    deep = f" глуб. {num(f['depth'])}" if blind else ""
    if thread:
        tl = thread.get("length")
        line1 = f"{pre}{comma(thread['designation'])}" + (f" глуб. {num(tl)}" if tl else "")
        return [line1, f"сверл. %%c{num(f['diameter'])}{deep}" if blind else None]
    line1 = f"{pre}%%c{num(f['diameter'])}{deep}"
    if f.get("cbore"):
        cb = f["cbore"]
        return [line1, f"цековка %%c{num(cb['diameter'])} глуб. {num(cb['depth'])}"]
    if f.get("csk"):
        ck = f["csk"]
        return [line1, f"зенковка %%c{num(ck['diameter'])}x{num(ck.get('angle_deg', 90))}°"]
    return [line1, None]


def annotation_data(spec):
    feats = [f for f in spec.get("features", []) if isinstance(f, dict)]
    threads = {f.get("on"): f for f in feats if f.get("type") == "thread"}
    holes = []
    for f in feats:
        if f.get("type") != "hole":
            continue
        pts = expand_positions(f)
        if not pts:
            continue
        pat = f.get("pattern") if isinstance(f.get("pattern"), dict) else {}
        extra = [f["diameter"]]
        if f.get("cbore"):
            extra.append(f["cbore"]["diameter"])
        if f.get("csk"):
            extra.append(f["csk"]["diameter"])
        holes.append({
            "id": f.get("id"), "face": f["face"], "d": float(f["diameter"]),
            "d_max": float(max(extra)), "pts": [list(p) for p in pts],
            "callout": hole_callout(f, len(pts), threads.get(f.get("id"))),
            "polar": ({"pcd": float(pat["pcd"]), "center": list((pat.get("center") or [0, 0])[:2])}
                      if pat.get("type") == "polar" else None),
            "grid": ({"nx": int(pat.get("nx", 1)), "ny": int(pat.get("ny", 1)),
                      "pitch_x": float(pat.get("pitch_x", 0)), "pitch_y": float(pat.get("pitch_y", 0))}
                     if pat.get("type") == "grid" else None),
        })
    notes = []
    for a in spec.get("assumptions", []) or []:
        if not isinstance(a, dict):
            continue
        src = str(a.get("source", "")).lower()
        if a.get("note") or a.get("kind") == "note" or any(h in src for h in NOTE_HINTS):
            m = re.search(r"['‘’\"«]([^'‘’\"»]*(?:неуказан|общ)[^'‘’\"»]*)['‘’\"»]", str(a.get("source", "")),
                          re.IGNORECASE)
            notes.append(comma(m.group(1)) if m else comma(f"{a['what']}: {a['value']}"))
    drawing = spec.get("drawing") or {}
    for n in drawing.get("notes", []) or []:
        if isinstance(n, str) and n.strip():
            notes.append(comma(n.strip()))
    seen, uniq = set(), []
    for n in notes:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    base = spec["base"]
    prof = base.get("profile") or {}
    rho, rho_src = density_for(spec)
    return {
        "title": str(drawing.get("title") or spec["part"]),
        "number": str(drawing.get("number") or ""),
        "material": str(spec.get("material") or ""),
        "mass_kg": spec.get("mass_kg"),
        "density_g_cm3": rho, "density_source": rho_src,
        "revolved": base.get("type") == "revolve" or (base.get("type") == "extrude" and prof.get("shape") == "circle"),
        "holes": holes,
        "notes": uniq,
    }


def fingerprint(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- template

TEMPLATE = r'''#!/usr/bin/env python3
"""__PART__ — associative ЕСКД drawing (ассоциативный чертёж) of drawing __DRAWING_ID__.

Generated by drawing-to-3d/eskd_drawing_codegen.py from __SPEC_NAME__. The views
are re-projected from the 3D model every run: change the spec, regenerate
__CAD_SCRIPT__ and this script, run again — the drawing follows the model.

    pip install cadquery                     # brings OCP (OpenCascade) and ezdxf
    python __PART___drawing.py               # -> __PART___drawing.dxf/.png/.svg/.report.json
    python __PART___drawing.py --sheet A3 --scale 1:2 --views front,top,left,iso

Standards: ГОСТ 2.301 (formats), 2.302 (scales), 2.303 (lines), 2.304 (font),
2.305 (views, first-angle), 2.307 (dimensions), 2.316 (notes), 2.104 (title block
form 1). The DXF uses text style "GOST type A" with font file isocpeur.ttf; the
CAD that opens it substitutes its own ЕСКД font if that file is missing.
"""
import argparse
import importlib.util
import json
import math
import os
import sys
import textwrap

import ezdxf
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepLib import BRepLib
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Line
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS

HERE = os.path.dirname(os.path.abspath(__file__))
PART = __PART_REPR__
CAD_SCRIPT = __CAD_REPR__
SPEC_FINGERPRINT = __FP_REPR__
ANNOT = __ANNOT__
DEFAULT_SHEET = __SHEET_REPR__
DEFAULT_SCALE = __SCALE_REPR__
DEFAULT_VIEWS = __VIEWS_REPR__

SHEETS = {"A4": (210, 297), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}
SCALES = __SCALES__
PLANS = []
FONT_STYLE, FONT_FILE, FONT_FAMILY = "GOST type A", "isocpeur.ttf", "GOST type A"
TB_W, TB_H = 185.0, 55.0          # title block form 1
DIM_FIRST, DIM_STEP = 10.0, 8.0   # ГОСТ 2.307: 6-10 mm to the contour, 7-10 between dimension lines
GAP = 14.0                        # clear space between padded views
CL_OVER = 3.0                     # centre lines run 2-5 mm past the contour
TXT = 3.5                         # main text height
CHAR_W = 0.7                      # text width per char / height (estimate for fitting)


# ---------------------------------------------------------------- model

def load_cad():
    path = os.path.join(HERE, CAD_SCRIPT)
    if not os.path.exists(path):
        sys.exit(f"error: {CAD_SCRIPT} not found next to this script; generate it with cadquery_codegen.py")
    spec = importlib.util.spec_from_file_location(f"{PART}_cad", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def unit(a):
    n = math.sqrt(dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n)


def neg(a):
    return (-a[0], -a[1], -a[2])


def view_frames(revolved):
    """(N toward the viewer, X right) per view. Main view: right r, up u; first angle."""
    r, u = ((0, 0, 1), (0, 1, 0)) if revolved else ((1, 0, 0), (0, 0, 1))
    nf = cross(r, u)
    iso_n = unit(tuple(nf[i] - r[i] + u[i] for i in range(3)))      # front + left + top faces seen
    iso_x = unit(cross(u, iso_n))
    return {"front": (nf, r), "top": (u, r), "left": (neg(r), nf), "iso": (iso_n, iso_x)}


def ydir(n, x):
    return cross(n, x)


# ---------------------------------------------------------------- HLR projection

def _compound_edges(comp):
    if comp is None or comp.IsNull():
        return []
    BRepLib.BuildCurves3d_s(comp)
    out, ex = [], TopExp_Explorer(comp, TopAbs_EDGE)
    while ex.More():
        out.append(TopoDS.Edge_s(ex.Current()))
        ex.Next()
    return out


def _ang(c, p):
    return math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360


def edge_prims(edge, defl):
    """2D primitives of one projected edge: line / circle / arc / polyline."""
    ad = BRepAdaptor_Curve(edge)
    t0, t1 = ad.FirstParameter(), ad.LastParameter()
    P = lambda t: (ad.Value(t).X(), ad.Value(t).Y())  # noqa: E731
    kind = ad.GetType()
    if kind == GeomAbs_Line:
        a, b = P(t0), P(t1)
        return [("line", a, b)] if math.dist(a, b) > 1e-6 else []
    if kind == GeomAbs_Circle:
        c = ad.Circle()
        ctr, rad = (c.Location().X(), c.Location().Y()), c.Radius()
        if abs(t1 - t0) >= 2 * math.pi - 1e-7:
            return [("circle", ctr, rad)]
        a0, am, a1 = _ang(ctr, P(t0)), _ang(ctr, P((t0 + t1) / 2)), _ang(ctr, P(t1))
        if (am - a0) % 360 <= (a1 - a0) % 360:
            return [("arc", ctr, rad, a0, a1)]
        return [("arc", ctr, rad, a1, a0)]
    qd = GCPnts_QuasiUniformDeflection(ad, defl, t0, t1)
    pts = [(qd.Value(i).X(), qd.Value(i).Y()) for i in range(1, qd.NbPoints() + 1)] if qd.IsDone() else [P(t0), P(t1)]
    return [("poly", pts)] if len(pts) > 1 else []


def project(shape, n, x, hidden=True):
    """Hidden-line removal: {'visible': prims, 'hidden': prims} in projection coordinates."""
    algo = HLRBRep_Algo()
    algo.Add(shape)
    algo.Projector(HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*n), gp_Dir(*x))))
    algo.Update()
    algo.Hide()
    h = HLRBRep_HLRToShape(algo)
    out = {"visible": [], "hidden": []}
    # Sharp edges + silhouettes; smooth (tangent) and seam edges are not drawn per ЕСКД practice.
    for e in _compound_edges(h.VCompound()) + _compound_edges(h.OutLineVCompound()):
        out["visible"] += edge_prims(e, 0.01)
    if hidden:
        for e in _compound_edges(h.HCompound()) + _compound_edges(h.OutLineHCompound()):
            out["hidden"] += edge_prims(e, 0.01)
    return out


def prim_points(p):
    if p[0] == "line":
        return [p[1], p[2]]
    if p[0] == "poly":
        return p[1]
    c, r = p[1], p[2]
    if p[0] == "circle":
        return [(c[0] + r * math.cos(math.radians(a)), c[1] + r * math.sin(math.radians(a))) for a in range(0, 360, 2)]
    a0, a1 = p[3], p[4]
    span = (a1 - a0) % 360 or 360
    k = max(2, int(span / 2))
    return [(c[0] + r * math.cos(math.radians(a0 + span * i / k)), c[1] + r * math.sin(math.radians(a0 + span * i / k)))
            for i in range(k + 1)]


def extents(prims):
    pts = [q for p in prims for q in prim_points(p)]
    xs, ys = [q[0] for q in pts], [q[1] for q in pts]
    return (min(xs), min(ys), max(xs), max(ys)), pts


# ---------------------------------------------------------------- model queries (associativity)

def bbox_corners(bb):
    return [(x, y, z) for x in (bb.xmin, bb.xmax) for y in (bb.ymin, bb.ymax) for z in (bb.zmin, bb.zmax)]


def axis_faces(shape):
    """[(kind, radius, point, dir, (lo, hi) along global axes)] for cylinders and cones."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    out, ex = [], TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        f = TopoDS.Face_s(ex.Current())
        s = BRepAdaptor_Surface(f)
        kind = s.GetType()
        if kind in (GeomAbs_Cylinder, GeomAbs_Cone):
            g = s.Cylinder() if kind == GeomAbs_Cylinder else s.Cone()
            ax = g.Axis()
            loc = ax.Location()
            d = ax.Direction()
            b = Bnd_Box()
            BRepBndLib.AddOptimal_s(f, b, False, False)
            lo = b.Get()
            rad = g.Radius() if kind == GeomAbs_Cylinder else g.RefRadius()
            out.append((kind, rad, (loc.X(), loc.Y(), loc.Z()), (d.X(), d.Y(), d.Z()),
                        (lo[0:3], lo[3:6])))
        ex.Next()
    return out


def hole_axis(face, u, v):
    if face in ("XY", ">Z", "<Z"):
        return (u, v, 0.0), (0.0, 0.0, 1.0)
    if face in ("YZ", ">X", "<X"):
        return (0.0, u, v), (1.0, 0.0, 0.0)
    return (u, 0.0, v), (0.0, 1.0, 0.0)


def hole_extent(afaces, p, d, bb):
    """Range of the hole along its axis, from the model's coaxial cylinders/cones."""
    k = [i for i in range(3) if abs(d[i]) > 0.5][0]
    lo, hi, found = None, None, False
    for kind, rad, loc, adir, (bmin, bmax) in afaces:
        if abs(abs(dot(adir, d)) - 1) > 1e-6:
            continue
        off = sub(loc, p)
        perp = [off[i] - dot(off, d) * d[i] for i in range(3)]
        if math.sqrt(dot(perp, perp)) > 1e-3:
            continue
        found = True
        lo = bmin[k] if lo is None else min(lo, bmin[k])
        hi = bmax[k] if hi is None else max(hi, bmax[k])
    if not found:
        rng = [(bb.xmin, bb.xmax), (bb.ymin, bb.ymax), (bb.zmin, bb.zmax)][k]
        return rng, False
    return (lo, hi), True


# ---------------------------------------------------------------- layout

class View:
    def __init__(self, name, n, x, geo, bb):
        self.name, self.n, self.x, self.y = name, n, x, ydir(n, x)
        self.geo = geo
        prims = geo["visible"] + geo["hidden"]
        (self.xmin, self.ymin, self.xmax, self.ymax), _ = extents(prims)
        self.w, self.h = self.xmax - self.xmin, self.ymax - self.ymin
        cs = bbox_corners(bb)
        px, py = [dot(c, self.x) for c in cs], [dot(c, self.y) for c in cs]
        self.expect = (min(px), min(py), max(px), max(py))
        self.pad = {"l": 6.0, "r": 6.0, "b": 6.0, "t": 6.0}
        self.ox = self.oy = 0.0
        self.s = 1.0

    def to_paper(self, x, y):
        return (self.ox + self.s * (x - self.xmin), self.oy + self.s * (y - self.ymin))

    def p3(self, p):
        return self.to_paper(dot(p, self.x), dot(p, self.y))

    def rect(self):
        """Padded rectangle in paper mm (x0, y0, x1, y1)."""
        return (self.ox - self.pad["l"], self.oy - self.pad["b"],
                self.ox + self.s * self.w + self.pad["r"], self.oy + self.s * self.h + self.pad["t"])


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def inside(a, b):
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def arrange(views, s, sheet, notes_h):
    """Place views first-angle at scale s on sheet. Returns True if everything fits."""
    W, H = SHEETS[sheet] if sheet != "A4" else (210, 297)
    for v in views.values():
        v.s = s
    layout_callouts(views)
    f = views["front"]
    f.ox, f.oy = 0.0, 0.0
    fw, fh = f.s * f.w, f.s * f.h
    if "left" in views:
        lv = views["left"]
        lv.ox, lv.oy = fw + f.pad["r"] + GAP + lv.pad["l"], 0.0
    if "top" in views:
        tv = views["top"]
        tv.ox, tv.oy = 0.0, -(f.pad["b"] + GAP + tv.pad["t"] + tv.s * tv.h)
    if "iso" in views:
        iv = views["iso"]
        col2 = views["left"].rect()[0] + views["left"].pad["l"] if "left" in views else fw + f.pad["r"] + GAP + iv.pad["l"]
        if "top" in views:
            tt = views["top"].rect()
            iv.ox = max(col2, tt[2] + GAP + iv.pad["l"])
            iv.oy = tt[3] - iv.pad["t"] - iv.s * iv.h
        else:
            iv.ox, iv.oy = col2, -(f.pad["b"] + GAP + iv.pad["t"] + iv.s * iv.h)
    rects = [v.rect() for v in views.values()]
    bx0, by0 = min(r[0] for r in rects), min(r[1] for r in rects)
    bx1, by1 = max(r[2] for r in rects), max(r[3] for r in rects)
    inner = (20.0 + 3, 5.0 + 3, W - 5.0 - 3, H - 5.0 - 3)
    forbid = [(W - 5 - TB_W, 5, W - 5, 5 + TB_H + (notes_h + 8 if notes_h else 0)),
              (20, H - 5 - 14, 90, H - 5)]                 # title block + notes, graph 26
    bw, bh = bx1 - bx0, by1 - by0
    steps = [i / 20 for i in range(21)]
    grid = sorted(((fx, fy) for fx in steps for fy in steps), key=lambda q: abs(q[0] - 0.5) + abs(q[1] - 0.5))
    cands = [(inner[0] + fx * (inner[2] - inner[0] - bw), inner[1] + fy * (inner[3] - inner[1] - bh))
             for fx, fy in grid]
    for cx, cy in cands:
        dx, dy = cx - bx0, cy - by0
        ok = True
        for v in views.values():
            r = v.rect()
            r = (r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy)
            if not inside(r, inner) or any(overlap(r, fb) for fb in forbid):
                ok = False
                break
        if ok:
            for v in views.values():
                v.ox += dx
                v.oy += dy
            return True
    return False


def choose(views, sheet_arg, scale_arg, notes_h, max_dim):
    sheets = list(SHEETS) if sheet_arg == "auto" else [sheet_arg]
    scales = SCALES if scale_arg == "auto" else [x for x in SCALES if x[1] == scale_arg]
    if scale_arg == "auto":
        # Phase 1: 1:1 or enlargement on the smallest sheet; enlargement only while the
        # part stays under 200 mm on paper. Phase 2: reductions, preferring A3..A1.
        for sh in sheets:
            for val, lab in scales:
                if val >= 1 and (val == 1 or max_dim * val <= 200) and arrange(views, val, sh, notes_h):
                    return sh, val, lab, True
        for sh in sheets:
            for val, lab in scales:
                if val < 1 and arrange(views, val, sh, notes_h):
                    return sh, val, lab, True
    else:
        for sh in sheets:
            for val, lab in scales:
                if arrange(views, val, sh, notes_h):
                    return sh, val, lab, True
    sh, (val, lab) = sheets[-1], scales[-1]
    arrange(views, val, sh, notes_h)
    return sh, val, lab, False


# ---------------------------------------------------------------- DXF setup

def new_doc():
    doc = ezdxf.new("R2010", setup=False, units=4)
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LWDISPLAY"] = 1
    doc.header["$LTSCALE"] = 1.0
    doc.header["$DIMDSEP"] = ord(",")
    st = doc.styles.add(FONT_STYLE, font=FONT_FILE)
    try:
        st.set_extended_font_data(family=FONT_FAMILY, italic=True)
    except Exception:  # older ezdxf
        pass
    doc.header["$TEXTSTYLE"] = FONT_STYLE
    # ГОСТ 2.303: штриховая 2-8 / 1-2 mm; штрихпунктирная тонкая 5-30 / 3-5 mm.
    doc.linetypes.add("ESKD_HIDDEN", pattern=[5.5, 4.0, -1.5], description="ГОСТ 2.303 штриховая __ __ __")
    doc.linetypes.add("ESKD_AXIS", pattern=[22.0, 16.0, -2.75, 0.5, -2.75],
                      description="ГОСТ 2.303 штрихпунктирная тонкая ____ . ____")
    for name, lt, lw in (("CONTOUR", "Continuous", 50), ("HIDDEN", "ESKD_HIDDEN", 25),
                         ("AXIS", "ESKD_AXIS", 25), ("THIN", "Continuous", 25), ("DIM", "Continuous", 25),
                         ("TEXT", "Continuous", 25), ("FRAME", "Continuous", 50), ("FRAME_THIN", "Continuous", 25)):
        doc.layers.add(name, color=7, linetype=lt, lineweight=lw)
    ds = doc.dimstyles.new("ESKD")
    for k, val in {"dimtxsty": FONT_STYLE, "dimtxt": TXT, "dimasz": 3.0, "dimexe": 2.0, "dimexo": 0.0,
                   "dimgap": 1.0, "dimtad": 1, "dimtih": 0, "dimtoh": 0, "dimdec": 2, "dimzin": 8,
                   "dimdsep": ord(","), "dimlunit": 2, "dimlwd": 25, "dimlwe": 25, "dimscale": 1.0,
                   "dimdli": DIM_STEP, "dimclrd": 256, "dimclre": 256, "dimclrt": 256}.items():
        ds.dxf.set(k, val)
    return doc


def text(msp, s, x, y, h=TXT, align="MIDDLE_CENTER", rot=0.0, layer="TEXT"):
    from ezdxf.enums import TextEntityAlignment
    t = msp.add_text(s, height=h, rotation=rot, dxfattribs={"style": FONT_STYLE, "layer": layer})
    t.set_placement((x, y), align=getattr(TextEntityAlignment, align))
    return t


def tw(s, h):
    return len(s.replace("%%c", "Ø")) * h * CHAR_W


def fit_text(msp, s, x0, y0, x1, y1, hs=(5.0, 3.5, 2.5)):
    """Text centred in a box; shrinks, then wraps onto two lines."""
    w, hbox = x1 - x0 - 2, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for h in hs:
        if tw(s, h) <= w and h <= hbox * 0.7:
            return [text(msp, s, cx, cy, h)]
    words = s.split()
    if len(words) > 1:
        best = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
        a, b = " ".join(words[:best]), " ".join(words[best:])
        for h in hs:
            if max(tw(a, h), tw(b, h)) <= w and 2.6 * h <= hbox:
                return [text(msp, a, cx, cy + 0.8 * h, h), text(msp, b, cx, cy - 0.8 * h, h)]
    h = max(1.8, min(hs[-1], w / max(1, len(s) * CHAR_W)))
    return [text(msp, s, cx, cy, h)]


# ---------------------------------------------------------------- sheet: frame + title block form 1

def frame_and_title(msp, sheet, fields):
    W, H = SHEETS[sheet] if sheet != "A4" else (210, 297)
    msp.add_lwpolyline([(0, 0), (W, 0), (W, H), (0, H)], close=True, dxfattribs={"layer": "FRAME_THIN"})  # trim line
    msp.add_lwpolyline([(20, 5), (W - 5, 5), (W - 5, H - 5), (20, H - 5)], close=True, dxfattribs={"layer": "FRAME"})
    X0, Y0 = W - 5 - TB_W, 5.0

    def L(x0, y0, x1, y1, thick=True):
        msp.add_line((X0 + x0, Y0 + y0), (X0 + x1, Y0 + y1), dxfattribs={"layer": "FRAME" if thick else "FRAME_THIN"})

    msp.add_lwpolyline([(X0, Y0), (X0 + TB_W, Y0), (X0 + TB_W, Y0 + TB_H), (X0, Y0 + TB_H)], close=True,
                       dxfattribs={"layer": "FRAME"})
    # Left part (65 wide): 11 rows x 5 mm; columns 7 | 10 | 23 | 15 | 10.
    for y in (5, 10, 15, 20, 25, 40, 45, 50):
        L(0, y, 65, y, thick=False)
    for y in (30, 35):
        L(0, y, 65, y)
    L(7, 30, 7, 55)
    for x in (17, 40, 55, 65):
        L(x, 0, x, 55)
    # Graph 2 (designation) 120 x 15 at the top; graph 1 (name) 70 x 25; graph 3 (material) 70 x 15.
    L(65, 40, 185, 40)
    L(65, 15, 185, 15)
    L(135, 0, 135, 40)
    # Right part (50): Лит. 15 (3 x 5) | Масса 17 | Масштаб 18; Лист / Листов row; graph 9 (organisation).
    L(135, 35, 185, 35)
    L(135, 20, 185, 20)
    L(150, 20, 150, 40)
    L(167, 20, 167, 40)
    L(140, 20, 140, 35, thick=False)
    L(145, 20, 145, 35, thick=False)
    L(155, 15, 155, 20, thick=False)

    for x, lab in ((3.5, "Изм."), (12, "Лист"), (28.5, "№ докум."), (47.5, "Подп."), (60, "Дата")):
        text(msp, lab, X0 + x, Y0 + 32.5, 2.5)
    for i, lab in enumerate(("Разраб.", "Пров.", "Т.контр.", "", "Н.контр.", "Утв.")):
        if lab:
            text(msp, lab, X0 + 1, Y0 + 27.5 - 5 * i, 2.5, align="MIDDLE_LEFT")
    for x, lab in ((142.5, "Лит."), (158.5, "Масса"), (176, "Масштаб")):
        text(msp, lab, X0 + x, Y0 + 37.5, 2.5)
    text(msp, f"Лист {fields['sheet_no']}", X0 + 136, Y0 + 17.5, 2.5, align="MIDDLE_LEFT")
    text(msp, f"Листов {fields['sheets']}", X0 + 156, Y0 + 17.5, 2.5, align="MIDDLE_LEFT")
    if fields["number"]:
        fit_text(msp, fields["number"], X0 + 65, Y0 + 40, X0 + 185, Y0 + 55, hs=(7.0, 5.0, 3.5))
    fit_text(msp, fields["title"], X0 + 65, Y0 + 15, X0 + 135, Y0 + 40, hs=(5.0, 3.5))
    if fields["material"]:
        fit_text(msp, fields["material"], X0 + 65, Y0, X0 + 135, Y0 + 15, hs=(3.5, 2.5))
    if fields["mass"]:
        text(msp, fields["mass"], X0 + 158.5, Y0 + 27.5, TXT)
    text(msp, fields["scale"], X0 + 176, Y0 + 27.5, 5.0)
    # Graph 26: designation rotated 180° in a 70 x 14 box at the top-left corner of the frame.
    msp.add_lwpolyline([(20, H - 5), (90, H - 5), (90, H - 19), (20, H - 19)], close=True,
                       dxfattribs={"layer": "FRAME"})
    if fields["number"]:
        text(msp, fields["number"], 55, H - 12, 5.0 if tw(fields["number"], 5) < 66 else 3.5, rot=180)
    # Left additional graphs 19-23 (12 wide, on the binding margin).
    y = 5.0
    msp.add_line((8, 5), (8, 150), dxfattribs={"layer": "FRAME"})
    msp.add_line((13, 5), (13, 150), dxfattribs={"layer": "FRAME"})
    for hgt, lab in ((25, "Инв. № подл."), (35, "Подп. и дата"), (25, "Взам. инв. №"),
                     (25, "Инв. № дубл."), (35, "Подп. и дата")):
        msp.add_line((8, y + hgt), (20, y + hgt), dxfattribs={"layer": "FRAME"})
        text(msp, lab, 10.5, y + hgt / 2, 2.5, rot=90)
        y += hgt
    text(msp, "Копировал", X0 + 100, 2.5, 2.5)
    text(msp, f"Формат {sheet}", W - 5 - 25, 2.5, 2.5)
    return (X0, Y0)


# ---------------------------------------------------------------- annotation

def add_prims(msp, v, prims, layer):
    s = v.s
    for p in prims:
        if p[0] == "line":
            msp.add_line(v.to_paper(*p[1]), v.to_paper(*p[2]), dxfattribs={"layer": layer})
        elif p[0] == "circle":
            msp.add_circle(v.to_paper(*p[1]), s * p[2], dxfattribs={"layer": layer})
        elif p[0] == "arc":
            msp.add_arc(v.to_paper(*p[1]), s * p[2], p[3], p[4], dxfattribs={"layer": layer})
        else:
            msp.add_lwpolyline([v.to_paper(*q) for q in p[1]], dxfattribs={"layer": layer})


def centre_cross(msp, c, r):
    """Centre lines of a circle; solid thin if Ø < 12 mm on paper (ГОСТ 2.303)."""
    layer = "THIN" if 2 * r < 12 else "AXIS"
    e = r + CL_OVER
    msp.add_line((c[0] - e, c[1]), (c[0] + e, c[1]), dxfattribs={"layer": layer})
    msp.add_line((c[0], c[1] - e), (c[0], c[1] + e), dxfattribs={"layer": layer})


def linear_dim(msp, p1, p2, base, angle, s, label=None):
    d = msp.add_linear_dim(base=base, p1=p1, p2=p2, angle=angle, dimstyle="ESKD",
                           text=label or "<>", override={"dimlfac": 1.0 / s},
                           dxfattribs={"layer": "DIM"})
    d.render()
    return d


def extreme(pts, axis, want_max, tie_axis, tie_max):
    key = (lambda q: q[axis]) if not want_max else (lambda q: -q[axis])
    best = min(pts, key=key)[axis]
    cand = [q for q in pts if abs(q[axis] - best) < 1e-6]
    return (max if tie_max else min)(cand, key=lambda q: q[tie_axis])


def annotate(msp, views, model, warns):
    """Dimensions, centre lines, hole callouts. Returns counts."""
    bb, afaces = model["bb"], model["afaces"]
    counts = {"dimensions": 0, "callouts": 0, "centre_lines": 0}
    revolved = ANNOT["revolved"]
    # --- overall dimensions
    f = views["front"]
    fpts = [f.to_paper(*q) for q in extents(f.geo["visible"])[1]]
    if revolved:
        a, b = extreme(fpts, 0, False, 1, False), extreme(fpts, 0, True, 1, False)
        ybot = min(q[1] for q in fpts)
        linear_dim(msp, a, b, (a[0], ybot - DIM_FIRST), 0, f.s)
        a, b = extreme(fpts, 1, False, 0, False), extreme(fpts, 1, True, 0, False)
        xl = min(q[0] for q in fpts)
        linear_dim(msp, a, b, (xl - DIM_FIRST, a[1]), 90, f.s, "%%c<>")
        counts["dimensions"] += 2
    else:
        a, b = extreme(fpts, 1, False, 0, False), extreme(fpts, 1, True, 0, False)
        xl = min(q[0] for q in fpts)
        linear_dim(msp, a, b, (xl - DIM_FIRST - (DIM_STEP if v_has_pitch(views, "front", "y") else 0), a[1]), 90, f.s)
        counts["dimensions"] += 1
        plan = views.get("top")
        if plan is not None:
            tpts = [plan.to_paper(*q) for q in extents(plan.geo["visible"])[1]]
            extra_x = DIM_STEP if v_has_pitch(views, "top", "x") else 0
            extra_y = DIM_STEP if v_has_pitch(views, "top", "y") else 0
            a, b = extreme(tpts, 0, False, 1, False), extreme(tpts, 0, True, 1, False)
            ybot = min(q[1] for q in tpts)
            linear_dim(msp, a, b, (a[0], ybot - DIM_FIRST - extra_x), 0, plan.s)
            a, b = extreme(tpts, 1, False, 0, False), extreme(tpts, 1, True, 0, False)
            xl = min(q[0] for q in tpts)
            linear_dim(msp, a, b, (xl - DIM_FIRST - extra_y, a[1]), 90, plan.s)
            counts["dimensions"] += 2
        else:
            lv = views.get("left")
            if lv is not None:
                lpts = [lv.to_paper(*q) for q in extents(lv.geo["visible"])[1]]
                a, b = extreme(lpts, 0, False, 1, False), extreme(lpts, 0, True, 1, False)
                ybot = min(q[1] for q in lpts)
                linear_dim(msp, a, b, (a[0], ybot - DIM_FIRST), 0, lv.s)
                counts["dimensions"] += 1
    # --- part axis of a revolved body
    if revolved:
        p0, d = (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        for v in views.values():
            if v.name == "iso":
                continue
            if abs(abs(dot(v.n, d)) - 1) < 1e-6:
                c = v.p3(p0)
                r = v.s * max(bb.xmax - bb.xmin, bb.ymax - bb.ymin) / 2
                e = r + 5
                msp.add_line((c[0] - e, c[1]), (c[0] + e, c[1]), dxfattribs={"layer": "AXIS"})
                msp.add_line((c[0], c[1] - e), (c[0], c[1] + e), dxfattribs={"layer": "AXIS"})
            else:
                a, b = v.p3((0, 0, bb.zmin)), v.p3((0, 0, bb.zmax))
                u = unit((b[0] - a[0], b[1] - a[1], 0))
                msp.add_line((a[0] - 5 * u[0], a[1] - 5 * u[1]), (b[0] + 5 * u[0], b[1] + 5 * u[1]),
                             dxfattribs={"layer": "AXIS"})
            counts["centre_lines"] += 1
    # --- holes: centre lines in every view, callout in the view that shows them as circles
    for hole in ANNOT["holes"]:
        circ_views = []
        for u, vv in hole["pts"]:
            p, d = hole_axis(hole["face"], u, vv)
            (lo, hi), found = hole_extent(afaces, p, d, bb)
            if not found:
                warns.append(f"hole {hole['id']} at ({u:g}, {vv:g}) not found in the model (no coaxial cylinder)")
            k = [i for i in range(3) if abs(d[i]) > 0.5][0]
            pa, pb = list(p), list(p)
            pa[k], pb[k] = lo, hi
            for v in views.values():
                if v.name == "iso":
                    continue
                if abs(abs(dot(v.n, d)) - 1) < 1e-6:          # seen as a circle
                    c = v.p3(p)
                    if hole["polar"] is None:
                        centre_cross(msp, c, v.s * hole["d_max"] / 2)
                        counts["centre_lines"] += 1
                    if v not in circ_views:
                        circ_views.append(v)
                else:                                          # seen from the side: axis line
                    a, b = v.p3(tuple(pa)), v.p3(tuple(pb))
                    if math.dist(a, b) < 1e-6:
                        continue
                    uu = ((b[0] - a[0]) / math.dist(a, b), (b[1] - a[1]) / math.dist(a, b))
                    msp.add_line((a[0] - CL_OVER * uu[0], a[1] - CL_OVER * uu[1]),
                                 (b[0] + CL_OVER * uu[0], b[1] + CL_OVER * uu[1]), dxfattribs={"layer": "AXIS"})
                    counts["centre_lines"] += 1
        main = next((pl["view"] for pl in PLANS if pl and pl["hole"] is hole), None)
        for v in circ_views:
            draw_pattern(msp, v, hole, v.name == main, counts)
    return counts


def draw_pattern(msp, v, hole, main, counts):
    """PCD circle + radial centre marks for a polar pattern; pitch dims for a grid (plan view)."""
    if hole["polar"]:
        pc = hole["polar"]
        p, d = hole_axis(hole["face"], pc["center"][0], pc["center"][1])
        cc = v.p3(p)
        rr = v.s * pc["pcd"] / 2
        msp.add_circle(cc, rr, dxfattribs={"layer": "AXIS"})
        for u, vv in hole["pts"]:
            hc = v.p3(hole_axis(hole["face"], u, vv)[0])
            ang = math.atan2(hc[1] - cc[1], hc[0] - cc[0])
            e = v.s * hole["d_max"] / 2 + CL_OVER
            msp.add_line((hc[0] - e * math.cos(ang), hc[1] - e * math.sin(ang)),
                         (hc[0] + e * math.cos(ang), hc[1] + e * math.sin(ang)), dxfattribs={"layer": "THIN"})
        counts["centre_lines"] += 1
        if not main:
            return
        dd = msp.add_diameter_dim(center=cc, radius=rr, angle=135, dimstyle="ESKD",
                                  override={"dimlfac": 1.0 / v.s, "dimtad": 1}, dxfattribs={"layer": "DIM"})
        dd.render()
        counts["dimensions"] += 1
    if hole["grid"] and v.name == "top" and len(hole["pts"]) >= 2:
        g = hole["grid"]
        cs = [v.p3(hole_axis(hole["face"], u, vv)[0]) for u, vv in hole["pts"]]
        xs, ys = sorted({round(q[0], 6) for q in cs}), sorted({round(q[1], 6) for q in cs})
        tpts = [v.to_paper(*q) for q in extents(v.geo["visible"])[1]]
        if len(xs) > 1 and g["pitch_x"]:
            ybot = min(q[1] for q in tpts)
            linear_dim(msp, (xs[0], ys[0]), (xs[1], ys[0]), (xs[0], ybot - DIM_FIRST), 0, v.s)
            counts["dimensions"] += 1
        if len(ys) > 1 and g["pitch_y"]:
            xl = min(q[0] for q in tpts)
            linear_dim(msp, (xs[0], ys[0]), (xs[0], ys[1]), (xl - DIM_FIRST, ys[0]), 90, v.s)
            counts["dimensions"] += 1


def v_has_pitch(views, name, axis):
    if name != "top" or "top" not in views:
        return False
    return any(h["grid"] and h["face"] in (">Z", "<Z", "XY") and
               ((axis == "x" and h["grid"]["nx"] > 1) or (axis == "y" and h["grid"]["ny"] > 1))
               for h in ANNOT["holes"])


def plan_callouts(views):
    """Pick, per hole feature, the view that shows it as a circle (visible preferred) and the
    instance nearest the outside. Top-row views (front, left) get shelves ABOVE the view;
    the plan view (top) gets them to the RIGHT. Scale-independent."""
    plans = []
    for hole in ANNOT["holes"]:
        best = None
        for u, vv in hole["pts"]:
            p, d = hole_axis(hole["face"], u, vv)
            for v in views.values():
                if v.name == "iso" or abs(abs(dot(v.n, d)) - 1) > 1e-6:
                    continue
                visible = (dot(v.n, d) * (1 if hole["face"][0] == ">" else -1) > 0) if hole["face"][0] in "<>" else True
                px, py = dot(p, v.x), dot(p, v.y)
                rank = (0 if visible else 1, ("front", "left", "top").index(v.name),
                        -(py * 1000 + px if v.name != "top" else px + py))   # nearest the shelf
                if best is None or rank < best[0]:
                    best = (rank, v.name, p)
        plans.append(None if best is None else {
            "hole": hole, "view": best[1], "p": best[2], "mode": "right" if best[1] == "top" else "above",
            "lines": [ln for ln in hole["callout"] if ln]})
    return plans


def layout_callouts(views):
    """Shelf positions relative to each view's origin at the current scale; sets paddings."""
    for v in views.values():
        v.calls = []
        v.pad["t"] = 6.0
        v.pad["r"] = max(6.0, v.pad.get("r_dims", 6.0))
    for pl in PLANS:
        if pl is not None and pl["view"] in views:
            views[pl["view"]].calls.append(pl)
    for v in views.values():
        if not v.calls:
            continue
        s = v.s
        rel = lambda p: ((dot(p, v.x) - v.xmin) * s, (dot(p, v.y) - v.ymin) * s)  # noqa: E731
        right_edge = v.w * s
        if v.calls[0]["mode"] == "above":
            cur = v.h * s + 4
            for pl in sorted(v.calls, key=lambda q: -rel(q["p"])[0]):   # rightmost hole -> lowest shelf
                c = rel(pl["p"])
                below = (TXT + 1.5) if len(pl["lines"]) > 1 else 0
                ty = cur + below + 1
                cur = ty + TXT + 4.5
                pl["c"], pl["t"] = c, (c[0] + s * pl["hole"]["d"] / 2 + 6, ty)
                right_edge = max(right_edge, pl["t"][0] + max(tw(x, TXT) for x in pl["lines"]) + 3)
            v.pad["t"] = cur - v.h * s + 3
        else:
            cur = v.h * s
            for pl in sorted(v.calls, key=lambda q: -rel(q["p"])[1]):    # highest hole -> top shelf
                c = rel(pl["p"])
                ty = cur - TXT - 1.5
                cur = ty - ((TXT + 1.5) if len(pl["lines"]) > 1 else 0) - 5
                pl["c"], pl["t"] = c, (v.w * s + 8, ty)
                right_edge = max(right_edge, pl["t"][0] + max(tw(x, TXT) for x in pl["lines"]) + 3)
        v.pad["r"] = max(v.pad["r"], right_edge - v.w * s + 2)


def draw_callouts(msp, views):
    n = 0
    for v in views.values():
        for pl in getattr(v, "calls", []):
            c = (v.ox + pl["c"][0], v.oy + pl["c"][1])
            tx, ty = v.ox + pl["t"][0], v.oy + pl["t"][1]
            r = v.s * pl["hole"]["d"] / 2
            ang = math.atan2(ty - c[1], tx - c[0])
            start = (c[0] + r * math.cos(ang), c[1] + r * math.sin(ang))
            width = max(tw(x, TXT) for x in pl["lines"]) + 2
            msp.add_leader([start, (tx, ty), (tx + width, ty)], dimstyle="ESKD",
                           override={"dimasz": 3.0}, dxfattribs={"layer": "DIM"})
            text(msp, pl["lines"][0], tx + 1, ty + 1, TXT, align="BOTTOM_LEFT")
            if len(pl["lines"]) > 1:
                text(msp, pl["lines"][1], tx + 1, ty - 1, TXT, align="TOP_LEFT")
            n += 1
    return n


def notes_block(msp, sheet, notes):
    if not notes:
        return 0.0
    W, _ = SHEETS[sheet] if sheet != "A4" else (210, 297)
    x0 = W - 5 - TB_W
    lines = []
    for i, n in enumerate(notes, 1):
        wrapped = textwrap.wrap(f"{i}. {n}", width=int(TB_W / (TXT * CHAR_W)))
        lines += [wrapped[0]] + ["   " + w for w in wrapped[1:]]
    y = 5 + TB_H + 10 + (len(lines) - 1) * 6
    for ln in lines:
        text(msp, ln, x0 + 2, y, TXT, align="BOTTOM_LEFT")
        y -= 6
    return len(lines) * 6.0


def notes_height():
    n = 0
    for i, note in enumerate(ANNOT["notes"], 1):
        n += len(textwrap.wrap(f"{i}. {note}", width=int(TB_W / (TXT * CHAR_W))))
    return n * 6.0


# ---------------------------------------------------------------- self-check

def verify_dxf(path, sheet, X0, Y0):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    W, H = SHEETS[sheet] if sheet != "A4" else (210, 297)
    checks = {}

    def rect_of(e):
        pts = [tuple(round(c, 4) for c in p[:2]) for p in e.get_points()]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    rects = [rect_of(e) for e in msp.query("LWPOLYLINE[layer=='FRAME']") if e.closed]
    checks["frame_20_5_5_5"] = (20, 5, W - 5, H - 5) in rects
    tb = (round(X0, 4), round(Y0, 4), round(X0 + TB_W, 4), round(Y0 + TB_H, 4))
    checks["title_block_185x55_at_bottom_right"] = tb in rects and tb[2] == W - 5 and tb[1] == 5
    xs = {round(e.dxf.start.x - X0, 3) for e in msp.query("LINE[layer=='FRAME']")
          if abs(e.dxf.start.x - e.dxf.end.x) < 1e-6 and X0 - 1e-6 <= e.dxf.start.x <= X0 + TB_W}
    checks["title_block_columns_7_17_40_55_65_135_150_167"] = {7, 17, 40, 55, 65, 135, 150, 167} <= xs
    ys = {round(e.dxf.start.y - Y0, 3) for e in msp.query("LINE")
          if e.dxf.layer in ("FRAME", "FRAME_THIN") and abs(e.dxf.start.y - e.dxf.end.y) < 1e-6
          and X0 - 1e-6 <= min(e.dxf.start.x, e.dxf.end.x) and max(e.dxf.start.x, e.dxf.end.x) <= X0 + TB_W + 1e-6}
    checks["title_block_rows_5mm_grid"] = {5, 10, 15, 20, 25, 30, 35, 40, 45, 50} <= ys
    checks["dimension_entities"] = len(msp.query("DIMENSION"))
    checks["audit_errors"] = len(doc.audit().errors)
    return checks


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=f"ЕСКД drawing of {PART} projected from its 3D model.")
    ap.add_argument("--out", default=HERE, help="output folder (default: next to this script)")
    ap.add_argument("--sheet", default=DEFAULT_SHEET, choices=["auto"] + list(SHEETS))
    ap.add_argument("--scale", default=DEFAULT_SCALE, help="auto or a ГОСТ 2.302 scale, e.g. 1:2, 2:1, 1:2,5")
    ap.add_argument("--views", default=DEFAULT_VIEWS, help="auto or comma list of front,top,left,iso")
    ap.add_argument("--no-preview", action="store_true", help="skip the PNG/SVG preview")
    a = ap.parse_args(argv)
    scale_arg = a.scale.replace(".", ",")
    if scale_arg != "auto" and scale_arg not in [x[1] for x in SCALES]:
        ap.error(f"--scale {a.scale!r} is not a ГОСТ 2.302 scale: {', '.join(x[1] for x in SCALES)}")

    cad = load_cad()
    warns = []
    import hashlib
    fp = hashlib.sha256(json.dumps(cad.SPEC, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    if fp != SPEC_FINGERPRINT:
        warns.append(f"{CAD_SCRIPT} was generated from a different spec than this drawing script: "
                     "regenerate both so callouts and title block match the model")
    wp = cad.build()
    report = cad.measure(wp)
    solid = wp.val()
    shape = solid.wrapped
    bb = solid.BoundingBox()
    revolved = ANNOT["revolved"]
    names = a.views
    if names == "auto":
        names = "front,left" if revolved else "front,top,left"
    names = [n.strip() for n in names.split(",") if n.strip()]
    if "front" not in names or any(n not in ("front", "top", "left", "iso") for n in names):
        ap.error("--views must include front and use only front, top, left, iso")
    frames = view_frames(revolved)
    views = {}
    for n in ("front", "top", "left", "iso"):
        if n in names:
            N, X = frames[n]
            views[n] = View(n, N, X, project(shape, N, X, hidden=(n != "iso")), bb)
    # paddings for dimensions and callouts (paper mm, independent of scale)
    f = views["front"]
    f.pad["l"] = DIM_FIRST + 8 + (DIM_STEP if revolved else 0)
    if revolved:
        f.pad["b"] = DIM_FIRST + 8
    if "top" in views:
        t = views["top"]
        t.pad["l"] = DIM_FIRST + 8 + (DIM_STEP if v_has_pitch(views, "top", "y") else 0)
        t.pad["b"] = DIM_FIRST + 8 + (DIM_STEP if v_has_pitch(views, "top", "x") else 0)
    elif not revolved and "left" in views:
        views["left"].pad["b"] = DIM_FIRST + 8
    global PLANS
    PLANS = plan_callouts(views)
    for pl, hole in zip(PLANS, ANNOT["holes"]):
        if pl is None:
            warns.append(f"hole {hole['id']}: no view shows it as a circle; callout skipped (add a view or section)")
    nh = notes_height()
    max_dim = max(bb.xlen, bb.ylen, bb.zlen)
    sheet, s, scale_label, fits = choose(views, a.sheet, scale_arg, nh, max_dim)
    if not fits:
        warns.append(f"views do not fit {sheet} at {scale_label} without overlapping the title block; "
                     "choose a larger --sheet or smaller --scale")

    doc = new_doc()
    msp = doc.modelspace()
    for v in views.values():
        add_prims(msp, v, v.geo["hidden"], "HIDDEN")
        add_prims(msp, v, v.geo["visible"], "CONTOUR")
    counts = annotate(msp, views, {"bb": bb, "afaces": axis_faces(shape)}, warns)
    counts["callouts"] = draw_callouts(msp, views)
    notes_block(msp, sheet, ANNOT["notes"])
    rho = ANNOT["density_g_cm3"]
    mass = report["volume_mm3"] * rho * 1e-6 if rho else ANNOT["mass_kg"]
    if rho and ANNOT["mass_kg"] and abs(mass - ANNOT["mass_kg"]) > 0.05 * ANNOT["mass_kg"]:
        warns.append(f"model mass {mass:.3f} kg differs from the spec title-block mass {ANNOT['mass_kg']} kg by "
                     f"more than 5% (density {rho} g/cm³ from {ANNOT['density_source']})")
    mass_txt = "" if not mass else (f"{mass:.1f}" if mass >= 10 else f"{mass:.2f}" if mass >= 1 else f"{mass:.3f}")
    X0, Y0 = frame_and_title(msp, sheet, {
        "title": ANNOT["title"], "number": ANNOT["number"], "material": ANNOT["material"],
        "mass": mass_txt.replace(".", ","), "scale": scale_label, "sheet_no": 1, "sheets": 1})

    os.makedirs(a.out, exist_ok=True)
    base = os.path.join(a.out, f"{PART}_drawing")
    doc.saveas(base + ".dxf")
    print(f"wrote {base}.dxf  ({sheet}, scale {scale_label})")

    # self-check: projected extents = model bbox x scale; projection links; sheet geometry
    checks = {"views": {}}
    ok = True
    for v in views.values():
        e = v.expect
        dev = max(abs(v.xmin - e[0]), abs(v.ymin - e[1]), abs(v.xmax - e[2]), abs(v.ymax - e[3]))
        good = dev < 0.01 if v.name != "iso" else True
        ok &= good
        checks["views"][v.name] = {"paper_w": round(v.s * v.w, 3), "paper_h": round(v.s * v.h, 3),
                                   "model_w": round(e[2] - e[0], 3), "model_h": round(e[3] - e[1], 3),
                                   "max_dev_mm": round(dev, 4), "ok": good}
    if "top" in views:
        link = abs(views["top"].ox - f.ox) < 1e-6 and abs(views["top"].w - f.w) < 1e-6
        checks["front_top_vertical_link"] = link
        ok &= link
    if "left" in views:
        link = abs(views["left"].oy - f.oy) < 1e-6 and abs(views["left"].h - f.h) < 1e-6
        checks["front_left_horizontal_link"] = link
        ok &= link
    checks["sheet"] = verify_dxf(base + ".dxf", sheet, X0, Y0)
    ok &= all(v for k, v in checks["sheet"].items() if isinstance(v, bool)) and checks["sheet"]["audit_errors"] == 0
    checks["fits_sheet"] = fits
    out = {"part": PART, "sheet": sheet, "scale": scale_label, "views": list(views), "mass_kg": mass,
           "counts": counts, "checks": checks, "warnings": warns, "ok": bool(ok and fits)}
    with open(base + ".report.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(f"wrote {base}.report.json")

    if not a.no_preview:
        try:
            import matplotlib
            matplotlib.use("Agg")
            from ezdxf.addons.drawing import matplotlib as dm
            from ezdxf.addons.drawing.config import Configuration, LineweightPolicy
            cfg = Configuration(lineweight_policy=LineweightPolicy.ABSOLUTE, lineweight_scaling=1.0)
            W, H = SHEETS[sheet] if sheet != "A4" else (210, 297)
            for ext, dpi in (("png", 200), ("svg", 72)):
                dm.qsave(msp, f"{base}.{ext}", bg="#FFFFFF", dpi=dpi, config=cfg,
                         size_inches=(W / 25.4, H / 25.4))
                print(f"wrote {base}.{ext}")
        except Exception as exc:  # preview is optional
            print(f"preview skipped: {exc}")
    for w in warns:
        print(f"WARNING {w}")
    print("self-check " + ("PASSED" if out["ok"] else "FAILED") + f": {counts['dimensions']} dimensions, "
          f"{counts['callouts']} callouts, {counts['centre_lines']} centre lines")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def generate(spec, spec_name="spec.json", cad_script=None, sheet="auto", scale="auto", views="auto"):
    annot = annotation_data(spec)
    drawing = spec.get("drawing") or {}
    drawing_id = " ".join(str(drawing.get(k)) for k in ("number", "title") if drawing.get(k)) or "(unnamed)"
    cad = cad_script or f"{spec['part']}_cad.py"
    rep = {
        "__DRAWING_ID__": drawing_id.replace('"""', "'''"),
        "__SPEC_NAME__": os.path.basename(spec_name),
        "__CAD_SCRIPT__": cad,
        "__PART_REPR__": repr(spec["part"]),
        "__CAD_REPR__": repr(cad),
        "__FP_REPR__": repr(fingerprint(spec)),
        "__ANNOT__": repr(annot),
        "__SHEET_REPR__": repr(sheet),
        "__SCALE_REPR__": repr(scale),
        "__VIEWS_REPR__": repr(views),
        "__SCALES__": repr(SCALES),
        "__PART__": spec["part"],
    }
    code = TEMPLATE
    for k, v in rep.items():
        code = code.replace(k, v)
    return code, annot


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a script that makes an associative ЕСКД drawing (DXF R2010 + preview) "
                    "projected from the part's CadQuery model.",
        epilog="Exit: 0 written · 2 spec invalid · 3 open questions · 64 usage")
    ap.add_argument("spec", nargs="?", help="part-spec.v1 JSON")
    ap.add_argument("--sample", action="store_true", help="use the built-in sample spec")
    ap.add_argument("--cad-script", help="the part's <part>_cad.py (must sit next to the drawing script)")
    ap.add_argument("--sheet", default="auto", help="auto | A4 | A3 | A2 | A1 | A0")
    ap.add_argument("--scale", default="auto", help="auto or a ГОСТ 2.302 scale (1:1, 1:2, 1:2,5, 2:1 ...)")
    ap.add_argument("--views", default="auto", help="auto or comma list of front,top,left,iso (front required)")
    ap.add_argument("--out", help="script path (default: <part>_drawing.py next to the spec; '-' = stdout)")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

    sheet = a.sheet.upper() if a.sheet.lower() != "auto" else "auto"
    if sheet != "auto" and sheet not in SHEETS:
        print(f"error: --sheet must be auto or one of {', '.join(SHEETS)}", file=sys.stderr)
        return 64
    scale = a.scale.replace(".", ",")
    if scale != "auto" and scale not in [lab for _, lab in SCALES]:
        print(f"error: --scale {a.scale!r} is not a ГОСТ 2.302 scale: {', '.join(lab for _, lab in SCALES)}",
              file=sys.stderr)
        return 64
    views = a.views.replace(" ", "").lower()
    if views != "auto":
        names = [v for v in views.split(",") if v]
        if "front" not in names or any(v not in VIEW_NAMES for v in names):
            print("error: --views must include front and use only front, top, left, iso", file=sys.stderr)
            return 64
        views = ",".join(names)

    if a.sample:
        spec, spec_path = json.loads(json.dumps(SAMPLE_SPEC)), "sample"
    elif a.spec:
        spec, err = load_json(a.spec)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 64
        spec_path = a.spec
    else:
        ap.print_usage(sys.stderr)
        return 64

    rep, open_q = validate(spec)
    if rep.errors:
        print("Refusing to generate: the spec is invalid. Run part_spec_validator.py for details.", file=sys.stderr)
        for e in rep.errors:
            print(f"  ERROR {e}", file=sys.stderr)
        return 2
    if open_q:
        print("Refusing to generate: the drawing readout still has open questions:", file=sys.stderr)
        for q in open_q:
            print(f"  ? {q}", file=sys.stderr)
        return 3

    cad = None
    if a.cad_script:
        if not os.path.isfile(a.cad_script):
            print(f"error: --cad-script {a.cad_script} not found", file=sys.stderr)
            return 64
        cad = os.path.basename(a.cad_script)
        with open(a.cad_script, encoding="utf-8") as fh:
            src = fh.read()
        if f"PART = {spec['part']!r}" not in src:
            print(f"error: {a.cad_script} does not define PART = {spec['part']!r}; it is not this part's model",
                  file=sys.stderr)
            return 64

    code, annot = generate(spec, spec_path, cad, sheet, scale, views)
    if a.out == "-" or (a.sample and not a.out):
        sys.stdout.write(code)
        return 0
    if a.out:
        out = a.out
    elif a.cad_script:
        out = os.path.join(os.path.dirname(os.path.abspath(a.cad_script)), f"{spec['part']}_drawing.py")
    else:
        out = os.path.join(os.path.dirname(os.path.abspath(spec_path)), f"{spec['part']}_drawing.py")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(code)
    cad_name = cad or f"{spec['part']}_cad.py"
    cad_here = os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(out)), cad_name))
    if a.output == "json":
        print(json.dumps({"written": out, "cad_script": cad_name, "cad_script_present": cad_here,
                          "holes": len(annot["holes"]), "notes": annot["notes"],
                          "density_g_cm3": annot["density_g_cm3"], "sheet": sheet, "scale": scale,
                          "views": views, "warnings": rep.warnings}, indent=2, ensure_ascii=False))
    else:
        print(f"wrote {out}")
        for w in rep.warnings:
            print(f"  WARNING {w}")
        if not cad_here:
            print(f"  NOTE {cad_name} is not next to it yet: generate it with cadquery_codegen.py into the same folder")
        print(f"next: pip install cadquery && python {os.path.basename(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
