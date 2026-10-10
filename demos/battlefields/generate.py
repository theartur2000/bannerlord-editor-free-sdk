"""Tech demo, offline half: a battlefield generated from a spot on the campaign map.

This is a demo built with the SDK, not part of it. It turns a map position into a complete scene folder:

    python demos/battlefields/generate.py 493.6 403.2 --name gen_field_test --preview out.png

The ground comes from the world map's own heights around the position (sdk/worldmap.py), small relief from shipped
battlefields is quilted over it (sdk/terraform.py), texture layers are painted by slope and height, and the scene gets a
border, spawn paths through the middle and a navmesh (sdk/scenebuild.py). The texture layers, atmosphere and outer mesh
come from a shipped battlefield used as the template. Everything is read from the installed game when it runs.

Status: the files are written and read back by the SDK. They have not been loaded by the game yet.
"""
import argparse, math, os, sys, time, zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk"))
import config, scenebuild, terraform, terrain, worldmap   # noqa: E402

TEMPLATE = ("SandBoxCore", "battle_terrain_a")
DONORS = (("SandBoxCore", "battle_terrain_a"), ("SandBoxCore", "battle_terrain_b"))
FIELD = 1200.0            # metres across (the template's 6 x 6 nodes of 200 m)
PATCH = 24.0              # map units of land a field covers
BORDER_RADIUS = 330.0     # the walkable area, about the size of a shipped battlefield's
PATHS = 4                 # spawn paths through the middle, one per 45 degrees


def scene_dir(module, name): return os.path.join(config.GAME, "Modules", module, "SceneObj", name)


WET = (8, 10, 11, 18, 19, 22)   # campaign terrain types that are water: lake, water, river, coastal sea, open sea, non-navigable river
FORD = 6
FOREST = 4


