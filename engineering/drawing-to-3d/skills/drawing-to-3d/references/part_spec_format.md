# Part Spec Format — `part-spec.v1` reference

> Lens: *the spec is the drawing readout in machine form. Everything the model
> contains must be in it, and everything in it must come from the drawing.*
>
> This page follows `scripts/part_spec_validator.py` and
> `scripts/cadquery_codegen.py` exactly. When a rule here and the code disagree,
> the code wins: report the mismatch. Confidence is **high** throughout unless
> marked, because every rule was checked against the validator and a CadQuery 2.8
> build.

---

## 1. Pipeline and exit codes

```
part.spec.json ──► part_spec_validator.py ──► cadquery_codegen.py ──► <part>_cad.py ──► model_verifier.py
                   0 valid                     0 written                0 one valid      0 PASS
                   1 warnings (--strict)       2 spec invalid           solid written    1 FAIL
                   2 errors                    3 open questions         1 otherwise      64 usage
                   3 open questions            64 usage
                   64 usage / unreadable
```

- **Validator.** Exit codes are tested in this order: errors (2), then open
  questions (3), then warnings with `--strict` (1). Without `--strict`, warnings
  still exit 0. `--output json` prints `verdict` (`VALID`, `WARNINGS`,
  `INVALID`, `OPEN-QUESTIONS`), `exit`, `errors`, `warnings`,
  `open_questions`, `features` and `hole_instances`. `--print-sample` prints the
  built-in flange-plate spec and `--sample` validates it.
- **Unreadable JSON** (bad syntax, not UTF-8, missing file) exits 64. JSON
  `NaN`/`Infinity` are loaded as the strings `"NaN"`/`"Infinity"`, so they fail
  the number checks (exit 2) instead of reaching the generated script.
- **Report** (`<part>.report.json`, written by the generated script) carries
  `valid`, `solids`, `bbox_mm`, `bbox_min_mm`, `volume_mm3`, `area_mm2`,
  `faces`, `edges`, `cylinder_diameters_mm`, `cylinders` (per cylindrical face:
  `diameter`, `axis_point`, `axis_dir`) and `threads`. The verifier uses
  `cylinders` to find each spec hole **at its drawn position**.
- **Codegen** runs the same `validate()` and refuses on errors (2) or open
  questions (3). Warnings are printed but do not block. `--out -` writes to
  stdout, and so does `--sample` without `--out`. The default output is
  `<part>_cad.py` next to the spec.

## 2. Coordinate frame

- Global **X** right, **Y** back, **Z** up (front view looks along +Y).
- `extrude` base: the profile is centred on the origin in XY and extruded
  from **z = 0 to `height`**. `revolve` base: revolved about the **Z axis**; z
  comes from `profile_rz`.
- **Every** coordinate in the spec is a global coordinate, as dimensioned on the
  drawing's view of that face. The generated script converts it to each face
  workplane's local (sometimes mirrored) axes at run time with `L()`/`A()`.
  Never pre-mirror anything.

| `face` | Plane | Coordinates `(u, v)` mean | Cut / extrude direction |
|---|---|---|---|
| `>Z` | top face (max Z) | (X, Y) | into the part = −Z (boss: +Z) |
| `<Z` | bottom face (min Z) | (X, Y) | into the part = +Z (boss: −Z) |
| `>X` | right face (max X) | (Y, Z) | −X |
| `<X` | left face (min X) | (Y, Z) | +X |
| `>Y` | back face (max Y) | (X, Z) | −Y |
| `<Y` | front face (min Y), the one the front view shows | (X, Z) | +Y |
| `XY` | datum plane z = 0 | (X, Y) | both ways along Z, `"through"` only |
| `XZ` | datum plane y = 0 | (X, Z) | both ways along Y, `"through"` only |
| `YZ` | datum plane x = 0 | (Y, Z) | both ways along X, `"through"` only |

