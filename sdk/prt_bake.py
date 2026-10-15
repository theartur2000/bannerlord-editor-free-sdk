"""Bake light probes for prt_data.bin offline: the editor's probe baker (rglPRT_editor_tools.cpp) on a numpy ray caster.

    python sdk/prt_bake.py compare <scene> [--probes N] [--rays R] [--albedo A] [--entry K]
                    bake a random sample of the scene's shipped probes at their shipped positions, compare with the file
    python sdk/prt_bake.py bake <scene> --bounds x0,y0,z0,x1,y1,z1 [--spacing h,v] [--rays R] [--albedo A] [--out FILE]
                    a new prt_data.bin: every cell of the box a probe at its centre, sky and bounce baked

The editor's baker (Modding Kit editor build, names in bl2_re JOURNAL "2026-10-14 prt_data.bin"):
  1. Probes: a grid between the scene's two prt_border_probe entities (spacing from the scene, 1.5 / 2.5 m in the arenas);
     probes are spawned from the navmesh, moved to agent height, switched off where they see no faces
     (activate_probes_with_visible_faces_from_nav_mesh), and the ones that need a depth test are picked by casting rays
     ("Fetching visibility needed probes"). That edit data (prt_edit_data.bin) is kept on TaleWorlds' file server and
     not shipped; the finished positions and masks are in prt_data.bin.
  2. Ray scene (prt_build_ray_scene, Embree 3): the render meshes of the visible static entities (filter: scene_entities),
     the terrain, and "prt_grass_proxy" meshes for grass.
  3. Per probe 12 jobs (prt_probe_calculate_sh_coefs): for each of the six directions the sky and sun bounce
     (bake_probes below: 1800 rays, 7200 when the scene asks for high quality) and the point light bounce (the same
     paths, at each hit one shadow ray to a point light within 30 m picked with weight 1 - d^2 / 900).
  4. Coefficients cut to 1/10000 steps (prt_quantize_sh_coefs), then written (sdk/prt.py).
This port: scene_entities resolves prefabs and transforms from scene.xscene and Modules/*/Prefabs, scene_geometry takes
the meshes' highest level of detail (sdk/meshread.py) and the terrain (sdk/terrain.py), RayScene is a uniform-grid ray
caster in numpy (two-sided, nearest hit), bake_probes is the editor's path tracer with a constant albedo.

Measured against shipped probes (compare: positions from the file, Monte Carlo, so not byte-identical by nature):
  arena_aserai_a, 60 random probes x 6 directions x 256 rays (the editor: 1800), albedo 0.5:
    sky visibility  mean abs error 0.033, correlation 0.966 (per direction 0.77 to 0.93); shipped mean 0.309, baked 0.324
    bounce SH       correlation 0.937 / 0.915 / 0.942 / 0.929 (Y00, Y1-1, Y10, Y11, grey); shipped = 0.51 x baked, so the
                    scene's effective albedo is about 0.25; after that scale the mean relative error is 0.14 for Y00 and
                    Y10 and 0.41 / 0.43 for the small horizontal terms Y1-1 / Y11 (noise of 256 rays)
  A current-format (PRT4) scene was not measured: the NavalDLC scenes have 9 to 12 million triangles and a 40-probe
  sample did not finish in hours with this caster.
The baked sky is slightly higher than shipped: grass proxies, flora and alpha-tested leaves are not in the ray scene.

Not done: probe placement from the navmesh, the visibility probe choice, point lights, the hit texture and vertex
colour (albedo is one constant; the shipped bounce is linear in it to first order, so the scale column of compare is
an effective albedo), interpolated vertex normals (face normals here), grass proxies and flora, mesh_bender entities.
The numpy caster traces a few hundred paths a second on one core in a scene of a million triangles (arena_aserai_a: 92,160
paths in 376 s), slower in the 10-million-triangle NavalDLC scenes: a full bake of a town (tens of thousands of probes x 6
x 1800 paths) is days of CPU; compare samples.
"""
import glob, math, os, re, sys, time
import xml.etree.ElementTree as ET

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, meshread, prt, terrain
else:
    from . import config, meshread, prt, terrain

