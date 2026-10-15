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
import os, re, shutil, subprocess, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


_BASE = None          # the map the edits apply to: a generated continent (worldmap/continent of a project) or, when None, SandBox's


def base_dir():
    return _BASE or source_dir()


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
        if e.get("op", "bump") in ("move", "terrain"): continue     # settlement moves and terrain types change no ground
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
    """The height at x, y before (shipped, or the continent's) and after the edits (map units)."""
    info, T, z = _read(base_dir()); cell = info["node_size"] / 256.0
    j, i = int(round(y / cell)), int(round(x / cell))
    before = float(z[j, i]); return before, before + float(edits_field(edits, z.shape, cell)[j, i])


def map_flora(edits):
    """The shipped map's flora (flora.py dict) with every tree and plant moved up or down by the edits' change under it."""
    import flora
    info, T, z = _read(base_dir()); cell = info["node_size"] / 256.0
    dz = edits_field(edits, z.shape, cell)
    d = flora.read(base_dir()); P = flora.positions(d)
    j = np.clip(np.rint(P[:, 1] / cell).astype(int), 0, z.shape[0] - 1); i = np.clip(np.rint(P[:, 0] / cell).astype(int), 0, z.shape[1] - 1)
    shift = dz[j, i]; d["rec"]["m"][:, 3, 2] += shift.astype(np.float32)
    return d, int((np.abs(shift) > 1e-3).sum())


# The map's tile set (its colours) is opened from <module>/SceneObj/Main_map/TileSets: the engine takes the first .gts file
# there, whatever vista_tileset says (bl2_re JOURNAL 2026-10-14). The .gts names its 8 page files by paths relative to its own
# folder (UTF-16 slots of 0x218 bytes from 0xAAD38), so a 4 MB copy whose names point back at SandBox's pages is enough.
GTS_PAGES_AT, GTS_PAGE_SLOT, GTS_NAME_FIELD = 0xAAD38, 0x218, 0x200   # a slot: the name (256 UTF-16), then u32, 16-byte page checksum, u32
SANDBOX_PAGES = "../../../../SandBox/SceneObj/Main_map/TileSets/pages/"   # from <game>/Modules/<module>/SceneObj/Main_map/TileSets


def tileset_pointer(pages_prefix=SANDBOX_PAGES):
    """SandBox's WorldMap.gts with its page names pointing at SandBox's pages (bytes)."""
    b = bytearray(open(os.path.join(source_dir(), "TileSets", "WorldMap.gts"), "rb").read())
    for k in range(32):
        o = GTS_PAGES_AT + k * GTS_PAGE_SLOT
        name = bytes(b[o:o + GTS_PAGE_SLOT]).decode("utf-16-le", "replace").split("\0")[0]
        if not name.startswith("pages/"): break
        new = (pages_prefix + name[len("pages/"):]).encode("utf-16-le")
        if len(new) + 2 > GTS_NAME_FIELD: raise ValueError("page path too long for its slot: %s" % new.decode("utf-16-le"))
        b[o:o + GTS_NAME_FIELD] = new + bytes(GTS_NAME_FIELD - len(new))   # the checksum after the name stays
    else: raise ValueError("more page names than expected in WorldMap.gts")
    if k == 0: raise ValueError("no page names at the expected place in WorldMap.gts (another game version?)")
    return bytes(b)


