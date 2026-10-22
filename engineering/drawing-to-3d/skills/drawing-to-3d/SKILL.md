---
name: drawing-to-3d
description: Use when turning an engineering drawing (чертёж, blueprint) into a precise 3D model — a photo, scan, PDF or DXF of a part (ЕСКД/ГОСТ, ISO or ASME) that must become STEP + STL, or when a CadQuery model of a drawn part is wanted. Reads the views into a dimensioned part spec, refuses to guess any missing dimension (asks instead), generates a CadQuery script the user runs on their own PC, then verifies the built solid against the drawing's overall size, hole table, title-block mass and thread callouts before calling it done.
argument-hint: "[drawing file (png/jpg/pdf/dxf) or part spec JSON]"
license: MIT
metadata:
  version: 1.0.0
  build_pattern: "Drawing readout -> validated part-spec.v1 JSON -> generated CadQuery script (STEP/STL + measurement report) -> deterministic verifier + HTML view check; 5 stdlib scripts"
  distinct_from: "Not mesh sculpting, photogrammetry or AI image-to-3D (those approximate shape; this reproduces dimensions); not general CAD advice; not a 2D drafting or drawing-checking aid"
---

# Drawing to 3D — a dimensioned drawing in, a verified STEP/STL out

Eight stdlib scripts in `scripts/` (every one has `--help` and `--sample`). The drawing is read once into a
**part spec** (`part-spec.v1` JSON); everything after that is deterministic code, so a wrong model traces to one spec line.

## Hard rules

1. **Never guess a dimension.** Anything not readable goes into `open_questions`; validator and codegen refuse (exit 3) until the user answers. Values taken from general notes or standards go in `assumptions` with their `source`.
2. **mm only.** `units` must be `"mm"`; convert inch drawings (×25.4) before writing the spec.
3. **One valid solid.** A model with 2 bodies or an invalid shape is a failure, not a draft.
4. **Not done until** `model_verifier.py` says PASS **and** every view in the HTML viewer has been compared with the drawing.

## Workflow

1. **Read the drawing.** Image/scan: open it with Read, zoom on the title block (основная надпись) and every view. PDF: render pages to images first. DXF: never read pixels — `python3 scripts/dxf_geometry_extractor.py part.dxf` gives units, the exact hole table, arc radii, dimension texts (%%c→Ø) and notes (material, mass). Binary DXF/DWG exits 2: re-save as ASCII DXF.
2. **Fill the readout** in `assets/drawing_readout_worksheet.md`: projection method, views, overall sizes, every callout, general notes (технические требования).
3. **Map views to global axes** (X right, Y back, Z up). Front view (главный вид) = X/Z, top view (вид сверху) = X/Y, left view (вид слева) = Y/Z. ЕСКД/ISO-E is **first-angle**: top view sits *below* the front view, left view to the *right*. ASME/ISO-A is **third-angle**: top above, right view to the right. The model is identical either way; only where you look changes. Details: `references/drawing_reading_guide.md`.
4. **Write the spec** (format: `references/part_spec_format.md`; start from `python3 scripts/part_spec_validator.py --print-sample` or an asset). Base is either `extrude` (profile in XY, along +Z from z=0) or `revolve` (`profile_rz: [[r, z], ...]` about Z — shafts, bushings). All coordinates are **global drawing coordinates**: ±Z faces and `XY` take (X, Y); ±X faces and `YZ` take (Y, Z); ±Y faces and `XZ` take (X, Z).
5. **Validate:** `python3 scripts/part_spec_validator.py part.spec.json [--strict]` — 0 valid · 1 warnings under `--strict` · 2 errors · 3 open questions · 64 usage.
6. **Generate:** `python3 scripts/cadquery_codegen.py part.spec.json` → `<part>_cad.py` next to the spec (refuses on 2/3).
7. **Build** on the user's PC (or here if `import cadquery` works): `python <part>_cad.py --out build/ [--formats step,stl] [--stl-tolerance 0.01]` → `<part>.step`, `.stl`, `.report.json`. In CQ-editor the same file shows the model live.
8. **Verify:** `python3 scripts/model_verifier.py --spec part.spec.json --report build/<part>.report.json` — V1 one valid solid · V2 bbox vs `expect.bbox_mm` ± `tolerance_mm` (default 0.05) · V3 each hole/counterbore Ø present ≥ required count · V4 title-block mass vs volume×density ±3 % · V5 thread callouts carried. Exit 0 PASS / 1 FAIL.
9. **Look:** `python3 scripts/stl_viewer.py build/<part>.stl --report build/<part>.report.json` → one HTML page; press Front/Top/Left/Right/Back/Bottom/Iso with **Ortho** on and compare each against the drawing view.
10. **Iterate:** fix the spec (never the generated script), re-run 5→9.

### Feature table (exactly what the validator accepts)

| type | required | optional |
|---|---|---|
| `hole` | `face`, `diameter`, `depth` (number or `"through"`), `positions` [[u,v]…] or `pattern` | `cbore` {diameter, depth} (цековка) **or** `csk` {diameter, angle_deg 30–150, default 90} (зенковка) |
| `pocket` | `face`, `profile`, `depth` | `at` [u,v] (default [0,0]) |
| `boss` | `face`, `profile`, `height` | `at` |
| `slot` | `face`, `length` (overall, > width), `width`, `depth` | `at`, `angle_deg` |
| `fillet` | `radius`, `edges` | — |
| `chamfer` | `size`, `edges` | `size2` (unequal legs) |
| `revolve_cut` | `profile_rz` ≥3 [r,z] (grooves, undercuts — канавки) | — |
| `shell` | `face`, `thickness` (≠0, negative = inward) | — |
| `thread` | `on` (id of a hole/boss), `designation` as drawn, e.g. `M8x1.25-6H` | `length` |

