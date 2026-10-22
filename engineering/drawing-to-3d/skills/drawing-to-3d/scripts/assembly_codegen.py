#!/usr/bin/env python3
"""assembly_codegen.py — turn an assembly spec into a runnable CadQuery assembly script.

An assembly spec (schema ``assembly-spec.v1``) is the readout of an assembly
drawing (сборочный чертёж): which part specs go in, how many of each, where
each instance sits (explicit placement and/or mates, сопряжения), and how each
line appears in the specification (спецификация, ГОСТ 2.106).

    python assembly_codegen.py pump_cover.asm.json      # -> pump_cover_asm.py
    pip install cadquery
    python pump_cover_asm.py                            # -> STEP, STL, report, спецификация

The generated script writes:

* ``<assembly>.step``      — assembly STEP; every instance is a named, coloured
  node, so KOMPAS-3D / FreeCAD / SolidWorks open it as an assembly tree;
* ``<assembly>.stl``       — all instances merged, for viewers and slicers;
* ``<assembly>.report.json`` — overall bbox, per-instance placement after the
  mate solve, mate residuals, and the interference check (any pair of solids
  sharing more than 0.001 mm³ is a clash);
* ``<assembly>_spec.csv`` / ``<assembly>_spec.md`` — the specification with
  ГОСТ 2.106 columns (Формат, Зона, Поз., Обозначение, Наименование, Кол.,
  Примечание), sections in ГОСТ order, positions numbered automatically.

How parts get into the assembly script: every referenced part spec is run
through cadquery_codegen.generate() *here*, and the resulting part script is
embedded verbatim in ``<assembly>_asm.py`` and executed into its own module at
run time. The assembly script is therefore one self-contained file — no import
paths, no sibling ``*_cad.py`` that can drift. It records each part spec's
SHA-256 and warns at run time if the spec file next to it has changed since
(regenerate the assembly after editing a part). ``--emit-parts`` on the
generated script writes the embedded ``<part>_cad.py`` files out for inspection.

Placement: ``translate`` [x, y, z] mm and ``rotate`` [[axis, deg], ...] about the
global X/Y/Z axes through the origin, applied in the listed order, then the
translation. Mates (KOMPAS: Соосность, Совпадение, На расстоянии, Фиксация) are
mapped onto cq.Assembly constraints and solved; explicit placements of mated
instances are only the solver's starting guess.

  coaxial     a, b axial refs   -> Axis (0° aligned / 180° opposed) + PointOnLine
  coincident  a, b planar refs  -> Axis (180° opposed / 0° aligned) + PointInPlane(0)
  distance    a, b planar refs  -> Axis + PointInPlane(value): a lies `value` mm
                                   off b along b's normal
  fixed       a = instance      -> Fixed

References are ``<instance>@<ref>`` where ref is one of
  H1 / H1#3     hole (or threaded hole, or round boss/pocket) axis; #k picks the
                k-th position (1-based) of a patterned feature
  axis / axis:X the part's own Z (or X / Y) axis through its origin
  XY / -XY      a datum plane through the part origin, normal +Z (- flips it);
                likewise XZ (+Y) and YZ (+X)
  >Z, <Z[1] ... a CadQuery face selector, resolved at run time to the largest
                planar face it selects
A hole axis points along + of its face's axis (+Z for >Z, <Z and XY holes).
A mate that cannot be mapped (e.g. coincident between an axis and a plane, or
a face selector in a coaxial mate) is refused with exit 4, naming the mate.

This tool is stdlib-only; only the script it writes imports CadQuery.

Exit codes: 0 written · 2 spec invalid · 3 open questions · 4 unmappable mate · 64 usage.
"""

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cadquery_codegen import generate as generate_part  # noqa: E402
from part_spec_validator import (  # noqa: E402
    DATUMS, SAMPLE_SPEC, SELECTOR_RE, _num, expand_positions, face_axis_point, load_json, validate,
)

SCHEMA = "assembly-spec.v1"
NAME_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
DATUM_NORMALS = {"XY": (0.0, 0.0, 1.0), "XZ": (0.0, 1.0, 0.0), "YZ": (1.0, 0.0, 0.0)}
MATE_TYPES = {"coaxial", "coincident", "distance", "fixed"}
ORIENTATIONS = {"aligned", "opposed"}
DEFAULT_ORIENTATION = {"coaxial": "aligned", "coincident": "opposed", "distance": "opposed"}
# ГОСТ 2.106-2019 п. 5.1.3: order of sections in a specification.
SECTION_ORDER = ["Документация", "Комплексы", "Сборочные единицы", "Детали",
                 "Стандартные изделия", "Прочие изделия", "Материалы", "Комплекты"]
COMPONENT_SECTIONS = {"Сборочные единицы", "Детали", "Стандартные изделия", "Прочие изделия", "Материалы"}
SECTION_COLORS = {  # RGB 0..1 defaults per section when a component gives no colour
    "Сборочные единицы": (0.45, 0.55, 0.70),
    "Детали": (0.72, 0.74, 0.78),
    "Стандартные изделия": (0.85, 0.65, 0.20),
    "Прочие изделия": (0.40, 0.65, 0.45),
    "Материалы": (0.60, 0.50, 0.40),
}
STANDARD_HINT = re.compile(r"ГОСТ|ОСТ|ТУ|ISO|DIN|EN\b|GOST", re.I)
SPEC_COLUMNS = ["Формат", "Зона", "Поз.", "Обозначение", "Наименование", "Кол.", "Примечание"]
MAX_INSTANCES = 500  # interference is pairwise; keep the check tractable


class MateError(Exception):
    """A mate that is well-formed but cannot be expressed as cq.Assembly constraints."""


class Report:
    def __init__(self):
        self.errors, self.warnings, self.unmappable = [], [], []

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def warn(self, where, msg):
        self.warnings.append(f"{where}: {msg}")


# ---------------------------------------------------------------- sample

