#!/usr/bin/env python3
"""model_verifier.py — check a built model against the drawing it came from.

Reads the part spec (the drawing readout) and the ``<part>.report.json`` that
the generated CadQuery script writes after building, and checks:

  V1  the result is one valid solid (OpenCascade isValid, exactly 1 solid)
  V2  overall size matches the drawing's overall dimensions (expect.bbox_mm, ± tolerance)
  V3  every hole on the drawing exists in the model at its drawn position: a
      cylindrical face of the hole's diameter (and its counterbore's) whose axis
      passes within 0.05 mm of the hole centre, normal to the face it is drilled
      from. Fillets, rounded corners and slot ends of the same size do not count.
      (Reports from older scripts without per-cylinder axes fall back to
      counting diameters only, and say so.)
  V4  mass from the title block matches volume x material density (± 3 %, title
      blocks are rounded) — skipped when the drawing gives no mass
  V5  thread callouts are carried into the model report

A model is not "done" because the script ran; it is done when these pass.
The final check — overlaying views on the drawing — is visual and stays with
the human; this tool covers what can be measured.

Exit codes: 0 PASS · 1 FAIL · 64 usage. Stdlib only.
"""

import argparse
import json
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from part_spec_validator import SAMPLE_SPEC, expand_positions, face_axis_point, load_json  # noqa: E402

AXIS_TOL_MM = 0.05   # hole axis must pass this close to the drawn centre
DIAM_TOL_MM = 0.01

# g/cm^3 — typical values; the drawing's material grade wins if it states density.
DENSITY = {
    "steel": 7.85, "сталь": 7.85, "stainless": 7.93, "нержавейка": 7.93, "12х18н10т": 7.9,
    "cast iron": 7.2, "чугун": 7.2, "aluminium": 2.70, "aluminum": 2.70, "алюминий": 2.70,
    "д16т": 2.78, "6061": 2.70, "7075": 2.81, "brass": 8.5, "латунь": 8.5, "bronze": 8.8,
    "бронза": 8.8, "copper": 8.96, "медь": 8.96, "titanium": 4.43, "титан": 4.43,
    "abs": 1.04, "pla": 1.24, "petg": 1.27, "nylon": 1.14, "полиамид": 1.14,
    "pom": 1.41, "капролон": 1.15, "ptfe": 2.2, "фторопласт": 2.2,
}

