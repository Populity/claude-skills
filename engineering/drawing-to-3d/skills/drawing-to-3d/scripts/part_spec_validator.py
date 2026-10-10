#!/usr/bin/env python3
"""part_spec_validator.py — gate a part spec read off a drawing before any CAD code is written.

A part spec (schema ``part-spec.v1``) is the structured readout of an engineering
drawing: units, base body, an ordered feature list (holes, pockets, bosses,
slots, fillets, chamfers, revolve cuts, shells, thread callouts), acceptance
targets, the assumptions made, and the questions still open.

The validator refuses to let a half-read drawing become a model:

* every number must be positive and every feature fully dimensioned;
* any ``open_questions`` entry blocks generation (exit 3) — a missing dimension
  is asked about, never guessed silently;
* hole positions outside the base footprint, fillets larger than the stock,
  counterbores smaller than their hole, etc. are reported.

Exit codes: 0 valid (warnings allowed) · 1 warnings under --strict ·
2 spec errors · 3 open questions remain · 64 usage / unreadable input.

Stdlib only. The pattern-expansion helpers here are reused by cadquery_codegen.py.
"""

import argparse
import json
import math
import re
import sys

SCHEMA = "part-spec.v1"
FACES = {">Z", "<Z", ">X", "<X", ">Y", "<Y"}
# Datum planes through the origin: for cross holes / through cuts where no flat face
# exists (a hole across a shaft). Coordinates: XY -> (X, Y), XZ -> (X, Z), YZ -> (Y, Z).
DATUMS = {"XY", "XZ", "YZ"}
FEATURE_TYPES = {
    "hole", "pocket", "boss", "slot", "fillet", "chamfer",
    "revolve_cut", "shell", "thread",
}
PROFILE_SHAPES = {"rect", "circle", "slot", "polygon", "regular_polygon"}
# CadQuery string-selector grammar characters. Selectors are written into the
# generated script with repr(), so this is a sanity check, not an escape.
SELECTOR_RE = re.compile(r"^[<>|#%XYZ0-9 ()\[\]\-+.,a-zA-Z]{1,80}$")

SAMPLE_SPEC = {
    "schema": SCHEMA,
    "part": "flange_plate",
    "units": "mm",
    "material": "steel",
    "mass_kg": None,
    "drawing": {"title": "Flange plate", "number": "DEMO-001", "scale": "1:1"},
    "base": {
        "type": "extrude",
        "profile": {"shape": "rect", "width": 120, "length": 80, "corner_radius": 8},
        "height": 16,
    },
    "features": [
        {"id": "H1", "type": "hole", "face": ">Z", "diameter": 9, "depth": "through",
         "pattern": {"type": "grid", "nx": 2, "ny": 2, "pitch_x": 96, "pitch_y": 56}},
        {"id": "H2", "type": "hole", "face": ">Z", "diameter": 22, "depth": "through",
         "positions": [[0, 0]], "cbore": {"diameter": 34, "depth": 6}},
        {"id": "H3", "type": "hole", "face": ">Z", "diameter": 6.8, "depth": 14,
         "pattern": {"type": "polar", "count": 6, "pcd": 50, "start_angle_deg": 0}},
        {"id": "T1", "type": "thread", "on": "H3", "designation": "M8x1.25-6H", "length": 12},
        {"id": "P1", "type": "pocket", "face": "<Z", "at": [0, 0],
         "profile": {"shape": "rect", "width": 90, "length": 20, "corner_radius": 4},
         "depth": 4},
        {"id": "S1", "type": "slot", "face": ">Z", "at": [0, -30], "length": 30, "width": 8,
         "angle_deg": 0, "depth": 5},
        # Outer top outline only (the 4 sides + 4 corner arcs): a ">Z" selector would
        # also hit the slot and hole rims, and OCC fails where those sit < 1 mm apart.
        {"id": "C1", "type": "chamfer", "size": 1,
         "edges": {"near": [[0, 40, 16], [0, -40, 16], [60, 0, 16], [-60, 0, 16],
                            [57.657, 37.657, 16], [-57.657, 37.657, 16],
                            [57.657, -37.657, 16], [-57.657, -37.657, 16]]}},
    ],
    "expect": {"bbox_mm": [120, 80, 16], "tolerance_mm": 0.05},
    "assumptions": [
        {"id": "A1", "what": "Top outer edges chamfer", "value": "1x45°",
         "source": "general note 'Неуказанные фаски 1x45°'"}
    ],
    "open_questions": [],
}


