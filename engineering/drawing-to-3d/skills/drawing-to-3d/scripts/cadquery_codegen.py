#!/usr/bin/env python3
"""cadquery_codegen.py — turn a validated part spec into a runnable CadQuery script.

The generated script is the deliverable that runs on the user's PC:

    pip install cadquery
    python <part>_cad.py            # -> <part>.step, <part>.stl, <part>.report.json

Every feature becomes one labelled step carrying its drawing callout id, so an
OpenCascade failure (a fillet too large, a pocket that misses the body) names
the exact feature instead of a stack trace. The script measures what it built
(bounding box, volume, cylindrical-face diameters, solid validity) and writes a
report that model_verifier.py checks against the drawing.

This tool itself is stdlib-only and never imports CadQuery: it emits text. It
refuses to generate from a spec that fails part_spec_validator.py (exit 2) or
still has open questions (exit 3).

Exit codes: 0 written · 2 spec invalid · 3 open questions · 64 usage.
"""

import argparse
import json
import math
import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from part_spec_validator import DATUMS, SAMPLE_SPEC, expand_positions, load_json, validate  # noqa: E402


def _one_line(text):
    """Spec text made safe for a '#' comment or a docstring line in generated code.

    Anything that could end the line (newline, CR, U+2028, NUL, other control or
    format characters) becomes a space, so a drawing title or thread designation
    can never start a new line of code. Docstring delimiters are defused by
    _doc_text() below.
    """
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        out.append(" " if cat[0] in "CZ" and ch != " " else ch)
    return " ".join("".join(out).split())


def _doc_text(text):
    """_one_line() plus escaping for use inside a triple-quoted docstring."""
    return _one_line(text).replace("\\", "\\\\").replace('"', '\\"')


def _f(v):
    """Format a number compactly and deterministically."""
    v = float(v)
    return repr(int(v)) if v.is_integer() else repr(round(v, 6))


def _pt(p):
    return f"({_f(p[0])}, {_f(p[1])})"


def _pts(pts):
    return "[" + ", ".join(_pt(p) for p in pts) + "]"


def _loc(pts, face):
    """Coordinates as code: literal for the base, or converted to face-local at run time."""
    lit = _pts(pts)
    return lit if face is None else f"L(wp, {face!r}, {lit})"


def _ang(deg, face):
    return _f(deg) if face is None else f"A(wp, {face!r}, {_f(deg)})"


def profile_chain(p, at=(0, 0), face=None):
    """CadQuery chain (starting with '.') that places profile `p` centred at `at`.

    `at`, polygon points and angles are global in-face coordinates (as dimensioned
    on the drawing); for a face workplane they are converted with L()/A() in the
    generated script, so mirrored face axes (e.g. <Z flips Y) never move a feature.
    """
    s = p["shape"]
    loc = _loc([at], face)
    if s == "rect":
        w, l = p["width"], p["length"]
        cr = p.get("corner_radius") or 0
        ang = p.get("angle_deg") or 0
        if cr or ang or face is not None:
            chain = f".sketch().push({loc}).rect({_f(w)}, {_f(l)}, angle={_ang(ang, face)})"
            if cr:
                chain += f".reset().vertices().fillet({_f(cr)})"
            return chain + ".finalize()"
        return f".pushPoints({loc}).rect({_f(w)}, {_f(l)})"
    if s == "circle":
        return f".pushPoints({loc}).circle({_f(p['diameter'] / 2)})"
    if s == "slot":
        return (f".pushPoints({loc}).slot2D({_f(p['length'])}, {_f(p['width'])}, "
                f"{_ang(p.get('angle_deg', 0), face)})")
    if s == "regular_polygon":
        n, rad = int(p["sides"]), float(p["across_corners"]) / 2
        if face is None:
            return f".pushPoints({loc}).polygon({n}, {_f(p['across_corners'])})"
        # On a face the local axes may be mirrored (<X, >Y ...), which would mirror an
        # odd-sided polygon. Emit the vertices in global in-face coordinates instead:
        # first vertex on the global +u axis, as .polygon() does on the XY plane.
        pts = [(at[0] + rad * math.cos(2 * math.pi * i / n), at[1] + rad * math.sin(2 * math.pi * i / n))
               for i in range(n)]
        return f".polyline({_loc(pts, face)}).close()"
    if s == "polygon":
        pts = [(q[0] + at[0], q[1] + at[1]) for q in p["points"]]
        return f".polyline({_loc(pts, face)}).close()"
    raise ValueError(f"unknown profile shape {s!r}")