# Report written by a real build of SAMPLE_SPEC (cadquery_codegen.py --sample, CadQuery 2.8).
SAMPLE_REPORT = {
    "part": "flange_plate",
    "valid": True,
    "solids": 1,
    "bbox_mm": [120.0, 80.0, 16.0],
    "bbox_min_mm": [-60.0, -40.0, 0.0],
    "volume_mm3": 129498.006,
    "area_mm2": 29402.99,
    "faces": 52,
    "edges": 124,
    "cylinder_diameters_mm": [6.8, 6.8, 6.8, 6.8, 6.8, 6.8, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 9.0, 9.0, 9.0, 9.0, 16.0, 16.0, 16.0, 16.0, 22.0, 34.0],
    "cylinders": [
        {"diameter": 6.8, "axis_point": [-25.0, 0.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 6.8, "axis_point": [-12.5, -21.65064, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 6.8, "axis_point": [-12.5, 21.65064, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 6.8, "axis_point": [12.5, -21.65064, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 6.8, "axis_point": [12.5, 21.65064, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 6.8, "axis_point": [25.0, 0.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 8.0, "axis_point": [-41.0, -6.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 8.0, "axis_point": [-41.0, 6.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 8.0, "axis_point": [-11.0, -30.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 8.0, "axis_point": [11.0, -30.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 8.0, "axis_point": [41.0, -6.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 8.0, "axis_point": [41.0, 6.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 9.0, "axis_point": [-48.0, -28.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 9.0, "axis_point": [-48.0, 28.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 9.0, "axis_point": [48.0, -28.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 9.0, "axis_point": [48.0, 28.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 16.0, "axis_point": [-52.0, -32.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 16.0, "axis_point": [-52.0, 32.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 16.0, "axis_point": [52.0, -32.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 16.0, "axis_point": [52.0, 32.0, 0.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 22.0, "axis_point": [0.0, 0.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
        {"diameter": 34.0, "axis_point": [0.0, 0.0, 16.0], "axis_dir": [0.0, 0.0, 1.0]},
    ],
    "threads": [{"id": "T1", "type": "thread", "on": "H3", "designation": "M8x1.25-6H", "length": 12}],
}


def density_for(spec):
    if _num(spec.get("density_g_cm3")):
        return float(spec["density_g_cm3"])
    mat = str(spec.get("material") or "").lower()
    if spec.get("density_g_cm3") is not None:
        return None  # given but not a number: the validator reports it; do not guess
    for key, rho in sorted(DENSITY.items(), key=lambda kv: -len(kv[0])):
        if key in mat:
            return rho
    return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _vec3(v):
    return isinstance(v, list) and len(v) == 3 and all(_num(c) for c in v)


def _axis_hits(cyl, diameter, point, direction):
    """True if report cylinder `cyl` is a hole of `diameter` on the axis (point, direction)."""
    if not isinstance(cyl, dict) or not _num(cyl.get("diameter")):
        return False
    a, n = cyl.get("axis_point"), cyl.get("axis_dir")
    if not (_vec3(a) and _vec3(n)) or abs(cyl["diameter"] - diameter) > DIAM_TOL_MM:
        return False
    ln = math.sqrt(sum(c * c for c in n)) or 1.0
    n = [c / ln for c in n]
    if abs(abs(sum(n[i] * direction[i] for i in range(3))) - 1) > 1e-4:
        return False
    v = [point[i] - a[i] for i in range(3)]
    t = sum(v[i] * n[i] for i in range(3))
    return math.sqrt(max(0.0, sum(c * c for c in v) - t * t)) <= AXIS_TOL_MM


def hole_instances(spec):
    """[(feature id, diameter, kind, (u, v), face)] for every hole and counterbore on the drawing."""
    out, bad = [], []
    for f in spec.get("features", []) if isinstance(spec.get("features"), list) else []:
        if not isinstance(f, dict) or f.get("type") != "hole":
            continue
        face = f.get("face")
        if not _num(f.get("diameter")) or not isinstance(face, str):
            bad.append(str(f.get("id")))
            continue
        cb = f.get("cbore")
        for uv in expand_positions(f):
            out.append((f.get("id"), round(float(f["diameter"]), 4), "hole", uv, face))
            if isinstance(cb, dict) and _num(cb.get("diameter")):
                out.append((f.get("id"), round(float(cb["diameter"]), 4), "cbore", uv, face))
    return out, bad


def check_holes(spec, report):
    """V3 -> (ok, detail, skipped)."""
    inst, bad = hole_instances(spec)
    if bad:
        return False, f"hole(s) {', '.join(bad)} have no numeric diameter/face in the spec — run part_spec_validator.py", False
    if not inst:
        return True, "no holes on the drawing", True
    cyls = report.get("cylinders")
    if isinstance(cyls, list):
        found, missing = Counter(), []
        need = Counter((d, kind) for _, d, kind, _, _ in inst)
        for fid, d, kind, (u, v), face in inst:
            pt, dirn = face_axis_point(face, u, v)
            if any(_axis_hits(c, d, pt, dirn) for c in cyls):
                found[(d, kind)] += 1
            else:
                missing.append(f"{fid} {'cbore ' if kind == 'cbore' else ''}Ø{d:g} at ({u:g}, {v:g}) on {face}")
        detail = ", ".join(f"{'cbore ' if k == 'cbore' else ''}Ø{d:g}: {found[(d, k)]}/{n} at drawn position"
                           for (d, k), n in sorted(need.items()))
        if missing:
            more = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
            detail += " — missing: " + "; ".join(missing[:5]) + more
        return not missing, detail, False
    diams = report.get("cylinder_diameters_mm")
    if not isinstance(diams, list):
        return False, "report has no 'cylinders' or 'cylinder_diameters_mm' — regenerate and re-run the script", False
    have = Counter(round(d, 3) for d in diams if _num(d))
    need = Counter(round(d, 3) for _, d, _, _, _ in inst)
    detail = ", ".join(f"Ø{d:g}: need {n}, found {have.get(d, 0)}" for d, n in sorted(need.items()))
    ok = all(have.get(d, 0) >= n for d, n in need.items())
    return ok, detail + " (diameter count only: this report has no per-cylinder axes; regenerate the script to check positions)", False


def verify(spec, report):
    checks = []

    def add(cid, ok, msg, skipped=False):
        checks.append({"id": cid, "status": "SKIP" if skipped else ("PASS" if ok else "FAIL"), "detail": msg})

    missing_keys = [k for k in ("valid", "solids") if k not in report]
    ok = report.get("valid") is True and report.get("solids") == 1 and not missing_keys
    msg = f"valid={report.get('valid')} solids={report.get('solids')}"
    if missing_keys:
        msg += f" — report has no {' / '.join(missing_keys)}: not a report written by the generated script"
    elif not ok:
        msg += " — a fused/broken body; check the feature that failed or a boss that does not touch the body"
    if isinstance(report.get("part"), str) and isinstance(spec.get("part"), str) and report["part"] != spec["part"]:
        ok = False
        msg += f" — report is for part {report['part']!r}, the spec is {spec['part']!r}: wrong report?"
    add("V1", ok, msg)

    exp = spec.get("expect") if isinstance(spec.get("expect"), dict) else {}
    bb_exp = exp.get("bbox_mm")
    tol = exp.get("tolerance_mm", 0.05)
    bb = report.get("bbox_mm")
    if not bb_exp:
        add("V2", False, "no expect.bbox_mm in the spec", skipped=True)
    elif not _vec3(bb_exp) or not _num(tol) or tol <= 0:
        add("V2", False, f"spec expect.bbox_mm {bb_exp!r} / tolerance_mm {tol!r} is not [X, Y, Z] + a positive tolerance")
    elif not _vec3(bb):
        add("V2", False, f"report has no usable bbox_mm ({bb!r}) — regenerate and re-run the script")
    else:
        diffs = [round(b - e, 4) for b, e in zip(bb, bb_exp)]
        ok = all(abs(d) <= tol for d in diffs)
        add("V2", ok, f"model {bb} vs drawing {bb_exp} (±{tol}) — deltas {diffs}")

    ok, detail, skipped = check_holes(spec, report)
    add("V3", ok, detail, skipped=skipped)

    mass = spec.get("mass_kg")
    rho = density_for(spec)
    vol = report.get("volume_mm3")
    if _num(mass) and mass > 0 and rho and not _num(vol):
        add("V4", False, f"report has no usable volume_mm3 ({vol!r}) to compare with the {mass} kg title-block mass")
    elif _num(mass) and mass > 0 and rho and _num(vol):
        model_mass = vol * rho / 1e6  # mm3 * g/cm3 -> kg
        err = (model_mass - mass) / mass
        add("V4", abs(err) <= 0.03,
            f"title block {mass} kg vs model {model_mass:.3f} kg ({rho} g/cm3) — {err:+.1%}"
            + ("" if abs(err) <= 0.03 else " — a feature is missing, extra, or a dimension is misread"))
    else:
        why = "no mass_kg on the drawing" if not _num(mass) else "unknown material density (set density_g_cm3)"
        extra = f"; model volume {vol} mm3" if _num(vol) else ""
        add("V4", False, why + extra, skipped=True)

    feats = spec.get("features") if isinstance(spec.get("features"), list) else []
    want = {str(f.get("id")) for f in feats if isinstance(f, dict) and f.get("type") == "thread"}
    threads = report.get("threads")
    got = {str(t.get("id")) for t in threads if isinstance(t, dict)} if isinstance(threads, list) else set()
    if want and not isinstance(threads, list):
        add("V5", False, f"report has no 'threads' list; drawing calls out {sorted(want)}")
    elif want:
        add("V5", want <= got, f"thread callouts in report: {sorted(got)} (drawing: {sorted(want)})")
    else:
        add("V5", True, "no thread callouts", skipped=True)

    verdict = "FAIL" if any(c["status"] == "FAIL" for c in checks) else "PASS"
    return verdict, checks


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Verify a generated model's report.json against the drawing readout (part spec).",
        epilog="Exit: 0 PASS · 1 FAIL · 64 usage")
    ap.add_argument("--spec", help="part-spec.v1 JSON")
    ap.add_argument("--report", help="<part>.report.json written by the generated script")
    ap.add_argument("--sample", action="store_true", help="verify the built-in sample spec + report")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

    if a.sample:
        spec, report = SAMPLE_SPEC, SAMPLE_REPORT
    elif a.spec and a.report:
        spec, err = load_json(a.spec)
        report, err2 = (None, None) if err else load_json(a.report)
        if err or err2:
            print(f"error: {err or err2}", file=sys.stderr)
            return 64
        for name, obj in (("spec", spec), ("report", report)):
            if not isinstance(obj, dict):
                print(f"error: the {name} must be a JSON object, got {type(obj).__name__}", file=sys.stderr)
                return 64
    else:
        ap.print_usage(sys.stderr)
        return 64

    verdict, checks = verify(spec, report)
    if a.output == "json":
        print(json.dumps({"part": spec.get("part"), "verdict": verdict, "checks": checks},
                         indent=2, ensure_ascii=False))
    else:
        print(f"Model check: {spec.get('part')} — {verdict}")
        for c in checks:
            print(f"  {c['status']:<4} {c['id']}  {c['detail']}")
        if verdict == "PASS":
            print("  Last step is visual: compare the model's views with the drawing (stl_viewer.py).")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head/less that closed early
        sys.stderr.close()
        sys.exit(0)
