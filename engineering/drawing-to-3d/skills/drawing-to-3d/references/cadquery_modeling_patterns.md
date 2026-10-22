# CadQuery Modeling Patterns — what the generated script does, and how it fails

> Lens: *the generated script is a build recipe that names the drawing feature
> that broke. Fix the spec, regenerate, rebuild; touch the script by hand only
> for what the spec cannot say.*
>
> Checked against CadQuery 2.8 (OCP / OpenCascade 7.8). Confidence per claim:
> **high** = tested here or stated in the docs; **moderate** = common
> experience, version-dependent.

---

## 1. Anatomy of `<part>_cad.py`

`cadquery_codegen.py` emits one self-contained file:

| Piece | What it does |
|---|---|
| `PART`, `SPEC` | the part name and the whole spec, embedded, so the script needs no other file |
| `_face_point`, `L(wp, face, pts)`, `A(wp, face, deg)` | convert global in-face coordinates and angles to the face workplane's local ones (§3) |
| `near_edges(r, points)` | resolves `{"near": …}` edge picks (§5) |
| `_big(r)` | 2 × bounding-box diagonal + 1: a length that certainly crosses the part, used for datum cuts |
| `feature(label)` | a context manager. Any exception becomes `RuntimeError: feature <id> <label> failed: <OCC message>` |
| `build()` | `with feature("base"):` then one `with feature("<id> <label>"):` block per spec feature, in spec order |
| `measure()` | validity, solid count, bbox, bbox min, volume, area, face/edge counts, every cylindrical face's Ø (`cylinder_diameters_mm`) and its axis (`cylinders`: `diameter`, `axis_point`, `axis_dir`), thread callouts → `<part>.report.json` |
| `main()` | `--out DIR` (default `.`), `--formats step,stl[,brep,svg]`, `--stl-tolerance` (default 0.01). Exit **0 only if valid and exactly 1 solid**, else 1 |
| tail | `if __name__ == "__main__": main()`, `elif "show_object" in globals(): show_object(build())` (CQ-editor) |

Feature → code (what `feature_lines()` emits):

| Spec | CadQuery |
|---|---|
| base `extrude` | `cq.Workplane("XY")<profile>.extrude(h)` |
| base `revolve` | `cq.Workplane("XZ").polyline(rz).close().revolve(360, (0,0,0), (0,1,0))` (axis = global Z) |
| hole on a face | `wp = r.faces(face).workplane(centerOption="ProjectedOrigin")`, then `wp.pushPoints(L(...)).hole / cboreHole / cskHole` |
| hole / pocket / slot on a datum | `wp = cq.Workplane("YZ")`, then `r = r.cut(wp<profile>.extrude(_big(r), both=True))` |
| pocket / slot | `wp<profile>.cutBlind(-depth)` or `.cutThruAll()` |
| boss | `wp<profile>.extrude(height)` (adds to the solid) |
| regular_polygon on a face | `.polyline(L(wp, face, vertices)).close()`, vertices computed in global (u, v) with the first on +u (on the base: `.polygon(n, AC)`) |
| rect with corner radius, angle, or on a face | `.sketch().push(L(...)).rect(w, l, angle=A(...)).reset().vertices().fillet(cr).finalize()` |
| fillet / chamfer | `r.edges(sel).fillet(R)` or `r.newObject(near_edges(r, pts)).chamfer(a[, b])` |
| revolve_cut | revolve `profile_rz` as above, `r.cut(cutter)` |
| shell | `r.faces(face).shell(t)` |
| thread | a comment + `pass`. Recorded in the report only |

## 2. Feature order: base → add → cut → holes → edges

The spec list is the build order. Write it in this order:

1. **Base** (the biggest prism or the turned outline).
2. **Bosses** (material added), so later faces exist.
3. **Pockets, slots, revolve cuts**.
4. **Holes**, so they also cut through bosses.
5. **Fillets and chamfers last**, so they finish the final edges. The validator
   warns when a hole/pocket/boss/slot follows a fillet or chamfer.
6. **Shell**: usually right after the outer shape and before small holes. If
   it fails, move it earlier.

Two consequences of "faces are chosen when the feature runs" (confidence:
**high**, tested):

- After a boss on `>Z`, `faces(">Z")` is the **boss top** (a Ø40 × 8 boss on a
  12 mm plate makes `>Z` sit at z = 20). Blind depths are then measured from
  there.
- A hole meant to start on the plate beside the boss needs a different
  approach: put it before the boss, or give it from `<Z` with depth measured
  from the bottom.

## 3. Face workplanes are mirrored; the spec is not

CadQuery's face workplanes have their own local axes (confidence: **high**,
tested in CadQuery 2.8):

