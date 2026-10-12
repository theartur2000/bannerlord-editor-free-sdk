"""A top view of any battlefield scene folder, read from its files: relief shading, ground by texture layer, forest,
water, rocks, spawn paths, border. Works for generated scenes (Python or the in-game generator) and shipped ones.

    python demos/battlefields/fieldmap.py <scene folder> <out.png> [size]
"""
import os, re, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk"))
import scene, terrain   # noqa: E402


def render(folder, out, size=900):
    from PIL import Image, ImageDraw
    xs = os.path.join(folder, "scene.xscene"); info = terrain.scene_terrain_info(xs); t = terrain.Terrain(os.path.join(folder, "terrain.bin"))
    z = t.heightmap(info); n = z.shape[0]; cell = info["node_size"] * info["nx"] / (n - 1); W = t.weight_nodes(); nx, ny = info["nx"], info["ny"]
    text = open(xs, encoding="utf-8").read(); i = text.index("<layers"); parts = re.split(r'(?=<layer name=")', text[i:text.index("</layers>", i)])[1:]
    col = np.zeros((n, n, 3)); col[:] = (150, 140, 96)
    forest = np.zeros((n, n))
    for li, part in enumerate(parts):
        if li == 0: continue
        sm = part[part.index("<summer"):part.index("</summer>")]
        if 'is_enabled="true"' not in sm[:60]: continue
        nodes = [W[k][li] if li < len(W[k]) else None for k in range(nx * ny)]
        if all(a is None for a in nodes): continue
        w = terrain.join_nodes([a if a is not None else np.zeros((257, 257), np.uint8) for a in nodes], nx, ny).astype(float) / 255.0
        if w.shape != z.shape: continue
        name = (re.match(r'<layer name="([^"]*)"', part).group(1) + " " + (re.search(r'type="diffusemap" name="([^"]*)"', sm) or [None, ""])[1]).lower()
        if 'is_flora_layer="true"' in sm and "tree" in sm: forest = np.maximum(forest, w); continue
        c = (118, 116, 110) if ("rock" in name or "cliff" in name) else (128, 100, 70) if ("soil" in name or "earth" in name or "mud" in name) else \
            (196, 176, 120) if ("desert" in name or "sand" in name) else (98, 132, 62) if "<mesh " in sm else (120, 140, 80)
        col = col * (1 - w[..., None]) + np.array(c) * w[..., None]
    gy, gx = np.gradient(z, cell); nrm = np.dstack((-gx, -gy, np.ones_like(z))); nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    light = np.array([-0.5, 0.5, 0.7]); light /= np.linalg.norm(light); shade = np.clip((nrm * light).sum(axis=2), 0, 1)
    col = col * (0.45 + 0.75 * shade)[..., None]
    col = col * (1 - 0.45 * forest[..., None]) + np.array((40, 80, 40)) * 0.45 * forest[..., None]
    m = re.search(r'name="water_level" value="([-\d.]+)"', text); e = re.search(r'name="water_exists" value="(\w+)"', text)
    if m and e and e.group(1) == "true":
        under = z < float(m.group(1)); col[under] = col[under] * 0.35 + np.array((60, 110, 170)) * 0.65
    img = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8)[::-1]).resize((size, size), Image.LANCZOS); d = ImageDraw.Draw(img)
    def P(x, y): return (x / (n * cell) * size, size - y / (n * cell) * size)
    doc = scene.load(xs); border = []
    for en in doc.entities():
        p = en.position
        if p is None: continue
        if en.name == "border_soft": border.append((p[0], p[1])); continue
        nm = en.prefab or ""
        if "rock" in nm or "cliff" in nm:
            r = 2.5 * (en.scale or (1, 1, 1))[0] if ("cliff" in nm or "big" in nm) else 1.2; q = P(p[0], p[1]); d.ellipse((q[0] - r, q[1] - r, q[0] + r, q[1] + r), fill=(60, 58, 56))
        elif nm == "TacticalPosition": q = P(p[0], p[1]); d.polygon([(q[0], q[1] - 6), (q[0] - 5, q[1] + 4), (q[0] + 5, q[1] + 4)], outline=(255, 220, 0))
    if border:
        c = np.mean(border, axis=0); border.sort(key=lambda b: np.arctan2(b[1] - c[1], b[0] - c[0])); d.line([P(*b) for b in border] + [P(*border[0])], fill=(220, 50, 50), width=2)
    for k, pm in enumerate(re.finditer(r'<path name="spawn_path_\d+".*?</path>', text, re.S)):
        pts = [tuple(float(v) for v in q.split(",")[:2]) for q in re.findall(r'<point position="([^"]*)"', pm.group(0))]
        d.line([P(*q) for q in pts], fill=(80, 170, 255), width=3 if k == 0 else 1)
    img.save(out)


if __name__ == "__main__":
    if len(sys.argv) < 3: print(__doc__)
    else: render(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 900)
