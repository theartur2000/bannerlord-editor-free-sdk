"""Reader and writer for a scene's navmesh.bin (the walkable polygons the AI paths on).

Layout, worked out from the files of game v1.4.8.119303:

  "RNM1" u32 size of the rest
  u64 kind (3) u64 raw_size u64 stored_size     stored_size counts from here to the end of the file
  16 bytes that look like leftovers; kept as read, zero when built new
  LZ4 block -> payload:
      "NMG9" (also read and written: the older "NMG8" and "NMG7" that many shipped scenes still carry)
      u32 vertex count, then x y z as f32 per vertex
      u32 edge count, then per edge 6 x i32: -1, vertex a, vertex b, -1, -1, 0        (NMG7, NMG8: 5 x i32, without the last)
      u32 face count, then per face:
          i32 n (3 or 4), n x i32 vertex index, n x i32 edge index (edge i joins vertex i and vertex i + 1)
          i32 kind       2 in every file read so far
          i32 zero       0 in every file read so far
          i32 group      the face group. On the world map this is the campaign terrain type of the face
                         (TaleWorlds.Core.TerrainType: 1 plain, 2 desert, 4 forest, 5 steppe, 7 mountain, 8 lake, 10 water,
                         11 river, ...); 0 on the battlefields read so far
          f32 value      0.0 on most faces; 50.0 on a few faces of one battlefield (meaning not known)
          u32 flags      a bit mask; 0xFFFFFFFF on most battlefield faces, other values on the world map
          u8  zero       (absent in NMG7, where flags are small values such as 0x0F)
      u32 m (32), then m x 8 bytes (zero in every file read so far)

  Faces are convex, counter-clockwise seen from above. Every face of every shipped scene read (battlefields, towns, castles,
  villages, sieges, interiors, the world map) has 3 or 4 corners, and this reader refuses a face with more, so a large flat area
  is several faces. On a shipped battlefield neighbouring faces share whole edges (no vertex lies on the side of another face).
  Towns and sieges carry the same ground up to six times (group 0 and 1, flags 0x12 0x14 0x18 and 0x22 0x24 0x28), and the few
  faces whose group is an id such as 512 or 668 are the ones scene scripts switch on and off (gates, ladders, wall tops).
  mesh_cells() below builds a mesh of the battlefield kind from a height field.
"""
import math, os, struct, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tpac
else:
    from . import tpac