MAX_INSTANCES = 10000  # hole instances per feature: a typo'd count must not hang the build


def load_json(path):
    """Read a JSON file. Returns (obj, None) or (None, message) — never raises.

    NaN / Infinity are not JSON; they are kept as the strings "NaN" / "Infinity"
    so the field checks reject them with a normal spec error instead of letting
    them reach the generated script as bare names.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh, parse_constant=lambda c: c), None
    except (OSError, ValueError, RecursionError) as e:  # ValueError covers JSON + UTF-8 errors
        return None, f"cannot read {path}: {e}"


def _hashable(v):
    return v is None or isinstance(v, (str, int, float, bool))


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _pos(rep, where, obj, key, required=True):
    v = obj.get(key)
    if v is None:
        if required:
            rep.err(where, f"'{key}' is required")
        return None
    if not _num(v) or v <= 0:
        rep.err(where, f"'{key}' must be a positive number, got {v!r}")
        return None
    return float(v)


def _point(v):
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_num(c) for c in v)


# ---------------------------------------------------------------- patterns

def expand_positions(feat):
    """Return explicit [(x, y), ...] for a feature's positions/pattern (or [] if none).

    Never raises: malformed entries are skipped here and reported by check_pattern().
    """
    if not isinstance(feat, dict):
        return []
    pos = feat.get("positions", [])
    pts = [tuple(float(c) for c in p) for p in (pos if isinstance(pos, list) else []) if _point(p)]
    pat = feat.get("pattern")
    if isinstance(pat, dict) and not pattern_errors(pat):
        kind = pat.get("type")
        cx, cy = (pat.get("center") or [0, 0])[:2]
        if kind == "polar":
            n = int(pat.get("count", 0))
            r = float(pat.get("pcd", 0)) / 2.0
            a0 = float(pat.get("start_angle_deg", 0))
            span = float(pat.get("span_deg", 360))
            step = span / n if span >= 360 else (span / (n - 1) if n > 1 else 0)
            for i in range(n):
                a = math.radians(a0 + i * step)
                pts.append((round(cx + r * math.cos(a), 6), round(cy + r * math.sin(a), 6)))
        elif kind == "grid":
            nx, ny = int(pat.get("nx", 1)), int(pat.get("ny", 1))
            px, py = float(pat.get("pitch_x", 0)), float(pat.get("pitch_y", 0))
            for i in range(nx):
                for j in range(ny):
                    pts.append((round(cx + (i - (nx - 1) / 2) * px, 6),
                                round(cy + (j - (ny - 1) / 2) * py, 6)))
        elif kind == "linear":
            n = int(pat.get("count", 0))
            pitch = float(pat.get("pitch", 0))
            ang = math.radians(float(pat.get("direction_deg", 0)))
            sx, sy = (pat.get("start") or [0, 0])[:2]
            for i in range(n):
                pts.append((round(sx + i * pitch * math.cos(ang), 6),
                            round(sy + i * pitch * math.sin(ang), 6)))
    return pts


def pattern_errors(pat):
    """Problems with a pattern object, as a list of messages ([] = expandable)."""
    errs = []

    def need_int(key, lo, default=None):
        v = pat.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < lo:
            errs.append(f"pattern.{key} must be an integer >= {lo}, got {v!r}")
            return None
        if v > MAX_INSTANCES:
            errs.append(f"pattern.{key} = {v} is more than {MAX_INSTANCES} instances")
            return None
        return v

    def need_num(key, positive=False, default=None):
        v = pat.get(key, default)
        if not _num(v) or (positive and v <= 0):
            errs.append(f"pattern.{key} must be a {'positive ' if positive else ''}number, got {v!r}")
            return None
        return v

    for key in ("center", "start"):
        if key in pat and pat[key] is not None and not _point(pat[key]):
            errs.append(f"pattern.{key} must be [x, y]")
    kind = pat.get("type")
    if kind == "polar":
        need_int("count", 1)
        need_num("pcd", positive=True)
        need_num("start_angle_deg", default=0)
        span = need_num("span_deg", default=360)
        if span is not None and not 0 < span <= 360:
            errs.append("pattern.span_deg must be in (0, 360]: the angle from the first to the last hole (360 = full circle)")
    elif kind == "grid":
        nx, ny = need_int("nx", 1, 1), need_int("ny", 1, 1)
        if nx and nx > 1:
            need_num("pitch_x", positive=True)
        if ny and ny > 1:
            need_num("pitch_y", positive=True)
        if nx and ny and nx * ny > MAX_INSTANCES:
            errs.append(f"pattern grid nx*ny = {nx * ny} is more than {MAX_INSTANCES} instances")
    elif kind == "linear":
        n = need_int("count", 1)
        if n and n > 1:
            need_num("pitch", positive=True)
        need_num("direction_deg", default=0)
    else:
        errs.append(f"pattern.type must be polar | grid | linear, got {kind!r}")
    return errs


def face_axis_point(face, u, v):
    """(point, direction) of a hole axis at drawing coordinates (u, v) on a face/datum.

    The axis is normal to the face, so the face's own offset does not matter:
    +-Z / XY use (X, Y); +-X / YZ use (Y, Z); +-Y / XZ use (X, Z).
    """
    if face in ("XY", ">Z", "<Z"):
        return (u, v, 0.0), (0.0, 0.0, 1.0)
    if face in ("YZ", ">X", "<X"):
        return (0.0, u, v), (1.0, 0.0, 0.0)
    return (u, 0.0, v), (0.0, 1.0, 0.0)


def _segments_cross(a, b, c, d):
    """True if closed segments ab and cd intersect (collinear overlap included)."""
    def orient(p, q, r):
        v = (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    def on_seg(p, q, r):
        return min(p[0], r[0]) - 1e-12 <= q[0] <= max(p[0], r[0]) + 1e-12 and \
            min(p[1], r[1]) - 1e-12 <= q[1] <= max(p[1], r[1]) + 1e-12
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if o1 != o2 and o3 != o4:
        return True
    return (o1 == 0 and on_seg(a, c, b)) or (o2 == 0 and on_seg(a, d, b)) or \
        (o3 == 0 and on_seg(c, a, d)) or (o4 == 0 and on_seg(c, b, d))


def polygon_problem(pts):
    """None if pts is a simple polygon, else a message (duplicates, zero area, self-crossing)."""
    pts = [(float(p[0]), float(p[1])) for p in pts]
    if len(set(pts)) != len(pts):
        return "contains duplicate vertices"
    area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
               for i in range(len(pts))) / 2
    if abs(area) < 1e-9:
        return "has zero area (all points on one line)"
    n = len(pts)
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # neighbouring edges share a vertex
            if _segments_cross(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                return f"edges {i}-{(i + 1) % n} and {j}-{(j + 1) % n} cross (self-intersecting outline)"
    return None


def base_footprint(base):
    """Half-extents (hx, hy) of the base in XY, or None if unknown."""
    if not isinstance(base, dict):
        return None
    if base.get("type") == "extrude":
        p = base.get("profile") or {}
        s = p.get("shape")
        try:
            if s == "rect":
                return float(p["width"]) / 2, float(p["length"]) / 2
            if s == "circle":
                return float(p["diameter"]) / 2, float(p["diameter"]) / 2
            if s == "regular_polygon":
                return float(p["across_corners"]) / 2, float(p["across_corners"]) / 2
            if s == "polygon":
                xs = [abs(float(q[0])) for q in p["points"]]
                ys = [abs(float(q[1])) for q in p["points"]]
                return max(xs), max(ys)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    if base.get("type") == "revolve":
        try:
            r = max(float(q[0]) for q in base["profile_rz"])
            return r, r
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    return None


def base_zrange(base):
    if not isinstance(base, dict):
        return None
    if base.get("type") == "extrude" and _num(base.get("height")):
        return 0.0, float(base["height"])
    if base.get("type") == "revolve":
        try:
            zs = [float(q[1]) for q in base["profile_rz"]]
            return min(zs), max(zs)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    return None


def breakout(base, face, u, v, r):
    """Why a hole of radius r at (u, v) on `face` leaves the base body, or None.

    A warning-level check against the base's simple envelope (a rectangle,
    circle or revolved radius); features added later are not considered.
    """
    fp, zr = base_footprint(base), base_zrange(base)
    if not fp:
        return None
    hx, hy = fp
    prof = (base.get("profile") or {}) if base.get("type") == "extrude" else {}
    round_base = base.get("type") == "revolve" or prof.get("shape") == "circle"
    if face in (">Z", "<Z", "XY"):
        if round_base:
            if math.hypot(u, v) + r > hx + 1e-6:
                return f"breaks out of the Ø{2 * hx:g} outline"
        elif abs(u) + r > hx + 1e-6 or abs(v) + r > hy + 1e-6:
            return f"breaks out of the base footprint ±{hx:g}×±{hy:g}"
        return None
    if face in (">X", "<X", "YZ", ">Y", "<Y", "XZ"):
        lateral = hy if face in (">X", "<X", "YZ") else hx
        if abs(u) + r > lateral + 1e-6:
            return f"is outside the side face (±{lateral:g} wide)"
        if zr and (v - r < zr[0] - 1e-6 or v + r > zr[1] + 1e-6):
            return f"is outside the part height (z {zr[0]:g}..{zr[1]:g})"
    return None


def base_height(base):
    if not isinstance(base, dict):
        return None
    if base.get("type") == "extrude" and _num(base.get("height")):
        return float(base["height"])
    if base.get("type") == "revolve":
        try:
            zs = [float(q[1]) for q in base["profile_rz"]]
            return max(zs) - min(zs)
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    return None


# ---------------------------------------------------------------- checks

def check_profile(rep, where, p):
    if not isinstance(p, dict):
        rep.err(where, "'profile' must be an object")
        return
    s = p.get("shape")
    if not _hashable(s) or s not in PROFILE_SHAPES:
        rep.err(where, f"profile.shape must be one of {sorted(PROFILE_SHAPES)}, got {s!r}")
        return
    if s == "rect":
        w = _pos(rep, where, p, "width")
        l = _pos(rep, where, p, "length")
        cr = p.get("corner_radius")
        if cr is not None:
            if not _num(cr) or cr < 0:
                rep.err(where, "corner_radius must be >= 0")
            elif w and l and cr * 2 >= min(w, l):
                rep.err(where, f"corner_radius {cr} must be < half the smaller side ({min(w, l) / 2})")
        _angle(rep, where, p)
    elif s == "circle":
        _pos(rep, where, p, "diameter")
    elif s == "slot":
        L = _pos(rep, where, p, "length")
        W = _pos(rep, where, p, "width")
        if L and W and L <= W:
            rep.err(where, "slot length (overall, end to end) must exceed its width")
        _angle(rep, where, p)
    elif s == "regular_polygon":
        n = p.get("sides")
        n_ok = isinstance(n, int) and not isinstance(n, bool) and 3 <= n <= 360
        if not n_ok:
            rep.err(where, f"regular_polygon.sides must be an integer 3..360, got {n!r}")
        if p.get("across_flats") is not None:
            af = _pos(rep, where, p, "across_flats")
            if af and n_ok:
                ac = round(af / math.cos(math.pi / n), 6)
                if p.get("across_corners") is None:
                    p["across_corners"] = ac
                elif _num(p["across_corners"]) and abs(p["across_corners"] - ac) > 1e-3 * ac:
                    rep.err(where, f"across_flats {af} and across_corners {p['across_corners']} disagree for "
                                   f"{n} sides (across_flats {af} gives across_corners {ac}); give only one")
        _pos(rep, where, p, "across_corners")
    elif s == "polygon":
        pts = p.get("points")
        if not isinstance(pts, list) or len(pts) < 3 or not all(_point(q) for q in pts):
            rep.err(where, "polygon.points must be a list of >= 3 [x, y] pairs")
        else:
            prob = polygon_problem(pts)
            if prob:
                rep.err(where, f"polygon.points {prob}")


def _angle(rep, where, obj, key="angle_deg"):
    v = obj.get(key)
    if v is not None and not _num(v):
        rep.err(where, f"'{key}' must be a number (degrees), got {v!r}")


def check_profile_rz(rep, where, prof):
    if not isinstance(prof, list) or len(prof) < 3 or not all(_point(q) for q in prof):
        rep.err(where, "profile_rz must be a list of >= 3 [r, z] pairs (r = radius, z = axial)")
        return
    if any(q[0] < 0 for q in prof):
        rep.err(where, "profile_rz radii must be >= 0 (the profile may touch but not cross the axis)")
    elif max(q[0] for q in prof) <= 0:
        rep.err(where, "profile_rz has zero radius everywhere")
    else:
        prob = polygon_problem(prof)
        if prob:
            rep.err(where, f"profile_rz {prob}")


def check_base(rep, base):
    where = "base"
    if not isinstance(base, dict):
        rep.err(where, "'base' object is required")
        return
    t = base.get("type")
    if t == "extrude":
        check_profile(rep, where, base.get("profile"))
        _pos(rep, where, base, "height")
    elif t == "revolve":
        check_profile_rz(rep, where, base.get("profile_rz"))
    else:
        rep.err(where, "base.type must be 'extrude' (prismatic, along +Z) or 'revolve' (turned, about Z)")


def check_depth(rep, where, f, key="depth"):
    d = f.get(key)
    if d == "through":
        return "through"
    if not _num(d) or d <= 0:
        rep.err(where, f"'{key}' must be a positive number or \"through\", got {d!r}")
        return None
    return float(d)


def check_edges(rep, where, edges):
    if isinstance(edges, str):
        if not SELECTOR_RE.match(edges):
            rep.err(where, f"edge selector {edges!r} is not a CadQuery string selector")
    elif isinstance(edges, dict) and "near" in edges:
        near = edges["near"]
        pts = near if (isinstance(near, list) and near and isinstance(near[0], list)) else [near]
        if not all(isinstance(q, list) and len(q) == 3 and all(_num(c) for c in q) for q in pts):
            rep.err(where, "edges.near must be [x, y, z] or a list of [x, y, z] points")
    else:
        rep.err(where, "'edges' must be a selector string (e.g. \"|Z\", \">Z\") or {\"near\": [x, y, z] | [[x,y,z], ...]}")


def check_feature(rep, f, idx, ids, base):
    fid = f.get("id") if isinstance(f, dict) else None
    where = f"features[{idx}]" + (f" ({fid})" if fid else "")
    if not isinstance(f, dict):
        rep.err(where, "feature must be an object")
        return
    if not fid or not isinstance(fid, str):
        rep.err(where, "every feature needs an 'id' (use the drawing's callout, e.g. 'H1', 'Ø22')")
    elif fid in ids:
        rep.err(where, f"duplicate feature id {fid!r}")
    else:
        ids.add(fid)
    t = f.get("type")
    if not _hashable(t) or t not in FEATURE_TYPES:
        rep.err(where, f"type must be one of {sorted(FEATURE_TYPES)}, got {t!r}")
        return

    if not _hashable(f.get("face")):
        rep.err(where, f"'face' must be a string such as \">Z\" or \"YZ\", got {f.get('face')!r}")
        return
    if t in {"hole", "pocket", "slot"} and f.get("face") in DATUMS:
        if f.get("depth") != "through":
            rep.err(where, f"on datum plane {f['face']} the cut goes through both ways: depth must be \"through\"")
        if f.get("cbore") or f.get("csk"):
            rep.err(where, "counterbore/countersink needs a real face (>Z, <X, ...), not a datum plane")
    elif t in {"hole", "pocket", "boss", "slot", "shell"}:
        if f.get("face") not in FACES:
            allowed = sorted(FACES | DATUMS) if t in {"hole", "pocket", "slot"} else sorted(FACES)
            rep.err(where, f"'face' must be one of {allowed}")

    fp = base_footprint(base)
    if t == "hole":
        d = _pos(rep, where, f, "diameter")
        depth = check_depth(rep, where, f)
        pos, pat = f.get("positions"), f.get("pattern")
        n_err = len(rep.errors)
        if pos is not None:
            if not isinstance(pos, list) or not all(_point(q) for q in pos):
                rep.err(where, "'positions' must be a list of [x, y] pairs")
            elif len(pos) > MAX_INSTANCES:
                rep.err(where, f"more than {MAX_INSTANCES} positions")
        if pat is not None:
            if not isinstance(pat, dict):
                rep.err(where, "'pattern' must be an object with a 'type' (polar | grid | linear)")
            else:
                for e in pattern_errors(pat):
                    rep.err(where, e)
        pts = expand_positions(f)
        if not pts and len(rep.errors) == n_err:  # (a malformed positions/pattern was reported above)
            rep.err(where, "hole needs 'positions' [[x, y], ...] or a 'pattern'")
        dup = len(pts) - len(set(pts))
        if dup:
            rep.warn(where, f"{dup} hole position(s) repeated (positions + pattern overlap?)")
        cb, ck = f.get("cbore"), f.get("csk")
        if cb and ck:
            rep.err(where, "a hole has either 'cbore' or 'csk', not both")
        if isinstance(cb, dict):
            cd = _pos(rep, where + ".cbore", cb, "diameter")
            cdp = _pos(rep, where + ".cbore", cb, "depth")
            if d and cd and cd <= d:
                rep.err(where, f"counterbore Ø{cd} must exceed hole Ø{d}")
            if isinstance(depth, float) and cdp and cdp >= depth:
                rep.err(where, "counterbore depth must be less than hole depth")
            h = base_height(base)
            if depth == "through" and cdp and h and cdp >= h - 1e-6 and f.get("face") in {">Z", "<Z"}:
                rep.err(where, f"counterbore depth {cdp} reaches the full part height {h}: nothing is left of the hole")
        if isinstance(ck, dict):
            kd = _pos(rep, where + ".csk", ck, "diameter")
            ang = ck.get("angle_deg", 90)
            if not _num(ang) or not 30 <= ang <= 150:
                rep.err(where, "csk.angle_deg must be between 30 and 150 (usually 90)")
            if d and kd and kd <= d:
                rep.err(where, f"countersink Ø{kd} must exceed hole Ø{d}")
        outer = cb if isinstance(cb, dict) else ck if isinstance(ck, dict) else {}
        r_out = ((outer["diameter"] if _num(outer.get("diameter")) and outer["diameter"] > 0 else d) or 0) / 2
        for (x, y) in (pts if d else []):
            why = breakout(base, f.get("face"), x, y, r_out)
            if why:
                rep.warn(where, f"hole at ({x}, {y}) {why} — check the drawing")
        h = base_height(base)
        if isinstance(depth, float) and h and depth > h + 1e-6 and f.get("face") in {">Z", "<Z"}:
            rep.warn(where, f"blind depth {depth} exceeds part height {h}; use \"through\" if it is through")
    elif t in {"pocket", "boss"}:
        check_profile(rep, where, f.get("profile"))
        if not _point(f.get("at", [0, 0])):
            rep.err(where, "'at' must be [x, y]")
        if t == "pocket":
            check_depth(rep, where, f)
        else:
            _pos(rep, where, f, "height")
    elif t == "slot":
        L = _pos(rep, where, f, "length")
        W = _pos(rep, where, f, "width")
        if L and W and L <= W:
            rep.err(where, "slot 'length' is overall end-to-end and must exceed 'width'")
        _angle(rep, where, f)
        if not _point(f.get("at", [0, 0])):
            rep.err(where, "'at' must be [x, y]")
        check_depth(rep, where, f)
    elif t == "fillet":
        r = _pos(rep, where, f, "radius")
        check_edges(rep, where, f.get("edges"))
        h = base_height(base)
        if r and h and r >= h / 2 and isinstance(f.get("edges"), str) and "Z" in f["edges"] and f["edges"].startswith(("<", ">")):
            rep.warn(where, f"fillet R{r} is >= half the part height {h}; OCC will likely fail")
    elif t == "chamfer":
        _pos(rep, where, f, "size")
        if f.get("size2") is not None:
            _pos(rep, where, f, "size2")
        check_edges(rep, where, f.get("edges"))
    elif t == "revolve_cut":
        check_profile_rz(rep, where + " revolve_cut", f.get("profile_rz"))
    elif t == "shell":
        th = f.get("thickness")
        if not _num(th) or th == 0:
            rep.err(where, "shell.thickness must be a non-zero number (negative = inward)")
    elif t == "thread":
        if not isinstance(f.get("designation"), str) or not f["designation"].strip():
            rep.err(where, "thread needs a 'designation' exactly as on the drawing, e.g. 'M8x1.25-6H'")
        if not isinstance(f.get("on"), str):
            rep.err(where, "thread needs 'on': the id of the hole or boss it is cut in")
        if f.get("length") is not None:
            _pos(rep, where, f, "length")


def validate(spec):
    rep = Report()
    if not isinstance(spec, dict):
        rep.err("spec", "top level must be a JSON object")
        return rep, []
    if spec.get("schema") != SCHEMA:
        rep.err("schema", f"must be {SCHEMA!r}")
    part = spec.get("part")
    if not isinstance(part, str) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", part):
        rep.err("part", "must be a file-safe name: letters, digits, '_' or '-' (max 64)")
    if spec.get("units") != "mm":
        rep.err("units", "must be \"mm\". Convert inch drawings first (x 25.4) so STEP export is unambiguous")
    base = spec.get("base")
    check_base(rep, base)

    feats = spec.get("features", [])
    if not isinstance(feats, list):
        rep.err("features", "must be a list")
        feats = []
    ids = set()
    for i, f in enumerate(feats):
        check_feature(rep, f, i, ids, base)
    types = {f.get("id"): f.get("type") for f in feats if isinstance(f, dict) and isinstance(f.get("id"), str)}
    for i, f in enumerate(feats):
        if isinstance(f, dict) and f.get("type") == "thread" and isinstance(f.get("on"), str):
            where = f"features[{i}] ({f.get('id')})"
            if f["on"] not in ids:
                rep.err(where, f"thread refers to unknown feature {f.get('on')!r}")
            elif types.get(f["on"]) not in {"hole", "boss"}:
                rep.warn(where, f"thread is on {f['on']!r}, a {types.get(f['on'])}; threads are cut in a hole or on a boss")
    # Fillets/chamfers before solid-adding features usually means a misread order.
    seen_finish = False
    for f in feats:
        if not isinstance(f, dict):
            continue
        if f.get("type") in {"fillet", "chamfer"}:
            seen_finish = True
        elif seen_finish and f.get("type") in {"boss", "pocket", "hole", "slot"}:
            rep.warn("features", f"{f.get('id')} comes after a fillet/chamfer; edge finishing normally goes last")
            break

    exp = spec.get("expect")
    if not isinstance(exp, dict) or not exp.get("bbox_mm"):
        rep.warn("expect", "no expect.bbox_mm: the verifier will have no overall-dimension target. Take it from the drawing's overall dimensions")
    else:
        bb = exp["bbox_mm"]
        if not (isinstance(bb, list) and len(bb) == 3 and all(_num(c) and c > 0 for c in bb)):
            rep.err("expect.bbox_mm", "must be [X, Y, Z] overall sizes in mm")
        tol = exp.get("tolerance_mm")
        if tol is not None and (not _num(tol) or tol <= 0):
            rep.err("expect.tolerance_mm", f"must be a positive number in mm, got {tol!r}")
    if spec.get("mass_kg") is not None and (not _num(spec["mass_kg"]) or spec["mass_kg"] <= 0):
        rep.err("mass_kg", "must be a positive number (the title-block mass) or null")
    if spec.get("density_g_cm3") is not None and (not _num(spec["density_g_cm3"]) or spec["density_g_cm3"] <= 0):
        rep.err("density_g_cm3", "must be a positive number in g/cm³ (e.g. 7.85 for steel) or absent")
    if spec.get("drawing") is not None and not isinstance(spec["drawing"], dict):
        rep.err("drawing", "must be an object such as {\"title\": ..., \"number\": ..., \"scale\": ...}")

    assumptions = spec.get("assumptions", []) or []
    if not isinstance(assumptions, list):
        rep.err("assumptions", "must be a list of {what, value, source} objects")
        assumptions = []
    for i, a in enumerate(assumptions):
        if not isinstance(a, dict) or not a.get("what") or "value" not in a:
            rep.err(f"assumptions[{i}]", "each assumption needs 'what' and 'value' (and ideally 'source')")
    oq = spec.get("open_questions") or []
    if isinstance(oq, str):
        oq = [oq]  # a single question written as a string still blocks generation
    if not isinstance(oq, list):
        rep.err("open_questions", "must be a list of questions (strings)")
        oq = []
    open_q = [q if isinstance(q, str) else json.dumps(q, ensure_ascii=False) for q in oq if q]
    return rep, open_q


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Validate a part-spec.v1 JSON (a drawing readout) before generating CAD code.",
        epilog="Exit: 0 valid · 1 warnings with --strict · 2 errors · 3 open questions · 64 usage")
    ap.add_argument("spec", nargs="?", help="path to the part spec JSON")
    ap.add_argument("--sample", action="store_true", help="validate the built-in sample spec")
    ap.add_argument("--print-sample", action="store_true", help="print the sample spec and exit")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures (exit 1)")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

    if a.print_sample:
        print(json.dumps(SAMPLE_SPEC, indent=2, ensure_ascii=False))
        return 0
    if a.sample:
        spec = json.loads(json.dumps(SAMPLE_SPEC))
    elif a.spec:
        spec, err = load_json(a.spec)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 64
    else:
        ap.print_usage(sys.stderr)
        return 64

    rep, open_q = validate(spec)
    if rep.errors:
        code, verdict = 2, "INVALID"
    elif open_q:
        code, verdict = 3, "OPEN-QUESTIONS"
    elif rep.warnings and a.strict:
        code, verdict = 1, "WARNINGS"
    else:
        code, verdict = 0, "VALID"

    feats = spec.get("features", []) if isinstance(spec, dict) else []
    feats = feats if isinstance(feats, list) else []
    holes = sum(len(expand_positions(f)) for f in feats if isinstance(f, dict) and f.get("type") == "hole")
    if a.output == "json":
        print(json.dumps({"verdict": verdict, "exit": code, "errors": rep.errors,
                          "warnings": rep.warnings, "open_questions": open_q,
                          "features": len(feats), "hole_instances": holes}, indent=2, ensure_ascii=False))
    else:
        print(f"Part spec: {spec.get('part', '?') if isinstance(spec, dict) else '?'} — {verdict}")
        print(f"  features: {len(feats)}   hole instances: {holes}")
        for e in rep.errors:
            print(f"  ERROR    {e}")
        for w in rep.warnings:
            print(f"  WARNING  {w}")
        if open_q:
            print("  Open questions — answer these before any code is generated:")
            for q in open_q:
                print(f"    ? {q}")
    return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head/less that closed early
        sys.stderr.close()
        sys.exit(0)
