---
description: Check an already-built model (its report.json) against the drawing's part spec, and produce the HTML viewer for comparing views.
argument-hint: "[part.spec.json] [part.report.json] [part.stl]"
---

# /cs:cad-verify

The short pass. The model has already been built. Answer one question about
`$ARGUMENTS`:

> **Does this model match the drawing it came from?**

Act as `cs-cad-modeler` and answer in the user's language.

## Inputs

- `<part>.spec.json`: the drawing readout (part-spec.v1)
- `<part>.report.json`: written by the generated `<part>_cad.py` when it ran
- `<part>.stl`: optional, used for the viewer

If the report is missing, the model has not been built. Tell the user to run
`python <part>_cad.py` (after `pip install cadquery`) and send back the
report, or switch to `/cs:drawing-to-3d`.

## Run

```bash
python skills/drawing-to-3d/scripts/model_verifier.py --spec <part>.spec.json --report <part>.report.json
python skills/drawing-to-3d/scripts/stl_viewer.py <part>.stl --report <part>.report.json
```

Use `--output json` on either one for machine-readable results. Use
`model_verifier.py --sample` to see what a passing run looks like.

## The checks

| Check | What it measures |
|---|---|
| V1 | exactly one valid solid (OpenCascade `isValid`) |
| V2 | overall size equals the drawing's overall dimensions (`expect.bbox_mm`, ± tolerance) |
| V3 | every hole diameter on the drawing is present, at least as many times as called for (counterbores included) |
| V4 | title-block mass equals volume × material density (± 3 %), skipped when the drawing gives no mass |
| V5 | thread callouts are carried into the report |

Exit: **0 PASS · 1 FAIL** · 64 usage.

## Then: the views

The verifier covers what can be measured. The last check is visual. Open
`<part>.html`, turn on **Ortho**, and compare each view with the drawing.
On a first-angle sheet (ESKD / ISO-E), Top sits below Front. On a third-angle
sheet (ASME), it sits above. Look for features in the wrong place or
mirrored, a missing chamfer or fillet, and a counterbore on the wrong face.
None of these change the bounding box or the diameter count.

## Report

1. **Verdict**: PASS or FAIL.
2. **Each failing check**: what the drawing calls for, what the model has,
   and the spec feature id responsible.
3. **The fix**, which is always made in the spec (then regenerate with
   `cadquery_codegen.py` and rebuild). Never hand-edit the script for
   something the spec can express.
4. **View comparison**: which views were checked, and any disagreement.

## Do not

- Do not report PASS when only the script's run succeeded.
- Do not widen a tolerance or delete an `expect` entry to make a check pass.
- Do not skip the view comparison because V1 to V5 passed. A mirrored hole
  pattern passes every numeric check.
