"""A battlefield made only of custom assets: ground textures, rocks and plants that are not the game's.

The worked example uses Borderlands 2 models and textures. They are NOT part of this repository: the script reads them
from umodel exports of your own copy of that game and writes the converted packages to the SDK stage folder on your disk.

    python demos/bl2field/build.py --export <umodel export folder> --sets <folder of ground texture sets>
                                   [--kind valley] [--seed 7] [--rugged 0.7] [--name bl2_field] [--preview out.png]
    python demos/battlefields/install.py --scene bl2_field --assets <SDK stage>/bl2field_assets     (game closed)

--export  holds <package>/StaticMesh3/<mesh>.pskx, <package>/Material*/<material>.mat and <package>/Texture2D/<texture>.png,
          as "umodel -export -png" writes them
--sets    holds <set>_d.png, _n.png, _s.png, _h.png for each ground set named in GROUND

What it shows of the SDK:
  sdk/psk.py         reads the PSK models
  sdk/assetpack.py   writes texture, material and model packages into a stage and installs them into a module
  sdk/rockfit.py     fits each custom rock to the ground and raises the ground under it, so the terrain is its collision
  scenebuild.edit_layer   paints the terrain layers with the custom textures and drops the game's grass models
  procgen.design(palette=...)   the same generator as the other battlefields, fed a palette of custom assets
"""
import argparse, json, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk")); sys.path.insert(0, os.path.join(HERE, "..", "battlefields"))
import assetpack, config, psk, rockfit   # noqa: E402
import procgen                           # noqa: E402

PREFIX = "bl2f_"
GROUND = {"base": "bl2g_scrub", "grass": "bl2g_scrub2", "soil": "bl2g_soil", "rock": "bl2g_rockA", "snow": "bl2g_snow"}
# model: (package, mesh, kind, weight). kind is what the generator uses a rock for (procgen.ROCK_KIND).
ROCKS = {
    "cliff_a": ("Frost_P", "AridRockCliff_05", "wall", 10), "cliff_b": ("Fyrestone_P", "AridRockCliff_07", "wall", 8),
    "cliff_c": ("Fyrestone_P", "AridRockCliff_05a", "wall", 6), "bluff": ("CreatureSlaughter_P", "RockBluff", "wall", 6),
    "spire": ("Cove_P", "SlateTree", "tower", 3), "arch": ("Ash_P", "SlateArch", "tower", 2),
    "boulder_a": ("Boss_Cliffs_P", "SlateBoulder", "boulder", 8), "boulder_b": ("Boss_Cliffs_P", "SlateBoulder_02", "boulder", 8),
    "round": ("Ash_P", "BarrensRoundRock", "boulder", 6), "shale": ("ThresherRaid_P", "ShaleRock_3", "boulder", 6),
    "formation": ("DamTop_P", "BarrensRockFormation", "boulder", 5),
    "pile_a": ("Ash_P", "RockPile_01", "slab", 6), "pile_b": ("Fyrestone_P", "RockPile_02", "slab", 5), "pile_c": ("Boss_Cliffs_P", "SlateRockPile", "slab", 5),
    "loose": ("Ash_P", "LooseRocks_02", "slab", 5), "fan_a": ("HypInterlude_P", "BarrensSmallFanRock", "tower", 3), "fan_b": ("Caverns_P", "ShaleFanRock", "tower", 3),
}
NO_SUPPORT = ("arch",)       # walked under: the ground is not raised beneath it
# model: (package, mesh, spacing in metres, scale range, where, limit)
PLANTS = {
    "deadgrass": ("Caverns_P", "InterludeDeadGrass", 7.0, (0.8, 1.6), "any", 2200), "grass_thick": ("Boss_Cliffs_P", "GrassThick2", 15.0, (0.5, 1.0), "any", 700),
    "grass_tall": ("Caverns_P", "GrassTall", 13.0, (0.7, 1.2), "any", 800), "dry_rocks": ("SanctuaryAir_FX", "DryGrassRocks", 30.0, (0.7, 1.2), "any", 200),
    "shrub": ("Fyrestone_P", "Shrub_01", 30.0, (0.4, 0.8), "open", 200), "skagbush": ("CreatureSlaughter_P", "SkagBush", 36.0, (0.8, 1.3), "open", 150),
    "cactus": ("CraterLake_P", "DecoSingleCactus", 42.0, (0.8, 1.5), "open", 120), "cactus_clump": ("CraterLake_P", "DecoCactusClump", 60.0, (0.8, 1.3), "open", 70),
    "spikey": ("Caverns_P", "SpikeyPlant_01", 48.0, (0.8, 1.4), "open", 90), "pandora_plant": ("CreatureSlaughter_P", "PandoraPlant_04", 55.0, (0.8, 1.3), "open", 80),
    "deadtree_a": ("Fyrestone_P", "DeadTree01", 15.0, (0.9, 1.7), "wood", 500), "deadtree_b": ("Frost_P", "DeadTree02", 17.0, (0.9, 1.7), "wood", 400),
}


def _mat_file(export, pkg, material):
    for sub in ("MaterialInstanceConstant", "Material3"):
        f = os.path.join(export, pkg, sub, material + ".mat")
        if os.path.isfile(f):
            return dict(l.strip().split("=", 1) for l in open(f, encoding="utf-8", errors="replace") if "=" in l)
    return {}


def _texture(export, pkg, name):
    for ext in (".png", ".tga"):
        f = os.path.join(export, pkg, "Texture2D", name + ext)
        if os.path.isfile(f): return f
    return None