BOLT_SAMPLE = {
    "schema": "part-spec.v1", "part": "bolt_m8x30", "units": "mm",
    "material": "Сталь 35 ГОСТ 1050-2013",
    "drawing": {"title": "Болт М8-6gx30.58 ГОСТ 7798-70 (упрощённо)", "number": "ГОСТ 7798-70"},
    "base": {"type": "extrude", "profile": {"shape": "regular_polygon", "sides": 6, "across_flats": 13},
             "height": 5.5},
    "features": [
        {"id": "B1", "type": "boss", "face": "<Z", "at": [0, 0], "profile": {"shape": "circle", "diameter": 8},
         "height": 30},
        {"id": "T1", "type": "thread", "on": "B1", "designation": "M8-6g", "length": 22},
    ],
    "expect": {"bbox_mm": [15.011, 13, 35.5], "tolerance_mm": 0.05},
    "open_questions": [],
}

SAMPLE_ASSEMBLY = {
    "schema": SCHEMA,
    "assembly": "flange_bolted",
    "drawing": {"title": "Фланец с болтами (sample)", "number": "DEMO-100"},
    "components": [
        {"id": "flange", "part_spec": SAMPLE_SPEC,
         "instances": [{"name": "flange"}],
         "bom": {"section": "Детали", "designation": "DEMO-001", "name": "Плита фланцевая", "format": "A3"}},
        {"id": "bolt", "part_spec": BOLT_SAMPLE,
         "instances": [{"name": f"bolt_{i + 1}", "placement": {"translate": [x, y, 25]}}
                       for i, (x, y) in enumerate([(-48, -28), (-48, 28), (48, -28), (48, 28)])],
         "bom": {"section": "Стандартные изделия", "name": "Болт М8-6gx30.58 ГОСТ 7798-70"}},
    ],
    "mates": (
        [{"type": "fixed", "a": "flange"}]
        + [m for i in range(4) for m in (
            {"type": "coaxial", "a": f"bolt_{i + 1}@axis", "b": f"flange@H1#{i + 1}"},
            {"type": "coincident", "a": f"bolt_{i + 1}@-XY", "b": "flange@>Z"})]
    ),
    "expect": {"bbox_mm": [120, 80, 35.5], "tolerance_mm": 0.05},
    "open_questions": [],
}


# ---------------------------------------------------------------- small helpers

def _f(v):
    v = float(v)
    return repr(int(v)) if v.is_integer() else repr(round(v, 6))


def _vec3(v):
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(_num(c) for c in v)