class NavMesh:
    def __init__(self, path=None):
        self.version = 9
        self.vertices = np.zeros((0, 3), np.float32)
        self.edges = np.zeros((0, 2), np.int32)
        self.faces = []            # list of (vertex indices tuple, edge indices tuple)
        self.face_kind = []        # i32 per face (2)
        self.face_zero = []        # i32 per face (0)
        self.face_group = []       # i32 per face: face group; the campaign terrain type on the world map
        self.face_value = []       # 4 bytes per face (an f32)
        self.face_flags = []       # u32 per face
        self.face_end = []         # u8 per face (0)
        self.trailer = struct.pack("<I", 32) + bytes(256)
        self.prefix = bytes(16)
        self.edge_extra = None     # the four constant columns of the edge records, when read from a file
        if path: self.read(path)

    # ---- reading
    def read(self, path):
        with open(path, "rb") as f: d = f.read()
        assert d[:4] == b"RNM1", "not a navmesh.bin"
        size, = struct.unpack_from("<I", d, 4); assert size == len(d) - 8, ("navmesh.bin size field", size, len(d))
        kind, raw, stored = struct.unpack_from("<3Q", d, 8)
        assert kind == 3 and stored == size, (kind, stored, size)
        self.prefix = d[32:48]
        self.parse(tpac.lz4_decompress(d[48:8 + stored], raw))
        return self

    def parse(self, pay):
        assert pay[:3] == b"NMG" and pay[3:4] in (b"7", b"8", b"9"), pay[:4]
        self.version = int(pay[3:4]); ec = 6 if self.version >= 9 else 5; has_end = self.version >= 8
        nv, = struct.unpack_from("<I", pay, 4)
        self.vertices = np.frombuffer(pay, "<f4", nv * 3, 8).reshape(nv, 3).copy()
        o = 8 + nv * 12
        ne, = struct.unpack_from("<I", pay, o)
        e = np.frombuffer(pay, "<i4", ne * ec, o + 4).reshape(ne, ec)
        self.edges = e[:, 1:3].copy(); self.edge_extra = e[:, [0] + list(range(3, ec))].copy()
        o += 4 + ne * ec * 4
        nf, = struct.unpack_from("<I", pay, o); o += 4
        self.faces = []; self.face_kind = []; self.face_zero = []; self.face_group = []; self.face_value = []; self.face_flags = []; self.face_end = []
        for _ in range(nf):
            c, = struct.unpack_from("<i", pay, o)
            assert 3 <= c <= 4, ("face with %d corners at offset %d" % (c, o))
            vs = struct.unpack_from("<%di" % c, pay, o + 4); es = struct.unpack_from("<%di" % c, pay, o + 4 + 4 * c)
            t = o + 4 + 8 * c
            kind, zero, group, value, flags = struct.unpack_from("<3ifI", pay, t)
            end = pay[t + 20] if has_end else 0
            self.faces.append((vs, es)); self.face_kind.append(kind); self.face_zero.append(zero); self.face_group.append(group)
            self.face_value.append(pay[t + 12:t + 16]); self.face_flags.append(flags); self.face_end.append(end)
            o = t + (21 if has_end else 20)
        self.trailer = pay[o:]
        return self

    # ---- writing
    def payload(self):
        ec = 6 if self.version >= 9 else 5; has_end = self.version >= 8
        out = [b"NMG" + str(self.version).encode(), struct.pack("<I", len(self.vertices)), np.ascontiguousarray(self.vertices, "<f4").tobytes()]
        ne = len(self.edges)
        e = np.empty((ne, ec), "<i4")
        if self.edge_extra is not None and len(self.edge_extra) == ne and self.edge_extra.shape[1] == ec - 2: e[:, [0] + list(range(3, ec))] = self.edge_extra
        else:
            e[:, 0] = -1; e[:, 3] = -1; e[:, 4] = -1
            if ec == 6: e[:, 5] = 0
        e[:, 1:3] = self.edges
        out += [struct.pack("<I", ne), e.tobytes(), struct.pack("<I", len(self.faces))]
        for k, (vs, es) in enumerate(self.faces):
            c = len(vs)
            out.append(struct.pack("<i%di%di" % (c, c), c, *vs, *es))
            out.append(struct.pack("<3i", self.face_kind[k], self.face_zero[k], self.face_group[k]) + self.face_value[k]
                       + (struct.pack("<IB", self.face_flags[k], self.face_end[k]) if has_end else struct.pack("<I", self.face_flags[k])))
        out.append(self.trailer)
        return b"".join(out)

    def tobytes(self, lz4=None):
        pay = self.payload(); z = (lz4 or tpac.lz4_compress)(pay)
        stored = 40 + len(z)
        return b"RNM1" + struct.pack("<I", stored) + struct.pack("<3Q", 3, len(pay), stored) + self.prefix + z

    def save(self, path, lz4=None):
        with open(path, "wb") as f: f.write(self.tobytes(lz4))

    # ---- building
    @classmethod
    def from_polygons(cls, vertices, polygons, group=0, flags=0xFFFFFFFF):
        """A navmesh from a vertex array (n, 3) and polygons (tuples of 3 or 4 vertex indices, counter-clockwise from
        above). Edges are created and shared between neighbours. group and flags: one value for every face, or one per face
        (the face group is the id scripts and the AI know an area by; 0xFFFFFFFF is the flags value of most battlefield faces)."""
        nm = cls(); nm.vertices = np.asarray(vertices, np.float32).reshape(-1, 3)
        index = {}; edges = []
        gs = list(group) if hasattr(group, "__len__") else [group] * len(polygons)
        fl = list(flags) if hasattr(flags, "__len__") else [flags] * len(polygons)
        assert len(gs) == len(polygons) and len(fl) == len(polygons), "group and flags: one value, or one per polygon"
        for n, poly in enumerate(polygons):
            c = len(poly); assert c in (3, 4)
            es = []
            for i in range(c):
                a, b = int(poly[i]), int(poly[(i + 1) % c]); key = (a, b) if a < b else (b, a)
                j = index.get(key)
                if j is None: j = index[key] = len(edges); edges.append((a, b))
                es.append(j)
            nm.faces.append((tuple(int(v) for v in poly), tuple(es)))
            nm.face_kind.append(2); nm.face_zero.append(0); nm.face_group.append(int(gs[n])); nm.face_value.append(bytes(4))
            nm.face_flags.append(int(fl[n])); nm.face_end.append(0)
        nm.edges = np.asarray(edges, np.int32).reshape(-1, 2)
        return nm

    @classmethod
    def from_height_field(cls, z, cell, keep, k, regions=None, region_flags=None, height_tol=0.5, max_size=32.0):
        """A mesh of large convex faces over the cells of `keep`: see mesh_cells(), which this wraps. The group of a face is
        its region id and its flags are region_flags[id] ({id: u32}), else 0xFFFFFFFF."""
        V, polys, groups = mesh_cells(z, cell, k, keep, regions, height_tol, max_size)
        rf = region_flags or {}
        return cls.from_polygons(V, polys, groups, [rf.get(g, 0xFFFFFFFF) for g in groups])

    # ---- measuring
    def problems(self, eps=1e-6):
        """What is wrong with the geometry, as counts (all zero: a mesh made the way shipped battlefields are):
        not_convex          faces that are not strictly convex and counter-clockwise seen from above (zero area faces and a
                            corner between two collinear sides count)
        bad_edges           face edges that do not join corner i and corner i + 1
        edge_over_two       edges that more than two faces use
        edge_same_way       edges two faces both run along the same way (one of them is turned the wrong way)
        t_junctions         edges used by one face that have another vertex on them: the face next door is not joined along
                            the whole edge
        duplicate_vertices  vertices at the same place as another one
        unused_vertices     vertices no face uses"""
        V = self.vertices.astype(np.float64); bad_c = bad_e = 0; use = {}; way = {}
        for vs, es in self.faces:
            c = len(vs); p = V[list(vs)][:, :2]; d = np.roll(p, -1, 0) - p; d2 = np.roll(d, -1, 0)
            if not (d[:, 0] * d2[:, 1] - d[:, 1] * d2[:, 0] > eps).all(): bad_c += 1
            for i in range(c):
                a, b = int(self.edges[es[i]][0]), int(self.edges[es[i]][1])
                if {a, b} != {vs[i], vs[(i + 1) % c]}: bad_e += 1
                use[es[i]] = use.get(es[i], 0) + 1; way.setdefault(es[i], []).append((vs[i], vs[(i + 1) % c]))
        used = sorted({v for f in self.faces for v in f[0]}); pos = {}
        for i in range(len(V)): pos.setdefault(tuple(np.round(V[i], 3)), []).append(i)
        # a vertex on the inside of a one-sided edge, found through a hash of the vertices in 8 m squares
        grid = {}
        for i in used: grid.setdefault((int(V[i, 0] // 8), int(V[i, 1] // 8)), []).append(i)
        tj = 0
        for e, c in use.items():
            if c != 1: continue
            a, b = (int(x) for x in self.edges[e]); d = V[b, :2] - V[a, :2]; L2 = float(d @ d)
            if L2 < 1e-12: continue
            x0, x1 = sorted((V[a, 0], V[b, 0])); y0, y1 = sorted((V[a, 1], V[b, 1])); hit = False
            for gx in range(int((x0 - 0.1) // 8), int((x1 + 0.1) // 8) + 1):
                for gy in range(int((y0 - 0.1) // 8), int((y1 + 0.1) // 8) + 1):
                    for i in grid.get((gx, gy), ()):
                        if i == a or i == b: continue
                        t = float((V[i, :2] - V[a, :2]) @ d) / L2
                        if 0.001 < t < 0.999 and math.hypot(*(V[i, :2] - V[a, :2] - t * d)) < 0.01: hit = True; break
                    if hit: break
                if hit: break
            tj += hit
        return {"not_convex": bad_c, "bad_edges": bad_e, "edge_over_two": sum(1 for c in use.values() if c > 2),
                "edge_same_way": sum(1 for w in way.values() if len(w) == 2 and w[0] == w[1]), "t_junctions": tj,
                "duplicate_vertices": sum(len(v) - 1 for v in pos.values() if len(v) > 1), "unused_vertices": len(V) - len(used)}

    def components(self):
        """Number of groups of faces joined through shared edges (1: one connected walkable area)."""
        parent = list(range(len(self.faces)))
        def find(a):
            while parent[a] != a: parent[a] = parent[parent[a]]; a = parent[a]
            return a
        seen = {}
        for n, (vs, es) in enumerate(self.faces):
            for e in es:
                m = seen.setdefault(e, n)
                if m != n: parent[find(n)] = find(m)
        return len({find(n) for n in range(len(self.faces))})

    def height_error(self, z, cell):
        """How far the ground is from the faces, in metres: {"max", "mean", "p99", "samples"} of |ground - face| over the height
        samples z (row = y, `cell` metres apart) that lie inside a face. A quad is read as two triangles, split along the
        diagonal that fits better, because it is not known how the game reads a quad that is not flat."""
        V = self.vertices.astype(np.float64); errs = []
        for vs, _ in self.faces:
            p = V[list(vs)]; j0 = max(0, int(math.ceil(p[:, 1].min() / cell))); j1 = min(z.shape[0] - 1, int(math.floor(p[:, 1].max() / cell)))
            i0 = max(0, int(math.ceil(p[:, 0].min() / cell))); i1 = min(z.shape[1] - 1, int(math.floor(p[:, 0].max() / cell)))
            if j1 < j0 or i1 < i0: continue
            gy, gx = np.mgrid[j0:j1 + 1, i0:i1 + 1]; X = gx * cell; Y = gy * cell; G = z[j0:j1 + 1, i0:i1 + 1]; best = None
            for tris in (((0, 1, 2), (0, 2, 3)), ((1, 2, 3), (1, 3, 0))) if len(vs) == 4 else (((0, 1, 2),),):
                e = np.full(G.shape, np.inf)
                for a, b, c in tris:
                    (xa, ya, za), (xb, yb, zb), (xc, yc, zc) = p[a], p[b], p[c]
                    den = (yb - yc) * (xa - xc) + (xc - xb) * (ya - yc)
                    if abs(den) < 1e-12: continue
                    l1 = ((yb - yc) * (X - xc) + (xc - xb) * (Y - yc)) / den; l2 = ((yc - ya) * (X - xc) + (xa - xc) * (Y - yc)) / den; l3 = 1 - l1 - l2
                    e = np.where((l1 >= -1e-9) & (l2 >= -1e-9) & (l3 >= -1e-9), np.abs(G - (l1 * za + l2 * zb + l3 * zc)), e)
                if best is None or e[np.isfinite(e)].max(initial=0) < best[np.isfinite(best)].max(initial=0): best = e
            errs.append(best[np.isfinite(best)])
        e = np.concatenate(errs) if errs else np.zeros(1)
        return {"max": float(e.max()), "mean": float(e.mean()), "p99": float(np.percentile(e, 99)), "samples": int(e.size)}

    def stats(self):
        v = self.vertices
        area = 0.0
        for vs, _ in self.faces:
            p = v[list(vs)][:, :2]; x, y = p[:, 0], p[:, 1]
            area += float((x * np.roll(y, -1) - np.roll(x, -1) * y).sum()) / 2
        return {"vertices": len(v), "edges": len(self.edges), "faces": len(self.faces),
                "quads": sum(len(f[0]) == 4 for f in self.faces), "area_m2": round(area, 1),
                "groups": sorted(set(self.face_group))[:24], "flags": [hex(x) for x in sorted(set(self.face_flags))[:8]]}


# ---------------------------------------------------------------- meshing a height field

def convex_pieces(pts, eps=1e-6):
    """Cuts a convex outline into the fewest strictly convex pieces of 3 or 4 corners, using only its own vertices.
    pts: [(x, y), ...] counter-clockwise; corners lying on a straight side are allowed (they are why the outline needs cutting:
    each of them is a vertex of the face next door). Returns lists of indices into pts. Among the cuts with the fewest pieces the
    one whose worst piece is the roundest (4 pi area / perimeter^2) is taken."""
    def cross(a, b, c): return (pts[b][0] - pts[a][0]) * (pts[c][1] - pts[b][1]) - (pts[b][1] - pts[a][1]) * (pts[c][0] - pts[b][0])
    def strict(ix): return all(cross(ix[i - 1], ix[i], ix[(i + 1) % len(ix)]) > eps for i in range(len(ix)))
    def round_(ix):
        q = [pts[i] for i in ix]; a = per = 0.0
        for i in range(len(q)):
            (x0, y0), (x1, y1) = q[i], q[(i + 1) % len(q)]; a += x0 * y1 - x1 * y0; per += math.hypot(x1 - x0, y1 - y0)
        return 2 * math.pi * a / (per * per)
    memo = {}
    def best(arc):                  # arc: consecutive indices; the polygon closes from the last back to the first
        if arc in memo: return memo[arc]
        r = None
        if len(arc) <= 4 and strict(arc): r = (1, round_(arc), [arc])
        elif len(arc) >= 4:
            for i in range(len(arc)):
                for j in range(i + 2, len(arc)):
                    if i == 0 and j == len(arc) - 1: continue
                    s1, s2 = best(arc[i:j + 1]), best(arc[j:] + arc[:i + 1])
                    if s1 is None or s2 is None: continue
                    c = (s1[0] + s2[0], min(s1[1], s2[1]), s1[2] + s2[2])
                    if r is None or (c[0], -c[1]) < (r[0], -r[1]): r = c
        memo[arc] = r
        return r
    return [list(p) for p in best(tuple(range(len(pts))))[2]]


def _fit(z, S, by, bx):
    """Per block of S x S height samples (by x bx blocks): the largest distance of a sample from the bilinear surface through the
    block's four corner samples, and the block's twist: half the gap between the two diagonals' middle heights (0 for a flat quad)."""
    c = z[0:by * S + 1:S, 0:bx * S + 1:S]; c00, c01, c10, c11 = c[:-1, :-1], c[:-1, 1:], c[1:, :-1], c[1:, 1:]
    t = np.arange(S + 1) / S; top = c00[..., None] * (1 - t) + c01[..., None] * t; bot = c10[..., None] * (1 - t) + c11[..., None] * t
    dev = np.zeros((by, bx)); W = bx * S + 1
    for u in range(S + 1):
        rows = z[u:u + (by - 1) * S + 1:S, :W]
        view = np.lib.stride_tricks.as_strided(rows, (by, bx, S + 1), (rows.strides[0], S * rows.strides[1], rows.strides[1]))
        s = u / S; dev = np.maximum(dev, np.abs(view - ((1 - s) * top + s * bot)).max(axis=2))
    return dev, np.abs(c00 + c11 - c01 - c10) / 2


def mesh_cells(z, cell, k, keep, regions=None, height_tol=0.5, max_size=32.0):
    """Large convex faces over the cells of `keep`, following the ground. Returns (vertices (n, 3), polygons, group per polygon).

    z         height samples in metres (row = y, `cell` metres apart); a mesh cell is k x k samples, so its side is k * cell
    keep      bool (gy, gx): the mesh cells that may be walked on; exactly these are covered, no more, no less
    regions   int (gy, gx) region id per cell, or None for 0: a face never spans two ids, and the id becomes the face group
    height_tol  metres: a square of cells becomes one face only when every height sample in it lies within this of the
              bilinear surface through its four corners and the square's corners are within this of being flat
    max_size  metres: the longest side of a face

    How: a quadtree. Squares of 2^n cells are tried largest first; the first that lies wholly in `keep`, in one region and on
    planar ground is a face. Neighbouring squares are then made to differ by at most one level, so that the side of a large
    square meets at most two smaller squares. Those meeting points are real vertices of the large square, so it is cut into
    triangles and quads that use them (convex_pieces), and every edge is shared by whole with the face next door."""
    gy, gx = keep.shape; rid = np.zeros(keep.shape, np.int64) if regions is None else np.asarray(regions, np.int64)
    if not keep.any(): return np.zeros((0, 3)), [], []
    step = k * cell; K = 0
    while (2 << K) * step <= max_size + 1e-9 and (2 << K) <= min(gy, gx): K += 1
    valid = [keep]
    for lv in range(1, K + 1):
        s = 1 << lv; by, bx = gy >> lv, gx >> lv
        ok = keep[:by * s, :bx * s].reshape(by, s, bx, s).all(axis=(1, 3))
        r = rid[:by * s, :bx * s].reshape(by, s, bx, s); ok &= r.min(axis=(1, 3)) == r.max(axis=(1, 3))
        dev, twist = _fit(z, s * k, by, bx)
        valid.append(ok & (dev <= height_tol) & (twist <= height_tol))
    lev = -np.ones((gy, gx), np.int8)
    for lv in range(K, -1, -1):
        s = 1 << lv; v = np.kron(valid[lv], np.ones((s, s), bool)); full = np.zeros((gy, gx), bool); full[:v.shape[0], :v.shape[1]] = v
        lev[full & (lev < 0)] = lv
    # neighbouring squares differ by at most one level: split the larger one, and look again (a split can cause another)
    changed = True
    while changed:
        changed = False
        for lv in range(K, 1, -1):
            m = lev == lv; bad = np.zeros_like(m)
            for a, b in (((slice(1, None), slice(None)), (slice(None, -1), slice(None))), ((slice(None, -1), slice(None)), (slice(1, None), slice(None))),
                         ((slice(None), slice(1, None)), (slice(None), slice(None, -1))), ((slice(None), slice(None, -1)), (slice(None), slice(1, None)))):
                nb = lev[a]; bad[b] |= m[b] & (nb >= 0) & (nb <= lv - 2)
            if bad.any():
                jj, ii = np.nonzero(bad)
                for j0, i0 in {((j >> lv) << lv, (i >> lv) << lv) for j, i in zip(jj.tolist(), ii.tolist())}: lev[j0:j0 + (1 << lv), i0:i0 + (1 << lv)] = lv - 1
                changed = True
    J, I = np.mgrid[0:gy, 0:gx]; leaves = []
    for lv in range(K + 1):
        s = 1 << lv; a = (lev == lv) & (J % s == 0) & (I % s == 0)
        leaves += [(lv, j, i) for j, i in zip(*(x.tolist() for x in np.nonzero(a)))]
    leaves.sort(key=lambda t: (t[1], t[2]))
    used = np.zeros((gy + 1, gx + 1), bool)
    for lv, j, i in leaves: s = 1 << lv; used[j, i] = used[j, i + s] = used[j + s, i] = used[j + s, i + s] = True
    vid = -np.ones((gy + 1, gx + 1), np.int64); verts = []; polys = []; groups = []; cut = {}
    def vtx(j, i):
        if vid[j, i] < 0: vid[j, i] = len(verts); verts.append((i * step, j * step, float(z[j * k, i * k])))
        return int(vid[j, i])
    for lv, j, i in leaves:
        s = 1 << lv; ring = [(j, i)]
        if s > 1:       # the nodes of other squares on this square's sides, in counter-clockwise order
            ring += [(j, c) for c in (np.nonzero(used[j, i + 1:i + s])[0] + i + 1).tolist()] + [(j, i + s)]
            ring += [(r, i + s) for r in (np.nonzero(used[j + 1:j + s, i + s])[0] + j + 1).tolist()] + [(j + s, i + s)]
            ring += [(j + s, c) for c in (np.nonzero(used[j + s, i + 1:i + s])[0] + i + 1).tolist()[::-1]] + [(j + s, i)]
            ring += [(r, i) for r in (np.nonzero(used[j + 1:j + s, i])[0] + j + 1).tolist()[::-1]]
        else: ring += [(j, i + 1), (j + 1, i + 1), (j + 1, i)]
        if len(ring) == 4: pieces = [[0, 1, 2, 3]]
        else:
            key = tuple((r - j, c - i) for r, c in ring)
            if key not in cut: cut[key] = convex_pieces([(c - i, r - j) for r, c in ring])
            pieces = cut[key]
        for p in pieces:
            polys.append(tuple(vtx(*ring[n]) for n in p)); groups.append(int(rid[j, i]))
    return np.asarray(verts, np.float64).reshape(-1, 3), polys, groups


def check(path):
    """True when the payload parsed and written again is byte-identical (the LZ4 block itself is not compared)."""
    with open(path, "rb") as f: d = f.read()
    kind, raw, stored = struct.unpack_from("<3Q", d, 8)
    pay = tpac.lz4_decompress(d[48:8 + stored], raw)
    nm = NavMesh().parse(pay)
    return nm.payload() == pay


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "info":
        print(NavMesh(a[1]).stats())
    elif len(a) == 2 and a[0] == "check":
        print("payload identical" if check(a[1]) else "DIFFERS")
    elif len(a) == 2 and a[0] == "problems":
        nm = NavMesh(a[1]); print(nm.problems(), "connected areas:", nm.components())
    else:
        print("navmesh.py info <navmesh.bin> | check <navmesh.bin> | problems <navmesh.bin>")
