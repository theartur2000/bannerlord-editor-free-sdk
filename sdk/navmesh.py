"""Reader and writer for a scene's navmesh.bin (the walkable polygons the AI paths on).

Layout, worked out from the files of game v1.4.8.119303:

  "RNM1" u32 size of the rest
  u64 kind (3) u64 raw_size u64 stored_size     stored_size counts from here to the end of the file
  16 bytes that look like leftovers; kept as read, zero when built new
  LZ4 block -> payload:
      "NMG9"
      u32 vertex count, then x y z as f32 per vertex
      u32 edge count, then per edge 6 x i32: -1, vertex a, vertex b, -1, -1, 0
      u32 face count, then per face:
          i32 n (3 or 4), n x i32 vertex index, n x i32 edge index (edge i joins vertex i and vertex i + 1)
          i32 kind       2 in every file read so far
          i32 zero       0 in every file read so far
          i32 group      the face group. On the world map this is the campaign terrain type of the face
                         (TaleWorlds.Core.TerrainType: 1 plain, 2 desert, 4 forest, 5 steppe, 7 mountain, 8 lake, 10 water,
                         11 river, ...); 0 on the battlefields read so far
          f32 value      0.0 on most faces; 50.0 on a few faces of one battlefield (meaning not known)
          u32 flags      a bit mask; 0xFFFFFFFF on most battlefield faces, other values on the world map
          u8  zero
      u32 m (32), then m x 8 bytes (zero in every file read so far)

  Faces are convex, counter-clockwise seen from above.
"""
import os, struct, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tpac
else:
    from . import tpac


class NavMesh:
    def __init__(self, path=None):
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
        size, = struct.unpack_from("<I", d, 4); assert size == len(d) - 8, (size, len(d))
        kind, raw, stored = struct.unpack_from("<3Q", d, 8)
        assert kind == 3 and stored == size, (kind, stored, size)
        self.prefix = d[32:48]
        self.parse(tpac.lz4_decompress(d[48:8 + stored], raw))
        return self

    def parse(self, pay):
        assert pay[:4] == b"NMG9", pay[:4]
        nv, = struct.unpack_from("<I", pay, 4)
        self.vertices = np.frombuffer(pay, "<f4", nv * 3, 8).reshape(nv, 3).copy()
        o = 8 + nv * 12
        ne, = struct.unpack_from("<I", pay, o)
        e = np.frombuffer(pay, "<i4", ne * 6, o + 4).reshape(ne, 6)
        self.edges = e[:, 1:3].copy(); self.edge_extra = e[:, [0, 3, 4, 5]].copy()
        o += 4 + ne * 24
        nf, = struct.unpack_from("<I", pay, o); o += 4
        self.faces = []; self.face_kind = []; self.face_zero = []; self.face_group = []; self.face_value = []; self.face_flags = []; self.face_end = []
        for _ in range(nf):
            c, = struct.unpack_from("<i", pay, o)
            assert 3 <= c <= 4, ("face with %d corners at offset %d" % (c, o))
            vs = struct.unpack_from("<%di" % c, pay, o + 4); es = struct.unpack_from("<%di" % c, pay, o + 4 + 4 * c)
            t = o + 4 + 8 * c
            kind, zero, group, value, flags, end = struct.unpack_from("<3ifIB", pay, t)
            self.faces.append((vs, es)); self.face_kind.append(kind); self.face_zero.append(zero); self.face_group.append(group)
            self.face_value.append(pay[t + 12:t + 16]); self.face_flags.append(flags); self.face_end.append(end)
            o = t + 21
        self.trailer = pay[o:]
        return self

    # ---- writing
    def payload(self):
        out = [b"NMG9", struct.pack("<I", len(self.vertices)), np.ascontiguousarray(self.vertices, "<f4").tobytes()]
        ne = len(self.edges)
        e = np.empty((ne, 6), "<i4")
        if self.edge_extra is not None and len(self.edge_extra) == ne: e[:, [0, 3, 4, 5]] = self.edge_extra
        else: e[:, 0] = -1; e[:, 3] = -1; e[:, 4] = -1; e[:, 5] = 0
        e[:, 1:3] = self.edges
        out += [struct.pack("<I", ne), e.tobytes(), struct.pack("<I", len(self.faces))]
        for k, (vs, es) in enumerate(self.faces):
            c = len(vs)
            out.append(struct.pack("<i%di%di" % (c, c), c, *vs, *es))
            out.append(struct.pack("<3i", self.face_kind[k], self.face_zero[k], self.face_group[k]) + self.face_value[k]
                       + struct.pack("<IB", self.face_flags[k], self.face_end[k]))
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
        above). Edges are created and shared between neighbours."""
        nm = cls(); nm.vertices = np.asarray(vertices, np.float32)
        index = {}; edges = []
        for poly in polygons:
            c = len(poly); assert c in (3, 4)
            es = []
            for i in range(c):
                a, b = int(poly[i]), int(poly[(i + 1) % c]); key = (a, b) if a < b else (b, a)
                j = index.get(key)
                if j is None: j = index[key] = len(edges); edges.append((a, b))
                es.append(j)
            nm.faces.append((tuple(int(v) for v in poly), tuple(es)))
            nm.face_kind.append(2); nm.face_zero.append(0); nm.face_group.append(group); nm.face_value.append(bytes(4))
            nm.face_flags.append(flags); nm.face_end.append(0)
        nm.edges = np.asarray(edges, np.int32).reshape(-1, 2)
        return nm

    def stats(self):
        v = self.vertices
        area = 0.0
        for vs, _ in self.faces:
            p = v[list(vs)][:, :2]; x, y = p[:, 0], p[:, 1]
            area += float((x * np.roll(y, -1) - np.roll(x, -1) * y).sum()) / 2
        return {"vertices": len(v), "edges": len(self.edges), "faces": len(self.faces),
                "quads": sum(len(f[0]) == 4 for f in self.faces), "area_m2": round(area, 1),
                "groups": sorted(set(self.face_group))[:24], "flags": [hex(x) for x in sorted(set(self.face_flags))[:8]]}


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
    else:
        print("navmesh.py info <navmesh.bin> | check <navmesh.bin>")