def make(center, name, out_dir, patch=PATCH, flora=True, water=True, log=print):
    t0 = time.time()
    wm = worldmap.WorldMap()
    tpl = scenebuild.Template(scene_dir(*TEMPLATE))
    assert abs(tpl.size[0] - FIELD) < 1e-6, tpl.size
    n = tpl.samples[0]; cell = tpl.cell
    seed = zlib.crc32(("%.2f,%.2f" % center).encode())
    rng = np.random.default_rng(seed + 7)
    types = wm.types_patch(center, patch, n)

    # 1. the map's own shape
    k = FIELD / patch
    vertical = 0.4 * k * math.sqrt(patch / 12.0)
    base = (wm.patch(center, patch, n) - worldmap.SEA_LEVEL) * vertical
    sea = base < 0.0
    # 2. small relief from shipped battlefields
    bank = terraform.DetailBank(cell)
    for mod, nm in DONORS:
        d = scene_dir(mod, nm)
        bank.add(terrain.Terrain(os.path.join(d, "terrain.bin")).heightmap(terrain.scene_terrain_info(os.path.join(d, "scene.xscene"))))
    z = bank.apply(base, seed)

    # 3. water. A scene has one water level, so the river, lake or sea of the map is levelled: its channel is cut to a flat
    # bed under the level (shallow where the map has a ford), and no other ground is left below the level.
    wet = np.isin(types, WET) | sea; ford = (types == FORD) & ~wet
    level = None; deep = np.zeros(z.shape, bool); under = np.zeros(z.shape, bool)
    if water and (wet | ford).mean() > 0.004:
        chan = (wet | ford).astype(np.float64)
        soft = terraform.blur_fast(chan, 9.0 / cell)                          # 0 on dry land, 1 mid-channel
        level = float(np.median(z[wet | ford]))
        depth = terraform.blur_fast(np.where(wet, 2.4, np.where(ford, 0.45, 0.0)), 6.0 / cell)
        inside = terraform.smoothstep(soft, 0.35, 0.75)
        z = z * (1 - inside) + (level - depth) * inside
        dry = level + 0.6
        outside = 1 - terraform.smoothstep(soft, 0.05, 0.35)
        z = np.where(z < dry, z + (dry - z) * outside, z)
        under = z < level - 0.05; deep = z < level - 0.9
    off = 40.0 - float(z.min())                                               # shipped fields sit some tens of metres up
    z = z + off
    if level is not None: level += off
    log("ground: relief %.0f m, water %s, %.1f s" % (z.max() - z.min(), ("level %.1f m over %.1f %% (deep %.1f %%)" % (level, 100 * under.mean(), 100 * deep.mean())) if level is not None else "none", time.time() - t0))

    # 4. texture layers by rule, on the layers of the template
    names = tpl.layer_names; sl = terraform.slope(terraform.blur_fast(z, 1.5), cell)
    noise = terraform.fbm_field(z.shape, rng, 9, 4); noise2 = terraform.fbm_field(z.shape, rng, 24, 3)
    curve = z - terraform.blur_fast(z, 10)                                   # negative in hollows
    def L(nm): return names.index(nm) if nm in names else None
    def m8(a): return np.clip(np.rint(np.clip(a, 0, 1) * 255), 0, 255).astype(np.uint8)
    masks = {}
    def paint(nm, a):
        i = L(nm)
        if i is not None and i != 0: masks[i] = m8(a)
    paint("mountain_grass", terraform.smoothstep(noise, 0.05, 0.45) * (1 - terraform.smoothstep(sl, 0.25, 0.5)))
    paint("grass", terraform.smoothstep(noise2, 0.0, 0.5) * (1 - terraform.smoothstep(sl, 0.2, 0.45)))
    paint("dry_ground", terraform.smoothstep(-noise, 0.15, 0.6) * (1 - terraform.smoothstep(sl, 0.3, 0.55)))
    paint("soil", terraform.smoothstep(-curve, 0.25, 0.9) * terraform.smoothstep(sl, 0.08, 0.25))
    wetground = terraform.blur_fast(under.astype(np.float64), 3.0 / cell) if level is not None else np.zeros(z.shape)
    paint("flow", np.maximum(terraform.smoothstep(-curve, 0.6, 1.6), terraform.smoothstep(wetground, 0.15, 0.6)))   # bare earth in hollows, on the river bed and its banks
    paint("base_rock", terraform.smoothstep(sl, 0.45, 0.70))
    paint("hard_rock", terraform.smoothstep(sl, 0.75, 1.05))
    paint("base_snow", 1 - terraform.smoothstep(sl, 0.45, 0.7))               # enabled in winter only
    paint("detail_snow", (1 - terraform.smoothstep(sl, 0.3, 0.6)) * terraform.smoothstep(noise, -0.2, 0.4))
    # 5. trees: the flora layer of the template where the map has forest, with a ragged edge; none in water or on cliffs
    forest = np.zeros(z.shape)
    if flora and tpl.flora_layers:
        f = terraform.blur_fast((types == FOREST).astype(np.float64), 14.0 / cell)
        forest = terraform.smoothstep(f + 0.22 * noise2, 0.42, 0.62) * (1 - terraform.smoothstep(sl, 0.5, 0.75))
        forest = forest * (1 - terraform.smoothstep(wetground, 0.02, 0.2))
        masks[tpl.flora_layers[0]] = m8(forest)
        log("forest: %.1f %% of the field (map: %.1f %%), flora layer %s" % (100 * (forest > 0.5).mean(), 100 * (types == FOREST).mean(), names[tpl.flora_layers[0]]))

    # 6. border, spawn paths (the four bearings with the least deep water, forest and steep ground on them)
    c = FIELD / 2
    def at(a, x, y): return a[int(round(y / cell)), int(round(x / cell))]
    border = [(c + BORDER_RADIUS * math.cos(a), c + BORDER_RADIUS * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 32, endpoint=False)]
    cands = []
    for p in range(8):
        a = math.pi * p / 8; pts = []; bad = 0.0
        for q in np.linspace(-0.72, 0.72, 7):
            x = c + BORDER_RADIUS * q * math.cos(a); y = c + BORDER_RADIUS * q * math.sin(a)
            pts.append((x, y, float(at(z, x, y)))); bad += 3.0 * bool(at(deep, x, y)) + float(at(forest, x, y)) + 2.0 * (float(at(sl, x, y)) > 0.5)
        ends = 4.0 * (bool(at(deep, *pts[0][:2])) + bool(at(deep, *pts[-1][:2])))
        cands.append((bad + ends, p, pts))
    paths = [t[2] for t in sorted(sorted(cands, key=lambda t: t[0])[:PATHS], key=lambda t: t[1])]
    walk = scenebuild.polygon_mask(border, z.shape, cell) & ~deep

    info = scenebuild.build(tpl, out_dir, name, z, masks, entities=(), spawn_paths=paths, border=border, walkable=walk, water_level=level, log=log)
    info.update({"seed": seed, "center": center, "patch": patch, "terrain_mix": wm.terrain_mix(center, patch), "water": under, "forest": forest,
                 "masks": masks, "border": border, "paths": paths, "layer_names": names, "seconds": time.time() - t0, "water_level": level})
    log("scene %s written to %s in %.1f s" % (name, out_dir, info["seconds"]))
    return info


