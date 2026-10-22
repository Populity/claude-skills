#!/usr/bin/env python3
"""dxf_geometry_extractor.py — pull exact geometry and dimensions out of an ASCII DXF drawing.

When the drawing exists as a CAD file (AutoCAD, KOMPAS-3D, nanoCAD, FreeCAD,
LibreCAD can all save DXF), nothing should be read off pixels. This tool parses
the ENTITIES section with the standard library and reports:

  * drawing units ($INSUNITS) and extents;
  * circles grouped by diameter with every centre, layer and linetype — the
    hole table, exact. Circles on centre-line layers (CENTER, AXIS, ОСЕВ...) or
    drawn dash-dot (a bolt-circle / PCD) are listed apart as construction circles;
  * arc radii (fillets, slot ends, rounded corners);
  * every DIMENSION's measured value and text override (Ø, R, M, tolerances);
    when the file stores no measurement (group 42) it is computed from the
    definition points, and Ø / R are prefixed from the dimension type;
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
import math
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


CENTRE_LAYER_RE = re.compile(r"^(center|centre|axis|axes|cl|осев|ось|оси)", re.IGNORECASE)
CENTRE_LTYPE_RE = re.compile(r"center|centre|dashdot|dash_dot|осев|штрихпунктир", re.IGNORECASE)


def pairs(text):
    lines = text.splitlines()
    for i in range(0, len(lines) - 1, 2):
        yield lines[i].strip(), lines[i + 1].rstrip("\r")


# MTEXT codes that take an argument up to ';' and codes that stand alone.
_MTEXT_ARG = set("ACcFfHQTWp")
_MTEXT_TOGGLE = set("LlOoKk")


def _mtext_plain(s):
    """Strip MTEXT inline formatting (\\P, \\L, \\H2.5x;, \\S1/2;, {...}) to plain text."""
    out, i, n = [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "{}":
            i += 1
            continue
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        code = s[i + 1]
        if code in "PNX":                   # paragraph / column break
            out.append(" ")
            i += 2
        elif code == "~":                   # non-breaking space
            out.append(" ")
            i += 2
        elif code in "\\{}":               # escaped literal
            out.append(code)
            i += 2
        elif code == "U" and re.match(r"\+[0-9A-Fa-f]{4}", s[i + 2:i + 7]):
            out.append(chr(int(s[i + 3:i + 7], 16)))
            i += 7
        elif code == "M" and re.match(r"\+[0-9A-Fa-f]{5}", s[i + 2:i + 8]):
            i += 8                          # legacy multibyte code: cannot map, drop
        elif code == "S":                   # stacked fraction / tolerance: 1^2 1/2 1#2
            j = s.find(";", i + 2)
            j = n if j < 0 else j
            out.append(re.sub(r"[\^#/]", "/", s[i + 2:j]).replace("/ ", "/").strip().strip("/"))
            i = j + 1
        elif code in _MTEXT_ARG:
            j = s.find(";", i + 2)
            i = n if j < 0 else j + 1
        elif code in _MTEXT_TOGGLE:
            i += 2
        else:
            out.append(code)
            i += 2
    return "".join(out)


def decode_text(s, mtext=True):
    """DXF control codes -> readable symbols (%%c = Ø, %%d = °, %%p = ±).

    MTEXT (and dimension text, which is MTEXT-formatted) also has its inline
    formatting removed; plain TEXT keeps backslashes as written.
    """
    s = re.sub(r"%%[cC]", "Ø", s)
    s = re.sub(r"%%[dD]", "°", s)
    s = re.sub(r"%%[pP]", "±", s)
    s = re.sub(r"%%[uUoOkK]", "", s)            # underline / overline / strike toggles
    s = re.sub(r"%%(\d{3})", lambda m: chr(int(m.group(1))), s)
    s = s.replace("%%%", "%")
    if mtext:
        s = _mtext_plain(s)
    return " ".join(s.split())


def parse(text):
    """-> (units, entities, layer linetypes). Raises ValueError if there is no ENTITIES section."""
    units, layer_ltype = "unitless", {}
    section = None
    ents, tables = [], []
    cur = None
    last_var = None
    saw_entities = False
    for code, val in pairs(text):
        if code == "0":
            val = val.strip()
            if cur:
                (tables if section == "TABLES" else ents).append(cur)
                cur = None
            if val == "SECTION":
                section = "?"
                continue
            if val == "ENDSEC":
                section = None
                continue
            if section == "ENTITIES":
                cur = {"type": val, "codes": defaultdict(list)}
            elif section == "TABLES" and val == "LAYER":
                cur = {"type": val, "codes": defaultdict(list)}
            continue
        if section == "?" and code == "2":
            section = val.strip()
            saw_entities = saw_entities or section == "ENTITIES"
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
        (tables if section == "TABLES" else ents).append(cur)
    if not saw_entities:
        raise ValueError("no ENTITIES section — is this an ASCII DXF?")
    for t in tables:
        name = (t["codes"].get("2") or [""])[0].strip()
        if name:
            layer_ltype[name] = (t["codes"].get("6") or ["CONTINUOUS"])[0].strip()
    return units, ents, layer_ltype


def _f(c, code, i=0, default=None):
    try:
        v = float(c[code][i])
    except (KeyError, IndexError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _dim_measure(kind, c):
    """Measurement from a DIMENSION's definition points when group 42 is absent."""
    p = {k: (_f(c, f"1{k}"), _f(c, f"2{k}")) for k in "0345"}

    def dist(a, b):
        if None in a or None in b:
            return None
        return math.hypot(b[0] - a[0], b[1] - a[1])
    if kind in ("linear", "aligned"):
        a, b = p["3"], p["4"]
        if None in a or None in b:
            return None
        if kind == "aligned":
            return dist(a, b)
        ang = math.radians(_f(c, "50", default=0.0))
        return abs((b[0] - a[0]) * math.cos(ang) + (b[1] - a[1]) * math.sin(ang))
    if kind in ("diameter", "radius"):
        return dist(p["0"], p["5"])
    return None


