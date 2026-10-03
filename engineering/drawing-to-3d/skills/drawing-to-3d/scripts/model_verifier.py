#!/usr/bin/env python3
"""model_verifier.py — check a built model against the drawing it came from.

Reads the part spec (the drawing readout) and the ``<part>.report.json`` that
the generated CadQuery script writes after building, and checks:

  V1  the result is one valid solid (OpenCascade isValid, exactly 1 solid)
  V2  overall size matches the drawing's overall dimensions (expect.bbox_mm, ± tolerance)
  V3  every hole diameter on the drawing exists in the model, at least as many
      times as the drawing calls for (cylindrical faces; counterbores included)
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
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from part_spec_validator import SAMPLE_SPEC, expand_positions  # noqa: E402

# g/cm^3 — typical values; the drawing's material grade wins if it states density.
DENSITY = {
    "steel": 7.85, "сталь": 7.85, "stainless": 7.93, "нержавейка": 7.93, "12х18н10т": 7.9,
    "cast iron": 7.2, "чугун": 7.2, "aluminium": 2.70, "aluminum": 2.70, "алюминий": 2.70,
    "д16т": 2.78, "6061": 2.70, "7075": 2.81, "brass": 8.5, "латунь": 8.5, "bronze": 8.8,
    "бронза": 8.8, "copper": 8.96, "медь": 8.96, "titanium": 4.43, "титан": 4.43,
    "abs": 1.04, "pla": 1.24, "petg": 1.27, "nylon": 1.14, "полиамид": 1.14,
    "pom": 1.41, "капролон": 1.15, "ptfe": 2.2, "фторопласт": 2.2,
}

SAMPLE_REPORT = {
    "part": "flange_plate", "valid": True, "solids": 1,
    "bbox_mm": [120.0, 80.0, 16.0], "volume_mm3": 129220.488,
    "cylinder_diameters_mm": [6.8] * 6 + [8.0] * 6 + [9.0] * 4 + [16.0] * 4 + [22.0, 34.0],
    "threads": [{"id": "T1", "type": "thread", "on": "H3", "designation": "M8x1.25-6H"}],
}


def density_for(spec):
    if _num(spec.get("density_g_cm3")):
        return float(spec["density_g_cm3"])
    mat = str(spec.get("material") or "").lower()
    for key, rho in sorted(DENSITY.items(), key=lambda kv: -len(kv[0])):
        if key in mat:
            return rho
    return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def verify(spec, report):
    checks = []

    def add(cid, ok, msg, skipped=False):
        checks.append({"id": cid, "status": "SKIP" if skipped else ("PASS" if ok else "FAIL"), "detail": msg})

    ok = bool(report.get("valid")) and report.get("solids") == 1
    add("V1", ok, f"valid={report.get('valid')} solids={report.get('solids')}"
        + ("" if ok else " — a fused/broken body; check the feature that failed or a boss that does not touch the body"))

    exp = spec.get("expect") or {}
    bb_exp = exp.get("bbox_mm")
    tol = float(exp.get("tolerance_mm", 0.05))
    bb = report.get("bbox_mm")
    if bb_exp and bb:
        diffs = [round(b - e, 4) for b, e in zip(bb, bb_exp)]
        ok = all(abs(d) <= tol for d in diffs)
        add("V2", ok, f"model {bb} vs drawing {bb_exp} (±{tol}) — deltas {diffs}")
    else:
        add("V2", False, "no expect.bbox_mm in the spec", skipped=True)

    have = Counter(round(d, 3) for d in report.get("cylinder_diameters_mm", []))
    need = Counter()
    for f in spec.get("features", []):
        if f.get("type") != "hole":
            continue
        n = len(expand_positions(f))
        need[round(float(f["diameter"]), 3)] += n
        if isinstance(f.get("cbore"), dict):
            need[round(float(f["cbore"]["diameter"]), 3)] += n
    missing = {d: (n, have.get(d, 0)) for d, n in need.items() if have.get(d, 0) < n}
    if need:
        detail = ", ".join(f"Ø{d}: need {n}, found {have.get(d, 0)}" for d, n in sorted(need.items()))
        add("V3", not missing, detail)
    else:
        add("V3", True, "no holes on the drawing", skipped=True)

    mass = spec.get("mass_kg")
    rho = density_for(spec)
    vol = report.get("volume_mm3")
    if _num(mass) and rho and _num(vol):
        model_mass = vol * rho / 1e6  # mm3 * g/cm3 -> kg
        err = (model_mass - mass) / mass
        add("V4", abs(err) <= 0.03,
            f"title block {mass} kg vs model {model_mass:.3f} kg ({rho} g/cm3) — {err:+.1%}"
            + ("" if abs(err) <= 0.03 else " — a feature is missing, extra, or a dimension is misread"))
    else:
        why = "no mass_kg on the drawing" if not _num(mass) else "unknown material density (set density_g_cm3)"
        extra = f"; model volume {vol} mm3" if _num(vol) else ""
        add("V4", False, why + extra, skipped=True)

    want = {f["id"] for f in spec.get("features", []) if f.get("type") == "thread"}
    got = {t.get("id") for t in report.get("threads", [])}
    if want:
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
        try:
            with open(a.spec, encoding="utf-8") as fh:
                spec = json.load(fh)
            with open(a.report, encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: {e}", file=sys.stderr)
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
    sys.exit(main())
