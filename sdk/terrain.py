"""Reader for a scene's terrain.bin (heights, normals, layer indices and weights, physics materials).

Layout, worked out from the files of game v1.4.8.119303 (see docs/FORMATS_SCENE.md):

  "ZGR6" "RTRN" u32 version (2)
  5 table entries of 20 bytes: tag[4] u32 version(1) u32 offset u32 stored_size u32 raw_size
      MIDX  layer index maps for the whole terrain
      HGHT  heights, one image per node
      NRML  normals, one image per node
      WGHT  layer weights, per node
      PHYM  physics material maps, per node
  chunk at offset:
      per-node chunks start with one u32 per node: the node's offset inside the raw payload
      u64 kind (3) u64 raw_size u64 stored_size        stored_size counts from here to the end of the chunk
      16 bytes that are not read back (two u32 and a stale pointer)
      LZ4 block -> raw payload

  Raw payloads:
      MIDX  4 x [u32 length, PNG 8-bit grey, (nodes_x * 256) x (nodes_y * 256)]
      HGHT  per node [u32 length, PNG 16-bit grey 257 x 257]
      NRML  per node [u32 length, PNG RGBA 257 x 257]
      WGHT  per node [u32 count, count x (u32 length, PNG 8-bit grey 257 x 257)]
      PHYM  per node 4 x [u32 index, u32 length (66049), 257 x 257 bytes]

A node covers node_size metres with 257 x 257 samples; neighbours share their border row. Heights are
min_height + value / 65535 * (max_height - min_height), with the range from the <terrain> element of scene.xscene.
"""
import os, re, struct, sys, zlib

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tpac
else:
    from . import tpac

TAGS = ("MIDX", "HGHT", "NRML", "WGHT", "PHYM")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_decode(b):
    """8-bit or 16-bit grey and RGB(A) PNG to a numpy array (h, w) or (h, w, channels). Uses Pillow when it is installed
    (about 100 times faster); the fallback below needs only numpy."""
    assert b[:8] == PNG_MAGIC, "not a PNG"
    try:
        from PIL import Image
        import io
        im = Image.open(io.BytesIO(b))
        a = np.asarray(im)
        return a.astype(np.uint16) if im.mode.startswith("I") else a
    except ImportError:
        return _png_decode_slow(b)


def _png_decode_slow(b):
    o = 8; idat = []; w = h = bd = ct = None
    while o < len(b):
        n, tag = struct.unpack_from(">I4s", b, o)
        body = b[o + 8:o + 8 + n]; o += 12 + n
        if tag == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", body)
            assert il == 0 and bd in (8, 16) and ct in (0, 2, 6), ("unsupported PNG", bd, ct, il)
        elif tag == b"IDAT": idat.append(body)
        elif tag == b"IEND": break
    ch = {0: 1, 2: 3, 6: 4}[ct]; bpp = ch * bd // 8; stride = w * bpp
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), np.uint8).reshape(h, stride + 1)
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.int32)
    for y in range(h):
        f = raw[y, 0]; line = raw[y, 1:].astype(np.int32)
        if f == 0: cur = line
        elif f == 2: cur = (line + prev) & 255
        elif f == 1: cur = (np.cumsum(line.reshape(-1, bpp), axis=0) & 255).reshape(-1)
        else:
            cur = line.copy()
            for x in range(stride):
                a = cur[x - bpp] if x >= bpp else 0
                bb = prev[x]
                if f == 3: p = (a + bb) >> 1
                else:
                    c = prev[x - bpp] if x >= bpp else 0
                    pa = abs(bb - c); pb = abs(a - c); pc = abs(a + bb - 2 * c)
                    p = a if pa <= pb and pa <= pc else bb if pb <= pc else c
                cur[x] = (cur[x] + p) & 255
        out[y] = cur; prev = cur
    if bd == 16:
        a = out.reshape(h, w, ch, 2).astype(np.uint16); a = (a[..., 0] << 8) | a[..., 1]
    else:
        a = out.reshape(h, w, ch)
    return a[..., 0] if ch == 1 else a


