# Drawing readout worksheet (лист считывания чертежа)

Fill this in **before** writing `part-spec.v1` JSON. Each number in the spec must
appear on this sheet with the view it was read from. Anything you cannot read
goes into section 9 (open questions), and the spec is not generated until that
list is empty.

Mark every value **D** (read from the drawing) or **A** (assumed). Every **A**
becomes an entry in the spec's `assumptions` with its `source`.

---

## 1. Drawing identity (основная надпись, title block)

| Field | Value | D / A | Where on the sheet |
|---|---|---|---|
| Part name (наименование) | | | |
| Drawing number (обозначение) | | | |
| Material (материал), grade + standard | | | e.g. "Сталь 45 ГОСТ 1050-2013" |
| Density used, g/cm³ (only if the grade is not in the verifier's table) | | | |
| Mass (масса), kg | | | becomes `mass_kg`; leave blank if not given |
| Scale (масштаб) | | | |
| Sheet / revision (лист / изм.) | | | |
| Projection method | ☐ first angle (ESKD, ISO E) ☐ third angle (ASME, ISO A) | | symbol in the title block, or view layout |
| Units | ☐ mm ☐ inch (convert ×25.4 before the spec) | | |

> Do not scale the drawing. Use a printed number only, or exact geometry from a DXF
> (`scripts/dxf_geometry_extractor.py`).

## 2. Views present (виды, разрезы, сечения)

Choose the global axes **once**, then record which plane each view shows. Spec
coordinates are global: ±Z faces / XY use (X, Y); ±X faces / YZ use (Y, Z);
±Y faces / XZ use (X, Z).

| View (вид / разрез / сечение / выносной элемент) | Label on the sheet | Shows global plane | Horizontal axis on the sheet = | Vertical axis on the sheet = | Notes |
|---|---|---|---|---|---|
| Main view (главный вид) | | | | | |
| Top view (вид сверху) | | | | | |
| Left view (вид слева) | | | | | |
| Section (разрез А-А) | | | | | |
| Local view / detail (вид Б, выносной элемент) | | | | | |

Origin (начало координат) chosen at: ______________________ (normally the dimensioning
datum: a centre line, an end face, the underside).

## 3. Overall dimensions (габаритные размеры)

| Axis | Size, mm | D / A | View |
|---|---|---|---|
| X | | | |
| Y | | | |
| Z | | | |

These go to `expect.bbox_mm`. Tolerance for the verifier (`expect.tolerance_mm`): ______

## 4. Base body

☐ **Extrude** (prismatic part: plate, bracket, housing) — profile in XY, extruded along +Z from z=0.
  Profile shape: ☐ rect ☐ circle ☐ polygon ☐ regular_polygon ☐ slot
  Sizes: __________________ corner radius: ______ height: ______

☐ **Revolve** (turned part: shaft, bush, flange hub — тело вращения) — `profile_rz` half-section,
  radius r ≥ 0, axial z, revolved about global Z.
  Profile points (r, z), walked around the half-section:
  `[ [0, 0], [ , ], [ , ], ... ]`

Why this choice: ____________________________________________

## 5. Feature table (элементы)

One row per callout. Use the drawing's own callout as the id (`H1`, `Ø22`, `А`).
Positions are global in-face coordinates (see section 2). A pattern is one row.

| id (callout) | type | size | depth / height | position(s) or pattern | face / datum | source view | D / A |
|---|---|---|---|---|---|---|---|
| | hole | Ø | through / | | >Z | | |
| | hole + counterbore (цековка) | Ø  / cbore Ø × depth | | | | | |
| | hole + countersink (зенковка) | Ø  / csk Ø × angle | | | | | |
| | cross hole | Ø | through | | XY / XZ / YZ | | |
| | pocket (карман, выборка) | profile | | at | | | |
| | boss (бобышка, прилив) | profile | height | at | | | |
| | slot (паз) | length (overall) × width | | at, angle | | | |
| | groove (канавка) — revolve_cut | profile_rz | | | — | | |
| | fillet (скругление) | R | — | edges: selector or near points | — | | |
| | chamfer (фаска) | size × 45° | — | edges: selector or near points | — | | |
| | shell | wall thickness | — | removed face | | | |

Checks while filling:
- Count instances: "4 отв." / "6 отв." must equal the number of positions in the row.
- Slot length is end to end (overall), not centre to centre.
- A pattern on a PCD (диаметр расположения): pcd, count, start angle measured from +X.
- Edge-finishing (fillet, chamfer) rows come last, after all solid features.
- For near-point edges on a turned part, pick points off the +X seam (on −X or ±Y).

## 6. Threads (резьбы)

| id | on (hole / boss id) | designation exactly as drawn | thread length | drill / minor Ø modelled | D / A |
|---|---|---|---|---|---|
| | | e.g. M8x1.25-6H | | e.g. Ø6.8 | |

Threads are cosmetic: the hole is modelled at drill diameter, the callout is carried
into the report (verifier check V5).

## 7. General notes (технические требования)

| Note on the drawing | What it sets in the model | Applied to which edges |
|---|---|---|
| Unspecified radii (неуказанные радиусы) R__ | | |
| Unspecified chamfers (неуказанные фаски) __×45° | | |
| General tolerances (неуказанные предельные отклонения) | not modelled; record only | — |
| Surface finish, coating, heat treatment | not modelled; record only | — |

## 8. Cross-check between views

| Feature | Seen in view 1 | Seen in view 2 | Consistent? |
|---|---|---|---|
| | | | ☐ |

Every hidden line (штриховая линия) in one view must be explained by a feature in
another view. A circle in one view is a cylinder, a sphere or a cone tip — check
which in the adjacent view before calling it a hole.

## 9. Open questions (вопросы к конструктору)

Anything missing, contradictory or illegible. These go into `open_questions`;
the validator refuses to generate code while any remain (exit 3).

1.
2.

## 10. Sign-off checklist

- [ ] Every number in the spec appears in sections 1–7 with its source view.
- [ ] Every value marked **A** is in the spec's `assumptions` with a `source`.
- [ ] Section 9 is empty, and `open_questions` is `[]`.
- [ ] `part_spec_validator.py` exit 0.
- [ ] The generated script builds one valid solid.
- [ ] `model_verifier.py` verdict PASS: V1 solid, V2 overall size, V3 holes, V4 mass (if the title block gives one), V5 threads.
- [ ] Each drawing view compared visually with the model (`stl_viewer.py`): main view, top view, side view, every section.
- [ ] Readout done by: ____________ date: ____________
