"""Builds a complete scene folder (scene.xscene, terrain.bin, navmesh.bin, atmosphere.xml, flora.bin, ShaderCache) from
arrays, starting from a shipped scene as the template.

The template supplies everything this module does not generate: the terrain's texture layers, the outer mesh, the
atmosphere, the terrain shader header. The caller supplies the ground (heights, layer masks) and what stands on it
(entities, spawn paths, the walkable border, the navmesh). Nothing of the template is copied into this repository: it is
read from the user's install when a scene is built.

What the scene file needs to agree with terrain.bin (found by comparing shipped scenes, game v1.4.8.119303):
  <terrain min_height max_height>         the range the 16-bit heights span
  <node idx idy>                          idx = column, idy = row; terrain.bin node k = idy * nodes_x + idx
  layer_is_used_mask_<season>             bit i set when layer i has a weight map in the node AND is enabled that season
  node min_height / max_height            the node's own range
Layer weights are not normalised: layer 0 is the base at 255, every later layer is an opacity painted over the ones before.
The physics map of a season holds, per sample, the index of the layer whose material counts there.
"""
import os, shutil, struct, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scene, terrain, navmesh, terraform
else:
    from . import scene, terrain, navmesh, terraform

SEASONS = ("summer", "fall", "winter", "spring")
EMPTY_FLORA = b"FLR2" + struct.pack("<II", 4, 0)


class Template:
    """A shipped scene folder used as the starting point."""

    def __init__(self, folder):
        self.folder = folder
        self.doc = scene.load(os.path.join(folder, "scene.xscene"))
        self.terrain_el = self.doc.root.find("terrain")
        assert self.terrain_el is not None, "the template has no terrain"
        self.nx = int(self.terrain_el.get("node_dimension_x")); self.ny = int(self.terrain_el.get("node_dimension_y"))
        self.node_size = float(self.terrain_el.get("node_size"))
        self.layers = self.terrain_el.find("layers").elements("layer")
        self.layer_names = [l.get("name") for l in self.layers]
        self.enabled = {s: [(l.find(s) is not None and l.find(s).get("is_enabled") == "true") for l in self.layers] for s in SEASONS}
        self.flora_layers = [i for i, l in enumerate(self.layers) if l.find("summer") is not None and l.find("summer").get("is_flora_layer") == "true"]
        self.physics = [(l.find("summer").get("physics_material") if l.find("summer") is not None else None) for l in self.layers]

    @property
    def size(self): return (self.nx * self.node_size, self.ny * self.node_size)

    @property
    def samples(self): return (self.ny * 256 + 1, self.nx * 256 + 1)

    @property
    def cell(self): return self.node_size / 256.0


def physics_maps(template, masks, shape, threshold=128):
    """Per season the index of the topmost enabled layer whose mask reaches the threshold (layer 0 elsewhere)."""
    out = np.zeros((len(SEASONS),) + shape, np.uint8)
    for s, season in enumerate(SEASONS):
        for i in sorted(masks):
            if i == 0 or not template.enabled[season][i]: continue
            out[s][masks[i] >= threshold] = i
    return out


