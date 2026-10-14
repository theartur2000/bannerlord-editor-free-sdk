"""Editing the campaign world map's heights.

What works (verified in game 2026-10-14): write_height_textures, at the end of this file and behind `bl worldmap.bump`.
The shipped map takes its heights from two textures, not from terrain.bin; a module overrides them by name and the map
keeps its shipped look. Everything above that section is the record of how this was found, plus a scene-copy route
(copy_map, set_heights, bump) that works only with terrain_render_version removed from the copy, which draws the map
without its colours.

Measured in game (2026-10-14): a module's SceneObj/Main_map/scene.xscene is the one the campaign reads (the log names it,
and MapScene.GetMainMapModule picks the last active module that has one), and the copy's terrain.bin written here holds the
edit, but the map is drawn and queried (MapSceneWrapper.GetHeightAtPoint) exactly as shipped: a 6-unit rise was neither
visible nor measurable. So `bl` offers no map height command; what follows writes valid files for that next step.

Narrowed down in game the same day (campaign started by the test driver, heights read with `bl live mapheight`):
- vista_tileset="none" in the module's scene.xscene does take effect: the map is then drawn without the tile set's colours
  (dark ground; the map mods TOR and Shokuho set it and ship their own vista_diffuse_name / vista_normalmap textures).
- The heights stay SandBox's (they match its terrain.bin to about 0.005) whether the module's terrain.bin holds a +10 rise,
  is a 0-byte file, or the module is loaded before Sandbox. They also stay the same while each of these is locked against
  any opening for the whole run: SandBox's terrain.bin, SandBox/SceneEditData/Main_map/terrain_ed.bin, an inactive
  module's copy of the map, and (with the tile set off) every file of SandBox's tile set. The source of the map's heights
  is therefore none of those files; the engine side is being traced (bl2_re JOURNAL.md, 2026-10-14).
- The campaign's height query (MapScene.GetHeightAtPoint) is a physics query on the scene, not the navmesh, but the party
  navmesh's vertices lie about half a unit above the terrain, so set_heights moves them by the same change.


SandBox's map scene (MapScene.GetMainMapModule) is read from the last active module that has SceneObj/Main_map/scene.xscene,
so a module carries an edited copy of the map scene. What this module edits: the map's heights (terrain.bin, 16 x 16 nodes
of 53 map units, 16-bit heights between the scene's min and max). The map's look comes from its TileSets folder (2.1 GB of
texture tiles); the copy links to SandBox's instead of copying it (a directory junction), so heights can change without
duplicating the tiles. Settlements, roads and the party navmesh stay as they are: moving land under a town or raising a
mountain across a road needs the navmesh (navmesh.bin, faces with terrain types) changed too, which is not done here.

    copy_map(project_dir)                          -> <project>/SceneObj/Main_map (files copied, TileSets linked)
    bump(scene_dir, x, y, radius, height)          a smooth rise (or dip, height < 0) in map units around x, y
    set_heights(scene_dir, z)                      replace the whole height field (array of map units, row = y)
"""
import os, shutil, subprocess, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def source_dir():
    import config
    return os.path.join(config.MODULES, "SandBox", "SceneObj", "Main_map")


def copy_map(project, link_tilesets=True, dry=False):
    src = source_dir(); dst = os.path.join(project, "SceneObj", "Main_map")
    if not os.path.isfile(os.path.join(src, "scene.xscene")): raise ValueError("no map scene at " + src)
    if os.path.exists(dst): raise ValueError("%s exists already" % dst)
    files = sorted(os.listdir(src))
    if dry: return {"would_copy": [f for f in files if f != "TileSets"], "tilesets": "link" if link_tilesets else "copy", "to": dst}
    os.makedirs(dst)
    for f in files:
        s = os.path.join(src, f); d = os.path.join(dst, f)
        if f == "TileSets" and link_tilesets:
            # a directory junction needs no rights beyond writing the folder; copying would be 2.1 GB
            r = subprocess.run(["cmd", "/c", "mklink", "/J", d, s], capture_output=True, text=True)
            if r.returncode != 0: raise ValueError("could not link TileSets: " + (r.stdout + r.stderr).strip())
        elif os.path.isdir(s): shutil.copytree(s, d)
        else: shutil.copyfile(s, d)
    return {"map": dst, "tilesets": "linked to " + os.path.join(src, "TileSets") if link_tilesets else "copied"}


def _read(scene_dir):
    import terrain
    info = terrain.scene_terrain_info(os.path.join(scene_dir, "scene.xscene"))
    T = terrain.Terrain(os.path.join(scene_dir, "terrain.bin"))
    return info, T, T.heightmap(info)