def write_map_scene(edits, project, tileset="pointer", dry=False, look=None, stage=None, module=None):
    """<project>/SceneObj/Main_map: the shipped map scene as is, with its flora moved by the edits (so trees stand on raised
    ground) and settlements moved. The game then reads this folder's scene.xscene, flora.bin and tile set. tileset: "pointer"
    (a 4 MB WorldMap.gts whose pages are SandBox's, the default: shippable), "link" (a directory junction to SandBox's
    TileSets) or "none" (the map's ground is then drawn without its colours).
    look: None = the project's worldmap/look.json (else "shipped"). {"mode": "painted", "image": optional PNG} draws the map
    from terrain.bin (the edits' heights) with one colour texture (painted from the terrain types and the relief, or the
    image), staged into `stage` as <module>_worldmap_colour: the shipped renderer and its tile set are then not used."""
    import json
    global _BASE
    cont = os.path.join(project, "worldmap", "continent")
    _BASE = cont if os.path.isfile(os.path.join(cont, "scene.xscene")) else None
    if look is None:
        lp = os.path.join(project, "worldmap", "look.json")
        look = json.load(open(lp)) if os.path.isfile(lp) else {"mode": "shipped"}
    painted = look.get("mode") == "painted" or _BASE is not None     # a generated continent has no shipped look
    import flora
    src = base_dir(); dst = os.path.join(project, "SceneObj", "Main_map")
    d, moved = map_flora(edits)
    if dry: return {"map_scene": dst, "trees_moved": moved}
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        s = os.path.join(src, f); t = os.path.join(dst, f)
        if f in ("TileSets", "flora.bin", "scene.xscene"): continue
        if os.path.isdir(s):
            if not os.path.isdir(t): shutil.copytree(s, t)
        elif f == "terrain.bin" or not os.path.isfile(t) or os.path.getsize(t) != os.path.getsize(s): shutil.copyfile(s, t)
    flora.write(dst, d)
    moves = apply_moves(edits, os.path.join(src, "scene.xscene"), os.path.join(dst, "scene.xscene"))
    colour = None
    if painted:                                   # heights into terrain.bin (it moves the navmesh too, rewritten just below)
        info, T, z = _read(src); set_heights(dst, z + edits_field(edits, z.shape, info["node_size"] / 256.0))
    nav = write_map_navmesh(edits, os.path.join(src, "navmesh.bin"), os.path.join(dst, "navmesh.bin"))
    if painted:
        if not stage or not module: raise ValueError("the painted look needs the stage and the module name")
        colour = write_colour_map(edits, dst, stage, module, look.get("image"))
        name = colour["name"].encode()
        p = os.path.join(dst, "scene.xscene"); b = open(p, "rb").read()
        b = b.replace(b' terrain_render_version="1"', b"")
        import re
        b = re.sub(rb'vista_diffuse_name="[^"]*"', b'vista_diffuse_name="' + name + b'"', b)
        open(p, "wb").write(b)
        tileset = "none"
    ts = os.path.join(dst, "TileSets")
    if tileset == "none" and os.path.lexists(ts):
        if _is_junction(ts): os.rmdir(ts)
        else: shutil.rmtree(ts)
    if tileset == "link" and not os.path.lexists(ts):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", ts, os.path.join(src, "TileSets")], capture_output=True, text=True)
        if r.returncode != 0: raise ValueError("could not link TileSets: " + (r.stdout + r.stderr).strip())
    elif tileset == "pointer":
        if os.path.lexists(ts) and _is_junction(ts): os.rmdir(ts)          # never write through a link into SandBox's folder
        os.makedirs(ts, exist_ok=True)
        with open(os.path.join(ts, "WorldMap.gts"), "wb") as f: f.write(tileset_pointer())
    return {"map_scene": dst, "trees_moved": moved, "settlements_moved": moves, "navmesh": nav, "tilesets": tileset, "look": look.get("mode"), "colour_map": colour}


PALETTE = {0: (28, 52, 86), 1: (112, 138, 62), 2: (196, 172, 118), 3: (232, 232, 236), 4: (44, 82, 38), 5: (150, 142, 84), 6: (60, 96, 120),
           7: (122, 116, 106), 8: (40, 72, 110), 10: (36, 66, 104), 11: (50, 90, 125), 13: (110, 96, 80), 14: (120, 140, 70), 15: (70, 90, 60),
           16: (205, 180, 120), 17: (120, 100, 80), 18: (36, 66, 104), 19: (28, 52, 86), 20: (200, 190, 150), 21: (100, 95, 90),
           22: (50, 90, 125), 23: (112, 138, 62), 24: (36, 66, 104), 25: (50, 90, 125)}   # by terrain type (worldmap.TERRAIN_TYPES)