def _arc_points(x, y, r, a0, a1):
    """End points plus the quadrant points an arc actually passes through."""
    a0, a1 = a0 % 360, a1 % 360
    sweep = (a1 - a0) % 360 or 360
    angs = [a0, a0 + sweep] + [q for q in (0, 90, 180, 270, 360, 450, 540, 630)
                               if a0 < q < a0 + sweep]
    return [(x + r * math.cos(math.radians(a)), y + r * math.sin(math.radians(a))) for a in angs]


def extract(text):
    # R2000-R2004 files written in a Latin code page store other characters as \U+XXXX
    # in every string (layer names, text): unescape them before anything compares names.
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), text)
    units, ents, layer_ltype = parse(text)
    xs, ys = [], []
    circles = defaultdict(list)
    circle_meta = defaultdict(lambda: {"layers": set(), "linetypes": set()})
    construction = defaultdict(list)
    construction_meta = defaultdict(lambda: {"layers": set(), "linetypes": set()})
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
        layer = (c.get("8") or ["0"])[0].strip()
        layers[layer] += 1
        if t == "LINE":
            pt(_f(c, "10"), _f(c, "20"))
            pt(_f(c, "11"), _f(c, "21"))
        elif t == "CIRCLE":
            x, y, r = _f(c, "10", default=0.0), _f(c, "20", default=0.0), _f(c, "40")
            if r and r > 0:
                lt = (c.get("6") or ["BYLAYER"])[0].strip()
                eff = layer_ltype.get(layer, "CONTINUOUS") if lt.upper() in ("BYLAYER", "BYBLOCK") else lt
                is_cl = bool(CENTRE_LAYER_RE.match(layer) or CENTRE_LTYPE_RE.search(eff))
                d = round(2 * r, 4)
                (construction if is_cl else circles)[d].append([round(x, 4), round(y, 4)])
                meta = (construction_meta if is_cl else circle_meta)[d]
                meta["layers"].add(layer)
                meta["linetypes"].add(eff)
                pt(x - r, y - r)
                pt(x + r, y + r)
        elif t == "ARC":
            x, y, r = _f(c, "10", default=0.0), _f(c, "20", default=0.0), _f(c, "40")
            if r and r > 0:
                arcs[round(r, 4)] += 1
                for q in _arc_points(x, y, r, _f(c, "50", default=0.0), _f(c, "51", default=360.0)):
                    pt(*q)
        elif t == "POLYLINE":
            pass  # its own 10/20 is a dummy elevation point; the VERTEX entities carry the geometry
        elif t in ("LWPOLYLINE", "VERTEX"):
            for i in range(len(c.get("10", []))):
                pt(_f(c, "10", i), _f(c, "20", i))
        elif t == "DIMENSION":
            kind_code = int(_f(c, "70", default=0) or 0) & 0x07
            kind = {0: "linear", 1: "aligned", 2: "angular", 3: "diameter", 4: "radius",
                    5: "angular3p", 6: "ordinate"}.get(kind_code, "other")
            meas = _f(c, "42")
            if meas is None or meas < 0:  # R12 and some exporters store no measurement
                meas = _dim_measure(kind, c)
                meas = round(meas, 6) if meas is not None else None
            prefix = {"diameter": "Ø", "radius": "R"}.get(kind, "")
            value = f"{prefix}{meas:g}" if meas is not None else "(not stored)"
            raw = (c.get("1") or [""])[0]
            txt = decode_text(raw)
            if not txt or txt == "<>":
                shown = value
            elif "<>" in txt:
                before = txt.split("<>", 1)[0]
                # do not double a prefix the override already carries (e.g. "%%c<>" -> "Ø<>")
                v = value[len(prefix):] if prefix and before.rstrip().endswith(prefix) else value
                shown = txt.replace("<>", v)
            else:
                shown = txt
            dims.append({"kind": kind, "measured": meas, "text": shown})
        elif t in ("TEXT", "MTEXT", "ATTRIB"):
            body = "".join(c.get("3", [])) + "".join(c.get("1", []))
            if body.strip():
                notes.append(decode_text(body, mtext=(t == "MTEXT")))

    hole_table = [{"diameter": d, "count": len(v), "centers": v,
                   "layers": sorted(circle_meta[d]["layers"]), "linetypes": sorted(circle_meta[d]["linetypes"])}
                  for d, v in sorted(circles.items())]
    construction_circles = [{"diameter": d, "count": len(v), "centers": v,
                             "layers": sorted(construction_meta[d]["layers"]),
                             "linetypes": sorted(construction_meta[d]["linetypes"])}
                            for d, v in sorted(construction.items())]
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
        "construction_circles": construction_circles,
        "arc_radii": [{"radius": r, "count": n} for r, n in sorted(arcs.items())],
        "dimensions": dims,
        "notes": notes,
        "spec_hints": hints,
    }