def _wp(face):
    if face in DATUMS:
        return f"wp = cq.Workplane({face!r})"
    return f'wp = r.faces({face!r}).workplane(centerOption="ProjectedOrigin")'


def _through_datum(chain):
    """A cut through the whole part, both ways from a datum plane."""
    return f"r = r.cut(wp{chain}.extrude(_big(r), both=True))"


def _edge_steps(edges, op):
    """Return code lines applying `op` (e.g. 'fillet(2)') to the selected edges.

    A selector string goes to CadQuery as is. {"near": ...} points pick, for each
    point, the edge geometrically closest to it (true distance, seam lines of
    revolved faces excluded) and finish them all in one operation.
    """
    if isinstance(edges, str):
        return [f"r = r.edges({edges!r}).{op}"]
    near = edges["near"]
    pts = near if isinstance(near[0], list) else [near]
    lit = "[" + ", ".join(f"({_f(q[0])}, {_f(q[1])}, {_f(q[2])})" for q in pts) + "]"
    return [f"r = r.newObject(near_edges(r, {lit})).{op}"]


def _label(f):
    t = f["type"]
    if t == "hole":
        n = len(expand_positions(f))
        depth = "through" if f.get("depth") == "through" else f"depth {_f(f['depth'])}"
        extra = ""
        if f.get("cbore"):
            extra = f", cbore Ø{_f(f['cbore']['diameter'])}x{_f(f['cbore']['depth'])}"
        elif f.get("csk"):
            extra = f", csk Ø{_f(f['csk']['diameter'])}x{_f(f['csk'].get('angle_deg', 90))}°"
        return f"hole Ø{_f(f['diameter'])} {depth}{extra} x{n} on {f['face']}"
    if t == "fillet":
        return f"fillet R{_f(f['radius'])}"
    if t == "chamfer":
        return f"chamfer {_f(f['size'])}" + (f"x{_f(f['size2'])}" if f.get("size2") else "x45°")
    if t in {"pocket", "boss"}:
        dim = f.get("depth", f.get("height"))
        return f"{t} {f['profile']['shape']} {'through' if dim == 'through' else _f(dim)} on {f['face']}"
    if t == "slot":
        return f"slot {_f(f['length'])}x{_f(f['width'])} on {f['face']}"
    return t