def paint_colours(scene_dir, size=2048):
    """An RGB picture of the map (rows from the far edge, as the map's textures): each spot coloured by its terrain type in
    scene_dir's navmesh, shaded by the slope of scene_dir's terrain.bin."""
    import worldmap
    T = worldmap.WorldMap(scene_dir).terrain_types(size)
    info, Tr, z = _read(scene_dir)
    idx = (np.arange(size) * (z.shape[0] - 1) / size).astype(int); h = z[np.ix_(idx, idx)]
    img = np.zeros((size, size, 3), np.float64)
    for k, c in PALETTE.items(): img[T == k] = c
    gy, gx = np.gradient(h, info["nx"] * info["node_size"] / size); slope = np.hypot(gx, gy)
    light = np.clip(1.0 - 0.6 * np.clip(slope, 0, 1) + 0.25 * (-gx - gy) / (1 + slope), 0.45, 1.25)
    return np.clip(img * light[..., None], 0, 255).astype(np.uint8)[::-1]


def write_colour_map(edits, scene_dir, stage, module, image=None):
    """Stages <module>_worldmap_colour (BC1 with mips) from `image` (any size; row 0 = the map's far edge) or painted."""
    import pngio, tpac
    name = module.lower() + "_worldmap_colour"
    png = os.path.join(scene_dir, "..", "..", "worldmap", name + ".png")
    os.makedirs(os.path.dirname(png), exist_ok=True)
    if image: shutil.copyfile(image, png)
    else: pngio.write_png(png, paint_colours(scene_dir))
    out, rdc, r = tpac.write_texture(png, name=name, fmt="bc1", force=True, out_dir=os.path.join(stage, "Assets"), rdc_dir=os.path.join(stage, "RuntimeDataCache"))
    return {"name": name, "source": os.path.normpath(png), "package": out, "size": [r["w"], r["h"]]}


def terrain_id(name):
    import worldmap
    ids = {v.lower(): k for k, v in worldmap.TERRAIN_TYPES.items()}
    if str(name).lower() not in ids: raise ValueError("no terrain type %s (one of %s)" % (name, ", ".join(worldmap.TERRAIN_TYPES.values())))
    return ids[str(name).lower()]


def _holds(P, c):
    """Whether the convex polygon P (n, 2) holds the point c."""
    s = None
    for k in range(len(P)):
        a, b = P[k], P[(k + 1) % len(P)]; x = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(x) < 1e-12: continue
        if s is None: s = x > 0
        elif (x > 0) != s: return False
    return True


def write_map_navmesh(edits, src, dst):
    """The party navmesh with its vertices raised or lowered by the bumps (they lie about half a unit above the ground) and the
    terrain type (face group) of the faces whose centre is inside a "terrain" edit's circle set to its type. Returns counts."""
    import navmesh
    n = navmesh.NavMesh(src)
    info, T, z = _read(base_dir()); cell = info["node_size"] / 256.0
    dz = edits_field(edits, z.shape, cell); v = n.vertices
    j = np.clip(np.rint(v[:, 1] / cell).astype(int), 0, z.shape[0] - 1); i = np.clip(np.rint(v[:, 0] / cell).astype(int), 0, z.shape[1] - 1)
    lift = dz[j, i]; n.vertices = (v + np.c_[np.zeros((len(v), 2)), lift]).astype(np.float32)
    faces = 0
    terr = [e for e in edits if e.get("op") == "terrain"]
    if terr:
        V2 = n.vertices[:, :2].astype(np.float64)
        for e in terr:
            c = np.array([float(e["x"]), float(e["y"])]); r = float(e["radius"]); t = terrain_id(e["type"])
            near = np.hypot(*(V2 - c).T) <= r                    # map faces are a few units across: a face counts when its
            for k, (vs, _) in enumerate(n.faces):                 # centre or a corner is inside the circle, or it holds the centre
                P = V2[list(vs)]
                if near[list(vs)].any() or np.hypot(*(P.mean(0) - c)) <= r or _holds(P, c):
                    n.face_group[k] = t; faces += 1
    n.save(dst)
    return {"vertices_moved": int((np.abs(lift) > 1e-4).sum()), "faces_retyped": faces}


