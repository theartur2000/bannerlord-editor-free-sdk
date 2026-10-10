"""What the installed game offers to build scenes from: its battle scenes by terrain type, and the prefabs they use.

The game lists its single-player battle scenes in Modules/SandBox/ModuleData/sp_battle_scenes.xml (id, terrain type, forest
density, the world map indices they stand for). This module reads that list, finds each scene's folder and terrain size,
and collects which prefabs (rocks, cliffs, trees, ruins) the scenes of a terrain type place and at what scale. The result
is cached as JSON under the SDK home; nothing of it is stored in this repository.

    cat = catalog.load()
    cat.scenes("Plain")              -> [{"id", "dir", "terrain", "forest", "nx", "ny", "node_size", "prefabs": {...}}, ...]
    cat.prefabs("Desert", "cliff")   -> [(prefab name, uses, median scale), ...]
"""
import collections, glob, json, os, re, sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, scene
else:
    from . import config, scene

CACHE = os.path.join(config.SDK_HOME, "cache", "battle_scenes.json")
# prefabs that are scene furniture for the game logic, not things to look at
LOGIC = re.compile(r"^(TacticalPosition|TacticalRegion|sp_|spawn|flee_line|border|barrier|.*ambient_sound|defender_|attacker_|camera|editor_|.*_set$)", re.I)


