#!/usr/bin/env python3
"""kompas_codegen.py — turn a validated part spec into a script that builds the part
natively in KOMPAS-3D (ASCON), with a real, editable feature tree.

    kompas_codegen.py SPEC [--out PATH] [--sample] [--output text|json]

The emitted ``<part>_kompas.py`` runs on the user's Windows PC (KOMPAS-3D v18-v23,
``pip install pywin32``). It attaches to a running KOMPAS (or starts one), creates
a part, and builds every spec feature as a named tree element ("H1 Ø9 4 отв.",
"C1 фаска 1x45°" ...): sketch on a default or offset plane + extrusion / rotation,
fillets and chamfers by edge points, shell, cosmetic threads. Then it sets the
material, checks the overall size and mass against the drawing, saves
``<part>.m3d`` and exports ``<part>.step``. ``--dry-run`` prints the planned
operations without importing win32com (smoke-testable on any OS).

API choice: KOMPAS API5 (``KOMPAS.Application.5`` -> ksDocument3D / ksPart /
ksEntity + *Definition interfaces). It is the interface used by the SDK's own
3D samples and by most published Python examples, and every call used here
exists in the KOMPAS type library (checked against makepy wrappers of the V20
typelibs: API5 LIBID {0422828C-F174-495E-AC5D-D31014DBBE87}, constants
ksConstants3D.tlb). Operation -> API map (full table with sources in the report
and in the generated script's header):

  new part        KompasObject.Document3D(); doc.Create(False, True); doc.GetPart(pTop_Part=-1)
  plane           part.GetDefaultEntity(o3d_planeXOY=1 | XOZ=2 | YOZ=3)
  offset plane    NewEntity(o3d_planeOffset=14) -> ksPlaneOffsetDefinition.SetPlane/offset/direction
  sketch          NewEntity(o3d_sketch=5) -> ksSketchDefinition.SetPlane; BeginEdit() -> ksDocument2D
                  ksCircle / ksLineSeg / ksArcBy3Points (style 1 = main line, 3 = axis line)
  extrusions      o3d_baseExtrusion=24 / bossExtrusion=25 / cutExtrusion=26
                  .directionType (dtNormal=0, dtReverse=1, dtBoth=2), .SetSketch,
                  .SetSideParam(side1, etBlind=0 | etThroughAll=1, depth, draft, draftOutward)
  rotations       o3d_baseRotated=27 / cutRotated=29 -> .SetSideParam(side1, angle)
  fillet/chamfer  o3d_fillet=34 / o3d_chamfer=33; edges = part.EntityCollection(o3d_edge=7)
                  .SelectByPoint(x, y, z) -> definition.array().Add(edge)
  shell           o3d_shellOperation=43 -> .thickness / .thinType / .FaceArray().Add(face)
  thread          o3d_thread=58 ("Условное изображение резьбы") -> .SetBaseObject(face), dr, p, length
  material/mass   part.SetMaterial(name, density); part.GetMass()
  bbox            part.GetGabarit(full, customizable) -> (ok, x1, y1, z1, x2, y2, z2)
  save / STEP     doc.SaveAs(path.m3d); doc.AdditionFormatParam() .format = format_STEP(3);
                  doc.SaveAsToAdditionFormat(path.step, param)

Sketch-plane axis orientation of the default planes is NOT assumed: the generated
script measures it once with throw-away probe parts (GetGabarit) and refuses to
build if that calibration fails, because a wrong guess would mirror features.

Every feature position stays in the spec's global drawing coordinates
(+-Z faces and XY use (X, Y); +-X and YZ use (Y, Z); +-Y and XZ use (X, Z)) and is
converted to 3D here, so the dry-run shows exactly where each hole goes.

Exit codes: 0 written · 2 spec invalid · 3 open questions ·
4 spec uses something this backend cannot express (listed, never dropped) · 64 usage.
Stdlib only; never imports win32com.
"""

import argparse
import json
import math
import os
import pprint
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from part_spec_validator import (  # noqa: E402
    DATUMS, SAMPLE_SPEC, expand_positions, validate,
)

try:  # the hardened validator ships a NaN-safe loader; fall back if absent
    from part_spec_validator import load_json  # noqa: E402
except ImportError:  # pragma: no cover
    def load_json(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), None
        except (OSError, ValueError) as e:
            return None, f"cannot read {path}: {e}"

# Face -> (default plane, axis index of the face normal, outward sign)
FACE_INFO = {
    ">Z": ("XOY", 2, +1), "<Z": ("XOY", 2, -1),
    ">X": ("YOZ", 0, +1), "<X": ("YOZ", 0, -1),
    ">Y": ("XOZ", 1, +1), "<Y": ("XOZ", 1, -1),
    "XY": ("XOY", 2, 0), "YZ": ("YOZ", 0, 0), "XZ": ("XOZ", 1, 0),
}

# Coarse ISO metric pitches (ГОСТ 24705 / ISO 261) for "M8"-style designations.
COARSE_PITCH = {1: 0.25, 1.2: 0.25, 1.6: 0.35, 2: 0.4, 2.5: 0.45, 3: 0.5, 4: 0.7, 5: 0.8,
                6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5,
                22: 2.5, 24: 3.0, 27: 3.0, 30: 3.5, 36: 4.0, 42: 4.5, 48: 5.0}

DENSITY_G_CM3 = {  # used only when the spec gives no density_g_cm3
    "steel": 7.85, "сталь": 7.85, "stainless": 7.9, "aluminium": 2.7, "aluminum": 2.7,
    "алюминий": 2.7, "д16": 2.78, "brass": 8.5, "латунь": 8.5, "bronze": 8.8,
    "бронза": 8.8, "copper": 8.94, "медь": 8.94, "titanium": 4.5, "титан": 4.5,
    "cast iron": 7.2, "чугун": 7.2, "сч": 7.2,
}


def _r(v):
    v = float(v)
    return int(v) if v.is_integer() else round(v, 6)


def _f(v):
    v = _r(v)
    return str(v)


def to3d(face, u, v, c):
    """Global 3D point of in-face drawing coordinates (u, v) on a plane at offset c."""
    axis = FACE_INFO[face][1]
    if axis == 2:
        return [_r(u), _r(v), _r(c)]
    if axis == 0:
        return [_r(c), _r(u), _r(v)]
    return [_r(u), _r(c), _r(v)]


def _vec(axis, sign):
    out = [0, 0, 0]
    out[axis] = sign
    return out


# ------------------------------------------------------------ envelope tracking