def feature_lines(f):
    t = f["type"]
    if t == "hole":
        pts = expand_positions(f)
        d = _f(f["diameter"])
        depth = None if f.get("depth") == "through" else _f(f["depth"])
        dep = f", depth={depth}" if depth else ""
        head = f"wp.pushPoints({_loc(pts, f['face'])})"
        if f["face"] in DATUMS:
            return [_wp(f["face"]), _through_datum(f".pushPoints({_loc(pts, f['face'])}).circle({_f(f['diameter'] / 2)})")]
        if f.get("cbore"):
            cb = f["cbore"]
            return [_wp(f["face"]), f"r = {head}.cboreHole({d}, {_f(cb['diameter'])}, {_f(cb['depth'])}{dep})"]
        if f.get("csk"):
            ck = f["csk"]
            return [_wp(f["face"]), f"r = {head}.cskHole({d}, {_f(ck['diameter'])}, {_f(ck.get('angle_deg', 90))}{dep})"]
        return [_wp(f["face"]), f"r = {head}.hole({d}{dep})"]
    if t in {"pocket", "slot"} and f["face"] in DATUMS:
        prof = f["profile"] if t == "pocket" else {"shape": "slot", "length": f["length"],
                                                    "width": f["width"], "angle_deg": f.get("angle_deg", 0)}
        return [_wp(f["face"]), _through_datum(profile_chain(prof, f.get("at", [0, 0]), f["face"]))]
    if t == "pocket":
        cut = ".cutThruAll()" if f["depth"] == "through" else f".cutBlind({_f(-f['depth'])})"
        return [_wp(f["face"]), f"r = wp{profile_chain(f['profile'], f.get('at', [0, 0]), f['face'])}{cut}"]
    if t == "boss":
        return [_wp(f["face"]),
                f"r = wp{profile_chain(f['profile'], f.get('at', [0, 0]), f['face'])}.extrude({_f(f['height'])})"]
    if t == "slot":
        prof = {"shape": "slot", "length": f["length"], "width": f["width"], "angle_deg": f.get("angle_deg", 0)}
        cut = ".cutThruAll()" if f["depth"] == "through" else f".cutBlind({_f(-f['depth'])})"
        return [_wp(f["face"]), f"r = wp{profile_chain(prof, f.get('at', [0, 0]), f['face'])}{cut}"]
    if t == "fillet":
        return _edge_steps(f["edges"], f"fillet({_f(f['radius'])})")
    if t == "chamfer":
        op = f"chamfer({_f(f['size'])}" + (f", {_f(f['size2'])})" if f.get("size2") else ")")
        return _edge_steps(f["edges"], op)
    if t == "revolve_cut":
        return [f"cutter = cq.Workplane(\"XZ\").polyline({_pts(f['profile_rz'])}).close()"
                f".revolve(360, (0, 0, 0), (0, 1, 0))",
                "r = r.cut(cutter)"]
    if t == "shell":
        return [f"r = r.faces({f['face']!r}).shell({_f(f['thickness'])})"]
    if t == "thread":
        return [f"# Cosmetic thread {_one_line(f['designation'])} on {_one_line(f['on'])}: modelled at its drill/nominal",
                "# diameter (standard CAD practice); the callout is recorded in the report."]
    raise ValueError(t)


def base_lines(base):
    if base["type"] == "extrude":
        return [f"r = cq.Workplane(\"XY\"){profile_chain(base['profile'])}.extrude({_f(base['height'])})"]
    return [f"r = cq.Workplane(\"XZ\").polyline({_pts(base['profile_rz'])}).close()"
            ".revolve(360, (0, 0, 0), (0, 1, 0))"]


