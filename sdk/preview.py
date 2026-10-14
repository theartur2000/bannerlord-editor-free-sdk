"""Pictures without the game: a mesh from four sides, a scene from above.

An agent has no eyes in the editor, and starting the game for every look costs a minute. These renders take a second
and answer the usual questions: is the model the right way up, how big is it, is it an open shell, where are the rocks,
the forest, the spawn lines.

    python sdk/preview.py mesh <shipped mesh name | file.obj | file.psk> <out.png> [size]
    python sdk/preview.py scene <scene folder> <out.png> [size]

mesh:  six views on one sheet (front, side, top, three-quarter, back, from below), flat shaded, z up as in the game. Triangles seen from
       behind are drawn red: on a closed model none are visible, on a one-sided shell (most shipped rocks) the red side
       is the side the game does not draw.
scene: relief shading from terrain.bin, the strongest painted texture layer as colour, entities as dots (rocks grey,
       trees green, others orange), spawn paths as lines, the walkable area's corners joined.
Needs Pillow only for the text labels and the scene's lines; arrays are written with the SDK's own PNG writer otherwise.
"""
import math, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BACK = np.array([0.72, 0.25, 0.22])       # triangles seen from behind
FRONT = np.array([0.78, 0.76, 0.72])
BG = np.array([0.13, 0.13, 0.14])
VIEWS = (("front", 0.0, 0.0), ("side", 90.0, 0.0), ("top", 0.0, 90.0), ("three-quarter", 35.0, 25.0), ("back", 180.0, 0.0), ("from below", 0.0, -90.0))


def _basis(yaw, pitch):
    """Rows: screen right, screen up, towards the viewer. yaw turns the camera around z, pitch lifts it; z is up."""
    a, e = math.radians(yaw), math.radians(pitch)
    fwd = np.array([-math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), -math.sin(e)])      # the camera looks along this
    right = np.array([math.cos(a), math.sin(a), 0.0])
    up = np.cross(right, fwd)
    return np.stack([right, up, -fwd])


def render(P, F, size=512, yaw=35.0, pitch=25.0, extent=None):
    """An orthographic picture of a triangle mesh as a float (size, size, 3) array in 0..1.
    P (n, 3) positions, F (k, 3) triangles. extent: half the width of the view in metres (default: fits the mesh)."""
    P = np.asarray(P, np.float64); F = np.asarray(F, np.int64)
    img = np.empty((size, size, 3)); img[:] = BG
    if not len(F): return img
    centre = (P.min(0) + P.max(0)) / 2
    B = _basis(yaw, pitch); Q = (P - centre) @ B.T                     # x right, y up, z towards the viewer
    if extent is None: extent = max(float(np.abs(Q[:, :2]).max()), 1e-6) * 1.08
    s = (size - 1) / (2 * extent)
    X = Q[:, 0] * s + (size - 1) / 2; Y = (size - 1) / 2 - Q[:, 1] * s; Z = Q[:, 2]
    a, b, c = Q[F[:, 0]], Q[F[:, 1]], Q[F[:, 2]]
    n = np.cross(b - a, c - a); ln = np.linalg.norm(n, axis=1); ok = ln > 1e-12
    n[ok] /= ln[ok, None]
    light = np.array([0.35, 0.55, 0.76]); light /= np.linalg.norm(light)
    facing = n[:, 2] >= 0
    lit = 0.30 + 0.70 * np.abs(n @ light)
    col = np.where(facing[:, None], FRONT, BACK) * lit[:, None]
    zbuf = np.full((size, size), -np.inf)
    order = np.argsort(Z[F].max(axis=1))                               # far to near: fewer depth rejections later
    for t in order:
        if not ok[t]: continue
        i0, i1, i2 = F[t]
        x0, y0, x1, y1, x2, y2 = X[i0], Y[i0], X[i1], Y[i1], X[i2], Y[i2]
        lo_x = max(int(math.floor(min(x0, x1, x2))), 0); hi_x = min(int(math.ceil(max(x0, x1, x2))), size - 1)
        lo_y = max(int(math.floor(min(y0, y1, y2))), 0); hi_y = min(int(math.ceil(max(y0, y1, y2))), size - 1)
        if lo_x > hi_x or lo_y > hi_y: continue
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12: continue
        xs = np.arange(lo_x, hi_x + 1)[None, :]; ys = np.arange(lo_y, hi_y + 1)[:, None]
        w0 = ((y1 - y2) * (xs - x2) + (x2 - x1) * (ys - y2)) / den
        w1 = ((y2 - y0) * (xs - x2) + (x0 - x2) * (ys - y2)) / den
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        if not inside.any(): continue
        z = w0 * Z[i0] + w1 * Z[i1] + w2 * Z[i2]
        zb = zbuf[lo_y:hi_y + 1, lo_x:hi_x + 1]
        hit = inside & (z > zb)
        zb[hit] = z[hit]
        img[lo_y:hi_y + 1, lo_x:hi_x + 1][hit] = col[t]
    return img