| Face | Local x | Local y |
|---|---|---|
| `>Z` | +X | +Y |
| `<Z` | +X | **−Y** |
| `>X` | +Y | +Z |
| `<X` | **−Y** | +Z |
| `>Y` | **−X** | +Z |
| `<Y` | +X | +Z |

Writing drawing coordinates straight into `pushPoints` would mirror features on
the bottom and on three side faces. The generated script therefore
passes every coordinate through `L(wp, face, pts)`, which builds the global 3D
point (`_face_point`) and calls `wp.plane.toLocalCoords`. Angles go through
`A()`. `centerOption="ProjectedOrigin"` puts the workplane origin at the global
origin projected onto the face, so `(0, 0)` always means "on the global axis"
and never "face centre".

`regular_polygon` on a face has no angle field. Its vertices are computed in
global (u, v), first corner on +u, and passed through `L()` as a polyline, so
an odd-sided polygon is not mirrored on `<X` or `>Y`. `revolve_cut` and the
revolve base never use `L()`: they are global by construction.

## 4. Selector cheat-sheet (`edges` strings, and `face` internally)

Counts measured on a 20 × 20 × 10 box (confidence: **high**):

| Selector | Meaning | Box result |
|---|---|---|
| `">Z"` | in the max-Z plane (edges: all edges lying at the top) | 4 |
| `"<Z"` | in the min-Z plane | 4 |
| `"|Z"` | parallel to Z (vertical edges) | 4 |
| `"#Z"` | perpendicular to Z (horizontal edges) | 8 |
| `"%LINE"`, `"%CIRCLE"` | by geometry type | 12, 0 |
| `">Z and |X"` | top edges parallel to X | 2 |
| `"|Z and >X"` | vertical edges on the +X side | 2 |
| `"not(<Z)"` | everything except the bottom ring | 8 |
| `">Z[-2]"` (faces) | second-from-top face group | stepped shaft: the shoulder at z = 40 |
| `"not(%CIRCLE)"` | straight edges only | (outer outline without hole rims, on a plate) |

Rules of thumb:

- `">Z"` on a plate with holes selects **every** top edge, hole and slot rims
  included (23 edges on the flange sample). Chamfering all of them fails where
  two rims are closer than twice the chamfer. That is exactly what happened in
  the flange plate, where slot S1 and the 240°/300° H3 holes are ~1 mm apart:
  OCC returns `BRep_API: command not done`. Use `near` points on the outline,
  or `">Z and not(%CIRCLE)"` when the outline has no arcs.
- An index such as `">X[-2]"` works on groups of parallel faces. On a selection
  with a single group it raises "Attempted to access index -2 of a list with
  length 1".
- The validator only checks the selector's characters. Its meaning is checked
  by building, so look at the result in the viewer.

## 5. `near` edge picking and the seam rule

`{"near": [x, y, z]}` resolves in `near_edges()`:

1. Collect the solid's edges, **excluding seams**: an edge that is closed on a
   face (`BRep_Tool.IsClosed_s(edge, face)`), which is the line where a
   cylinder or a revolved surface closes on itself. A seam is not an edge on
   the drawing, and filleting it fails.
2. For each point, take the edge at the smallest **true distance**
   (`edge.distance(vertex)`).
3. Fail with `no edge within 0.5 mm of (x, y, z); nearest is d mm away` if the
   best is further than 0.5 mm.
4. Remove duplicates and apply one fillet/chamfer to all picks.

Why not CadQuery's `NearestToPointSelector`? It ranks by the edge's **centre**,
and a circle's centre is on its axis, so a point on a rim can lose to an
unrelated short edge (confidence: **high**, tested). Practical tips:

- Put the point **on** the edge: for a circle of radius r at height z, use any
  `(r·cos a, r·sin a, z)`. Global coordinates, not face-local.
- The revolve base (`XZ` sketch) puts its seam on the **+X** side. Points such
  as `[0, 12, 0]` or `[-12, 0, 40]` sit away from it. Seams are excluded
  anyway, so this is about clarity, not correctness.
- A shoulder edge on a shaft is the circle where the two diameters meet:
  `[0, r_small, z_shoulder]`.

## 6. Datum cuts: cross holes, radial holes, slots across a shaft

A hole on `">X"` of a cylinder fails with *"If multiple objects selected, they
all must be planar faces"*: there is no flat face there (confidence: **high**,
tested). Use a datum plane:

```json
{"id": "H2", "type": "hole", "face": "YZ", "diameter": 4, "depth": "through", "positions": [[0, 20]]}
```

The circle is drawn on the YZ plane at (Y = 0, Z = 20) and extruded
`_big(r)` **both ways**, so it crosses the whole part along X. Datum cuts are
always through (the validator enforces it) and cannot carry a counterbore or
countersink. A blind radial hole, or one at an angle other than along X/Y/Z,
needs a hand-extension (§10).

