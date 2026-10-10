# Drawing Reading Guide — from a sheet to numbers, without guessing

> Lens: *every number in the part spec must come from text printed on the
> drawing, from a named standard applied to that text, or from an open question.*
>
> Covers Russian ЕСКД/ГОСТ drawings first and ISO / ASME second. Each claim that
> is not obvious has a confidence level: **high** = stated in the standard or
> checked against the tools; **moderate** = common practice, check against the
> standard if it matters; **low** = rule of thumb.

---

## 1. The one rule: read the text, never measure the picture

A drawing is a picture with numbers on it, and only the numbers count. Scale,
paper shrinkage, scanner DPI, phone perspective and "drawn not to scale" views
make any pixel measurement wrong by an unknown amount. ГОСТ 2.307 and ISO 129-1
both state that the dimension value printed on the drawing is the real size,
whatever the scale of the view (confidence: **high**).

Use pixels only for **topology**: which view a circle belongs to, whether a hole
goes through, which side a chamfer is on. Never use them for **values**.

If the drawing is a CAD file (DXF), you don't read it by eye. Run
`scripts/dxf_geometry_extractor.py drawing.dxf` and take hole diameters,
centres, arc radii, dimension values and notes from its output (§9).

## 2. Global axes and the origin the spec uses

The part spec (see `part_spec_format.md`) has one fixed frame:

- **X** to the right in the front view (главный вид).
- **Y** going back, away from the viewer of the front view.
- **Z** up.
- **Origin** for an `extrude` base: the centre of the base footprint, on the
  bottom face (the base profile is centred on (0, 0) and extruded from z = 0 to
  `height`).
- **Origin** for a `revolve` base: on the axis. Z is the axis, and z values come
  straight from `profile_rz`, so put z = 0 at the end face the drawing
  dimensions from.

Drawings dimension from edges and datums, not from the centre, so convert. A
hole "12 from the left edge" of a 120-wide plate is at x = −60 + 12 = **−48**. A
hole "12 from the front edge" of an 80-deep plate is at y = −40 + 12 = **−28**.
Write the arithmetic in the assumption's `source` or in your notes. A sign error
is the most common bad readout, and V3/V2 in `model_verifier.py` can't catch a
mirrored hole.

## 3. Projection systems and where each view goes

Two systems are in use. The 3D model is identical in both; only the position of
the views on the sheet changes.

| System | Standards | Symbol | Top view (вид сверху) | Left view (вид слева) |
|---|---|---|---|---|
| **First angle** | ЕСКД ГОСТ 2.305-2008; ISO 128 "method E" (European) | truncated cone, small circle on the right | **below** the front view | **right** of the front view |
| **Third angle** | ASME Y14.3; ISO 128 "method A" (American) | truncated cone, small circle on the left | **above** the front view | **left** of the front view |

Confidence: **high** for the placement rules. ЕСКД drawings normally carry no
projection symbol because first angle is the only system ГОСТ 2.305 allows. An
ASME drawing should show the symbol near the title block.

What each view shows does not depend on the system. A "view from the left" is
always what you see standing at −X. Read each view by its own axes:

| View (ЕСКД / ISO / ASME) | Seen from | Paper → (right) | Paper ↑ (up) | Spec face whose `(u, v)` you fill |
|---|---|---|---|---|
| Главный вид / front | −Y | +X | +Z | `<Y` → (X, Z) |
| Вид сверху / top | +Z | +X | +Y | `>Z` → (X, Y) |
| Вид снизу / bottom | −Z | +X | **−Y** | `<Z` → (X, Y) |
| Вид слева / left | −X | **−Y** | +Z | `<X` → (Y, Z) |
| Вид справа / right | +X | +Y | +Z | `>X` → (Y, Z) |
| Вид сзади / rear | +Y | **−X** | +Z | `>Y` → (X, Z) |

Confidence: **high** (follows from the projection geometry). The bold entries
are the traps. The spec always takes **global** coordinates. A hole dimensioned
"15 to the right of centre" in the **left** view has Y = **−15**, and a hole
"10 above centre" in the bottom view has Y = **−10**. The generated script then
converts global coordinates to each face's mirrored local axes itself (see
`cadquery_modeling_patterns.md` §3), so don't try to pre-mirror anything.