def grid_navmesh(z, cell, walkable, step=4.0, max_slope=0.75):
    """A navmesh of square faces `step` metres wide over the samples where `walkable` is true and the ground is not steeper
    than max_slope (rise over run). Only the largest connected area is kept. z: heights in metres, row = y."""
    k = max(1, int(round(step / cell)))
    sl = terraform.slope(terraform.blur_fast(z, 2), cell)
    ok = walkable & (sl <= max_slope)
    gy = (z.shape[0] - 1) // k; gx = (z.shape[1] - 1) // k
    # a cell is walkable when its four corners and its centre are
    c = ok[0:gy * k + 1:k, 0:gx * k + 1:k]
    cells = c[:-1, :-1] & c[1:, :-1] & c[:-1, 1:] & c[1:, 1:] & ok[k // 2:gy * k:k, k // 2:gx * k:k]
    # largest connected component (4-neighbour flood from the fullest label)
    lab = np.zeros(cells.shape, np.int32); cur = 0; best = (0, 0)
    for sy, sx in zip(*np.nonzero(cells)):
        if lab[sy, sx]: continue
        cur += 1; stack = [(sy, sx)]; lab[sy, sx] = cur; n = 0
        while stack:
            y, x = stack.pop(); n += 1
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < cells.shape[0] and 0 <= xx < cells.shape[1] and cells[yy, xx] and not lab[yy, xx]:
                    lab[yy, xx] = cur; stack.append((yy, xx))
        if n > best[0]: best = (n, cur)
    keep = lab == best[1] if best[0] else cells
    vid = -np.ones((gy + 1, gx + 1), np.int64); verts = []; polys = []
    def v(j, i):
        if vid[j, i] < 0:
            vid[j, i] = len(verts); verts.append((i * k * cell, j * k * cell, float(z[j * k, i * k])))
        return int(vid[j, i])
    for j, i in zip(*np.nonzero(keep)):
        polys.append((v(j, i), v(j, i + 1), v(j + 1, i + 1), v(j + 1, i)))     # counter-clockwise seen from above
    return navmesh.NavMesh.from_polygons(np.asarray(verts, np.float32).reshape(-1, 3), polys), keep


def build(template, out_dir, name, heights, masks=None, entities=(), spawn_paths=(), border=(), walkable=None,
          nav_step=4.0, nav_max_slope=0.75, author="bannerlord-editor-free-sdk", log=print):
    """Writes the scene folder out_dir for a scene called `name`.

    heights      float array in metres, shape template.samples (row = y)
    masks        {layer index: uint8 array of the same shape}: the opacity of each painted layer; layer 0 needs none
    entities     list of dicts for scene.Scene.add_entity (prefab=, position=, rotation=, scale=, tags=, scripts=, name=)
    spawn_paths  list of point lists [(x, y, z), ...]; they become spawn_path_00, spawn_path_01, ...
    border       [(x, y), ...]: the walkable area's corners (the game sorts them around their centre)
    walkable     bool array: where the navmesh may go (default: inside the border)
    Returns a dict of what was written."""
    t = template; masks = dict(masks or {})
    assert heights.shape == t.samples, (heights.shape, t.samples)
    os.makedirs(out_dir, exist_ok=True)
    cell = t.cell; nx, ny = t.nx, t.ny; L = len(t.layers)
    zmin = float(heights.min()); zmax = float(heights.max())
    if zmax - zmin < 1e-3: zmax = zmin + 1.0
    h16 = np.clip(np.rint((heights - zmin) / (zmax - zmin) * 65535.0), 0, 65535).astype(np.uint16)
    z = zmin + h16.astype(np.float64) / 65535.0 * (zmax - zmin)          # what the game will see

    # ---- terrain.bin
    normals = terraform_normals(z, cell)
    phys = physics_maps(t, masks, heights.shape)
    hn = terrain.split_nodes(h16, nx, ny); nn = terrain.split_nodes(normals, nx, ny)
    mn = {i: terrain.split_nodes(m, nx, ny) for i, m in masks.items()}
    base = np.full((terrain.NODE, terrain.NODE), 255, np.uint8)
    weights = []; present = []
    for k in range(nx * ny):
        layers = [None] * L; layers[0] = base
        for i, parts in mn.items():
            if i != 0 and parts[k].any(): layers[i] = parts[k]
        weights.append(layers); present.append([w is not None for w in layers])
    ps = [terrain.split_nodes(phys[s], nx, ny) for s in range(len(SEASONS))]
    pn = [np.stack([ps[s][k] for s in range(len(SEASONS))]) for k in range(nx * ny)]
    blank = np.full((ny * 256, nx * 256), 255, np.uint8)
    data = {"version": 2, "heights": hn, "normals": nn, "weights": weights, "index_maps": [blank] * 4, "physics": pn}
    tb = terrain.build(data)
    with open(os.path.join(out_dir, "terrain.bin"), "wb") as f: f.write(tb)
    log("terrain.bin %d bytes, heights %.1f..%.1f m" % (len(tb), zmin, zmax))

    # ---- scene.xscene
    doc = scene.loads(t.doc.tobytes())
    doc.root.set("name", name)
    ter = doc.root.find("terrain")
    ter.set("min_height", "%.3f" % zmin); ter.set("max_height", "%.3f" % zmax)
    for node in ter.find("nodes").elements("node"):
        ix, iy = int(node.get("idx")), int(node.get("idy")); k = iy * nx + ix
        zz = z[iy * 256:iy * 256 + 257, ix * 256:ix * 256 + 257]
        for season in SEASONS:
            node.set("layer_is_used_mask_" + season, sum(1 << i for i in range(L) if present[k][i] and t.enabled[season][i]))
        node.set("height_complexity", "%.3f" % min(1.0, 1.1 * float(terraform.slope(zz, cell).mean())))
        node.set("min_height", "%.3f" % float(zz.min())); node.set("max_height", "%.3f" % float(zz.max()))
    ents = doc.entities_element()
    for e in list(ents.elements()): ents.remove(e)
    def ground(x, y):
        i = min(z.shape[1] - 1, max(0, int(round(x / cell)))); j = min(z.shape[0] - 1, max(0, int(round(y / cell))))
        return float(z[j, i])
    for x, y in border:
        el = doc.add_entity(name="border_soft", position=(x, y, ground(x, y)), tags=("walk_area_vertex",))
        vm = scene.Element("visibility_masks", None, [scene.Element("visibility_mask", [["name", "visible_only_when_editing"], ["value", "true"]])])
        el.el.children.insert(0, vm); vm.parent = el.el
        el.el._reindent(el.el.depth(), doc._nl)
    for e in entities: doc.add_entity(**e)
    paths = doc.root.find("Paths")
    if paths is None: paths = doc.root.append(scene.Element("Paths"))
    for p in list(paths.elements()): paths.remove(p)
    for n, pts in enumerate(spawn_paths):
        pe = scene.Element("path", [["name", "spawn_path_%02d" % n], ["interpolation_type", "linear"], ["scene_upgrade_level_mask_", "4294967295"], ["season_mask_", "15"]])
        pe.children.append(scene.Element("flags")); pl = scene.Element("points"); pe.children.append(pl)
        for (x, y, zz) in pts:
            pt = scene.Element("point", [["position", scene._fmt((x, y, zz))], ["scale", "1.000, 1.000, 1.000"], ["color", "1.000, 1.000, 1.000, 1.000"], ["rotation", "0.00000, 0.00000, 0.00000, 1.00000"]])
            pt.children.append(scene.Element("first_tangent", [["position", scene._fmt((x, y + 2.0, zz))]]))
            pt.children.append(scene.Element("second_tangent", [["position", scene._fmt((x, y - 2.0, zz))]]))
            for c in pt.children: c.parent = pt
            pl.children.append(pt); pt.parent = pl
        for c in pe.children: c.parent = pe
        paths.append(pe)
    ed = doc.root.find("editor_data")
    if ed is not None: ed.set("author", author); ed.set("comment", name)
    doc.save(os.path.join(out_dir, "scene.xscene"))

    # ---- navmesh.bin
    if walkable is None:
        walkable = polygon_mask(border, heights.shape, cell) if len(border) >= 3 else np.ones(heights.shape, bool)
    nm, cells = grid_navmesh(z, cell, walkable, nav_step, nav_max_slope)
    nm.save(os.path.join(out_dir, "navmesh.bin"))
    st = nm.stats(); log("navmesh.bin %d faces, %.0f m2" % (st["faces"], st["area_m2"]))

    # ---- the rest
    with open(os.path.join(out_dir, "flora.bin"), "wb") as f: f.write(EMPTY_FLORA)
    for fn in ("atmosphere.xml",):
        src = os.path.join(t.folder, fn)
        if os.path.isfile(src): shutil.copyfile(src, os.path.join(out_dir, fn))
    sc = os.path.join(t.folder, "ShaderCache")
    if os.path.isdir(sc): shutil.copytree(sc, os.path.join(out_dir, "ShaderCache"), dirs_exist_ok=True)
    return {"min": zmin, "max": zmax, "navmesh": st, "entities": len(doc.entities()), "paths": len(spawn_paths), "z": z, "nav_cells": cells,
            "layers_painted": sorted(masks)}


def terraform_normals(z, cell): return terrain.normals_from_heights(z, cell)


def polygon_mask(points, shape, cell):
    """True for the samples inside the polygon (points in metres, sorted around their centre like the game does)."""
    p = np.asarray(points, np.float64); c = p.mean(axis=0)
    p = p[np.argsort(np.arctan2(p[:, 1] - c[1], p[:, 0] - c[0]))]
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    x = xx * cell; y = yy * cell
    inside = np.zeros(shape, bool); n = len(p); j = n - 1
    for i in range(n):
        xi, yi = p[i]; xj, yj = p[j]
        cross = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi)
        inside ^= cross; j = i
    return inside
