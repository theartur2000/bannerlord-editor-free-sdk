"""Cloth simulation data of Bannerlord's meshes: read, write what can be written, round trip.

    python sdk/cloth.py info <mesh name>                       a mesh's cloth records: proxy mesh, clothing materials, cloth map, cloth cook
    python sdk/cloth.py check [--limit N] [--module M] [package]
                                                               decode and re-encode every shipped cloth record, compare byte for byte (N of M)
    python sdk/cloth.py cook <mesh name> [--out DIR] [--build B] [--no-map]
                                                               cook a shipped mesh's cloth (the mesh simulated in place, or its proxy) and map
                                                               its submeshes on the proxy, from the edit data alone; write the bytes to
                                                               <SDK_STAGE_DIR>/cloth and compare them with the shipped entries
    python sdk/cloth.py recook [--limit N] [--module M] [--maps V*K] [--max-particles N] [package]
                                                               re-cook every shipped cook (and rebuild the maps up to V*K render vertices x
                                                               proxy triangles) from its sources, compare byte for byte (N of M)

Cloth is spread over four places (game v1.4.8, measured on every package of every official module, read only):

1. The metamesh record. Per submesh the record carries a "clothing material" (below) and, in the submesh's flag list, "uses_cloth_simulation"
   (the submesh that is simulated) or "force_enable_cloth". The metamesh itself carries `cloth_mesh`, the guid of the cloth PROXY metamesh (a
   coarse mesh of the same garment, "clo_<name>"; 161 positions for a helmet that has 6465 vertices), and a string (the body region: "head",
   "human_body", "arms", "legs", "cape_body" ...). tw_formats.parse_metamesh_record reads both.
   The clothing material is the block at the end of each submesh (parse_clothing / pack_clothing): f32 (1 for most meshes), the material name
   (empty on most), nine f32 (bending, shearing, stretching, anchor stiffness, damping, gravity, linear inertia, air drag multiplier, wind),
   an i32 (120 for most, 240 for a few), two bytes, f32 maximum linear velocity (-1 = none), f32 linear velocity multiplier, one byte. The
   names and values of the ones with a name are listed in ModuleData/cloth_materials.xml (simulation: stretching_stiffness, anchor_stiffness,
   bending_stiffness, shearing_stiffness_, damping, gravity, linear_inertia, max_linear_velocity, linear_velocity_multiplier, wind,
   air_drag_multiplier); ModuleData/cloth_bodies.xml lists the collision capsules of the bodies cloth collides with.
2. "Cloth map" (af41613e, 1572 entries, owner = a render submesh of a mesh with a cloth proxy): how the submesh's vertices follow the proxy.
   u32 count n = 3 x vertices and n u16 (per vertex the three particles, in the proxy cook's particle order, of a proxy triangle), u32 count
   m = 4 x vertices and m f32 (per vertex: three barycentric weights that add up to 1, then the offset along the interpolated proxy normal).
   map_cloth writes it as the editor does (section "cloth map writer").
3. "Cloth cook" (b97e0f10, 898 entries, version 1, owner = the simulated submesh: a cloth proxy "clo_..." or a banner/flag simulated in
   place): the editor's cooked fabric (rglCloth_cooker.cpp; layout and algorithm in section "cloth cook: the fabric cooker"). parse_fabric
   reads its header and 13 buffers exactly; cook_fabric / pack_fabric write it from the submesh's edit data. parse_cook / pack_cook are the
   older generic split, kept for the round trip.
4. Nothing else: the vertex stream carries no cloth data.

Measured over all official modules (`check`): 197,072 clothing blocks, 1,572 cloth maps and 898 cloth cooks decode and re-encode byte-identical. Their own rules hold with
exceptions in the shipped data: 4 maps have more vertices than the submesh they belong to (e.g. battania_light_armor_c: 9,890 against 8,390), 3 cooks disagree with their submesh (bounds on 3,
particle count on 1: ship_roof_a_l3).

Writing (measured against the shipped data, game v1.4.8, every official module; `recook`):
  - Cooks: of 898 shipped cooks, 664 re-cook byte-identical from their submesh's edit data (51 with the Modding Kit editor's constants, 613
    with another build's, BUILD_VARIANTS). 211 differ only in the per-particle inverse masses (a few ulps) and/or normals (one step): made on
    a processor with another rsqrtss estimate (2 of them, flag_5, also let a zero-area triangle turn into NaN). 8 differ in anchors: the four
    10,326-particle horse harnesses (one fixed particle anchored to a coincident one) and the four 38,691-particle flags (fast search, and
    inverse masses). 12 are not cooks of their mesh as it is now (other particle count or vertex mapping). 3 cannot be re-cooked (2 without
    edit data, ship_roof_a_l3 without a simulated vertex).
  - Maps: of the 789 maps with render vertices x proxy triangles <= 3,000,000 (the other 712 were not run: hours of numpy), 433 rebuild
    byte-identical from the render and proxy edit data and the proxy's shipped cook; 756 bind every vertex to the same proxy triangle in the
    same order (2,248,682 of 2,249,376 vertices), 1,743,319 vertices have bit-identical weights, the rest differ by a few ulps (largest
    0.0019): the same processor dependence (alternative estimates match more of those vertices, none all). 71 maps have no proxy with a
    single submesh and a cook. A map needs every render vertex x every proxy triangle iterated (up to 100 steps): about 20 s per million.
Not verified, not done:
  - Nothing here was tried in the game. Which submeshes the editor cooks (a proxy, or a submesh flagged uses_cloth_simulation on a mesh
    without proxy) is read from the shipped data, not from the editor's code.
  - The per-particle normals and inverse masses use the rsqrtss estimate of Intel processors. Shipped data made on another processor
    differs from a re-cook in those (and maps in their weights) by a few ulps; its estimate table is not known.
  - The anchor search is the editor's per-particle Dijkstra up to exact_anchor_limit; above it (the 38,691-particle flags) a faster search
    that can differ by one in a few 16-bit anchor distances.
  - The stored (LZ4) form of a written entry uses this SDK's compressor; raw bytes, hashes and rows are the identical part.
No game content is stored in the repository: the module reads the install at run time.
"""
import glob, heapq, mmap, os, struct, sys, time, uuid

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, sdk_mesh, tpac, tpac_pack, tw_formats
else:
    from . import config, sdk_mesh, tpac, tpac_pack, tw_formats

CLOTH_MAP = uuid.UUID("af41613e-bd4d-46ec-8284-debb56c042dc").bytes_le     # data: "Cloth map"
CLOTH_COOK = uuid.UUID("b97e0f10-df49-4666-8f1d-ada6630d4b4e").bytes_le    # data: "Cloth cook"
tpac.DATA_KIND.update({CLOTH_MAP: 0, CLOTH_COOK: 1})


# ---------------------------------------------------------------------------------------------------------- clothing material (record)
MATERIAL_FIELDS = ("bending_stiffness", "shearing_stiffness", "stretching_stiffness", "anchor_stiffness", "damping", "gravity", "linear_inertia", "air_drag", "wind")

def parse_clothing(p):
    """The block tw_formats.parse_metamesh_record returns as a submesh's "params": dict(factor, name, <MATERIAL_FIELDS>, i0, b0, b1,
    max_linear_velocity, linear_velocity_multiplier, b2)."""
    o = 0; d = {}
    d["factor"], n = struct.unpack_from("<fI", p, o); o += 8; d["name"] = bytes(p[o:o + n]).decode("utf-8", "surrogateescape"); o += n
    for k, v in zip(MATERIAL_FIELDS, struct.unpack_from("<9f", p, o)): d[k] = v
    o += 36; d["i0"], = struct.unpack_from("<i", p, o); o += 4; d["b0"], d["b1"] = p[o], p[o + 1]; o += 2
    d["max_linear_velocity"], d["linear_velocity_multiplier"] = struct.unpack_from("<2f", p, o); o += 8; d["b2"] = p[o]; o += 1
    if o != len(p): raise ValueError("clothing block layout differs")
    return d

def pack_clothing(d):
    nm = d["name"].encode("utf-8", "surrogateescape")
    return struct.pack("<fI", d["factor"], len(nm)) + nm + struct.pack("<9f", *[d[k] for k in MATERIAL_FIELDS]) + struct.pack("<iBB", d["i0"], d["b0"], d["b1"]) \
        + struct.pack("<2fB", d["max_linear_velocity"], d["linear_velocity_multiplier"], d["b2"])


# ---------------------------------------------------------------------------------------------------------- cloth map
def parse_map(b):
    """dict(index (v, 3) u16, weight (v, 4) f32) of a cloth map entry."""
    n, = struct.unpack_from("<I", b, 0); idx = np.frombuffer(b, "<u2", n, 4); o = 4 + 2 * n
    m, = struct.unpack_from("<I", b, o); w = np.frombuffer(b, "<f4", m, o + 4); o += 4 + 4 * m
    if o != len(b) or n % 3 or m % 4 or n // 3 != m // 4: raise ValueError("cloth map layout differs")
    return dict(index=idx.reshape(-1, 3), weight=w.reshape(-1, 4))

