# drawing-to-3d

> A model is not done when the script runs. It is done when it matches the
> drawing.

Give it a photo, scan, PDF or DXF of an engineering drawing, either Russian
ЕСКД/ГОСТ or ISO/ASME. It produces a precise 3D model of the part as **STEP +
STL**, built by a generated **CadQuery** script that runs on your own PC, and
it checks the result against the drawing before calling the model done.

Two rules make it precise and not just plausible:

1. **No dimension is guessed.** Anything that cannot be read from the drawing
   goes into `open_questions`, and the pipeline refuses to generate code until
   every question is answered.
2. **Every feature carries its drawing callout.** If a fillet is too large or a
   pocket misses the body, the build error names the place on the sheet, not a
   line in a stack trace.

## Install

```bash
/plugin marketplace add alirezarezvani/claude-skills
/plugin install drawing-to-3d
```

## Use

```bash
/cs:drawing-to-3d ~/drawings/flange.jpg                   # full pipeline
/cs:cad-verify flange_plate.spec.json flange_plate.report.json flange_plate.stl
```

## Quick start on your PC

The generated script is the deliverable. It needs **Python 3.10+** and
CadQuery. It works on Windows, macOS and Linux.

```bash
pip install cadquery
python flange_plate_cad.py                      # → flange_plate.step, .stl, .report.json
python flange_plate_cad.py --out out --formats step,stl,svg
```

On Windows, if `pip install cadquery` fails, use conda instead:
`conda install -c conda-forge cadquery`.

Then open the files:

| File | Open with |
|---|---|
| `<part>.step` | KOMPAS-3D, FreeCAD, Fusion 360, SolidWorks, Inventor, any CAD system |
| `<part>.stl` | any slicer or mesh viewer (PrusaSlicer, Cura, Windows 3D Viewer) |
| `<part>.html` | any browser. This is the viewer from `stl_viewer.py`, with standard views and an Ortho toggle |

## The five scripts

All five live in `skills/drawing-to-3d/scripts/`. They are stdlib-only: no
network, no LLM calls, and no dependencies. CadQuery is needed only by the
*generated* script. Each one supports `--help`, `--sample` and
`--output json`.

| Script | What it does | Exit codes |
|---|---|---|
| `part_spec_validator.py` | Gates the drawing readout (`part-spec.v1`). It checks that every number is positive, every feature is fully dimensioned, holes are inside the footprint, and counterbores are larger than their hole. It **blocks while any question is open.** | 0 valid · 1 warnings (`--strict`) · 2 errors · **3 open questions** · 64 usage |
| `cadquery_codegen.py` | Spec → one runnable `<part>_cad.py`. Each feature is a labelled step, and the script measures what it built (bbox, volume, hole diameters, validity) into `<part>.report.json`. | 0 written · 2 spec invalid · 3 open questions · 64 usage |
| `model_verifier.py` | Checks the report against the drawing. V1 one valid solid · V2 overall size · V3 every hole Ø and its count · V4 title-block mass vs volume × density · V5 thread callouts | **0 PASS · 1 FAIL** · 64 usage |
| `dxf_geometry_extractor.py` | Reads exact hole tables, radii, dimension values and title-block notes from an ASCII DXF, so nothing is read off pixels when a CAD file exists. | 0 extracted · 2 not ASCII DXF · 64 usage |
| `stl_viewer.py` | Packs the STL into one self-contained HTML viewer. It has Front, Top, Left, Right, Bottom, Back and Iso views, an Ortho toggle and feature edges, so you can lay views over the drawing. | 0 written · 2 unreadable STL · 64 usage |

## Pipeline

```
drawing (photo / scan / PDF)          drawing (ASCII DXF)
          │                                    │
          │                       dxf_geometry_extractor.py  ── exact holes, radii, dims
          ▼                                    │
  read per drawing_reading_guide.md  ◄─────────┘
          │   (title block → projection method → views → callouts → tech. requirements)
          ▼
   <part>.spec.json   (part-spec.v1, mm, ids = drawing callouts)
          │
   part_spec_validator.py ── exit 2: fix spec
          │                └─ exit 3: ASK the user, never guess ─┐
          ▼                                                      │
   cadquery_codegen.py  ──►  <part>_cad.py                       │
          │                                                      │
          ▼  (runs on your PC: pip install cadquery)             │
   <part>.step  <part>.stl  <part>.report.json                   │
          │                                                      │
   model_verifier.py ── FAIL: fix the spec, not the script ──────┘
          │ PASS
          ▼
   stl_viewer.py ──► <part>.html ── compare each view with the drawing (Ortho)
          │
          ▼
        DONE
```

## Files

```
engineering/drawing-to-3d/
├── .claude-plugin/plugin.json
├── README.md
├── agents/cs-cad-modeler.md
├── commands/
│   ├── cs-drawing-to-3d.md
│   └── cs-cad-verify.md
└── skills/drawing-to-3d/
    ├── SKILL.md
    ├── scripts/
    │   ├── part_spec_validator.py
    │   ├── cadquery_codegen.py
    │   ├── model_verifier.py
    │   ├── dxf_geometry_extractor.py
    │   └── stl_viewer.py
    ├── references/
    │   ├── drawing_reading_guide.md
    │   ├── part_spec_format.md
    │   └── cadquery_modeling_patterns.md
    └── assets/
        ├── flange_plate.spec.json
        ├── stepped_shaft.spec.json
        ├── bracket.spec.json
        ├── flange_plate.dxf
        └── drawing_readout_worksheet.md
```

Start with [`skills/drawing-to-3d/SKILL.md`](skills/drawing-to-3d/SKILL.md).
The spec format is in
[`references/part_spec_format.md`](skills/drawing-to-3d/references/part_spec_format.md),
and the guidance for reading ЕСКД and ISO/ASME sheets is in
[`references/drawing_reading_guide.md`](skills/drawing-to-3d/references/drawing_reading_guide.md).

## Limits

- **Prismatic and turned parts only.** It handles plates, brackets, flanges,
  housings, shafts and bushings: extrusions, revolves, holes, pockets,
  bosses, slots, fillets, chamfers and shells. It does not handle freeform or
  organic surfaces, sculpted castings, blades or lofted ergonomic shapes.
- **Threads are cosmetic.** The model gets a tap-drill (or major-diameter)
  hole, and the callout (for example `M8x1.25-6H`) is carried into the report.
  Your CAD system or the shop applies the actual thread.
- **Assemblies: one part per spec.** Model each detail (деталь) of an
  assembly drawing separately, then assemble them in your CAD.
- **Tolerances, fits, roughness and heat treatment** are notes, not geometry.
  The model is built at nominal size.
- **DWG and binary DXF** must be re-saved as ASCII DXF first.
- **A photo is read, not measured.** Dimensions come from the dimension text,
  never from scaling pixels, even when the sheet states a scale.

## Not this plugin

| You want | Use |
|---|---|
| A 3D model from a written description, not a drawing | ask Claude directly; this plugin starts from a drawing |
| Mesh repair, sculpting, organic shapes | Blender, or a mesh tool |
| 2D drafting / producing the drawing itself | your CAD system (KOMPAS-3D, FreeCAD TechDraw) |
| Tolerance stack-up or strength analysis | an engineering analysis tool; this plugin models nominal geometry |

This plugin turns **an existing drawing of one part** into a verified solid
model.

## License

MIT. CadQuery itself (installed separately on your PC) is Apache-2.0.
