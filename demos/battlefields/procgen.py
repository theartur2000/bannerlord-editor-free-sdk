"""Tech demo: a battlefield invented fresh for every battle, with the world map as the rule book.

The map does not supply the shape of the ground. It supplies the rules: which biome, how rugged, how much forest, whether
there is a river, a lake or the sea and on which side. The generator then composes a field the way a level designer
would: it picks a layout (ridge, valley, hill, river crossing, forest clearing, pass, coastal bluff, broken ground, mesas),
draws its landforms, frames the play area with scenery, lays real small relief from shipped battlefields over it, cuts
the water, grows the forest, sets rocks on the steep ground, and marks high ground and chokepoints for the battle AI.

    python demos/battlefields/procgen.py --map 493.6 403.2 --name gen_proc_1 --preview out.png      rules from a map position
    python demos/battlefields/procgen.py --biome Plain --kind ridge --rugged 0.5 --seed 7 --preview out.png

Same seed, same field. Without --seed every run is a new field. This is a demo built with the SDK, not part of it.
"""
import argparse, math, os, sys, time, zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk"))
import catalog, config, rockfit, scenebuild, terraform, terrain, worldmap   # noqa: E402
from terraform import smoothstep                                    # noqa: E402

FIELD = 1200.0
N = 1537
C = FIELD / 2
PLAY = 330.0                  # radius of the walkable area
SPAWN = 235.0                 # the armies start this far from the middle
KINDS = ("ridge", "valley", "hill", "crossing", "clearing", "pass", "bluff", "broken", "mesa", "ledge")
TEMPLATES = {"Plain": "battle_terrain_a", "Desert": "battle_terrain_b", "Steppe": "battle_terrain_c"}
SKIP_ROCKS = ("far", "bridge", "heap", "top", "ambush", "wall")   # scenery for the distance, buildings, and pieces with special uses
PROPS = r"^(forest_ruinous_|dry_tree_|tree_root_|log_pine|desert_bones|desert_cow_skull)"
# What each rock is, from looking at every one alone on flat ground in the game (demos/battlefields/lineup.py):
#   wall     wide layered outcrops: the faces of steep ground
#   tower    tall narrow blocks: only on steep ground, sunk deep, or they stand like monoliths
#   boulder  rounded boulders and small stones: the rocks that lie out in the open and below cliffs
#   slab     low wide slabs and earth ledges: outcrops on gentle ground
ROCK_KIND = (("terrain_green_rock", "boulder"), ("cave_rock", "boulder"), ("rock_cliff_d", "slab"), ("cliff_soil", "slab"),
             ("rock_cliff_c", "tower"), ("rock_desert_cliff", "tower"), ("rock_cliff_a", "wall"), ("rock_cliff_b", "wall"))


def rock_kind(name):
    for key, kind in ROCK_KIND:
        if name.startswith(key): return kind
    return "wall"


class Rules:
    """What the map says about a place."""

    def __init__(self, biome="Plain", rugged=0.4, forest=0.2, water=None, water_dir=0.0, source=""):
        self.biome = biome; self.rugged = float(rugged); self.forest = float(forest)
        self.water = water; self.water_dir = float(water_dir); self.source = source

    def __repr__(self):
        return "Rules(%s, rugged %.2f, forest %.2f, water %s%s)" % (self.biome, self.rugged, self.forest, self.water, (" from " + self.source) if self.source else "")


def rules_from_map(center, wm=None, window=40.0):
    """Reads the rules for a campaign map position from its surroundings (a square of `window` map units)."""
    wm = wm or worldmap.WorldMap()
    t = wm.types_patch(center, window, 161)
    total = float(t.size)
    def share(*ids): return float(np.isin(t, ids).sum()) / total
    desert = share(2, 16, 13); steppe = share(5); swamp = share(15)
    plain = share(1, 4, 7, 14, 3, 20)
    biome = max((("Desert", desert), ("Steppe", steppe), ("Swamp", swamp), ("Plain", plain)), key=lambda kv: kv[1])[0]
    z = wm.patch(center, window, 97)
    relief = float(np.percentile(z, 95) - np.percentile(z, 5))
    rugged = float(np.clip(0.10 + relief / 6.0 + 0.9 * share(7) + 0.5 * share(13, 21), 0.05, 1.0))
    land = max(1e-6, 1.0 - share(0, 8, 10, 11, 18, 19, 22))
    forest = float(np.clip(share(4) / land, 0.0, 1.0))
    river = share(11, 6, 22); lake = share(8); sea = share(10, 18, 19) + (share(0) if share(10, 18, 19) > 0.02 else 0.0)
    water = None; wdir = 0.0
    kind = max((("sea", sea, 0.10), ("river", river, 0.025), ("lake", lake, 0.04)), key=lambda k: k[1] / k[2])
    if kind[1] >= kind[2]:
        water = kind[0]
        ids = {"sea": (10, 18, 19, 0), "river": (11, 6, 22), "lake": (8,)}[water]
        ys, xs = np.nonzero(np.isin(t, ids))
        wdir = math.atan2(float(ys.mean()) - 80.0, float(xs.mean()) - 80.0)
    return Rules(biome, rugged, forest, water, wdir, "map %.1f, %.1f" % center)


def pick_kind(rules, rng):
    r = rules.rugged
    w = {"ridge": 1.0 + r, "valley": 0.8 + 1.2 * r, "hill": 1.2, "broken": 0.5 + r, "pass": 2.2 * max(0.0, r - 0.35),
         "clearing": 3.0 * max(0.0, rules.forest - 0.25), "crossing": 0.0, "bluff": 0.0, "mesa": 0.0, "ledge": 1.6 * max(0.0, r - 0.55)}
    if rules.biome == "Desert": w["mesa"] = 1.2 + r; w["clearing"] = 0.0
    if rules.water == "river": w["crossing"] = 3.0
    if rules.water == "sea": w["bluff"] = 4.0
    names = list(w); p = np.array([w[k] for k in names]); p /= p.sum()
    return names[int(rng.choice(len(names), p=p))]


