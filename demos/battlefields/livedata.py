"""The data the in-game generator (mod/GenFields/src/gen) needs, written once from the user's own install.

    python demos/battlefields/livedata.py [--biome Plain] [--out <folder>]     default: <SDK stage>/livedata

The C# generator does the generating; it does not read the game's packages, scenes or meshes. Everything it needs from
them is put into one folder per biome here:
  slot/            a complete scene folder (generated the usual way): the content the slot holds when the game starts.
                   The game must know a scene folder at startup; the generator then rewrites three files in it.
  skeleton.xscene  that scene's scene.xscene with @TOKENS@ where the generator writes: name, heights, the per-node
                   attributes, water, entities, paths. Texture layers are already edited (six-side rock, bush density).
  template.txt     the texture layers: role, seasons, whether a layer carries detail meshes; water defaults
  paint.bin        per layer the weight at 1025 quantiles, inside and outside the fighting area (Template.paint_stats)
  rocks.txt/.bin   the rocks of the biome: how the shipped scenes use each (catalog.rock_usage) and its shape
                   (rim points, vertices, triangles: rockfit.rock_shape)
  props.txt        fallen trees, logs, bones: names and weights
  detail.bin       small relief of five shipped battlefields (two bands), the stock the generator quilts from
install.py --live copies the folders into Modules/GenFields/GenData and the slots into SceneObj.
None of this is in the repository: it is derived from the game on the user's disk.
"""
import argparse, os, shutil, struct, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk")); sys.path.insert(0, HERE)
import catalog, config, rockfit, scene, scenebuild, terraform, terrain   # noqa: E402
import procgen                                                         # noqa: E402

SEASONS = ("summer", "fall", "winter", "spring")
START = {"Plain": ("hill", 0.5, 0.2), "Steppe": ("hill", 0.5, 0.06), "Desert": ("hill", 0.5, 0.03)}


PLAYABLE = 4    # slots that hold a real field and are listed in Custom Battle (Live.cs PlayableSlots)
SLOTS = 64      # scene folders a biome gets in the module: the rest are placeholders, one a battle a session


def slot_name(biome, n=0): return "gf_live_" + biome.lower() + ("" if n == 0 else "_%d" % (n + 1))


