"""Fit a rock to the ground it stands on, so nothing of it hangs in the air.

Almost every shipped rock is an open shell: a face or a dome without a back or a bottom (sdk/meshread.py open_edges).
Where that open rim is above the ground, the player sees through the rock. The shipped battlefields hide rims behind
other rocks and behind terrain sculpted by hand; about a third of a rim still stands clear in them, out of the
player's usual sight. A generator cannot count on that, so here the ground is made to meet the rock:

    f = fit(z, cell, "rock_cliff_b", x, y, yaws, leans, sinks, scale)   the pose with the least rim in the air
    cover(z, cell, f["rim"])                                             earth banked up over what is left of it
    footprint(z, cell, f["body"])                                        the cells the rock stands on (for the navmesh)

A scene turns an entity by rotation_euler = (x, y, z) as  R = Rz . Rx . Ry  (measured: of the six orders this one buries
the rims of the shipped placements deepest), then scales, then moves.
"""
import math, os, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, meshread
else:
    from . import config, meshread

CACHE = os.path.join(config.SDK_HOME, "cache", "rockshapes")
_shapes = {}


def euler_matrix(r):
    """The rotation a scene applies for rotation_euler = r."""
    cx, sx, cy, sy, cz, sz = math.cos(r[0]), math.sin(r[0]), math.cos(r[1]), math.sin(r[1]), math.cos(r[2]), math.sin(r[2])
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]); Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]); Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Rx @ Ry


def register(name, positions, triangles, step=1.0):
    """Makes a custom model known by its geometry (positions (n, 3), triangles (k, 3) of position indices), so fit, cover
    and footprint work for it as for a shipped rock. Equal positions are welded before the open edges are looked for."""
    P, inv = np.unique(np.round(np.asarray(positions, np.float64), 3), axis=0, return_inverse=True)
    F = inv.reshape(-1)[np.asarray(triangles, np.int64)]; F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    pts = []
    for a, b in meshread.open_edges(P, F):
        n = max(1, int(np.linalg.norm(P[b] - P[a]) / step))
        for t in (np.arange(n) + 0.5) / n: pts.append(P[a] * (1 - t) + P[b] * t)
    _shapes[name] = {"rim": np.array(pts, np.float64).reshape(-1, 3), "body": P, "tris": F}
    return _shapes[name]


def top_surface(shape, cell, W, T):
    """The height of a placed model's upper surface over the terrain grid: (j0, i0, top) where top[j - j0, i - i0] is the
    highest point of the model above cell (j, i), -1e9 where the model is not above the cell. W: world vertices, T: triangles."""
    lo = W.min(0); hi = W.max(0)
    j0 = max(0, int(lo[1] / cell)); j1 = min(shape[0], int(hi[1] / cell) + 2); i0 = max(0, int(lo[0] / cell)); i1 = min(shape[1], int(hi[0] / cell) + 2)
    top = np.full((j1 - j0, i1 - i0), -1e9)
    a, b, c = W[T[:, 0]], W[T[:, 1]], W[T[:, 2]]
    for k in range(len(T)):                                   # rasterise each triangle's height over the cells it covers
        xs = (a[k, 0], b[k, 0], c[k, 0]); ys = (a[k, 1], b[k, 1], c[k, 1])
        ia = max(i0, int(np.ceil(min(xs) / cell))); ib = min(i1 - 1, int(max(xs) / cell)); ja = max(j0, int(np.ceil(min(ys) / cell))); jb = min(j1 - 1, int(max(ys) / cell))
        if ia > ib or ja > jb: continue
        X, Y = np.meshgrid(np.arange(ia, ib + 1) * cell, np.arange(ja, jb + 1) * cell)
        d = (b[k, 1] - c[k, 1]) * (a[k, 0] - c[k, 0]) + (c[k, 0] - b[k, 0]) * (a[k, 1] - c[k, 1])
        if abs(d) < 1e-9: continue
        w0 = ((b[k, 1] - c[k, 1]) * (X - c[k, 0]) + (c[k, 0] - b[k, 0]) * (Y - c[k, 1])) / d
        w1 = ((c[k, 1] - a[k, 1]) * (X - c[k, 0]) + (a[k, 0] - c[k, 0]) * (Y - c[k, 1])) / d
        w2 = 1 - w0 - w1; inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        zz = np.where(inside, w0 * a[k, 2] + w1 * b[k, 2] + w2 * c[k, 2], -1e9)
        sub = top[ja - j0:jb + 1 - j0, ia - i0:ib + 1 - i0]; np.maximum(sub, zz, out=sub)
    return j0, i0, top