def _roles(tpl):
    """Sorts the template's texture layers into jobs, whatever the template calls them."""
    roles = {}
    def add(role, i): roles.setdefault(role, []).append(i)
    for i, name in enumerate(tpl.layer_names):
        if i == 0: continue
        nm = name.lower(); phys = (tpl.physics[i] or "").lower()
        if i in tpl.flora_layers: add("grasscover" if "grass" in nm else "flora", i)
        elif not tpl.enabled["summer"][i] and tpl.enabled["winter"][i]: add("snow", i)
        elif phys == "stone" or "rock" in nm or "cliff" in nm: add("rock", i)
        elif phys in ("soil", "sand", "mud") or any(k in nm for k in ("soil", "flow", "dirt", "mud", "earth")): add("soil", i)
        else: add("grass", i)
    return roles


def design(rules, kind=None, seed=None, log=print, palette=None):
    """Everything about one battlefield as arrays and lists; write() turns it into a scene folder.

    palette: build the field from custom assets instead of the game's (demos/bl2field makes one). A dict:
      "layers"  {"base" | "grass" | "soil" | "rock" | "snow": texture names for scenebuild.edit_layer}: what each kind of
                ground is painted with; the template's own detail meshes are dropped
      "rocks"   {model name: the fields of catalog.rock_usage plus "kind": "wall" | "tower" | "boulder" | "slab"}; the
                models must be known to rockfit (rockfit.register)
      "plants"  [{"mesh", "spacing" metres, "scale": (lo, hi), "where": "open" | "wood" | "any", "sink": metres}]: models
                scattered as entities (grass, bushes, trees)
      "props"   [model names] left lying about
      "support" True: the ground is raised under each rock, so the terrain is its collision body (a rock with
                "support": False in "rocks" is left out: an arch, anything to walk under)"""
    t0 = time.time()
    if seed is None: seed = int.from_bytes(os.urandom(4), "little")
    rng = np.random.default_rng(seed)
    kind = kind or pick_kind(rules, rng)
    if kind == "mesa" and rules.biome != "Desert": kind = "broken"
    cat = catalog.load()
    tid = TEMPLATES.get(rules.biome) or TEMPLATES["Plain"]
    tscene = [s for s in cat.records if s["id"] == tid][0]
    tpl = scenebuild.Template(tscene["dir"])
    F = terraform.Field(N, FIELD, rng); cell = F.cell; r = rules.rugged
    axis = float(rng.uniform(0, math.pi))
    if kind == "bluff": axis = rules.water_dir + math.pi / 2          # the armies face each other along the shore
    ca, sa = math.cos(axis), math.sin(axis)
    U = (F.X - C) * ca + (F.Y - C) * sa; V = -(F.X - C) * sa + (F.Y - C) * ca
    def pt(u, v): return (C + u * ca - v * sa, C + u * sa + v * ca)
    def uni(a, b): return float(rng.uniform(a, b))
    rr = F.dist(C, C)
    feats = []                                                         # (type, x, y, radius, facing)
    z = (5.0 + 12.0 * r) * terraform.fbm(F.shape, cell, 520.0, 3, rng, erode=0.4)
    river = None; lake = None; sea = None; ring = None; ledge = None

    def line(u0, v0, u1, v1, count): return [pt(u0 + (u1 - u0) * k / (count - 1), v0 + (v1 - v0) * k / (count - 1)) for k in range(count)]
    if kind == "ridge":
        u0 = uni(-70, 70); half = uni(340, 430); bend = uni(-70, 70); h = 17 + 26 * r
        sad = [(uni(0.4, 0.6), uni(0.55, 0.8), uni(0.1, 0.16))]
        if rng.random() < 0.5: sad.append((uni(0.14, 0.26) if rng.random() < 0.5 else uni(0.74, 0.86), uni(0.4, 0.7), uni(0.08, 0.12)))
        pts = line(u0 + bend, -half, u0 - bend, half, 9)
        z += F.chain(pts, uni(85, 120), h, gaps=sad, jitter=0.35)
        for pos, _, _ in sad:
            k = pts[int(round(pos * 8))]; feats.append(("choke", k[0], k[1], 22, axis))
        for pos in (0.25, 0.75):
            k = pts[int(round(pos * 8))]; feats.append(("high", k[0], k[1], 30, axis))
    elif kind == "valley":
        off = uni(155, 195); h = 30 + 40 * r
        for sgn in (-1, 1):
            gaps = [(uni(0.3, 0.7), uni(0.3, 0.5), uni(0.06, 0.1))] if rng.random() < 0.25 else []
            pts = line(-430, sgn * (off + uni(-25, 25)), 430, sgn * (off + uni(-25, 25)), 9)
            z += F.chain(pts, uni(85, 115), h * uni(0.85, 1.15), gaps=gaps, plateau=uni(0.0, 0.3), sharp=1.5, jitter=0.4)
            m = pt(uni(-120, 120), sgn * (off - 55)); feats.append(("high", m[0], m[1], 30, axis + (math.pi / 2 if sgn < 0 else -math.pi / 2)))
        for _ in range(int(rng.integers(1, 3))):
            k = pt(uni(-150, 150), uni(-60, 60)); z += F.hill(k[0], k[1], uni(35, 55), uni(4, 8))
    elif kind == "hill":
        sgn = 1 if rng.random() < 0.5 else -1
        p = pt(uni(-60, 60), sgn * uni(70, 150)); R = uni(150, 210)
        z += F.hill(p[0], p[1], R, 24 + 30 * r, plateau=uni(0.3, 0.45))
        feats.append(("high", p[0], p[1], 40, axis))
        q = pt(uni(-200, 200), -sgn * uni(180, 260)); z += F.hill(q[0], q[1], uni(80, 120), uni(10, 18))
        feats.append(("high", q[0], q[1], 25, axis))
    elif kind == "crossing":
        river = {"u0": uni(-40, 40), "amp": uni(25, 60), "lam": uni(110, 190), "phi": uni(0, 6.28),
                 "fords": [uni(-250, -130), uni(-70, 70), uni(130, 250)][:int(rng.integers(2, 4))]}
        z += (6 + 10 * r) * smoothstep(np.abs(U - river["u0"]), 40, 320)
    elif kind == "clearing":
        ring = {"radius": uni(245, 285)}
        for _ in range(int(rng.integers(3, 6))):
            k = pt(uni(-180, 180), uni(-180, 180)); z += F.hill(k[0], k[1], uni(40, 80), uni(3, 6 + 5 * r))
    elif kind == "pass":
        w0 = uni(60, 90); h = 42 + 46 * r
        for sgn in (-1, 1):
            rad = uni(120, 160)
            pts = line(-420, sgn * (w0 + rad + uni(40, 110)), 420, sgn * (w0 + rad + uni(40, 110)), 11)
            pts[5] = pt(uni(-50, 50), sgn * (w0 + rad * 0.95))                       # the narrow place
            z += F.chain(pts, rad, h * uni(0.85, 1.1), sharp=1.7, jitter=0.45)
            z += F.hill(*pt(uni(-150, 150), sgn * (w0 + rad + 230)), uni(230, 310), h * 0.9)
            feats.append(("high", *pt(uni(-80, 80), sgn * (w0 + rad * 0.6)), 30, axis + (math.pi / 2 if sgn < 0 else -math.pi / 2)))
        feats.append(("choke", C, C, 30, axis))
    elif kind == "bluff":
        sea = {"shore": uni(170, 235), "bluff": 9 + 11 * r}
    elif kind == "ledge":       # a shelf on a mountainside: a wall above it on one side, a sheer drop on the other
        ledge = {"half": uni(62, 82), "edge": uni(95, 125), "drop": 38 + 32 * r, "wall": 45 + 45 * r}      # the wall is nearer than the drop: armies deploy clear of the edge
        z += ledge["wall"] * smoothstep(-V - 0.85 * ledge["half"], 0, 150) + 0.03 * np.maximum(-V - ledge["half"] - 150, 0)
        for _ in range(int(rng.integers(2, 5))):
            k = pt(uni(-260, 260), uni(-40, 40)); z += F.hill(k[0], k[1], uni(30, 55), uni(2, 5))
        feats.append(("high", *pt(uni(-90, 90), -ledge["half"] - 35), 25, axis + math.pi / 2))
        feats.append(("choke", C, C, 30, axis))
    elif kind == "broken":
        K = int(12 + 10 * r); made = 0; tries = 0
        while made < K and tries < 400:
            tries += 1
            u, v = uni(-420, 420), uni(-420, 420)
            if math.hypot(u, v) > 430 or math.hypot(abs(u) - SPAWN, v) < 85: continue
            x, y = pt(u, v); z += F.hill(x, y, uni(28, 75), uni(5, 9 + 9 * r), plateau=uni(0, 0.4), sharp=2.0); made += 1
            if math.hypot(u, v) < 300 and rng.random() < 0.3: feats.append(("high", x, y, 18, axis))
    elif kind == "mesa":
        made = 0; tries = 0
        while made < int(rng.integers(3, 6)) and tries < 200:
            tries += 1
            u, v = uni(-380, 380), uni(120, 380) * (1 if rng.random() < 0.5 else -1)
            x, y = pt(u, v); z += F.hill(x, y, uni(90, 160), uni(16, 30), plateau=uni(0.55, 0.7), sharp=3.0, wobble=0.55); made += 1
            if math.hypot(u, v) < 300: feats.append(("high", x, y, 35, axis))

    if rules.biome == "Desert": z += (1.5 + 2.5 * r) * terraform.fbm(F.shape, cell, 95.0, 2, rng, ridged=True)       # dunes
    # bend the drawn landforms: no round hills, no straight ridges (domain warping)
    wx = 42.0 * terraform.noise(F.shape, cell, 270.0, rng) + 14.0 * terraform.noise(F.shape, cell, 90.0, rng)
    wy = 42.0 * terraform.noise(F.shape, cell, 270.0, rng) + 14.0 * terraform.noise(F.shape, cell, 90.0, rng)
    z = terraform.warp(z, wx, wy, cell)
    # the frame: hills or mountains around the play area, by how rugged the land is (none out at sea)
    frame = smoothstep(rr * (1 + 0.12 * F.wobble(300.0)), 400, 590) * (12 + 95 * r) * (0.55 + 0.45 * terraform.fbm(F.shape, cell, 380.0, 4, rng, ridged=True, erode=0.5))
    frame = frame * (0.86 + 0.28 * terraform.fbm(F.shape, cell, 70.0, 3, rng, ridged=True))
    if sea is not None:
        dshore = V + sea["shore"] + 28 * F.wobble(220.0)               # metres inland from the waterline, negative out at sea
        land = smoothstep(dshore, 40, 170)
        prof = np.where(dshore < 0, np.maximum(dshore * 0.06, -6.0), 0.0)
        prof = prof + 1.6 * smoothstep(dshore, 0, 26) + sea["bluff"] * smoothstep(dshore, 26, 62) + 0.02 * np.maximum(dshore - 62, 0)
        z = prof + z * land * 0.8 + frame * land
        feats.append(("high", *pt(uni(-60, 60), -sea["shore"] + 90), 30, axis))
    else:
        z += frame

    # real landforms and real small relief, lifted from shipped battlefields of this biome and quilted
    donors = [s for s in cat.scenes(rules.biome if rules.biome in TEMPLATES else "Plain", 6, 200.0)][:5]
    if tscene not in donors: donors = [tscene] + donors[:4]
    mid = terraform.DetailBank(cell, wavelength=20.0, clip=3.5, longest=240.0); fine = terraform.DetailBank(cell, wavelength=24.0)
    for s in donors:
        zd = terrain.Terrain(os.path.join(s["dir"], "terrain.bin")).heightmap(terrain.scene_terrain_info(os.path.join(s["dir"], "scene.xscene")))
        mid.add(zd); fine.add(zd)
    calm = 1.0
    if sea is not None: calm = smoothstep(dshore, 10, 80)
    z = z + (0.5 + 0.9 * r) * mid.synthesize(F.shape, seed + 1, patch=260.0) * calm
    # running water: gullies and valleys where the flow gathers, ridges between them; then scree under the steepest faces
    z0e = z
    z, drain = terraform.erode_fluvial(z, cell, strength=4.0 + 6.0 * r, iterations=9, rng=rng)
    if sea is not None: z = z0e + (z - z0e) * calm
    if r > 0.5 or kind in ("pass", "mesa"): z = terraform.erode_thermal(z, cell, talus=1.15, iterations=14)
    z = z + (fine.apply(z, seed + 2) - z) * calm
    # level ground where the armies deploy
    zb = terraform.blur_fast(z, 22.0 / cell)
    for sgn in (-1, 1):
        x, y = pt(sgn * SPAWN, 0); w = 0.75 * (1 - smoothstep(F.dist(x, y), 35, 90)); z = z * (1 - w) + zb * w

    offlimits = np.zeros(F.shape, bool)
    if ledge is not None:
        over = V - (ledge["edge"] + 14 * F.wobble(130.0) + 5 * F.wobble(38.0))          # metres past the edge
        z = z - ledge["drop"] * smoothstep(over, 0.0, 7.0) - 0.05 * np.clip(over - 7.0, 0.0, 400.0)
        offlimits = over > -2.5                                                    # nothing walks on the face or below it
    moved = []
    for typ, x, y, rad, face in feats:
        if typ == "high":
            j0, j1 = int(max(0, (y - 60) / cell)), int(min(N, (y + 60) / cell)); i0, i1 = int(max(0, (x - 60) / cell)), int(min(N, (x + 60) / cell))
            if j1 > j0 and i1 > i0:
                jj, ii = np.unravel_index(int(np.argmax(zb[j0:j1, i0:i1])), (j1 - j0, i1 - i0)); x, y = (i0 + ii) * cell, (j0 + jj) * cell
        moved.append((typ, x, y, rad, face))
    feats = moved

    # water
    level = None; under = np.zeros(F.shape, bool); deep = np.zeros(F.shape, bool)
    if river is not None:
        Uc = river["u0"] + river["amp"] * np.sin(V / river["lam"] + river["phi"]) + 22 * F.wobble(260.0)
        dr = np.abs(U - Uc)
        ford = np.zeros(F.shape)
        for vf in river["fords"]: ford = np.maximum(ford, 1 - smoothstep(np.abs(V - vf), 14, 32))
        W = 9.0 + 5.0 * ford
        level = float(np.percentile(z[dr < 9.0], 25)) - 0.3
        z = np.minimum(z, level + 0.6 + 0.05 * np.maximum(dr - W, 0) ** 1.2)       # the river sits in a valley
        z = np.where(dr > W, np.maximum(z, level + 0.6), z)                         # and nothing else is under water
        inside = 1 - smoothstep(dr, 0.6 * W, 1.15 * W)
        z = z * (1 - inside) + (level - (2.3 * (1 - ford) + 0.4 * ford) * (1 - smoothstep(dr, 0.3 * W, W))) * inside
        z = np.maximum(z, level - 2.5)                                              # no pits under the river bed
        for vf in river["fords"]:
            if abs(vf) < 250:
                ucv = river["u0"] + river["amp"] * math.sin(vf / river["lam"] + river["phi"])
                feats.append(("choke", *pt(ucv, vf), 20, axis))
    if sea is not None:
        level = 0.0
        z = np.where(dshore > 30, np.maximum(z, 0.9 + 0.01 * np.minimum(dshore, 200)), z)      # nothing inland dips under the sea
    if level is not None: under = z < level - 0.05; deep = z < level - 0.9
    off = 40.0 - float(z.min()); z = z + off
    if level is not None: level += off

    sl = terraform.slope(terraform.blur_fast(z, 1.5), cell)
    # rocks: on the steep ground, on the high points, and a scatter of outcrops. Which rock, how big, how deep in the ground
    # and how tilted comes from how the shipped battlefields of this biome use each one (catalog.rock_usage). They come
    # before the painting because the ground is banked up against each one (sdk/rockfit.py): a rock is an open shell and
    # where its rim is in the air the player sees through it.
    if palette:
        usage = dict(palette["rocks"])
    else:
        usage = catalog.rock_usage().get(rules.biome) or catalog.rock_usage().get("Plain", {})
        usage = {k: v for k, v in usage.items() if v["n"] >= 10 and not any(w in k for w in SKIP_ROCKS)}
        if rules.biome == "Plain": usage = {k: v for k, v in usage.items() if "desert" not in k}      # sand-coloured rock in a green field
    def kind_of(name): return usage[name]["kind"] if palette else rock_kind(name)
    kinds = {}
    for k in usage: kinds.setdefault(kind_of(k), []).append(k)
    walls = kinds.get("wall") or list(usage)
    towers = kinds.get("tower") or walls
    boulders = kinds.get("boulder") or kinds.get("slab") or walls
    slabs = kinds.get("slab") or boulders
    def pick(names):
        w = np.array([usage[k]["n"] for k in names], float); return names[int(rng.choice(len(names), p=w / w.sum()))]
    def at(a, x, y): return a[min(N - 1, max(0, int(round(y / cell)))), min(N - 1, max(0, int(round(x / cell))))]
    spawn_pts = [pt(-SPAWN, 0), pt(SPAWN, 0)]
    def clear(x, y, d=75.0): return all(math.hypot(x - sx, y - sy) > d for sx, sy in spawn_pts)
    spots = []
    steep = sl > 0.45
    gy_, gx_ = np.gradient(terraform.blur_fast(z, 3.0 / cell), cell)
    near_cliff = terraform.blur_fast(steep.astype(np.float64), 14.0 / cell)
    def usable(x, y, reach=545.0): return math.hypot(x - C, y - C) < reach and clear(x, y) and not at(under, x, y)
    for x, y in terraform.poisson(FIELD, 17.0, rng, lambda x, y: usable(x, y) and bool(at(steep, x, y))): spots.append((x, y, "cliff"))
    for x, y in terraform.poisson(FIELD, 11.0, rng, lambda x, y: usable(x, y, 420.0) and not at(steep, x, y) and 0.12 < float(at(near_cliff, x, y)) < 0.5):
        if rng.random() < 0.35: spots.append((x, y, "scree"))
    for f in feats:
        if f[0] != "high": continue
        for _ in range(int(rng.integers(1, 3))):
            a = uni(0, 6.28); d = uni(4, f[3] * 0.6); x, y = f[1] + d * math.cos(a), f[2] + d * math.sin(a)
            if usable(x, y): spots.append((x, y, "lump"))
    lumps_pts = terraform.poisson(FIELD, 70.0, rng, lambda x, y: usable(x, y, 400.0) and float(at(sl, x, y)) < 0.35)
    for x, y in lumps_pts[:int(10 + 16 * r)]: spots.append((x, y, "lump"))
    def nearest(lst, n):        # a budget per job, the ones nearest the fighting first
        if len(lst) <= n: return lst
        order = np.argsort([rng.random() + 0.9 * math.hypot(q[0] - C, q[1] - C) / 545.0 for q in lst])
        return [lst[k] for k in sorted(order[:n])]
    spots = nearest([q for q in spots if q[2] == "cliff"], 130) + [q for q in spots if q[2] == "lump"] + nearest([q for q in spots if q[2] == "scree"], 70)
    if not usage: spots = []          # a palette without rocks
    entities = []; blocked = np.zeros(F.shape, bool); rocks = []
    wet = terraform.blur_fast(under.astype(np.float64), 6.0 / cell) > 0.02 if level is not None else None
    banked = 0.0
    for x, y, why in spots:
        if why == "cliff": name = pick(towers if rng.random() < 0.12 else walls)
        elif why == "scree": name = pick(boulders)
        else: name = pick(slabs if rng.random() < 0.3 and float(at(sl, x, y)) < 0.18 else boulders)     # a slab is a thin plate: only where it can lie flat
        u = usage[name]; rk = kind_of(name)
        sc = u["scale"] * uni(0.85, 1.15) if rng.random() < 0.85 else uni(u["scale"], max(u["scale"], u["scale_hi"]))
        sink = uni(u["dz_lo"], min(0.2, u["dz_hi"])) if u["dz_lo"] < min(0.2, u["dz_hi"]) else min(0.0, u["dz"])
        try: body = rockfit.rock_shape(name)["body"]
        except Exception: continue
        zlo = float(body[:, 2].min()); h0 = float(np.ptp(body[:, 2])); big = h0 * u["scale"] > 6.0
        def bury(share): return -zlo * sc - share * h0 * sc              # the origin's height for that share of the rock under the ground
        if rk == "tower": sink = min(sink, bury(0.1))                                          # a tower stands well into the ground
        if why == "lump":                                                                   # a rock out in the open: a boulder or a slab, part of it in the ground
            sc *= uni(0.45, 0.85) if rk == "boulder" and big else uni(0.8, 1.2)
            sink = bury(uni(0.12, 0.25)) if rk == "boulder" else min(sink, bury(uni(0.2, 0.35)))
        if why == "scree": sc *= uni(0.25, 0.45) if big else uni(0.8, 1.3); sink = bury(uni(0.15, 0.3))   # fallen blocks below a cliff
        tilt = 0.0 if rk == "slab" else 0.1 if rk == "tower" else min(0.5, u["tilt"])     # a tilted plate or tower sticks out of a slope like a claw
        if u.get("facing_r", 0.0) > 0.65:      # a one-sided cliff face: turned the way the shipped scenes turn it on a slope
            down = math.atan2(-float(at(gy_, x, y)), -float(at(gx_, x, y))) + math.radians(u["facing"])
            yaws = [down + float(rng.normal(0.0, 0.15)), down - 0.35, down + 0.35]; leans = [u["lean"] * q for q in (0.3, 0.8, 1.3, 1.8)]; rolls = (uni(-0.04, 0.04),)
        else:
            yaws = [uni(-3.14, 3.14) for _ in range(6)]; leans = (uni(-tilt, tilt) / 2, 0.0); rolls = (uni(-tilt, tilt) / 2, 0.0)
        tall = h0 * sc
        f = rockfit.fit(z, cell, name, x, y, yaws, leans, [sink + q * tall for q in (0.06, 0.0, -0.06, -0.12, -0.2, -0.32, -0.45)], sc, rolls,
                        show=0.2 if why == "scree" else 0.3, max_fill=5.0)
        if f is None: continue                 # the ground here is the wrong shape for this rock
        banked += rockfit.cover(z, cell, f["rim"], keep=wet)
        rockfit.footprint(z, cell, f["body"], mask=blocked)
        if palette and palette.get("support") and u.get("support", True): rockfit.support(z, cell, name, f["pos"], f["rot"], sc, keep=wet)
        entities.append({"mesh" if palette else "prefab": name, "position": f["pos"], "rotation": f["rot"], "scale": (sc, sc, sc)})
        rocks.append((x, y, sc, why))

    # ground textures by job
    sl = terraform.slope(terraform.blur_fast(z, 1.5), cell)
    curve = z - terraform.blur_fast(z, 10)
    wetground = terraform.blur_fast(under.astype(np.float64), 3.0 / cell)
    roles = _roles(tpl); masks = {}
    def m8(a): return np.clip(np.rint(np.clip(a, 0, 1) * 255), 0, 255).astype(np.uint8)
    flat = 1 - smoothstep(sl, 0.25, 0.5)
    dry = 1 - smoothstep(wetground, 0.05, 0.3)
    cov = tpl.coverage()
    def patches(share):
        """A mask of natural patches covering `share` of the map, nearly on or off (detail meshes need more than half strength)."""
        n = terraform.noise(F.shape, cell, 140.0, rng) + 0.6 * terraform.noise(F.shape, cell, 55.0, rng) + 0.3 * terraform.noise(F.shape, cell, 22.0, rng)
        thr = float(np.quantile(n[::4, ::4], 1 - share)); return smoothstep(n, thr - 0.04, thr + 0.04)
    for k, i in enumerate(roles.get("grass", [])):
        if tpl.meshes[i]:      # the layer that carries the grass and plant models: as widespread as the template paints it (61 % on the plains)
            masks[i] = m8(patches(float(np.clip(cov[i] * 1.15, 0.55, 0.85))) * flat * dry)
        else:
            masks[i] = m8(smoothstep(terraform.noise(F.shape, cell, (90.0, 45.0, 160.0, 60.0)[k % 4], rng), 0.05, 0.5) * flat)
    # where water gathers on sloping ground: bare earth and gravel in the gullies (not on the flats, where a drainage line means nothing)
    runoff = smoothstep(terraform.blur_fast(np.log10(np.maximum(drain, 1.0)), 2.5 / cell), 3.6, 4.5) * smoothstep(sl, 0.07, 0.16) * (1 - smoothstep(sl, 0.5, 0.8))
    for k, i in enumerate(roles.get("soil", [])):
        a = np.maximum(smoothstep(-curve, 0.25, 0.9) * smoothstep(sl, 0.08, 0.25), 0.8 * runoff) if k == 0 else np.maximum(smoothstep(-curve, 0.6, 1.6), smoothstep(wetground, 0.15, 0.6))
        if k == 0 and len(roles.get("soil", [])) == 1: a = np.maximum(a, smoothstep(wetground, 0.15, 0.6))
        masks[i] = m8(a)
    for k, i in enumerate(roles.get("rock", [])):
        masks[i] = m8(smoothstep(sl, 0.38, 0.58) if k == 0 else smoothstep(sl, 0.7 + 0.08 * (k - 1), 1.0 + 0.08 * (k - 1)))
    for k, i in enumerate(roles.get("snow", [])):
        masks[i] = m8((1 - smoothstep(sl, 0.45, 0.7)) * (1.0 if k == 0 else smoothstep(terraform.noise(F.shape, cell, 70.0, rng), -0.2, 0.4)))

    # forest
    fnoise = terraform.fbm(F.shape, cell, 230.0, 4, rng)
    frac = float(np.clip(rules.forest * 1.1 + 0.03, 0.03, 0.65))
    if rules.biome == "Desert": frac = max(frac, 0.2)
    if ring is not None:
        forest = smoothstep(rr * (1 + 0.22 * F.wobble(170.0)), ring["radius"] - 30, ring["radius"] + 25)
        forest = np.maximum(forest, smoothstep(fnoise, 0.42, 0.52) * 0.9 * smoothstep(rr, 90, 150))
    else:
        thr = float(np.quantile(fnoise[rr < 520], 1 - frac))
        forest = smoothstep(fnoise, thr - 0.03, thr + 0.05) * (0.25 + 0.75 * smoothstep(np.abs(V), 55, 150))
    if rules.biome != "Desert":      # the hills around the field are wooded where they are not too steep
        forest = np.maximum(forest, smoothstep(rr, 440, 520) * smoothstep(terraform.noise(F.shape, cell, 120.0, rng), -0.25, 0.05))
    else:                            # desert scrub does not stand in woods: small clumps all over the field
        cn = terraform.noise(F.shape, cell, 55.0, rng) + 0.7 * terraform.noise(F.shape, cell, 22.0, rng)
        forest = np.maximum(forest * 0.6, smoothstep(cn, *(float(np.quantile(cn[::4, ::4], q)) for q in (0.90, 0.94))))
    forest = forest * (1 - smoothstep(sl, 0.5, 0.75)) * (1 - smoothstep(wetground, 0.02, 0.2))
    for sgn in (-1, 1): forest = forest * smoothstep(F.dist(*pt(sgn * SPAWN, 0)), 55, 95)
    forest = smoothstep(terraform.blur_fast(forest, 2.0 / cell), 0.42, 0.52) * (terraform.blur_fast(blocked.astype(np.float64), 1.5 / cell) < 0.05)
    for k, i in enumerate(roles.get("flora", [])): masks[i] = m8(forest)
    for i in roles.get("grasscover", []): masks[i] = m8(patches(float(np.clip(cov[i] * 1.15, 0.4, 0.8))) * flat * dry)

    # the small things the shipped battlefields of the biome leave lying about: dead and fallen trees, roots, logs, bones
    props = [(k_, n_) for k_, n_, _ in cat.prefabs(rules.biome if rules.biome in TEMPLATES else "Plain", PROPS)][:14]
    if palette: props = [(k_, 1) for k_ in palette.get("props", [])]
    if props:
        wts = np.array([n_ for _, n_ in props], float) ** 0.5; wts /= wts.sum()
        edge = terraform.blur_fast(forest, 8.0 / cell)
        def lies(x, y):
            return math.hypot(x - C, y - C) < 440 and clear(x, y, 35.0) and not at(under, x, y) and not at(blocked, x, y) and float(at(sl, x, y)) < 0.3
        for x, y in terraform.poisson(FIELD, 32.0, rng, lies)[:60]:
            name = props[int(rng.choice(len(props), p=wts))][0]
            woody = "tree" in name or "log" in name or "root" in name
            if rng.random() > (0.9 if 0.05 < float(at(edge, x, y)) < 0.9 else 0.2) and woody: continue
            if not woody and rng.random() > 0.35: continue
            if palette: woody = False
            entities.append({"mesh" if palette else "prefab": name, "position": (x, y, float(at(z, x, y)) - 0.05), "rotation": (0.0, 0.0, uni(-3.14, 3.14))})
    fy, fx = np.nonzero((forest > 0.6) & (rr < PLAY))
    for _ in range(2 if len(fy) > 2000 and rules.biome != "Desert" and not palette else 0):
        k_ = int(rng.integers(len(fy))); entities.append({"prefab": "detail_birds_forest_sound", "position": (fx[k_] * cell, fy[k_] * cell, float(z[fy[k_], fx[k_]]) + 6.0)})

    # custom plants: models scattered as entities (the template's detail meshes are the game's own, so a palette drops them)
    planted = 0
    for pl in (palette or {}).get("plants", []):
        lo, hi = pl.get("scale", (0.8, 1.2)); where = pl.get("where", "any"); reach = pl.get("reach", 360.0)
        def grows(x, y):
            if math.hypot(x - C, y - C) > reach or at(under, x, y) or at(blocked, x, y) or float(at(sl, x, y)) > pl.get("max_slope", 0.4): return False
            w = float(at(forest, x, y))
            return w > 0.5 if where == "wood" else w < 0.5 if where == "open" else True
        for x, y in terraform.poisson(FIELD, pl["spacing"], rng, grows)[:pl.get("limit", 900)]:
            if not clear(x, y, pl.get("clear", 0.0)): continue
            sc_ = uni(lo, hi)
            entities.append({"mesh": pl["mesh"], "position": (x, y, float(at(z, x, y)) - pl.get("sink", 0.05) * sc_), "rotation": (0.0, 0.0, uni(-3.14, 3.14)), "scale": (sc_, sc_, sc_)})
            planted += 1
    # markers for the battle AI
    for typ, x, y, rad, face in feats:
        if math.hypot(x - C, y - C) > PLAY - 20 or at(under, x, y): continue
        entities.append({"prefab": "TacticalPosition", "position": (x, y, float(at(z, x, y))), "rotation": (0.0, 0.0, face),
                         "scripts": {"TacticalPosition": {"_width": "%.3f" % max(15.0, rad), "_tacticalPositionType": "HighGround" if typ == "high" else "ChokePoint",
                                                          "_tacticalRegionMembership": "Opening"}}})
    entities.append({"prefab": "TacticalRegion", "position": (C, C, float(at(z, C, C))), "scripts": {"TacticalRegion": {"radius": "45.000", "tacticalRegionType": "Opening"}}})
    fy, fx = np.nonzero((forest > 0.6) & (rr < PLAY - 40))
    if len(fy) > 4000:
        for _ in range(2):
            k = int(rng.integers(len(fy))); x, y = fx[k] * cell, fy[k] * cell
            entities.append({"prefab": "TacticalRegion", "position": (x, y, float(z[fy[k], fx[k]])), "scripts": {"TacticalRegion": {"radius": "35.000", "tacticalRegionType": "Forest"}}})

    # border and spawn paths: the axis first, then the other bearings with the least water, forest and steep ground
    border = [(C + PLAY * math.cos(a), C + PLAY * math.sin(a)) for a in np.linspace(0, 2 * math.pi, 32, endpoint=False)]
    def path(angle):
        pts = []; bad = 0.0
        for q in np.linspace(-SPAWN / PLAY, SPAWN / PLAY, 7):
            x = C + PLAY * q * math.cos(angle); y = C + PLAY * q * math.sin(angle)
            pts.append((x, y, float(at(z, x, y)))); bad += 3.0 * bool(at(deep, x, y)) * (abs(q) > 0.3) + float(at(forest, x, y)) + 2.0 * (float(at(sl, x, y)) > 0.5) + 6.0 * bool(at(offlimits, x, y))
        return bad + 6.0 * (bool(at(deep, *pts[0][:2])) + bool(at(deep, *pts[-1][:2]))), pts
    others = sorted((path(axis + math.pi * k / 8) + (k,) for k in range(1, 8)), key=lambda t: t[0])[:3]
    paths = [path(axis)[1]] + [o[1] for o in sorted(others, key=lambda t: t[2])]
    walk = scenebuild.polygon_mask(border, F.shape, cell) & ~deep & ~blocked & ~offlimits

    def g(x, y): return float(at(z, x, y))
    a_, b_ = spawn_pts
    def toward(p, q, back=0.0, side=0.0):
        dx, dy = q[0] - p[0], q[1] - p[1]; L = math.hypot(dx, dy) or 1.0
        return (p[0] - dx / L * back - dy / L * side, p[1] - dy / L * back + dx / L * side)
    views = [("spawnA", toward(a_, (C, C), 20.0) + (0.0,), (C, C, g(C, C))), ("spawnB", toward(b_, (C, C), 20.0) + (0.0,), (C, C, g(C, C))),
             ("centre", (C, C, 0.0), (a_[0], a_[1], g(*a_))), ("flank", (C - 170 * sa, C + 170 * ca, 0.0), (C, C, g(C, C)))]
    for typ, x, y, rad, face in feats[:3]:
        e = toward((x, y), (C, C), -30.0, 12.0); views.append((typ, e + (0.0,), (x, y, g(x, y) + 1.0)))
    if ledge is not None:
        e = pt(-70, ledge["edge"] - 18); t_ = pt(40, ledge["edge"] + 22); views.append(("cliff", e + (0.0,), (t_[0], t_[1], g(*t_))))
    near = [q for q in rocks if q[3] != "scree" and math.hypot(q[0] - C, q[1] - C) < PLAY]
    for q in near[:2]:
        e = toward(q[:2], (C, C), -24.0, 8.0); views.append(("rock", e + (0.0,), (q[0], q[1], g(q[0], q[1]) + 1.5)))
    d = {"rules": rules, "kind": kind, "seed": seed, "axis": axis, "palette": palette, "planted": planted, "template": tpl, "z": z, "masks": masks, "roles": roles, "forest": forest, "views": views,
         "under": under, "deep": deep, "level": level, "rocks": rocks, "entities": entities, "features": feats, "border": border, "paths": paths,
         "walk": walk, "slope": sl, "seconds": time.time() - t0}
    play = rr < PLAY
    d["stats"] = {"relief_play": float(z[play].max() - z[play].min()), "steep_play": float((sl[play] > 0.5).mean()), "forest_play": float((forest[play] > 0.5).mean()),
                  "water_play": float(under[play].mean()), "rocks": len(rocks)}
    log("%s %s seed %d: play-area relief %.0f m, steep %.0f %%, forest %.0f %%, water %.0f %%, %d rocks, %.1f s"
        % (rules.biome, kind, seed, d["stats"]["relief_play"], 100 * d["stats"]["steep_play"], 100 * d["stats"]["forest_play"], 100 * d["stats"]["water_play"], len(rocks), d["seconds"]))
    return d