def set_heights(scene_dir, z):
    """Writes the height field z (map units, the map's own shape) into terrain.bin and the scene's height ranges."""
    import terrain, scene
    info, T, old = _read(scene_dir)
    z = np.asarray(z, np.float64)
    if z.shape != old.shape: raise ValueError("heights of shape %s, the map has %s" % (z.shape, old.shape))
    lo, hi = float(z.min()), float(z.max())
    if hi - lo < 1e-6: hi = lo + 1.0
    # the shipped range is kept when the new heights fit in it, so unchanged nodes keep their exact 16-bit values
    if lo >= info["min"] and hi <= info["max"]: lo, hi = info["min"], info["max"]
    h16 = np.clip(np.rint((z - lo) / (hi - lo) * 65535.0), 0, 65535).astype(np.uint16)
    data = T.load(); old_nodes = data["heights"]; data["heights"] = terrain.split_nodes(h16, info["nx"], info["ny"])
    changed = [k for k, (a, b) in enumerate(zip(old_nodes, data["heights"])) if not np.array_equal(a, b)]
    blob = terrain.build(data)
    with open(os.path.join(scene_dir, "terrain.bin"), "wb") as f: f.write(blob)
    zq = lo + h16.astype(np.float64) / 65535.0 * (hi - lo)
    doc = scene.load(os.path.join(scene_dir, "scene.xscene")); ter = doc.root.find("terrain")
    if (lo, hi) != (info["min"], info["max"]): ter.set("min_height", "%.3f" % lo); ter.set("max_height", "%.3f" % hi)
    for node in ter.find("nodes").elements("node"):
        ix, iy = int(node.get("idx")), int(node.get("idy")); zz = zq[iy * 256:iy * 256 + 257, ix * 256:ix * 256 + 257]
        if node.get("min_height") is not None and ((lo, hi) != (info["min"], info["max"]) or iy * info["nx"] + ix in changed):   # untouched nodes keep their text
            node.set("min_height", "%.3f" % float(zz.min())); node.set("max_height", "%.3f" % float(zz.max()))
    doc.save()
    moved = _move_navmesh(scene_dir, zq - old, info["node_size"] / 256.0)
    return {"min": round(lo, 3), "max": round(hi, 3), "terrain_bytes": len(blob), "nodes_changed": len(changed), "navmesh_vertices_moved": moved}


def _move_navmesh(scene_dir, dz, cell):
    """Raises or lowers the party navmesh's vertices by the terrain's change under them. The campaign takes heights from
    the navmesh (MapSceneWrapper.GetHeightAtPoint), whose vertices lie about half a unit above the terrain."""
    import navmesh
    p = os.path.join(scene_dir, "navmesh.bin")
    if not os.path.isfile(p): return 0
    n = navmesh.NavMesh(p); v = n.vertices
    fx = np.clip(v[:, 0] / cell, 0, dz.shape[1] - 1.001); fy = np.clip(v[:, 1] / cell, 0, dz.shape[0] - 1.001)
    i0 = fx.astype(int); j0 = fy.astype(int); tx = fx - i0; ty = fy - j0
    d = (dz[j0, i0] * (1 - tx) * (1 - ty) + dz[j0, i0 + 1] * tx * (1 - ty) + dz[j0 + 1, i0] * (1 - tx) * ty + dz[j0 + 1, i0 + 1] * tx * ty)
    moved = int((np.abs(d) > 1e-4).sum())
    if moved:
        n.vertices = (v + np.stack([np.zeros_like(d), np.zeros_like(d), d], 1)).astype(np.float32); n.save(p)
    return moved


def bump(scene_dir, x, y, radius, height):
    """A smooth rise of `height` map units at x, y falling to nothing at `radius` (a cosine profile)."""
    info, T, z = _read(scene_dir)
    cell = info["node_size"] / 256.0
    jj, ii = np.mgrid[0:z.shape[0], 0:z.shape[1]]
    d = np.hypot(ii * cell - x, jj * cell - y)
    w = np.where(d < radius, 0.5 + 0.5 * np.cos(np.pi * np.minimum(d / radius, 1.0)), 0.0)
    before = float(z[int(round(y / cell)), int(round(x / cell))])
    res = set_heights(scene_dir, z + height * w)
    res.update(at=[x, y], height_before=round(before, 3), height_after=round(before + height, 3), samples_changed=int((w > 0).sum()))
    return res


# ---- the working route: the map's height textures (found by native tracing, verified in game 2026-10-14)
#
# The shipped map's <terrain> has terrain_render_version="1". With it the engine takes the map's heights from two textures
# looked up by name, not from terrain.bin: main_map_physics_heightmap (2048 x 2048, the PhysX heightfield behind
# GetHeightAtPoint, so party positions and battles) and world_map_heightmap (4096 x 4096, what the map renderer draws).
# Both are R16_UNORM, cover the whole map (nodes x node_size), store the far edge (largest y) in row 0, and a value v means
# min_height + v / 65535 * (max_height - min_height) with the scene's own min and max. The War Sails module overrides both
# by name from its own packages, and a module can do the same: no copy of the map scene, its tile set or terrain.bin is
# needed, and the map keeps its shipped look (the tile set's colours stay where they are, on the new relief).
# Map flora (trees) keeps its stored heights: ground raised under a forest buries it, ground lowered leaves it floating.