def support(z, cell, name, pos, rot, scale=1.0, under=0.35, keep=None, inset=2):
    """Raises the ground (z, in place) under a placed model to just below its upper surface, so the terrain itself is
    what agents collide with and stand on: the way to give a custom model a body without a collision shape. Works for
    anything without overhangs to walk under (rocks, mounds, wrecks); the model hides the raised ground.
    inset: the raised ground stays this many cells inside the model's outline and never stands higher than the lowest
    surface near it, so no steps of terrain show under a lip or beside a wall.
    Needs the model's triangles (register). Returns the cells changed."""
    s = _shapes[name]; W = place(s["body"], pos, rot, scale); T = s["tris"]
    j0, i0, top = top_surface(z.shape, cell, W, T)
    j1, i1 = j0 + top.shape[0], i0 + top.shape[1]
    if inset > 0:                                              # a minimum filter: each cell takes the lowest value around it
        pad = np.pad(top, inset, mode="constant", constant_values=-1e9); low = top.copy(); h, w = top.shape
        for dj in range(-inset, inset + 1):
            for di in range(-inset, inset + 1):
                if dj * dj + di * di <= inset * inset + 1: np.minimum(low, pad[inset + dj:inset + dj + h, inset + di:inset + di + w], out=low)
        top = low
    old = z[j0:j1, i0:i1]; new = np.where(top > -1e8, np.maximum(old, top - under), old)
    if keep is not None: new = np.where(keep[j0:j1, i0:i1], old, new)
    changed = int((new > old + 1e-6).sum()); z[j0:j1, i0:i1] = new
    return changed


def rock_shape(name, step=1.0):
    """{"rim": points every `step` metres along the open edges (m, 3), "body": the mesh's vertices (n, 3), "tris": its
    triangles as vertex indices (k, 3)} of a prefab whose metamesh has the same name (true of the shipped rocks). Kept in
    memory and under <SDK home>/cache."""
    if name in _shapes: return _shapes[name]
    f = os.path.join(CACHE, name + ".v2.npz")
    if os.path.isfile(f):
        with np.load(f) as d: s = {"rim": d["rim"], "body": d["body"], "tris": d["tris"]}
    else:
        P, F = meshread.shape(name); pts = []
        for a, b in meshread.open_edges(P, F):
            n = max(1, int(np.linalg.norm(P[b] - P[a]) / step))
            for t in (np.arange(n) + 0.5) / n: pts.append(P[a] * (1 - t) + P[b] * t)
        s = {"rim": np.array(pts, np.float64).reshape(-1, 3), "body": P, "tris": F}
        os.makedirs(CACHE, exist_ok=True); np.savez_compressed(f, **s)
    _shapes[name] = s
    return s


def ground(z, cell, x, y):
    """Terrain height at world x, y (arrays), bilinear."""
    fx = np.clip(np.asarray(x) / cell, 0, z.shape[1] - 1.001); fy = np.clip(np.asarray(y) / cell, 0, z.shape[0] - 1.001)
    i = fx.astype(int); j = fy.astype(int); u = fx - i; v = fy - j
    return z[j, i] * (1 - u) * (1 - v) + z[j, i + 1] * u * (1 - v) + z[j + 1, i] * (1 - u) * v + z[j + 1, i + 1] * u * v


def place(points, pos, rot, scale):
    """Local points of a prefab as world points for an entity at pos, rotation_euler rot, scale (number or triple)."""
    return (points * np.asarray(scale, np.float64)) @ euler_matrix(rot).T + np.asarray(pos, np.float64)


def fit(z, cell, name, x, y, yaws, leans=(0.0,), sinks=(0.0,), scale=1.0, rolls=(0.0,), show=0.3, max_fill=6.0):
    """Tries every pose and returns the one whose open rim needs the least earth to cover, among those that still show
    at least `show` of the rock above the ground: {"pos", "rot", "scale", "rim" world points, "body" world points,
    "fill" mean height of rim above ground, "high" its 95th percentile, "shown"}. A pose whose underside (the lowest
    eighth of the mesh) is not in the ground is never taken. None if no pose stands, or even the best one leaves the rim
    more than max_fill metres up (the ground here is the wrong shape for this rock)."""
    s = rock_shape(name); g0 = float(ground(z, cell, x, y)); best = None
    foot = s["body"][:, 2] <= np.percentile(s["body"][:, 2], 12)            # the underside: it has to be in the ground, or the rock floats
    for yaw in yaws:
        for lean in leans:
            for roll in rolls:
                M = euler_matrix((lean, roll, yaw)).T
                rim = (s["rim"] * scale) @ M; body = (s["body"] * scale) @ M
                gr = ground(z, cell, rim[:, 0] + x, rim[:, 1] + y) if len(rim) else np.zeros(0); gb = ground(z, cell, body[:, 0] + x, body[:, 1] + y)
                for sink in sinks:
                    h = rim[:, 2] + g0 + sink - gr; up = np.maximum(h, 0.0)
                    hb = body[:, 2] + g0 + sink - gb
                    shown = float((hb > 0.3).mean())
                    if shown < show or np.percentile(hb[foot], 90) > 0.0: continue
                    fill = float(up.mean()) if len(up) else 0.0; high = float(np.percentile(h, 95)) if len(h) else 0.0
                    cost = fill + 0.35 * max(high, 0.0) - 1.5 * min(shown, 0.6)
                    if best is None or cost < best["cost"]:
                        best = {"cost": cost, "pos": (x, y, g0 + sink), "rot": (lean, roll, yaw), "scale": scale, "fill": fill, "high": high, "shown": shown}
    if best is None or best["high"] > max_fill: return None
    best["rim"] = place(s["rim"], best["pos"], best["rot"], scale); best["body"] = place(s["body"], best["pos"], best["rot"], scale); best["tris"] = s["tris"]
    return best


