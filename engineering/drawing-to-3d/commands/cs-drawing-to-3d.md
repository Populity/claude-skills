---
description: Turn an engineering drawing (photo, scan, PDF or DXF; ESKD/GOST or ISO/ASME) into a verified 3D model, as STEP + STL built by a CadQuery script you run on your own PC.
argument-hint: "[drawing image/PDF/DXF path]"
---

# /cs:drawing-to-3d

Full pipeline for `$ARGUMENTS`: drawing → part spec → CadQuery script →
STEP/STL → verified against the drawing. Act as `cs-cad-modeler`
(`agents/cs-cad-modeler.md`) and answer in the user's language.

Opening question, before any geometry:

> **Which dimension is not on the drawing?**

## 0. Pre-flight

- No file given → ask for the drawing. Accept a photo, scan, PDF or ASCII DXF.
  DWG and binary DXF must be re-saved as ASCII DXF (KOMPAS-3D, AutoCAD,
  nanoCAD, FreeCAD and LibreCAD can all do this).
- Freeform or organic surfaces (castings with sculpted surfaces, blades,
  ergonomic shells) → refuse. This plugin builds prismatic and turned parts.
- Assembly drawing (сборочный чертёж) → one part per spec. Ask which detail
  (деталь) to model first.

## 1. Exact geometry (DXF only)

```bash
python skills/drawing-to-3d/scripts/dxf_geometry_extractor.py <drawing.dxf>
```

Take hole diameters and centres, radii and dimension values from this output,
not from the picture. Exit 2 means the file is not ASCII DXF: ask the user to
re-save it.

## 2. Read the drawing

Follow `skills/drawing-to-3d/references/drawing_reading_guide.md` and fill in
`skills/drawing-to-3d/assets/drawing_readout_worksheet.md`:

1. Title block (основная надпись): part name, number, scale, material, mass.
2. Projection method: first-angle (ESKD / ISO-E, Top view **below** Front) or
   third-angle (ASME / ISO-A, Top view above Front).
3. Overall dimensions, then the base body.
4. Every feature with its callout: holes and patterns, counterbores and
   countersinks, pockets, slots, bosses, fillets, chamfers, threads, and
   turned steps (revolve).
5. Technical requirements (технические требования): unspecified radii and
   chamfers, which are often stated only here.

## 3. Write the part spec

Write `<part>.spec.json` in `part-spec.v1` format
(`skills/drawing-to-3d/references/part_spec_format.md`; worked examples in
`skills/drawing-to-3d/assets/flange_plate.spec.json`, `stepped_shaft.spec.json`
and `bracket.spec.json`). Rules:

- `units: "mm"`, and convert inch drawings.
- Each feature `id` is the drawing's callout.
- Put the overall dimensions in `expect.bbox_mm` and the title-block mass in
  `mass_kg`.
- Anything you cannot read with certainty goes in `open_questions`. **Never
  guess.**

## 4. Gate

```bash
python skills/drawing-to-3d/scripts/part_spec_validator.py <part>.spec.json
```

| Exit | Meaning | Action |
|---|---|---|
| 0 | valid | continue |
| 1 | warnings (only with `--strict`) | fix or justify each warning |
| 2 | spec errors | fix the spec and re-run |
| 3 | open questions | **stop**: ask every question in one numbered message, naming the view for each, and wait |
| 64 | unreadable input | check the path or the JSON syntax |

## 5. Generate

```bash
python skills/drawing-to-3d/scripts/cadquery_codegen.py <part>.spec.json
```

This writes `<part>_cad.py` next to the spec. Exit 2 or 3 means go back to
step 4. Don't hand-edit the script to change something the spec can express.
Fix the spec and regenerate instead.

## 6. Build

The script runs wherever CadQuery is installed: in this environment if it is
available, otherwise on the user's PC:

```bash
pip install cadquery          # Python 3.10+, Windows / macOS / Linux
python <part>_cad.py          # → <part>.step, <part>.stl, <part>.report.json
python <part>_cad.py --out out --formats step,stl,svg
```

The script exits 1 if the result is not exactly one valid solid. Any
OpenCascade failure names the feature id that caused it.

## 7. Verify

```bash
python skills/drawing-to-3d/scripts/model_verifier.py --spec <part>.spec.json --report <part>.report.json
```

The verifier runs five checks (V1 to V5): one valid solid, overall size,
every hole diameter and its count, title-block mass against volume ×
density, and thread callouts. On exit 1 (FAIL), trace the failing check to
its spec feature, fix the spec, and go back to step 4. Never widen a
tolerance to get a PASS. After three failed loops on the same check, stop
and report.

## 8. Compare the views

```bash
python skills/drawing-to-3d/scripts/stl_viewer.py <part>.stl --report <part>.report.json
```

Open `<part>.html` in a browser, switch on Ortho, and compare Front, Top, Left
and any section with the drawing.

## Report

1. **Status**: PASS, FAIL (with the failing V-checks) or BLOCKED (with the
   open questions).
2. **Assumptions**, each with its reason.
3. **Files and how to use them**: `<part>.spec.json`, `<part>_cad.py`,
   `<part>.step` (opens in KOMPAS-3D, FreeCAD, SolidWorks, Fusion 360 or
   Inventor), `<part>.stl` (3D printing, any viewer) and `<part>.html`.
4. **Not modelled**: threads are cosmetic (tap-drill hole plus the callout),
   and tolerances, roughness and heat treatment remain notes.

## Do not

- Don't fill in a missing dimension because it "looks right" or from the
  sheet scale. Measuring off a scaled photo is not reading a dimension.
- Don't call a model done because the script ran. Done is verifier PASS plus
  a view comparison.
- Don't use any unit other than millimetres in the spec.
