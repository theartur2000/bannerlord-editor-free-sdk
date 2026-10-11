"""Reader and writer for a scene's terrain.bin (heights, normals, layer index maps, layer weights, physics materials).

Layout, worked out from the files of game v1.4.8.119303:

  "ZGR6" "RTRN" u32 version (2)
  5 table entries of 20 bytes: tag[4] u32 version(1) u32 offset u32 stored_size u32 raw_size
      MIDX  layer index maps for the whole terrain
      HGHT  heights, one image per node
      NRML  normals, one image per node
      WGHT  layer weights, per node
      PHYM  physics material maps, per node
  chunk at offset:
      per-node chunks (all but MIDX) start with one u32 per node: the node's offset inside the raw payload
      u64 kind (3) u64 raw_size u64 stored_size        stored_size counts from here to the end of the chunk
      16 bytes whose values look like leftovers (two u32 and a stale pointer); kept as read, zero when built new
      LZ4 block -> raw payload
  A chunk can be absent: stored_size and raw_size 0, no node table (the world map has no NRML and no WGHT).

  Raw payloads:
      MIDX  4 x [u32 length, PNG 8-bit grey, (nodes_x * 256) x (nodes_y * 256)]
      HGHT  per node [u32 length, PNG 16-bit grey 257 x 257]
      NRML  per node [u32 length, PNG RGBA 257 x 257]: x in red, y in green, blue 63, alpha 0
      WGHT  per node 16 layer slots in the scene's layer order, whatever the number of layers: u32 present, then if present
            [u32 length, PNG 8-bit grey 257 x 257]. The game reads all 16: a node with fewer makes it read past the node
            (usually zeros, sometimes not: a crash on load now and then, in its image resize at client +0x53f8ee)
      PHYM  per node 4 x [u32 index 0..3, u32 length (66049), 257 x 257 bytes]: one material index map per season

A node covers node_size metres with 257 x 257 samples on battlefields (other scenes also use 129, 65, ...: the size is
whatever the node's images have); neighbours share their border row and column. Heights are
min_height + value / 65535 * (max_height - min_height), with the range from the <terrain> element of scene.xscene.
Row = y, column = x. Node k sits at column k % nodes_x, row k // nodes_x.

The PNGs carry a pHYs chunk of 2835 pixels per metre and 8192-byte IDAT chunks of a zlib level 6 stream. Two encoder
profiles are in use, and png_encode() reproduces both byte for byte:
  8-bit grey (WGHT, MIDX)      per row the best of all five filters, default zlib strategy
  16-bit grey, RGBA (HGHT, NRML)   per row the best of None, Sub and Paeth only, filtered zlib strategy
"Best" is the smallest sum of absolute signed filtered bytes, the first candidate on a tie.
"""
import os, re, struct, sys, zlib

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tpac
else:
    from . import tpac

TAGS = ("MIDX", "HGHT", "NRML", "WGHT", "PHYM")
LAYER_SLOTS = 16      # weight slots per node; the game reads this many however few layers the scene has
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
NODE = 257
SEASONS = 4


# ---------------------------------------------------------------- PNG

def png_decode(b):
    """8-bit or 16-bit grey and RGB(A) PNG to a numpy array (h, w) or (h, w, channels). Uses Pillow when it is installed
    (about 100 times faster); the fallback needs only numpy."""
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


def _png_chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff)