TEMPLATE = '''#!/usr/bin/env python3
"""{part} — 3D model generated from drawing {drawing}.

Generated by drawing-to-3d/cadquery_codegen.py from {spec_name}. Edit the spec
and regenerate rather than hand-editing, so drawing and model stay in step.

    pip install cadquery
    python {script_name}                 # writes {part}.step, {part}.stl, {part}.report.json
    python {script_name} --out build/    # choose output folder

Open the STEP in FreeCAD / Fusion 360 / SolidWorks / KOMPAS-3D; open the STL in
any viewer or slicer. Opening this file in CQ-editor shows the model live.
"""
import argparse
import contextlib
import json
import math
import os
import sys

import cadquery as cq

PART = {part!r}
SPEC = {spec_json}


def _face_point(face, origin, u, v):
    """Global 3D point for drawing coordinates (u, v) on a face.

    +-Z faces and XY use (X, Y); +-X faces and YZ use (Y, Z); +-Y faces and XZ
    use (X, Z) -- always global axes, exactly as dimensioned on the drawing's views.
    """
    if face == "XY":
        return cq.Vector(u, v, 0)
    if face == "XZ":
        return cq.Vector(u, 0, v)
    if face == "YZ":
        return cq.Vector(0, u, v)
    axis = face[-1]
    if axis == "Z":
        return cq.Vector(u, v, origin.z)
    if axis == "X":
        return cq.Vector(origin.x, u, v)
    return cq.Vector(u, origin.y, v)


def L(wp, face, pts):
    """Convert global in-face coordinates to the workplane's local ones."""
    out = []
    for u, v in pts:
        p = wp.plane.toLocalCoords(_face_point(face, wp.plane.origin, u, v))
        out.append((round(p.x, 9), round(p.y, 9)))
    return out


def A(wp, face, deg):
    """Convert a global in-face angle to the workplane's local angle."""
    (x0, y0), (x1, y1) = L(wp, face, [(0, 0), (math.cos(math.radians(deg)), math.sin(math.radians(deg)))])
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def near_edges(r, points):
    """For each point, the edge closest to it (true distance, not edge centre).

    Seam lines of revolved/cylindrical faces are skipped: they are not real edges
    on the drawing and cannot be filleted or chamfered.
    """
    from OCP.BRep import BRep_Tool
    solid = r.val()
    seams = []
    for face in solid.Faces():
        for e in face.Edges():
            if BRep_Tool.IsClosed_s(e.wrapped, face.wrapped):
                seams.append(e)
    edges = [e for e in solid.Edges() if not any(e.isSame(s) for s in seams)]
    picked = []
    for p in points:
        v = cq.Vertex.makeVertex(*p)
        best = min(edges, key=lambda e: e.distance(v))
        if best.distance(v) > 0.5:
            raise ValueError(f"no edge within 0.5 mm of {{p}}; nearest is {{best.distance(v):.2f}} mm away")
        if not any(best.isSame(x) for x in picked):
            picked.append(best)
    return picked


def _big(r):
    """A length that certainly passes through the whole part."""
    return 2 * r.val().BoundingBox().DiagonalLength + 1


@contextlib.contextmanager
def feature(label):
    try:
        yield
    except Exception as exc:  # name the drawing feature, not just the OCC error
        raise RuntimeError(f"feature {{label}} failed: {{exc}}") from exc


def build():
{body}
    return r


def measure(wp):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    shape = wp.val()
    solids = wp.solids().vals()
    bb = shape.BoundingBox()
    cyl, axes = [], []
    for face in shape.Faces():
        if face.geomType() == "CYLINDER":
            c = BRepAdaptor_Surface(face.wrapped).Cylinder()
            d = round(2 * c.Radius(), 4)
            cyl.append(d)
            loc, dirn = c.Axis().Location(), c.Axis().Direction()
            n = [dirn.X(), dirn.Y(), dirn.Z()]
            if next(x for x in n if abs(x) > 1e-9) < 0:  # one sign per axis, for readability
                n = [-x for x in n]
            axes.append({{"diameter": d, "axis_point": [round(v, 5) + 0.0 for v in (loc.X(), loc.Y(), loc.Z())],
                         "axis_dir": [round(x, 6) + 0.0 for x in n]}})  # + 0.0 drops "-0.0"
    return {{
        "part": PART,
        "valid": bool(shape.isValid()),
        "solids": len(solids),
        "bbox_mm": [round(bb.xlen, 4), round(bb.ylen, 4), round(bb.zlen, 4)],
        "bbox_min_mm": [round(bb.xmin, 4), round(bb.ymin, 4), round(bb.zmin, 4)],
        "volume_mm3": round(shape.Volume(), 3),
        "area_mm2": round(shape.Area(), 3),
        "faces": len(shape.Faces()),
        "edges": len(shape.Edges()),
        "cylinder_diameters_mm": sorted(cyl),
        # per cylindrical face: diameter + axis line, so the verifier can tell a hole
        # at its drawn position from a fillet, rounded corner or slot end of equal size
        "cylinders": sorted(axes, key=lambda c: (c["diameter"], c["axis_point"])),
        "threads": [f for f in SPEC.get("features", []) if f.get("type") == "thread"],
    }}


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"Build {{PART}} and export STEP/STL + a measurement report.")
    ap.add_argument("--out", default=".", help="output folder (default: current)")
    ap.add_argument("--formats", default="step,stl", help="comma list of step, stl, brep, svg")
    ap.add_argument("--stl-tolerance", type=float, default=0.01,
                    help="STL linear deflection in mm, absolute: max distance between the mesh and the true "
                         "surface (smaller = finer mesh; default 0.01)")
    ap.add_argument("--stl-angular-tolerance", type=float, default=0.1,
                    help="STL angular deflection in radians between neighbouring facets (smaller = finer "
                         "mesh on curves; default 0.1)")
    a = ap.parse_args(argv)
    if a.stl_tolerance <= 0 or a.stl_angular_tolerance <= 0:
        ap.error("--stl-tolerance and --stl-angular-tolerance must be > 0")
    os.makedirs(a.out, exist_ok=True)
    r = build()
    report = measure(r)
    base = os.path.join(a.out, PART)
    for fmt in [x.strip().lower() for x in a.formats.split(",") if x.strip()]:
        path = f"{{base}}.{{fmt}}"
        if fmt == "stl":
            # Shape.exportStl directly: cq.exporters.export() leaves relative=True, which
            # turns the tolerance into a fraction of each edge's length instead of mm.
            solids = r.vals()
            shape = solids[0] if len(solids) == 1 else cq.Compound.makeCompound(solids)
            shape.exportStl(path, tolerance=a.stl_tolerance, angularTolerance=a.stl_angular_tolerance,
                            relative=False)
        else:
            cq.exporters.export(r, path)
        print(f"wrote {{path}}")
    with open(f"{{base}}.report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"wrote {{base}}.report.json")
    bx, by, bz = report["bbox_mm"]
    print(f"bbox {{bx}} x {{by}} x {{bz}} mm   volume {{report['volume_mm3']}} mm3   "
          f"valid={{report['valid']}} solids={{report['solids']}}")
    return 0 if report["valid"] and report["solids"] == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
elif "show_object" in globals():  # CQ-editor
    show_object(build(), name=PART)  # noqa: F821
'''