def write(d, name, out_dir, log=print):
    """The scene folder of a design."""
    # rock textures projected from six sides: on steep ground a texture laid on from above stretches into stripes
    edits = {i: {"six_side": True} for i in d["roles"].get("rock", [])}
    pal = d.get("palette")
    if pal:        # every layer painted with the palette's textures, none of the template's detail meshes
        tpl = d["template"]; role_of = {i: role for role, ids in d["roles"].items() for i in ids}
        for i in range(len(tpl.layers)):
            role = "base" if i == 0 else {"grasscover": "grass", "flora": "grass"}.get(role_of.get(i, "grass"), role_of.get(i, "grass"))
            tex = pal["layers"].get(role) or pal["layers"]["base"]
            edits[i] = dict(edits.get(i, {}), textures=tex, meshes=[], attrs=pal.get("layer_attrs", {}).get(role, {}))
    info = scenebuild.build(d["template"], out_dir, name, d["z"], d["masks"], entities=d["entities"], spawn_paths=d["paths"], border=d["border"],
                            walkable=d["walk"], water_level=d["level"], layer_edits=edits, log=log)
    d["nav_cells"] = info["nav_cells"]; d["z"] = info["z"]
    with open(os.path.join(out_dir, "views.txt"), "w", encoding="utf-8") as f:      # name, eye x y z, target x y z: for the test camera (GenFields autotest word "views")
        for nm, eye, tgt in d["views"]: f.write("%s %.2f %.2f %.2f %.2f %.2f %.2f\n" % ((nm,) + tuple(eye) + tuple(tgt)))
    return info