A face is chosen **at the moment the feature runs** (CadQuery `faces(">Z")`
picks the face whose centre is furthest along +Z). After a `boss` on `>Z`, a
later `>Z` feature lands on the **boss top**, not the plate. Datum planes always
pass through the origin and are the only option on a curved part (a hole across
a shaft); see `cadquery_modeling_patterns.md` §6.

## 3. Top-level keys

| Key | Type | Required | Rule / default | Used by |
|---|---|---|---|---|
| `schema` | string | yes | exactly `"part-spec.v1"` | validator |
| `part` | string | yes | `[A-Za-z0-9_-]{1,64}`; becomes the file name `<part>.step` / `_cad.py` | all |
| `units` | string | yes | exactly `"mm"`; convert inch drawings first (× 25.4) | validator |
| `base` | object | yes | §4 | codegen |
| `features` | list | no (default `[]`) | §5, built **in list order** | codegen |
| `material` | string | no | verbatim from the title block; `model_verifier.py` matches it against its `DENSITY` table by substring | V4 |
| `density_g_cm3` | number > 0 | no | overrides the `DENSITY` lookup; set it for grades not in the table | V4 |
| `mass_kg` | number > 0 or `null` | no | title-block mass. `null`/absent → V4 skipped | V4 (±3 %) |
| `drawing` | object | no | free-form: `title`, `number`, `scale` are used in the script header; extra keys (the assets add `projection`, `views`, `file`) are allowed and not checked | codegen header |
| `expect` | object | no, but **warns** if `bbox_mm` is missing | `bbox_mm`: `[X, Y, Z]`, all > 0; `tolerance_mm` > 0, default **0.05** in the verifier | V2 |
| `assumptions` | list | no | each `{"what": …, "value": …}` required, `id` and `source` recommended | humans |
| `open_questions` | list of strings (one string is also accepted) | no | **any non-empty entry blocks generation (exit 3)**; non-string entries are reported as JSON text | validator, codegen |

The whole spec is embedded in the generated script as `SPEC`, and its
`thread` features are copied into `<part>.report.json`.

## 4. Base

### 4.1 `extrude` (prismatic parts)

```json
"base": {"type": "extrude", "profile": {"shape": "rect", "width": 80, "length": 50}, "height": 8}
```

`profile`: any profile shape (§6) except that `at` is always (0, 0). `height` > 0.
The generated code is `cq.Workplane("XY")<profile>.extrude(height)`.

### 4.2 `revolve` (turned parts: shafts, bushings, flanged hubs)

```json
"base": {"type": "revolve", "profile_rz": [[0, 0], [12, 0], [12, 40], [9, 40], [9, 90], [0, 90]]}
```

`profile_rz`: ≥ 3 `[r, z]` points, a closed half-section outline
(r = radius, z = axial). Rules: every r ≥ 0 (it may **touch** the axis, never
cross it), at least one r > 0, and the outline must be a simple polygon: no
duplicate points, no zero area, no self-crossing. Walk the outline once
around. The generated code revolves the polyline on the `XZ` workplane through
360° about global Z.

## 5. Features

Common to all: `id` (non-empty string, unique; use the drawing callout such
as `"H1"` or `"Ø22"`) and `type`, one of `hole`, `pocket`, `boss`, `slot`,
`fillet`, `chamfer`, `revolve_cut`, `shell`, `thread`. Unknown keys are ignored.

### 5.1 `hole`

| Field | Required | Rule |
|---|---|---|
| `face` | yes | any of the 6 faces or 3 datums |
| `diameter` | yes | > 0. For a tapped hole, the **tap-drill** Ø (M8 → 6.8) |
| `depth` | yes | > 0, or `"through"`. Must be `"through"` on a datum |
| `positions` | one of these two | list of `[u, v]` |
| `pattern` | one of these two | §7. Both may be given: the positions come first, then the pattern points |
| `cbore` | no | `{"diameter": > hole Ø, "depth": > 0}`; depth < hole depth; on a through hole on ±Z it must be < the part height |
| `csk` | no | `{"diameter": > hole Ø, "angle_deg": 30..150, default 90}` |