def png_encode(a):
    """A PNG as the game's files hold it: uint8 (h, w) grey, uint16 (h, w) 16-bit grey, uint8 (h, w, 4) RGBA. Each row gets
    the candidate filter with the smallest sum of absolute signed bytes (the first one on a tie). 8-bit grey: all five
    filters and the default zlib strategy; 16-bit grey and RGBA: None, Sub or Paeth and the filtered strategy (module docstring)."""
    a = np.asarray(a)
    if a.dtype == np.uint16:
        assert a.ndim == 2; h, w = a.shape; bd, ct, bpp = 16, 0, 2
        rows = np.empty((h, w, 2), np.uint8); rows[..., 0] = a >> 8; rows[..., 1] = a & 255
        rows = rows.reshape(h, w * 2)
    else:
        assert a.dtype == np.uint8
        if a.ndim == 2: h, w = a.shape; bd, ct, bpp = 8, 0, 1; rows = a
        else:
            h, w, ch = a.shape; assert ch == 4; bd, ct, bpp = 8, 6, 4; rows = a.reshape(h, w * 4)
    cur = rows.astype(np.int16)
    left = np.zeros_like(cur); left[:, bpp:] = cur[:, :-bpp]
    up = np.zeros_like(cur); up[1:] = cur[:-1]
    ul = np.zeros_like(cur); ul[1:, bpp:] = cur[:-1, :-bpp]
    p = left + up - ul
    pa = np.abs(p - left); pb = np.abs(p - up); pc = np.abs(p - ul)
    paeth = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, ul))
    cands = np.stack([cur, cur - left, cur - up, cur - ((left + up) >> 1), cur - paeth]).astype(np.uint8)   # wraps mod 256
    cost = np.where(cands < 128, cands, 256 - cands.astype(np.int32)).sum(axis=2)
    if bpp == 1: strategy = zlib.Z_DEFAULT_STRATEGY
    else:
        strategy = zlib.Z_FILTERED
        cost[[2, 3]] = np.iinfo(cost.dtype).max      # only None, Sub and Paeth compete
    best = np.argmin(cost, axis=0)
    stride = rows.shape[1]
    out = np.empty((h, stride + 1), np.uint8)
    out[:, 0] = best
    out[:, 1:] = cands[best, np.arange(h)]
    raw = out.tobytes()
    # the window shrinks for small images the way libpng does it: the smallest power of two that holds the data plus 262
    wbits = 15; half = 1 << 14
    while len(raw) + 262 <= half and wbits > 8: half >>= 1; wbits -= 1
    c = zlib.compressobj(6, zlib.DEFLATED, wbits, 8, strategy)
    z = c.compress(raw) + c.flush()
    png = [PNG_MAGIC, _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bd, ct, 0, 0, 0)),
           _png_chunk(b"pHYs", struct.pack(">IIB", 2835, 2835, 1))]
    for i in range(0, len(z), 8192): png.append(_png_chunk(b"IDAT", z[i:i + 8192]))
    png.append(_png_chunk(b"IEND", b""))
    return b"".join(png)


# ---------------------------------------------------------------- reading