RAYS = 1800                  # rays per probe and basis direction (7200 when the scene asks for the high quality bake)
MAX_DISTANCE = 800.0         # ray length
BOUNCES = 10                 # path length limit; Russian roulette from the 7th segment on
OFFSET = 0.001               # every ray starts this far along its direction
SKIP_NAMES = ("horizon", "skybox", "compass", "editor_map_border")
SKIP_SCRIPTS = ("CastleGate", "DestructableComponent")
SKIP_TAGS = ("do_not_affect_prt", "broken_child")
MODULE_ORDER = ("Native", "SandBoxCore", "SandBox", "CustomBattle", "StoryMode", "Multiplayer", "NavalDLC")


# ------------------------------------------------------------------------------------------------------------ the scene as triangles
_prefabs = None


def prefab_index():
    """{prefab name: <game_entity> element} of the top-level entities of every Modules/*/Prefabs/*.xml. The official
    modules win a duplicate name (a later one in MODULE_ORDER over an earlier one); other modules only add names."""
    global _prefabs
    if _prefabs is not None: return _prefabs
    mods = sorted(os.listdir(config.MODULES), key=lambda m: (MODULE_ORDER.index(m) if m in MODULE_ORDER else -1, m))
    out = {}
    for m in mods:
        for p in sorted(glob.glob(os.path.join(config.MODULES, m, "Prefabs", "*.xml"))):
            try: root = ET.parse(p).getroot()
            except ET.ParseError: continue
            for e in root.findall("game_entity"):
                if e.get("name"): out[e.get("name")] = e
    _prefabs = out
    return out


def euler_matrix(r):
    """rotation_euler (x, y, z) as the scenes apply it: R = Rz . Rx . Ry (sdk/rockfit.py)."""
    cx, sx, cy, sy, cz, sz = math.cos(r[0]), math.sin(r[0]), math.cos(r[1]), math.sin(r[1]), math.cos(r[2]), math.sin(r[2])
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]]); Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]); Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Rx @ Ry


def _vec(s, n=3, d=0.0):
    if not s: return (d,) * n
    v = [float(x) for x in s.split(",")]
    return tuple(v + [d] * (n - len(v)))[:n]


def _local(el):
    t = el.find("transform"); M = np.eye(4)
    if t is not None:
        M[:3, :3] = euler_matrix(_vec(t.get("rotation_euler"))) @ np.diag(_vec(t.get("scale"), d=1.0))
        M[:3, 3] = _vec(t.get("position"))
    return M


def _affects_prt(el, prefab):
    """The editor's filter (prt_entity_affects_prt, editor 1809f14e0) as far as the XML shows it: not tagged
    do_not_affect_prt / broken_child, name without horizon / skybox / compass / editor_map_border, no CastleGate or
    DestructableComponent script, static (mobility not 1) or tagged solid_child."""
    tags = set()
    for src in (prefab, el):
        if src is None: continue
        t = src.find("tags")
        if t is not None: tags |= {x.get("name") for x in t.findall("tag")}
    if tags & set(SKIP_TAGS): return False
    name = (el.get("name") or "") + " " + (el.get("prefab") or "")
    if any(s in name for s in SKIP_NAMES): return False
    for src in (prefab, el):
        if src is None: continue
        sc = src.find("scripts")
        if sc is not None and any(any(k in (s.get("name") or "") for k in SKIP_SCRIPTS) for s in sc.findall("script")): return False
    mob = el.get("mobility") or (prefab.get("mobility") if prefab is not None else None)
    return mob != "1" or "solid_child" in tags


def _levels_ok(el, active):
    lv = el.find("levels")
    if lv is None or active is None: return True
    names = {x.get("name") for x in lv.findall("level")}
    return bool(names & active)


def scene_entities(folder, levels=None):
    """[(metamesh name, 4 x 4 world matrix)] of every mesh the probe baker would ray against. levels: the set of active
    upgrade level names (an entry name split at its spaces; "base" is always on); None takes every entity."""
    root = ET.parse(os.path.join(folder, "scene.xscene")).getroot()
    pre = prefab_index(); out = []
    active = None if levels is None else set(levels) | {"base"}

    def walk(el, parent, depth):
        if depth > 16 or not _levels_ok(el, active): return
        prefab = pre.get(el.get("prefab")) if el.get("prefab") else None
        if not _affects_prt(el, prefab): return
        M = parent @ _local(el)
        for src in ([prefab] if prefab is not None else []) + [el]:
            comps = src.find("components")
            for c in (comps.findall("meta_mesh_component") if comps is not None else []):
                if c.get("name"): out.append((c.get("name"), M))
            ch = src.find("children")
            for c in (ch.findall("game_entity") if ch is not None else []): walk(c, M, depth + 1)
    ents = root.find("entities")
    for el in (ents.findall("game_entity") if ents is not None else []): walk(el, np.eye(4), 0)
    return out