def mesh_stats(P, F):
    """Size, triangle count and how open the mesh is (edges that belong to one triangle only)."""
    P = np.asarray(P, np.float64); F = np.asarray(F, np.int64)
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]); e.sort(axis=1)
    u, c = np.unique(e, axis=0, return_counts=True); rim = u[c == 1]
    a, b, cc = P[F[:, 0]], P[F[:, 1]], P[F[:, 2]]
    vol = float(np.einsum("ij,ij->i", a, np.cross(b, cc)).sum() / 6.0)
    return {"vertices": int(len(P)), "triangles": int(len(F)), "min": [round(float(x), 3) for x in P.min(0)], "max": [round(float(x), 3) for x in P.max(0)],
            "size": [round(float(x), 3) for x in (P.max(0) - P.min(0))], "open_edges": int(len(rim)),
            "open_rim_m": round(float(np.linalg.norm(P[rim[:, 0]] - P[rim[:, 1]], axis=1).sum()), 2) if len(rim) else 0.0,
            "closed": bool(len(rim) == 0), "signed_volume_m3": round(vol, 3)}


def mesh_sheet(P, F, out, size=512, title=None):
    """The six views of VIEWS on one 3 x 2 sheet, all at the same scale. Returns mesh_stats."""
    P = np.asarray(P, np.float64)
    extent = max(float(np.linalg.norm(P.max(0) - P.min(0))) / 2 * 1.04, 1e-6)       # the half diagonal: fits from every side
    tiles = [render(P, F, size, yaw, pitch, extent) for _, yaw, pitch in VIEWS]
    sheet = np.concatenate([np.concatenate(tiles[:3], axis=1), np.concatenate(tiles[3:], axis=1)], axis=0)
    st = mesh_stats(P, F)
    _save(sheet, out, labels=[(8 + (k % 3) * size, 6 + (k // 3) * size, VIEWS[k][0]) for k in range(6)] +
          [(8, 2 * size - 22, "%s   %d triangles   %.2f x %.2f x %.2f m   %s" % (title or "", st["triangles"], st["size"][0], st["size"][1], st["size"][2],
                                                                        "closed" if st["closed"] else "open rim %.1f m" % st["open_rim_m"]))])
    return st


def _save(img, out, labels=(), lines=()):
    a = np.clip(np.rint(img * 255.0), 0, 255).astype(np.uint8)
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        import pngio
        pngio.write_png(out, a); return
    im = Image.fromarray(a); d = ImageDraw.Draw(im)
    for pts, colour, width in lines: d.line(pts, fill=colour, width=width)
    for x, y, text in labels:
        box = d.textbbox((x, y), text); d.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill=(20, 20, 22))
        d.text((x, y), text, fill=(235, 235, 235))
    im.save(out)


def load_mesh(source):
    """(positions, triangles, name) from a shipped metamesh name, an .obj file or a .psk / .pskx file."""
    low = source.lower()
    if low.endswith(".obj"):
        P = []; F = []
        with open(source, encoding="utf-8", errors="replace") as f:
            for line in f:
                w = line.split()
                if not w: continue
                if w[0] == "v": P.append([float(x) for x in w[1:4]])
                elif w[0] == "f":
                    idx = [int(x.split("/")[0]) for x in w[1:]]
                    idx = [i - 1 if i > 0 else len(P) + i for i in idx]
                    for k in range(1, len(idx) - 1): F.append([idx[0], idx[k], idx[k + 1]])
        return np.array(P, np.float64).reshape(-1, 3), np.array(F, np.int64).reshape(-1, 3), os.path.basename(source)
    if low.endswith((".psk", ".pskx")):
        import psk
        m = psk.read(source)
        # Unreal's space: centimetres, left-handed. Into the game's: metres, y negated; the triangle order stays as it is.
        P = m["points"] * np.array([0.01, -0.01, 0.01])
        return P, m["wedge_point"][m["faces"]], os.path.basename(source)
    import meshread
    P, F = meshread.shape(source)
    return P, F, source


# ---------------------------------------------------------------- a scene from above

LAYER_COLOURS = ((0.36, 0.44, 0.24), (0.47, 0.45, 0.42), (0.42, 0.50, 0.28), (0.86, 0.87, 0.90), (0.50, 0.42, 0.30), (0.30, 0.38, 0.20),
                 (0.62, 0.56, 0.40), (0.40, 0.36, 0.30), (0.55, 0.60, 0.35), (0.34, 0.30, 0.26), (0.70, 0.66, 0.52), (0.26, 0.34, 0.22),
                 (0.58, 0.50, 0.44), (0.46, 0.52, 0.40), (0.38, 0.40, 0.44), (0.66, 0.62, 0.58))


def _layer_colour(name, i):
    n = name.lower()
    for words, c in ((("snow", "ice"), (0.88, 0.89, 0.92)), (("rock", "cliff", "stone"), (0.50, 0.49, 0.47)), (("sand", "dune", "desert"), (0.76, 0.68, 0.48)),
                     (("grass", "meadow", "moss"), (0.38, 0.50, 0.26)), (("soil", "dirt", "ground", "mud", "earth"), (0.46, 0.38, 0.28)),
                     (("road", "path"), (0.58, 0.52, 0.42)), (("forest", "leaf", "leaves"), (0.26, 0.36, 0.20))):
        if any(w in n for w in words): return c
    return LAYER_COLOURS[i % len(LAYER_COLOURS)]


def scene_map(folder, out, size=1024, season="summer"):
    """A top view of a scene folder. Returns what was drawn, as numbers."""
    import scene, terrain
    xs = os.path.join(folder, "scene.xscene")
    info = terrain.scene_terrain_info(xs)
    if info is None: raise ValueError("%s has no terrain" % xs)
    T = terrain.Terrain(os.path.join(folder, "terrain.bin")); z = T.heightmap(info)
    cell = info["node_size"] / 256.0; H, W = z.shape; step = max(1, int(math.ceil(max(H, W) / size)))
    zz = z[::step, ::step]; shade = terrain.hillshade(zz, cell * step)[::-1].astype(np.float64) / 255.0      # hillshade returns north up: back to row 0 = y 0
    # Colour: the strongest texture layer of every sample, among the layers the season shows (a snow layer is painted in
    # every scene that has winter, and switched off in the other seasons).
    doc = scene.load(xs)
    layers = doc.root.find("terrain").find("layers").elements("layer")
    shown = [(l.find(season) is not None and l.find(season).get("is_enabled") == "true") for l in layers]
    top = np.zeros(zz.shape, np.int64); share = {}
    try:
        nodes = T.weight_nodes(); best = np.zeros(zz.shape, np.float64)
        for i in range(1, min(len(layers), max(len(n) for n in nodes))):
            if not shown[i]: continue
            parts = [n[i] if i < len(n) and n[i] is not None else np.zeros((terrain.NODE, terrain.NODE), np.uint8) for n in nodes]
            w = terrain.join_nodes(parts, info["nx"], info["ny"])[::step, ::step][:zz.shape[0], :zz.shape[1]].astype(np.float64)
            take = (w > best) & (w > 96); best[take] = w[take]; top[take] = i
        share = {info["layers"][i]: round(float((top == i).mean()), 3) for i in range(len(layers)) if (top == i).any()}
    except Exception as e:                                             # a terrain without weight chunks still gets its relief
        share = {"error": str(e)}
    palette = np.array([_layer_colour(info["layers"][i] if i < len(info["layers"]) else "", i) for i in range(max(1, len(layers)))])
    colour = palette[np.minimum(top, len(palette) - 1)]
    img = colour * (0.25 + 1.6 * shade[:, :, None])
    water = None
    env = doc.root.find("environment_properties"); wp = env.find("water_properties") if env is not None else None
    if wp is not None:
        props = {p.get("name"): p.get("value") for p in wp.elements("property")}
        if props.get("water_exists") == "true": water = float(props.get("water_level", "0"))
    if water is not None:
        wet = zz < water; img[wet] = img[wet] * 0.35 + np.array([0.16, 0.34, 0.52]) * 0.65
    img = img[::-1]                                                     # row 0 of the terrain is y = 0: north goes up
    sx = (img.shape[1] - 1) / (W - 1) / cell; sy = (img.shape[0] - 1) / (H - 1) / cell
    def px(x, y): return (x * sx, img.shape[0] - 1 - y * sy)
    counts = {"rocks": 0, "trees": 0, "other": 0}
    for e in doc.entities(True):
        p = e.position
        if p is None: continue
        name = ((e.prefab or "") + " " + (e.name or "")).lower()
        if "border" in name or "walk_area" in " ".join(e.tags): continue
        kind = "rocks" if ("rock" in name or "cliff" in name or "stone" in name) else "trees" if ("tree" in name or "forest" in name or "bush" in name or "pine" in name) else "other"
        counts[kind] += 1
        x, y = px(p[0], p[1]); i, j = int(round(x)), int(round(y))
        if 1 <= i < img.shape[1] - 1 and 1 <= j < img.shape[0] - 1:
            img[j - 1:j + 2, i - 1:i + 2] = {"rocks": (0.80, 0.80, 0.82), "trees": (0.10, 0.36, 0.12), "other": (0.95, 0.55, 0.15)}[kind]
    lines = []
    paths = doc.root.find("Paths"); n_paths = 0
    if paths is not None:
        for pe in paths.elements("path"):
            pts = [tuple(float(v) for v in pt.get("position").split(",")) for pt in pe.find("points").elements("point")]
            if len(pts) >= 2: lines.append(([px(p[0], p[1]) for p in pts], (255, 235, 60), 2)); n_paths += 1
    corners = [e.position for e in doc.entities() if "walk_area_vertex" in e.tags and e.position is not None]
    if len(corners) >= 3:
        cx = sum(p[0] for p in corners) / len(corners); cy = sum(p[1] for p in corners) / len(corners)
        corners.sort(key=lambda p: math.atan2(p[1] - cy, p[0] - cx))
        lines.append(([px(p[0], p[1]) for p in corners + corners[:1]], (255, 255, 255), 1))
    _save(img, out, labels=[(8, 6, "%s   %.0f x %.0f m   heights %.1f to %.1f m" % (os.path.basename(os.path.normpath(folder)), (W - 1) * cell, (H - 1) * cell, float(z.min()), float(z.max())))], lines=lines)
    return {"size_m": [round((W - 1) * cell, 1), round((H - 1) * cell, 1)], "heights_m": [round(float(z.min()), 2), round(float(z.max()), 2)],
            "entities": counts, "spawn_paths": n_paths, "border_corners": len(corners), "water_level": water, "strongest_layer_share": share,
            "pixels": [int(img.shape[1]), int(img.shape[0])]}


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "mesh":
        P, F, title = load_mesh(a[1]); print(mesh_sheet(P, F, a[2], int(a[3]) if len(a) > 3 else 512, title))
    elif len(a) >= 3 and a[0] == "scene":
        print(scene_map(a[1], a[2], int(a[3]) if len(a) > 3 else 1024))
    else:
        print(__doc__)