def pack_map(d):
    i = np.asarray(d["index"], "<u2").reshape(-1, 3); w = np.asarray(d["weight"], "<f4").reshape(-1, 4)
    if len(i) != len(w): raise ValueError("one index triple and one weight vec4 per vertex")
    return struct.pack("<I", i.size) + i.tobytes() + struct.pack("<I", w.size) + w.tobytes()

def map_problems(d, vertices=None, proxy_positions=None):
    """Structure checks of a cloth map: vertex count, indices inside the proxy, weights adding up to 1. Returns a list of short strings."""
    bad = []
    if vertices is not None and len(d["index"]) != vertices: bad.append("vertex count")
    if proxy_positions is not None and len(d["index"]) and int(d["index"].max()) >= proxy_positions: bad.append("index outside the proxy")
    if len(d["weight"]) and float(np.abs(d["weight"][:, :3].sum(1) - 1).max()) > 1e-3: bad.append("weights do not add up to 1")
    return bad


# ---------------------------------------------------------------------------------------------------------- cloth cook
def parse_cook(b, arrays=9):
    """dict(hash, counts (5 u32), bounds (min vec4, max vec4), arrays [raw bytes of each count-prefixed 4-byte array], tail bytes). The arrays
    are read while their count fits the file, at most `arrays` of them: the first two have the constraint count, the later ones are short."""
    if len(b) < 56: raise ValueError("cloth cook too short")
    h, c0, c1, c2, c3, c4 = struct.unpack_from("<6I", b, 0); bounds = np.frombuffer(b, "<f4", 8, 24).reshape(2, 4); o = 56; arr = []
    while len(arr) < arrays and o + 4 <= len(b):
        c, = struct.unpack_from("<I", b, o)
        if len(arr) < 2 and c != c0: raise ValueError("cloth cook arrays differ from the header")
        if o + 4 + 4 * c > len(b) or (len(arr) >= 2 and c > 4 * c0 + 64): break
        arr.append(bytes(b[o:o + 4 + 4 * c])); o += 4 + 4 * c
    return dict(hash=h, counts=(c0, c1, c2, c3, c4), bounds=bounds.copy(), arrays=arr, tail=bytes(b[o:]))

def pack_cook(d):
    c = d["counts"]
    return struct.pack("<6I", d["hash"], *c) + np.asarray(d["bounds"], "<f4").tobytes() + b"".join(d["arrays"]) + d["tail"]

def cook_problems(d, positions=None, bounds=None):
    bad = []
    if positions is not None and d["counts"][1] != positions: bad.append("particle count")
    if bounds is not None and not np.allclose(d["bounds"][:, :3], np.asarray(bounds, np.float32).reshape(2, -1)[:, :3], atol=1e-4): bad.append("bounds")
    return bad


# ---------------------------------------------------------------------------------------------------------- cloth cook: the fabric cooker
# The editor's cooker (rglCloth_cooker.cpp in the Modding Kit's TaleWorlds.Native.dll, read with Ghidra) is TaleWorlds' own. Its constraint
# step is NvCloth's ClothFabricCooker (same 1-ring / 2-ring edge scores: +0.1 stretch per 1-ring visit, shear max(0, 0.15 - |0.45 - ratio|), bend
# max(0, 0.1 - ratio) x 3, ratio = |cross| / |d|^2), but everything around it differs from NvCloth and from PhysX 3.4's PxClothFabricCooker:
# the edges live in a sorted map (not a hash map), there is no horizontal/vertical split by gravity, the sets are an edge colouring of the
# constraint graph (boost's Misra-Gries edge_coloring, not NvCloth's greedy heap colouring), there are no NvCloth tethers (one anchor per
# particle instead, by graph distance) and the particles are reordered. So neither NvCloth nor PhysX 3.4 produces these files; this is a
# reimplementation of the editor's cooker.
#
# A cook is the header and 13 count-prefixed buffers (FAB_BUFFERS below, in file order; counts are elements of the given size):
#   header   u32 anchor scale (f32: largest anchor distance / 65536; 0 without fixed particles), u32 constraints, u32 particles,
#            u32 first simulated particle, u32 first particle with a normal update, u32 triangle corner count; bounds min, max (vec4, w 1)
#   rest     f32 per constraint: rest length                         pairs   u16 x 2 per constraint (particles, new order)
#   sets     8 u32 per set: (start, count) for stretch, bend, shear, static constraints; one set per edge colour
#   tsets    (count, start) per colour of the simulated triangles (sequential vertex colouring of triangles that share a position)
#   pos      2 u32 per particle: x, y, z quantised to 21 bits over [-100, 100]
#   nrm      u32 per particle: area-weighted normal, 11/11/10 bits      corner  u32 per render vertex: its particle
#   tris     3 u32 per simulated triangle, in tsets order                 alpha   f32 per particle: vertex colour alpha / 255
#   invmass  f32 per particle: 1 / summed area of its triangles           bones   4 u8 per particle (skinned meshes; byte count)
#   weights  4 f32 per particle (skinned meshes)                          anchor  u32 per particle: anchor particle << 16 | distance / scale
# A position is simulated when the average alpha (top byte of the vertex colour) of its vertices is not 0; a constraint goes to the static range
# of its set when the average byte 0 of the colour is 0 at both ends (builds with static ranges). Particles are ordered: fixed ones without
# a normal update, fixed ones in a simulated triangle, then the simulated ones, each in position order.
#
# Shipped cooks come from several editor builds. Three things differ between them (measured on the 898 shipped cooks; BUILD_VARIANTS):
# position quantisation (x + 100) * 10485.755 or (x + 100) / 200 * 2097151 (the builds with the second also scale the anchor distance as
# d / max * 65535 instead of 1 / max * d * 65535), alpha * 0.003921569 or alpha / 255, and static constraints with the static-set start
# filled in, or neither (all such constraints stay in their stretch/bend/shear range). The Modding Kit's editor uses the first of each
# ("editor"). The rsqrtss estimate is 1/sqrt of the operand's 10-bit mantissa bucket
# midpoint rounded to 12 bits (it equals Bochs' table of Intel's rsqrtss and reproduces most shipped cooks); cooks made on a processor
# with another estimate differ in the per-particle normals and inverse masses by an ulp or so.
FAB_BUFFERS = ("rest", "pairs", "sets", "tsets", "pos", "nrm", "corner", "tris", "alpha", "invmass", "bones", "weights", "anchor")
FAB_FILE_ORDER = FAB_BUFFERS
_FAB_SIZE = dict(sets=32, tsets=8, bones=1)
BUILD_VARIANTS = {"editor": dict(quant="mul", alpha="mul", tail=True)}
for _q, _a, _t in ((q, a, t) for q in ("mul", "div") for a in ("mul", "div") for t in (True, False)):
    BUILD_VARIANTS.setdefault("%s_%s_%s" % (_q, _a, "tail" if _t else "notail"), dict(quant=_q, alpha=_a, tail=_t))
_f32 = np.float32


def rsqrt_estimate(x):
    """The rsqrtss estimate: 1/sqrt of the midpoint of the operand's 10-bit mantissa bucket (and exponent parity), rounded to 12 fraction
    bits. This equals Bochs' tables of Intel's rsqrtss and reproduces most shipped cooks. Vectorised over float32 values > 0."""
    x = np.asarray(x, _f32); b = x.view(np.uint32).astype(np.int64); e = (b >> 23) & 0xff; fr = b & 0x7fffff
    r = 1.0 / np.sqrt((1 + ((fr >> 13) + 0.5) / 1024.0) * np.where(e & 1, 1.0, 2.0))
    q = np.floor((r * 2 - 1) * 4096 + 0.5).astype(np.int64); er = 0x7e - ((e - 0x7f) >> 1)
    out = ((er << 23) | (q << 11)).astype(np.uint32).view(_f32)
    # zero and denormal operands give infinity of their sign, infinity gives 0, negative numbers and NaN give NaN
    out = np.where(e == 0, np.where(b >> 31, -np.inf, np.inf), out); out = np.where((b >> 31) & (e != 0), np.nan, out)
    out = np.where((e == 0xff) & (b >> 31 == 0), np.where(fr == 0, 0, np.nan), out)
    return out.astype(_f32)


def _rsqrt(x):
    """The engine's 1/sqrt: the estimate refined by one Newton step, r * 0.5 * (3 - x * (r * r)), all float32."""
    x = np.asarray(x, _f32)
    with np.errstate(all="ignore"):
        r = rsqrt_estimate(x); return (r * _f32(0.5)) * (_f32(3) - x * (r * r))


def _len32(d):
    """|d| of float32 3-vectors (rows) as the engine sums it: sqrt((x*x + y*y) + z*z)."""
    d = np.asarray(d, _f32)
    return np.sqrt((d[..., 0] * d[..., 0] + d[..., 1] * d[..., 1]) + d[..., 2] * d[..., 2])