_meshes = {}


def mesh_triangles(name):
    """(positions (n, 3), triangles (k, 3)) of a metamesh's highest level of detail, submeshes joined; None if unknown."""
    if name not in _meshes:
        try:
            parts = meshread.read(name)
        except KeyError:
            _meshes[name] = None; return None
        P = []; F = []; base = 0
        for sub, p, t in parts:
            P.append(p); F.append(t + base); base += len(p)
        _meshes[name] = (np.concatenate(P), np.concatenate(F)) if P else None
    return _meshes[name]


def terrain_triangles(folder, lo=None, hi=None, step=1, hole=None):
    """The terrain as triangles (two per cell of every step-th height sample), cut to the box lo..hi in x and y; hole =
    (lo, hi): leave out the cells whose centre is inside that box (where a finer copy is used)."""
    xs = os.path.join(folder, "scene.xscene"); tb = os.path.join(folder, "terrain.bin")
    with open(xs, "rb") as f: text = f.read()
    if not os.path.isfile(tb) or b'<terrain enabled="false"' in text or b"<terrain " not in text: return None
    info = terrain.scene_terrain_info(xs)
    z = _heightmap(tb, info); cell = info["node_size"] * info["nx"] / (z.shape[1] - 1)
    j0, i0, j1, i1 = 0, 0, z.shape[0] - 1, z.shape[1] - 1
    if lo is not None:
        i0 = max(0, int(lo[0] / cell)); j0 = max(0, int(lo[1] / cell)); i1 = min(z.shape[1] - 1, int(math.ceil(hi[0] / cell))); j1 = min(z.shape[0] - 1, int(math.ceil(hi[1] / cell)))
    js = np.unique(np.r_[np.arange(j0, j1 + 1, step), j1]); is_ = np.unique(np.r_[np.arange(i0, i1 + 1, step), i1])
    if len(js) < 2 or len(is_) < 2: return None
    X, Y = np.meshgrid(is_ * cell, js * cell); Z = z[np.ix_(js, is_)]
    P = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1); w = len(is_)
    a = (np.arange(len(js) - 1)[:, None] * w + np.arange(w - 1)[None, :]).ravel()
    if hole is not None:
        cx = (X[:-1, :-1] + X[1:, 1:]).ravel() / 2; cy = (Y[:-1, :-1] + Y[1:, 1:]).ravel() / 2
        a = a[~((cx > hole[0][0]) & (cx < hole[1][0]) & (cy > hole[0][1]) & (cy < hole[1][1]))]
    F = np.concatenate([np.stack([a, a + 1, a + w + 1], 1), np.stack([a, a + w + 1, a + w], 1)])
    return P, F


_hm = {}


def _heightmap(tb, info):
    if tb not in _hm: _hm.clear(); _hm[tb] = terrain.Terrain(tb).heightmap(info)
    return _hm[tb]


def scene_geometry(folder, levels=None, box=None, margin=20.0, far_step=8, log=None):
    """All triangles the baker rays against, in world space: (V (n, 3), F (k, 3)). The terrain at full resolution
    within box (lo, hi) grown by margin in x and y, every far_step-th sample elsewhere."""
    V = []; F = []; base = 0; missing = set()
    for name, M in scene_entities(folder, levels):
        m = mesh_triangles(name)
        if m is None: missing.add(name); continue
        V.append(m[0] @ M[:3, :3].T + M[:3, 3]); F.append(m[1] + base); base += len(m[0])
    if box is None: parts = [terrain_triangles(folder)]
    else:
        lo = np.asarray(box[0], float) - margin; hi = np.asarray(box[1], float) + margin
        parts = [terrain_triangles(folder, lo, hi), terrain_triangles(folder, step=far_step, hole=(lo, hi))]
    for t in parts:
        if t is None: continue
        V.append(t[0]); F.append(t[1] + base); base += len(t[0])
    if log and missing: log("meshes not found: %d (%s)" % (len(missing), ", ".join(sorted(missing)[:8])))
    if not V: return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
    return np.concatenate(V), np.concatenate(F)


