"""A line-up scene: every rock prefab the shipped battlefields of a biome use, alone on flat ground, with a camera
viewpoint on each. Run it in the game (GenFields autotest word "views") to see what each prefab looks like and how big it
is before letting a generator place it.

    python demos/battlefields/lineup.py --biome Plain --name gen_lineup_plain

The poles: each rock stands at scale 1, origin on the ground, no tilt. A row of them 60 m apart; view k looks at rock k
from 38 m away and 7 m up. views.txt in the scene folder lists them by prefab name.
"""
import argparse, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk"))
import catalog, config, scenebuild   # noqa: E402

TEMPLATES = {"Plain": "battle_terrain_a", "Desert": "battle_terrain_b", "Steppe": "battle_terrain_c"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--biome", default="Plain"); ap.add_argument("--name", default="gen_lineup"); ap.add_argument("--count", type=int, default=16); ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    cat = catalog.load(); usage = catalog.rock_usage().get(a.biome, {})
    skip = () if a.all else ("far", "bridge", "heap", "top", "ambush", "wall")     # --all: also the pieces the generator leaves out
    names = [k for k, v in sorted(usage.items(), key=lambda kv: -kv[1]["n"]) if v["n"] >= 10 and not any(w in k for w in skip)][:a.count]
    tpl = scenebuild.Template([s for s in cat.records if s["id"] == TEMPLATES.get(a.biome, "battle_terrain_a")][0]["dir"])
    import terraform
    rng = np.random.default_rng(1)
    z = 50.0 + 0.5 * terraform.noise(tpl.samples, tpl.cell, 120.0, rng)   # nearly flat; a perfectly flat scene with no painted layer crashed the game on load
    masks = {i: np.clip(128 + 200 * terraform.noise(tpl.samples, tpl.cell, 80.0, rng), 0, 255).astype(np.uint8) for i in (1, 2) if i < len(tpl.layers)}
    ents = []; views = []; cols = 8
    for k, nm in enumerate(names):
        x = 180.0 + 120.0 * (k % cols); y = 420.0 + 240.0 * (k // cols)
        g = float(z[int(round(y / tpl.cell)), int(round(x / tpl.cell))])
        ents.append({"prefab": nm, "position": (x, y, g), "rotation": (0.0, 0.0, 0.0)})
        views.append((nm, (x + 4.0, y - 38.0, g + 7.0), (x, y, g + 3.0)))
    out = os.path.join(config.SDK_STAGE_DIR, "scenes", a.name)
    border = [(600 + 330 * np.cos(t), 600 + 330 * np.sin(t)) for t in np.linspace(0, 2 * np.pi, 32, endpoint=False)]
    paths = [[(380.0 + 73 * i, 600.0, 50.0) for i in range(7)]]
    scenebuild.build(tpl, out, a.name, z, masks, entities=ents, spawn_paths=paths, border=border)
    with open(os.path.join(out, "views.txt"), "w", encoding="utf-8") as f:
        for nm, eye, tgt in views: f.write("%s %.2f %.2f %.2f %.2f %.2f %.2f\n" % ((nm,) + eye + tgt))
    print(len(names), "rocks in", out); print(", ".join(names))


if __name__ == "__main__":
    main()
