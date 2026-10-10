"""The campaign world map as data: its height field, its settlements, and the patch of land around any map position.

The world map is an ordinary scene (Modules/SandBox/SceneObj/Main_map on game v1.4.8.119303) whose terrain.bin holds
16 x 16 nodes of 53 map units, 4097 x 4097 height samples: one sample every 0.207 units. Campaign positions (a
settlement's posX / posY, a party's position) are the same x / y as the terrain's.

    wm = worldmap.WorldMap()                      # reads the installed game's map
    z = wm.patch((205.0, 440.0), size=12.0)       # the land around that position, as a height array
"""
import os, re, sys, zlib

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, terrain, navmesh
else:
    from . import config, terrain, navmesh

MAP_SCENE = ("SandBox", "Main_map")
TERRAIN_TYPES = {1: "Plain", 2: "Desert", 3: "Snow", 4: "Forest", 5: "Steppe", 6: "Fording", 7: "Mountain", 8: "Lake", 10: "Water",
                 11: "River", 13: "Canyon", 14: "RuralArea", 15: "Swamp", 16: "Dune", 17: "Bridge", 18: "CoastalSea", 19: "OpenSea",
                 20: "Beach", 21: "Cliff", 22: "NonNavigableRiver", 23: "LandRestriction", 24: "SeaRestriction", 25: "UnderBridge"}
SEA_LEVEL = 0.9   # map units. An estimate: the sea floor is at 0.0, the shore starts near 0.3, the lowest town stands at 1.65


def _cubic_weights(src, dst, lo, hi):
    """Catmull-Rom sample positions and weights to read dst samples covering [lo, hi] (in source sample units)."""
    x = np.linspace(lo, hi, dst)
    i = np.floor(x).astype(np.int64); t = x - i
    w = np.stack([((-t + 2) * t - 1) * t / 2, ((3 * t - 5) * t * t + 2) / 2, ((-3 * t + 4) * t + 1) * t / 2, (t - 1) * t * t / 2], axis=1)
    idx = np.clip(np.stack([i - 1, i, i + 1, i + 2], axis=1), 0, src - 1)
    return idx, w


def resample(a, x0, x1, y0, y1, nx, ny):
    """Bicubic (Catmull-Rom) read of the window [x0, x1] x [y0, y1], given in sample units of a (row = y), as (ny, nx)."""
    ix, wx = _cubic_weights(a.shape[1], nx, x0, x1)
    iy, wy = _cubic_weights(a.shape[0], ny, y0, y1)
    rows = (a[iy] * wy[:, :, None]).sum(axis=1)            # (ny, width)
    return (rows[:, ix] * wx[None, :, :]).sum(axis=2)      # (ny, nx)


def fbm(shape, seed, octaves=5, base=8, gain=0.5):
    """Smooth fractal noise in about [-1, 1]: octaves of random grids, each read bicubically. Same seed, same field."""
    rng = np.random.default_rng(seed); out = np.zeros(shape); amp = 1.0; total = 0.0; n = base
    for _ in range(octaves):
        g = rng.standard_normal((n + 3, n + 3)) * 0.5
        out += amp * resample(g, 1, n + 1, 1, n + 1, shape[1], shape[0])
        total += amp; amp *= gain; n *= 2
    return out / total