class Envelope:
    """Axis-aligned envelope of the part, used to place face planes.

    Mirrors what CadQuery's faces(">Z") picks: the outermost face in that direction.
    A boss grows the envelope on its face; cuts never shrink it.
    """

    def __init__(self, base):
        if base["type"] == "extrude":
            p = base["profile"]
            s = p["shape"]
            if s == "rect":
                hx, hy = p["width"] / 2, p["length"] / 2
                ang = math.radians(p.get("angle_deg") or 0)
                ex = abs(hx * math.cos(ang)) + abs(hy * math.sin(ang))
                ey = abs(hx * math.sin(ang)) + abs(hy * math.cos(ang))
                self.lo, self.hi = [-ex, -ey, 0.0], [ex, ey, float(base["height"])]
            elif s == "circle":
                r = p["diameter"] / 2
                self.lo, self.hi = [-r, -r, 0.0], [r, r, float(base["height"])]
            elif s == "regular_polygon":
                r = p["across_corners"] / 2
                self.lo, self.hi = [-r, -r, 0.0], [r, r, float(base["height"])]
            elif s == "slot":
                hl, hw = p["length"] / 2, p["width"] / 2
                ang = math.radians(p.get("angle_deg") or 0)
                ex = abs((hl - hw) * math.cos(ang)) + hw
                ey = abs((hl - hw) * math.sin(ang)) + hw
                self.lo, self.hi = [-ex, -ey, 0.0], [ex, ey, float(base["height"])]
            else:  # polygon
                xs = [float(q[0]) for q in p["points"]]
                ys = [float(q[1]) for q in p["points"]]
                self.lo, self.hi = [min(xs), min(ys), 0.0], [max(xs), max(ys), float(base["height"])]
        else:
            r = max(float(q[0]) for q in base["profile_rz"])
            zs = [float(q[1]) for q in base["profile_rz"]]
            self.lo, self.hi = [-r, -r, min(zs)], [r, r, max(zs)]

    def face_coord(self, face):
        _, axis, sign = FACE_INFO[face]
        if sign == 0:
            return 0.0
        return self.hi[axis] if sign > 0 else self.lo[axis]

    def grow(self, face, height):
        _, axis, sign = FACE_INFO[face]
        if sign > 0:
            self.hi[axis] += height
        elif sign < 0:
            self.lo[axis] -= height


# ------------------------------------------------------------ 2D profiles -> sketch primitives

def _rot(u, v, ang):
    ca, sa = math.cos(ang), math.sin(ang)
    return u * ca - v * sa, u * sa + v * ca


def profile_prims(p, at, face, c):
    """Sketch primitives (3D global points) for profile p centred at `at` on a face plane."""
    s = p["shape"]
    au, av = float(at[0]), float(at[1])
    P = lambda u, v: to3d(face, au + u, av + v, c)  # noqa: E731
    if s == "circle":
        return [{"t": "circle", "c": P(0, 0), "r": _r(p["diameter"] / 2)}]
    if s == "rect":
        return _rounded_rect(p["width"], p["length"], p.get("corner_radius") or 0,
                             math.radians(p.get("angle_deg") or 0), P)
    if s == "slot":
        return _slot(p["length"], p["width"], math.radians(p.get("angle_deg") or 0), P)
    if s == "regular_polygon":
        n, r = int(p["sides"]), p["across_corners"] / 2
        pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]
        return _closed(pts, P)
    if s == "polygon":
        return _closed([(float(q[0]), float(q[1])) for q in p["points"]], P)
    raise ValueError(f"unknown profile shape {s!r}")


def _closed(pts, P):
    return [{"t": "line", "a": P(*pts[i]), "b": P(*pts[(i + 1) % len(pts)])} for i in range(len(pts))]


def _rounded_rect(w, l, cr, ang, P):
    hw, hl = w / 2, l / 2
    if not cr:
        return _closed([_rot(x, y, ang) for x, y in ((-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl))], P)
    prims = []
    # corners: (centre of arc, start angle) counter-clockwise from bottom-right
    corners = [((hw - cr, -hl + cr), -90), ((hw - cr, hl - cr), 0), ((-hw + cr, hl - cr), 90), ((-hw + cr, -hl + cr), 180)]
    arcs = []
    for (cx, cy), a0 in corners:
        a1 = a0 + 90
        pt = lambda a: (cx + cr * math.cos(math.radians(a)), cy + cr * math.sin(math.radians(a)))  # noqa: E731
        arcs.append((pt(a0), pt(a0 + 45), pt(a1)))
    for i, (s, m, e) in enumerate(arcs):
        prims.append({"t": "arc3", "a": P(*_rot(*s, ang)), "m": P(*_rot(*m, ang)), "b": P(*_rot(*e, ang))})
        nxt = arcs[(i + 1) % 4][0]
        prims.append({"t": "line", "a": P(*_rot(*e, ang)), "b": P(*_rot(*nxt, ang))})
    return prims


def _slot(length, width, ang, P):
    r, h = width / 2, (length - width) / 2
    q = lambda x, y: P(*_rot(x, y, ang))  # noqa: E731
    return [
        {"t": "line", "a": q(-h, -r), "b": q(h, -r)},
        {"t": "arc3", "a": q(h, -r), "m": q(h + r, 0), "b": q(h, r)},
        {"t": "line", "a": q(h, r), "b": q(-h, r)},
        {"t": "arc3", "a": q(-h, r), "m": q(-h - r, 0), "b": q(-h, -r)},
    ]


def rz_prims(profile_rz):
    """Closed (r, z) profile drawn in the XOZ plane (global X = r, Z = z) + the Z axis line."""
    pts = [(float(q[0]), float(q[1])) for q in profile_rz]
    prims = [{"t": "line", "a": [_r(pts[i][0]), 0, _r(pts[i][1])],
              "b": [_r(pts[(i + 1) % len(pts)][0]), 0, _r(pts[(i + 1) % len(pts)][1])]}
             for i in range(len(pts))]
    zs = [p[1] for p in pts]
    pad = max(1.0, 0.05 * (max(zs) - min(zs)))
    prims.append({"t": "axis", "a": [0, 0, _r(min(zs) - pad)], "b": [0, 0, _r(max(zs) + pad)]})
    return prims


# ------------------------------------------------------------ labels (tree names)

def _hole_name(f, n):
    d = f"{f['id']} Ø{_f(f['diameter'])}"
    d += f" {n} отв." if n > 1 else ""
    d += " сквозн." if f["depth"] == "through" else f" глуб. {_f(f['depth'])}"
    return d


def parse_thread(designation):
    """'M8x1.25-6H' -> (8.0, 1.25); 'M8-6H' -> (8.0, 1.25 coarse); else None."""
    m = re.match(r"^\s*[MМ]\s*(\d+(?:[.,]\d+)?)\s*(?:[xх×]\s*(\d+(?:[.,]\d+)?))?", designation)
    if not m:
        return None
    d = float(m.group(1).replace(",", "."))
    if m.group(2):
        return d, float(m.group(2).replace(",", "."))
    key = int(d) if d.is_integer() else d
    return (d, COARSE_PITCH[key]) if key in COARSE_PITCH else None


# ------------------------------------------------------------ spec -> ops