def preview(d, path=None, size=900, title=True):
    """A top view: ground coloured by its textures and shaded, water, forest, rocks, AI markers, border, spawn paths."""
    from PIL import Image, ImageDraw, ImageFont
    z = d["z"]; cell = FIELD / (N - 1); tpl = d["template"]; roles = d["roles"]
    gy, gx = np.gradient(z, cell)
    nrm = np.dstack((-gx, -gy, np.ones_like(z))); nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    light = np.array([-0.5, 0.5, 0.7]); light /= np.linalg.norm(light)
    sh = np.clip((nrm * light).sum(axis=2), 0, 1) ** 1.3
    desert = d["rules"].biome == "Desert"; steppe = d["rules"].biome == "Steppe"
    col = np.zeros(z.shape + (3,)) + np.array((196, 170, 118) if desert else (150, 148, 88) if steppe else (116, 134, 76))
    tint = {"grass": [(96, 124, 66), (132, 150, 84), (158, 146, 98), (110, 128, 70)], "soil": [(126, 102, 74), (104, 86, 66)], "rock": [(128, 124, 118), (98, 96, 94), (88, 86, 84)]}
    if desert: tint = {"grass": [(204, 180, 128), (188, 160, 110)], "soil": [(176, 146, 100), (160, 130, 92)], "rock": [(150, 122, 96), (128, 104, 84), (118, 96, 80)]}
    for role in ("grass", "soil", "rock"):
        for k, i in enumerate(roles.get(role, [])):
            if i in d["masks"]:
                a = (d["masks"][i].astype(float) / 255)[..., None]; col = col * (1 - a) + np.array(tint[role][k % len(tint[role])]) * a
    img = col * (0.35 + 0.75 * sh[..., None])
    fa = np.clip(d["forest"], 0, 1)[..., None] * 0.78
    img = img * (1 - fa) + np.array((70, 84, 40) if desert else (30, 66, 38)) * fa * (0.55 + 0.6 * sh[..., None])
    img[d["under"]] = img[d["under"]] * 0.3 + np.array([44, 92, 146]) * 0.7
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)[::-1]).resize((size, size), Image.LANCZOS)
    dr = ImageDraw.Draw(im); s = size / FIELD
    def P(x, y): return (x * s, size - y * s)
    for x, y, sc, why in d["rocks"]:
        r = max(1.5, 2.6 * sc / (0.5 if desert else 1.0) * s * 2.2); px, py = P(x, y); dr.ellipse([px - r, py - r, px + r, py + r], fill=(58, 54, 52))
    b = d["border"]; dr.line([P(*p) for p in b + b[:1]], fill=(235, 70, 60), width=2)
    for k, pts in enumerate(d["paths"]):
        c = (90, 170, 255) if k == 0 else (60, 110, 170)
        dr.line([P(p[0], p[1]) for p in pts], fill=c, width=3 if k == 0 else 1)
        if k == 0:
            for p in (pts[0], pts[-1]): px, py = P(p[0], p[1]); dr.ellipse([px - 6, py - 6, px + 6, py + 6], outline=c, width=3)
    for typ, x, y, rad, face in d["features"]:
        px, py = P(x, y); c = (255, 220, 60) if typ == "high" else (255, 120, 220)
        dr.polygon([(px, py - 7), (px - 6, py + 5), (px + 6, py + 5)], outline=c)
    if title:
        try: font = ImageFont.truetype("arial.ttf", 20)
        except Exception: font = ImageFont.load_default()
        st = d["stats"]; text = "%s  %s   relief %d m  steep %d%%  forest %d%%  rocks %d" % (d["rules"].biome, d["kind"], st["relief_play"], 100 * st["steep_play"], 100 * st["forest_play"], st["rocks"])
        dr.rectangle([0, 0, size, 28], fill=(18, 18, 20)); dr.text((8, 3), text, fill=(235, 235, 235), font=font)
    if path: im.save(path)
    return im


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--map", nargs=2, type=float, metavar=("X", "Y")); ap.add_argument("--biome", default="Plain")
    ap.add_argument("--kind", choices=KINDS); ap.add_argument("--rugged", type=float, default=0.4); ap.add_argument("--forest", type=float, default=0.2)
    ap.add_argument("--water", choices=("river", "lake", "sea")); ap.add_argument("--seed", type=int)
    ap.add_argument("--name"); ap.add_argument("--out"); ap.add_argument("--preview")
    a = ap.parse_args()
    rules = rules_from_map(tuple(a.map)) if a.map else Rules(a.biome, a.rugged, a.forest, a.water)
    print(rules)
    d = design(rules, a.kind, a.seed)
    if a.name:
        out = a.out or os.path.join(config.SDK_STAGE_DIR, "scenes", a.name)
        write(d, a.name, out); print("scene", a.name, "written to", out)
    if a.preview: preview(d, a.preview); print("preview", a.preview)