# --------------------------------------------------------------------------------------------------------------------- the ray caster
class Grid:
    """Triangles binned into a uniform grid; cast() walks every ray through the cells it crosses (3-D DDA) and tests the
    triangles of each cell, all rays of a batch in step. Two-sided, nearest hit, like the editor's Embree scene.
    bounds = (lo, hi) limits the grid to a box (triangles are binned where they overlap it); ids picks the triangles."""

    PAIRS = 4_000_000            # ray-triangle tests per numpy step at most

    def __init__(self, a, e1, e2, ids=None, bounds=None, per_cell=1, max_dim=400):
        self.a, self.e1, self.e2 = a, e1, e2
        ids = np.arange(len(a)) if ids is None else np.asarray(ids, np.int64); self.n = len(ids)
        b = a[ids] + e1[ids]; c = a[ids] + e2[ids]
        tlo = np.minimum(np.minimum(a[ids], b), c); thi = np.maximum(np.maximum(a[ids], b), c)
        if bounds is None: self.lo = tlo.min(0) - 1e-3 if len(ids) else np.zeros(3); self.hi = thi.max(0) + 1e-3 if len(ids) else np.ones(3)
        else: self.lo = np.asarray(bounds[0], np.float64); self.hi = np.asarray(bounds[1], np.float64)
        ext = self.hi - self.lo
        cell = (np.prod(ext) / max(1.0, self.n / per_cell)) ** (1.0 / 3.0)
        dims = np.clip(np.ceil(ext / cell), 1, max_dim).astype(np.int64)
        self.dims = dims; self.cell = ext / dims
        inside = np.all(thi >= self.lo, 1) & np.all(tlo <= self.hi, 1)
        ids, tlo, thi = ids[inside], tlo[inside], thi[inside]
        i0 = np.clip(((tlo - self.lo) / self.cell).astype(np.int64), 0, dims - 1)
        i1 = np.clip(((thi - self.lo) / self.cell).astype(np.int64), 0, dims - 1)
        span = i1 - i0 + 1; cnt = span.prod(1)
        t = np.repeat(np.arange(len(ids)), cnt); k = np.arange(cnt.sum()) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        sx = span[t, 0]; sy = span[t, 1]
        ix = i0[t, 0] + k % sx; iy = i0[t, 1] + (k // sx) % sy; iz = i0[t, 2] + k // (sx * sy)
        cid = (iz * dims[1] + iy) * dims[0] + ix
        o = np.argsort(cid, kind="stable"); self.tris = ids[t[o]]
        self.start = np.searchsorted(cid[o], np.arange(dims.prod() + 1))

    def cast(self, O, D, tmin=None, tmax=MAX_DISTANCE):
        """Nearest hit of rays O + t D, tmin < t <= tmax (tmin per ray, default 0). -> (hit bool, t, triangle, u, v)
        arrays; t is inf and the triangle -1 where nothing is hit."""
        O = np.asarray(O, np.float64); D = np.asarray(D, np.float64); R = len(O)
        hit_t = np.full(R, np.inf); hit_tri = np.full(R, -1, np.int64); hu = np.zeros(R); hv = np.zeros(R)
        if self.n == 0 or R == 0: return hit_t < 0, hit_t, hit_tri, hu, hv
        tlow = np.zeros(R) if tmin is None else np.asarray(tmin, np.float64)
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / D
            t0 = (self.lo - O) * inv; t1 = (self.hi - O) * inv
        t0 = np.where(np.isnan(t0), -np.inf, t0); t1 = np.where(np.isnan(t1), np.inf, t1)
        tn = np.maximum(np.minimum(t0, t1).max(1), tlow); tf = np.minimum(np.maximum(t0, t1).min(1), np.broadcast_to(tmax, (R,)))
        alive = np.nonzero(tn <= tf)[0]
        p = O[alive] + D[alive] * tn[alive, None]
        idx = np.clip(((p - self.lo) / self.cell).astype(np.int64), 0, self.dims - 1)
        d = D[alive]; step = np.where(d > 0, 1, -1)
        nxt = self.lo + (idx + (d > 0)) * self.cell
        with np.errstate(divide="ignore", invalid="ignore"):
            tmx = np.where(d != 0, (nxt - O[alive]) / d, np.inf)
            tdl = np.where(d != 0, self.cell / np.abs(d), np.inf)
        tlim = tf[alive]; tlo_ = tlow[alive]
        O32 = O.astype(np.float32); D32 = D.astype(np.float32)
        while len(alive):
            cid = (idx[:, 2] * self.dims[1] + idx[:, 1]) * self.dims[0] + idx[:, 0]
            s = self.start[cid]; cnt = self.start[cid + 1] - s
            texit = np.minimum(tmx.min(1), tlim)
            csum = np.cumsum(cnt); total = int(csum[-1]) if len(csum) else 0
            q0 = 0
            while q0 < total:                                      # bounded memory: slices of the (ray, triangle) pairs
                q1 = min(total, q0 + self.PAIRS)
                k = np.arange(q0, q1); ray = np.searchsorted(csum, k, side="right")
                tri = self.tris[s[ray] + k - (csum[ray] - cnt[ray])]; g = alive[ray]
                t, u, v, ok = _intersect(O32[g], D32[g], self.a[tri], self.e1[tri], self.e2[tri])
                ok &= (t > tlo_[ray]) & (t <= texit[ray]) & (t < hit_t[g])
                if ok.any():
                    sel = np.nonzero(ok)[0]; gi = g[sel]; ti = t[sel]
                    order = np.lexsort((ti, gi)); sel = sel[order]; gi = gi[order]
                    first = np.r_[True, gi[1:] != gi[:-1]]; sel = sel[first]; gi = gi[first]
                    better = t[sel] < hit_t[gi]; sel = sel[better]; gi = gi[better]
                    hit_t[gi] = t[sel]; hit_tri[gi] = tri[sel]; hu[gi] = u[sel]; hv[gi] = v[sel]
                q0 = q1
            done = np.isfinite(hit_t[alive])
            ax = tmx.argmin(1); r = np.arange(len(alive))
            idx[r, ax] += step[r, ax]; tnew = tmx[r, ax]; tmx[r, ax] += tdl[r, ax]
            keep = ~(done | (tnew > tlim) | np.any(idx < 0, 1) | np.any(idx >= self.dims, 1))
            alive, idx, step, tmx, tdl, tlim, tlo_ = alive[keep], idx[keep], step[keep], tmx[keep], tdl[keep], tlim[keep], tlo_[keep]
        return np.isfinite(hit_t), hit_t, hit_tri, hu, hv


class RayScene:
    """A fine grid over the probes' box and a coarse grid for the triangles not wholly inside it; a ray takes the nearer
    hit of the two. Triangle numbers in the results index V, F as given."""

    def __init__(self, V, F, box=None):
        V = np.asarray(V, np.float64); F = np.asarray(F, np.int64)
        a = V[F[:, 0]]; e1 = V[F[:, 1]] - a; e2 = V[F[:, 2]] - a
        good = np.linalg.norm(np.cross(e1, e2), axis=1) > 1e-12
        self.F = F; self.a = a.astype(np.float32); self.e1 = np.where(good[:, None], e1, 0.0).astype(np.float32); self.e2 = np.where(good[:, None], e2, 0.0).astype(np.float32)
        ids = np.nonzero(good)[0]
        if box is None:
            self.near = Grid(self.a, self.e1, self.e2, ids); self.far = None; return
        lo = np.asarray(box[0], np.float64); hi = np.asarray(box[1], np.float64)
        b = a[ids] + e1[ids]; c = a[ids] + e2[ids]
        tlo = np.minimum(np.minimum(a[ids], b), c); thi = np.maximum(np.maximum(a[ids], b), c)
        inner = np.all(tlo >= lo, 1) & np.all(thi <= hi, 1)
        self.near = Grid(self.a, self.e1, self.e2, ids, bounds=(lo, hi))
        outer = ids[~inner]
        self.far = Grid(self.a, self.e1, self.e2, outer, per_cell=16, max_dim=128) if len(outer) else None
        self.box = (lo, hi)

    def cast(self, O, D, tmax=MAX_DISTANCE):
        hit, t, tri, u, v = self.near.cast(O, D, tmax=tmax)
        if self.far is None: return hit, t, tri, u, v
        # the far grid holds every triangle not wholly inside the box: it can only be nearer for a ray that starts
        # outside the box or leaves it, so it is asked for everything up to the near hit
        h2, t2, tr2, u2, v2 = self.far.cast(O, D, tmax=np.minimum(t, tmax))
        nearer = h2 & (t2 < t)
        hit = hit | h2; t = np.where(nearer, t2, t); tri = np.where(nearer, tr2, tri); u = np.where(nearer, u2, u); v = np.where(nearer, v2, v)
        return hit, t, tri, u, v


def _intersect(O, D, a, e1, e2):
    """Moller-Trumbore, two-sided, on (n, 3) arrays written out per component (np.cross is slow on many short rows)."""
    dx, dy, dz = D[:, 0], D[:, 1], D[:, 2]; ex, ey, ez = e1[:, 0], e1[:, 1], e1[:, 2]; fx, fy, fz = e2[:, 0], e2[:, 1], e2[:, 2]
    px = dy * fz - dz * fy; py = dz * fx - dx * fz; pz = dx * fy - dy * fx
    det = ex * px + ey * py + ez * pz
    ok = np.abs(det) > 1e-12; inv = 1.0 / np.where(ok, det, 1.0)
    sx = O[:, 0] - a[:, 0]; sy = O[:, 1] - a[:, 1]; sz = O[:, 2] - a[:, 2]
    u = (sx * px + sy * py + sz * pz) * inv
    qx = sy * ez - sz * ey; qy = sz * ex - sx * ez; qz = sx * ey - sy * ex
    v = (dx * qx + dy * qy + dz * qz) * inv
    t = (fx * qx + fy * qy + fz * qz) * inv
    ok &= (u >= 0) & (v >= 0) & (u + v <= 1)
    return t, u, v, ok


# ---------------------------------------------------------------------------------------------------------------------------- the bake
def hemisphere(n, normal, rng):
    """n directions uniform on the sphere (z = 2u - 1, angle 2 pi v, as the editor draws them), turned into the
    hemisphere around normal (n, 3) or (3,) by negating those below it."""
    z = rng.random(n) * 2.0 - 1.0; phi = rng.random(n) * (2.0 * math.pi); r = np.sqrt(1.0 - z * z)
    d = np.stack([r * np.cos(phi), r * np.sin(phi), z], 1)
    c = np.einsum("ij,ij->i", d, np.broadcast_to(normal, d.shape))
    return np.where((c <= 0)[:, None], -d, d), np.abs(c)


def sky_visibility(grid, positions, rays=256, seed=1, batch=200000):
    """Per probe and basis direction (DIRS order) the editor's sky term: 2 / N * sum over N rays of the hemisphere of
    cos(ray, direction) for the rays that leave the scene (no hit within 800 m). -> (n, 6) float32."""
    rng = np.random.default_rng(seed); P = np.asarray(positions, np.float64); n = len(P)
    out = np.zeros((n, 6), np.float64)
    per = max(1, batch // (6 * rays))
    for s in range(0, n, per):
        pp = P[s:s + per]; m = len(pp)
        D = []; C = []
        for k in range(m):
            for d in range(6):
                dd, cc = hemisphere(rays, prt.DIR_VECTORS[d], rng); D.append(dd); C.append(cc)
        D = np.concatenate(D); C = np.concatenate(C); O = np.repeat(pp, 6 * rays, axis=0) + D * OFFSET
        hit = grid.cast(O, D)[0]
        out[s:s + m] = (np.where(hit, 0.0, C).reshape(m, 6, rays).sum(2)) * (2.0 / rays)
    return out.astype(np.float32)


def bake_probes(scene, positions, rays=256, albedo=0.5, seed=1, batch=150000, log=None):
    """The editor's per-probe bake (prt_trace_sky_and_sun_bounce, editor 1807c7d60) for every probe and basis direction:
    rays uniform over the direction's hemisphere; a ray that leaves the scene at once adds its cosine to the sky term; one
    that hits bounces on (uniform hemisphere around the surface normal turned towards the ray, throughput *= 2 cos albedo,
    Russian roulette from the 7th segment, at most 10 segments) and, when it finally leaves the scene in direction w,
    adds throughput / pi times the L1 SH basis of w (0.2820948, -0.48860252 y, 0.48860252 z, -0.48860252 x) to the
    bounce coefficients. Sky = 2 / N * sum, coefficients * 2 pi / N. albedo: a grey value or (r, g, b) for every surface
    (the editor reads the hit texture and vertex colour; not done here). -> sky (n, 6), bounce (n, 6, 4, 3) float32."""
    rng = np.random.default_rng(seed); P = np.asarray(positions, np.float64); n = len(P)
    A = np.broadcast_to(np.asarray(albedo, np.float64), (3,))
    sky = np.zeros((n, 6)); sh = np.zeros((n, 6, 4, 3))
    per = max(1, batch // (6 * rays)); t0 = time.time()
    for s in range(0, n, per):
        pp = P[s:s + per]; m = len(pp); R = m * 6 * rays
        D = np.empty((R, 3)); C = np.empty(R)
        for k in range(m * 6):
            D[k * rays:(k + 1) * rays], C[k * rays:(k + 1) * rays] = hemisphere(rays, prt.DIR_VECTORS[k % 6], rng)
        owner = np.arange(R) // rays                                          # probe * 6 + direction
        O = np.repeat(pp, 6 * rays, axis=0); T = np.ones((R, 3)); alive = np.arange(R)
        for seg in range(BOUNCES):
            hit, t, tri, u, v = scene.cast(O + D * OFFSET, D)
            esc = ~hit
            if seg == 0:
                np.add.at(sky.reshape(-1), s * 6 + owner[alive[esc]], C[esc])
            elif esc.any():
                w = D[esc]; col = T[esc] / math.pi
                basis = np.stack([np.full(len(w), 0.2820948), -0.48860252 * w[:, 1], 0.48860252 * w[:, 2], -0.48860252 * w[:, 0]], 1)
                np.add.at(sh.reshape(-1, 4, 3), s * 6 + owner[alive[esc]], basis[:, :, None] * col[:, None, :])
            keep = np.nonzero(hit)[0]
            if not len(keep): break
            alive = alive[keep]; O = O[keep] + D[keep] * OFFSET + D[keep] * t[keep, None]; Din = D[keep]; T = T[keep]
            nrm = np.cross(scene.e1[tri[keep]].astype(np.float64), scene.e2[tri[keep]].astype(np.float64))
            nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
            nrm = np.where((np.einsum("ij,ij->i", -Din, nrm) < 0)[:, None], -nrm, nrm)
            D, c = hemisphere(len(alive), nrm, rng)
            T = T * (2.0 * c)[:, None] * A
            if seg > 5:
                p = np.clip(T.max(1), 0.0, 1.0); go = rng.random(len(p)) < p
                alive, O, D, T = alive[go], O[go], D[go], T[go] / p[go, None]
        if log: log("probes %d of %d (%.0f s)" % (min(n, s + per), n, time.time() - t0))
    return (sky * (2.0 / rays)).astype(np.float32), (sh * (2.0 * math.pi / rays)).astype(np.float32)


# ------------------------------------------------------------------------------------------------------------------- compare and bake
def _scene_folder(name):
    if os.path.isdir(name): return name
    hits = glob.glob(os.path.join(config.MODULES, "*", "SceneObj", name))
    if not hits: raise SystemExit("no scene folder %s" % name)
    return hits[0]


def compare(scene, probes=100, rays=256, albedo=0.5, entry=0, seed=0, log=print):
    """Bakes a random sample of a shipped entry's active probes at their shipped positions and compares with the shipped
    values. -> dict(sky_mae, sky_corr, sky_corr_dir [6], bounce_corr [4], bounce_scale [4], bounce_rel_err [4], n, rays)."""
    folder = _scene_folder(scene); d = prt.read(folder)
    if d["magic"] == prt.LEGACY: raise SystemExit("legacy PRT1 file")
    ent = d["entries"][entry]; e = prt.parse_entry(ent["blob"]); P = prt.probes(e, d["magic"])
    levels = [x for x in ent["name"].split(" ") if x] if ent["name"] else None
    box = (np.array(e["bounds_min"]) - 5.0, np.array(e["bounds_max"]) + 5.0)
    t = time.time(); V, F = scene_geometry(folder, levels, box, log=log); rs = RayScene(V, F, box)
    if log: log("%s: %d triangles, ray scene %.0f s" % (os.path.basename(folder), len(F), time.time() - t))
    sel = np.sort(np.random.default_rng(seed).choice(len(P["cells"]), min(probes, len(P["cells"])), replace=False))
    sky, sh = bake_probes(rs, P["position"][sel], rays, albedo, seed + 1, log=log)
    rsky, rsh = P["sky"][sel], P["bounce"][sel]
    corr = lambda a, b: float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else float("nan")
    out = dict(n=len(sel), rays=rays, sky_mae=float(np.abs(rsky - sky).mean()), sky_corr=corr(rsky.ravel(), sky.ravel()),
               sky_mean=(float(rsky.mean()), float(sky.mean())), sky_corr_dir=[corr(rsky[:, k], sky[:, k]) for k in range(6)],
               bounce_corr=[], bounce_scale=[], bounce_rel_err=[])
    for j in range(4):
        a = rsh[:, :, j, :].mean(2).ravel(); b = sh[:, :, j, :].mean(2).ravel()
        k = float(a @ b / (b @ b)) if b @ b > 0 else 0.0
        out["bounce_corr"].append(corr(a, b)); out["bounce_scale"].append(k)
        out["bounce_rel_err"].append(float(np.abs(a - k * b).mean() / max(1e-9, np.abs(a).mean())))
    return out


def bake_grid(scene, lo, hi, spacing=(2.0, 2.5), rays=RAYS, albedo=0.5, log=print):
    """A prt_data.bin (dict for prt.pack) for the box lo..hi of a scene: every cell an active probe at its centre (no
    navmesh placement, no visibility probes, no point lights), sky and bounce baked with bake_probes."""
    folder = _scene_folder(scene)
    (nx, ny, nz), bmin = prt.grid_for_bounds(lo, hi, spacing)
    cells = np.arange(nx * ny * nz); xyz = prt.cell_xyz(cells, (nx, ny, nz))
    pos = np.asarray(bmin) + (xyz + 0.5) * np.array([spacing[0], spacing[0], spacing[1]])
    box = (np.asarray(bmin) - 5.0, np.asarray(bmin) + np.array([nx, ny, nz]) * [spacing[0], spacing[0], spacing[1]] + 5.0)
    V, F = scene_geometry(folder, None, box, log=log); rs = RayScene(V, F, box)
    sky, sh = bake_probes(rs, pos, rays, albedo, log=log)
    sh = (np.floor((np.clip(sh, -2, 2) + 2.0) * 10000.0) / 10000.0 - 2.0).astype(np.float32)      # prt_quantize_sh_coefs
    e = prt.build_entry((nx, ny, nz), (spacing[0], spacing[0], spacing[1]), bmin, cells, pos, sky, sh)
    e["name"] = ""
    return dict(magic=b"PRT4", entries=[e])


def main(argv):
    vals = ("--probes", "--rays", "--albedo", "--bounds", "--spacing", "--entry", "--out")
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in vals)]
    opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["compare"] and len(a) == 2:
        config.require_game()
        r = compare(a[1], int(opt("--probes", 100)), int(opt("--rays", 256)), float(opt("--albedo", 0.5)), int(opt("--entry", 0)))
        print("%d probes x 6 directions x %d rays" % (r["n"], r["rays"]))
        print("sky visibility: mean abs error %.4f, correlation %.4f (shipped mean %.3f, baked %.3f); by direction %s" % (
            r["sky_mae"], r["sky_corr"], r["sky_mean"][0], r["sky_mean"][1], " ".join("%.3f" % c for c in r["sky_corr_dir"])))
        for j in range(4):
            print("bounce SH %d (grey): correlation %.3f, best scale %.3f, mean rel. error after scaling %.3f" % (
                j, r["bounce_corr"][j], r["bounce_scale"][j], r["bounce_rel_err"][j]))
        return 0
    if a[:1] == ["bake"] and len(a) == 2:
        config.require_game()
        b = [float(v) for v in opt("--bounds", "").split(",") if v.strip()]
        if len(b) != 6: print("--bounds x0,y0,z0,x1,y1,z1 is needed"); return 2
        sp = tuple(float(v) for v in opt("--spacing", "2,2.5").split(","))
        d = bake_grid(a[1], b[:3], b[3:], sp, int(opt("--rays", RAYS)), float(opt("--albedo", 0.5)))
        out = opt("--out", "prt_data.bin"); prt.write(out, d); print("wrote", out); return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