def generate(spec, spec_name="spec.json"):
    body = []
    with_base = base_lines(spec["base"])
    body.append('    with feature("base"):')
    body += [f"        {ln}" for ln in with_base]
    for f in spec.get("features", []):
        body.append(f"    with feature({(f['id'] + ' ' + _label(f))!r}):")
        lines = feature_lines(f)
        if all(ln.startswith("#") for ln in lines):
            lines.append("pass")
        body += [f"        {ln}" for ln in lines]
    drawing = spec.get("drawing") if isinstance(spec.get("drawing"), dict) else {}
    drawing_id = " ".join(str(drawing.get(k)) for k in ("number", "title") if drawing.get(k)) or "(unnamed)"
    return TEMPLATE.format(
        part=spec["part"], drawing=_doc_text(drawing_id), spec_name=_doc_text(os.path.basename(spec_name)),
        script_name=f"{spec['part']}_cad.py",
        spec_json=repr(spec),
        body="\n".join(body),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a runnable CadQuery script (STEP/STL export + measurement report) from a part spec.",
        epilog="Exit: 0 written · 2 spec invalid · 3 open questions · 64 usage")
    ap.add_argument("spec", nargs="?", help="part-spec.v1 JSON")
    ap.add_argument("--sample", action="store_true", help="use the built-in sample spec")
    ap.add_argument("--out", help="script path (default: <part>_cad.py next to the spec; '-' = stdout)")
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

    try:
        code = generate(spec, spec_path)
    except (KeyError, TypeError, ValueError) as e:  # a spec shape the validator let through
        print(f"Refusing to generate: cannot turn the spec into code ({type(e).__name__}: {e}).", file=sys.stderr)
        return 2
    if a.out == "-" or (a.sample and not a.out):
        if a.output == "json":
            print(json.dumps({"written": "-", "features": len(spec.get("features", [])),
                              "warnings": rep.warnings, "script": code}, indent=2, ensure_ascii=False))
        else:
            sys.stdout.write(code)
        return 0
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(spec_path)), f"{spec['part']}_cad.py")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(code)
    except OSError as e:
        print(f"error: cannot write {out}: {e}", file=sys.stderr)
        return 64
    if a.output == "json":
        print(json.dumps({"written": out, "features": len(spec.get("features", [])),
                          "warnings": rep.warnings}, indent=2, ensure_ascii=False))
    else:
        print(f"wrote {out}")
        for w in rep.warnings:
            print(f"  WARNING {w}")
        print(f"next: pip install cadquery && python {os.path.basename(out)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head/less that closed early
        sys.stderr.close()
        sys.exit(0)