def _edge_colouring(nv, edges):
    """boost::edge_coloring (Misra-Gries) on an undirected graph with nv vertices, edges added in the given order; returns the colour of each
    edge. Out-edges are visited in insertion order, as boost's adjacency_list with vecS does."""
    out = [[] for _ in range(nv)]; col = [-1] * len(edges)
    for e, (u, v) in enumerate(edges): out[u].append((v, e)); out[v].append((u, e))
    def free(u, c):
        if c < 0: return False
        for _, e in out[u]:
            if col[e] == c: return False
        return True
    def first_free(u):
        c = 0
        while not free(u, c): c += 1
        return c
    def edge(x, y):
        for t, e in out[x]:
            if t == y: return e
    for x, y in edges:
        fan = [y]; grown = True
        while grown:                                          # maximal fan of x starting at y
            grown = False
            for v, e in out[x]:
                c = col[e]
                if c >= 0 and free(fan[-1], c) and v not in fan: fan.append(v); grown = True
        c = first_free(x); d = first_free(fan[-1])
        for v, e in out[x]:                                   # invert the c/d path that starts at x with colour d
            if col[e] == d:
                col[e] = c; cur, prev, look, put = v, e, c, d
                while True:
                    nxt = next(((v2, e2) for v2, e2 in out[cur] if col[e2] == look and e2 != prev), None)
                    if nxt is None: break
                    col[nxt[1]] = put; cur, prev = nxt; look, put = put, look
                break
        w = next((i for i, u in enumerate(fan) if free(u, d)), len(fan) - 1)
        for i in range(w): col[edge(x, fan[i])] = col[edge(x, fan[i + 1])]   # rotate the fan
        col[edge(x, fan[w])] = d
    return col


def _anchor_distance(nb, wts, src, targets):
    """The editor's per-pair Dijkstra from src (float32 sums along the path from src): (distance, target) of the nearest target, ties to the
    lowest target index; (inf, -1) when none is reachable. Stops once the nearest target is settled."""
    dist = {src: _f32(0)}; done = set(); heap = [(0.0, src)]; best = None
    while heap:
        du, u = heapq.heappop(heap)
        if u in done: continue
        if best is not None and du > best[0]: break
        done.add(u)
        if u in targets:
            if best is None or u < best[1]: best = (du, u)
            continue
        du32 = dist[u]
        for v, w in zip(nb[u], wts[u]):
            if v in done: continue
            nd = _f32(du32 + w)
            if v not in dist or nd < dist[v]: dist[v] = nd; heapq.heappush(heap, (float(nd), v))
    return (dist[best[1]], best[1]) if best else (np.inf, -1)