def _is_junction(path):
    try: return os.path.islink(path) or bool(getattr(os.lstat(path), "st_file_attributes", 0) & 0x400)
    except OSError: return False


def _entity_chain(el, name, chain=()):
    """The map scene's entities from the top down to the one named `name` (scene.Entity list), or None."""
    import scene
    for c in el.elements("game_entity"):
        e = scene.Entity(c)
        if c.get("name") == name: return list(chain) + [e]
        ch = c.find("children")
        if ch is not None:
            r = _entity_chain(ch, name, tuple(chain) + (e,))
            if r: return r
    return None


def settlement_entity(scene_doc, sid):
    """(entity, global position) of a settlement's model in the map scene: the entity named after the settlement id, which
    hangs under an unrotated, unscaled group (its global position is the settlement's posX, posY)."""
    chain = _entity_chain(scene_doc.entities_element(), sid)
    if chain is None: raise ValueError("no entity named %s in the map scene" % sid)
    for p in chain[:-1]:
        if any(abs(r) > 1e-6 for r in (p.rotation or (0, 0, 0))) or (p.scale and any(abs(s - 1) > 1e-6 for s in p.scale)):
            raise ValueError("%s hangs under a turned or scaled entity (%s): not handled" % (sid, p.el.get("name")))
    pos = np.sum([np.asarray(e.position or (0, 0, 0), np.float64) for e in chain], axis=0)
    return chain[-1], pos


def ground_at(edits, x, y):
    """The map's ground height at x, y after the edits."""
    return height_at(edits, x, y)[1]


def apply_moves(edits, src_scene, dst_scene):
    """Writes the map scene with the settlement models of the edits' "move" entries at their new places (the same height above
    the ground as before). Returns [{id, from, to}]."""
    import scene
    doc = scene.load(src_scene); done = []
    for e in edits:
        if e.get("op") != "move": continue
        ent, pos = settlement_entity(doc, e["id"])
        lift = pos[2] - ground_at([], pos[0], pos[1])
        new = np.array([e["x"], e["y"], ground_at(edits, e["x"], e["y"]) + lift])
        ent.position = tuple(np.asarray(ent.position, np.float64) + new - pos)
        done.append({"id": e["id"], "from": [round(float(v), 3) for v in pos], "to": [round(float(v), 3) for v in new]})
    doc.save(dst_scene)
    return done


def settlement_data(sid):
    """posX, posY, gate_posX, gate_posY of a settlement in SandBox's settlements.xml (floats; gate None when absent)."""
    import re, config
    t = open(os.path.join(config.MODULES, "SandBox", "ModuleData", "settlements.xml"), encoding="utf-8").read()
    m = re.search(r'<Settlement\s[^>]*?id="%s"[^>]*>' % re.escape(sid), t, re.S)
    if not m: raise ValueError("no settlement %s in SandBox's settlements.xml" % sid)
    g = lambda k: (lambda r: float(r.group(1)) if r else None)(re.search(r'\s%s="([-0-9.]+)"' % k, m.group(0)))
    return g("posX"), g("posY"), g("gate_posX"), g("gate_posY")


# ---- a new continent: the base map an edit list starts from, generated from a heightmap image

DECOR = re.compile(r"^(_occ_quad|_decal|map_decal|decal |map_mountain_rock|mainmap_cliff|map_rock|map_bridge|wooden_brige|stone_bridge|"
                   r"map_icon_waterfall|water_plane|lake_|log_|editor_cube)")