HEIGHT_TEXTURES = (("main_map_physics_heightmap", "pack_9.tpac"), ("world_map_heightmap", "pack_19.tpac"))


def shipped_texture(name, hint=None):
    """A shipped R16 texture of Native's packages by name, as stored (uint16, row 0 first). hint: the package to try first."""
    import glob, tpac, tpac_pack, config
    folder = os.path.join(config.MODULES, "Native", "AssetPackages")
    paths = sorted(glob.glob(os.path.join(folder, "*.tpac")))
    if hint and os.path.join(folder, hint) in paths: paths.remove(os.path.join(folder, hint)); paths.insert(0, os.path.join(folder, hint))
    for p in paths:
        meta = tpac_pack.read_package(p, data=False)
        for it in meta["items"]:
            if (it["name"].decode() if isinstance(it["name"], bytes) else it["name"]) != name: continue
            r, _ = tpac.parse_tex_record(it["record"], 0)
            if r["format"] != "R16_UNORM": raise ValueError("%s is %s, not R16_UNORM" % (name, r["format"]))
            for e in it["entries"]:
                if e["rest"][16:32] != tpac.TEX_PIXELS: continue
                with open(p, "rb") as f: f.seek(e["off"]); d = f.read(e["stored"])
                if e["stored"] < e["raw"]: d = tpac.lz4_decompress(d, e["raw"])
                return np.frombuffer(d[:r["w"] * r["h"] * 2], "<u2").reshape(r["h"], r["w"]).copy()
    raise ValueError("no texture %s in %s" % (name, folder))


def edits_field(edits, shape, cell):
    """The height change (map units, the terrain grid's shape) of a list of edits; each edit is
    {"op": "bump", "x", "y", "radius", "height"}: a smooth rise (or dip) with a cosine profile."""
    dz = np.zeros(shape, np.float64)
    for e in edits:
        if e.get("op", "bump") != "bump": raise ValueError("unknown map edit %r" % e.get("op"))
        r = float(e["radius"]); x, y = float(e["x"]), float(e["y"])
        j0, j1 = max(0, int((y - r) / cell)), min(shape[0], int((y + r) / cell) + 2)
        i0, i1 = max(0, int((x - r) / cell)), min(shape[1], int((x + r) / cell) + 2)
        jj, ii = np.mgrid[j0:j1, i0:i1]
        d = np.hypot(ii * cell - x, jj * cell - y)
        dz[j0:j1, i0:i1] += np.where(d < r, 0.5 + 0.5 * np.cos(np.pi * np.minimum(d / r, 1.0)), 0.0) * float(e["height"])
    return dz


def write_height_textures(edits, out_dir, rdc_dir, png_dir=None, dry=False):
    """Writes main_map_physics_heightmap and world_map_heightmap (the shipped ones plus the edits) as texture packages
    into out_dir (and their caches into rdc_dir), ready for install. The scene's height range stays as shipped, so the
    edited heights are clipped to it; the result says how many pixels were."""
    import tpac, pngio
    info, T, z = _read(source_dir())
    lo, hi = info["min"], info["max"]; cell = info["node_size"] / 256.0
    dz = edits_field(edits, z.shape, cell)
    out = {"edits": len(edits), "range": [lo, hi], "textures": []}
    for name, hint in HEIGHT_TEXTURES:
        v = shipped_texture(name, hint).astype(np.float64); n = v.shape[0]
        idx = np.minimum((np.arange(n) * (z.shape[0] - 1) / n).astype(int), z.shape[0] - 1)
        nv = v + dz[np.ix_(idx, idx)][::-1] / (hi - lo) * 65535.0
        clipped = int(((nv < 0) | (nv > 65535)).sum()); nv = np.clip(nv, 0, 65535)
        row = {"name": name, "size": n, "pixels_changed": int((np.abs(nv - v) >= 0.5).sum()), "pixels_clipped": clipped}
        if not dry:
            os.makedirs(png_dir or out_dir, exist_ok=True)
            png = os.path.join(png_dir or out_dir, name + ".png")          # an 8-bit look at it: the package keeps all 16 bits
            pngio.write_png(png, (nv / 257.0).astype(np.uint8))
            img = np.zeros((n, n, 4), np.float32); img[..., 0] = nv / 65535.0; img[..., 3] = 1.0
            path, rdc, _ = tpac.write_texture(png, name=name, fmt="r16", usage="heightmap", mips=False, force=True,
                                              out_dir=out_dir, rdc_dir=rdc_dir, image=img)
            row.update(package=path, cache=rdc)
        out["textures"].append(row)
    return out


def height_at(edits, x, y):
    """The shipped height at x, y and the height after the edits (map units)."""
    info, T, z = _read(source_dir()); cell = info["node_size"] / 256.0
    j, i = int(round(y / cell)), int(round(x / cell))
    before = float(z[j, i]); return before, before + float(edits_field(edits, z.shape, cell)[j, i])