def cook_fabric(positions, vertex_position, vertex_colour, triangles, skin=None, exact_anchor_limit=4e7):
    """Cook the cloth fabric of a simulation mesh as the editor does. positions (n, 3|4), vertex_position (v,) position index of each
    vertex, vertex_colour (v,) u32 colours (alpha, the top byte, marks simulated vertices; a constraint between two positions whose
    byte 0 averages 0 goes to the static range of its set), triangles (k, 3) vertex indices, skin: per
    position weights (structured array w (4 f32), b (4 u8), as sdk_mesh.WEIGHT) or None. Returns a dict for pack_fabric (header, bounds,
    buffers, and per particle in cook order: positions, alpha, anchor particle, anchor distance), or None when no vertex is simulated. The anchor search is the editor's (one search per simulated particle) while simulated
    particles x particles <= exact_anchor_limit, a faster near-exact one above."""
    P = np.asarray(positions, _f32)[:, :3]; n = len(P); vp = np.asarray(vertex_position, np.int64)
    vc = np.asarray(vertex_colour, np.uint32); T = vp[np.asarray(triangles, np.int64).reshape(-1, 3)]
    cnt = np.bincount(vp, minlength=n).astype(_f32)
    def average(byte):                                        # per position: integer average of one colour byte over its vertices
        s = np.bincount(vp, weights=((vc >> (8 * byte)) & 0xff).astype(np.float64), minlength=n).astype(_f32)
        with np.errstate(all="ignore"): return np.where(cnt > 0, s / cnt, 0).astype(np.int64)
    alpha = average(3); sim = alpha != 0; low = average(0)
    if not sim.any(): return None
    nb = [set() for _ in range(n)]
    for a, b, c in T.tolist(): nb[a].update((b, c)); nb[b].update((c, a)); nb[c].update((a, b))
    nb = [sorted(x) for x in nb]
    # normals and areas: per triangle the unit normal times the area, summed per position in triangle order
    e1 = P[T[:, 1]] - P[T[:, 0]]; e2 = P[T[:, 2]] - P[T[:, 0]]
    cx = e2[:, 2] * e1[:, 1] - e1[:, 2] * e2[:, 1]; cy = e2[:, 0] * e1[:, 2] - e1[:, 0] * e2[:, 2]; cz = e1[:, 0] * e2[:, 1] - e2[:, 0] * e1[:, 1]
    with np.errstate(all="ignore"):
        l2 = (cx * cx + cy * cy) + cz * cz; r = _rsqrt(l2); area = (r * l2) * _f32(0.5)
        add = np.stack([(cx * r) * area, (r * cy) * area, (r * cz) * area, area], 1)
    add[l2 == 0] = 0                       # a zero-area triangle adds nothing (measured: 14 of 16 shipped cooks with one; rsqrtss(0) = inf would poison)
    N = np.zeros((n, 4), _f32)
    for j in range(4): np.add.at(N[:, j], T.reshape(-1), np.repeat(add[:, j], 3))
    # a sum of exactly zero gets (0, 0, 1) and area 1 (the engine's compare would also take NaN for zero)
    flat = ((N[:, 0] == 0) | np.isnan(N[:, 0])) & ((N[:, 1] == 0) | np.isnan(N[:, 1])) & ((N[:, 2] == 0) | np.isnan(N[:, 2])); N[flat, 2] = 1; N[flat, 3] = 1
    with np.errstate(all="ignore"): N[:, :3] *= _rsqrt((N[:, 0] * N[:, 0] + N[:, 1] * N[:, 1]) + N[:, 2] * N[:, 2])[:, None]
    # simulated triangles: those with a simulated corner; coloured so that triangles sharing a position differ (boost sequential colouring)
    tsim = np.nonzero(sim[T].any(1))[0]; normal_update = np.zeros(n, bool); normal_update[T[tsim].reshape(-1)] = True
    at = [[] for _ in range(n)]
    for k, t in enumerate(tsim.tolist()):
        for p in set(T[t].tolist()): at[p].append(k)
    tcol = [-1] * len(tsim); ncol = 0
    for k, t in enumerate(tsim.tolist()):
        used = {tcol[o] for p in set(T[t].tolist()) for o in at[p] if o != k}
        c = 0
        while c < ncol and c in used: c += 1
        ncol = max(ncol, c + 1); tcol[k] = c
    tsets, tris = [], []
    for c in range(ncol):
        g = [int(t) for k, t in enumerate(tsim) if tcol[k] == c]; tsets.append((len(g), len(tris) // 3)); tris += T[g].reshape(-1).tolist()
    # constraints: 1-ring and 2-ring edges with a simulated end, scored as NvCloth does, in a map sorted by (low, high) position
    acc = {}; cv = {}
    def vertex(a):
        if a not in cv: cv[a] = len(cv)
    zero = _f32(0)
    for i in range(n):
        pi_ = P[i]
        for m in nb[i]:
            a, b = (i, m) if i < m else (m, i)
            if sim[a] or sim[b]:
                e = acc.setdefault((a, b), [zero, zero, zero]); e[0] = _f32(e[0] + _f32(0.1)); vertex(a); vertex(b)
            am = P[m] - pi_
            for q in nb[m]:
                if q == i: continue
                a, b = (i, q) if i < q else (q, i)
                if not (sim[a] or sim[b]): continue
                bq = P[q] - P[m]; dq = P[q] - pi_
                c3 = np.array([bq[2] * am[1] - bq[1] * am[2], bq[0] * am[2] - am[0] * bq[2], am[0] * bq[1] - bq[0] * am[1]], _f32)
                with np.errstate(all="ignore"): ratio = _f32(_len32(c3) / ((dq[0] * dq[0] + dq[1] * dq[1]) + dq[2] * dq[2]))
                vertex(a); vertex(b); e = acc.setdefault((a, b), [zero, zero, zero])
                sh = _f32(_f32(0.15) - abs(_f32(_f32(0.45) - ratio))); e[1] = _f32(e[1] + (sh if sh > 0 else zero))
                bd = _f32(_f32(0.1) - ratio); e[2] = _f32(e[2] + (_f32(bd * _f32(3)) if bd > 0 else zero))
    keys = sorted(acc)
    def kind(k):                                              # 0 stretch, 1 bend, 2 shear
        st, sh, bd = acc[k]
        return 1 if (bd > sh and bd > st) else 2 if (sh > bd and sh > st) else 0
    col = _edge_colouring(len(cv), [(cv[a], cv[b]) for a, b in keys]); ncs = max(col) + 1 if col else 0
    members = [[] for _ in range(ncs)]                         # per set, in map order: low end, high end, kind, static (byte 0 of both ends 0)
    for k, c in zip(keys, col): members[c].append((k[0], k[1], kind(k), int(low[k[0]] == 0 and low[k[1]] == 0)))
    # particle order
    order = np.r_[np.nonzero(~sim & ~normal_update)[0], np.nonzero(~sim & normal_update)[0], np.nonzero(sim)[0]]
    new = np.empty(n, np.int64); new[order] = np.arange(n)
    first_normal, first_sim = int((~sim & ~normal_update).sum()), int((~sim).sum())
    # anchors: each simulated particle's nearest fixed particle that touches a simulated one, by graph distance
    border = {i for i in range(n) if not sim[i] and any(sim[m] for m in nb[i])}
    anchor = np.arange(n); adist = np.zeros(n, _f32); scale = 0; anc = np.zeros(0, np.uint32)
    if not sim.all():
        wts = [_len32(P[x] - P[i]).astype(_f32) if x else [] for i, x in enumerate(nb)]
        simv = np.nonzero(sim)[0].tolist()
        if border and len(simv) * n <= exact_anchor_limit:
            for v in simv:                                    # the editor's search, per simulated particle
                d, a = _anchor_distance(nb, wts, v, border)
                if a >= 0: anchor[v] = a; adist[v] = d
        elif border:
            # large meshes: one search from all anchor candidates at once, the distance summed back along the found path from the
            # particle's side; only the farthest particles (they set the scale) are searched as the editor does. Not guaranteed bit-identical
            # to the editor: paths of equal length can be summed in another order, which moves a 16-bit distance by one now and then.
            dist = np.full(n, np.inf); src = np.full(n, -1, np.int64); par = np.full(n, -1, np.int64); heap = [(0.0, f, f) for f in sorted(border)]
            for _, f, _f in heap: dist[f] = 0; src[f] = f
            heapq.heapify(heap); done = np.zeros(n, bool)
            while heap:
                du, s_, u = heapq.heappop(heap)
                if done[u]: continue
                done[u] = True
                for v, w in zip(nb[u], wts[u]):
                    nd = du + float(w)
                    if not done[v] and (nd < dist[v] or (nd == dist[v] and s_ < src[v])): dist[v] = nd; src[v] = s_; par[v] = u; heapq.heappush(heap, (nd, s_, v))
            for v in simv:
                if src[v] < 0: continue
                path = [v]
                while path[-1] != src[v]: path.append(int(par[path[-1]]))
                pa = np.array(path); anchor[v] = src[v]; adist[v] = np.add.accumulate(_len32(P[pa[:-1]] - P[pa[1:]]).astype(_f32), dtype=_f32)[-1]
            mx = _f32(adist.max())
            for v in simv:
                if adist[v] >= mx * _f32(0.99998):
                    d, a = _anchor_distance(nb, wts, v, border)
                    if a >= 0: anchor[v] = a; adist[v] = d
        mx = _f32(adist.max()); scale = int(np.array(mx * _f32(1.5259022e-05), _f32).view(np.uint32))
        anc = np.zeros(n, np.int64); anc[new] = new[np.minimum(anchor, 0xffff)]             # anchor particle of each particle (new order)
    # per particle data
    with np.errstate(all="ignore"): nq = N[:, :3] * _f32(0.5) + _f32(0.5)
    nq = np.minimum(np.where(nq > 0, nq, _f32(0)), _f32(1))                         # max(v, 0) as maxps does it: NaN gives 0
    nrm = np.zeros(n, np.uint32)
    nrm[new] = ((((nq[:, 1] * _f32(2047)).astype(np.int64) & 0x7ff) | ((nq[:, 0] * _f32(2047)).astype(np.int64) << 11)) << 10
                | ((nq[:, 2] * _f32(1023)).astype(np.int64) & 0x3ff)).astype(np.uint32)
    with np.errstate(all="ignore"): inv = np.zeros(n, _f32); inv[new] = _f32(1) / N[:, 3]
    members = [np.array(m, np.int64).reshape(-1, 4) for m in members]
    for m in members: m[:, :2] = new[m[:, :2]]
    bufs = dict(tsets=np.array(tsets, "<u4").reshape(-1, 2).tobytes(), nrm=nrm.tobytes(),
                corner=new[vp].astype("<u4").tobytes(), tris=new[np.array(tris, np.int64)].astype("<u4").tobytes(), invmass=inv.tobytes(), anchor=b"")
    if skin is not None and len(skin):
        k = min(len(skin), n); w = np.zeros((n, 4), "<f4"); bi = np.zeros((n, 4), "u1"); w[new[:k]] = skin["w"][:k]; bi[new[:k]] = skin["b"][:k]
        bufs["weights"], bufs["bones"] = w.tobytes(), bi.tobytes()
    else:
        bufs["weights"] = bufs["bones"] = b""
    lo = np.r_[P.min(0), 1].astype("<f4"); hi = np.r_[P.max(0), 1].astype("<f4")
    return dict(header=[scale, len(keys), n, first_sim, first_normal, len(tris)], bounds=np.stack([lo, hi]), buffers=bufs, sets=members,
                positions=P[order], alpha=alpha[order], anchor=anc, anchor_distance=adist[order])


def pack_fabric(fab, build="editor"):
    """The cook bytes of a cook_fabric result, with the constants of one editor build (BUILD_VARIANTS; "editor" = the Modding Kit's)."""
    v = BUILD_VARIANTS[build]; P = fab["positions"]; b = dict(fab["buffers"])
    if v["quant"] == "mul": q = ((P + _f32(100)) * _f32(10485.755)).astype(np.int64)
    else: q = (((P + _f32(100)) / _f32(200)) * _f32(2097151)).astype(np.int64)
    pos = np.zeros((len(P), 2), np.int64); pos[:, 0] = ((q[:, 1] >> 10) & 0x7ff) | (q[:, 0] << 11); pos[:, 1] = ((q[:, 1] << 21) | q[:, 2]) * 2
    b["pos"] = (pos & 0xffffffff).astype("<u4").tobytes()
    a = fab["alpha"].astype(_f32); b["alpha"] = (a * _f32(0.003921569) if v["alpha"] == "mul" else a / _f32(255)).astype("<f4").tobytes()
    if len(fab["anchor"]):
        d = fab["anchor_distance"]; mx = _f32(d.max())
        with np.errstate(all="ignore"):
            q = ((_f32(1) / mx) * d * _f32(65535) if v["quant"] == "mul" else (d / mx) * _f32(65535)).astype(np.int64)
        q = np.where((q < 0) | (q > 0xffff), 0xffff, q); b["anchor"] = ((fab["anchor"] << 16) | q).astype("<u4").tobytes()
    # constraint sets: per set the stretch, bend, shear and static ranges, each in map order; builds without the static start have no
    # static range (measured: static constraints appear only in cooks whose sets carry that start)
    recs, cons = [], []
    for m in fab["sets"]:
        static = m[:, 3].astype(bool) if v["tail"] else np.zeros(len(m), bool); rec = []
        for t in range(4):
            sel = m[static] if t == 3 else m[(m[:, 2] == t) & ~static]
            rec += [len(cons) if (t < 3 or v["tail"]) else 0, len(sel)]; cons += sel[:, :2].tolist()
        recs.append(rec)
    ca = np.array(cons, np.int64).reshape(-1, 2)
    b["rest"] = _len32(P[ca[:, 0]] - P[ca[:, 1]]).astype("<f4").tobytes(); b["pairs"] = ca.astype("<u2").tobytes()
    b["sets"] = np.array(recs, "<u4").reshape(-1, 8).tobytes()
    out = struct.pack("<6I", *fab["header"]) + np.asarray(fab["bounds"], "<f4").tobytes()
    for k in FAB_FILE_ORDER: out += struct.pack("<I", len(b[k]) // _FAB_SIZE.get(k, 4)) + b[k]
    return out


def parse_fabric(b):
    """A cook's header (6 u32), bounds (2, 4) and named buffers (raw bytes), as pack_fabric lays them out."""
    h = list(struct.unpack_from("<6I", b, 0)); bounds = np.frombuffer(b, "<f4", 8, 24).reshape(2, 4); o = 56; bufs = {}
    for k in FAB_FILE_ORDER:
        n, = struct.unpack_from("<I", b, o); o += 4; size = n * _FAB_SIZE.get(k, 4); bufs[k] = bytes(b[o:o + size]); o += size
        if o > len(b): raise ValueError("cloth cook shorter than its buffers")
    if o != len(b): raise ValueError("cloth cook longer than its buffers")
    return dict(header=h, bounds=bounds.copy(), buffers=bufs)


def cook_mesh(edit, build="editor"):
    """Cook bytes for one submesh's edit data (sdk_mesh.parse_edit dict or raw bytes), or None when no vertex is simulated."""
    E = sdk_mesh.parse_edit(edit) if isinstance(edit, (bytes, bytearray, memoryview)) else edit
    fab = cook_fabric(E["P"], E["V"]["pi"], E["V"]["c0"], E["F"], E["W"] if E.get("W") is not None and len(E["W"]) else None)
    return None if fab is None else pack_fabric(fab, build)


def cook_submesh(sub, build="editor"):
    """Cook bytes for a submesh dict as sdk_mesh.write_metamesh takes it (P, pi, c0, F, W); None when no vertex has a non-zero colour alpha.
    The entry for the package is (CLOTH_COOK, bytes, sub["guid"]) next to the submesh's edit data."""
    fab = cook_fabric(sub["P"], sub["pi"], sub["c0"], sub["F"], sub.get("W"))
    return None if fab is None else pack_fabric(fab, build)


# ---------------------------------------------------------------------------------------------------------- cloth map writer
# rglCloth_cooker::map_mesh (editor DLL): for every render vertex, every triangle of the simulation (proxy) mesh is tried. A triangle's
# point is found by iterating: project the vertex along the interpolated vertex normal (n2 field of the proxy's vertices) onto the
# triangle's plane, take its barycentric coordinates, blend them in (weight 1, then x 0.8 per step), re-interpolate the normal, until the
# coordinates move less than sqrt(tolerance) (at most 100 steps; the plane distance must stay under 1e-4 and the weights must add up to 1
# within 0.001). The score is the squared distance of the coordinates outside [0, 1] plus offset / perimeter (2 x when the offset is
# negative); the lowest score wins, the first triangle on ties. When no triangle converges the tolerance (1e-4) grows x 10, at most 5 times;
# a vertex that never maps gets indices 0 and weights (1, 0, 0, 0). The map stores the triangle's three particles (the cook's corner
# buffer) and (w0, w1, w2, offset along the normalised interpolated normal); the first two are swapped so that they ascend when the
# triangle's interpolated cooked normal faces the render vertex's normal and descend otherwise.

def _tri_frames(a, b, c):
    """Per triangle what map_mesh's barycentric step needs that does not depend on the point: unit normal, edges, Gram terms."""
    ax, ay, az = a[:, 0], a[:, 1], a[:, 2]
    nx = (b[:, 1] - ay) * (c[:, 2] - az) - (b[:, 2] - az) * (c[:, 1] - ay)
    ny = (b[:, 2] - az) * (c[:, 0] - ax) - (b[:, 0] - ax) * (c[:, 2] - az)
    nz = (b[:, 0] - ax) * (c[:, 1] - ay) - (b[:, 1] - ay) * (c[:, 0] - ax)
    r = _rsqrt((ny * ny + nx * nx) + nz * nz)
    e1x, e1y, e1z = b[:, 0] - ax, b[:, 1] - ay, b[:, 2] - az
    e2x, e2y, e2z = c[:, 0] - ax, c[:, 1] - ay, c[:, 2] - az
    d00 = (e1y * e1y + e1x * e1x) + e1z * e1z; d01 = (e2y * e1y + e2x * e1x) + e2z * e1z; d11 = (e2y * e2y + e2x * e2x) + e2z * e2z
    den = d11 * d00 - d01 * d01
    return dict(a=a, n=np.stack([nx * r, ny * r, nz * r], 1), e1=np.stack([e1x, e1y, e1z], 1), e2=np.stack([e2x, e2y, e2z], 1),
                d00=d00, d01=d01, d11=d11, den=den, inv=_f32(1) / den)


def _bary(f, q):
    """Barycentric coordinates of q's projection on the triangles of _tri_frames f and q's distance to their planes (rows, float32)."""
    a, n, e1, e2 = f["a"], f["n"], f["e1"], f["e2"]
    dist = ((q[:, 1] - a[:, 1]) * n[:, 1] + (q[:, 0] - a[:, 0]) * n[:, 0]) + (q[:, 2] - a[:, 2]) * n[:, 2]
    pz = a[:, 2] - (q[:, 2] - n[:, 2] * dist); py = a[:, 1] - (q[:, 1] - n[:, 1] * dist); px = a[:, 0] - (q[:, 0] - n[:, 0] * dist)
    d21 = (py * e2[:, 1] + px * e2[:, 0]) + pz * e2[:, 2]; d20 = (py * e1[:, 1] + px * e1[:, 0]) + pz * e1[:, 2]
    nz = f["den"] != 0
    v = np.where(nz, (d21 * f["d01"] - d20 * f["d11"]) * f["inv"], _f32(0)).astype(_f32)
    w = np.where(nz, (d20 * f["d01"] - d21 * f["d00"]) * f["inv"], _f32(0)).astype(_f32)
    return _f32(1) - (w + v), v, w, dist


def _map_pairs(x, a, b, c, na, nb, nc, tol):
    """One render point per row against one triangle per row: (converged, w0, w1, w2, offset), float32 as the editor computes them."""
    k = len(a)
    fx = (b[:, 1] - a[:, 1]) * (c[:, 2] - a[:, 2]) - (b[:, 2] - a[:, 2]) * (c[:, 1] - a[:, 1])
    fz = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    fy = (b[:, 2] - a[:, 2]) * (c[:, 0] - a[:, 0]) - (b[:, 0] - a[:, 0]) * (c[:, 2] - a[:, 2])
    l2 = (fy * fy + fx * fx) + fz * fz; alive = l2 != 0
    r = _rsqrt(l2); fx, fy, fz = fx * r, fy * r, fz * r
    third = _f32(0.33333334)
    m = np.stack([((na[:, 0] + nb[:, 0]) + nc[:, 0]) * third, ((nb[:, 1] + na[:, 1]) + nc[:, 1]) * third, ((nb[:, 2] + na[:, 2]) + nc[:, 2]) * third], 1)
    B = np.full((k, 3), third, _f32); lam = _f32(1); ok = np.zeros(k, bool)
    pd = ((fx * a[:, 0] + fy * a[:, 1]) + fz * a[:, 2]) - ((fx * x[:, 0] + fy * x[:, 1]) + fz * x[:, 2])
    act = np.nonzero(alive)[0]
    # the rows still iterating, compacted: every per-row array below is aligned with act
    C = dict(x=x[act], fx=fx[act], fy=fy[act], fz=fz[act], pd=pd[act], na=na[act], nb=nb[act], nc=nc[act], tol=tol[act], m=m[act], B=B[act])
    C.update({"f_" + kk: vv for kk, vv in _tri_frames(a[act], b[act], c[act]).items()})
    for _ in range(100):
        if not len(act): break
        mx, my, mz = C["m"][:, 0], C["m"][:, 1], C["m"][:, 2]; xa = C["x"]
        d = (C["fy"] * my + C["fx"] * mx) + C["fz"] * mz; t = C["pd"] / d
        q = np.where((d == 0)[:, None], xa, np.stack([mx * t + xa[:, 0], my * t + xa[:, 1], mz * t + xa[:, 2]], 1)).astype(_f32)
        u, v, w, dist = _bary({kk[2:]: vv for kk, vv in C.items() if kk.startswith("f_")}, q)
        b0, b1, b2 = C["B"][:, 0], C["B"][:, 1], C["B"][:, 2]
        err = (v - b1) * (v - b1) + (u - b0) * (u - b0) + (w - b2) * (w - b2)
        om = _f32(1) - lam; n0 = u * lam + om * b0; n1 = v * lam + om * b1; n2 = om * b2 + w * lam
        live = ~(np.abs(dist) > _f32(1e-4)) & ~(np.abs(_f32(1) - ((n0 + n1) + n2)) > _f32(0.001))
        A0, A1, A2 = C["na"], C["nb"], C["nc"]
        C["B"] = np.stack([n0, n1, n2], 1)
        C["m"] = np.stack([(n0 * A0[:, 0] + n1 * A1[:, 0]) + n2 * A2[:, 0], (n1 * A1[:, 1] + n0 * A0[:, 1]) + n2 * A2[:, 1],
                           (n0 * A0[:, 2] + n1 * A1[:, 2]) + n2 * A2[:, 2]], 1)
        done = live & (err < C["tol"])
        B[act[live]] = C["B"][live]; m[act[live]] = C["m"][live]
        alive[act[~live]] = False; ok[act[done]] = True
        keep = live & ~done
        if not keep.all():
            act = act[keep]; C = {kk: vv[keep] for kk, vv in C.items()}
        lam = _f32(lam * _f32(0.8))
    ok &= alive
    b0, b1, b2 = B[:, 0], B[:, 1], B[:, 2]; mx, my, mz = m[:, 0], m[:, 1], m[:, 2]
    rr = _rsqrt((mx * mx + my * my) + mz * mz)
    def at(k_): return (b1 * b[:, k_] + b0 * a[:, k_]) + b2 * c[:, k_]
    off = ((x[:, 1] - at(1)) * (rr * my) + (x[:, 0] - at(0)) * (rr * mx)) + (x[:, 2] - at(2)) * (mz * rr)
    return ok, b0.copy(), b1.copy(), b2.copy(), off.astype(_f32)


def _outside(b0, b1, b2):
    def o(t):
        v = np.abs(t - _f32(0.5)) - _f32(0.5); return np.where(v > 0, v, _f32(0)).astype(_f32)
    return (o(b1) * o(b1) + o(b0) * o(b0)) + o(b2) * o(b2)


def _unpack_normal(u, doubled=False):
    """A cooked 11/11/10 normal as map_mesh reads it back (the third corner's is decoded with half the scale, doubled)."""
    u = np.asarray(u, np.int64); x, y, z = (u >> 21).astype(_f32), ((u >> 10) & 0x7ff).astype(_f32), (u & 0x3ff).astype(_f32)
    if not doubled: return x * _f32(0.0009770396) - _f32(1), y * _f32(0.0009770396) - _f32(1), z * _f32(0.0019550342) - _f32(1)
    x, y, z = x * _f32(0.0004885198), y * _f32(0.0004885198), z * _f32(0.0009775171)
    return (x + x) - _f32(1), (y + y) - _f32(1), (z + z) - _f32(1)


def map_cloth(render, sim, cook, chunk=512, first=8):
    """The cloth map of a render submesh on its simulation (proxy) mesh, as the editor writes it. render, sim: edit data (raw bytes or
    sdk_mesh.parse_edit dicts) of the render submesh and of the proxy's single submesh; cook: the proxy's cook bytes (its corner and normal
    buffers are used). Returns dict(index (v, 3) u16, weight (v, 4) f32) for pack_map."""
    Rm = sdk_mesh.parse_edit(render) if isinstance(render, (bytes, bytearray, memoryview)) else render
    S = sdk_mesh.parse_edit(sim) if isinstance(sim, (bytes, bytearray, memoryview)) else sim
    fab = parse_fabric(cook); corner = np.frombuffer(fab["buffers"]["corner"], "<u4").astype(np.int64); nrm = np.frombuffer(fab["buffers"]["nrm"], "<u4")
    SP = np.asarray(S["P"], _f32)[:, :3]; SV = S["V"]; SF = np.asarray(S["F"], np.int64); K = len(SF); spi = np.asarray(SV["pi"], np.int64)
    TA, TB, TC = SP[spi[SF[:, 0]]], SP[spi[SF[:, 1]]], SP[spi[SF[:, 2]]]
    NA, NB, NC = [np.asarray(SV["n2"][SF[:, j], :3], _f32) for j in range(3)]
    def ln(d): return np.sqrt((d[:, 1] * d[:, 1] + d[:, 0] * d[:, 0]) + d[:, 2] * d[:, 2])
    per = ((ln(TB - TA) + ln(TC - TA)) + ln(TC - TB)).astype(_f32)
    cen = (TA.astype(np.float64) + TB + TC) / 3; rad = np.max([np.linalg.norm(T - cen, axis=1) for T in (TA, TB, TC)], 0) if K else np.zeros(0)
    X = np.asarray(Rm["P"], _f32)[np.asarray(Rm["V"]["pi"], np.int64), :3]; nv = len(X)
    best_s = np.full(nv, np.inf); best_k = np.full(nv, -1, np.int64); best_w = np.zeros((nv, 4), _f32)
    with np.errstate(all="ignore"):
        for s0 in range(0, nv if K else 0, chunk):
            vs_all = np.arange(s0, min(nv, s0 + chunk))
            # a lower bound of each triangle's distance in units of its perimeter: a triangle with score s lies within about
            # (s + sqrt(3 s)) perimeters of the vertex, so candidates are tried nearest first and the rest skipped once out of reach
            lb = (np.linalg.norm(X[vs_all, None, :].astype(np.float64) - cen[None], axis=2) - rad[None]) / np.maximum(per.astype(np.float64), 1e-12)[None]
            order = np.argsort(lb, 1, kind="stable"); rnd = np.zeros(len(vs_all), np.int64); lo = np.zeros(len(vs_all), np.int64)
            pend = np.arange(len(vs_all)); width = first
            while len(pend):
                rows = np.repeat(pend, width); cols = (lo[pend][:, None] + np.arange(width)[None]).reshape(-1)
                keep = cols < K; rows, cols = rows[keep], cols[keep]; ks = order[rows, cols]; vs = vs_all[rows]
                tol = np.full(len(rows), _f32(1e-4), _f32)
                for i in range(1, 5): tol = np.where(rnd[rows] >= i, tol * _f32(10), tol).astype(_f32)
                ok, b0, b1, b2, off = _map_pairs(X[vs], TA[ks], TB[ks], TC[ks], NA[ks], NB[ks], NC[ks], tol)
                o = off / per[ks]
                sc = ((_outside(b0, b1, b2) + np.where(-o > 0, -o, _f32(0)) * _f32(2)) + np.where(o > 0, o, _f32(0))).astype(np.float64)
                sel = np.nonzero(ok)[0]
                if len(sel):
                    srt = sel[np.lexsort((ks[sel], sc[sel], vs[sel]))]; first_of = np.r_[True, vs[srt][1:] != vs[srt][:-1]]
                    for i in srt[first_of]:
                        v = vs[i]
                        if sc[i] < best_s[v] or (sc[i] == best_s[v] and ks[i] < best_k[v]): best_s[v] = sc[i]; best_k[v] = ks[i]; best_w[v] = (b0[i], b1[i], b2[i], off[i])
                lo[pend] += width; nxt = []
                for p in pend.tolist():
                    v = vs_all[p]
                    if lo[p] >= K:
                        if best_k[v] < 0 and rnd[p] < 4: rnd[p] += 1; lo[p] = 0; nxt.append(p)
                        continue
                    if best_k[v] >= 0 and lb[p, order[p, lo[p]]] > 2 * (best_s[v] + np.sqrt(3 * best_s[v])) + 0.1: continue
                    nxt.append(p)
                pend = np.array(nxt, np.int64); width = min(max(K, 1), width * 4)
    index = np.zeros((nv, 3), "<u2"); weight = np.zeros((nv, 4), "<f4"); weight[:, 0] = 1
    for v in np.nonzero(best_k >= 0)[0].tolist():
        k = best_k[v]; i0, i1, i2 = (int(corner[SF[k, j]]) for j in range(3)); w0, w1, w2, o = best_w[v]
        n0, n1, n2 = _unpack_normal(nrm[i0]), _unpack_normal(nrm[i1]), _unpack_normal(nrm[i2], True)
        nx = (n1[0] * w1 + n0[0] * w0) + n2[0] * w2; ny = (n1[1] * w1 + n0[1] * w0) + n2[1] * w2; nz = (n1[2] * w1 + n0[2] * w0) + n2[2] * w2
        with np.errstate(all="ignore"): r = _rsqrt(_f32((ny * ny + nx * nx) + nz * nz))
        vn = np.asarray(Rm["V"]["n2"][v], _f32); dot = ((r * ny) * vn[1] + (r * nx) * vn[0]) + (r * nz) * vn[2]
        if (dot >= 0 and i1 < i0) or (not dot >= 0 and i0 < i1): i0, i1, w0, w1 = i1, i0, w1, w0
        index[v] = (i0, i1, i2); weight[v] = (w0, w1, w2, o)
    return dict(index=index, weight=weight)


def map_submesh(render_sub, proxy_sub, proxy_cook):
    """Cloth map bytes for a render submesh dict on a proxy submesh dict (both as sdk_mesh.write_metamesh takes them; the proxy's normals N
    are the ones the map follows) and the proxy's cook bytes (cook_submesh). The entry is (CLOTH_MAP, bytes, render_sub["guid"])."""
    return pack_map(map_cloth(sdk_mesh.edit_blob(render_sub), sdk_mesh.edit_blob(proxy_sub), proxy_cook))


def recook_matches(raw, fab):
    """Which build's constants reproduce a shipped cook from its re-cooked fabric: (build name or None, names of the buffers/header parts
    that still differ under the closest build's constants)."""
    for name in BUILD_VARIANTS:
        if pack_fabric(fab, name) == raw: return name, []
    try: a = parse_fabric(raw)
    except ValueError: return None, ["layout"]
    best = None
    for name in BUILD_VARIANTS:
        c = parse_fabric(pack_fabric(fab, name)); diff = [k for k in FAB_FILE_ORDER if a["buffers"][k] != c["buffers"][k]]
        diff += ["header %d" % i for i in range(6) if a["header"][i] != c["header"][i]] + (["bounds"] if not np.array_equal(a["bounds"], c["bounds"]) else [])
        if best is None or len(diff) < len(best): best = diff
    return None, best


# ---------------------------------------------------------------------------------------------------------- measurement
def packages(module=None):
    pats = [os.path.join(config.MODULES, module or "*", d, "**", "*.tpac") for d in ("AssetPackages", "EmAssetPackages")]
    return sorted(set(p for pat in pats for p in glob.glob(pat, recursive=True)))

def _read(m, e):
    st = bytes(m[e["off"]:e["off"] + e["stored"]])
    return tpac.lz4_decompress(st, e["raw"]) if e["stored"] < e["raw"] else st

def proxy_index(paths):
    """{metamesh guid: (package path, item name, submesh position counts)} over the given packages."""
    out = {}
    for p in paths:
        try:
            with open(p, "rb") as f:
                h = f.read(0x24)
                if h[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", h, 0x1c); pk = tpac_pack.parse_package(h + f.read(meta))
        except Exception: continue
        for it in pk["items"]:
            if it["type"] == sdk_mesh.META_TYPE and it["guid"] not in out:
                try: out[it["guid"]] = (p, it["name"].decode("utf-8", "replace"), [s["positions"] for s in tw_formats.parse_metamesh_record(it["record"])["submeshes"]])
                except Exception: pass
    return out

def check(paths=None, limit=None, log=None):
    """Round-trip every cloth map, cloth cook and clothing-material block of the given packages (default: all of them). Returns a dict with the
    counts: submeshes and clothing blocks (blocks identical), maps (identical, structure problems, bound to a proxy found), cooks (identical,
    header problems), failures {class: (count, example)}."""
    files = list(paths) if paths else packages(); proxies = proxy_index(files) if files else {}
    out = dict(submeshes=0, clothing=0, clothing_identical=0, simulated=0, maps=0, maps_identical=0, maps_checked_against_proxy=0, map_problems=0,
               cooks=0, cooks_identical=0, cooks_layout=0, cook_problems=0, named_materials=0, failures={}); t0 = time.time()
    def fail(k, where): c, e0 = out["failures"].get(k, (0, where)); out["failures"][k] = (c + 1, e0)
    for p in files:
        with open(p, "rb") as f:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                if m[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
                for it in pk["items"]:
                    if it["type"] != sdk_mesh.META_TYPE: continue
                    where = "%s:%s" % (os.path.basename(p), it["name"].decode("utf-8", "replace"))
                    try: rec = tw_formats.parse_metamesh_record(it["record"])
                    except Exception as ex: fail("record: %s" % str(ex)[:40], where); continue
                    subs = {s["guid"]: s for s in rec["submeshes"]}; px = proxies.get(rec["cloth_mesh"]) if rec["cloth_mesh"] != bytes(16) else None
                    for s in rec["submeshes"]:
                        out["submeshes"] += 1; out["clothing"] += 1; out["simulated"] += "uses_cloth_simulation" in s["flags"]
                        try: out["clothing_identical"] += pack_clothing(parse_clothing(s["params"])) == s["params"]; out["named_materials"] += bool(parse_clothing(s["params"])["name"])
                        except Exception as ex: fail("clothing: %s" % str(ex)[:40], where)
                    for e in it["entries"]:
                        ty = bytes(e["rest"][16:32])
                        if ty not in (CLOTH_MAP, CLOTH_COOK): continue
                        if limit and out["maps"] + out["cooks"] >= limit: return out
                        s = subs.get(bytes(e["rest"][0:16])); raw = _read(m, e)
                        if struct.pack("<Q", tpac.xxh64(raw)) != e["rest"][32:40]: fail("hash", where)
                        if e["rest"][40:] != struct.pack("<IB", 0 if ty == CLOTH_MAP else 1, e["stored"] < e["raw"]): fail("row", where)
                        try:
                            if ty == CLOTH_MAP:
                                out["maps"] += 1; d = parse_map(raw); same = pack_map(d) == raw; out["maps_identical"] += same
                                if not same: fail("map repack", where)
                                npos = None
                                if px: npos = max(px[2]) if px[2] else None; out["maps_checked_against_proxy"] += 1
                                bad = map_problems(d, s["vertices"] if s else None, npos)
                                if bad: out["map_problems"] += 1; [fail("map: " + b_, where) for b_ in bad]
                            else:
                                out["cooks"] += 1; d = parse_cook(raw); same = pack_cook(d) == raw; out["cooks_identical"] += same
                                if not same: fail("cook repack", where)
                                try: parse_fabric(raw); out["cooks_layout"] += 1
                                except (ValueError, struct.error): fail("cook: fabric layout", where)
                                bad = cook_problems(d, s["positions"] if s else None, [s["bbox_min"], s["bbox_max"]] if s else None)
                                if bad: out["cook_problems"] += 1; [fail("cook: " + b_, where) for b_ in bad]
                        except Exception as ex:
                            fail("%s: %s" % ("map" if ty == CLOTH_MAP else "cook", str(ex)[:40]), where)
            finally:
                m.close()
        if log: log("%-48s maps %d cooks %d (%.0f s)" % (os.path.basename(p), out["maps"], out["cooks"], time.time() - t0))
    return out


def _metameshes(path):
    """(item guid, name, record, {(owner, data type): raw bytes}) of every metamesh in a package that has cloth data or is a cloth proxy."""
    with open(path, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if m[:4] != b"TPAC": return
            meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
            for it in pk["items"]:
                if it["type"] != sdk_mesh.META_TYPE: continue
                yield it["guid"], it["name"].decode("utf-8", "replace"), it["record"], \
                    {(bytes(e["rest"][0:16]), bytes(e["rest"][16:32])): e for e in it["entries"]}, m
        finally:
            m.close()


def recook(paths=None, limit=None, max_particles=None, map_limit=0, log=None):
    """Rebuild shipped cloth data from its sources and compare byte for byte: every cloth cook from its submesh's edit data (cook_fabric), and,
    when map_limit > 0, every cloth map whose render vertices x proxy triangles <= map_limit from the render submesh's and the proxy's edit
    data and the proxy's shipped cook (map_cloth). limit counts cooks + maps looked at; max_particles skips larger cooks. Returns counts:
    cooks, cooks_editor (identical with the Modding Kit editor's constants), cooks_other (identical with another build's constants), cook_diff
    {differing part: count}, cooks_skipped, maps, maps_identical, maps_index_identical (same triangles and order), maps_skipped, examples."""
    files = list(paths) if paths else packages(); proxies = proxy_index(files) if map_limit else {}
    out = dict(cooks=0, cooks_editor=0, cooks_other=0, cook_diff={}, cooks_skipped=0, maps=0, maps_identical=0, maps_index_identical=0,
               maps_skipped=0, examples=[]); cache = {}; t0 = time.time()
    def proxy_data(g):
        if g not in cache:
            cache[g] = None
            if g in proxies:
                for guid, name, rec, ents, m in _metameshes(proxies[g][0]):
                    if guid != g: continue
                    subs = tw_formats.parse_metamesh_record(rec)["submeshes"]
                    if len(subs) == 1:
                        e, c = ents.get((subs[0]["guid"], sdk_mesh.EDIT_DATA)), ents.get((subs[0]["guid"], CLOTH_COOK))
                        if e and c: cache[g] = (_read(m, e), _read(m, c))
        return cache[g]
    for p in files:
        for guid, name, recb, ents, m in _metameshes(p):
            kinds = [k for k in ents if k[1] in (CLOTH_COOK, CLOTH_MAP)]
            if not kinds: continue
            rec = tw_formats.parse_metamesh_record(recb)
            for owner, ty in kinds:
                if limit and out["cooks"] + out["maps"] + out["cooks_skipped"] + out["maps_skipped"] >= limit: return out
                edit = ents.get((owner, sdk_mesh.EDIT_DATA)); raw = _read(m, ents[(owner, ty)])
                if edit is None: out["cooks_skipped" if ty == CLOTH_COOK else "maps_skipped"] += 1; continue
                E = sdk_mesh.parse_edit(_read(m, edit))
                if ty == CLOTH_COOK:
                    if max_particles and len(E["P"]) > max_particles: out["cooks_skipped"] += 1; continue
                    fab = cook_fabric(E["P"], E["V"]["pi"], E["V"]["c0"], E["F"], E["W"] if len(E["W"]) else None)
                    out["cooks"] += 1
                    if fab is None: out["cook_diff"]["no simulated vertex"] = out["cook_diff"].get("no simulated vertex", 0) + 1; continue
                    build, diff = recook_matches(raw, fab)
                    if build == "editor": out["cooks_editor"] += 1
                    elif build: out["cooks_other"] += 1
                    else:
                        for k in diff: out["cook_diff"][k] = out["cook_diff"].get(k, 0) + 1
                        if len(out["examples"]) < 8: out["examples"].append("cook %s: %s" % (name, ", ".join(diff)))
                else:
                    px = proxy_data(rec["cloth_mesh"]) if map_limit else None
                    if px is None: out["maps_skipped"] += 1; continue
                    S = sdk_mesh.parse_edit(px[0])
                    if len(E["V"]) * len(S["F"]) > map_limit: out["maps_skipped"] += 1; continue
                    mp = map_cloth(E, S, px[1]); out["maps"] += 1; sh = parse_map(raw)
                    same = pack_map(mp) == raw; out["maps_identical"] += same
                    out["maps_index_identical"] += len(sh["index"]) == len(mp["index"]) and bool((sh["index"] == mp["index"]).all())
                    if not same and len(out["examples"]) < 8: out["examples"].append("map %s" % name)
        if log: log("%-48s cooks %d maps %d (%.0f s)" % (os.path.basename(p), out["cooks"], out["maps"], time.time() - t0))
    return out


def _find_mesh(name):
    """(item guid, record, {(owner, type): raw}) of a shipped metamesh by name, and the same for its cloth proxy (or None)."""
    rel = _meshread().index().get(name)
    if rel is None: return None, None
    def load(path, want_name=None, want_guid=None):
        for guid, nm, rec, ents, m in _metameshes(path):
            if (want_name and nm == want_name) or (want_guid and guid == want_guid):
                return guid, nm, tw_formats.parse_metamesh_record(rec), {k: _read(m, e) for k, e in ents.items() if k[1] in (sdk_mesh.EDIT_DATA, CLOTH_MAP, CLOTH_COOK)}
    item = load(os.path.join(config.MODULES, rel), want_name=name); proxy = None
    if item and item[2]["cloth_mesh"] != bytes(16):
        px = proxy_index(packages()).get(item[2]["cloth_mesh"])
        if px: proxy = load(px[0], want_guid=item[2]["cloth_mesh"])
    return item, proxy


def cook_command(name, out_dir=None, build="editor", maps=True):
    """Cook every simulated submesh of a shipped metamesh (and map its render submeshes on its proxy), write the bytes to out_dir, compare
    each with the shipped entry. Returns 0 when everything written equals the shipped bytes."""
    item, proxy = _find_mesh(name)
    if item is None: print("no metamesh named", name); return 1
    out_dir = out_dir or os.path.join(config.SDK_STAGE_DIR, "cloth"); os.makedirs(out_dir, exist_ok=True); bad = 0; done = 0
    def cook_item(it, proxy=False):
        nonlocal bad, done
        res = {}
        for s in it[2]["submeshes"]:
            edit = it[3].get((s["guid"], sdk_mesh.EDIT_DATA))
            # the editor cooks a cloth proxy and the submeshes simulated in place (banners, flags: no proxy); submeshes of a mesh with a proxy
            # follow it by a map
            in_place = it[2]["cloth_mesh"] == bytes(16) and "uses_cloth_simulation" in s["flags"]
            if edit is None or not (proxy or in_place or (s["guid"], CLOTH_COOK) in it[3]): continue
            E = sdk_mesh.parse_edit(edit); fab = cook_fabric(E["P"], E["V"]["pi"], E["V"]["c0"], E["F"], E["W"] if len(E["W"]) else None)
            if fab is None: continue
            b = pack_fabric(fab, build); res[s["guid"]] = b; done += 1
            fn = os.path.join(out_dir, s["name"] + ".cloth_cook"); open(fn, "wb").write(b)
            shipped = it[3].get((s["guid"], CLOTH_COOK)); same = shipped == b
            matched = recook_matches(shipped, fab) if shipped is not None and not same else (build if same else None, [])
            bad += shipped is not None and not matched[0]
            print("cook %-40s particles %d, constraints %d, %d sets, first simulated %d -> %s; shipped: %s" % (
                s["name"], fab["header"][2], fab["header"][1], len(fab["sets"]), fab["header"][3], fn,
                "none" if shipped is None else "identical" if same else ("identical with build constants %s" % matched[0]) if matched[0] else "differs in " + ", ".join(matched[1])))
        return res
    cook_item(item)
    if maps and proxy is not None:
        psub = proxy[2]["submeshes"]
        if len(psub) != 1: print("proxy %s has %d submeshes; the editor maps only on a single-submesh proxy" % (proxy[1], len(psub))); return 1
        pcook = cook_item(proxy, True).get(psub[0]["guid"]); pedit = proxy[3].get((psub[0]["guid"], sdk_mesh.EDIT_DATA))
        for s in item[2]["submeshes"]:
            shipped = item[3].get((s["guid"], CLOTH_MAP)); edit = item[3].get((s["guid"], sdk_mesh.EDIT_DATA))
            if shipped is None or edit is None or pcook is None or pedit is None: continue
            t0 = time.time(); b = pack_map(map_cloth(edit, pedit, pcook)); done += 1
            fn = os.path.join(out_dir, s["name"] + ".cloth_map"); open(fn, "wb").write(b); bad += b != shipped
            print("map  %-40s %d vertices on %s -> %s; shipped: %s (%.0f s)" % (s["name"], struct.unpack_from("<I", b, 0)[0] // 3,
                  proxy[1], fn, "identical" if b == shipped else "differs", time.time() - t0))
    if not done: print("%s: no simulated submesh with a simulated vertex (vertex colour alpha), no proxy to map on" % name)
    return 1 if bad else 0


# ---------------------------------------------------------------------------------------------------------- command line
def info(name):
    meshread = _meshread(); rel = meshread.index().get(name)
    if rel is None: print("no metamesh named", name); return 1
    with open(os.path.join(config.MODULES, rel), "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
            for it in pk["items"]:
                if it["type"] != sdk_mesh.META_TYPE or it["name"].decode("utf-8", "replace") != name: continue
                rec = tw_formats.parse_metamesh_record(it["record"]); subs = {s["guid"]: s for s in rec["submeshes"]}
                print("%s: proxy mesh %s, region %r" % (name, uuid.UUID(bytes_le=rec["cloth_mesh"]) if rec["cloth_mesh"] != bytes(16) else "none", rec["s0"]))
                for s in rec["submeshes"]:
                    c = parse_clothing(s["params"])
                    print("  %-34s flags %s  clothing %s" % (s["name"], s["flags"], ", ".join("%s %.3g" % (k.replace("_stiffness", ""), c[k]) for k in MATERIAL_FIELDS) + (" name %r" % c["name"] if c["name"] else "")))
                for e in it["entries"]:
                    ty = bytes(e["rest"][16:32])
                    if ty not in (CLOTH_MAP, CLOTH_COOK): continue
                    s = subs.get(bytes(e["rest"][0:16])); raw = _read(m, e)
                    if ty == CLOTH_MAP:
                        d = parse_map(raw); print("  cloth map of %s: %d vertices bound to proxy triangles (largest index %d)" % (s["name"] if s else "?", len(d["index"]), d["index"].max() if len(d["index"]) else -1))
                    else:
                        d = parse_fabric(raw); h = d["header"]
                        print("  cloth cook of %s: %d particles (simulated from %d, normal update from %d), %d constraints in %d sets, %d simulated triangles, %s" % (
                            s["name"] if s else "?", h[2], h[3], h[4], h[1], len(d["buffers"]["sets"]) // 32, h[5] // 3,
                            "skinned" if d["buffers"]["weights"] else "not skinned"))
                return 0
        finally:
            m.close()
    return 1

def _meshread():
    if __package__ in (None, ""):
        import meshread
    else:
        from . import meshread
    return meshread

def main(argv):
    vals = ("--limit", "--module", "--out", "--build", "--maps", "--max-particles")
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in vals)]; opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["info"] and len(a) == 2:
        config.require_game(); return info(a[1])
    if a[:1] == ["cook"] and len(a) == 2:
        config.require_game(); return cook_command(a[1], opt("--out"), opt("--build", "editor"), maps="--no-map" not in argv)
    if a[:1] == ["recook"]:
        config.require_game()
        r = recook([a[1]] if len(a) > 1 else packages(opt("--module")), int(opt("--limit", 0)) or None, int(opt("--max-particles", 0)) or None,
                   int(float(opt("--maps", 0))), log=print if "--verbose" in argv else None)
        print("cloth cooks re-cooked %d: byte-identical %d with the editor's constants, %d with another build's constants, %d differ (skipped %d)" % (
            r["cooks"], r["cooks_editor"], r["cooks_other"], r["cooks"] - r["cooks_editor"] - r["cooks_other"], r["cooks_skipped"]))
        for k, c in sorted(r["cook_diff"].items(), key=lambda x: -x[1]): print("  differing %-20s %d" % (k, c))
        if r["maps"] or r["maps_skipped"]: print("cloth maps rebuilt %d: byte-identical %d, same triangles %d (skipped %d)" % (r["maps"], r["maps_identical"], r["maps_index_identical"], r["maps_skipped"]))
        for e in r["examples"]: print("  e.g.", e)
        return 0 if r["cooks"] + r["maps"] and r["cooks_editor"] + r["cooks_other"] == r["cooks"] and r["maps_identical"] == r["maps"] else 1
    if a[:1] == ["check"]:
        config.require_game(); r = check([a[1]] if len(a) > 1 else packages(opt("--module")), int(opt("--limit", 0)) or None, log=print if "--verbose" in argv else None)
        print("submeshes %d: clothing material blocks byte-identical %d of %d (%d simulated, %d with a material name)" % (r["submeshes"], r["clothing_identical"], r["clothing"], r["simulated"], r["named_materials"]))
        print("cloth maps %d: byte-identical %d of %d, structure problems %d (proxy found for %d)" % (r["maps"], r["maps_identical"], r["maps"], r["map_problems"], r["maps_checked_against_proxy"]))
        print("cloth cooks %d: byte-identical %d of %d, fabric layout read exactly %d, header problems %d" % (r["cooks"], r["cooks_identical"], r["cooks"], r["cooks_layout"], r["cook_problems"]))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  fails %-30s %6d  e.g. %s" % (k, c, ex))
        ok = r["clothing_identical"] == r["clothing"] and r["maps_identical"] == r["maps"] and r["cooks_identical"] == r["cooks"] and not r["failures"]
        return 0 if ok and (r["maps"] or r["cooks"]) else 1
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