SHORE = 1.6          # map units: land starts here, above the sea planes (0.4 to 1.3) of the shipped map scene
LAND_TYPES = {"Plain": 1, "Snow": 3, "Forest": 4, "Mountain": 7}
TREES = ("map_pine_c", "worldmap_tree_beech_01", "map_pine_a", "map_pine_b")


def _resample(a, n):
    """Bilinear resample of a 2-d array to (n, n)."""
    a = np.asarray(a, np.float64); h, w = a.shape
    y = np.linspace(0, h - 1, n); x = np.linspace(0, w - 1, n)
    y0 = np.floor(y).astype(int); x0 = np.floor(x).astype(int); y1 = np.minimum(y0 + 1, h - 1); x1 = np.minimum(x0 + 1, w - 1)
    ty = (y - y0)[:, None]; tx = (x - x0)[None, :]
    return (a[np.ix_(y0, x0)] * (1 - ty) * (1 - tx) + a[np.ix_(y0, x1)] * (1 - ty) * tx + a[np.ix_(y1, x0)] * ty * (1 - tx) + a[np.ix_(y1, x1)] * ty * tx)


def _blur(a, r):
    """Box blur of radius r cells, twice (close to a gaussian)."""
    if r < 1: return a
    for _ in range(2):
        for ax in (0, 1):
            c = np.cumsum(np.pad(a, [(r + 1, r) if k == ax else (0, 0) for k in (0, 1)], mode="edge"), axis=ax)
            a = (np.take(c, range(2 * r + 1, c.shape[ax]), axis=ax) - np.take(c, range(0, c.shape[ax] - 2 * r - 1), axis=ax)) / (2 * r + 1)
    return a


def continent_heights(image, high=23.0, low=-0.723, sea=0.3, smooth=2):
    """The map's height grid (4097 x 4097, row = y) from a grey image (PNG or .npy; row 0 = the far edge, the largest y):
    grey below `sea` (0..1) is sea floor from `low` up to 0.2, above it land from SHORE up to `high`, with a short ramp."""
    import pngio
    if str(image).lower().endswith(".npy"): v = np.load(image).astype(np.float64)
    else: im = pngio.read_png(image); v = im["rgba"][..., 0].astype(np.float64)
    v = (v - v.min()) / max(v.max() - v.min(), 1e-9)
    v = _blur(_resample(v[::-1], 4097), smooth)
    band = 0.015
    t = np.clip((v - (sea - band)) / (2 * band), 0, 1); t = t * t * (3 - 2 * t)
    seab = low + np.clip(v / max(sea, 1e-6), 0, 1) * (0.2 - low); land = SHORE + np.clip((v - sea) / max(1 - sea, 1e-6), 0, 1) * (high - SHORE)
    return seab * (1 - t) + land * t