## 7. Fillets and chamfers: the usual failure

`BRep_API: command not done` (OCC's `StdFail_NotDone`) from a fillet or chamfer
almost always means one of:

| Cause | Fix |
|---|---|
| Radius ≥ half the width of an adjacent face (R10 on the vertical edges of a 20-wide box fails) | Read the radius again. Strictly less than half is required (confidence: **high**) |
| Selector hits more edges than meant (§4) | Narrow it, or use `near` |
| Two finished edges closer than the sum of their sizes (a thin wall between features) | Finish only the edges the drawing shows; leave the rest sharp, as an assumption |
| A fillet meeting an earlier fillet at a corner with a much larger radius | Do the larger radius first, as a separate feature |
| Fillet on a seam | Already excluded for `near`; avoid seams in selector strings |

The validator warns at R ≥ half the part height for `>…Z`/`<…Z` selectors. The
warning is conservative: R5 on the top edges of a 10-high box does build.

**Unequal chamfers (`size2`).** CadQuery's `chamfer(length, length2)` assigns
the legs by OCC's face order. On a box's top-back edge, `chamfer(2, 4)` put 2
on the side face and 4 on the top. Always check which leg went where in the
viewer, and swap the values if needed (confidence: **moderate**: consistent
per edge, but not documented as a rule).

## 8. Other features

- **Revolve profiles** must not cross the axis (r < 0). A cross-axis profile
  fails in OCC with `command not done`; the validator refuses it first. An
  outline that touches the axis (r = 0 segment) is the normal solid-shaft case.
- **revolve_cut**: the outline must overlap the body, or nothing changes.
  Since that is silent, compare the volume or look at it. Grooves are
  rectangles `[[r_bottom, z0], [r_outer, z0], [r_outer, z1], [r_bottom, z1]]`;
  give `r_outer` slightly above the real radius (10 vs 9) so the cut is clean.
- **Shell**: `faces(face).shell(-t)` removes that face and leaves walls of
  thickness t inside. It fails when t is larger than the smallest inner
  curvature radius, or with complex filleted geometry. A thickness bigger than
  the part does not always raise: check the volume.
- **Bosses must touch.** A boss that floats off the body leaves 2 solids. V1
  fails, the script exits 1.
- **Cuts that miss** do not raise. V3 catches a missing or misplaced hole:
  each spec hole (and counterbore) must match a report cylinder of the same Ø
  (±0.01 mm), with its axis normal to the hole's face and passing within
  **0.05 mm** of the drawn centre. That also catches a hole mirrored by
  hand-written local coordinates. Countersinks (conical faces) are not
  checked. V4 (mass) catches missed pockets, slots and grooves; V2 usually
  does not.
- **Cosmetic threads.** The hole stays at drill Ø and the designation goes to
  the report. That is standard CAD practice: STEP has no portable thread
  feature, and a real helix makes the file heavy and slow. Model a real thread
  only for 3D printing, as a hand-extension (for example with the third-party
  `cq_warehouse.thread` package; confidence: **moderate**, outside core
  CadQuery).

## 9. Output files

**STEP** (`.step`) is the exact B-rep and the deliverable for CAD and CNC.
CadQuery 2.8 writes **AP214** (`FILE_SCHEMA('AUTOMOTIVE_DESIGN …')`), in mm.

| Program | How to open |
|---|---|
| FreeCAD | File → Open (or Import) the `.step`; it opens as a Part solid. Check with Part → Check geometry |
| KOMPAS-3D | Файл → Открыть, file type STEP (AP203/AP214/AP242); it opens as a part (деталь) with an imported body |
| Fusion 360 | File → Open → Open from my computer (it uploads and translates the file), or upload in the Data Panel |
| SolidWorks | File → Open, type STEP AP203/214/242 (*.step; *.stp). Run Import Diagnostics if asked |

Confidence: **moderate** on exact menu names, which shift between versions.

**STL** is a triangle mesh for viewing, printing and `stl_viewer.py`. The
generated script calls `cq.exporters.export(r, path, tolerance=…,
angularTolerance=0.1)`. Measured on CadQuery 2.8 (confidence: **high**):

- CadQuery meshes STL with **`relative=True`** by default, so `--stl-tolerance`
  is a fraction of each edge's size, **not mm**. The script's help text says
  "mm"; read it as relative.
- The hard-coded `angularTolerance=0.1` rad usually decides the mesh. A
  Ø50 × 20 cylinder with a Ø10 hole gave 50 484 bytes at tolerance 0.1, 0.01
  and 0.001 alike. Changing `angularTolerance` to 0.5 gave 11 684 bytes, and
  0.01 gave 502 884 bytes (with tolerance 0.001).
- For a finer print mesh, edit `angularTolerance` in the generated script, or
  export from the STEP in your slicer/CAD. Coarse is fine for viewing.

**BREP** (`--formats brep`) is OpenCascade's native format, lossless for
FreeCAD/OCC. **SVG** is a quick 2D projection.

## 10. CQ-editor live preview

Open `<part>_cad.py` in CQ-editor and press Render. The script detects
`show_object` in its globals and calls `show_object(build(), name=PART)`.
CQ-editor shows the model, and a failing feature appears in its traceback as
`feature <id> … failed`. The standalone `python <part>_cad.py` path is
unaffected. Confidence: **moderate** that the detection works in every
CQ-editor version (it relies on CQ-editor injecting `show_object`).

## 11. When the spec cannot say it: extend the script by hand

part-spec.v1 cannot express features on inclined faces, holes at an angle,
blind radial holes in shafts, lofts, sweeps, ribs with draft, knurls, text,
real threads, or holes on a cylindrical surface other than through-datum ones.

**First choice: put it in `open_questions`** and agree with the user on how to
handle it. Then either simplify (for example, model a blind radial hole as a
through hole and record that as an assumption) or extend by hand.

**Extending by hand**, the safe way:

1. Copy `<part>_cad.py` to `<part>_cad_custom.py`. Regenerating overwrites the
   original.
2. Add a block **inside `build()`**, after the last generated feature and
   before `return r`, in the same style, so failures are still named:

```python
    with feature("X1 radial hole Ø3 at z=70, 30° from +X (hand-added)"):
        ang, z, d = 30, 70, 3
        L_ = _big(r)
        dirv = cq.Vector(math.cos(math.radians(ang)), math.sin(math.radians(ang)), 0)
        start = cq.Vector(0, 0, z) - dirv * (L_ / 2)
        r = r.cut(cq.Workplane().add(cq.Solid.makeCylinder(d / 2, L_, start, dirv)))
```

   (Tested on the stepped shaft: one valid solid. A hole tilted on a flat
   face also works: `r.faces(">Z").workplane().transformed(rotate=(20, 0, 0)).hole(4, 10)`.)
3. Record it in the spec's `assumptions` (`"what": "X1 hand-added in
   <part>_cad_custom.py"`), so the next reader knows the spec no longer
   describes the whole model.
4. Re-run the build and `model_verifier.py`. V3 only checks spec holes, so
   confirm the hand-added feature in the viewer.

**Why prefer extending the spec.** A hand edit breaks the chain "drawing →
spec → model". Regenerating loses it, the validator never checks it, V3/V5
do not know about it, and the next change to the drawing must be merged by
hand. If the same kind of feature comes up twice, it belongs in the schema
(raise it with the tools' owner), not in another hand edit.

---

## Sources

1. CadQuery documentation, *String Selectors Reference*.
   <https://cadquery.readthedocs.io/en/latest/selectors.html>
2. CadQuery documentation, *Workplanes* and *API Reference* (`faces`,
   `workplane(centerOption=…)`, `hole`, `cboreHole`, `cskHole`, `fillet`,
   `chamfer`, `shell`, `revolve`).
   <https://cadquery.readthedocs.io/en/latest/workplane.html>,
   <https://cadquery.readthedocs.io/en/latest/classreference.html>
3. CadQuery documentation, *Importing and Exporting Files* (STEP, STL
   `tolerance`/`angularTolerance`), plus the CadQuery 2.8 source
   `occ_impl/shapes.py::exportStl` (`relative=True` default).
   <https://cadquery.readthedocs.io/en/latest/importexport.html>
4. Open CASCADE Technology reference: `BRepFilletAPI_MakeFillet`,
   `BRepMesh_IncrementalMesh` (linear/angular deflection, relative mode),
   `BRep_Tool::IsClosed` (seam edges). <https://dev.opencascade.org/doc/refman/html/>
5. CQ-editor (CadQuery GUI) README: `show_object` and the live preview.
   <https://github.com/CadQuery/CQ-editor>
6. ISO 10303-214 (STEP AP214): the exchange format written by `exportStep`.
7. FreeCAD wiki, *Import Export* / *STEP*.
   <https://wiki.freecad.org/Import_Export>
8. Shigley's / Machinery's Handbook conventions for cosmetic threads and
   chamfer callouts. Modelling threads at the drill Ø is the standard CAD
   practice that STEP exchange relies on (confidence: **moderate**).

## Related tools in this skill

- `cadquery_codegen.py`: §1–§6 (what is emitted)
- `model_verifier.py`: §8 (V1 solids, V3 hole Ø + axis position, V4 mass)
- `stl_viewer.py`: §4 and §7 (checking selectors and chamfer legs by eye)