def export(biome, out, log=print):
    os.makedirs(out, exist_ok=True)
    name = slot_name(biome); slot = os.path.join(out, "slot")
    kind, rugged, forest = START[biome]
    d = procgen.design(procgen.Rules(biome, rugged, forest, None), kind, 1000 + len(biome), log=lambda *a: None)
    if os.path.isdir(slot): shutil.rmtree(slot)
    procgen.write(d, name, slot, log=lambda *a: None)
    tpl = d["template"]; roles = d["roles"]; L = len(tpl.layers)

    # ---- skeleton
    doc = scene.load(os.path.join(slot, "scene.xscene"))
    doc.root.set("name", "@NAME@")
    ter = doc.root.find("terrain"); ter.set("min_height", "@TMIN@"); ter.set("max_height", "@TMAX@")
    for node in ter.find("nodes").elements("node"):
        k = int(node.get("idy")) * tpl.nx + int(node.get("idx"))
        for s in SEASONS: node.set("layer_is_used_mask_" + s, "@N%d_M%s@" % (k, s[0].upper() + s[1]))
        node.set("height_complexity", "@N%d_HC@" % k); node.set("min_height", "@N%d_MIN@" % k); node.set("max_height", "@N%d_MAX@" % k)
    wl = we = None
    for pr in doc.root.find("environment_properties").find("water_properties").elements("property"):
        if pr.get("name") == "water_level": wl = pr.get("value"); pr.set("value", "@WATER_LEVEL@")
        elif pr.get("name") == "water_exists": we = pr.get("value"); pr.set("value", "@WATER_EXISTS@")
    ents = doc.entities_element()
    for e in list(ents.elements()): ents.remove(e)
    paths = doc.root.find("Paths")
    for p in list(paths.elements()): paths.remove(p)
    text = doc.tobytes().decode("utf-8")
    import re
    text, n1 = re.subn(r"<entities>.*?</entities>|<entities\s*/>", "<entities>@ENTITIES@</entities>", text, count=1, flags=re.S)
    text, n2 = re.subn(r"<Paths>.*?</Paths>|<Paths\s*/>", "<Paths>@PATHS@</Paths>", text, count=1, flags=re.S)
    assert n1 == 1 and n2 == 1, (n1, n2)
    for tok in ("@NAME@", "@TMIN@", "@TMAX@", "@WATER_LEVEL@", "@WATER_EXISTS@", "@ENTITIES@", "@PATHS@", "@N0_MIN@", "@N%d_MAX@" % (tpl.nx * tpl.ny - 1)): assert text.count(tok) == 1, tok
    with open(os.path.join(out, "skeleton.xscene"), "w", encoding="utf-8", newline="") as f: f.write(text)

    # ---- template
    role_of = {i: r for r, ids in roles.items() for i in ids}; role_of[0] = "base"
    stats = tpl.paint_stats()
    with open(os.path.join(out, "template.txt"), "w", encoding="utf-8") as f:
        f.write("biome %s\nnodes %d %d\nnode_size %g\nlayers %d\nwater_level %s\nwater_exists %s\n" % (biome, tpl.nx, tpl.ny, tpl.node_size, L, wl, we))
        for i in range(L):
            f.write("layer %d %s %s %d %d %d %d %d %d\n" % (i, role_of.get(i, "grass"), tpl.layer_names[i].replace(" ", "_"), *[int(tpl.enabled[s][i]) for s in SEASONS],
                                                           int(bool(tpl.meshes[i])), int(i in tpl.flora_layers)))
    q = np.zeros((L, 2, 1025), "<f4")
    for i in range(L): q[i, 0] = stats[i]["q_in"]; q[i, 1] = stats[i]["q_out"]
    q.tofile(os.path.join(out, "paint.bin"))

    # ---- rocks
    usage = catalog.rock_usage().get(biome) or catalog.rock_usage().get("Plain", {})
    usage = {k: v for k, v in usage.items() if v["n"] >= 10 and not any(w in k for w in procgen.SKIP_ROCKS)}
    if biome == "Plain": usage = {k: v for k, v in usage.items() if "desert" not in k}
    rows = []; blob = bytearray()
    for name_, u in sorted(usage.items()):
        try: s = rockfit.rock_shape(name_)
        except Exception as e: log("no shape for %s: %s" % (name_, e)); continue
        rim = np.asarray(s["rim"], "<f4").reshape(-1, 3); body = np.asarray(s["body"], "<f4"); tris = np.asarray(s["tris"], "<i4")
        rows.append("%s %s %d %g %g %g %g %g %g %g %g %g %d %d %d" % (name_, procgen.rock_kind(name_), u["n"], u["scale"], u["scale_hi"], u["dz"], u["dz_lo"], u["dz_hi"], u["tilt"],
                                                                 u.get("facing", 0.0), u.get("facing_r", 0.0), u.get("lean", 0.0), len(rim), len(body), len(tris)))
        blob += rim.tobytes() + body.tobytes() + tris.tobytes()
    with open(os.path.join(out, "rocks.txt"), "w", encoding="utf-8") as f: f.write("\n".join(rows) + "\n")
    with open(os.path.join(out, "rocks.bin"), "wb") as f: f.write(blob)

    # ---- props
    cat = catalog.load()
    props = [(k_, n_) for k_, n_, _ in cat.prefabs(biome, procgen.PROPS)][:14]
    with open(os.path.join(out, "props.txt"), "w", encoding="utf-8") as f: f.write("".join("%s %d\n" % p for p in props))

    # ---- small relief of shipped battlefields, two bands, as 16-bit millimetres
    tscene = [s for s in cat.records if s["id"] == procgen.TEMPLATES[biome]][0]
    donors = [s for s in cat.scenes(biome, 6, 200.0)][:5]
    if tscene not in donors: donors = [tscene] + donors[:4]
    cell = tpl.cell
    mid = terraform.DetailBank(cell, wavelength=20.0, clip=3.5, longest=240.0); fine = terraform.DetailBank(cell, wavelength=24.0)
    for s in donors:
        zd = terrain.Terrain(os.path.join(s["dir"], "terrain.bin")).heightmap(terrain.scene_terrain_info(os.path.join(s["dir"], "scene.xscene")))
        mid.add(zd); fine.add(zd)
    def mm(a): return np.clip(np.rint(a * 1000.0), -32767, 32767).astype("<i2")
    with open(os.path.join(out, "detail.bin"), "wb") as f:
        f.write(struct.pack("<4sIf", b"GFD1", len(donors), cell))
        for dm, df in zip(mid.donors, fine.donors):
            m4 = dm[::4, ::4]                              # the mid band has nothing finer than 20 m: a quarter of the samples keeps it
            f.write(struct.pack("<4I", df.shape[0], df.shape[1], m4.shape[0], m4.shape[1])); f.write(mm(df).tobytes()); f.write(mm(m4).tobytes())
    size = sum(os.path.getsize(os.path.join(out, x)) for x in os.listdir(out) if os.path.isfile(os.path.join(out, x)))
    log("%s: %d layers, %d rocks, %d props, %d donors, %.1f MB in %s" % (biome, L, len(rows), len(props), len(donors), size / 1e6, out))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--biome", choices=sorted(START)); ap.add_argument("--out", default=os.path.join(config.SDK_STAGE_DIR, "livedata"))
    a = ap.parse_args()
    for b in ([a.biome] if a.biome else sorted(START)): export(b, os.path.join(a.out, b))


if __name__ == "__main__":
    main()