class Terrain:
    """A terrain.bin on disk. Reads the 112-byte header at once and each chunk when it is asked for."""

    def __init__(self, path):
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
        self._raw = {}; self._prefix = {}

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
            if e["stored"] == 0:   # an absent chunk (the world map has no NRML and no WGHT): no node table either
                self._raw[tag] = b""; self._prefix[tag] = None
                return b""
            blob = self._read(e["offset"] + e["table_bytes"], e["stored"])
            kind, raw_size, stored = struct.unpack_from("<3Q", blob, 0)
            assert kind == 3 and raw_size == e["raw"] and stored == e["stored"], (tag, kind, raw_size, stored, e)
            pay = tpac.lz4_decompress(blob[40:], raw_size)
            assert len(pay) == raw_size, (tag, len(pay), raw_size)
            self._raw[tag] = pay; self._prefix[tag] = blob[24:40]
        return self._raw[tag]

    def prefix(self, tag):
        self.raw(tag); return self._prefix[tag]

    def node_blobs(self, tag):
        pay = self.raw(tag)
        if self.table[tag]["stored"] == 0: return []
        offs = self.node_offsets(tag) + [len(pay)]
        return [pay[offs[i]:offs[i + 1]] for i in range(len(offs) - 1)]

    # PNG blobs as stored (for comparisons) and decoded arrays
    def height_pngs(self):
        out = []
        for b in self.node_blobs("HGHT"):
            n, = struct.unpack_from("<I", b, 0); assert n + 4 == len(b); out.append(b[4:])
        return out

    def height_nodes(self):
        """One (257, 257) uint16 array per node, in file order."""
        return [png_decode(b) for b in self.height_pngs()]

    def normal_pngs(self):
        out = []
        for b in self.node_blobs("NRML"):
            n, = struct.unpack_from("<I", b, 0); assert n + 4 == len(b); out.append(b[4:])
        return out

    def normal_nodes(self):
        """One (257, 257, 4) uint8 array per node."""
        return [png_decode(b) for b in self.normal_pngs()]

    def weight_pngs(self):
        """Per node a list with one entry per terrain layer: the PNG bytes, or None where the layer is absent in the node."""
        out = []
        for b in self.node_blobs("WGHT"):
            q = 0; layers = []
            while q < len(b):
                present, = struct.unpack_from("<I", b, q); q += 4
                assert present in (0, 1), present
                if present:
                    n, = struct.unpack_from("<I", b, q); layers.append(b[q + 4:q + 4 + n]); q += 4 + n
                else: layers.append(None)
            out.append(layers)
        return out

    def weight_nodes(self):
        return [[None if p is None else png_decode(p) for p in layers] for layers in self.weight_pngs()]

    def index_pngs(self):
        pay = self.raw("MIDX"); q = 0; out = []
        while q < len(pay):
            n, = struct.unpack_from("<I", pay, q); out.append(pay[q + 4:q + 4 + n]); q += 4 + n
        return out

    def index_maps(self):
        return [png_decode(p) for p in self.index_pngs()]

    def physics_nodes(self):
        """Per node an array (4, 257, 257) uint8: the physics material index map of each season."""
        out = []
        for b in self.node_blobs("PHYM"):
            maps = []; q = 0
            for s in range(SEASONS):
                idx, n = struct.unpack_from("<2I", b, q); side = int(round(n ** 0.5))
                assert idx == s and side * side == n, ("physics block", idx, n)
                maps.append(np.frombuffer(b, np.uint8, n, q + 8).reshape(side, side)); q += 8 + n
            assert q == len(b)
            out.append(np.stack(maps))
        return out

    def heightmap(self, info):
        """The whole terrain as one float array in metres, shape (ny * 256 + 1, nx * 256 + 1); [j, i] is at
        x = i * node_size / 256, y = j * node_size / 256. info is scene_terrain_info() of the scene's scene.xscene."""
        return info["min"] + join_nodes(self.height_nodes(), info["nx"], info["ny"]).astype(np.float64) / 65535.0 * (info["max"] - info["min"])

    def load(self):
        """Everything decoded, as the dict build() takes."""
        return {"version": self.version, "heights": self.height_nodes(), "normals": self.normal_nodes(),
                "weights": self.weight_nodes(), "index_maps": self.index_maps(), "physics": self.physics_nodes(),
                "prefix": {t: self.prefix(t) for t in TAGS}}


def join_nodes(nodes, nx, ny):
    """Per-node (257, 257[, c]) arrays to one (ny * 256 + 1, nx * 256 + 1[, c]) array."""
    assert len(nodes) == nx * ny, (len(nodes), nx, ny)
    side = max(h.shape[0] for h in nodes); step = side - 1
    full = np.zeros((ny * step + 1, nx * step + 1) + nodes[0].shape[2:], nodes[0].dtype)
    for k, h in enumerate(nodes):
        j, i = divmod(k, nx)
        if h.shape[0] != side:                       # a coarser node: repeat its samples up to the finest node's grid
            r = step // (h.shape[0] - 1)
            h = np.kron(h[:-1, :-1], np.ones((r, r), h.dtype)) if h.ndim == 2 else h
            pad = np.zeros((side, side), nodes[0].dtype); pad[:step, :step] = h; pad[step, :step] = h[-1]; pad[:step, step] = h[:, -1]; pad[step, step] = h[-1, -1]; h = pad
        full[j * step:j * step + side, i * step:i * step + side] = h
    return full


def split_nodes(full, nx, ny):
    """The reverse of join_nodes: neighbours get the shared border samples."""
    assert full.shape[0] == ny * 256 + 1 and full.shape[1] == nx * 256 + 1, full.shape
    return [np.ascontiguousarray(full[j * 256:j * 256 + NODE, i * 256:i * 256 + NODE]) for j in range(ny) for i in range(nx)]