def unsupported(spec):
    """Features this backend cannot express faithfully: [(id, reason)]."""
    out = []
    for f in spec.get("features", []):
        if f["type"] in {"fillet", "chamfer"} and isinstance(f.get("edges"), str):
            out.append((f["id"], f"{f['type']} uses the CadQuery selector {f['edges']!r}; KOMPAS picks "
                                 "edges by point — rewrite as {\"near\": [[x, y, z], ...]} with one point ON each edge"))
    return out


def build_ops(spec):
    """Plain-data operation list (JSON-safe) + warnings. Positions in global 3D mm."""
    base = spec["base"]
    env = Envelope(base)
    ops, warns = [], []
    by_id = {f["id"]: f for f in spec.get("features", [])}
    face_c = {}  # feature id -> plane offset used (for threads)

    if base["type"] == "extrude":
        ops.append({"id": "base", "kind": "base_extrude", "name": "Основание (выдавливание)",
                    "plane": "XOY", "offset": 0, "dir": [0, 0, 1], "end": "blind",
                    "depth": _r(base["height"]),
                    "sketch": profile_prims(base["profile"], (0, 0), ">Z", 0)})
    else:
        ops.append({"id": "base", "kind": "base_revolve", "name": "Основание (вращение)",
                    "plane": "XOZ", "offset": 0, "angle": 360, "sketch": rz_prims(base["profile_rz"])})

    for f in spec.get("features", []):
        t, fid = f["type"], f["id"]
        if t in {"hole", "pocket", "slot", "boss"}:
            face = f["face"]
            plane, axis, sign = FACE_INFO[face]
            c = env.face_coord(face)
            face_c[fid] = c
            datum = face in DATUMS
            into = "both" if datum else _vec(axis, -sign)
            common = {"plane": plane, "offset": _r(c), "face": face}
            if t == "hole":
                pts = expand_positions(f)
                d = f["diameter"]
                through = f["depth"] == "through"
                axes = [{"at": to3d(face, u, v, c), "dir": into} for u, v in pts]
                if f.get("cbore"):
                    cb = f["cbore"]
                    ops.append({"id": fid, "kind": "cut", **common, "dir": into, "end": "blind",
                                "depth": _r(cb["depth"]),
                                "name": f"{fid} цековка Ø{_f(cb['diameter'])}x{_f(cb['depth'])}",
                                "sketch": [{"t": "circle", "c": to3d(face, u, v, c), "r": _r(cb["diameter"] / 2)} for u, v in pts]})
                if f.get("csk"):
                    ck = f["csk"]
                    ang = float(ck.get("angle_deg", 90))
                    cone_depth = (ck["diameter"] - d) / 2 / math.tan(math.radians(ang / 2))
                    ops.append({"id": fid, "kind": "cut", **common, "dir": into, "end": "blind",
                                "depth": _r(round(cone_depth, 6)), "draft_deg": _r(ang / 2), "draft_outward": False,
                                "name": f"{fid} зенковка Ø{_f(ck['diameter'])}x{_f(ang)}°",
                                "sketch": [{"t": "circle", "c": to3d(face, u, v, c), "r": _r(ck["diameter"] / 2)} for u, v in pts]})
                ops.append({"id": fid, "kind": "cut", **common, "dir": into,
                            "end": "through" if through else "blind",
                            "depth": None if through else _r(f["depth"]),
                            "name": _hole_name(f, len(pts)), "hole_axes": axes,
                            "sketch": [{"t": "circle", "c": to3d(face, u, v, c), "r": _r(d / 2)} for u, v in pts]})
            elif t in {"pocket", "slot"}:
                prof = f["profile"] if t == "pocket" else {
                    "shape": "slot", "length": f["length"], "width": f["width"], "angle_deg": f.get("angle_deg", 0)}
                through = f["depth"] == "through"
                label = (f"{fid} паз {_f(f['length'])}x{_f(f['width'])}" if t == "slot"
                         else f"{fid} карман {prof['shape']}")
                label += " сквозн." if through else f" глуб. {_f(f['depth'])}"
                ops.append({"id": fid, "kind": "cut", **common, "dir": into,
                            "end": "through" if through else "blind",
                            "depth": None if through else _r(f["depth"]), "name": label,
                            "sketch": profile_prims(prof, f.get("at", [0, 0]), face, c)})
            else:  # boss
                ops.append({"id": fid, "kind": "boss", **common, "dir": _vec(axis, sign), "end": "blind",
                            "depth": _r(f["height"]), "name": f"{fid} бобышка {f['profile']['shape']} h{_f(f['height'])}",
                            "sketch": profile_prims(f["profile"], f.get("at", [0, 0]), face, c)})
                env.grow(face, float(f["height"]))
        elif t == "revolve_cut":
            ops.append({"id": fid, "kind": "cut_revolve", "name": f"{fid} проточка (вырез вращением)",
                        "plane": "XOZ", "offset": 0, "angle": 360, "sketch": rz_prims(f["profile_rz"])})
        elif t in {"fillet", "chamfer"}:
            near = f["edges"]["near"]
            pts = near if isinstance(near[0], list) else [near]
            op = {"id": fid, "kind": t, "edge_points": [[_r(c) for c in q] for q in pts]}
            if t == "fillet":
                op.update(radius=_r(f["radius"]), name=f"{fid} скругление R{_f(f['radius'])}")
            else:
                s2 = f.get("size2")
                op.update(size=_r(f["size"]), size2=_r(s2 if s2 else f["size"]),
                          name=f"{fid} фаска {_f(f['size'])}" + (f"x{_f(s2)}" if s2 else "x45°"))
            ops.append(op)
        elif t == "shell":
            face = f["face"]
            _, axis, sign = FACE_INFO[face]
            c = env.face_coord(face)
            others = [i for i in range(3) if i != axis]
            cands = []
            for fu in (0.5, 0.25, 0.75):
                for fv in (0.5, 0.25, 0.75):
                    p = [0.0, 0.0, 0.0]
                    p[axis] = c
                    p[others[0]] = env.lo[others[0]] + fu * (env.hi[others[0]] - env.lo[others[0]])
                    p[others[1]] = env.lo[others[1]] + fv * (env.hi[others[1]] - env.lo[others[1]])
                    cands.append([_r(round(q, 6)) for q in p])
            th = float(f["thickness"])
            ops.append({"id": fid, "kind": "shell", "name": f"{fid} оболочка {_f(abs(th))}",
                        "thickness": _r(abs(th)), "inward": th < 0, "face_points": cands})
        elif t == "thread":
            host = by_id.get(f["on"], {})
            parsed = parse_thread(f["designation"])
            name = f"{fid} резьба {f['designation']}"
            pts3 = []
            if parsed and host.get("type") == "hole":
                face = host["face"]
                _, axis, sign = FACE_INFO[face]
                c = face_c.get(host["id"], 0.0)
                r = host["diameter"] / 2
                skip = (host["cbore"]["depth"] if host.get("cbore") else 0) + 0.5
                into = _vec(axis, -sign) if sign else [0, 0, 0]
                for u, v in expand_positions(host):
                    p = to3d(face, u + r, v, c)
                    pts3.append([_r(round(p[i] + into[i] * skip, 6)) for i in range(3)])
                length = f.get("length") or (None if host["depth"] == "through" else host["depth"])
            elif parsed and host.get("type") == "boss" and host["profile"]["shape"] == "circle":
                face = host["face"]
                _, axis, sign = FACE_INFO[face]
                c = face_c.get(host["id"], 0.0)
                r = host["profile"]["diameter"] / 2
                at = host.get("at", [0, 0])
                p = to3d(face, at[0] + r, at[1], c)
                p[axis] = _r(p[axis] + sign * host["height"] / 2)
                pts3.append(p)
                length = f.get("length") or host["height"]
            if pts3:
                ops.append({"id": fid, "kind": "thread", "name": name, "dr": _r(parsed[0]), "p": _r(parsed[1]),
                            "length": _r(length) if length else None, "face_points": pts3})
            else:
                why = ("designation not parsed as metric M..." if not parsed
                       else f"host {f['on']!r} is not a hole or a round boss")
                warns.append(f"{fid}: cosmetic thread {f['designation']} NOT drawn in KOMPAS ({why}); "
                             "add it by hand: Условное изображение резьбы")
                ops.append({"id": fid, "kind": "note", "name": name, "text": f"not drawn: {why}"})
    return ops, warns