Auxiliary views (дополнительный вид, "Вид А" with an arrow, ISO "auxiliary
view") look along an inclined direction. Features on an inclined face cannot be
expressed in part-spec.v1 (faces are only ±X/±Y/±Z and the datums XY/XZ/YZ), so
they go into `open_questions`, or they become a documented hand-extension
(`cadquery_modeling_patterns.md` §10).

## 4. Dimension symbols

| Symbol / text | Meaning | Becomes in the spec |
|---|---|---|
| `Ø20` | diameter | `diameter: 20`; a circle profile, hole or boss |
| `R5` | radius | fillet `radius`, rect `corner_radius`, half of a slot `width` |
| `Сфера R10`, `SR10`, `SØ20` | spherical radius / diameter | not expressible as a feature, except as a `revolve` profile segment (approximate the arc with points) or an open question |
| `□20` | square 20×20 | `rect` profile, width = length = 20 |
| `M8`, `M8×1`, `M8-6H`, `M8×1.25-6g` | metric thread (ГОСТ 24705 / ISO 261) | a `thread` feature + a hole at the tap-drill Ø (§5) |
| `2×45°`, `C2` | chamfer 2 mm at 45° | `chamfer`, `size: 2` |
| `2×30°` | chamfer, leg 2 at 30° | `size` + `size2` (two legs, see §4.1) |
| `4 отв. Ø9`, `4×Ø9`, `4X Ø9` | 4 identical holes | one `hole` feature with 4 positions or a `pattern` |
| `Ø50` on a thin dash-dot circle, `PCD 50`, `B.C. Ø50` | pitch circle of a hole pattern | `pattern.type: "polar"`, `pcd: 50` |
| `6 отв. Ø6,6 равномерно` / `EQ SP` | equally spaced on the circle | polar, `span_deg` left at 360 |
| `⌴ Ø15 ↧ 9` | counterbore Ø15, depth 9 (ISO 129-1, ASME Y14.5) | `cbore: {diameter: 15, depth: 9}` |
| `⌵ Ø13 × 90°` | countersink Ø13, 90° included angle | `csk: {diameter: 13, angle_deg: 90}` |
| `↧ 12` | depth | hole/pocket `depth` |
| `▷ 1:5`, `Конусность 1:5` | taper | `revolve` profile points (a cone is a sloped segment) |
| `∠ 1:10`, `Уклон 1:10` | slope | a `polygon` profile, or an open question |
| `*` after a number / "Размер для справок" | reference dimension | use only as a cross-check, never as the source |
| `(45)` in brackets (ISO/ASME) | reference dimension | same |

Decimal separators: ЕСКД uses a **comma** (`Ø6,6`, `Масса 1,02`). JSON needs a
point (`6.6`, `1.02`).

### 4.1 Chamfers

- `C×45°` → `chamfer`, `size: C`.
- A chamfer given as leg × angle (`2×30°`) or as two legs: compute the second
  leg (`size2 = size × tan(angle)` when the angle is measured from the face the
  first leg lies on), write both, and record the arithmetic as an assumption.
  Which face gets which leg comes from OpenCascade's face order, not from the
  spec. Confirm it in the viewer (`cadquery_modeling_patterns.md` §5.4,
  confidence **moderate**).

### 4.2 Holes in ЕСКД notation

- ЕСКД writes the count in front of the diameter: `4 отв. Ø9`. ISO writes `4×Ø9`
  and ASME `4X Ø9`.
- ЕСКД practice usually shows a counterbore or countersink in a local section
  (местный разрез) or a section view and dimensions it there. ГОСТ 2.318
  allows simplified depiction of small holes, with the diameters and depths
  written on a leader line. Confidence: **moderate**; see the standard for the
  exact forms.
- Whatever the notation, extract four numbers: hole Ø, hole depth (or
  "through"), outer Ø, and outer depth or angle.

### 4.3 Threads (ГОСТ 24705 / ISO 261, tolerance class ГОСТ 16093 / ISO 965)

- ЕСКД omits the pitch for **coarse** threads (`M8` = M8×1.25) and writes it for
  fine threads (`M8×1`). `6H` (capital) = internal thread, `6g` (lowercase) =
  external thread, `LH` = left-hand. Confidence: **high**.
- The spec models a thread **cosmetically**: the hole at the tap-drill Ø (or a
  boss at the nominal major Ø), plus a `thread` feature that carries the
  designation verbatim into the model report (checked by V5).
- A blind tapped hole on an ЕСКД drawing carries two depths: the full-thread
  depth and the larger drill depth. The drill depth goes in the hole's `depth`.
  The thread depth goes in the `thread` feature's `length`.

Tap-drill diameters for metric coarse threads, as drill Ø ≈ d − P (ISO 2306 /
ГОСТ 19257 give the toleranced ranges; these are the common shop values;
confidence **high** for M3–M20 coarse):

| Thread | Pitch P | Tap drill Ø |
|---|---|---|
| M3 | 0.5 | 2.5 |
| M4 | 0.7 | 3.3 |
| M5 | 0.8 | 4.2 |
| M6 | 1.0 | 5.0 |
| M8 | 1.25 | 6.8 |
| M10 | 1.5 | 8.5 |
| M12 | 1.75 | 10.2 |
| M14 | 2.0 | 12.0 |
| M16 | 2.0 | 14.0 |
| M18 | 2.5 | 15.5 |
| M20 | 2.5 | 17.5 |

Fine pitch uses the same rule: M8×1 → 7.0, M10×1.25 → 8.8, M12×1.5 → 10.5,
M16×1.5 → 14.5, M20×1.5 → 18.5. A tap-drill Ø taken from this table is an
**assumption** (`source: "tap drill for M8 per ISO 2306"`), not an open question:
it follows mechanically from the callout.

Clearance holes are different. When the drawing writes only "под болт M8"
("for an M8 bolt") with no Ø, the choice between fine, medium and coarse series
(ISO 273 / ГОСТ 11284: 8.4 / 9 / 10 for M8) is a design decision. Ask the user.

## 5. Patterns: PCD, grids and rows

- **PCD / "Ø50 окр."**: the thin dash-dot circle carrying the holes. Read the
  start angle from the view. The spec's `start_angle_deg` is measured
  counter-clockwise from **+X**, in the face's global (u, v) axes, so in the left
  view (paper → = −Y) the visual direction flips (§3).
- **"6 отв. равномерно"**: `count: 6`, full circle. For a partial arc
  (`3 отв. … 90°`), set `span_deg: 90`. Holes then sit at both ends of the arc
  (span / (count − 1) spacing). With `span_deg` = 360 or more the spacing is
  span / count.
- **Grid**: the spec's grid is **centred** on `center`. A drawing that gives
  "first hole 12 from the edge, pitch 96" must be converted to a centred grid,
  or written as explicit `positions`.
- **Row**: `linear` with `start` (the first hole, global u, v), `pitch` and
  `direction_deg`.

## 6. Sections, cut-outs and detail views

| ЕСКД (ГОСТ 2.305) | ISO / ASME | What it tells you |
|---|---|---|
| Разрез А–А | Section A–A | cut-plane contents **and** what is behind the plane. This is where internal depths, counterbores, grooves and bores are dimensioned |
| Сечение Б–Б | Removed section | cut-plane contents only (shaft keyways, rib profiles) |
| Местный разрез | Broken-out / local section | one hole's depth or counterbore, in the middle of a view |
| Половина вида + половина разреза | Half section | symmetric part: outside on one half, inside on the other, split by the axis line |
| Выносной элемент `А (5:1)` | Detail view `A (5:1)` | an enlarged area with its own scale. The dimension text is still real size |
| Разрыв | Break | a long part drawn shorter. The length dimension is real |

Conventions that cause misreads (confidence: **high**, ГОСТ 2.305 / ISO 128):

- Ribs, spokes, thin walls, and shafts/bolts/pins cut **lengthwise** are **not
  hatched**. An unhatched rib in a section is solid, not a gap.
- Hidden lines (dashed, ГОСТ 2.303) show internal features in a plain view. Take
  their dimensions from the section that shows the feature, if one exists.
- A cutting-plane line with letters (А–А) tells you which way the section looks.
  Map the section's axes with the table in §3, as if it were a view from that
  direction.

## 7. General notes (технические требования) and the title block

General notes above the title block fill in what the dimensions leave out.
Each one used becomes an **assumption** with the note quoted as `source`:

| Note | Spec |
|---|---|
| "Неуказанные радиусы R1" / "Unspecified radii R1" | `fillet` on the edges the views show as rounded; assumption cites the note |
| "Неуказанные фаски 1×45°" | `chamfer` with `size: 1` on the edges drawn with chamfers |
| "Острые кромки притупить" / "Break sharp edges" | **not modelled** (≤ 0.5 mm, cosmetic); record as an assumption with value "not modelled" |
| "Неуказанные предельные отклонения … H14, h14, ±IT14/2" or "ГОСТ 30893.1-m" / "ISO 2768-m" | no geometry; model at nominal |
| "* Размеры для справок" | reference dimensions; cross-check only |
| "Покрытие …", "HRC …", "Термообработка …" | no geometry |

The **title block** (основная надпись, ГОСТ 2.104) gives the spec's metadata.
In the standard form, field 1 is the name, field 2 the document number,
field 3 the material, field 5 the mass and field 6 the scale. Confidence:
**moderate** on the field numbers; the fields are labelled on the form either way.

- **Material** (`Сталь 45 ГОСТ 1050-2013`, `Д16Т ГОСТ 4784-2019`) → `material`,
  verbatim. `model_verifier.py` looks up density by substring. `сталь`, `д16т`,
  `латунь`, `12х18н10т` and English names are in its `DENSITY` table. For a grade
  that is not in the table (АМг6, 40Х13, …) set `density_g_cm3` explicitly, or
  V4 is skipped.
- **Mass** (`Масса 1,02` or a bare number in kg) → `mass_kg: 1.02`. It is the
  strongest whole-part check there is: V4 fails beyond ±3 % (title-block masses
  are rounded).
- **Scale** (`1:2`, `2:1`) → `drawing.scale`. Never use it to compute sizes:
  dimensions are already real size.

## 8. Tolerances, fits and roughness

- **Fits and deviations** (`Ø20H7`, `Ø40 −0,2`, `50±0,1`, `Ø20 +0,021`): model
  at **nominal** (20, 40, 50). The model is the nominal geometry, and tolerances
  belong to the drawing and the inspection plan (confidence: **high** for CAD
  practice). If the user prints or machines straight from the model and needs
  mid-tolerance size, that is their request to make. Don't silently shift
  numbers.
- **Geometric tolerances** (⊥, ∥, ⌖, frames per ГОСТ 2.308 / ISO 1101 /
  ASME Y14.5): no geometry. The datum letters (А, Б / A, B) are still useful:
  they tell you which faces the dimensions are taken from.
- **Surface roughness** (√Ra 3.2, ГОСТ 2.309 / ISO 21920-1): ignored for geometry.
- **Inches**: the spec requires `"units": "mm"`. Multiply by 25.4 before writing.

## 9. Reading from photos, scans, PDFs and DXF

| Input | How to read it |
|---|---|
| Vector PDF | Zoom in, read the text; text in a vector PDF is crisp at any zoom |
| Scan / raster PDF | Read the text; if a digit is unclear (`3` vs `8`, `6` vs `б`), cross-check with a dimension chain (§10) or ask |
| Phone photo | Perspective, curl and glare distort shapes. Read only the text. Ignore any scale bar, since it is distorted with the rest. Ask for a straight-on, flat, well-lit retake of any area you can't read |
| Hand sketch | Treat every unlabelled size as an open question |
| ASCII DXF | `dxf_geometry_extractor.py`: hole table by diameter with every centre, arc radii, every DIMENSION's measured value and text override, notes, and `spec_hints` for mass/material/thread notes. Binary DXF or DWG → exit 2; re-save as ASCII DXF |

DXF notes (confidence: **moderate**):

- All views share one sheet coordinate space. Hole centres from the extractor
  are **sheet** coordinates: subtract the view's datum (for example the centre of
  the part outline in that view) to get spec coordinates. Map axes per §3.