Every feature needs a unique `id` (use the drawing callout). `face`: `>Z <Z >X <X >Y <Y`; holes, pockets and slots may also use datum planes `XY XZ YZ` — through-only cuts both ways, no cbore/csk (cross holes in shafts). `profile.shape`: `rect` (width, length, corner_radius, angle_deg), `circle` (diameter), `slot`, `polygon` (points), `regular_polygon` (sides + across_corners or across_flats). `pattern`: `polar` (count, pcd, start_angle_deg, span_deg, center), `grid` (nx, ny, pitch_x, pitch_y, center), `linear` (count, pitch, direction_deg, start). `edges`: a CadQuery selector (`"|Z"`, `">Z"`) or `{"near": [x,y,z] | [[x,y,z],…]}` — the closest real edge to each point (seams skipped, must be within 0.5 mm). Threads are **cosmetic**: model the hole at drill Ø (M8 → 6.8), the callout goes to the report. Modelling recipes: `references/cadquery_modeling_patterns.md`.

## Worked example — flange plate (`assets/flange_plate.dxf`, `assets/flange_plate.spec.json`)

Readout: plate 120×80×16, R8 corners; 4×Ø9 through on a 96×56 grid; Ø22 through with цековка Ø34×6; 6×M8-6H on PCD 50 (drill Ø6.8 ↧14); title block: Сталь 45, mass 1,02 kg.

```json
{"id": "H2", "type": "hole", "face": ">Z", "diameter": 22, "depth": "through",
 "positions": [[0, 0]], "cbore": {"diameter": 34, "depth": 6}},
{"id": "T1", "type": "thread", "on": "H3", "designation": "M8x1.25-6H", "length": 12}
```

```bash
python3 scripts/part_spec_validator.py assets/flange_plate.spec.json   # VALID, 7 features, 11 hole instances
python3 scripts/cadquery_codegen.py assets/flange_plate.spec.json --out flange_plate_cad.py
python flange_plate_cad.py --out build/    # bbox 120.0 x 80.0 x 16.0 mm  valid=True solids=1
python3 scripts/model_verifier.py --spec assets/flange_plate.spec.json --report build/flange_plate.report.json
#  PASS V2  model [120.0, 80.0, 16.0] vs drawing [120, 80, 16] (±0.05)
#  PASS V3  Ø6.8: need 6, found 6, Ø9.0: need 4, found 4, Ø22.0: need 1, found 1, Ø34.0: need 1, found 1
#  PASS V4  title block 1.02 kg vs model 1.017 kg (7.85 g/cm3) — -0.3%
python3 scripts/stl_viewer.py build/flange_plate.stl --report build/flange_plate.report.json
```

## Troubleshooting

| Symptom | Meaning → fix |
|---|---|
| `feature F1 fillet R8 failed: BRep_API: command not done` | Radius too big for the adjacent faces, or two finishes collide on a thin wall. Re-read the radius; narrow `edges` to the edges the drawing marks. |
| `no edge within 0.5 mm of (x, y, z); nearest is N mm away` | The `near` point is not on an edge in global coords — recheck which view/axis the point came from. |
| `If multiple objects selected, they all must be planar faces` | The `face` (e.g. `>Y` on a round shaft) is curved. Use a datum plane (`XZ`/`YZ`) with `depth: "through"`. |
| V2 FAIL | A view mapped to the wrong axis, a misread overall size, or a boss/pocket on the wrong face. |
| V3 `need n, found < n` | Hole missing, merged with another feature, or pattern count/PCD misread. |
| V4 off by > 3 % | A feature missing or extra, or a misread dimension; unknown alloy → set top-level `density_g_cm3`. |
| V1 `solids=2` | A boss does not touch the body, or a cut split the part — check its `at` and face. |

## Like KOMPAS-3D: beyond one STEP file

Run these only after the part passes `model_verifier.py`. Each emits a script for the user's PC.

| Need | Command | Output |
|---|---|---|
| Editable model in KOMPAS-3D (дерево построения) | `kompas_codegen.py spec.json` → `<part>_kompas.py` (Windows, `pip install pywin32`; try `--dry-run` first) | `.m3d` + STEP; selector-string fillets exit 4 → rewrite as `near` points |
| ЕСКД drawing (виды, рамка, основная надпись) | `eskd_drawing_codegen.py spec.json --cad-script <part>_cad.py` | DXF R2010 + PNG preview; sections, tolerances, roughness added by hand |
| Assembly + спецификация ГОСТ 2.106 | `assembly_codegen.py assembly.asm.json` (`assembly-spec.v1`, see `assets/assembly_example/`) | assembly STEP, clash report, spec CSV/MD; exit 1 on a clash or unsatisfied mate |

## On the user's PC

Python 3.10+ on Windows, macOS or Linux, then `pip install cadquery` (in a venv; `conda install -c conda-forge cadquery` if pip has no wheel). Optional: CQ-editor for a live preview. Open the STEP in FreeCAD, KOMPAS-3D, Fusion 360 or SolidWorks; the STL in any slicer or viewer. Send back `<part>.report.json` (or the error line naming the feature id) for verification.