def build_assets(export, sets, stage_dir, log=print):
    """Converts everything into the stage and returns the palette for procgen.design."""
    from PIL import Image
    st = assetpack.Stage(stage_dir); shapes = {}; materials = {}
    layers = {}
    for role, s in GROUND.items():
        f = lambda suf: os.path.join(sets, s + suf + ".png")
        layers[role] = st.terrain_set(PREFIX + "ground_" + role, f("_d"), f("_n") if os.path.isfile(f("_n")) else None,
                                      f("_s") if os.path.isfile(f("_s")) else None, f("_h") if os.path.isfile(f("_h")) else None)
        layers[role]["areamap"] = "none"         # the template's large-scale tint map is one of the game's textures ("none", not an empty name: that paints the whole terrain black)
    log("%d ground sets" % len(layers))

    def model(key, pkg, mesh):
        path = os.path.join(export, pkg, "StaticMesh3", mesh + ".pskx")
        mats = []
        for mn in psk.read(path)["materials"]:
            mk = (pkg, mn)
            if mk not in materials:
                info = _mat_file(export, pkg, mn); dif = _texture(export, pkg, info.get("Diffuse", "")); nrm = _texture(export, pkg, info.get("Normal", ""))
                name = PREFIX + "m_" + mn.lower()
                if dif is None: dif = Image.new("RGB", (64, 64), (118, 104, 86))
                im = Image.open(dif) if isinstance(dif, str) else dif
                # some materials keep their transparency in a texture of its own (Opacity, or one named ..._Alpha)
                mask = next((v for k, v in info.items() if k == "Opacity" or (k.startswith("Other") and v.lower().endswith("_alpha"))), None)
                mask = _texture(export, pkg, mask) if mask else None
                if mask and im.mode != "RGBA":
                    im = im.convert("RGB"); im.putalpha(Image.open(mask).convert("L").resize(im.size, Image.LANCZOS))
                cut = im.mode == "RGBA" and 0.03 < float((np.asarray(im)[..., 3] < 128).mean()) < 0.97
                materials[mk] = st.material(name, im, normal=nrm, cutout=cut)
            mats.append(materials[mk])
        P, F = st.psk_mesh(PREFIX + key, path, mats)
        return P, F

    rocks = {}
    for key, (pkg, mesh, kind, weight) in ROCKS.items():
        try: P, F = model(key, pkg, mesh)
        except FileNotFoundError as e: log("skipped %s: %s" % (key, e)); continue
        name = PREFIX + key; shapes[name] = (P, F); H = float(np.ptp(P[:, 2]))
        rocks[name] = {"kind": kind, "n": weight, "slope": 0.6 if kind in ("wall", "tower") else 0.2, "steep_share": 0.5, "dz": -0.12 * H,
                       "dz_lo": -0.28 * H, "dz_hi": -0.04 * H, "scale": 1.0, "scale_hi": 1.25, "tilt": 0.12, "support": key not in NO_SUPPORT}
    plants = []
    for key, (pkg, mesh, spacing, scale, where, limit) in PLANTS.items():
        try: model(key, pkg, mesh)
        except FileNotFoundError as e: log("skipped %s: %s" % (key, e)); continue
        plants.append({"mesh": PREFIX + key, "spacing": spacing, "scale": list(scale), "where": where, "limit": limit, "clear": 0.0, "sink": 0.04})
    np.savez_compressed(os.path.join(stage_dir, "shapes.npz"), **{k + "|P": v[0] for k, v in shapes.items()}, **{k + "|F": v[1] for k, v in shapes.items()})
    palette = {"layers": layers, "rocks": rocks, "plants": plants, "props": [], "support": True}
    with open(os.path.join(stage_dir, "palette.json"), "w", encoding="utf-8") as f: json.dump(palette, f, indent=1)
    log("%d rocks, %d plants, %d materials in %s" % (len(rocks), len(plants), len(materials), stage_dir))
    return palette


def load_palette(stage_dir):
    """The palette of a stage built earlier, with its rock shapes made known to rockfit."""
    with open(os.path.join(stage_dir, "palette.json"), "r", encoding="utf-8") as f: palette = json.load(f)
    with np.load(os.path.join(stage_dir, "shapes.npz")) as d:
        for name in palette["rocks"]: rockfit.register(name, d[name + "|P"], d[name + "|F"])
    return palette


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--export"); ap.add_argument("--sets"); ap.add_argument("--stage", default=os.path.join(config.SDK_STAGE_DIR, "bl2field_assets"))
    ap.add_argument("--kind", default="valley", choices=procgen.KINDS); ap.add_argument("--seed", type=int, default=7); ap.add_argument("--rugged", type=float, default=0.7)
    ap.add_argument("--forest", type=float, default=0.12); ap.add_argument("--name", default="bl2_field"); ap.add_argument("--preview")
    ap.add_argument("--reuse", action="store_true", help="use the assets already in the stage")
    a = ap.parse_args()
    if not a.reuse:
        if not a.export or not a.sets: sys.exit("--export and --sets are needed (or --reuse)")
        build_assets(a.export, a.sets, a.stage)
    palette = load_palette(a.stage)
    rules = procgen.Rules("Desert", a.rugged, a.forest, None)
    d = procgen.design(rules, a.kind, a.seed, palette=palette)
    out = os.path.join(config.SDK_STAGE_DIR, "scenes", a.name)
    procgen.write(d, a.name, out); print("scene", a.name, "written to", out, "with", d["planted"], "plants")
    if a.preview: procgen.preview(d, a.preview); print("preview", a.preview)


if __name__ == "__main__":
    main()
