#!/usr/bin/env python3
"""dxf_geometry_extractor.py — pull exact geometry and dimensions out of an ASCII DXF drawing.

When the drawing exists as a CAD file (AutoCAD, KOMPAS-3D, nanoCAD, FreeCAD,
LibreCAD can all save DXF), nothing should be read off pixels. This tool parses
the ENTITIES section with the standard library and reports:

  * drawing units ($INSUNITS) and extents;
  * circles grouped by diameter with every centre — the hole table, exact;
  * arc radii (fillets, slot ends, rounded corners);
  * every DIMENSION's measured value and text override (Ø, R, M, tolerances);
  * TEXT/MTEXT notes (general notes, title block: material, mass, scale);
  * per-layer entity counts (separates outline, centre lines, hatching).

A DXF sheet holds several views side by side in one coordinate space, so the
output is geometry per sheet; which circles belong to which view is decided
when the part spec is written (see references/drawing_reading_guide.md).

Binary DXF and DWG are not supported: re-save as ASCII DXF (any version).

Exit codes: 0 extracted · 2 not an ASCII DXF / no ENTITIES · 64 usage.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

INSUNITS = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}

SAMPLE_DXF = """0
SECTION
2
HEADER
9
$INSUNITS
70
4
0
ENDSEC
0
SECTION
2
ENTITIES
0
LWPOLYLINE
8
OUTLINE
90
4
70
1
10
-60
20
-40
10
60
20
-40
10
60
20
40
10
-60
20
40
0
CIRCLE
8
OUTLINE
10
-48
20
-28
40
4.5
0
CIRCLE
8
OUTLINE
10
48
20
-28
40
4.5
0
CIRCLE
8
OUTLINE
10
48
20
28
40
4.5
0
CIRCLE
8
OUTLINE
10
-48
20
28
40
4.5
0
CIRCLE
8
OUTLINE
10
0
20
0
40
11
0
DIMENSION
8
DIM
1
4 отв. %%c9
42
9
0
DIMENSION
8
DIM
1
<>
42
120
0
TEXT
8
NOTES
1
Сталь 45 ГОСТ 1050-2013
0
TEXT
8
NOTES
1
Масса 1,02 кг
0
ENDSEC
0
EOF
"""


def pairs(text):
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        yield lines[i].strip(), lines[i + 1].rstrip("\r")


def decode_text(s):
    """DXF control codes -> readable symbols (%%c = Ø, %%d = °, %%p = ±)."""
    s = re.sub(r"%%[cC]", "Ø", s)
    s = re.sub(r"%%[dD]", "°", s)
    s = re.sub(r"%%[pP]", "±", s)
    s = re.sub(r"\\[Pp]", " ", s)                # MTEXT paragraph break
    s = re.sub(r"\\[A-Za-z][^;\\]*;", "", s)     # MTEXT inline formatting codes
    s = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace("{", "").replace("}", "").strip()


def parse(text):
    if "ENTITIES" not in text:
        raise ValueError("no ENTITIES section — is this an ASCII DXF?")
    units = "unitless"
    section = None
    ents = []
    cur = None
    last_var = None
    for code, val in pairs(text):
        if code == "0":
            if cur:
                ents.append(cur)
                cur = None
            if val == "SECTION":
                section = "?"
                continue
            if val == "ENDSEC":
                section = None
                continue
            if section == "ENTITIES":
                cur = {"type": val, "codes": defaultdict(list)}
            continue
        if section == "?" and code == "2":
            section = val
            continue
        if section == "HEADER":
            if code == "9":
                last_var = val
            elif last_var == "$INSUNITS" and code == "70":
                try:
                    units = INSUNITS.get(int(val), f"code {val}")
                except ValueError:
                    pass
            continue
        if cur is not None:
            cur["codes"][code].append(val)
    if cur:
        ents.append(cur)
    return units, ents


def _f(c, code, i=0, default=None):
    try:
        return float(c[code][i])
    except (KeyError, IndexError, ValueError):
        return default


def extract(text):
    units, ents = parse(text)
    xs, ys = [], []
    circles = defaultdict(list)
    arcs = Counter()
    dims, notes = [], []
    layers = Counter()
    types = Counter()

    def pt(x, y):
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)

    for e in ents:
        t, c = e["type"], e["codes"]
        types[t] += 1
        layers[(c.get("8") or ["0"])[0]] += 1
        if t == "LINE":
            pt(_f(c, "10"), _f(c, "20"))
            pt(_f(c, "11"), _f(c, "21"))
        elif t == "CIRCLE":
            x, y, r = _f(c, "10"), _f(c, "20"), _f(c, "40")
            if r:
                circles[round(2 * r, 4)].append([round(x, 4), round(y, 4)])
                pt(x - r, y - r)
                pt(x + r, y + r)
        elif t == "ARC":
            x, y, r = _f(c, "10"), _f(c, "20"), _f(c, "40")
            if r:
                arcs[round(r, 4)] += 1
                pt(x, y)
        elif t in ("LWPOLYLINE", "POLYLINE", "VERTEX"):
            for i in range(len(c.get("10", []))):
                pt(_f(c, "10", i), _f(c, "20", i))
        elif t == "DIMENSION":
            meas = _f(c, "42")
            raw = (c.get("1") or ["<>"])[0]
            txt = decode_text(raw)
            shown = txt.replace("<>", f"{meas:g}" if meas is not None else "") if txt else (
                f"{meas:g}" if meas is not None else "")
            kind_code = int(_f(c, "70", default=0) or 0) & 0x0F
            kind = {0: "linear", 1: "aligned", 2: "angular", 3: "diameter", 4: "radius",
                    5: "angular3p", 6: "ordinate"}.get(kind_code, "other")
            dims.append({"kind": kind, "measured": meas, "text": shown})
        elif t in ("TEXT", "MTEXT", "ATTRIB"):
            body = "".join(c.get("3", [])) + "".join(c.get("1", []))
            if body.strip():
                notes.append(decode_text(body))

    hole_table = [{"diameter": d, "count": len(v), "centers": v}
                  for d, v in sorted(circles.items())]
    hints = []
    for n in notes:
        low = n.lower()
        if re.search(r"масса|mass|weight", low):
            hints.append({"mass_note": n})
        if re.search(r"сталь|steel|алюмин|alum|д16|латун|brass|бронз|bronze|чугун|iron|titan|титан", low):
            hints.append({"material_note": n})
        if re.search(r"\bM\d+|М\d+", n):
            hints.append({"thread_note": n})
    return {
        "units": units,
        "extents": ({"min": [min(xs), min(ys)], "max": [max(xs), max(ys)],
                     "size": [round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4)]} if xs else None),
        "entity_counts": dict(types),
        "layers": dict(layers),
        "hole_table": hole_table,
        "arc_radii": [{"radius": r, "count": n} for r, n in sorted(arcs.items())],
        "dimensions": dims,
        "notes": notes,
        "spec_hints": hints,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract exact holes, radii, dimensions and notes from an ASCII DXF drawing.",
        epilog="Exit: 0 extracted · 2 not an ASCII DXF · 64 usage")
    ap.add_argument("dxf", nargs="?", help="drawing.dxf (ASCII)")
    ap.add_argument("--sample", action="store_true", help="run on a built-in sample drawing")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

    if a.sample:
        text = SAMPLE_DXF
    elif a.dxf:
        try:
            with open(a.dxf, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 64
        if raw.startswith(b"AutoCAD Binary DXF"):
            print("error: binary DXF — re-save as ASCII DXF", file=sys.stderr)
            return 2
        if raw[:2] == b"AC" and raw[2:6].isdigit():
            print("error: this looks like a DWG file — export it as ASCII DXF", file=sys.stderr)
            return 2
        for enc in ("utf-8", "cp1251", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    else:
        ap.print_usage(sys.stderr)
        return 64

    try:
        out = extract(text)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if a.output == "json":
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print(f"Units: {out['units']}")
    if out["extents"]:
        print(f"Extents: {out['extents']['size'][0]} x {out['extents']['size'][1]} (sheet, all views)")
    print("Holes (circles by diameter):")
    for h in out["hole_table"]:
        cs = ", ".join(f"({x:g}, {y:g})" for x, y in h["centers"][:8])
        more = " ..." if len(h["centers"]) > 8 else ""
        print(f"  Ø{h['diameter']:g} x{h['count']}: {cs}{more}")
    if out["arc_radii"]:
        print("Arc radii: " + ", ".join(f"R{r['radius']:g} x{r['count']}" for r in out["arc_radii"]))
    print("Dimensions:")
    for d in out["dimensions"]:
        print(f"  [{d['kind']}] {d['text']}")
    if out["notes"]:
        print("Notes:")
        for n in out["notes"]:
            print(f"  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