def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _rot(axis, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "X":
        return [[1, 0, 0], [0, c, -s], [0, s, c]]
    if axis == "Y":
        return [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


def placement_matrix(placement):
    """(translate, 3x3 rotation) for a placement: rotations in listed order, then translate."""
    m = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    for axis, deg in (placement or {}).get("rotate") or []:
        m = _matmul(_rot(axis, deg), m)
    t = [float(c) for c in ((placement or {}).get("translate") or [0, 0, 0])]
    return t, [[round(c, 9) + 0.0 for c in row] for row in m]


def _natural(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s or "")]


# ---------------------------------------------------------------- reference resolution

def resolve_ref(part_spec, ref):
    """Classify a mate reference against a part spec.

    Returns a marker dict: {"kind": "axial"|"planar", "point": [..], "dir": [..]}
    (static, part-local coordinates) or {"kind": "planar", "selector": s} (resolved
    on the built solid at run time). Raises ValueError for an unknown/ambiguous
    reference and MateError for a known feature that has no axis or plane to mate.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("empty reference")
    ref = ref.strip()
    m = re.fullmatch(r"axis(?::([XYZ]))?", ref)
    if m:
        return {"kind": "axial", "point": [0.0, 0.0, 0.0], "dir": list(AXES[m.group(1) or "Z"]),
                "ref": ref}
    m = re.fullmatch(r"(-?)(XY|XZ|YZ)", ref)
    if m:
        n = DATUM_NORMALS[m.group(2)]
        sign = -1.0 if m.group(1) else 1.0
        return {"kind": "planar", "point": [0.0, 0.0, 0.0], "dir": [sign * c + 0.0 for c in n], "ref": ref}
    feats = {f.get("id"): f for f in part_spec.get("features", []) if isinstance(f, dict)}
    m = re.fullmatch(r"(.+?)(?:#(\d+))?", ref)
    fid, idx = m.group(1), m.group(2)
    if fid in feats:
        f = feats[fid]
        if f.get("type") == "thread":
            f = feats.get(f.get("on"), {})
        t = f.get("type")
        if t == "hole":
            pts = expand_positions(f)
        elif t in {"boss", "pocket"} and (f.get("profile") or {}).get("shape") == "circle":
            pts = [tuple(f.get("at") or [0, 0])]
        else:
            raise MateError(f"feature {fid!r} is a {t or 'feature'} with no single axis; "
                            "mate on a hole, a round boss/pocket, 'axis', a datum or a face selector")
        if idx is None:
            if len(pts) != 1:
                raise ValueError(f"{fid} has {len(pts)} positions; say which one, e.g. {fid}#1 .. {fid}#{len(pts)}")
            k = 0
        else:
            k = int(idx) - 1
            if not 0 <= k < len(pts):
                raise ValueError(f"{fid}#{idx}: {fid} has positions 1..{len(pts)}")
        point, direction = face_axis_point(f.get("face"), pts[k][0], pts[k][1])
        return {"kind": "axial", "point": [float(c) for c in point], "dir": list(direction), "ref": ref}
    if ref[0] in "<>|#%" and SELECTOR_RE.match(ref):
        return {"kind": "planar", "selector": ref, "ref": ref}
    raise ValueError(f"unknown reference {ref!r}: not a feature id of {part_spec.get('part')!r}, "
                     "'axis', a datum (XY, -XZ ...) or a face selector (>Z, <X[1] ...)")


def map_mate(mate, a, b):
    """cq.Assembly constraints for one mate, as [(kind, a_marker_use, b_marker_use, param)]."""
    t = mate["type"]
    if t == "fixed":
        return [("Fixed", None, None, None)]
    orient = mate.get("orientation") or DEFAULT_ORIENTATION[t]
    angle = 0 if orient == "aligned" else 180
    if t == "coaxial":
        if a["kind"] != "axial" or b["kind"] != "axial":
            raise MateError("coaxial needs two axes (hole/boss ids, 'axis'); "
                            f"got {a['kind']} {a['ref']!r} and {b['kind']} {b['ref']!r}")
        return [("Axis", "line", "line", angle), ("PointOnLine", "point", "line", None)]
    if a["kind"] != "planar" or b["kind"] != "planar":
        raise MateError(f"{t} needs two planes (datums XY/-XY..., face selectors >Z...); "
                        f"got {a['kind']} {a['ref']!r} and {b['kind']} {b['ref']!r}")
    value = float(mate.get("value") or 0) if t == "distance" else 0.0
    return [("Axis", "line", "line", angle), ("PointInPlane", "point", "plane", value)]


# ---------------------------------------------------------------- validation

def _load_part(comp, base_dir, where, rep):
    """(spec dict, source label, sha256 or None, rel path or None) for a component's part_spec."""
    ps = comp.get("part_spec")
    if isinstance(ps, dict):
        return json.loads(json.dumps(ps)), f"inline:{comp.get('id')}", None, None
    if not isinstance(ps, str) or not ps.strip():
        rep.err(where, "'part_spec' must be a relative path to a part-spec.v1 JSON (or an inline object)")
        return None, None, None, None
    if os.path.isabs(ps):
        rep.err(where, f"part_spec {ps!r} must be relative to the assembly spec, not absolute")
        return None, None, None, None
    path = os.path.normpath(os.path.join(base_dir, ps))
    spec, err = load_json(path)
    if err:
        rep.err(where, err)
        return None, None, None, None
    with open(path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    return spec, ps, sha, path


def _check_placement(rep, where, pl):
    if pl is None:
        return
    if not isinstance(pl, dict):
        rep.err(where, "'placement' must be {translate: [x, y, z], rotate: [[\"X|Y|Z\", deg], ...]}")
        return
    unknown = set(pl) - {"translate", "rotate"}
    if unknown:
        rep.err(where, f"placement has unknown keys {sorted(unknown)}")
    if "translate" in pl and not _vec3(pl["translate"]):
        rep.err(where, f"placement.translate must be [x, y, z] in mm, got {pl['translate']!r}")
    rot = pl.get("rotate")
    if rot is not None:
        if not isinstance(rot, list) or not all(
                isinstance(r, list) and len(r) == 2 and r[0] in AXES and _num(r[1]) for r in rot):
            rep.err(where, "placement.rotate must be a list of [\"X\"|\"Y\"|\"Z\", degrees] pairs")


def validate_assembly(asm, base_dir):
    """Validate an assembly spec and every part it references.

    Returns (rep, open_questions, model) where model holds the resolved parts,
    instances and mapped mates used by the generator.
    """
    rep = Report()
    open_q = []
    model = {"parts": {}, "components": [], "instances": [], "mates": []}
    if not isinstance(asm, dict):
        rep.err("spec", "top level must be a JSON object")
        return rep, open_q, model
    if asm.get("schema") != SCHEMA:
        rep.err("schema", f"must be {SCHEMA!r}")
    name = asm.get("assembly")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        rep.err("assembly", "must be a file-safe name: letters, digits, '_' or '-' (max 64)")
    drw = asm.get("drawing")
    if not isinstance(drw, dict):
        rep.err("drawing", "must be an object {title, number}: the assembly drawing's title block")
    else:
        if not drw.get("title"):
            rep.err("drawing", "'title' (наименование сборки) is required")
        if not drw.get("number"):
            rep.warn("drawing", "no 'number' (обозначение): the specification's Документация line will be omitted")

    comps = asm.get("components")
    if not isinstance(comps, list) or not comps:
        rep.err("components", "must be a non-empty list")
        comps = []
    comp_ids, inst_names, part_src = set(), {}, {}
    for ci, comp in enumerate(comps):
        where = f"components[{ci}]"
        if not isinstance(comp, dict):
            rep.err(where, "must be an object")
            continue
        cid = comp.get("id")
        if not isinstance(cid, str) or not NAME_RE.fullmatch(cid):
            rep.err(where, "'id' must be a file-safe name")
            cid = None
        elif cid in comp_ids:
            rep.err(where, f"duplicate component id {cid!r}")
        else:
            comp_ids.add(cid)
            where = f"components[{ci}] ({cid})"

        bom = comp.get("bom")
        if not isinstance(bom, dict):
            rep.err(where, "'bom' object {section, designation, name, note} is required")
            bom = {}
        section = bom.get("section")
        if section not in COMPONENT_SECTIONS:
            rep.err(where, f"bom.section must be one of {[s for s in SECTION_ORDER if s in COMPONENT_SECTIONS]}, got {section!r}")
        if not isinstance(bom.get("name"), str) or not bom["name"].strip():
            rep.err(where, "bom.name (Наименование) is required")
        if section in {"Детали", "Сборочные единицы"} and not bom.get("designation"):
            rep.err(where, f"bom.designation (Обозначение) is required in section {section!r} (ГОСТ 2.106)")
        if section == "Стандартные изделия" and isinstance(bom.get("name"), str) and not STANDARD_HINT.search(bom["name"]):
            rep.warn(where, "standard item name carries no standard number (ГОСТ/ОСТ/ТУ/ISO); "
                            "write it in full, e.g. 'Болт М8-6gx30.58 ГОСТ 7798-70'")
        for k in ("designation", "name", "note", "format", "zone"):
            if bom.get(k) is not None and not isinstance(bom[k], str):
                rep.err(where, f"bom.{k} must be a string")

        insts = comp.get("instances")
        qty = comp.get("qty")
        part = None
        if comp.get("part_spec") is not None:
            spec, src, sha, path = _load_part(comp, base_dir, where, rep)
            if spec is not None:
                prep, pq = validate(spec)
                for e in prep.errors:
                    rep.err(f"{where} part {src}", e)
                for w in prep.warnings:
                    rep.warn(f"{where} part {src}", w)
                open_q += [f"{cid} ({src}): {q}" for q in pq]
                if not prep.errors:
                    part = spec["part"]
                    key = path or src
                    if part in part_src and part_src[part] != key:
                        rep.err(where, f"part name {part!r} is already used by {part_src[part]}; part names must be unique")
                    else:
                        part_src[part] = key
                        model["parts"].setdefault(part, {"spec": spec, "spec_file": src if path else None,
                                                          "abs_path": path, "sha256": sha})
        if insts is not None:
            if comp.get("part_spec") is None:
                rep.err(where, "placed 'instances' need a 'part_spec' to build")
            if not isinstance(insts, list) or not insts:
                rep.err(where, "'instances' must be a non-empty list of {name, placement}")
                insts = []
            if qty is not None and (not isinstance(qty, int) or isinstance(qty, bool) or qty != len(insts)):
                rep.err(where, f"qty {qty!r} disagrees with {len(insts)} instances; give one or make them match")
            for ii, inst in enumerate(insts):
                iw = f"{where}.instances[{ii}]"
                if not isinstance(inst, dict):
                    rep.err(iw, "must be an object {name, placement}")
                    continue
                iname = inst.get("name")
                if not isinstance(iname, str) or not NAME_RE.fullmatch(iname):
                    rep.err(iw, "'name' must be a file-safe name (letters, digits, '_' or '-')")
                    continue
                if iname in inst_names:
                    rep.err(iw, f"duplicate instance name {iname!r} (also in {inst_names[iname]})")
                    continue
                inst_names[iname] = cid
                _check_placement(rep, iw, inst.get("placement"))
                model["instances"].append({"name": iname, "component": cid, "part": part,
                                           "placement": inst.get("placement") or {}})
            count = len(insts)
        else:
            if qty is None:
                rep.err(where, "give 'instances' (placed in 3D) or 'qty' (specification line only)")
                count = 0
            elif section == "Материалы" and (isinstance(qty, str) and qty.strip() or _num(qty) and qty > 0):
                count = qty
            elif not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                rep.err(where, f"qty must be a positive integer, got {qty!r}")
                count = 0
            else:
                count = qty
            if comp.get("part_spec") is not None:
                rep.warn(where, "has a part_spec but no instances: listed in the specification, not placed in 3D")
        color = comp.get("color")
        if color is not None and not (_vec3(color) and all(0 <= c <= 1 for c in color)):
            rep.err(where, "color must be [r, g, b] with components 0..1")
            color = None
        model["components"].append({"id": cid, "part": part, "bom": bom, "count": count,
                                    "color": list(color or SECTION_COLORS.get(section, (0.7, 0.7, 0.7)))})
    if len(model["instances"]) > MAX_INSTANCES:
        rep.err("components", f"{len(model['instances'])} placed instances is more than {MAX_INSTANCES}")

    # ---- mates
    mates = asm.get("mates") or []
    if not isinstance(mates, list):
        rep.err("mates", "must be a list")
        mates = []
    inst_part = {i["name"]: i["part"] for i in model["instances"]}
    has_fixed = False
    mated = []
    for mi, mate in enumerate(mates):
        where = f"mates[{mi}]"
        if not isinstance(mate, dict):
            rep.err(where, "must be an object {type, a, b, value}")
            continue
        t = mate.get("type")
        if not isinstance(t, str) or t not in MATE_TYPES:
            rep.err(where, f"type must be one of {sorted(MATE_TYPES)}, got {t!r}")
            continue
        label = f"{where} {t} {mate.get('a')}" + (f" ~ {mate.get('b')}" if mate.get("b") else "")
        orient = mate.get("orientation")
        if orient is not None and (t == "fixed" or orient not in ORIENTATIONS):
            rep.err(where, f"orientation must be one of {sorted(ORIENTATIONS)} (not for 'fixed'), got {orient!r}")
        if t == "distance":
            if not _num(mate.get("value")) or mate["value"] < 0:
                rep.err(where, f"distance needs 'value' >= 0 mm, got {mate.get('value')!r}")
        elif mate.get("value") is not None:
            rep.err(where, f"{t} takes no 'value'")
        sides = ["a"] if t == "fixed" else ["a", "b"]
        if t == "fixed" and mate.get("b") is not None:
            rep.err(where, "fixed takes only 'a' (an instance name)")
        markers, ok = {}, True
        for side in sides:
            r = mate.get(side)
            if not isinstance(r, str) or not r:
                rep.err(where, f"'{side}' is required: \"<instance>@<ref>\"")
                ok = False
                continue
            inst, _, ref = r.partition("@")
            if inst not in inst_part:
                rep.err(where, f"'{side}': unknown instance {inst!r}")
                ok = False
                continue
            if t == "fixed":
                if ref:
                    rep.err(where, "fixed takes a bare instance name, without '@ref'")
                    ok = False
                markers[side] = {"inst": inst}
                continue
            if not ref:
                rep.err(where, f"'{side}' needs a reference after '@', e.g. {inst}@axis or {inst}@>Z")
                ok = False
                continue
            if inst_part[inst] is None:
                ok = False
                continue
            try:
                mk = resolve_ref(model["parts"][inst_part[inst]]["spec"], ref)
            except ValueError as e:
                rep.err(where, f"'{side}': {e}")
                ok = False
                continue
            except MateError as e:
                rep.unmappable.append(f"{label}: '{side}': {e}")
                ok = False
                continue
            mk["inst"] = inst
            markers[side] = mk
        if not ok:
            continue
        if t != "fixed" and markers["a"]["inst"] == markers["b"]["inst"]:
            rep.err(where, "a mate joins two different instances")
            continue
        try:
            cons = map_mate(mate, markers.get("a"), markers.get("b"))
        except MateError as e:
            rep.unmappable.append(f"{label}: {e}")
            continue
        has_fixed |= t == "fixed"
        mated += [markers[s]["inst"] for s in sides]
        model["mates"].append({"label": label, "type": t, "orientation": mate.get("orientation") or DEFAULT_ORIENTATION.get(t),
                               "value": mate.get("value"), "a": markers["a"], "b": markers.get("b"),
                               "constraints": cons})
    if model["mates"] and not has_fixed:
        first = model["mates"][0]["a"]["inst"]
        rep.warn("mates", f"no 'fixed' mate: the solver holds {first!r} (the first mated instance) where it is placed")
    for m in model["mates"]:
        m["a"].setdefault("ref", None)
    model["mated"] = sorted(set(mated))

    exp = asm.get("expect")
    if exp is None:
        rep.warn("expect", "no expect.bbox_mm: the overall size of the assembly is not checked")
    elif not isinstance(exp, dict) or not (isinstance(exp.get("bbox_mm"), list) and len(exp["bbox_mm"]) == 3
                                            and all(_num(c) and c > 0 for c in exp["bbox_mm"])):
        rep.err("expect", "expect.bbox_mm must be [X, Y, Z] overall sizes in mm (the gabarit dimensions)")
    elif exp.get("tolerance_mm") is not None and (not _num(exp["tolerance_mm"]) or exp["tolerance_mm"] <= 0):
        rep.err("expect.tolerance_mm", "must be a positive number in mm")

    oq = asm.get("open_questions") or []
    if isinstance(oq, str):
        oq = [oq]
    if not isinstance(oq, list):
        rep.err("open_questions", "must be a list of strings")
        oq = []
    open_q = [f"assembly: {q}" if isinstance(q, str) else json.dumps(q, ensure_ascii=False) for q in oq if q] + open_q
    return rep, open_q, model


# ---------------------------------------------------------------- specification (ГОСТ 2.106)

def build_bom(asm, model):
    """Specification rows in ГОСТ 2.106 order with automatic positions.

    Returns a list of rows: {"kind": "section"|"item", ...}. Документация gets
    the assembly drawing (обозначение + ' СБ'), without a position. Within
    Сборочные единицы / Детали lines are ordered by обозначение, other sections
    by наименование; positions run 1, 2, 3 ... through the sections.
    """
    rows = []
    drw = asm.get("drawing") or {}
    by_section = {s: [] for s in SECTION_ORDER}
    if drw.get("number"):
        by_section["Документация"].append({
            "format": drw.get("format", ""), "zone": "", "designation": f"{drw['number']} СБ",
            "name": "Сборочный чертеж", "qty": "", "note": "", "component": None})
    for order, c in enumerate(model["components"]):
        b = c["bom"]
        by_section.setdefault(b.get("section"), []).append({
            "format": b.get("format", ""), "zone": b.get("zone", ""), "designation": b.get("designation", ""),
            "name": b.get("name", ""), "qty": c["count"], "note": b.get("note", ""),
            "component": c["id"], "_order": order})
    pos = 0
    for s in SECTION_ORDER:
        items = by_section.get(s) or []
        if not items:
            continue
        if s in {"Сборочные единицы", "Детали"}:
            items.sort(key=lambda r: (_natural(r["designation"]), r.get("_order", 0)))
        elif s != "Документация":
            items.sort(key=lambda r: (_natural(r["name"]), r.get("_order", 0)))
        rows.append({"kind": "section", "name": s})
        for r in items:
            r.pop("_order", None)
            if s != "Документация":
                pos += 1
                r["pos"] = pos
            else:
                r["pos"] = ""
            r["kind"] = "item"
            rows.append(r)
    return rows


def bom_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(SPEC_COLUMNS)
    for r in rows:
        if r["kind"] == "section":
            w.writerow(["", "", "", "", r["name"], "", ""])
        else:
            w.writerow([r["format"], r["zone"], r["pos"], r["designation"], r["name"], r["qty"], r["note"]])
    return buf.getvalue()


def bom_md(asm, rows):
    drw = asm.get("drawing") or {}
    esc = lambda s: str(s).replace("|", "\\|")  # noqa: E731
    out = [f"# Спецификация — {drw.get('title', asm.get('assembly'))}", ""]
    if drw.get("number"):
        out += [f"Обозначение: **{drw['number']}**", ""]
    out += ["| " + " | ".join(SPEC_COLUMNS) + " |", "|" + "|".join([":-:", ":-:", ":-:", ":--", ":--", ":-:", ":--"]) + "|"]
    for r in rows:
        if r["kind"] == "section":
            out.append(f"|  |  |  |  | **<u>{esc(r['name'])}</u>** |  |  |")
        else:
            out.append("| " + " | ".join(esc(r[k]) for k in ("format", "zone", "pos", "designation", "name", "qty", "note")) + " |")
    out += ["", "Колонки и порядок разделов — ГОСТ 2.106-2019 (форма 1). Позиции присвоены автоматически "
                "сквозной нумерацией по разделам; резервные позиции не оставлены."]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- code generation

TEMPLATE = r'''#!/usr/bin/env python3
"""__ASSEMBLY__ — assembly generated from __DRAWING__.

Generated by drawing-to-3d/assembly_codegen.py from __SPEC_NAME__. Edit the
assembly or part specs and regenerate rather than hand-editing this file.

    pip install cadquery
    python __SCRIPT_NAME__                 # writes STEP, STL, report and спецификация
    python __SCRIPT_NAME__ --out build/    # choose output folder
    python __SCRIPT_NAME__ --emit-parts    # also write each embedded <part>_cad.py

Each part's CadQuery script (made by cadquery_codegen.py) is embedded below in
PARTS and executed into its own module, so this file runs on its own.
Exit: 0 assembled cleanly · 1 clash, unsolved mate, invalid part or size mismatch.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import sys
import types

import cadquery as cq

ASSEMBLY = __ASSEMBLY_R__
SPEC = __SPEC__
PARTS = __PARTS__
COMPONENTS = __COMPONENTS__
INSTANCES = __INSTANCES__
MATES = __MATES__
BOM = __BOM__
SPEC_COLUMNS = __COLUMNS__
CLASH_MM3 = 0.001          # shared volume above this is a clash
MATE_TOL_MM = 1e-3         # a solved mate must hold to this distance ...
MATE_TOL_DEG = 0.01        # ... and this angle
HERE = os.path.dirname(os.path.abspath(__file__))

_modules, _shapes = {}, {}


def part_module(part):
    """Execute the embedded part script into its own module (once)."""
    if part not in _modules:
        mod = types.ModuleType(f"part_{part}")
        mod.__file__ = os.path.join(HERE, f"{part}_cad.py")
        exec(compile(PARTS[part]["script"], f"<{part}_cad.py>", "exec"), mod.__dict__)
        _modules[part] = mod
    return _modules[part]


def part_shape(part):
    if part not in _shapes:
        wp = part_module(part).build()
        _shapes[part] = (wp.val(), part_module(part).measure(wp))
    return _shapes[part]


def stale_parts():
    """Part specs that changed on disk since this script was generated."""
    out = []
    for part, p in PARTS.items():
        if not p.get("spec_file") or not p.get("sha256"):
            continue
        path = os.path.join(HERE, p["spec_file"])
        if os.path.exists(path):
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != p["sha256"]:
                    out.append(p["spec_file"])
    return out


def placement(pl):
    """cq.Location: rotations about global axes in listed order, then translate."""
    loc = cq.Location()
    for axis, deg in pl.get("rotate") or []:
        loc = cq.Location(cq.Vector(0, 0, 0), cq.Vector(*{"X": (1, 0, 0), "Y": (0, 1, 0), "Z": (0, 0, 1)}[axis]), deg) * loc
    return cq.Location(cq.Vector(*(pl.get("translate") or (0, 0, 0)))) * loc


def marker(part, mk):
    """(point, direction) of a mate reference, in the part's own coordinates."""
    if "selector" in mk:
        shape = part_shape(part)[0]
        faces = [f for f in cq.Workplane().add(shape).faces(mk["selector"]).vals() if f.geomType() == "PLANE"]
        if not faces:
            raise RuntimeError(f"{mk['inst']}@{mk['selector']}: the selector picks no planar face")
        face = max(faces, key=lambda f: f.Area())
        c, n = face.Center(), face.normalAt(face.Center())
        off = [f for f in faces if abs((f.Center() - c).dot(n)) > 1e-6 or f.normalAt(f.Center()).dot(n) < 1 - 1e-9]
        if off:  # a selector that is not one plane would silently mate the wrong face
            raise RuntimeError(f"{mk['inst']}@{mk['selector']}: the selector picks {len(faces)} faces on "
                               "different planes; use a more specific selector or a datum (XY, -XY ...)")
        return c, n
    return cq.Vector(*mk["point"]), cq.Vector(*mk["dir"])


def _entity(use, p, d):
    if use == "point":
        return cq.Vertex.makeVertex(p.x, p.y, p.z)
    if use == "line":
        return cq.Edge.makeLine(p - d * 10, p + d * 10)
    return cq.Face.makePlane(20, 20, p, d)


def build():
    """The cq.Assembly with every instance placed (and mates solved)."""
    assy = cq.Assembly(name=ASSEMBLY)
    for inst in INSTANCES:
        shape, _ = part_shape(inst["part"])
        assy.add(shape, name=inst["name"], loc=placement(inst["placement"]),
                 color=cq.Color(*inst["color"], 1.0))
    if MATES:
        part_of = {i["name"]: i["part"] for i in INSTANCES}
        for m in MATES:
            for kind, ua, ub, param in m["constraints"]:
                try:
                    if kind == "Fixed":
                        assy.constrain(m["a"]["inst"], "Fixed")
                        continue
                    pa, da = marker(part_of[m["a"]["inst"]], m["a"])
                    pb, db = marker(part_of[m["b"]["inst"]], m["b"])
                    args = [m["a"]["inst"], _entity(ua, pa, da), m["b"]["inst"], _entity(ub, pb, db), kind]
                    assy.constrain(*args, param=param) if param is not None else assy.constrain(*args)
                except Exception as exc:
                    raise RuntimeError(f"mate {m['label']} ({kind}) failed: {exc}") from exc
        assy.solve()
    return assy


def world_shape(assy, name):
    return assy.objects[name].obj.moved(assy.objects[name].loc)


def check_mates(assy):
    """Residual of every mate after the solve, in world coordinates."""
    part_of = {i["name"]: i["part"] for i in INSTANCES}
    out = []
    for m in MATES:
        if m["type"] == "fixed":
            out.append({"mate": m["label"], "ok": True})
            continue
        res = {}
        locs = [assy.objects[m[s]["inst"]].loc for s in ("a", "b")]
        (pa, da), (pb, db) = (marker(part_of[m[s]["inst"]], m[s]) for s in ("a", "b"))
        pa, pb = (cq.Vertex.makeVertex(p.x, p.y, p.z).moved(l).Center() for p, l in ((pa, locs[0]), (pb, locs[1])))
        o = cq.Vertex.makeVertex(0, 0, 0)
        da, db = ((cq.Vertex.makeVertex(d.x, d.y, d.z).moved(l).Center() - o.moved(l).Center()).normalized()
                  for d, l in ((da, locs[0]), (db, locs[1])))
        want = 0.0 if m["orientation"] == "aligned" else 180.0
        ang = math.degrees(math.acos(max(-1.0, min(1.0, da.dot(db)))))
        res["angle_error_deg"] = round(abs(ang - want), 6)
        if m["type"] == "coaxial":
            v = pa - pb
            res["offset_mm"] = round((v - db * v.dot(db)).Length, 6)
        else:
            res["offset_mm"] = round(abs((pa - pb).dot(db) - float(m.get("value") or 0)), 6)
        res["ok"] = res["offset_mm"] <= MATE_TOL_MM and res["angle_error_deg"] <= MATE_TOL_DEG
        out.append({"mate": m["label"], **res})
    return out


def _bb_overlap(a, b, eps=1e-6):
    return (a.xmin < b.xmax - eps and b.xmin < a.xmax - eps and a.ymin < b.ymax - eps and
            b.ymin < a.ymax - eps and a.zmin < b.zmax - eps and b.zmin < a.zmax - eps)


def interference(assy):
    """Pairwise solid intersection. Shared volume > CLASH_MM3 is a clash; touching is a contact."""
    names = [i["name"] for i in INSTANCES]
    shapes = {n: world_shape(assy, n) for n in names}
    boxes = {n: s.BoundingBox() for n, s in shapes.items()}
    clashes, contacts = [], []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not _bb_overlap(boxes[a], boxes[b]) and shapes[a].distance(shapes[b]) > 1e-6:
                continue
            vol = shapes[a].intersect(shapes[b]).Volume()
            if vol > CLASH_MM3:
                clashes.append({"a": a, "b": b, "volume_mm3": round(vol, 4)})
            elif shapes[a].distance(shapes[b]) <= 1e-4:
                contacts.append({"a": a, "b": b})
    return clashes, contacts


def _loc_report(loc):
    t = loc.wrapped.Transformation()
    tr = t.TranslationPart()
    return {"translate_mm": [round(tr.X(), 4) + 0.0, round(tr.Y(), 4) + 0.0, round(tr.Z(), 4) + 0.0],
            "rotation_matrix": [[round(t.Value(r, c), 6) + 0.0 for c in (1, 2, 3)] for r in (1, 2, 3)]}


def write_spec(base):
    with open(f"{base}_spec.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(SPEC_COLUMNS)
        for r in BOM:
            if r["kind"] == "section":
                w.writerow(["", "", "", "", r["name"], "", ""])
            else:
                w.writerow([r["format"], r["zone"], r["pos"], r["designation"], r["name"], r["qty"], r["note"]])
    with open(f"{base}_spec.md", "w", encoding="utf-8") as fh:
        fh.write(SPEC_MD)
    return [f"{base}_spec.csv", f"{base}_spec.md"]


SPEC_MD = __SPEC_MD__


def main(argv=None):
    ap = argparse.ArgumentParser(description=f"Build assembly {ASSEMBLY}: STEP/STL, report, спецификация.")
    ap.add_argument("--out", default=".", help="output folder (default: current)")
    ap.add_argument("--stl-tolerance", type=float, default=0.05, help="STL chord tolerance, mm")
    ap.add_argument("--emit-parts", action="store_true", help="also write each embedded <part>_cad.py")
    ap.add_argument("--no-interference", action="store_true", help="skip the pairwise clash check")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    base = os.path.join(a.out, ASSEMBLY)
    for s in stale_parts():
        print(f"WARNING part spec {s} changed since this script was generated: regenerate it", file=sys.stderr)
    if a.emit_parts:
        for part, p in PARTS.items():
            with open(os.path.join(a.out, f"{part}_cad.py"), "w", encoding="utf-8") as fh:
                fh.write(p["script"])
            print(f"wrote {os.path.join(a.out, part + '_cad.py')}")

    assy = build()
    mates = check_mates(assy)
    clashes, contacts = ([], []) if a.no_interference else interference(assy)
    compound = assy.toCompound()
    bb = compound.BoundingBox()
    parts = {}
    for part in sorted({i["part"] for i in INSTANCES}):
        m = part_shape(part)[1]
        parts[part] = {k: m[k] for k in ("valid", "solids", "bbox_mm", "volume_mm3")}
    pos_of = {r.get("component"): r.get("pos") for r in BOM if r["kind"] == "item"}
    report = {
        "assembly": ASSEMBLY,
        "bbox_mm": [round(bb.xlen, 4), round(bb.ylen, 4), round(bb.zlen, 4)],
        "bbox_min_mm": [round(bb.xmin, 4), round(bb.ymin, 4), round(bb.zmin, 4)],
        "instances": [{"name": i["name"], "component": i["component"], "part": i["part"],
                       "pos": pos_of.get(i["component"]), **_loc_report(assy.objects[i["name"]].loc)}
                      for i in INSTANCES],
        "parts": parts,
        "mates": mates,
        "interference": {"checked": not a.no_interference, "threshold_mm3": CLASH_MM3,
                         "clashes": clashes, "contacts": contacts},
    }
    problems = []
    if any(not p["valid"] for p in parts.values()):
        problems.append("invalid part solid")
    if any(not m["ok"] for m in mates):
        problems.append("mate not satisfied after solve")
    if clashes:
        problems.append(f"{len(clashes)} clash(es)")
    exp = SPEC.get("expect") or {}
    if exp.get("bbox_mm"):
        tol = exp.get("tolerance_mm", 0.05)
        dev = [round(m - e, 4) for m, e in zip(report["bbox_mm"], exp["bbox_mm"])]
        report["expect"] = {"bbox_mm": exp["bbox_mm"], "tolerance_mm": tol, "deviation_mm": dev,
                            "ok": all(abs(d) <= tol for d in dev)}
        if not report["expect"]["ok"]:
            problems.append(f"bbox {report['bbox_mm']} != expected {exp['bbox_mm']} ± {tol}")
    report["ok"], report["problems"] = not problems, problems

    assy.export(f"{base}.step")
    print(f"wrote {base}.step")
    cq.exporters.export(compound, f"{base}.stl", tolerance=a.stl_tolerance, angularTolerance=0.1)
    print(f"wrote {base}.stl")
    with open(f"{base}.report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    print(f"wrote {base}.report.json")
    for p in write_spec(base):
        print(f"wrote {p}")
    bx, by, bz = report["bbox_mm"]
    print(f"bbox {bx} x {by} x {bz} mm   instances {len(INSTANCES)}   mates {len(mates)}   "
          f"clashes {len(clashes)}   contacts {len(contacts)}")
    for c in clashes:
        print(f"  CLASH {c['a']} x {c['b']}: {c['volume_mm3']} mm3")
    for m in mates:
        if not m["ok"]:
            print(f"  MATE NOT SATISFIED {m['mate']}: offset {m.get('offset_mm')} mm, angle {m.get('angle_error_deg')} deg")
    for p in problems:
        print(f"  PROBLEM {p}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
elif "show_object" in globals():  # CQ-editor
    show_object(build(), name=ASSEMBLY)  # noqa: F821
'''


def generate(asm, model, spec_name, script_dir):
    """Return the text of <assembly>_asm.py. script_dir: where the script will live (for spec paths)."""
    parts = {}
    for part, p in model["parts"].items():
        rel = os.path.relpath(p["abs_path"], script_dir) if p["abs_path"] else None
        parts[part] = {"spec_file": rel, "sha256": p["sha256"],
                       "script": generate_part(p["spec"], p["spec_file"] or f"{part} (inline)")}
    color = {c["id"]: c["color"] for c in model["components"]}
    instances = [{"name": i["name"], "component": i["component"], "part": i["part"],
                  "placement": i["placement"], "color": color[i["component"]]} for i in model["instances"]]
    mates = [{k: m[k] for k in ("label", "type", "orientation", "value", "a", "b", "constraints")}
             for m in model["mates"]]
    rows = build_bom(asm, model)
    drw = asm.get("drawing") or {}
    drawing = " ".join(str(drw.get(k)) for k in ("number", "title") if drw.get(k)) or "(unnamed)"
    spec_public = {k: v for k, v in asm.items() if k != "components"}
    spec_public["components"] = [{k: v for k, v in c.items() if k != "part_spec"} for c in asm["components"]]
    subs = {
        "__ASSEMBLY__": asm["assembly"], "__DRAWING__": drawing,
        "__SPEC_NAME__": os.path.basename(spec_name), "__SCRIPT_NAME__": f"{asm['assembly']}_asm.py",
        "__ASSEMBLY_R__": repr(asm["assembly"]), "__SPEC__": repr(spec_public), "__PARTS__": repr(parts),
        "__COMPONENTS__": repr([{k: c[k] for k in ("id", "part", "count", "color")} for c in model["components"]]),
        "__INSTANCES__": repr(instances), "__MATES__": repr(mates), "__BOM__": repr(rows),
        "__COLUMNS__": repr(SPEC_COLUMNS), "__SPEC_MD__": repr(bom_md(asm, rows)),
    }
    out = TEMPLATE
    for k in sorted(subs, key=len, reverse=True):  # __ASSEMBLY_R__ before __ASSEMBLY__
        out = out.replace(k, subs[k])
    return out, rows


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a CadQuery assembly script (assembly STEP, STL, mate solve, clash check, "
                    "ГОСТ 2.106 specification) from an assembly-spec.v1 JSON.",
        epilog="Exit: 0 written · 2 spec invalid · 3 open questions · 4 unmappable mate · 64 usage")
    ap.add_argument("spec", nargs="?", help="assembly-spec.v1 JSON")
    ap.add_argument("--sample", action="store_true", help="use the built-in sample (flange + 4 bolts M8)")
    ap.add_argument("--print-sample", action="store_true", help="print the sample assembly spec and exit")
    ap.add_argument("--out", help="script path (default: <assembly>_asm.py next to the spec; '-' = stdout)")
    ap.add_argument("--bom-out", metavar="DIR",
                    help="also write <assembly>_spec.csv/.md to DIR now (no CadQuery needed)")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

    if a.print_sample:
        print(json.dumps(SAMPLE_ASSEMBLY, indent=2, ensure_ascii=False))
        return 0
    if a.sample:
        asm, spec_path = json.loads(json.dumps(SAMPLE_ASSEMBLY)), "sample"
        base_dir = os.getcwd()
    elif a.spec:
        asm, err = load_json(a.spec)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 64
        spec_path = a.spec
        base_dir = os.path.dirname(os.path.abspath(a.spec))
    else:
        ap.print_usage(sys.stderr)
        return 64

    rep, open_q, model = validate_assembly(asm, base_dir)
    if rep.errors:
        print("Refusing to generate: the assembly spec is invalid.", file=sys.stderr)
        for e in rep.errors:
            print(f"  ERROR {e}", file=sys.stderr)
        if a.output == "json":
            print(json.dumps({"verdict": "INVALID", "exit": 2, "errors": rep.errors, "warnings": rep.warnings,
                              "unmappable_mates": rep.unmappable}, indent=2, ensure_ascii=False))
        return 2
    if open_q:
        print("Refusing to generate: open questions remain:", file=sys.stderr)
        for q in open_q:
            print(f"  ? {q}", file=sys.stderr)
        if a.output == "json":
            print(json.dumps({"verdict": "OPEN-QUESTIONS", "exit": 3, "open_questions": open_q},
                             indent=2, ensure_ascii=False))
        return 3
    if rep.unmappable:
        print("Refusing to generate: these mates cannot be expressed as CadQuery assembly constraints:",
              file=sys.stderr)
        for u in rep.unmappable:
            print(f"  MATE {u}", file=sys.stderr)
        print("  Use coaxial for two axes, coincident/distance for two planes, or give an explicit placement.",
              file=sys.stderr)
        if a.output == "json":
            print(json.dumps({"verdict": "UNMAPPABLE-MATE", "exit": 4, "unmappable_mates": rep.unmappable},
                             indent=2, ensure_ascii=False))
        return 4

    to_stdout = a.out == "-" or (a.sample and not a.out)
    out = None if to_stdout else (a.out or os.path.join(base_dir, f"{asm['assembly']}_asm.py"))
    code, rows = generate(asm, model, spec_path, os.path.dirname(os.path.abspath(out)) if out else base_dir)
    written = []
    if a.bom_out:
        os.makedirs(a.bom_out, exist_ok=True)
        stem = os.path.join(a.bom_out, asm["assembly"])
        with open(f"{stem}_spec.csv", "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(bom_csv(rows))
        with open(f"{stem}_spec.md", "w", encoding="utf-8") as fh:
            fh.write(bom_md(asm, rows))
        written += [f"{stem}_spec.csv", f"{stem}_spec.md"]
    if to_stdout:
        sys.stdout.write(code)
        return 0
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(code)
    written.insert(0, out)

    placements = []
    for i in model["instances"]:
        t, r = placement_matrix(i["placement"])
        placements.append({"name": i["name"], "part": i["part"], "translate_mm": t, "rotation_matrix": r,
                           "mated": i["name"] in model["mated"]})
    if a.output == "json":
        print(json.dumps({"verdict": "WRITTEN", "exit": 0, "written": written, "parts": sorted(model["parts"]),
                          "instances": placements,
                          "mates": [{"mate": m["label"], "constraints": [c[0] for c in m["constraints"]]}
                                    for m in model["mates"]],
                          "specification": [r for r in rows], "warnings": rep.warnings},
                         indent=2, ensure_ascii=False))
    else:
        for w in written:
            print(f"wrote {w}")
        print(f"  parts {len(model['parts'])}   instances {len(model['instances'])}   mates {len(model['mates'])} "
              f"-> {sum(len(m['constraints']) for m in model['mates'])} constraints")
        for w in rep.warnings:
            print(f"  WARNING {w}")
        print(f"next: pip install cadquery && python {os.path.basename(out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