# ---------------------------------------------------------------- writing

def _blob(png): return struct.pack("<I", len(png)) + png


def build(data, lz4=None):
    """terrain.bin bytes from a dict like Terrain.load() gives: heights (uint16 per node), normals (RGBA per node), weights
    (per node a list per layer, None where absent), index_maps (4 grey images), physics ((4, 257, 257) per node). "prefix"
    (16 bytes per chunk) is optional. lz4 is the block compressor (default: the SDK's own)."""
    lz4 = lz4 or tpac.lz4_compress
    n = len(data["heights"])
    assert len(data["normals"]) in (0, n) and len(data["weights"]) in (0, n) and len(data["physics"]) == n and len(data["index_maps"]) == 4
    pay = {"MIDX": [b"".join(_blob(png_encode(m)) for m in data["index_maps"])],
           "HGHT": [_blob(png_encode(h)) for h in data["heights"]],
           "NRML": [_blob(png_encode(v)) for v in data["normals"]],
           "WGHT": [b"".join(struct.pack("<I", 0) if w is None else struct.pack("<I", 1) + _blob(png_encode(w))
                             for w in list(layers) + [None] * (LAYER_SLOTS - len(layers)))
                    for layers in data["weights"]],
           "PHYM": [b"".join(struct.pack("<2I", s, p[s].size) + np.ascontiguousarray(p[s]).tobytes() for s in range(SEASONS))
                    for p in data["physics"]]}
    return assemble(pay, data.get("version", 2), data.get("prefix"), lz4)


def assemble(payloads, version=2, prefix=None, lz4=None):
    """The container around raw payloads: {tag: [bytes per node]} (MIDX: one entry)."""
    lz4 = lz4 or tpac.lz4_compress
    chunks = []
    for tag in TAGS:
        parts = payloads[tag]; raw = b"".join(parts)
        if not parts:   # absent chunk
            chunks.append((tag, b"", 0, 0)); continue
        table = b""
        if tag != "MIDX":
            o = 0; offs = []
            for p in parts: offs.append(o); o += len(p)
            table = struct.pack("<%dI" % len(offs), *offs)
        z = lz4(raw)
        stored = 40 + len(z)
        pre = (prefix or {}).get(tag) or bytes(16)
        chunks.append((tag, table + struct.pack("<3Q", 3, len(raw), stored) + pre + z, stored, len(raw)))
    out = bytearray(b"ZGR6RTRN" + struct.pack("<I", version))
    off = 12 + 20 * len(TAGS)
    for tag, body, stored, raw in chunks:
        out += tag.encode("ascii") + struct.pack("<4I", 1, off, stored, raw); off += len(body)
    for _, body, _, _ in chunks: out += body
    return bytes(out)


# ---------------------------------------------------------------- helpers

def normals_from_heights(z, cell):
    """The NRML image for a height array in metres (any shape) with cell metres per sample: x in red, y in green
    (0..255 for -1..1), blue 63, alpha 0. An approximation of what the editor stores, see the note in selftest."""
    gy, gx = np.gradient(z, cell)
    n = np.dstack((-gx, -gy, np.ones_like(z))); n /= np.linalg.norm(n, axis=2, keepdims=True)
    out = np.zeros(z.shape + (4,), np.uint8)
    out[..., 0] = np.clip(np.rint(n[..., 0] * 127.5 + 127.5), 0, 255)
    out[..., 1] = np.clip(np.rint(n[..., 1] * 127.5 + 127.5), 0, 255)
    out[..., 2] = 63
    return out