def decode_bytes(raw):
    """Decode a DXF's bytes to text.

    UTF-8 first (R2007+ files, or a BOM). Otherwise a legacy 8-bit file: the
    header's $DWGCODEPAGE (ANSI_1251 for Russian) is used — unless the header says
    a Latin page (ANSI_1252, which many exporters write regardless) while the text
    is plainly Cyrillic: runs of 3+ bytes in 0xC0-0xFF are words in cp1251, not
    accented Latin letters. latin-1 is the last resort and never fails.
    """
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    m = re.search(rb"\$DWGCODEPAGE\s*\r?\n\s*3\s*\r?\n\s*ANSI_(\d+)", raw[:20000])
    declared = f"cp{m.group(1).decode()}" if m else None
    cyrillic_runs = len(re.findall(rb"[\xc0-\xff]{3,}", raw))
    if declared in (None, "cp1252") and cyrillic_runs:
        declared = "cp1251"
    for enc in ([declared] if declared else []) + ["cp1251", "latin-1"]:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1")


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
        text = decode_bytes(raw)
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
    for title, table in (("Holes (circles by diameter):", out["hole_table"]),
                         ("Construction circles (centre-line layer / dash-dot: PCD, not holes):",
                          out["construction_circles"])):
        if not table and table is out["construction_circles"]:
            continue
        print(title)
        for h in table:
            cs = ", ".join(f"({x:g}, {y:g})" for x, y in h["centers"][:8])
            more = " ..." if len(h["centers"]) > 8 else ""
            where = f"  [layer {', '.join(h['layers'])}; {', '.join(h['linetypes'])}]"
            print(f"  Ø{h['diameter']:g} x{h['count']}: {cs}{more}{where}")
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
    try:
        sys.exit(main())
    except BrokenPipeError:  # output piped into head/less that closed early
        sys.stderr.close()
        sys.exit(0)
