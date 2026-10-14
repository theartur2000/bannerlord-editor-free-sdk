"""A playable scene from a heightmap picture, and the heightmap of a scene as a picture.

This is the bridge to any terrain tool: sculpt in Blender, World Machine, Gaea or a paint program, export a greyscale
image, and get a scene folder the game loads. The other direction takes a shipped or generated scene out for editing.

    python sdk/heightmap.py export <scene folder> <out.png>
    python sdk/heightmap.py scene <image> <name> [--template battle_terrain_a] [--low 0] [--high 80] [--out <folder>]

export  writes a 16-bit greyscale PNG (row 0 = north) and <out>.json with what the grey values mean: the lowest and
        highest height in metres and the size of the terrain. `scene` reads that file back when it lies next to the image.
scene   resamples the image to the template's grid and writes scene.xscene, terrain.bin and navmesh.bin with
        scenebuild.build. Texture layers, atmosphere and plants come from the template, a shipped scene. Painting: the
        template's base layer everywhere, its rock layer on steep ground, and its ground-cover layer (the first layer that
        carries grass or plant models) in patches on gentle ground, over as much of the walkable middle as the template
        itself covers. Two spawn paths cross the middle and the walkable area is a circle. Anything finer is a few lines
        of Python on top of scenebuild.build.
Images: 8 or 16-bit PNG (grey; of a colour image the first channel is used), .npy (a float array in metres is taken
as it is), .r16 / .raw (square, 16-bit little-endian).
"""
import json, math, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(path):
    """(array of float64 with row 0 = north, absolute) from an image file. absolute: the values are metres already."""
    low = path.lower()
    if low.endswith(".npy"):
        a = np.load(path).astype(np.float64)
        return (a[:, :, 0] if a.ndim == 3 else a), True
    if low.endswith((".r16", ".raw")):
        raw = np.fromfile(path, "<u2"); n = int(round(math.sqrt(len(raw))))
        if n * n != len(raw): raise ValueError("%s: %d samples are not a square" % (path, len(raw)))
        return raw.reshape(n, n).astype(np.float64) / 65535.0, False
    import terrain
    with open(path, "rb") as f: a = np.asarray(terrain.png_decode(f.read()))
    if a.ndim == 3: a = a[:, :, 0]
    return a.astype(np.float64) / (65535.0 if a.dtype == np.uint16 else 255.0), False


def export(scene_folder, out):
    """Writes the scene's heights as a 16-bit PNG and a .json beside it. Returns the json's content."""
    import terrain
    info = terrain.scene_terrain_info(os.path.join(scene_folder, "scene.xscene"))
    if info is None: raise ValueError("%s has no terrain" % scene_folder)
    z = terrain.Terrain(os.path.join(scene_folder, "terrain.bin")).heightmap(info)
    lo, hi = float(z.min()), float(z.max()); span = max(hi - lo, 1e-6)
    h16 = np.clip(np.rint((z - lo) / span * 65535.0), 0, 65535).astype(np.uint16)[::-1]         # north up
    with open(out, "wb") as f: f.write(terrain.png_encode(np.ascontiguousarray(h16)))
    meta = {"low_m": round(lo, 4), "high_m": round(hi, 4), "size_m": [info["nx"] * info["node_size"], info["ny"] * info["node_size"]],
            "samples": [int(z.shape[1]), int(z.shape[0])], "metres_per_sample": info["node_size"] / 256.0, "north": "row 0"}
    with open(out + ".json", "w", encoding="utf-8", newline="\n") as f: json.dump(meta, f, indent=2)
    return meta


def template_folder(template):
    """A scene folder from a path or from the id of a shipped scene."""
    if os.path.isfile(os.path.join(template, "scene.xscene")): return template
    import catalog
    for r in catalog.load(log=lambda *a: None).records:
        if r["id"] == template: return r["dir"]
    raise ValueError("no shipped scene named %s (and no scene folder at that path)" % template)


