#!/usr/bin/env python3
"""stl_viewer.py — wrap an STL in one self-contained HTML page for checking against the drawing.

Opens in any browser, no install. Drag to rotate, wheel to zoom, right-drag
(or Shift+drag) to pan. Buttons snap to the drawing's standard views — Front,
Top, Left, Right, Bottom, Back, Iso — and an Ortho toggle removes perspective,
so a view can be laid over the matching view on the drawing. Feature edges are
drawn so small details (chamfers, fillets, counterbores) stay readable, and the
overall size is printed in the corner.

View convention (matches part_spec coordinates): X right, Y back, Z up.
Front looks along +Y; Top looks down -Z. On a first-angle (ISO-E / ESKD)
sheet the Top view sits below the Front view; third-angle (ISO-A / ASME) puts
it above — the 3D model is the same either way.

Stdlib only; the page loads three.js r128 from cdnjs.
Exit codes: 0 written · 2 not a readable STL · 64 usage.
"""

import argparse
import base64
import html
import json
import os
import struct
import sys


def read_stl(data):
    """Return a flat list of float vertex coordinates (9 per triangle)."""
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + n * 50 == len(data):
            out = []
            for i in range(n):
                off = 84 + i * 50 + 12  # skip the facet normal
                out.extend(struct.unpack_from("<9f", data, off))
            return out
    text = data.decode("utf-8", errors="ignore")
    if "facet" not in text:
        raise ValueError("not an STL (neither binary nor ASCII)")
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == "vertex":
            out.extend(float(v) for v in parts[1:])
    if not out or len(out) % 9:
        raise ValueError("ASCII STL has no complete triangles")
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f4f4f2;--panel:#ffffffd9;--text:#1d1d1b;--muted:#6b6b66;--accent:#2f5d8a;--line:#d8d8d2}
@media (prefers-color-scheme:dark){:root{--bg:#18191b;--panel:#24262ad9;--text:#ecebe6;--muted:#a3a29b;--accent:#7fb0e0;--line:#3a3c40}}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,sans-serif;overflow:hidden}
#c{position:fixed;inset:0;touch-action:none}
.panel{position:fixed;left:12px;top:12px;right:12px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;
 background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;backdrop-filter:blur(6px)}
.panel b{margin-right:6px}
button{font:inherit;border:1px solid var(--line);background:transparent;color:var(--text);border-radius:6px;padding:4px 10px;cursor:pointer}
button:hover,button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
.info{position:fixed;left:12px;bottom:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 10px;color:var(--muted);font-variant-numeric:tabular-nums}
.info strong{color:var(--text)}
</style></head><body>
<canvas id="c"></canvas>
<div class="panel" role="toolbar" aria-label="Views">
 <b>__TITLE__</b>
 <button data-v="front">Front</button><button data-v="top">Top</button><button data-v="left">Left</button>
 <button data-v="right">Right</button><button data-v="back">Back</button><button data-v="bottom">Bottom</button>
 <button data-v="iso">Iso</button>
 <button id="ortho" aria-pressed="false">Ortho</button><button id="edges" aria-pressed="true">Edges</button>
</div>
<div class="info" id="info"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
const B64="__DATA__", META=__META__;
const bytes=Uint8Array.from(atob(B64),c=>c.charCodeAt(0));
const pos=new Float32Array(bytes.buffer);
const geo=new THREE.BufferGeometry();
geo.setAttribute('position',new THREE.BufferAttribute(pos,3));
geo.computeVertexNormals();geo.computeBoundingBox();
const bb=geo.boundingBox, size=new THREE.Vector3(), ctr=new THREE.Vector3();
bb.getSize(size);bb.getCenter(ctr);
const R=Math.max(size.x,size.y,size.z)||1;
const canvas=document.getElementById('c');
const renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
const scene=new THREE.Scene();
const dark=matchMedia('(prefers-color-scheme: dark)').matches;
const mat=new THREE.MeshStandardMaterial({color:dark?0x8fa6bd:0x7d95ad,metalness:.2,roughness:.65,
 polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1,side:THREE.DoubleSide});
const mesh=new THREE.Mesh(geo,mat);scene.add(mesh);
const edges=new THREE.LineSegments(new THREE.EdgesGeometry(geo,25),
 new THREE.LineBasicMaterial({color:dark?0xe8e8e8:0x1d2a36}));scene.add(edges);
scene.add(new THREE.HemisphereLight(0xffffff,0x3a3a3a,.55));
const sun=new THREE.DirectionalLight(0xffffff,.5);scene.add(sun);
const persp=new THREE.PerspectiveCamera(35,1,R/100,R*100);
const orthoCam=new THREE.OrthographicCamera(-1,1,1,-1,-R*100,R*100);
let cam=persp, theta=-Math.PI/4, phi=Math.PI/3, dist=R*2.6, zoom=1;
const target=ctr.clone();
const VIEWS={front:[-Math.PI/2,Math.PI/2],back:[Math.PI/2,Math.PI/2],left:[Math.PI,Math.PI/2],
 right:[0,Math.PI/2],top:[-Math.PI/2,1e-4],bottom:[-Math.PI/2,Math.PI-1e-4],iso:[-Math.PI/4,Math.PI/3]};
function place(){
 const sp=Math.sin(phi), off=new THREE.Vector3(Math.cos(theta)*sp,Math.sin(theta)*sp,Math.cos(phi)).multiplyScalar(dist);
 for(const c of [persp,orthoCam]){c.position.copy(target).add(off);
  c.up.set(0,0,1); if(phi<1e-3||phi>Math.PI-1e-3) c.up.set(0,1,0); c.lookAt(target);}
 sun.position.copy(persp.position);
 const w=innerWidth,h=innerHeight,a=w/h,half=R*0.75/zoom;
 orthoCam.left=-half*a;orthoCam.right=half*a;orthoCam.top=half;orthoCam.bottom=-half;orthoCam.updateProjectionMatrix();
 persp.aspect=a;persp.updateProjectionMatrix();
 renderer.setSize(w,h,false);renderer.render(scene,cam);
}
document.querySelectorAll('[data-v]').forEach(b=>b.onclick=()=>{[theta,phi]=VIEWS[b.dataset.v];target.copy(ctr);place();});
const ob=document.getElementById('ortho');
ob.onclick=()=>{cam=cam===persp?orthoCam:persp;ob.setAttribute('aria-pressed',cam===orthoCam);place();};
const eb=document.getElementById('edges');
eb.onclick=()=>{edges.visible=!edges.visible;eb.setAttribute('aria-pressed',edges.visible);place();};
let drag=null;
canvas.addEventListener('pointerdown',e=>{drag={x:e.clientX,y:e.clientY,pan:e.button===2||e.shiftKey};canvas.setPointerCapture(e.pointerId);});
canvas.addEventListener('pointerup',()=>drag=null);
canvas.addEventListener('contextmenu',e=>e.preventDefault());
canvas.addEventListener('pointermove',e=>{if(!drag)return;const dx=e.clientX-drag.x,dy=e.clientY-drag.y;drag.x=e.clientX;drag.y=e.clientY;
 if(drag.pan){const s=(cam===persp?dist:R*1.5/zoom)/innerHeight;
  const right=new THREE.Vector3().setFromMatrixColumn(cam.matrix,0),up=new THREE.Vector3().setFromMatrixColumn(cam.matrix,1);
  target.add(right.multiplyScalar(-dx*s)).add(up.multiplyScalar(dy*s));}
 else{theta-=dx*0.008;phi=Math.min(Math.PI-1e-3,Math.max(1e-3,phi-dy*0.008));}
 place();});
canvas.addEventListener('wheel',e=>{e.preventDefault();const k=Math.exp(e.deltaY*0.0012);dist*=k;zoom/=k;place();},{passive:false});
addEventListener('resize',place);
const f=v=>(Math.round(v*1000)/1000).toString();
document.getElementById('info').innerHTML=`Size <strong>${f(size.x)} × ${f(size.y)} × ${f(size.z)}</strong> mm · `+
 `${(pos.length/9).toLocaleString()} triangles`+(META.volume_mm3?` · volume ${Math.round(META.volume_mm3).toLocaleString()} mm³`:'');
place();
</script></body></html>
"""


def build_page(coords, title, meta):
    raw = struct.pack(f"<{len(coords)}f", *coords)
    return (PAGE.replace("__TITLE__", html.escape(title))
                .replace("__META__", json.dumps(meta))
                .replace("__DATA__", base64.b64encode(raw).decode("ascii")))


def sample_coords():
    """A 40 x 30 x 10 box as 12 triangles."""
    x, y, z = 40.0, 30.0, 10.0
    v = [(0, 0, 0), (x, 0, 0), (x, y, 0), (0, y, 0), (0, 0, z), (x, 0, z), (x, y, z), (0, y, z)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [c for t in tris for i in t for c in v[i]]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Wrap an STL into one self-contained HTML viewer with drawing-standard views.",
        epilog="Exit: 0 written · 2 unreadable STL · 64 usage")
    ap.add_argument("stl", nargs="?", help="model.stl (binary or ASCII)")
    ap.add_argument("--out", help="output HTML (default: <stl>.html)")
    ap.add_argument("--report", help="<part>.report.json to show volume")
    ap.add_argument("--title", help="page title (default: file name)")
    ap.add_argument("--sample", action="store_true", help="write a viewer for a sample box to --out or sample_viewer.html")
    ap.add_argument("--output", choices=["text", "json"], default="text")
    a = ap.parse_args(argv)

    meta = {}
    if a.sample:
        coords, title = sample_coords(), a.title or "sample box"
        out = a.out or "sample_viewer.html"
    elif a.stl:
        try:
            with open(a.stl, "rb") as fh:
                coords = read_stl(fh.read())
        except (OSError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        title = a.title or os.path.splitext(os.path.basename(a.stl))[0]
        out = a.out or os.path.splitext(a.stl)[0] + ".html"
    else:
        ap.print_usage(sys.stderr)
        return 64
    if a.report:
        try:
            with open(a.report, encoding="utf-8") as fh:
                rep = json.load(fh)
            meta["volume_mm3"] = rep.get("volume_mm3")
        except (OSError, json.JSONDecodeError):
            pass

    page = build_page(coords, title, meta)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    tris = len(coords) // 9
    if a.output == "json":
        print(json.dumps({"written": out, "triangles": tris, "bytes": len(page)}))
    else:
        print(f"wrote {out} ({tris} triangles, {len(page) // 1024} KB) — open it in a browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
