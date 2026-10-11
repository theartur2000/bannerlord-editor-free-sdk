"""A pool of generated battlefields for campaign play: every field battle is sent to one of them (GenFields picks by the
terrain of the map spot, see mod/GenFields/src/SubModule.cs).

    python demos/battlefields/pool.py [--jobs 3] [--only plain_valley] [--list]     generate the pool into the stage folder
    python demos/battlefields/install.py --dll <folder> --pool                      game closed: install it into the module

Until the generator runs inside the game, this is how a campaign gets generated fields: the scene folders have to exist
when the game starts. POOL lists them: name, biome, layout, ruggedness, forest, water, seed.
"""
import argparse, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
POOL = [
    ("gf_plain_valley_1", "Plain", "valley", 0.80, 0.25, None, 311), ("gf_plain_valley_2", "Plain", "valley", 0.65, 0.35, None, 402),
    ("gf_plain_ridge_1", "Plain", "ridge", 0.60, 0.25, None, 403), ("gf_plain_hill_1", "Plain", "hill", 0.50, 0.20, None, 404),
    ("gf_plain_hill_2", "Plain", "hill", 0.70, 0.45, None, 405), ("gf_plain_pass_1", "Plain", "pass", 0.90, 0.20, None, 320),
    ("gf_plain_ledge_1", "Plain", "ledge", 0.85, 0.15, None, 319), ("gf_plain_crossing_1", "Plain", "crossing", 0.40, 0.30, "river", 313),
    ("gf_plain_crossing_2", "Plain", "crossing", 0.60, 0.15, "river", 408), ("gf_plain_clearing_1", "Plain", "clearing", 0.30, 0.75, None, 314),
    ("gf_plain_bluff_1", "Plain", "bluff", 0.50, 0.20, "sea", 317), ("gf_plain_broken_1", "Plain", "broken", 0.75, 0.30, None, 411),
    ("gf_steppe_valley_1", "Steppe", "valley", 0.70, 0.05, None, 312), ("gf_steppe_ridge_1", "Steppe", "ridge", 0.65, 0.05, None, 413),
    ("gf_steppe_hill_1", "Steppe", "hill", 0.60, 0.08, None, 414), ("gf_steppe_broken_1", "Steppe", "broken", 0.85, 0.10, None, 415),
    ("gf_steppe_crossing_1", "Steppe", "crossing", 0.45, 0.08, "river", 416), ("gf_steppe_pass_1", "Steppe", "pass", 0.85, 0.05, None, 417),
    ("gf_desert_mesa_1", "Desert", "mesa", 0.60, 0.02, None, 315), ("gf_desert_mesa_2", "Desert", "mesa", 0.80, 0.02, None, 419),
    ("gf_desert_hill_1", "Desert", "hill", 0.55, 0.03, None, 318), ("gf_desert_valley_1", "Desert", "valley", 0.75, 0.03, None, 421),
    ("gf_desert_ridge_1", "Desert", "ridge", 0.65, 0.03, None, 422), ("gf_desert_broken_1", "Desert", "broken", 0.85, 0.03, None, 423),
]


def command(row, preview_dir=None):
    name, biome, kind, rugged, forest, water, seed = row
    c = [sys.executable, "-u", os.path.join(HERE, "procgen.py"), "--biome", biome, "--kind", kind, "--rugged", str(rugged), "--forest", str(forest),
         "--seed", str(seed), "--name", name]
    if water: c += ["--water", water]
    if preview_dir: c += ["--preview", os.path.join(preview_dir, name + ".png")]
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jobs", type=int, default=3); ap.add_argument("--only"); ap.add_argument("--list", action="store_true"); ap.add_argument("--previews")
    a = ap.parse_args()
    rows = [r for r in POOL if not a.only or a.only in r[0]]
    if a.list:
        for r in rows: print("%-22s %-7s %-9s rugged %.2f forest %.2f %s" % (r[0], r[1], r[2], r[3], r[4], r[5] or ""))
        return
    if a.previews: os.makedirs(a.previews, exist_ok=True)
    running = []; todo = list(rows); bad = 0
    while todo or running:
        while todo and len(running) < a.jobs:
            r = todo.pop(0); running.append((r, subprocess.Popen(command(r, a.previews), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)))
        r, p = running.pop(0); out = p.communicate()[0]
        line = next((l for l in out.splitlines() if " seed " in l), out.strip().splitlines()[-1] if out.strip() else "no output")
        print(("ok   " if p.returncode == 0 else "FAIL ") + r[0] + ": " + line, flush=True); bad += p.returncode != 0
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
