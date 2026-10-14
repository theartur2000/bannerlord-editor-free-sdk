"""Editing the campaign world map's heights: research, not a working feature yet.

Measured in game (2026-10-14): a module's SceneObj/Main_map/scene.xscene is the one the campaign reads (the log names it,
and MapScene.GetMainMapModule picks the last active module that has one), and the copy's terrain.bin written here holds the
edit, but the map is drawn and queried (MapSceneWrapper.GetHeightAtPoint) exactly as shipped: a 6-unit rise was neither
visible nor measurable. The map's heights evidently also live in its tile set (TileSets/WorldMap.gts and pages, 2.1 GB),
whose format is not decoded. So `bl` offers no map height command; what follows writes valid files for that next step.


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
    return {"min": round(lo, 3), "max": round(hi, 3), "terrain_bytes": len(blob), "nodes_changed": len(changed)}


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