def cover(z, cell, rim, margin=0.35, talus=0.75, keep=None, body=None, tris=None, top=None):
    """Banks earth over a rim: raises z (in place) so every rim point is `margin` under the ground, the new earth falling
    away at slope `talus` from each point. keep: a mask of cells that must not change (water). Returns the volume added.

    body, tris (world vertices and triangles of the rock): where the rock itself is under a cell, the earth is not raised
    more than `margin` above the rock's upper surface. Without that the bank over a high rim buries the lower rock next
    to it under metres of earth, and the rock is solid under there: an agent walking on the rock walks on under the
    ground (seen in play: a rider 5 m below the surface, looking out through the terrain)."""
    if not len(rim): return 0.0
    h = rim[:, 2] + margin - ground(z, cell, rim[:, 0], rim[:, 1]); pts = rim[h > 0]
    if not len(pts): return 0.0
    reach = float((h.max()) / talus) + 2 * cell
    j0 = max(0, int((pts[:, 1].min() - reach) / cell)); j1 = min(z.shape[0], int((pts[:, 1].max() + reach) / cell) + 2)
    i0 = max(0, int((pts[:, 0].min() - reach) / cell)); i1 = min(z.shape[1], int((pts[:, 0].max() + reach) / cell) + 2)
    Y, X = np.mgrid[j0:j1, i0:i1] * cell; need = np.full(X.shape, -1e9)
    for k in range(0, len(pts), 64):
        p = pts[k:k + 64]
        d = np.sqrt((X[None] - p[:, 0, None, None]) ** 2 + (Y[None] - p[:, 1, None, None]) ** 2)
        need = np.maximum(need, (p[:, 2, None, None] + margin - talus * np.maximum(d - cell, 0.0)).max(0))
    old = z[j0:j1, i0:i1]; new = np.maximum(old, need)
    if top is None and body is not None and tris is not None: top = top_surface(z.shape, cell, body, tris)
    if top is not None:          # (j0, i0, array) as top_surface gives it, or a whole map of the highest rock surface per cell
        if isinstance(top, tuple):
            tj, ti, t = top; full = np.full(z.shape, -1e9); full[tj:tj + t.shape[0], ti:ti + t.shape[1]] = t; top = full
        t = top[j0:j1, i0:i1]
        new = np.where(t > -1e8, np.minimum(new, np.maximum(old, t + margin)), new)
    if keep is not None: new = np.where(keep[j0:j1, i0:i1], old, new)
    added = float((new - old).sum() * cell * cell); z[j0:j1, i0:i1] = new
    return added


def footprint(z, cell, body, clear=0.6, mask=None):
    """Marks (in a bool mask shaped like z, made if not given) the cells where the rock stands more than `clear` metres
    out of the ground: where nothing can walk."""
    if mask is None: mask = np.zeros(z.shape, bool)
    up = body[body[:, 2] - ground(z, cell, body[:, 0], body[:, 1]) > clear]
    if not len(up): return mask
    j = np.clip(np.rint(up[:, 1] / cell).astype(int), 1, z.shape[0] - 2); i = np.clip(np.rint(up[:, 0] / cell).astype(int), 1, z.shape[1] - 2)
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1): mask[j + dj, i + di] = True
    return mask


def exposure(z, cell, name, pos, rot, scale=1.0):
    """How a placed rock meets the ground: (share of its rim more than 0.3 m in the air, 95th percentile of rim height
    above ground, share of the body above ground)."""
    s = rock_shape(name); rim = place(s["rim"], pos, rot, scale); body = place(s["body"], pos, rot, scale)
    h = rim[:, 2] - ground(z, cell, rim[:, 0], rim[:, 1]) if len(rim) else np.zeros(1)
    return float((h > 0.3).mean()), float(np.percentile(h, 95)), float((body[:, 2] - ground(z, cell, body[:, 0], body[:, 1]) > 0.3).mean())