def continent_types(z, cell, k, high, forest=0.35, seed=1):
    """Terrain type per navmesh cell (k x k height samples): 0 for sea (no navmesh there), else Plain, Forest (smooth noise,
    `forest` of the land), Mountain (steep or high) or Snow (highest)."""
    n = (z.shape[0] - 1) // k; zz = z[:n * k, :n * k].reshape(n, k, n, k)
    lo, hi, mean = zz.min(axis=(1, 3)), zz.max(axis=(1, 3)), zz.mean(axis=(1, 3))
    land = lo >= SHORE - 0.1
    slope = (hi - lo) / (k * cell); hn = (mean - SHORE) / max(high - SHORE, 1e-6)
    noise = _blur(np.random.default_rng(seed).random((n, n)), max(2, n // 60))
    cut = np.quantile(noise[land], 1 - forest) if land.any() and forest > 0 else np.inf
    t = np.where(land, LAND_TYPES["Plain"], 0)
    t = np.where(land & (noise >= cut), LAND_TYPES["Forest"], t)
    t = np.where(land & ((slope > 0.35) | (hn > 0.6)), LAND_TYPES["Mountain"], t)
    t = np.where(land & (hn > 0.85), LAND_TYPES["Snow"], t)
    return t.astype(np.int64), land


def continent(project, image, high=23.0, low=-0.723, sea=0.3, forest=0.35, seed=1, trees_per_cell=2.0, k=10):
    """Writes <project>/worldmap/continent: the map scene folder every later edit starts from (heights from the image, a
    party navmesh with terrain types, trees in the forests, the old map's rocks, cliffs, bridges and lakes taken out, every
    settlement model set on land). Returns a summary and the settlement data changes (posX, posY, gate) to patch."""
    import flora, navmesh, scene
    global _BASE
    _BASE = None
    src = source_dir(); dst = os.path.join(project, "worldmap", "continent"); os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        if f in ("TileSets",): continue
        a, b = os.path.join(src, f), os.path.join(dst, f)
        if os.path.isdir(a):
            if not os.path.isdir(b): shutil.copytree(a, b)
        else: shutil.copyfile(a, b)
    info, T, z0 = _read(src); cell = info["node_size"] / 256.0
    z = continent_heights(image, high, low, sea)
    set_heights(dst, z)
    import terrain                                   # the old map's texture layers would still draw where its rivers and rocks were
    Tc = terrain.Terrain(os.path.join(dst, "terrain.bin")); data = Tc.load()
    data["index_maps"] = [np.zeros_like(np.asarray(m)) for m in data["index_maps"]]
    blob = terrain.build(data)
    with open(os.path.join(dst, "terrain.bin"), "wb") as f: f.write(blob)
    types, land = continent_types(z, cell, k, high, forest, seed)
    flags = {int(t): 0xFFFFFF93 for t in LAND_TYPES.values()}
    nm = navmesh.NavMesh.from_height_field(z + 0.47, cell, land, k, regions=types, region_flags=flags)
    nm.save(os.path.join(dst, "navmesh.bin"))
    # the scene: the old geography's decoration out (at any depth, but not inside a settlement's model), settlement models onto land
    import re as re_
    tags = re_.findall(r'<Settlement\s[^>]*?>', open(os.path.join(config_modules(), "SandBox", "ModuleData", "settlements.xml"), encoding="utf-8").read(), re_.S)
    num = lambda t_, k_: (lambda r: float(r.group(1)) if r else None)(re_.search(r'\s%s="([-0-9.]+)"' % k_, t_))
    ids = [re_.search(r'\sid="([^"]+)"', t_).group(1) for t_ in tags]
    # posX/posY and gate_posX/gate_posY per settlement: the patch moves them by the same shift as the model
    xml_pts = {s: [(num(t_, "posX"), num(t_, "posY")), (num(t_, "gate_posX"), num(t_, "gate_posY"))] for s, t_ in zip(ids, tags)}
    doc = scene.load(os.path.join(dst, "scene.xscene")); idset = set(ids)
    def prune(el):
        n_ = 0
        for c in list(el.elements("game_entity")):
            nm_ = c.get("name") or ""; pf = c.get("old_prefab_name") or ""
            if nm_ in idset: continue
            comps = c.find("components")                # ground decals outside a settlement's model belong to the old ground
            has_decal = comps is not None and any(True for _ in comps.iter("decal_component"))
            if DECOR.match(nm_) or DECOR.match(pf) or has_decal: el.remove(c); n_ += 1; continue
            ch = c.find("children")
            if ch is not None: n_ += prune(ch)
        return n_
    removed = prune(doc.entities_element())
    n = types.shape[0]; step = k * cell; lj, li = np.nonzero(land & (types != LAND_TYPES["Snow"]))
    lx, ly = (li + 0.5) * step, (lj + 0.5) * step
    def on_land(x, y):
        j, i = int(y // step), int(x // step)
        return bool(0 <= j < n and 0 <= i < n and land[j, i] and types[j, i] != LAND_TYPES["Snow"])
    occupied = np.zeros(len(lx), bool); changes = {}; moved = 0; stuck = 0; spots = []
    for sid in ids:
        try: ent, pos = settlement_entity(doc, sid)
        except ValueError: continue
        # The model, posX/posY and the gate must all lie on the navmesh (the land cells). The campaign looks the gate's face up
        # (GetFaceIndex, -1 off the mesh) and the engine's path finding indexes its face table with it unchecked: a gate off the
        # mesh crashes the settlement distance cache (bl2_re JOURNAL 2026-10-14 "map navmesh crash").
        offs = [(0.0, 0.0)] + [(x - pos[0], y - pos[1]) for x, y in xml_pts.get(sid, []) if x is not None and y is not None]
        fits = lambda x, y: all(on_land(x + dx, y + dy) for dx, dy in offs)
        ok = fits(pos[0], pos[1])
        if ok: nx_, ny_ = pos[0], pos[1]
        else:
            d2 = (lx - pos[0]) ** 2 + (ly - pos[1]) ** 2 + occupied * 1e9; order = np.argsort(d2)
            q = next((int(c) for c in order if not occupied[c] and fits(lx[c], ly[c])), int(order[0])); occupied[q] = True
            nx_, ny_ = lx[q], ly[q]; moved += 1; stuck += not fits(nx_, ny_)
        lift = pos[2] - float(z0[int(round(pos[1] / cell)), int(round(pos[0] / cell))])
        nz = float(z[int(round(ny_ / cell)), int(round(nx_ / cell))]) + max(lift, 0.0)
        ent.position = tuple(np.asarray(ent.position, np.float64) + np.array([nx_, ny_, nz]) - pos)
        if not ok: changes[sid] = (nx_ - pos[0], ny_ - pos[1])
        spots.append((nx_, ny_))
    doc.save()
    # trees: scattered in the forest cells (and a few on the mountains), sized as the shipped map's of the same kind, none on a settlement
    shipped = flora.read(src); sm = np.linalg.norm(shipped["rec"]["m"][:, 2, :3], axis=1); sn = np.array(shipped["names"])
    size = {t: (np.percentile(sm[sn == t], 5), np.percentile(sm[sn == t], 95)) if (sn == t).any() else (0.6, 0.8) for t in TREES}
    rng = np.random.default_rng(seed + 1); names, recs = [], []; sp = np.array(spots) if spots else np.zeros((0, 2))
    for t, per, kinds in ((LAND_TYPES["Forest"], trees_per_cell, TREES), (LAND_TYPES["Mountain"], trees_per_cell * 0.15, TREES[3:])):
        jj, ii = np.nonzero(types == t); m = rng.poisson(per, len(jj))
        jj, ii = np.repeat(jj, m), np.repeat(ii, m)
        x = (ii + rng.random(len(ii))) * step; y = (jj + rng.random(len(jj))) * step
        if len(sp):
            near = np.zeros(len(x), bool)
            for a_, b_ in sp: near |= (x - a_) ** 2 + (y - b_) ** 2 < 3.0 ** 2
            x, y = x[~near], y[~near]
        gz = z[np.clip(np.rint(y / cell).astype(int), 0, z.shape[0] - 1), np.clip(np.rint(x / cell).astype(int), 0, z.shape[1] - 1)]
        for q in range(len(x)):
            kind = kinds[rng.integers(len(kinds))]
            nm_, r = flora.instance(kind, x[q], y[q], gz[q], rng.uniform(0, 360), rng.uniform(*size[kind]))
            names.append(nm_); recs.append(r)
    flora.write(dst, dict(names=names, rec=np.array(recs, flora.REC) if recs else np.zeros(0, flora.REC)))
    return {"continent": dst, "land_share": round(float(land.mean()), 3), "navmesh_faces": len(nm.faces), "trees": len(names),
            "decoration_removed": removed, "settlements_moved_onto_land": moved, "settlements_off_navmesh": stuck, "height_range": [round(float(z.min()), 3), round(float(z.max()), 3)]}, changes


def config_modules():
    import config
    return config.MODULES
