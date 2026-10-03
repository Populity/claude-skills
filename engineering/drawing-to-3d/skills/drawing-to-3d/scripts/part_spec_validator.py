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
        {"id": "C1", "type": "chamfer", "edges": ">Z", "size": 1},
    ],
    "expect": {"bbox_mm": [120, 80, 16], "tolerance_mm": 0.05},
    "assumptions": [
        {"id": "A1", "what": "Top outer edges chamfer", "value": "1x45°",
         "source": "general note 'Неуказанные фаски 1x45°'"}
    ],
    "open_questions": [],
}


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
    """Return explicit [(x, y), ...] for a feature's positions/pattern (or [] if none)."""
    pts = [tuple(float(c) for c in p) for p in feat.get("positions", []) if _point(p)]
    pat = feat.get("pattern")
    if isinstance(pat, dict):
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
        except (KeyError, TypeError, ValueError):
            return None
    if base.get("type") == "revolve":
        try:
            r = max(float(q[0]) for q in base["profile_rz"])
            return r, r
        except (KeyError, TypeError, ValueError):
            return None
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
        except (KeyError, TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------- checks

def check_profile(rep, where, p):
    if not isinstance(p, dict):
        rep.err(where, "'profile' must be an object")
        return
    s = p.get("shape")
    if s not in PROFILE_SHAPES:
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
    elif s == "circle":
        _pos(rep, where, p, "diameter")
    elif s == "slot":
        L = _pos(rep, where, p, "length")
        W = _pos(rep, where, p, "width")
        if L and W and L <= W:
            rep.err(where, "slot length (overall, end to end) must exceed its width")
    elif s == "regular_polygon":
        n = p.get("sides")
        if not isinstance(n, int) or n < 3:
            rep.err(where, "regular_polygon.sides must be an integer >= 3")
        if p.get("across_flats") is not None and p.get("across_corners") is None:
            af = _pos(rep, where, p, "across_flats")
            if af and isinstance(n, int) and n >= 3:
                p["across_corners"] = round(af / math.cos(math.pi / n), 6)
        _pos(rep, where, p, "across_corners")
    elif s == "polygon":
        pts = p.get("points")
        if not isinstance(pts, list) or len(pts) < 3 or not all(_point(q) for q in pts):
            rep.err(where, "polygon.points must be a list of >= 3 [x, y] pairs")
        elif len({tuple(q) for q in pts}) != len(pts):
            rep.err(where, "polygon.points contains duplicate vertices")


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
        prof = base.get("profile_rz")
        if not isinstance(prof, list) or len(prof) < 3 or not all(_point(q) for q in prof):
            rep.err(where, "profile_rz must be a list of >= 3 [r, z] pairs (r = radius, z = axial)")
        else:
            if any(q[0] < 0 for q in prof):
                rep.err(where, "profile_rz radii must be >= 0 (the profile may touch but not cross the axis)")
            if max(q[0] for q in prof) <= 0:
                rep.err(where, "profile_rz has zero radius everywhere")
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
    if t not in FEATURE_TYPES:
        rep.err(where, f"type must be one of {sorted(FEATURE_TYPES)}, got {t!r}")
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
        pts = expand_positions(f)
        if not pts:
            rep.err(where, "hole needs 'positions' [[x, y], ...] or a 'pattern'")
        pat = f.get("pattern")
        if isinstance(pat, dict) and pat.get("type") not in {"polar", "grid", "linear"}:
            rep.err(where, "pattern.type must be polar | grid | linear")
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
        if isinstance(ck, dict):
            kd = _pos(rep, where + ".csk", ck, "diameter")
            ang = ck.get("angle_deg", 90)
            if not _num(ang) or not 30 <= ang <= 150:
                rep.err(where, "csk.angle_deg must be between 30 and 150 (usually 90)")
            if d and kd and kd <= d:
                rep.err(where, f"countersink Ø{kd} must exceed hole Ø{d}")
        if fp and d and f.get("face") in {">Z", "<Z"}:
            hx, hy = fp
            outer = cb if isinstance(cb, dict) else ck if isinstance(ck, dict) else {}
            r_out = (outer["diameter"] if _num(outer.get("diameter")) else d) / 2
            for (x, y) in pts:
                if abs(x) + r_out > hx + 1e-6 or abs(y) + r_out > hy + 1e-6:
                    rep.warn(where, f"hole at ({x}, {y}) breaks out of the base footprint ±{hx}×±{hy} — check the drawing")
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
        prof = f.get("profile_rz")
        if not isinstance(prof, list) or len(prof) < 3 or not all(_point(q) for q in prof):
            rep.err(where, "revolve_cut.profile_rz must be >= 3 [r, z] pairs")
    elif t == "shell":
        th = f.get("thickness")
        if not _num(th) or th == 0:
            rep.err(where, "shell.thickness must be a non-zero number (negative = inward)")
    elif t == "thread":
        if not isinstance(f.get("designation"), str) or not f["designation"].strip():
            rep.err(where, "thread needs a 'designation' exactly as on the drawing, e.g. 'M8x1.25-6H'")
        if not isinstance(f.get("on"), str):
            rep.err(where, "thread needs 'on': the id of the hole or boss it is cut in")


def validate(spec):
    rep = Report()
    if not isinstance(spec, dict):
        rep.err("spec", "top level must be a JSON object")
        return rep, []
    if spec.get("schema") != SCHEMA:
        rep.err("schema", f"must be {SCHEMA!r}")
    part = spec.get("part")
    if not isinstance(part, str) or not re.match(r"^[A-Za-z0-9_\-]{1,64}$", part or ""):
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
    for i, f in enumerate(feats):
        if isinstance(f, dict) and f.get("type") == "thread" and f.get("on") not in ids:
            rep.err(f"features[{i}] ({f.get('id')})", f"thread refers to unknown feature {f.get('on')!r}")
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
    if spec.get("mass_kg") is not None and (not _num(spec["mass_kg"]) or spec["mass_kg"] <= 0):
        rep.err("mass_kg", "must be a positive number (the title-block mass) or null")

    for i, a in enumerate(spec.get("assumptions", []) or []):
        if not isinstance(a, dict) or not a.get("what") or "value" not in a:
            rep.err(f"assumptions[{i}]", "each assumption needs 'what' and 'value' (and ideally 'source')")
    open_q = [q for q in (spec.get("open_questions") or []) if q]
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
        try:
            with open(a.spec, encoding="utf-8") as fh:
                spec = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read spec: {e}", file=sys.stderr)
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
    sys.exit(main())