- A view drawn at a scale other than 1:1 has its geometry scaled on the sheet,
  while the DIMENSION text shows real values. Prefer `dimensions[].text` /
  `measured` over raw circle diameters, and divide sheet geometry by the view
  scale.
- A DIMENSION's text override (`<>` replaced by text) wins over the measured
  value. The drafter typed it on purpose. A mismatch between the two is worth
  an open question.

## 10. Cross-checks before you write the spec

1. **Chains add up.** The sum of chained dimensions equals the overall
   dimension. If not, one of them is misread: re-read before asking.
2. **Overall sizes exist.** Overall X/Y/Z go into `expect.bbox_mm` (V2). For a
   turned part, X = Y = the largest diameter.
3. **Counts match.** "4 отв." and four circles drawn. If they disagree, ask.
4. **Every feature appears in two views** (or in one view plus a section, or a
   Ø/□ symbol that implies the second). A circle seen in only one view can be a
   hole, a boss or a sphere.
5. **Mass is plausible.** Volume × density ≈ title-block mass. V4 checks this
   after the build, but a rough mental estimate catches a factor-of-10 misread
   early.

## 11. The never-guess checklist

**Write it as an `open_questions` entry** (blocks generation, validator exit 3)
when:

- a size or position a feature needs is not printed and cannot be derived
  arithmetically from printed dimensions;