def to_scene(image, name, template="battle_terrain_a", low=None, high=None, out_dir=None, rock_slope=0.75, play_radius=None,
             water_level=None, smooth=0.0, cover=None, seed=1, log=print):
    """cover: share (0..1) of the gentle ground the ground-cover layer takes; None = as much as the template's own middle."""
    """Builds the scene folder out_dir (default <SDK stage>/work/SceneObj/<name>) from a heightmap. Returns numbers."""
    import config, scenebuild, terraform, worldmap
    a, absolute = load(image)
    meta_file = image + ".json"
    if low is None and high is None and not absolute and os.path.isfile(meta_file):
        with open(meta_file, encoding="utf-8") as f: m = json.load(f)
        low, high = m["low_m"], m["high_m"]; log("heights from %s: %.2f to %.2f m" % (os.path.basename(meta_file), low, high))
    if not absolute:
        low = 0.0 if low is None else float(low); high = low + 80.0 if high is None else float(high)
        a = low + a * (high - low)
    a = a[::-1]                                                         # the game's row 0 is y = 0, the south edge
    t = scenebuild.Template(template_folder(template))
    H, W = t.samples
    if a.shape != (H, W):
        log("resampling %d x %d to the template's %d x %d" % (a.shape[1], a.shape[0], W, H))
        a = worldmap.resample(a, 0, a.shape[1] - 1, 0, a.shape[0] - 1, W, H)
    if smooth > 0: a = terraform.blur_fast(a, smooth / t.cell)
    cell = t.cell; size_x, size_y = t.size
    slope = terraform.slope(terraform.blur_fast(a, 1.5), cell)
    masks = {}
    rock = next((i for i, n in enumerate(t.layer_names) if i and any(w in n.lower() for w in ("rock", "cliff"))), None)
    if rock is not None:
        masks[rock] = np.clip(np.rint(terraform.smoothstep(slope, rock_slope * 0.8, rock_slope * 1.2) * 255.0), 0, 255).astype(np.uint8)
    # ground cover: the first layer with grass or plant models that is not a tree (flora) layer and shows in summer
    green = next((i for i in range(1, len(t.layer_names)) if t.meshes[i] and i not in t.flora_layers and t.enabled["summer"][i]), None)
    share = 0.0
    if green is not None and cover != 0:
        share = float(cover) if cover is not None else (t.paint_stats()[green]["strong"] or 0.5)
        gentle = slope < rock_slope * 0.6
        if gentle.any() and share > 0:
            n = terraform.fbm(a.shape, cell, 24.0, 3, np.random.default_rng(seed))        # patches of a couple of dozen metres
            thr = float(np.quantile(n[gentle], 1.0 - min(share, 1.0))); w = 0.04 * float(n.std() or 1.0)
            m = terraform.smoothstep(n, thr - w, thr + w) * (1.0 - terraform.smoothstep(slope, rock_slope * 0.4, rock_slope * 0.6))
            masks[green] = np.clip(np.rint(m * 255.0), 0, 255).astype(np.uint8)
    cx, cy = size_x / 2, size_y / 2
    R = float(play_radius) if play_radius else 0.30 * min(size_x, size_y)
    border = [(cx + R * math.cos(k * math.pi / 16), cy + R * math.sin(k * math.pi / 16)) for k in range(32)]
    def ground(x, y): return float(a[min(H - 1, max(0, int(round(y / cell)))), min(W - 1, max(0, int(round(x / cell))))])
    paths = []
    for ang in (0.0, math.pi / 2):                                      # one west to east, one south to north: the game picks one
        pts = []
        for k in range(9):
            d = (k / 8.0 * 2 - 1) * R * 0.72; x, y = cx + d * math.cos(ang), cy + d * math.sin(ang)
            pts.append((x, y, ground(x, y)))
        paths.append(pts)
    if out_dir is None: out_dir = os.path.join(config.SDK_STAGE_DIR, "work", "SceneObj", name)
    res = scenebuild.build(t, out_dir, name, a, masks=masks, spawn_paths=paths, border=border, water_level=water_level, log=log)
    steep = float((slope > rock_slope).mean())
    return {"folder": out_dir, "name": name, "template": os.path.basename(os.path.normpath(t.folder)), "size_m": [size_x, size_y],
            "ground_cover_layer": t.layer_names[green] if green is not None else None, "ground_cover_share": round(share, 3),
            "heights_m": [round(res["min"], 2), round(res["max"], 2)], "steep_share": round(steep, 3), "rock_layer": t.layer_names[rock] if rock is not None else None,
            "navmesh_faces": res["navmesh"]["faces"], "walkable_m2": res["navmesh"]["area_m2"], "play_radius_m": round(R, 1), "spawn_paths": len(paths)}


if __name__ == "__main__":
    a = sys.argv[1:]; opt = lambda k, d=None: a[a.index(k) + 1] if k in a else d
    if len(a) >= 3 and a[0] == "export": print(export(a[1], a[2]))
    elif len(a) >= 3 and a[0] == "scene":
        print(to_scene(a[1], a[2], opt("--template", "battle_terrain_a"), opt("--low") and float(opt("--low")), opt("--high") and float(opt("--high")), opt("--out")))
    else: print(__doc__)