def preview(info, path, size=1100):
    """A top view: shaded ground tinted by the painted layers, water, the navmesh area, the border and the spawn paths."""
    from PIL import Image, ImageDraw
    z = info["z"]; n = z.shape[0]; cell = FIELD / (n - 1)
    gy, gx = np.gradient(z, cell)
    nrm = np.dstack((-gx, -gy, np.ones_like(z))); nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    light = np.array([-0.5, 0.5, 0.7]); light /= np.linalg.norm(light)
    sh = np.clip((nrm * light).sum(axis=2), 0, 1) ** 1.3
    col = np.zeros(z.shape + (3,)) + np.array([118, 132, 78])                 # the base layer: grass
    tint = {"mountain_grass": (96, 124, 66), "grass": (128, 150, 84), "dry_ground": (158, 146, 98), "soil": (120, 98, 72), "flow": (104, 86, 66),
            "base_rock": (128, 124, 118), "hard_rock": (96, 94, 92)}
    for nm, c in tint.items():
        if nm in info["layer_names"]:
            i = info["layer_names"].index(nm)
            if i in info["masks"]:
                a = (info["masks"][i].astype(float) / 255)[..., None]; col = col * (1 - a) + np.array(c) * a
    img = col * (0.35 + 0.75 * sh[..., None])
    fa = np.clip(info.get("forest", 0), 0, 1)[..., None] * 0.75
    img = img * (1 - fa) + np.array([34, 70, 40]) * fa * (0.5 + 0.6 * sh[..., None])
    img[info["water"]] = img[info["water"]] * 0.3 + np.array([40, 84, 136]) * 0.7
    k = (n - 1) // info["nav_cells"].shape[0]
    nav = np.kron(info["nav_cells"], np.ones((k, k), bool)); navf = np.zeros(z.shape, bool); navf[:nav.shape[0], :nav.shape[1]] = nav
    edge = navf & ~(np.roll(navf, k, 0) & np.roll(navf, -k, 0) & np.roll(navf, k, 1) & np.roll(navf, -k, 1))
    img[edge] = img[edge] * 0.4 + np.array([255, 230, 90]) * 0.6
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)[::-1]).resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(im); s = size / FIELD
    def P(x, y): return (x * s, size - y * s)
    b = info["border"]; d.line([P(*p) for p in b + b[:1]], fill=(235, 70, 60), width=2)
    for i, pts in enumerate(info["paths"]):
        d.line([P(p[0], p[1]) for p in pts], fill=(90, 170, 255), width=2)
        for p in (pts[0], pts[-1]): d.ellipse([P(p[0], p[1])[0] - 4, P(p[0], p[1])[1] - 4, P(p[0], p[1])[0] + 4, P(p[0], p[1])[1] + 4], fill=(90, 170, 255))
    im.save(path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("x", type=float); ap.add_argument("y", type=float)
    ap.add_argument("--name", default="gen_field"); ap.add_argument("--out", default=None, help="scene folder (default: <SDK stage>/scenes/<name>)")
    ap.add_argument("--patch", type=float, default=PATCH); ap.add_argument("--preview", default=None)
    ap.add_argument("--no-flora", action="store_true"); ap.add_argument("--no-water", action="store_true")
    a = ap.parse_args()
    out = a.out or os.path.join(config.SDK_STAGE_DIR, "scenes", a.name)
    info = make((a.x, a.y), a.name, out, a.patch, flora=not a.no_flora, water=not a.no_water)
    if a.preview: preview(info, a.preview); print("preview", a.preview)