def hillshade(z, cell, exaggerate=1.0):
    """A shaded relief picture (uint8, north up: row 0 is the largest y) of a height array with cell metres per sample."""
    gy, gx = np.gradient(z * exaggerate, cell)
    n = np.dstack((-gx, -gy, np.ones_like(z))); n /= np.linalg.norm(n, axis=2, keepdims=True)
    light = np.array([-0.5, 0.5, 0.7]); light /= np.linalg.norm(light)
    shade = np.clip((n * light).sum(axis=2), 0, 1)
    t = (z - z.min()) / max(1e-9, z.max() - z.min())
    return np.clip((0.35 + 0.65 * shade) * (90 + 150 * t), 0, 255).astype(np.uint8)[::-1]


def scene_terrain_info(xscene_path):
    """node_dimension_x / y, node_size, min_height, max_height and the layer names from a scene.xscene."""
    with open(xscene_path, "rb") as f: head = f.read()
    i = head.find(b"<terrain ")
    if i < 0: return None
    a = dict(re.findall(r'(\w+)="([^"]*)"', head[i:head.find(b">", i)].decode("utf-8", "replace")))
    end = head.find(b"</terrain>", i)
    layers = [m.decode("utf-8", "replace") for m in re.findall(rb'<layer\s+name="([^"]*)"', head[i:end if end > 0 else len(head)])]
    return {"nx": int(a["node_dimension_x"]), "ny": int(a["node_dimension_y"]), "node_size": float(a["node_size"]),
            "min": float(a["min_height"]), "max": float(a["max_height"]), "layers": layers}


def check(path, full=True, log=print):
    """Rebuilds a terrain.bin from its decoded content and compares with the file. Returns (ok, details).
    Compared: every PNG byte for byte, then every chunk's raw payload and node table byte for byte. The LZ4 blocks
    themselves are not compared: the game's compressor (a high-compression LZ4) makes other choices than the SDK's."""
    t = Terrain(path); res = {}
    pngs = {"HGHT": t.height_pngs(), "NRML": t.normal_pngs(), "MIDX": t.index_pngs(),
            "WGHT": [p for layers in t.weight_pngs() for p in layers if p is not None]}
    slots = [len(layers) for layers in t.weight_pngs()]
    if slots:
        good = sum(k == LAYER_SLOTS for k in slots); res["weight slots"] = (good, len(slots))
        if good != len(slots): log("  WGHT: %d of %d nodes do not have %d layer slots (the game reads past them)" % (len(slots) - good, len(slots), LAYER_SLOTS))
    for tag, lst in pngs.items():
        same = sum(png_encode(png_decode(p)) == p for p in lst)
        res["png " + tag] = (same, len(lst)); log("  %s PNGs re-encoded identical: %d of %d" % (tag, same, len(lst)))
    if not full: return all(a == b for a, b in res.values()), res
    new = build(t.load())
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".terrain.bin"); os.close(fd)
    try:
        with open(tmp, "wb") as f: f.write(new)
        n = Terrain(tmp)
        same = 0
        for tag in TAGS:
            ok = n.raw(tag) == t.raw(tag) and n.node_offsets(tag) == t.node_offsets(tag) and n.table[tag]["raw"] == t.table[tag]["raw"]
            same += ok
            if not ok: log("  %s payload or node table differs" % tag)
        res["chunks"] = (same, len(TAGS))
        log("  chunk payloads and node tables identical: %d of %d (file %d bytes, rebuilt %d)" % (same, len(TAGS), t.size, len(new)))
    finally:
        os.remove(tmp)
    return all(a == b for a, b in res.values()), res


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "info":
        t = Terrain(a[1]); print("version", t.version, "nodes", t.nodes)
        for tag, e in t.table.items(): print(tag, e)
    elif len(a) >= 2 and a[0] == "check":
        ok, _ = check(a[1]); print("PASS" if ok else "FAIL")
    elif len(a) >= 3 and a[0] == "relief":
        # relief <scene folder> <out.png>
        from PIL import Image
        info = scene_terrain_info(os.path.join(a[1], "scene.xscene"))
        z = Terrain(os.path.join(a[1], "terrain.bin")).heightmap(info)
        Image.fromarray(hillshade(z, info["node_size"] / 256)).save(a[2]); print(a[2], z.shape)
    else:
        print("terrain.py info <terrain.bin> | check <terrain.bin> | relief <scene folder> <out.png>")