class Terrain:
    def __init__(self, path):
        """Reads the 112-byte header only; chunks are read from disk when asked for."""
        self.path = path
        self.size = os.path.getsize(path)
        with open(path, "rb") as f: d = f.read(112)
        assert d[:8] == b"ZGR6RTRN", "not a terrain.bin"
        self.version, = struct.unpack_from("<I", d, 8)
        self.table = {}
        o = 12
        for _ in range(5):
            tag = d[o:o + 4].decode("ascii"); ver, off, stored, raw = struct.unpack_from("<4I", d, o + 4)
            self.table[tag] = {"version": ver, "offset": off, "stored": stored, "raw": raw}; o += 20
        assert tuple(self.table) == TAGS, tuple(self.table)
        order = sorted(self.table.values(), key=lambda e: e["offset"])
        for e, nxt in zip(order, order[1:] + [{"offset": self.size}]):
            e["table_bytes"] = nxt["offset"] - e["offset"] - e["stored"]
        self.nodes = self.table["HGHT"]["table_bytes"] // 4
        self._raw = {}

    def _read(self, offset, n):
        with open(self.path, "rb") as f:
            f.seek(offset); return f.read(n)

    def node_offsets(self, tag):
        e = self.table[tag]
        return list(struct.unpack("<%dI" % (e["table_bytes"] // 4), self._read(e["offset"], e["table_bytes"])))

    def raw(self, tag):
        """The decompressed payload of a chunk."""
        if tag not in self._raw:
            e = self.table[tag]
            blob = self._read(e["offset"] + e["table_bytes"], e["stored"])
            kind, raw_size, stored = struct.unpack_from("<3Q", blob, 0)
            assert kind == 3 and raw_size == e["raw"] and stored == e["stored"], (tag, kind, raw_size, stored, e)
            pay = tpac.lz4_decompress(blob[40:], raw_size)
            assert len(pay) == raw_size, (tag, len(pay), raw_size)
            self._raw[tag] = pay
        return self._raw[tag]

    def node_blobs(self, tag):
        pay = self.raw(tag); offs = self.node_offsets(tag) + [len(pay)]
        return [pay[offs[i]:offs[i + 1]] for i in range(len(offs) - 1)]

    def height_nodes(self):
        """One (257, 257) uint16 array per node, in file order."""
        out = []
        for b in self.node_blobs("HGHT"):
            n, = struct.unpack_from("<I", b, 0)
            assert n + 4 == len(b)
            out.append(png_decode(b[4:]))
        return out

    def heightmap(self, info):
        """The whole terrain as one float array in metres, shape (ny * 256 + 1, nx * 256 + 1). Row = y, column = x: the
        sample [j, i] is at x = i * node_size / 256, y = j * node_size / 256. Node k sits at column k % nx, row k // nx;
        neighbours share their border samples. info is scene_terrain_info() of the scene's scene.xscene."""
        nx, ny = info["nx"], info["ny"]
        assert nx * ny == self.nodes, (nx, ny, self.nodes)
        full = np.zeros((ny * 256 + 1, nx * 256 + 1), np.uint16)
        for k, h in enumerate(self.height_nodes()):
            j, i = divmod(k, nx)
            full[j * 256:j * 256 + 257, i * 256:i * 256 + 257] = h
        return info["min"] + full.astype(np.float64) / 65535.0 * (info["max"] - info["min"])


def hillshade(z, cell, exaggerate=1.0):
    """A shaded relief picture (uint8, north up: row 0 is the largest y) of a height array with cell metres per sample."""
    gy, gx = np.gradient(z * exaggerate, cell)
    n = np.dstack((-gx, -gy, np.ones_like(z))); n /= np.linalg.norm(n, axis=2, keepdims=True)
    light = np.array([-0.5, 0.5, 0.7]); light /= np.linalg.norm(light)
    shade = np.clip((n * light).sum(axis=2), 0, 1)
    t = (z - z.min()) / max(1e-9, z.max() - z.min())
    return np.clip((0.35 + 0.65 * shade) * (90 + 150 * t), 0, 255).astype(np.uint8)[::-1]


def scene_terrain_info(xscene_path):
    """node_dimension_x / y, node_size, min_height, max_height from the <terrain> element of a scene.xscene."""
    with open(xscene_path, "rb") as f: head = f.read()
    i = head.find(b"<terrain ")
    if i < 0: return None
    a = dict(re.findall(r'(\w+)="([^"]*)"', head[i:head.find(b">", i)].decode("utf-8", "replace")))
    return {"nx": int(a["node_dimension_x"]), "ny": int(a["node_dimension_y"]), "node_size": float(a["node_size"]),
            "min": float(a["min_height"]), "max": float(a["max_height"])}


if __name__ == "__main__":
    t = Terrain(sys.argv[1])
    print("version", t.version, "nodes", t.nodes)
    for tag, e in t.table.items(): print(tag, e)