- printed dimensions contradict each other (a chain does not add up, the count
  does not match the drawn holes, the DXF text override disagrees with the
  measured value);
- a digit or symbol is illegible;
- you can't tell whether a hole is through or blind, or which face it starts
  from;
- a hole is "под болт M8" with no Ø (clearance series is a design choice);
- a feature is on an inclined face, a sphere, a draft, a sweep or a knurl that
  part-spec.v1 cannot express, and the user has not said how to handle it;
- the material is missing but the user wants V4 (mass) checked.

**Write it as an `assumptions` entry** (`what`, `value`, `source`) when:

- the value comes from a general note ("Неуказанные радиусы R1");
- it follows mechanically from a standard applied to a printed callout
  (tap-drill Ø from M8; coarse pitch from a bare M8);
- it is arithmetic on printed dimensions (edge distance → centred coordinate;
  overall − chain sum);
- it is symmetry about an axis line drawn on the sheet;
- it is a modelling convention: nominal size for a toleranced dimension,
  cosmetic thread, "break sharp edges" not modelled.

**Never:**

- measure a dimension from pixels, a ruler on the screen, or the scale;
- "round to a nice number" a value you could not read;
- fill a gap with a "typical" size without asking;
- leave an assumption without a `source`: the next reader must be able to
  find the note or the standard it came from.