`cbore` and `csk` are mutually exclusive and are not allowed on a datum. Code
emitted: `hole(d[, depth])`, `cboreHole(d, cb_d, cb_depth[, depth])` or
`cskHole(d, csk_d, angle[, depth])` on the face workplane, or a both-ways
`circle().extrude(_big(r), both=True)` cut on a datum.

### 5.2 `pocket` and `boss`

| Field | pocket | boss |
|---|---|---|
| `face` | 6 faces or 3 datums | 6 faces only |
| `profile` | yes (§6) | yes (§6) |
| `at` | `[u, v]` centre, default `[0, 0]` | same |
| `depth` / `height` | `depth` > 0 or `"through"` (datum → `"through"` only) | `height` > 0 |

Code: pocket = `cutBlind(-depth)` or `cutThruAll()`; boss = `extrude(height)`
outward from the face. A boss must touch the body, or V1 reports 2 solids.

### 5.3 `slot` (a stadium cut)

`face` (6 faces or 3 datums), `length` (**overall, end to end**, > `width`),
`width`, `depth` (> 0 or `"through"`), optional `at` (default `[0, 0]`) and
`angle_deg` (counter-clockwise from the face's first axis u, default 0). Same
code as a pocket with a `slot` profile (`slot2D(length, width, angle)`; a
CadQuery 2.8 check confirmed `slot2D(30, 8)` is 30 × 8 overall).

### 5.4 `fillet` and `chamfer`

| Field | fillet | chamfer |
|---|---|---|
| `radius` / `size` | `radius` > 0 | `size` > 0 (a 45° leg) |
| `size2` | — | optional > 0, the second leg of an unequal chamfer |
| `edges` | yes | yes |

`edges` is either:

- a **CadQuery selector string** (`">Z"`, `"|Z"`, `"|Z and >Y"`, `"not(<Z)"`,
  `"%CIRCLE"`), checked only for allowed characters (max 80), and passed through
  as `r.edges(sel)`; or
- `{"near": [x, y, z]}` or `{"near": [[x, y, z], ...]}`: for **each** point,
  the generated `near_edges()` picks the real edge at the smallest **true
  distance** (seam lines of cylinders and revolved faces excluded) and fails
  if none lies within **0.5 mm**. Pick a point that lies on the edge, in global
  coordinates.

Selector strings are broad: `">Z"` takes **every** edge in the top plane,
hole and slot rims included. Use `near` when the drawing means "the outer
outline". Check the selector cheat-sheet in `cadquery_modeling_patterns.md` §4.

### 5.5 `revolve_cut` (grooves, undercuts, internal bores of turned parts)

`profile_rz`: the same rules as the revolve base (§4.2). Revolved 360° about
global Z and subtracted.

### 5.6 `shell`

`face`: one of the 6 faces (that face is removed, so the part opens there).
`thickness`: non-zero number, **negative = wall inward** (outer size kept).
Code: `r.faces(face).shell(thickness)`.

### 5.7 `thread` (cosmetic)

| Field | Required | Rule |
|---|---|---|
| `on` | yes | `id` of an existing feature; warning unless it is a `hole` or `boss` |
| `designation` | yes | verbatim from the drawing, e.g. `"M8x1.25-6H"`, `"M20x1.5-6g"` |
| `length` | no | > 0, the thread length |

No geometry is emitted (a comment and `pass`). The hole stays at its drill Ø,
the callout goes to `report.json → threads`, and V5 checks that it is there.

## 6. Profile shapes

| `shape` | Fields | Meaning |
|---|---|---|
| `rect` | `width` (along u/X) > 0, `length` (along v/Y) > 0, `corner_radius` ≥ 0 and < half the shorter side, `angle_deg` (number) | centred on `at`; rotated CCW by `angle_deg` |
| `circle` | `diameter` > 0 | |
| `slot` | `length` (overall) > `width` > 0, `angle_deg` | stadium |
| `polygon` | `points`: ≥ 3 `[u, v]`, simple (no duplicates, non-zero area, no self-crossing) | points are **offsets from `at`**; closed automatically |
| `regular_polygon` | `sides` integer 3..360, plus `across_corners` > 0 **or** `across_flats` > 0 | AF is converted to AC = AF / cos(π/sides). Both given and disagreeing by > 0.1 % is an error. No `angle_deg`: the first corner lies on the face's **+u** axis from `at`. On the base it is CadQuery `.polygon(sides, AC)`; on a face the vertices are emitted as a polyline in global coordinates through `L()`, so mirrored faces never flip it |

Hex for a nut: `{"shape": "regular_polygon", "sides": 6, "across_flats": 13}`
(M8 nut, S = 13).

## 7. Patterns (holes only)

All coordinates are global `(u, v)` on the hole's face. Angles are measured
CCW from +u.

| `type` | Fields (default) | Expansion |
|---|---|---|
| `polar` | `count` int ≥ 1 (required), `pcd` > 0 (required), `start_angle_deg` (0), `span_deg` in (0, 360] (360), `center` [u, v] ([0, 0]) | span = 360: `count` holes at 360/count spacing. span < 360: holes at **both ends** of the arc, spacing span/(count − 1) |
| `grid` | `nx` (1), `ny` (1), `pitch_x` (> 0, required if nx > 1), `pitch_y` (> 0, required if ny > 1), `center` ([0, 0]) | **centred** on `center`: u = cx + (i − (nx − 1)/2)·pitch_x |
| `linear` | `count` int ≥ 1 (required), `pitch` (> 0, required if count > 1), `direction_deg` (0), `start` [u, v] ([0, 0]) | first hole at `start`, then every `pitch` along `direction_deg` |

Limits: at most 10 000 instances per pattern (`MAX_INSTANCES`), and the same
cap applies to `positions`. Repeated points (positions overlapping a pattern)
give a warning.

Example: 4 holes on PCD 30 at 45°, 135°, 225°, 315°:
`{"type": "polar", "count": 4, "pcd": 30, "start_angle_deg": 45}`.

## 8. What the validator reports

**Errors (exit 2):** every message is `"<where>: <message>"`.

| Category | Examples of the message |
|---|---|
| top level | `schema: must be 'part-spec.v1'`; `part: must be a file-safe name…`; `units: must be "mm"…`; `features: must be a list`; `drawing: must be an object…` |
| base | `base.type must be 'extrude' … or 'revolve'`; `profile_rz must be a list of >= 3 [r, z] pairs`; `profile_rz radii must be >= 0 …`; `profile_rz has zero radius everywhere`; `profile_rz contains duplicate vertices / has zero area / edges i-j and k-l cross` |
| identity | `every feature needs an 'id'`; `duplicate feature id`; `type must be one of [...]`; `'face' must be one of [...]` |
| numbers | `'<key>' is required`; `'<key>' must be a positive number, got …`; `'depth' must be a positive number or "through"`; `'angle_deg' must be a number` |
| profiles | `profile.shape must be one of [...]`; `corner_radius … must be < half the smaller side`; `slot length (overall, end to end) must exceed its width`; `regular_polygon.sides must be an integer 3..360`; `across_flats … and across_corners … disagree`; `polygon.points must be a list of >= 3 [x, y] pairs`; `polygon.points has zero area / … cross` |
| holes | `hole needs 'positions' … or a 'pattern'`; `'positions' must be a list of [x, y] pairs`; `pattern.type must be polar \| grid \| linear`; `pattern.<key> must be an integer >= 1 / a positive number`; `pattern.span_deg must be in (0, 360]`; `… more than 10000 instances`; `a hole has either 'cbore' or 'csk', not both`; `counterbore Ø… must exceed hole Ø…`; `counterbore depth must be less than hole depth`; `counterbore depth … reaches the full part height`; `csk.angle_deg must be between 30 and 150`; `countersink Ø… must exceed hole Ø…` |
| datums | `on datum plane … depth must be "through"`; `counterbore/countersink needs a real face` |
| edges | `edge selector … is not a CadQuery string selector`; `edges.near must be [x, y, z] or a list of [x, y, z] points`; `'edges' must be a selector string … or {"near": …}` |
| shell / thread | `shell.thickness must be a non-zero number`; `thread needs a 'designation'…`; `thread needs 'on'…`; `thread refers to unknown feature` |
| acceptance | `expect.bbox_mm: must be [X, Y, Z] overall sizes in mm`; `expect.tolerance_mm: must be a positive number`; `mass_kg: must be a positive number … or null`; `density_g_cm3: must be a positive number`; `assumptions[i]: each assumption needs 'what' and 'value'` |

**Warnings (exit 0, or 1 with `--strict`):**

- `hole at (u, v) breaks out of the base footprint / Ø outline`,
  `is outside the side face`, `is outside the part height`. These are checked
  against the **base envelope only**: a hole in a boss (the bracket's upright)
  warns even when it is correct. Read the warning, confirm it against the
  drawing, and move on.
- `blind depth … exceeds part height …; use "through"`.
- `fillet R… is >= half the part height …; OCC will likely fail` (only for
  `<…Z`/`>…Z` selector strings; conservative, since R = h/2 on a plain box
  edge does build).
- `thread is on '…', a <type>; threads are cut in a hole or on a boss`.
- `… hole position(s) repeated`.
- `<id> comes after a fillet/chamfer; edge finishing normally goes last`.
- `no expect.bbox_mm: the verifier will have no overall-dimension target`.

**Open questions (exit 3):** printed under "Open questions — answer these
before any code is generated". Generation stays blocked until the list is
empty; deleting a question without answering it defeats the gate. See
`drawing_reading_guide.md` §11 for what belongs there.

## 9. Complete annotated example — angle bracket (`assets/bracket.spec.json`)

Aluminium angle bracket: base plate 80 × 50 × 8, upright 80 × 8 × 52 along
the −Y edge (the side the front view looks at), two countersunk holes, a slot, a pocket underneath, two Ø9 holes
and a Ø20 spigot with a Ø10 bore on the upright's front face, and corner and
bend fillets. It passes validation (with the expected warnings, see below),
builds to one valid solid, and passes V1–V5.

```json
{
  "schema": "part-spec.v1",
  "part": "bracket",
  "units": "mm",
  "material": "Д16Т ГОСТ 4784-2019",
  "mass_kg": 0.17,
  "drawing": {
    "title": "Кронштейн угловой (Angle bracket)",
    "number": "DEMO-003",
    "scale": "1:1",
    "projection": "first angle (ESKD / ISO E)",
    "views": {
      "top view (вид сверху)": "global X right, Y up; base plate 80x50, csk holes, slot, hidden pocket",
      "front view (главный вид), looking along +Y": "global X right, Z up; upright 60 high, holes Ø9, spigot Ø20 with Ø10 bore",
      "left view (вид слева)": "global Y, Z; plate 8 thick, upright 8 thick, inner bend R4, spigot 4 proud"
    }
  },
  "base": {
    "type": "extrude",
    "profile": {"shape": "rect", "width": 80, "length": 50},
    "height": 8
  },
  "features": [
    {"id": "P1", "type": "pocket", "face": "<Z", "at": [0, 6],
     "profile": {"shape": "rect", "width": 40, "length": 30, "corner_radius": 3}, "depth": 2},
    {"id": "S1", "type": "slot", "face": ">Z", "at": [0, 12], "length": 24, "width": 9,
     "angle_deg": 0, "depth": "through"},
    {"id": "H1", "type": "hole", "face": ">Z", "diameter": 6.6, "depth": "through",
     "positions": [[-28, 12], [28, 12]], "csk": {"diameter": 13, "angle_deg": 90}},
    {"id": "B1", "type": "boss", "face": ">Z", "at": [0, -21],
     "profile": {"shape": "rect", "width": 80, "length": 8}, "height": 52},
    {"id": "H2", "type": "hole", "face": "<Y", "diameter": 9, "depth": "through",
     "positions": [[-28, 40], [28, 40]]},
    {"id": "B2", "type": "boss", "face": "<Y", "at": [0, 40],
     "profile": {"shape": "circle", "diameter": 20}, "height": 4},
    {"id": "H3", "type": "hole", "face": "<Y", "diameter": 10, "depth": "through",
     "positions": [[0, 40]]},
    {"id": "F1", "type": "fillet", "radius": 6, "edges": "|Z and >Y"},
    {"id": "F2", "type": "fillet", "radius": 4, "edges": {"near": [0, -17, 8]}}
  ],
  "expect": {"bbox_mm": [80, 54, 60], "tolerance_mm": 0.05},
  "assumptions": [
    {"id": "A1", "what": "Origin", "value": "centre of the base plate in plan, z=0 on the underside, upright at the -Y edge",
     "source": "symmetric dimensioning about the vertical centre line in the front and top views"},
    {"id": "A2", "what": "Upright modelled as a boss on the plate top face", "value": "80 x 8 at y=-21, 52 high (overall height 60)",
     "source": "overall height 60 and plate thickness 8 in the left view; one solid, machined from plate"},
    {"id": "A3", "what": "Countersink for M6 flat-head screws", "value": "Ø6.6 through, csk Ø13 x 90°",
     "source": "callout '2 отв. Ø6,6, зенковка Ø13x90°' in the top view"},
    {"id": "A4", "what": "Inner bend fillet", "value": "R4",
     "source": "callout 'R4' in the left view"},
    {"id": "A5", "what": "Front plan corners", "value": "R6",
     "source": "callout '2 скругл. R6' in the top view"},
    {"id": "A6", "what": "Title-block mass", "value": "0.17 kg",
     "source": "основная надпись; checked against volume x 2.78 g/cm³"}
  ],
  "open_questions": []
}
```

How to read each line against the drawing:

| Line | Why it is written this way |
|---|---|
| `material` + `mass_kg` | Title block, verbatim. `д16т` is in the verifier's `DENSITY` table (2.78 g/cm³), so V4 runs: 0.17 kg ± 3 % |
| `drawing.views` | Free-form notes of which view gave which axes. Not checked, but they make the readout auditable |
| `base` | The plate is the largest prism: 80 (X) × 50 (Y) × 8 (Z). No `corner_radius`: only the two corners on the +Y edge (away from the upright) are R6, so they come from fillet F1 |
| `P1` on `<Z` at `[0, 6]` | The hidden pocket underneath. Global Y = +6 even though `<Z` mirrors Y locally; the script converts it |
| `S1` `"through"` | The slot is 24 overall (not centre-to-centre) × 9 |
| `H1` + `csk` | "2 отв. Ø6,6, зенковка Ø13×90°" → explicit `positions`, not a pattern, because the drawing dimensions each from the centre line |
| `B1` boss on `>Z` | The upright: an 80 × 8 rectangle at y = −21 (−25 + 4), raised 52 so the total is 60. The feature order matters: from now on `>Z` would mean the upright top |
| `H2`, `B2`, `H3` on `<Y` | Front-view features: `(u, v)` = global (X, Z). Z = 40 is measured from the plate underside. `<Y` is the upright's front face, the most negative Y face. The B2 spigot is extruded 4 toward −Y, so the overall Y becomes 54 |
| `F1` `"|Z and >Y"` | Vertical edges at max Y = the two plate corners on the +Y edge, away from the upright (R6; "2 скругл. R6" in the top view) |
| `F2` `near [0, -17, 8]` | The inner bend: the edge where the upright's inner face (y = −17) meets the plate top (z = 8). One point selects exactly that edge |
| `expect.bbox_mm` | [80, 54, 60] from the overall dimensions (54 = 50 + the 4 mm spigot) |
| `assumptions` | Each with `source`: the callout or note it was read from |

Expected validator output: `VALID`, with one warning per H2 hole ("is outside
the part height (z 0..8)"). That is the base-envelope limitation from §8, and
correct here because the holes are in the upright.

### Revolve, datum cross hole and thread (`assets/stepped_shaft.spec.json`)

```json
"base": {"type": "revolve", "profile_rz": [[0, 0], [12, 0], [12, 40], [9, 40], [9, 90], [0, 90]]},
"features": [
  {"id": "G1", "type": "revolve_cut", "profile_rz": [[8, 60], [10, 60], [10, 63], [8, 63]]},
  {"id": "H1", "type": "hole", "face": ">Z", "diameter": 6.8, "depth": 16, "positions": [[0, 0]]},
  {"id": "T1", "type": "thread", "on": "H1", "designation": "M8x1.25-6H", "length": 12},
  {"id": "H2", "type": "hole", "face": "YZ", "diameter": 4, "depth": "through", "positions": [[0, 20]]},
  {"id": "C1", "type": "chamfer", "size": 1, "edges": {"near": [[0, 12, 0], [0, -9, 90], [-12, 0, 40]]}},
  {"id": "F1", "type": "fillet", "radius": 0.6, "edges": {"near": [0, 9, 40]}}
]
```

- `G1`: a 3-wide groove down to Ø16 between z = 60 and 63.
- `H1`: the M8 tapped hole in the Ø18 end face, drilled Ø6.8 ↧ 16, thread ↧ 12
  carried by T1.
- `H2`: a cross hole along X. A hole on `">X"` of a cylinder fails in CadQuery,
  so it goes on datum `YZ` at (Y = 0, Z = 20), through both ways.
- `C1`/`F1`: the `near` points use Y = ±12 / ±9 rather than +X. The revolve
  seam lies on the +X side; seams are excluded anyway, but a point away from
  them avoids ambiguity.

---

## Sources

1. `scripts/part_spec_validator.py` and `scripts/cadquery_codegen.py` in this
   skill: the schema is whatever these accept and emit (primary source).
2. CadQuery documentation, *API reference* and *Workplane* class:
   `hole`, `cboreHole`, `cskHole`, `slot2D`, `polygon`, `cutBlind`,
   `cutThruAll`, `shell`, `fillet`, `chamfer`.
   <https://cadquery.readthedocs.io/en/latest/classreference.html>
3. CadQuery documentation, *String Selectors Reference*.
   <https://cadquery.readthedocs.io/en/latest/selectors.html>
4. CadQuery documentation, *Workplanes* (face workplanes, `centerOption`,
   local coordinates). <https://cadquery.readthedocs.io/en/latest/workplane.html>
5. ГОСТ 2.307-2011 ЕСКД (dimensioning) and ISO 129-1:2018: the source of
   the values a spec carries (Ø, R, counts, chamfers).
6. ISO 261 / ГОСТ 24705-2004 (metric thread designations) and ISO 2306 /
   ГОСТ 19257 (tap-drill sizes), for `thread.designation` and the drill
   `diameter`.
7. ISO 10303-214 (STEP AP214), the format the generated script writes
   (`FILE_SCHEMA('AUTOMOTIVE_DESIGN …')` in CadQuery 2.8 output). Units are
   carried in the file, which is why the spec insists on mm.

## Related files

- `references/drawing_reading_guide.md`: how to get the numbers.
- `references/cadquery_modeling_patterns.md`: what each feature becomes in code.
- `assets/flange_plate.spec.json`, `assets/stepped_shaft.spec.json`,
  `assets/bracket.spec.json`: complete, passing specs.