def _density(spec):
    if spec.get("density_g_cm3"):
        return float(spec["density_g_cm3"])
    mat = str(spec.get("material") or "").strip().lower()
    for key, val in DENSITY_G_CM3.items():
        if mat.startswith(key):
            return val
    return None


# ------------------------------------------------------------ the generated script

TEMPLATE = r'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""__PART__ — built natively in KOMPAS-3D from drawing __DRAWING__.

Generated by drawing-to-3d/kompas_codegen.py from __SPEC_NAME__. Edit the spec and
regenerate rather than hand-editing, so the drawing and the model stay in step.

Requirements (Windows): KOMPAS-3D v18..v23, Python 3.8+ of the SAME bitness as KOMPAS
(64-bit for v18+), and pywin32:

    pip install pywin32
    python __SCRIPT__                 # builds, saves __PART__.m3d + __PART__.step here
    python __SCRIPT__ --out D:\work   # choose the output folder
    python __SCRIPT__ --dry-run       # print the planned operations; no KOMPAS needed

What it does: attaches to a running KOMPAS (or starts it), creates a new part, and
builds each drawing feature as a named element of the model tree (sketch + operation),
so the result is an ordinary editable KOMPAS part. Each feature runs inside a guard that
names the feature id; the build STOPS at the first failure and leaves the document open
for inspection instead of building something wrong.

First run calibration: the orientation of sketch axes on KOMPAS's default planes and
the direction of offset planes are measured with 9 throw-away probe parts and cached in
kompas_axes.json next to this script (delete it or pass --recalibrate to redo).