class WorldMap:
    def __init__(self, scene_dir=None):
        self.dir = scene_dir or os.path.join(config.GAME, "Modules", MAP_SCENE[0], "SceneObj", MAP_SCENE[1])
        self.info = terrain.scene_terrain_info(os.path.join(self.dir, "scene.xscene"))
        self.cell = self.info["node_size"] / 256.0
        self.size = (self.info["nx"] * self.info["node_size"], self.info["ny"] * self.info["node_size"])
        self._z = None; self._types = None

    @property
    def heights(self):
        """The map's height field in map units, (4097, 4097), row = y."""
        if self._z is None:
            self._z = terrain.Terrain(os.path.join(self.dir, "terrain.bin")).heightmap(self.info)
        return self._z

    def height_at(self, x, y):
        return float(resample(self.heights, x / self.cell, x / self.cell, y / self.cell, y / self.cell, 1, 1)[0, 0])

    def patch(self, center, size=12.0, samples=1537):
        """The square of `size` map units around `center`, read bicubically to samples x samples, in map units of height."""
        cx, cy = center; h = size / 2.0
        return resample(self.heights, (cx - h) / self.cell, (cx + h) / self.cell, (cy - h) / self.cell, (cy + h) / self.cell, samples, samples)

    def battlefield_heights(self, center, size=12.0, field=1200.0, samples=1537, vertical=None, detail=1.0, sea=SEA_LEVEL):
        """A first sketch of a battlefield's height field in metres for a map position: the patch scaled up to a square of
        `field` metres, heights scaled by `vertical` (default: 40 % of the horizontal factor, which keeps slopes fightable),
        plus generated detail below the map's resolution. Returns (heights, water) where water marks samples under the
        estimated sea level; heights are metres above it. The seed comes from the position, so the same place gives the same field."""
        z = self.patch(center, size, samples)
        k = field / size
        vertical = vertical if vertical is not None else 0.4 * k
        water = z < sea
        zm = (z - sea) * vertical
        seed = zlib.crc32(("%.2f,%.2f" % (round(center[0], 2), round(center[1], 2))).encode())
        cell = field / (samples - 1)
        gy, gx = np.gradient(zm, cell); slope = np.clip(np.hypot(gx, gy), 0, 1.5)
        rough = 0.6 + 4.0 * slope                           # flats stay calm, slopes get broken up
        zm = zm + detail * (rough * 2.2 * fbm(z.shape, seed, 6, 6) + 0.25 * fbm(z.shape, seed + 1, 4, 96))
        return zm, water

    def terrain_types(self, resolution=2048):
        """The campaign terrain type of every spot as a (resolution, resolution) uint8 grid (row = y), 0 where the map has
        no navmesh (deep sea, map border). Read from the map's navmesh: each face carries a TaleWorlds.Core.TerrainType."""
        if self._types is None or self._types.shape[0] != resolution:
            nm = navmesh.NavMesh(os.path.join(self.dir, "navmesh.bin"))
            g = np.zeros((resolution, resolution), np.uint8)
            sx = resolution / self.size[0]; sy = resolution / self.size[1]
            V = nm.vertices[:, :2] * np.array([sx, sy], np.float32)
            for (vs, _), group in zip(nm.faces, nm.face_group):
                p = V[list(vs)]
                x0 = max(0, int(np.floor(p[:, 0].min()))); x1 = min(resolution - 1, int(np.ceil(p[:, 0].max())))
                y0 = max(0, int(np.floor(p[:, 1].min()))); y1 = min(resolution - 1, int(np.ceil(p[:, 1].max())))
                if x1 < x0 or y1 < y0: continue
                xx, yy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
                inside = np.ones(xx.shape, bool)
                for i in range(len(vs)):
                    a = p[i]; b = p[(i + 1) % len(vs)]
                    inside &= ((b[0] - a[0]) * (yy - a[1]) - (b[1] - a[1]) * (xx - a[0])) >= -1e-4
                g[y0:y1 + 1, x0:x1 + 1][inside] = group
            self._types = g
        return self._types

    def terrain_type_at(self, x, y):
        g = self.terrain_types(); r = g.shape[0]
        return int(g[min(r - 1, max(0, int(y / self.size[1] * r))), min(r - 1, max(0, int(x / self.size[0] * r)))])

    def types_patch(self, center, size=12.0, samples=1537):
        """The campaign terrain type of the square of `size` map units around `center` as a (samples, samples) uint8
        array (row = y), read to the nearest cell of terrain_types()."""
        g = self.terrain_types(); r = g.shape[0]; cx, cy = center; h = size / 2.0
        xs = np.clip(((np.linspace(cx - h, cx + h, samples)) / self.size[0] * r).astype(int), 0, r - 1)
        ys = np.clip(((np.linspace(cy - h, cy + h, samples)) / self.size[1] * r).astype(int), 0, r - 1)
        return g[ys][:, xs]

    def terrain_mix(self, center, size=12.0):
        """{terrain type name: share} of the square of `size` map units around `center`."""
        g = self.terrain_types(); r = g.shape[0]; cx, cy = center; h = size / 2
        x0 = max(0, int((cx - h) / self.size[0] * r)); x1 = min(r, int((cx + h) / self.size[0] * r) + 1)
        y0 = max(0, int((cy - h) / self.size[1] * r)); y1 = min(r, int((cy + h) / self.size[1] * r) + 1)
        v, c = np.unique(g[y0:y1, x0:x1], return_counts=True)
        return {TERRAIN_TYPES.get(int(k), "none" if k == 0 else str(k)): round(float(n) / c.sum(), 3) for k, n in sorted(zip(v, c), key=lambda t: -t[1])}

    def settlements(self):
        """[(id, name, x, y)] from the SandBox settlements file."""
        path = os.path.join(config.GAME, "Modules", "SandBox", "ModuleData", "settlements.xml")
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f: s = f.read()
        out = []
        for m in re.finditer(r"<Settlement\b[^>]*>", s):
            a = dict(re.findall(r'(\w+)="([^"]*)"', m.group()))
            if "posX" in a and "posY" in a:
                out.append((a.get("id"), re.sub(r"^\{=[^}]*\}", "", a.get("name", "")), float(a["posX"]), float(a["posY"])))
        return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "patch":
        from PIL import Image
        wm = WorldMap(); x, y = float(a[1]), float(a[2]); size = float(a[4]) if len(a) > 4 else 12.0
        z, water = wm.battlefield_heights((x, y), size)
        Image.fromarray(terrain.hillshade(z, 1200.0 / (z.shape[0] - 1))).save(a[3] if len(a) > 3 else "patch.png")
        print("relief %.1f m, water %.1f %%" % (z.max() - z.min(), 100.0 * water.mean()))
    else:
        wm = WorldMap(); st = wm.settlements()
        print("map %s units, cell %.3f, %d settlements with a position" % (wm.size, wm.cell, len(st)))
        print("worldmap.py patch <x> <y> [out.png] [size in map units]")