def _scan(log=print):
    path = os.path.join(config.MODULES, "SandBox", "ModuleData", "sp_battle_scenes.xml")
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f: text = f.read()
    out = []
    for m in re.finditer(r"<Scene\b([^>]*)>", text):
        a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        sid = a.get("id")
        dirs = glob.glob(os.path.join(config.MODULES, "*", "SceneObj", sid))
        if not sid or not dirs: continue
        d = dirs[0]; x = os.path.join(d, "scene.xscene")
        try:
            doc = scene.load(x)
        except Exception as e:
            log("skipped %s: %s" % (sid, e)); continue
        ter = doc.root.find("terrain")
        rec = {"id": sid, "dir": d, "terrain": a.get("terrain", ""), "forest": a.get("forest_density", ""),
               "nx": int(ter.get("node_dimension_x")) if ter is not None else 0, "ny": int(ter.get("node_dimension_y")) if ter is not None else 0,
               "node_size": float(ter.get("node_size")) if ter is not None else 0.0,
               "terrain_magic_ok": False, "prefabs": {}}
        tb = os.path.join(d, "terrain.bin")
        if os.path.isfile(tb):
            with open(tb, "rb") as f: rec["terrain_magic_ok"] = f.read(8) == b"ZGR6RTRN"
        uses = collections.defaultdict(list)
        for e in doc.entities(True):
            if e.prefab and not LOGIC.match(e.prefab):
                s = e.scale
                uses[e.prefab].append(round(float(sum(s) / 3.0), 2) if s else 1.0)
        rec["prefabs"] = {k: [len(v), sorted(v)[len(v) // 2]] for k, v in uses.items()}
        if ter is not None:
            layers = ter.find("layers").elements("layer")
            rec["layers"] = [l.get("name") for l in layers]
            rec["flora_layers"] = [l.get("name") for l in layers if l.find("summer") is not None and l.find("summer").get("is_flora_layer") == "true"]
        out.append(rec)
    return out


class Catalog:
    def __init__(self, records): self.records = records

    def terrains(self):
        return collections.Counter(r["terrain"] for r in self.records)

    def scenes(self, terrain=None, nx=None, node_size=None, current_format=True):
        return [r for r in self.records if (terrain is None or r["terrain"] == terrain) and (nx is None or (r["nx"] == nx and r["ny"] == nx))
                and (node_size is None or abs(r["node_size"] - node_size) < 1e-6) and (not current_format or r["terrain_magic_ok"])]

    def prefabs(self, terrain=None, contains=None):
        """[(prefab, uses in all scenes of that terrain type, median scale)], most used first."""
        tot = collections.defaultdict(lambda: [0, []])
        for r in self.scenes(terrain, current_format=False):
            for k, (n, s) in r["prefabs"].items():
                if contains and not re.search(contains, k, re.I): continue
                tot[k][0] += n; tot[k][1].append(s)
        return sorted(((k, v[0], sorted(v[1])[len(v[1]) // 2]) for k, v in tot.items()), key=lambda t: -t[1])


def load(refresh=False, log=print):
    if not refresh and os.path.isfile(CACHE):
        with open(CACHE, "r", encoding="utf-8") as f: return Catalog(json.load(f))
    config.require_game()
    recs = _scan(log)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f: json.dump(recs, f)
    return Catalog(recs)


ROCK = re.compile(r"rock|cliff|stone|boulder", re.I)
ROCK_CACHE = os.path.join(config.SDK_HOME, "cache", "rock_usage.json")


def rock_usage(refresh=False, log=print):
    """{terrain type: {prefab: {n, slope, steep_share, dz, dz_lo, dz_hi, scale, scale_hi, tilt}}}: for every rock-like prefab,
    how the shipped battlefields of a terrain type use it. slope: the ground slope it stands on (median); dz: its origin's
    height above the ground (median and the 20th / 80th percentile; negative = sunk in); scale; tilt (radians, both axes)."""
    if not refresh and os.path.isfile(ROCK_CACHE):
        with open(ROCK_CACHE, "r", encoding="utf-8") as f: return json.load(f)
    import numpy as np
    if __package__ in (None, ""): import terrain, terraform
    else: from . import terrain, terraform
    stats = collections.defaultdict(lambda: collections.defaultdict(list))
    for rec in load().records:
        if not rec["terrain_magic_ok"] or rec["nx"] == 0: continue
        try:
            info = terrain.scene_terrain_info(os.path.join(rec["dir"], "scene.xscene"))
            z = terrain.Terrain(os.path.join(rec["dir"], "terrain.bin")).heightmap(info)
        except Exception as e:
            log("skipped %s: %s" % (rec["id"], e)); continue
        cell = info["node_size"] * info["nx"] / (z.shape[1] - 1)
        sl = terraform.slope(terraform.blur_fast(z, 3.0 / cell), cell)
        for e in scene.load(os.path.join(rec["dir"], "scene.xscene")).entities():
            if not e.prefab or not ROCK.search(e.prefab) or not e.position: continue
            pos = e.position; i = int(round(pos[0] / cell)); j = int(round(pos[1] / cell))
            if not (0 <= i < z.shape[1] and 0 <= j < z.shape[0]): continue
            st = stats[(rec["terrain"], e.prefab)]
            st["slope"].append(float(sl[j, i])); st["dz"].append(float(pos[2] - z[j, i]))
            sc = e.scale; st["scale"].append(float(sum(sc) / 3) if sc else 1.0)
            r = e.rotation or (0, 0, 0); st["tilt"].append(abs(r[0]) + abs(r[1]))
    out = {}
    for (biome, name), v in stats.items():
        if len(v["slope"]) < 6: continue
        med = lambda x: float(np.median(x))
        out.setdefault(biome, {})[name] = {"n": len(v["slope"]), "slope": round(med(v["slope"]), 2), "steep_share": round(float((np.array(v["slope"]) > 0.45).mean()), 2),
            "dz": round(med(v["dz"]), 1), "dz_lo": round(float(np.percentile(v["dz"], 20)), 1), "dz_hi": round(float(np.percentile(v["dz"], 80)), 1),
            "scale": round(med(v["scale"]), 2), "scale_hi": round(float(np.percentile(v["scale"], 85)), 2), "tilt": round(med(v["tilt"]), 2)}
    os.makedirs(os.path.dirname(ROCK_CACHE), exist_ok=True)
    with open(ROCK_CACHE, "w", encoding="utf-8") as f: json.dump(out, f)
    return out


if __name__ == "__main__":
    cat = load(refresh="--refresh" in sys.argv)
    print(len(cat.records), "battle scenes;", dict(cat.terrains()))
    for t in sorted(cat.terrains()):
        usable = cat.scenes(t, 6, 200.0)
        print("%-10s %3d scenes, %2d with 6 x 6 nodes of 200 m; rocks and cliffs: %s" % (t, len(cat.scenes(t, current_format=False)), len(usable),
              ", ".join("%s x%d" % (k, n) for k, n, s in cat.prefabs(t, "rock|cliff|stone|boulder")[:6])))