API used: KOMPAS API5 (KOMPAS.Application.5, typelib {0422828C-F174-495E-AC5D-D31014DBBE87}).
Constants are inlined below with the name they have in kompas6_constants_3d.py /
ksConstants3D.tlb. Lines marked "# VERIFY:" are calls whose exact behaviour could not be
tested without KOMPAS — if one misbehaves, the message names the feature and the call.
"""
import argparse
import json
import math
import os
import sys

PART = __PART_REPR__
SPEC_NAME = __SPEC_NAME_REPR__
EXPECT = __EXPECT__
MASS_KG = __MASS__
MATERIAL = __MATERIAL__
DENSITY_G_CM3 = __DENSITY__

# ---- KOMPAS constants (values from ksConstants3D.tlb / kompas6_constants_3d.py, V20) ----
o3d_planeXOY = 1          # Obj3dType.o3d_planeXOY
o3d_planeXOZ = 2          # Obj3dType.o3d_planeXOZ
o3d_planeYOZ = 3          # Obj3dType.o3d_planeYOZ
o3d_sketch = 5            # Obj3dType.o3d_sketch
o3d_face = 6              # Obj3dType.o3d_face
o3d_edge = 7              # Obj3dType.o3d_edge
o3d_planeOffset = 14      # Obj3dType.o3d_planeOffset
o3d_baseExtrusion = 24    # Obj3dType.o3d_baseExtrusion
o3d_bossExtrusion = 25    # Obj3dType.o3d_bossExtrusion
o3d_cutExtrusion = 26     # Obj3dType.o3d_cutExtrusion
o3d_baseRotated = 27      # Obj3dType.o3d_baseRotated
o3d_cutRotated = 29       # Obj3dType.o3d_cutRotated
o3d_chamfer = 33          # Obj3dType.o3d_chamfer
o3d_fillet = 34           # Obj3dType.o3d_fillet
o3d_shellOperation = 43   # Obj3dType.o3d_shellOperation
o3d_thread = 58           # Obj3dType.o3d_thread  ("Условное изображение резьбы")
dtNormal = 0              # Direction_Type.dtNormal
dtReverse = 1             # Direction_Type.dtReverse
dtBoth = 2                # Direction_Type.dtBoth
etBlind = 0               # End_Type.etBlind
etThroughAll = 1          # End_Type.etThroughAll
pTop_Part = -1            # Part_Type.pTop_Part
format_STEP = 3           # D3FormatConvType.format_STEP
ST_MIX_M = 0x3            # LDefin2D ST_MIX_M  (metres)
ST_MIX_KG = 0x10          # LDefin2D ST_MIX_KG (kilograms)
STYLE_MAIN = 1            # 2D line style 1 = основная (main) line
STYLE_AXIS = 3            # 2D line style 3 = осевая (axis) line — used as the rotation axis
API5_LIBID = "{0422828C-F174-495E-AC5D-D31014DBBE87}"
PLANE_TYPE = {"XOY": o3d_planeXOY, "XOZ": o3d_planeXOZ, "YOZ": o3d_planeYOZ}
PLANE_NORMAL_AXIS = {"XOY": 2, "XOZ": 1, "YOZ": 0}

# Planned operations: every coordinate is a GLOBAL 3D point in mm (X, Y, Z).
OPS = __OPS__


class FeatureError(RuntimeError):
    pass


# ---------------------------------------------------------------- geometry helpers

def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def to_local(axes, plane, p):
    """Global 3D point -> sketch (x, y) on `plane` (or a plane offset from it)."""
    ax = axes[plane]
    return dot(p, ax["ex"]), dot(p, ax["ey"])


# Best-guess orientation, used ONLY by --dry-run and --assume-default-axes.
# VERIFY: real orientation is measured by calibrate(); never build on this table blindly.
DEFAULT_AXES = {
    "XOY": {"ex": [1, 0, 0], "ey": [0, 1, 0], "n": [0, 0, 1],
            "off": {"True": {"sign": 1, "ex": [1, 0, 0], "ey": [0, 1, 0], "n": [0, 0, 1]},
                    "False": {"sign": -1, "ex": [1, 0, 0], "ey": [0, 1, 0], "n": [0, 0, 1]}}},
    "XOZ": {"ex": [1, 0, 0], "ey": [0, 0, 1], "n": [0, -1, 0],
            "off": {"True": {"sign": -1, "ex": [1, 0, 0], "ey": [0, 0, 1], "n": [0, -1, 0]},
                    "False": {"sign": 1, "ex": [1, 0, 0], "ey": [0, 0, 1], "n": [0, -1, 0]}}},
    "YOZ": {"ex": [0, 1, 0], "ey": [0, 0, 1], "n": [1, 0, 0],
            "off": {"True": {"sign": 1, "ex": [0, 1, 0], "ey": [0, 0, 1], "n": [1, 0, 0]},
                    "False": {"sign": -1, "ex": [0, 1, 0], "ey": [0, 0, 1], "n": [1, 0, 0]}}},
}


# ---------------------------------------------------------------- dry run

def fmt_p(p):
    return "(" + ", ".join(f"{c:g}" for c in p) + ")"


def dry_run(axes):
    print(f"Part {PART}: {len(OPS)} planned operations (dry run — nothing is sent to KOMPAS)")
    print("Sketch-local coordinates below use the DEFAULT axis guess; the real build measures them.")
    for i, op in enumerate(OPS, 1):
        k = op["kind"]
        head = f"{i:2d}. [{op['id']}] {op['name']}  <{k}>"
        if "plane" in op:
            plane = op["plane"]
            where = plane if not op.get("offset") else f"{plane} offset to {['X', 'Y', 'Z'][PLANE_NORMAL_AXIS[plane]]}={op['offset']:g}"
            head += f"  plane {where}"
        if k in ("base_extrude", "boss", "cut"):
            dirs = "both ways" if op["dir"] == "both" else "dir " + fmt_p(op["dir"])
            end = "through all" if op["end"] == "through" else f"depth {op['depth']:g}"
            head += f"  {dirs}, {end}"
            if op.get("draft_deg"):
                head += f", draft {op['draft_deg']:g}° (narrowing)"
        if k in ("base_revolve", "cut_revolve"):
            head += f"  rotate {op['angle']}° about Z"
        print(head)
        for ax in op.get("hole_axes", []):
            d = "both ways" if ax["dir"] == "both" else fmt_p(ax["dir"])
            print(f"      hole axis through {fmt_p(ax['at'])} -> {d}")
        for pr in op.get("sketch", []):
            if pr["t"] == "circle":
                lx, ly = to_local(axes, op["plane"], pr["c"])
                print(f"      circle c={fmt_p(pr['c'])} r={pr['r']:g}   sketch ({lx:g}, {ly:g})")
            elif pr["t"] in ("line", "axis"):
                print(f"      {pr['t']:<6} {fmt_p(pr['a'])} -> {fmt_p(pr['b'])}")
            else:
                print(f"      arc3   {fmt_p(pr['a'])} via {fmt_p(pr['m'])} -> {fmt_p(pr['b'])}")
        for q in op.get("edge_points", []):
            print(f"      edge through {fmt_p(q)}")
        if k == "fillet":
            print(f"      radius {op['radius']:g}")
        if k == "chamfer":
            print(f"      distances {op['size']:g} x {op['size2']:g}")
        if k == "shell":
            print(f"      thickness {op['thickness']:g} {'inward' if op['inward'] else 'outward'}; open face at {fmt_p(op['face_points'][0])}")
        if k == "thread":
            print(f"      M{op['dr']:g}x{op['p']:g} length {op['length'] if op['length'] else 'full'}; on faces at "
                  + ", ".join(fmt_p(q) for q in op["face_points"]))
        if k == "note":
            print(f"      NOTE {op['text']}")
    print(f"Then: material {MATERIAL!r} density {DENSITY_G_CM3} g/cm3; check bbox {EXPECT.get('bbox_mm')} and mass {MASS_KG}; "
          f"save {PART}.m3d + {PART}.step")
    return 0


# ---------------------------------------------------------------- KOMPAS session

def connect(hidden=False):
    import pythoncom  # noqa: F401  (pywin32)
    import win32com.client
    try:
        mod = win32com.client.gencache.EnsureModule(API5_LIBID, 0, 1, 0)
    except Exception as exc:  # typelib not registered -> dynamic dispatch still works
        print(f"  note: no typed API5 wrapper ({exc}); using dynamic dispatch")
        mod = None
    try:
        raw = win32com.client.GetActiveObject("KOMPAS.Application.5")
        print("  attached to running KOMPAS-3D")
    except Exception:
        raw = win32com.client.Dispatch("KOMPAS.Application.5")
        print("  started KOMPAS-3D")
    kompas = mod.KompasObject(raw._oleobj_) if mod is not None and hasattr(mod, "KompasObject") else raw
    kompas.Visible = not hidden
    return kompas


def new_part(kompas, visible=True):
    doc = kompas.Document3D()
    # VERIFY: Create(invisible, typeDoc) — typeDoc True = part (деталь), False = assembly.
    if not doc.Create(not visible, True):
        raise FeatureError("ksDocument3D.Create failed — is a KOMPAS licence available?")
    part = doc.GetPart(pTop_Part)
    if part is None:
        raise FeatureError("ksDocument3D.GetPart(pTop_Part) returned nothing")
    return doc, part


def gabarit(part):
    # VERIFY: GetGabarit(full=True, customizable=True) returns (ok, x1, y1, z1, x2, y2, z2) via
    # pywin32 out-parameters; "full" includes all bodies, "customizable" = by user settings.
    res = part.GetGabarit(True, True)
    vals = list(res) if isinstance(res, (tuple, list)) else []
    if len(vals) == 7:
        vals = vals[1:]
    if len(vals) != 6:
        raise FeatureError(f"GetGabarit returned {res!r}; cannot measure the part")
    x1, y1, z1, x2, y2, z2 = (float(v) for v in vals)
    return [min(x1, x2), min(y1, y2), min(z1, z2)], [max(x1, x2), max(y1, y2), max(z1, z2)]


def create(entity, what):
    ok = entity.Create()
    if ok is False or ok == 0:
        raise FeatureError(f"{what}: ksEntity.Create() returned False")
    return entity


def plane_entity(part, axes, plane, offset, name):
    """(plane entity, sketch x axis, sketch y axis, extrude normal) — all as measured by calibrate()."""
    base = part.GetDefaultEntity(PLANE_TYPE[plane])
    a = axes[plane]
    if abs(offset) < 1e-9:
        return base, a["ex"], a["ey"], a["n"]
    # Choose the direction flag whose measured offset side matches the wanted side.
    flag = "True" if a["off"]["True"]["sign"] * offset > 0 else "False"
    m = a["off"][flag]
    if m["sign"] * offset <= 0:
        raise FeatureError(f"offset plane {name}: neither direction flag reaches {offset:+g} (calibration?)")
    pe = part.NewEntity(o3d_planeOffset)
    pd = pe.GetDefinition()
    pd.SetPlane(base)
    pd.offset = abs(offset)
    pd.direction = flag == "True"   # VERIFY: measured by calibrate(), not assumed
    pe.name = name
    create(pe, f"offset plane {name}")
    return pe, m["ex"], m["ey"], m["n"]


def sketch(part, plane_ent, ex, ey, prims, name):
    se = part.NewEntity(o3d_sketch)
    sd = se.GetDefinition()
    sd.SetPlane(plane_ent)
    se.name = name
    create(se, f"sketch {name}")
    d2 = sd.BeginEdit()  # returns ksDocument2D of the sketch
    if d2 is None:
        raise FeatureError(f"sketch {name}: BeginEdit returned nothing")
    L = lambda p: (dot(p, ex), dot(p, ey))  # noqa: E731
    try:
        for pr in prims:
            if pr["t"] == "circle":
                x, y = L(pr["c"])
                d2.ksCircle(x, y, pr["r"], STYLE_MAIN)
            elif pr["t"] == "line":
                (x1, y1), (x2, y2) = L(pr["a"]), L(pr["b"])
                d2.ksLineSeg(x1, y1, x2, y2, STYLE_MAIN)
            elif pr["t"] == "axis":
                (x1, y1), (x2, y2) = L(pr["a"]), L(pr["b"])
                # VERIFY: an axis-style (style 3) segment in the sketch is taken as the rotation axis.
                d2.ksLineSeg(x1, y1, x2, y2, STYLE_AXIS)
            elif pr["t"] == "arc3":
                (x1, y1), (xm, ym), (x2, y2) = L(pr["a"]), L(pr["m"]), L(pr["b"])
                # 3-point arc: orientation-free, so a mirrored sketch plane cannot flip it.
                d2.ksArcBy3Points(x1, y1, xm, ym, x2, y2, STYLE_MAIN)
    finally:
        sd.EndEdit()
    return se


def _direction(n, want):
    """dtNormal if extruding along the sketch normal n goes `want`, else dtReverse."""
    s = dot(n, want)
    if abs(s) < 0.5:
        raise FeatureError(f"sketch normal {n} is not parallel to {want}")
    return dtNormal if s > 0 else dtReverse


def extrude(part, kind, sk, n, op):
    etype = {"base_extrude": o3d_baseExtrusion, "boss": o3d_bossExtrusion, "cut": o3d_cutExtrusion}[kind]
    e = part.NewEntity(etype)
    d = e.GetDefinition()
    end = etThroughAll if op["end"] == "through" else etBlind
    depth = op["depth"] or 0
    draft = op.get("draft_deg") or 0
    outward = bool(op.get("draft_outward", False))
    if op["dir"] == "both":
        # VERIFY: dtBoth + both sides through-all cuts symmetrically through a datum plane.
        d.directionType = dtBoth
        d.SetSideParam(True, end, depth, 0, False)
        d.SetSideParam(False, end, depth, 0, False)
    else:
        dt = _direction(n, op["dir"])
        d.directionType = dt
        # VERIFY: with dtReverse the parameters of side 2 (side1=False) are the ones used.
        # VERIFY: draftValue in degrees; draftOutward=False narrows the cut with depth (countersink cone).
        d.SetSideParam(dt == dtNormal, end, depth, draft, outward)
    if kind == "cut":
        try:
            d.cut = True  # ksCutExtrusionDefinition.cut — True = remove material
        except Exception:
            pass
    d.SetSketch(sk)
    e.name = op["name"]
    return create(e, op["name"])


def rotate(part, kind, sk, op):
    e = part.NewEntity(o3d_baseRotated if kind == "base_revolve" else o3d_cutRotated)
    d = e.GetDefinition()
    d.directionType = dtNormal
    d.toroidShapeType = False   # VERIFY: False = solid of revolution (not a thin toroid)
    d.SetSideParam(True, float(op["angle"]))
    d.SetSketch(sk)
    e.name = op["name"]
    return create(e, op["name"])


def pick(part, objtype, point, what):
    """The one edge/face that contains `point` (KOMPAS SelectByPoint), else a loud error."""
    coll = part.EntityCollection(objtype)
    # VERIFY: SelectByPoint(x, y, z) keeps only entities CONTAINING the point (SDK: "Удалить из
    # массива элементы не содержащие точку") — the point must lie on the edge/face, within
    # KOMPAS's own tolerance; CadQuery's 0.5 mm "near" slack does not exist here.
    coll.SelectByPoint(*[float(c) for c in point])
    n = coll.GetCount()
    if n == 0:
        raise FeatureError(f"{what}: no {'edge' if objtype == o3d_edge else 'face'} passes through {point} — "
                           "put the point exactly ON it (e.g. the middle of the edge)")
    if n > 1:
        raise FeatureError(f"{what}: {n} entities pass through {point} (a vertex?) — use a point inside one edge/face")
    return coll.GetByIndex(0)  # VERIFY: GetByIndex is 0-based (First() is the alternative)


def edge_op(part, op):
    e = part.NewEntity(o3d_fillet if op["kind"] == "fillet" else o3d_chamfer)
    d = e.GetDefinition()
    if op["kind"] == "fillet":
        d.radius = float(op["radius"])
    else:
        # VERIFY: SetChamferParam(transfer, distance1, distance2); transfer=False keeps the
        # distances in the given order (relevant only for unequal size x size2 chamfers).
        d.SetChamferParam(False, float(op["size"]), float(op["size2"]))
    d.tangent = False  # do not propagate along tangent edges (the spec lists every edge)
    arr = d.array()
    for q in op["edge_points"]:
        arr.Add(pick(part, o3d_edge, q, op["name"]))
    e.name = op["name"]
    return create(e, op["name"])


def shell_op(part, op):
    e = part.NewEntity(o3d_shellOperation)
    d = e.GetDefinition()
    d.thickness = float(op["thickness"])
    # VERIFY: thinType True = wall grows inward (material kept inside the outer skin).
    d.thinType = bool(op["inward"])
    last = None
    for q in op["face_points"]:
        try:
            face = pick(part, o3d_face, q, op["name"])
            break
        except FeatureError as exc:
            last = exc
    else:
        raise last
    d.FaceArray().Add(face)
    e.name = op["name"]
    return create(e, op["name"])


def thread_op(part, op):
    made = 0
    for i, q in enumerate(op["face_points"], 1):
        e = part.NewEntity(o3d_thread)
        d = e.GetDefinition()
        d.SetBaseObject(pick(part, o3d_face, q, op["name"]))  # cylindrical wall of the hole/boss
        d.autoDefinDr = False
        d.dr = float(op["dr"])   # nominal diameter
        d.p = float(op["p"])     # pitch
        if op["length"]:
            d.allLength = False
            d.length = float(op["length"])  # VERIFY: measured from the face the thread starts at
        else:
            d.allLength = True
        e.name = op["name"] + (f" #{i}" if len(op["face_points"]) > 1 else "")
        create(e, e.name)
        made += 1
    return made


# ---------------------------------------------------------------- calibration

def _probe(kompas, plane, offset, direction=True):
    """Extrude a 1 x 2 x 3 mm probe at sketch (10..11, 20..22) and measure it."""
    doc, part = new_part(kompas, visible=False)
    try:
        base = part.GetDefaultEntity(PLANE_TYPE[plane])
        pl = base
        if offset:
            pl = part.NewEntity(o3d_planeOffset)
            pd = pl.GetDefinition()
            pd.SetPlane(base)
            pd.offset = offset
            pd.direction = direction
            create(pl, "probe plane")
        se = part.NewEntity(o3d_sketch)
        sd = se.GetDefinition()
        sd.SetPlane(pl)
        create(se, "probe sketch")
        d2 = sd.BeginEdit()
        for a, b in (((10, 20), (11, 20)), ((11, 20), (11, 22)), ((11, 22), (10, 22)), ((10, 22), (10, 20))):
            d2.ksLineSeg(a[0], a[1], b[0], b[1], STYLE_MAIN)
        sd.EndEdit()
        e = part.NewEntity(o3d_baseExtrusion)
        d = e.GetDefinition()
        d.directionType = dtNormal
        d.SetSideParam(True, etBlind, 3, 0, False)
        d.SetSketch(se)
        create(e, "probe extrusion")
        return gabarit(part)
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _axes_from_box(lo, hi, offset):
    ext = [hi[i] - lo[i] for i in range(3)]
    mid = [(hi[i] + lo[i]) / 2 for i in range(3)]
    idx = {}
    for want, key in ((1, "x"), (2, "y"), (3, "n")):
        hits = [i for i in range(3) if abs(ext[i] - want) < 0.01]
        if len(hits) != 1:
            raise FeatureError(f"calibration probe box {lo}..{hi} is not 1x2x3 mm")
        idx[key] = hits[0]
    ex = [0, 0, 0]
    ex[idx["x"]] = 1 if mid[idx["x"]] > 0 else -1
    ey = [0, 0, 0]
    ey[idx["y"]] = 1 if mid[idx["y"]] > 0 else -1
    k = idx["n"]
    if offset:
        plane_at = lo[k] if abs(abs(lo[k]) - offset) < 0.01 else hi[k]
        if abs(abs(plane_at) - offset) > 0.01:
            raise FeatureError(f"offset probe box {lo}..{hi} does not touch ±{offset}")
        far = hi[k] if plane_at == lo[k] else lo[k]
        off_sign = 1 if plane_at > 0 else -1
    else:
        plane_at, far, off_sign = 0.0, (hi[k] if abs(lo[k]) < 0.01 else lo[k]), 0
    n = [0, 0, 0]
    n[k] = 1 if far > plane_at else -1
    return ex, ey, n, off_sign


def calibrate(kompas, cache_path):
    axes = {}
    for plane in ("XOY", "XOZ", "YOZ"):
        ex, ey, n, _ = _axes_from_box(*_probe(kompas, plane, 0), 0)
        if n[PLANE_NORMAL_AXIS[plane]] == 0:
            raise FeatureError(f"plane {plane}: measured normal {n} is not the expected axis")
        off = {}
        for flag in (True, False):
            oex, oey, on, sign = _axes_from_box(*_probe(kompas, plane, 5, flag), 5)
            off[str(flag)] = {"sign": sign, "ex": oex, "ey": oey, "n": on}
        if off["True"]["sign"] == off["False"]["sign"]:
            raise FeatureError(f"plane {plane}: offset direction flag has no effect — cannot place face planes")
        axes[plane] = {"ex": ex, "ey": ey, "n": n, "off": off}
        print(f"  calibrated {plane}: sketch x -> {ex}, y -> {ey}, extrude normal -> {n}; "
              f"offset direction=True goes {'+' if off['True']['sign'] > 0 else '-'}")
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(axes, fh, indent=1)
    except OSError:
        pass
    return axes


def load_axes(kompas, cache_path, recalibrate, assume_default):
    if assume_default:
        print("  WARNING: --assume-default-axes: building on the UNVERIFIED default plane orientation")
        return DEFAULT_AXES
    if not recalibrate and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as fh:
            print(f"  plane orientation from {os.path.basename(cache_path)}")
            return json.load(fh)
    print("  calibrating sketch-plane orientation (9 throw-away probe parts)...")
    return calibrate(kompas, cache_path)


# ---------------------------------------------------------------- build

def build(part, axes, keep_going=False):
    failures = []
    built = 0
    for op in OPS:
        k = op["kind"]
        try:
            if k in ("base_extrude", "boss", "cut"):
                pe, ex, ey, n = plane_entity(part, axes, op["plane"], op["offset"], f"Плоскость {op['id']}")
                sk = sketch(part, pe, ex, ey, op["sketch"], f"Эскиз {op['name']}")
                extrude(part, k, sk, n, op)
            elif k in ("base_revolve", "cut_revolve"):
                a = axes[op["plane"]]
                sk = sketch(part, part.GetDefaultEntity(PLANE_TYPE[op["plane"]]), a["ex"], a["ey"],
                            op["sketch"], f"Эскиз {op['name']}")
                rotate(part, k, sk, op)
            elif k in ("fillet", "chamfer"):
                edge_op(part, op)
            elif k == "shell":
                shell_op(part, op)
            elif k == "thread":
                thread_op(part, op)
            elif k == "note":
                print(f"  NOTE {op['id']}: {op['text']}")
                continue
            built += 1
            print(f"  ok   {op['name']}")
        except Exception as exc:  # name the drawing feature, not just the COM error
            msg = f"feature {op['id']} ({op['name']}) failed: {exc}"
            failures.append(msg)
            print(f"  FAIL {msg}")
            if not keep_going:
                raise FeatureError(msg) from exc
    return built, failures


def set_material(part):
    if not MATERIAL or not DENSITY_G_CM3:
        print("  material: not set (spec has no material/density)")
        return
    # VERIFY: SetMaterial(name, density) — density in g/cm3 as shown in KOMPAS's material
    # dialog. The mass check below catches a unit mismatch (it would be off by 10^3).
    if not part.SetMaterial(MATERIAL, float(DENSITY_G_CM3)):
        print("  WARNING: SetMaterial returned False")
    part.Update()


def measure(part):
    lo, hi = gabarit(part)
    size = [round(hi[i] - lo[i], 4) for i in range(3)]
    mass = None
    try:
        mp = part.CalcMassInertiaProperties(ST_MIX_M | ST_MIX_KG)  # VERIFY: .m = mass in kg
        mass = float(mp.m)
    except Exception:
        try:
            mass = float(part.GetMass())  # VERIFY: units follow the document settings (usually kg)
        except Exception:
            pass
    return {"bbox_mm": size, "bbox_min_mm": [round(c, 4) for c in lo], "mass_kg": mass}


def export(doc, out_dir, step=True):
    m3d = os.path.abspath(os.path.join(out_dir, PART + ".m3d"))
    if not doc.SaveAs(m3d):
        raise FeatureError(f"SaveAs {m3d} failed")
    print(f"  wrote {m3d}")
    if step:
        stp = os.path.abspath(os.path.join(out_dir, PART + ".step"))
        par = doc.AdditionFormatParam()
        par.Init()
        par.format = format_STEP  # VERIFY: plain format_STEP (3); AP214=214 / AP242=242 also exist
        if not doc.SaveAsToAdditionFormat(stp, par):
            raise FeatureError(f"STEP export to {stp} failed (try File > Save as > STEP by hand)")
        print(f"  wrote {stp}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"Build {PART} natively in KOMPAS-3D (feature tree), save .m3d + STEP.")
    ap.add_argument("--dry-run", action="store_true", help="print the planned operations; do not touch KOMPAS")
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)), help="output folder")
    ap.add_argument("--no-step", action="store_true", help="skip the STEP export")
    ap.add_argument("--recalibrate", action="store_true", help="re-measure the plane orientation")
    ap.add_argument("--assume-default-axes", action="store_true",
                    help="skip calibration and trust the built-in orientation table (not recommended)")
    ap.add_argument("--keep-going", action="store_true", help="continue after a failed feature (default: stop)")
    ap.add_argument("--hidden", action="store_true", help="do not show the KOMPAS window")
    a = ap.parse_args(argv)
    if a.dry_run:
        return dry_run(DEFAULT_AXES)

    os.makedirs(a.out, exist_ok=True)
    kompas = connect(hidden=a.hidden)
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kompas_axes.json")
    axes = load_axes(kompas, cache, a.recalibrate, a.assume_default_axes)
    doc, part = new_part(kompas, visible=not a.hidden)
    try:
        doc.comment = f"{PART} — generated from {SPEC_NAME}"
    except Exception:
        pass
    try:
        built, failures = build(part, axes, keep_going=a.keep_going)
    except FeatureError as exc:
        print(f"\nSTOPPED: {exc}\nThe part is left open in KOMPAS so you can see how far it got.")
        return 2
    set_material(part)
    report = measure(part)
    report.update(part=PART, features_built=built, failures=failures)
    ok = not failures
    tol = float(EXPECT.get("tolerance_mm") or 0.05)
    if EXPECT.get("bbox_mm"):
        got, want = sorted(report["bbox_mm"]), sorted(EXPECT["bbox_mm"])
        same = all(abs(g - w) <= tol for g, w in zip(got, want))
        report["bbox_check"] = "PASS" if same else f"FAIL: built {report['bbox_mm']} vs drawing {EXPECT['bbox_mm']}"
        ok = ok and same
    if MASS_KG and report["mass_kg"]:
        dev = abs(report["mass_kg"] - MASS_KG) / MASS_KG
        report["mass_check"] = "PASS" if dev <= 0.05 else f"CHECK: {report['mass_kg']:.4g} kg vs drawing {MASS_KG} kg ({dev:.0%})"
    export(doc, a.out, step=not a.no_step)
    with open(os.path.join(a.out, PART + ".kompas_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"\n{PART}: {built} features, bbox {report['bbox_mm']} mm "
          f"({report.get('bbox_check', 'no target')}), mass {report['mass_kg']} kg ({report.get('mass_check', 'no target')})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def generate(spec, spec_name="spec.json"):
    ops, warns = build_ops(spec)
    drawing = spec.get("drawing") or {}
    drawing_id = " ".join(str(drawing.get(k)) for k in ("number", "title") if drawing.get(k)) or "(unnamed)"
    exp = spec.get("expect") or {}
    rep = {
        "__PART__": spec["part"],
        "__DRAWING__": drawing_id.replace('"""', "'''"),
        "__SPEC_NAME__": os.path.basename(spec_name),
        "__SCRIPT__": f"{spec['part']}_kompas.py",
        "__PART_REPR__": repr(spec["part"]),
        "__SPEC_NAME_REPR__": repr(os.path.basename(spec_name)),
        "__EXPECT__": repr({k: exp.get(k) for k in ("bbox_mm", "tolerance_mm") if exp.get(k) is not None}),
        "__MASS__": repr(spec.get("mass_kg")),
        "__MATERIAL__": repr(spec.get("material")),
        "__DENSITY__": repr(_density(spec)),
        "__OPS__": pprint.pformat(ops, indent=1, width=110, sort_dicts=False),
    }
    code = TEMPLATE
    for k, v in rep.items():
        code = code.replace(k, v)
    return code, ops, warns


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a Python/pywin32 script that builds the part natively in KOMPAS-3D "
                    "(editable feature tree), saves .m3d and exports STEP.",
        epilog="Exit: 0 written · 2 spec invalid · 3 open questions · 4 unsupported by KOMPAS backend · 64 usage")
    ap.add_argument("spec", nargs="?", help="part-spec.v1 JSON")
    ap.add_argument("--sample", action="store_true", help="use the built-in sample spec")
    ap.add_argument("--out", help="script path (default: <part>_kompas.py next to the spec; '-' = stdout)")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

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
    bad = unsupported(spec)
    if bad:
        if a.output == "json":
            print(json.dumps({"unsupported": [{"id": i, "reason": r} for i, r in bad]}, indent=2, ensure_ascii=False))
        else:
            print("Refusing to generate: the KOMPAS backend cannot express these features "
                  "(nothing is dropped silently):", file=sys.stderr)
            for i, r in bad:
                print(f"  {i}: {r}", file=sys.stderr)
        return 4

    code, ops, warns = generate(spec, spec_path)
    warns = rep.warnings + warns
    if a.out == "-" or (a.sample and not a.out):
        sys.stdout.write(code)
        for w in warns:
            print(f"WARNING {w}", file=sys.stderr)
        return 0
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(spec_path)), f"{spec['part']}_kompas.py")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(code)
    if a.output == "json":
        print(json.dumps({"written": out, "operations": len(ops), "features": len(spec.get("features", [])),
                          "warnings": warns}, indent=2, ensure_ascii=False))
    else:
        print(f"wrote {out}  ({len(ops)} operations)")
        for w in warns:
            print(f"  WARNING {w}")
        print(f"next (Windows, KOMPAS-3D v18-v23): pip install pywin32 && python {os.path.basename(out)}"
              f"   (or --dry-run anywhere)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