---

## Sources

1. **ГОСТ 2.305-2008** ЕСКД. Изображения — виды, разрезы, сечения. First-angle
   view placement, sections, conventions for unhatched ribs and shafts.
2. **ГОСТ 2.307-2011** ЕСКД. Нанесение размеров и предельных отклонений.
   Dimension text is real size, symbols, chamfer notation, hole counts.
3. **ГОСТ 2.318-81** ЕСКД. Правила упрощенного нанесения размеров отверстий —
   simplified notation for holes. See the standard for the exact forms.
4. **ГОСТ 2.104-2006** ЕСКД. Основные надписи — the title block fields
   (material, mass, scale).
5. **ISO 128** (Technical product documentation — general principles of
   representation; projection methods A and E) and **ISO 129-1:2018**
   (indication of dimensions and tolerances, incl. ⌴ ⌵ ↧ symbols).
6. **ASME Y14.3** (orthographic and pictorial views, third-angle) and
   **ASME Y14.5-2018** (dimensioning and tolerancing; counterbore, countersink,
   depth symbols; "4X" notation).
7. **ISO 261 / ГОСТ 24705-2004** (metric thread basic dimensions),
   **ISO 965 / ГОСТ 16093** (tolerance classes 6H/6g), **ISO 2306 /
   ГОСТ 19257-73** (tap-drill diameters), **ISO 273 / ГОСТ 11284-75**
   (clearance holes).
8. Oberg, E. et al., **Machinery's Handbook** (Industrial Press, 31st ed.) —
   tap-drill and clearance tables, chamfer and thread conventions.

## Related tools in this skill

- `dxf_geometry_extractor.py` — §9
- `part_spec_validator.py` — §11 (exit 3 on open questions)
- `model_verifier.py` — §7 (V4 mass, `DENSITY`), §10 (V2 bbox, V3 hole counts)
- `stl_viewer.py` — §3 (view buttons follow the same X right, Y back, Z up frame)
