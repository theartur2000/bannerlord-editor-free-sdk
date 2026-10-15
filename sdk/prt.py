"""prt_data.bin of a scene: the baked light probe grid ("PRT", precomputed radiance transfer). Read, write, round trip.

    python sdk/prt.py info <scene name or prt_data.bin>     entries, grid, probe counts, mean values
    python sdk/prt.py check [name part] [--module M] [--limit N] [--jobs J] [--values]
                                                             decode every shipped file (every PNG to pixels) and encode it
                                                             again, compare byte for byte (N of M); --values also rebuilds
                                                             every PRT4 entry from its decoded probe values
    python sdk/prt.py neutral <out prt_data.bin> --bounds x0,y0,z0,x1,y1,z1 [--spacing h,v]
                                                             a grid of open-sky probes without bounce light (see below)

Baking new probes from a scene's geometry: sdk/prt_bake.py.

Layout (game v1.4.8, client TaleWorlds.Native.dll prt_load_file 1804518d0; editor build prt_write_finalized 18080a400),
little endian:

  u32 H, then H bytes: "PRT4" (what the current editor writes) or "PRT3" (an older build: finer quantisation, below),
      u32 n, n x [u32 length, entry name, u32 offset]. Entry k is at 4 + H + offset: u32 size, then the entry. The
      offsets run on without gaps. The name is the scene's active upgrade levels, sorted, each followed by one space,
      "base" left out ("civilian level_1 ", "level_2 siege "); "" for a scene without levels or tagged use_base_gi
      (prt_entry_name_from_levels 1803a1930). No entry of that name: the scene gets no probes.
  An entry:
      u32 nx, ny, nz; f32 spacing x, y, z (the editor writes the x spacing twice; the client stores both in one field);
      f32 min x, y, z; f32 max x, y, z (= min + n * spacing); u32 32 (renderer +0x64, constructor default 32: read as
      the visibility depth map size, not verified).
      u32 cells (= nx ny nz); u32 bytes + a bit mask (LSB first) of the active probes; u32 bytes + a bit mask of the
      probes that need the visibility test (a subset of the active ones but for a few cells in 4 entries); u32 3 x active + f32 x, y, z of each
      active probe, in cell order. Cell order everywhere: c = (y nx + x) nz + z. A probe's position is not its cell
      centre: the editor moves probes (navmesh placement, agent height).
      u32 size of the next section, then per z slice 9 PNGs (16-bit grey):
        6 "sky" images d = 0..5 (directions DIRS: +x +y -x -y -z +z), nx wide, ny high, whose pixel buffer is x-major: the
          sample of cell (x, y) is at x * ny + y. u16 = trunc(999 x sky visibility) (PRT3: 10000).
        3 "bounce" images, red, green, blue, 6 nx wide, 4 ny high, row-major: pixel (row 4 y + j, column 6 x + d) is
          coefficient j (L1 SH of the sun direction: Y00, Y1-1, Y10, Y11) of direction d. u16 = trunc(3999 (v + 2) / 4),
          v clamped to [-2, 2] (PRT3: 40000 steps); inactive cells 1999 (v = 0).
      f32 point_max (the largest point light value, at least 1), u32 size, then per z slice 18 PNGs (direction d x 3 +
      channel), nx x ny, row-major, where pixel p of slice z holds canonical cell z nx ny + p (not the cell at x, y, z:
      writer prt_write_point_lights and reader prt_decode_point_light_slices both index that way).
      u16 = trunc(Q v / point_max), Q = (uint)(point_max x 1999.9999) cut to 16 bits by both sides.
  The PNGs are what Qt (QImage::save, libpng 1.6.34) writes: see png_encode16.
  "PRT1" (mp_compact, vlandia_castle_interior_a): an older layout. The client reads its first u32 as the header size and
  finds no PRT3 / PRT4 magic, so those two scenes load no probes. Kept as bytes.

Measured (game v1.4.8, every prt_data.bin under Modules: 176 of the official modules and 2 copies in a test module,
8.6 GB, `check --jobs 4`, 3 hours): 178 of 178 files byte-identical (2 of them the legacy PRT1, kept as read), 563 of 563
entries, 661,149 of 661,149 PNGs decoded to pixels and encoded again byte for byte. 174 of 174 PRT4 entries (44 files)
also come out byte-identical when rebuilt from their decoded probe values (probes(mid=True) -> build_entry): the
quantisation, layouts and clamps above are the editor's. PRT3 entries decode with the PRT3 steps; build_entry writes PRT4.

What the game does with it (client):
  scene_load_prt (1801d3190) loads the file when the scene's gi_data_exists is true (the default when the property is
  absent; 323 shipped scenes set it false and have no file) and the graphics option at config +0xc40 is above 0 (0
  unloads it). From the entry it builds prt_probe_data (a u32 per cell: probe index, bits 19..30 visibility index or 0xfff,
  bit 31 active), a colour buffer of 7 float4 per probe (position, then per direction rgb light and the sky visibility
  w: relight()), and the list of visibility probes, for which the client renders depth cube maps when the scene loads
  (rglPrt_scene_loading_visibility_renderer, max 4095). The bounce light is turned into colour with the current sun
  (prt_sun_sh_colours_cb 1803a0060): that is why it is stored as sun-direction SH and not as colour.
  Shaders (Shaders/Sources/prt_functions.rsh, diffuse_ambient_calculator.rs, ambient_functions.rsh): a pixel inside the
  grid takes the 8 probes around it (trilinear weights times the cosine towards each probe, times the depth-map test for
  visibility probes); each probe gives sum_d max(0, n . d) (rgb_d + skycolour_d w_d) / sum_d max(0, n . d), skycolour_d
  being the atmosphere's averaged sky colours per direction (prt_average_sky_colours 18039f660) and the result scaled by
  prt_multiplier, prt_intensity_contrast and prt_color_contrast (scene properties). Outside the grid, or where no probe
  counts, the probe at prt_fallback_probe_index is used. The result replaces the diffuse ambient and scales the specular
  environment light; the summed w is the sky visibility (sky occlusion of reflections).
  Without the file (or with the option at 0) the view has no prt_probe_data, view_system_shader_flags (18028cee0) leaves
  SYSTEM_USE_PRT off, and the ambient comes from the global environment map with sky visibility 1: interiors are not
  darkened and get no bounced colour. Generated battle scenes render that way.
  neutral(): every cell an active probe at its centre with sky 1 in all six directions and no bounce or point light. The
  client then lights every point with the six averaged sky colours by its normal and sky visibility 1: the open-air look
  of a scene without the file, through the probe path (the colours are the cube map's direction averages, not the
  environment map itself, so it is close to but not identical with no file). sky < 1 darkens evenly; bounce_y00 adds a
  constant term that follows the sun's colour.

Not verified, not done:
  - Nothing written here was loaded in the game (the game was in use by another session).
  - The u32 32 in the header and the client's use of the visibility mask beyond the shader are read from code only.
No game content is stored in the repository: the module reads the install at run time.
"""
import glob, os, struct, sys, time, zlib

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
else:
    from . import config

MAGICS = (b"PRT3", b"PRT4")
LEGACY = b"PRT1"
DIRS = ("+x", "+y", "-x", "-y", "-z", "+z")          # the six basis directions of prt_functions.rsh, in data order
DIR_VECTORS = np.array([[1, 0, 0], [0, 1, 0], [-1, 0, 0], [0, -1, 0], [0, 0, -1], [0, 0, 1]], np.float64)
SH_SCALE = (0.2820948, 0.48860252, 0.48860252, 0.48860252)   # |Y00|, |Y1m| as the client evaluates them
SH_WINDOW = 0.94391274                                       # the client's factor on every sun SH coefficient


# ------------------------------------------------------------------------------------------------------------------- PNG (libpng as Qt drives it)
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
PHYS_72DPI = struct.pack(">IIB", 2835, 2835, 1)   # Qt's default 2835 dots per metre
CHUNK_PIXELS = 1 << 22                             # images filtered / unfiltered together, at most this many pixels


def _chunk(tag, body):
    return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xffffffff)


def png_chunks(p):
    if p[:8] != _PNG_SIG: raise ValueError("not a PNG")
    o = 8; out = []
    while o < len(p):
        n, = struct.unpack_from(">I", p, o); out.append((p[o + 4:o + 8], p[o + 8:o + 8 + n])); o += 12 + n
    return out


def _unfilter(raw, h, w, bpp):
    """Undo the PNG row filters of k images of h rows of w pixels of bpp bytes: raw (k, h, w * bpp + 1) uint8. Every byte
    depends on its left, upper and upper-left neighbours only, so all pixels on one anti-diagonal (row + column = t) of
    all k images are decoded in one step: h + w - 1 numpy steps for the whole stack."""
    k = raw.shape[0]; T = h + w - 1
    ft = raw[:, :, 0]
    if ft.max() > 4: raise ValueError("PNG filter type %d" % ft.max())
    # skewed layout: s[t, :, r] = pixel (r, t - r); the left neighbour is s[t - 1, :, r], the upper one s[t - 1, :, r - 1],
    # the upper-left one s[t - 2, :, r - 1]. Row index 0 and t indices 0, 1 are zero padding.
    x = raw[:, :, 1:].reshape(k, h, w, bpp)
    r_idx = np.arange(h)[:, None]; j_idx = np.arange(w)[None, :]
    xs = np.zeros((T, k, h, bpp), np.int16); xs[r_idx + j_idx, :, r_idx] = x.transpose(1, 2, 0, 3)
    s = np.zeros((T + 2, k, h + 1, bpp), np.int16)
    use = [(ft == v)[:, :, None] for v in range(4)]
    for t in range(T):
        r0 = max(0, t - w + 1); r1 = min(h - 1, t) + 1
        a = s[t + 1, :, r0 + 1:r1 + 1]; b = s[t + 1, :, r0:r1]; c = s[t, :, r0:r1]
        pa = np.abs(b - c); pb = np.abs(a - c); pc = np.abs(a + b - 2 * c)
        pr = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
        pr = np.where(use[0][:, r0:r1], 0, np.where(use[1][:, r0:r1], a, np.where(use[2][:, r0:r1], b, np.where(use[3][:, r0:r1], (a + b) >> 1, pr))))
        s[t + 2, :, r0 + 1:r1 + 1] = (xs[t, :, r0:r1] + pr) & 255
    out = s[r_idx + j_idx + 2, :, r_idx + 1]                     # (h, w, k, bpp)
    return out.transpose(2, 0, 1, 3).reshape(k, h, w * bpp)


def png_decode16_many(pngs, pillow=True):
    """16-bit greyscale PNGs -> list of (h, w) uint16; images of one size are unfiltered together. Pillow decodes them
    when it is installed (about ten times faster); the numpy path gives the same pixels."""
    try:
        if not pillow: raise ImportError
        import io
        from PIL import Image
        out = []
        for p in pngs:
            im = Image.open(io.BytesIO(p))
            if im.mode != "I;16": raise ValueError("PNG is not 16-bit greyscale (%s)" % im.mode)
            out.append(np.asarray(im, np.uint16))
        return out
    except ImportError:
        pass
    heads = []
    for p in pngs:
        ch = png_chunks(p)
        w, h, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", ch[0][1])
        if (depth, colour, interlace) != (16, 0, 0): raise ValueError("PNG is not 16-bit greyscale (%d, %d, %d)" % (depth, colour, interlace))
        heads.append((w, h, zlib.decompress(b"".join(b for t, b in ch if t == b"IDAT"))))
    out = [None] * len(pngs); groups = {}
    for i, (w, h, raw) in enumerate(heads): groups.setdefault((w, h), []).append(i)
    for (w, h), idx in groups.items():
        step = max(1, CHUNK_PIXELS // (h * w))
        for q0 in range(0, len(idx), step):
            raw = np.stack([np.frombuffer(heads[i][2], np.uint8).reshape(h, 2 * w + 1) for i in idx[q0:q0 + step]])
            u = _unfilter(raw, h, w, 2).astype(np.uint16)
            for j, i in enumerate(idx[q0:q0 + step]): out[i] = u[j, :, 0::2] << 8 | u[j, :, 1::2]
    return out


def png_decode16(p):
    """A 16-bit greyscale PNG -> (h, w) uint16."""
    return png_decode16_many([p])[0]


def png_encode16(img, phys=PHYS_72DPI):
    """(h, w) uint16 -> the PNG the editor writes (QImage::save, libpng 1.6.34 in Qt5Gui.dll): IHDR, pHYs 2835 dpm, IDAT in
    8192-byte pieces, IEND. Row filter per row = the smallest sum of |signed byte| of None, Sub and Paeth (ties go to the
    first; Up and Average are not offered), zlib level 6, Z_FILTERED, 15-bit window (libpng's narrower window for <= 16 KB
    images, and a zlib header naming the smallest window that holds the data), memLevel 8. A stream whose length is a
    multiple of 8192 gets an empty IDAT after the last full one, as libpng writes it."""
    return png_encode16_many([img], phys)[0]


def _filter_rows(img):
    """(k, h, w) uint16 -> (k, h, 2 w + 1) uint8 filtered scanlines, filter chosen per row as libpng does."""
    k, h, w = img.shape; rb = 2 * w
    x = np.empty((k, h, rb), np.int16); x[:, :, 0::2] = img >> 8; x[:, :, 1::2] = img & 255
    a = np.zeros_like(x); a[:, :, 2:] = x[:, :, :-2]
    b = np.zeros_like(x); b[:, 1:] = x[:, :-1]
    c = np.zeros_like(x); c[:, 1:, 2:] = x[:, :-1, :-2]
    pa = np.abs(b - c); pb = np.abs(a - c); pc = np.abs(a + b - 2 * c)
    cand = np.stack([x, x - a, x - np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))]) & 255
    cost = np.where(cand < 128, cand, 256 - cand).sum(axis=3, dtype=np.int64)       # (3, k, h)
    best = np.argmin(cost, axis=0)                                                   # first minimum wins
    rows = np.empty((k, h, rb + 1), np.uint8); rows[:, :, 0] = np.array([0, 1, 4], np.uint8)[best]
    rows[:, :, 1:] = np.take_along_axis(cand, best[None, :, :, None], 0)[0]
    return rows


def _optimize_cmf(z, size):
    """libpng's optimize_cmf: for <= 16 KB of image data the zlib header claims the smallest window that holds it."""
    if size > 16384 or (z[0] & 0x0f) != 8 or (z[0] & 0xf0) > 0x70: return z
    cinfo = z[0] >> 4; half = 1 << (cinfo + 7)
    if size > half: return z
    while True:
        half >>= 1; cinfo -= 1
        if not (cinfo > 0 and size <= half): break
    cmf = (z[0] & 0x0f) | (cinfo << 4); flg = z[1] & 0xe0; flg += 0x1f - ((cmf << 8) + flg) % 0x1f
    return bytes((cmf, flg)) + z[2:]


def png_encode16_many(imgs, phys=PHYS_72DPI):
    """The PNGs of several images (see png_encode16); images of one size are filtered together."""
    out = [None] * len(imgs); groups = {}
    for i, im in enumerate(imgs): groups.setdefault(np.shape(im), []).append(i)
    for (h, w), idx in groups.items():
      step = max(1, CHUNK_PIXELS // (h * w))
      for q0 in range(0, len(idx), step):
        rows = _filter_rows(np.array([imgs[i] for i in idx[q0:q0 + step]], np.uint16))
        for j, i in enumerate(idx[q0:q0 + step]):
            data = rows[j].tobytes(); bits = 15
            if len(data) <= 16384:                                                   # png_deflate_claim
                half = 1 << (bits - 1)
                while len(data) + 262 <= half: half >>= 1; bits -= 1
                bits = max(bits, 9)
            co = zlib.compressobj(6, zlib.DEFLATED, bits, 8, zlib.Z_FILTERED); z = co.compress(data) + co.flush()
            z = _optimize_cmf(z, len(data))
            o = [_PNG_SIG, _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 0, 0, 0, 0))]
            if phys is not None: o.append(_chunk(b"pHYs", phys))
            o += [_chunk(b"IDAT", z[q:q + 8192]) for q in range(0, len(z), 8192)]
            if len(z) % 8192 == 0: o.append(_chunk(b"IDAT", b""))   # libpng's last write after a full buffer is empty
            o.append(_chunk(b"IEND", b""))
            out[i] = b"".join(o)
    return out


# -------------------------------------------------------------------------------------------------------------------------------- the file
def parse(data):
    """dict(magic, entries=[dict(name, blob)]) of a prt_data.bin; a legacy PRT1 file -> dict(magic=b"PRT1", raw=bytes)."""
    if data[:4] == LEGACY: return dict(magic=LEGACY, raw=bytes(data))
    hs, = struct.unpack_from("<I", data, 0); magic = data[4:8]
    if magic not in MAGICS: raise ValueError("not a prt_data.bin (magic %r)" % magic)
    n, = struct.unpack_from("<I", data, 8); o = 12; table = []
    for _ in range(n):
        L, = struct.unpack_from("<I", data, o); name = data[o + 4:o + 4 + L].decode("utf-8", "surrogateescape"); o += 4 + L
        off, = struct.unpack_from("<I", data, o); o += 4; table.append((name, off))
    if o != 4 + hs: raise ValueError("prt_data.bin header size %d, table ends at %d" % (hs, o - 4))
    entries = []; expect = 0
    for name, off in table:
        if off != expect: raise ValueError("prt_data.bin entry %r at %d, expected %d" % (name, off, expect))
        size, = struct.unpack_from("<I", data, 4 + hs + off)
        entries.append(dict(name=name, blob=bytes(data[8 + hs + off:8 + hs + off + size]))); expect += 4 + size
    if 4 + hs + expect != len(data): raise ValueError("prt_data.bin: %d bytes after the last entry" % (len(data) - 4 - hs - expect))
    return dict(magic=magic, entries=entries)


def pack(d):
    if d["magic"] == LEGACY: return d["raw"]
    table = []; body = []; off = 0
    for e in d["entries"]:
        nm = e["name"].encode("utf-8", "surrogateescape"); blob = e["blob"] if "blob" in e else pack_entry(e)
        table.append(struct.pack("<I", len(nm)) + nm + struct.pack("<I", off)); body.append(struct.pack("<I", len(blob)) + blob); off += 4 + len(blob)
    head = d["magic"] + struct.pack("<I", len(table)) + b"".join(table)
    return struct.pack("<I", len(head)) + head + b"".join(body)


def read(path):
    if os.path.isdir(path): path = os.path.join(path, "prt_data.bin")
    with open(path, "rb") as f: return parse(f.read())


def write(path, d):
    if os.path.isdir(path): path = os.path.join(path, "prt_data.bin")
    with open(path, "wb") as f: f.write(pack(d))


# ------------------------------------------------------------------------------------------------------------------------------ one entry
HEAD = struct.Struct("<3I3f3f3fI")


def _blobs(b, o, n):
    out = []
    for _ in range(n):
        L, = struct.unpack_from("<I", b, o); out.append(b[o + 4:o + 4 + L]); o += 4 + L
    return out, o


def parse_entry(blob, images=True):
    """One probe grid. images=False keeps the PNGs as bytes (keys sky_png, bounce_png, point_png) instead of decoding them."""
    b = memoryview(blob).tobytes() if not isinstance(blob, bytes) else blob
    nx, ny, nz, sx, sy, sz, x0, y0, z0, x1, y1, z1, vis_res = HEAD.unpack_from(b, 0); o = HEAD.size
    cells, = struct.unpack_from("<I", b, o); o += 4
    if cells != nx * ny * nz: raise ValueError("cell count %d, grid %d x %d x %d" % (cells, nx, ny, nz))
    masks = []
    for _ in range(2):
        L, = struct.unpack_from("<I", b, o); m = np.frombuffer(b, np.uint8, L, o + 4); o += 4 + L
        if L != (cells + 7) // 8: raise ValueError("bit mask of %d bytes for %d cells" % (L, cells))
        masks.append(np.unpackbits(m, bitorder="little").astype(bool))
    nf, = struct.unpack_from("<I", b, o); pos = np.frombuffer(b, "<f4", nf, o + 4).reshape(-1, 3).copy(); o += 4 + 4 * nf
    e = dict(dims=(nx, ny, nz), spacing=(sx, sy, sz), bounds_min=(x0, y0, z0), bounds_max=(x1, y1, z1), vis_res=vis_res,
             active=masks[0][:cells], visibility=masks[1][:cells], positions=pos)
    if masks[0][cells:].any() or masks[1][cells:].any(): raise ValueError("bits set past the last cell")
    if len(pos) != e["active"].sum(): raise ValueError("%d positions for %d active probes" % (len(pos), e["active"].sum()))
    size2, = struct.unpack_from("<I", b, o); o += 4; s = o
    sky, bounce = [], []
    for _ in range(nz):
        p, o = _blobs(b, o, 9); sky.append(p[:6]); bounce.append(p[6:])
    if o - s != size2: raise ValueError("sky/bounce section size %d, read %d" % (size2, o - s))
    e["point_max"], size3 = struct.unpack_from("<fI", b, o); o += 8; s = o
    point = []
    for _ in range(nz):
        p, o = _blobs(b, o, 18); point.append(p)
    if o - s != size3 or o != len(b): raise ValueError("point light section size %d, read %d, %d bytes left" % (size3, o - s, len(b) - o))
    if not images:
        e.update(sky_png=sky, bounce_png=bounce, point_png=point); return e
    flat = [p for z in sky for p in z] + [p for z in bounce for p in z] + [p for z in point for p in z]
    imgs = png_decode16_many(flat); n1 = 6 * nz; n2 = n1 + 3 * nz
    e["sky"] = np.array(imgs[:n1]).reshape(nz, 6, ny, nx)
    e["bounce"] = np.array(imgs[n1:n2]).reshape(nz, 3, 4 * ny, 6 * nx)
    e["point"] = np.array(imgs[n2:]).reshape(nz, 18, ny, nx)
    return e


def _sections(e, key, k):
    if key + "_png" in e and key not in e: return e[key + "_png"]
    flat = png_encode16_many([img for z in e[key] for img in z])
    return [flat[i:i + k] for i in range(0, len(flat), k)]


def pack_entry(e):
    nx, ny, nz = e["dims"]; cells = nx * ny * nz
    out = [HEAD.pack(nx, ny, nz, *e["spacing"], *e["bounds_min"], *e["bounds_max"], e.get("vis_res", 32)), struct.pack("<I", cells)]
    for m in (e["active"], e["visibility"]):
        bits = np.zeros((cells + 7) // 8 * 8, bool); bits[:cells] = m
        mb = np.packbits(bits, bitorder="little").tobytes(); out.append(struct.pack("<I", len(mb)) + mb)
    pos = np.ascontiguousarray(e["positions"], "<f4"); out.append(struct.pack("<I", pos.size) + pos.tobytes())
    sky = _sections(e, "sky", 6); bounce = _sections(e, "bounce", 3); point = _sections(e, "point", 18)
    s2 = b"".join(struct.pack("<I", len(p)) + p for z in range(nz) for p in list(sky[z]) + list(bounce[z]))
    s3 = b"".join(struct.pack("<I", len(p)) + p for z in range(nz) for p in point[z])
    out += [struct.pack("<I", len(s2)), s2, struct.pack("<fI", e["point_max"], len(s3)), s3]
    return b"".join(out)


# ----------------------------------------------------------------------------------------------------------------- values of the probes
QUANT = {b"PRT4": (999, 3999), b"PRT3": (10000, 40000)}     # (sky visibility steps, bounce steps) the client reads per magic
BOUNCE_MIN, BOUNCE_RANGE = -2.0, 4.0                        # bounce value = u16 / steps * 4 - 2
POINT_STEPS = 1999.9999                                     # point light steps per unit of the entry's point_max


def cell_index(x, y, z, dims):
    """Canonical cell number of grid cell (x, y, z): the order of the bit masks and of the probe positions."""
    nx, ny, nz = dims
    return (np.asarray(y) * nx + np.asarray(x)) * nz + np.asarray(z)


def cell_xyz(c, dims):
    nx, ny, nz = dims; c = np.asarray(c)
    return np.stack([(c // nz) % nx, c // nz // nx, c % nz], -1)


def point_steps(point_max):
    """The integer the editor divides point light values by: (uint)(max * 1999.9999) cut to 16 bits (the client does
    the same cut, so values above ~32.8 decode consistently but with a coarse step)."""
    return int(np.float32(point_max) * np.float32(POINT_STEPS)) & 0xffff


def probes(e, magic=b"PRT4", mid=False):
    """The active probes of an entry decoded to the values the client puts in its colour buffer (before scene
    multipliers). -> dict(cells (n,), xyz (n, 3), position (n, 3), sky (n, 6), bounce (n, 6, 4, 3), point (n, 6, 3),
    occluded (n,) bool, inactive). Directions in DIRS order; bounce[:, d, j, c] = colour channel c of SH coefficient j
    (Y00, Y1-1, Y10, Y11 of the sun direction) for basis direction d. inactive = dict(sky=(cells, (m, 6)), point=(cells,
    (m, 6, 3)), visibility=cells): what the editor left in cells that are not active probes (point light data in 128 of
    the 174 shipped PRT4 entries, a visibility bit in 4 entries, sky never); the client never reads them, build_entry
    writes them back. mid=True returns the middle of each quantisation step instead of its bottom, so that build_entry writes the
    same integers back (the client's own decode, mid=False, re-encodes about 2 % of the values one step lower)."""
    nx, ny, nz = e["dims"]; qs, qb = QUANT[magic]; h = np.float32(0.5 if mid else 0.0)
    cells = np.nonzero(e["active"])[0]; xyz = cell_xyz(cells, e["dims"]); x, y, z = xyz.T
    sky_all = e["sky"].reshape(nz, 6, nx, ny).transpose(0, 3, 2, 1)                                   # [z, y, x, d]: buffers are x-major
    sky = (sky_all[z, y, x].astype(np.float32) + h) / np.float32(qs)                                  # (n, 6)
    b = e["bounce"].reshape(nz, 3, ny, 4, nx, 6)                                                      # [z, c, y, j, x, d]
    bounce = (b[z, :, y, :, x, :].transpose(0, 3, 2, 1).astype(np.float32) + h) / np.float32(qb) * np.float32(BOUNCE_RANGE) + np.float32(BOUNCE_MIN)
    pflat = e["point"].transpose(0, 2, 3, 1).reshape(nz * nx * ny, 18)
    pdec = lambda u: (u.astype(np.float32) + h * (u > 0)) / np.float32(point_steps(e["point_max"]) or 1) * np.float32(e["point_max"])
    point = pdec(pflat[cells]).reshape(-1, 6, 3)
    ic = np.nonzero(~e["active"])[0]; ixyz = cell_xyz(ic, e["dims"]); iv = sky_all[ixyz[:, 2], ixyz[:, 1], ixyz[:, 0]]
    ks = iv.any(axis=1); kp = pflat[ic].any(axis=1)
    inactive = dict(sky=(ic[ks], (iv[ks].astype(np.float32) + h) / np.float32(qs)), point=(ic[kp], pdec(pflat[ic[kp]]).reshape(-1, 6, 3)),
                    visibility=ic[e["visibility"][ic]])
    return dict(cells=cells, xyz=xyz, position=e["positions"].astype(np.float64), sky=sky, bounce=bounce, point=point,
                occluded=e["visibility"][cells], inactive=inactive)


def build_entry(dims, spacing, bounds_min, cells, position, sky, bounce=None, point=None, occluded=None, vis_res=32, inactive=None, point_max=None, bounds_max=None):
    """A new entry as the editor writes it (PRT4 quantisation, truncation toward zero, inactive cells 0 sky / 1999
    bounce / 0 point). cells: canonical cell numbers of the active probes, ascending; position (n, 3); sky (n, 6) in 0..1;
    bounce (n, 6, 4, 3) in [-2, 2] (clamped) or None for none; point (n, 6, 3) >= 0 or None; occluded (n,) bool or None.
    point_max: the point light scale (the editor: the largest point value, at least 1); inactive: see probes();
    bounds_max: default bounds_min + dims * spacing (y with the x spacing: the editor writes the x spacing twice)."""
    nx, ny, nz = dims; sx, sy, sz = spacing; n = len(cells); cells = np.asarray(cells, np.int64)
    if n and np.any(np.diff(cells) <= 0): raise ValueError("cells must be ascending and distinct")
    f32 = np.float32
    bounce = np.zeros((n, 6, 4, 3), f32) if bounce is None else np.asarray(bounce, f32)
    point = np.zeros((n, 6, 3), f32) if point is None else np.asarray(point, f32)
    active = np.zeros(nx * ny * nz, bool); active[cells] = True
    vis = np.zeros_like(active)
    if occluded is not None: vis[cells] = np.asarray(occluded, bool)
    if inactive is not None and "visibility" in inactive: vis[np.asarray(inactive["visibility"], np.int64)] = True
    xyz = cell_xyz(cells, dims); x, y, z = xyz.T
    sky_img = np.zeros((nz, 6, nx, ny), np.uint16)                                                     # x-major buffers of nx x ny images
    sky_img[z, :, x, y] = (f32(999) * np.asarray(sky, f32)).astype(np.int32).astype(np.uint16)
    if inactive is not None and len(inactive["sky"][0]):
        ix, iy, iz = cell_xyz(inactive["sky"][0], dims).T
        sky_img[iz, :, ix, iy] = (f32(999) * np.asarray(inactive["sky"][1], f32)).astype(np.int32).astype(np.uint16)
    full = np.zeros((nz, ny, nx, 6, 4, 3), f32); full[z, y, x] = bounce                               # inactive cells: value 0
    q = (f32(3999) * (np.clip(full, f32(-2), f32(2)) - f32(-2)) * f32(0.25)).astype(np.int32).astype(np.uint16)
    bounce_img = q.transpose(0, 5, 1, 4, 2, 3).reshape(nz, 3, 4 * ny, 6 * nx)                         # [z, c, y, j, x, d]
    pmax = float(max(f32(1.0), point.max() if n else f32(1.0))) if point_max is None else float(point_max)
    steps = f32(point_steps(pmax)); inv = f32(1.0) / f32(pmax)
    flat = np.zeros((nx * ny * nz, 18), f32); flat[cells] = point.reshape(n, 18)
    if inactive is not None and len(inactive["point"][0]): flat[inactive["point"][0]] = np.asarray(inactive["point"][1], f32).reshape(-1, 18)
    point_img = (steps * flat * inv).astype(np.int32).astype(np.uint16).reshape(nz, ny, nx, 18).transpose(0, 3, 1, 2)
    bmin = tuple(float(v) for v in bounds_min)
    bmax = (bmin[0] + nx * sx, bmin[1] + ny * sx, bmin[2] + nz * sz) if bounds_max is None else tuple(float(v) for v in bounds_max)
    return dict(dims=(nx, ny, nz), spacing=(sx, sx, sz), bounds_min=bmin, bounds_max=bmax, vis_res=vis_res, active=active,
                visibility=vis, positions=np.asarray(position, "<f4").reshape(-1, 3), sky=sky_img.reshape(nz, 6, ny, nx), bounce=bounce_img,
                point_max=pmax, point=point_img)


def sun_sh(to_sun):
    """The four L1 basis values the client evaluates for the sun (prt_sun_sh_colours_cb, client 1803a0060), window
    factor included: multiply by the sun colour (intensity x colour of the atmosphere) per channel."""
    x, y, z = np.asarray(to_sun, np.float64) / np.linalg.norm(to_sun)
    return np.array([SH_SCALE[0], -SH_SCALE[1] * y, SH_SCALE[2] * z, -SH_SCALE[3] * x]) * SH_WINDOW


def relight(P, to_sun, sun_colour, prt_multiplier=1.0, point_multiplier=1.0):
    """The client's colour buffer for decoded probes (probes()): (n, 6, 4) = per basis direction rgb light and the sky
    visibility w (prt_decode_sky_bounce_slices and prt_decode_point_light_slices, client 180453270 / 180453f90):
    rgb = sum_j max(0, bounce[d, j] * sun_colour * sun_sh(to_sun)[j]) * 2 * prt_multiplier
          + point[d] * prt_point_light_multiplier * 0.3.
    The shader adds the atmosphere's averaged sky colour of direction d times w and weights the six by the normal."""
    L = sun_sh(to_sun)[:, None] * np.asarray(sun_colour, np.float64)[None, :]                          # (4, 3)
    rgb = np.maximum(P["bounce"] * L[None, None], 0.0).sum(2) * 2.0 * prt_multiplier + P["point"] * point_multiplier * 0.3
    return np.concatenate([rgb, P["sky"][:, :, None]], 2)


def grid_for_bounds(lo, hi, spacing=(2.0, 2.5)):
    """(dims, bounds_min) of a probe grid over the box lo..hi the way the shipped grids sit on their two border probes:
    the cell count is the floor of the extent over the spacing, the minimum corner is lo."""
    h, v = spacing; lo = np.asarray(lo, float); hi = np.asarray(hi, float)
    dims = tuple(int(max(1, np.floor(d / s))) for d, s in zip(hi - lo, (h, h, v)))
    return dims, tuple(lo)


def neutral(lo, hi, spacing=(2.0, 2.5), sky=1.0, bounce_y00=None):
    """A prt_data.bin (dict for pack) whose every cell is an active probe at the cell centre that sees the open sky in all
    six directions and gets no bounce light: the client then lights every point with its six averaged sky colours by
    the normal (scaled by prt_multiplier), and sky visibility is 1 as without PRT. bounce_y00=(r, g, b) adds a constant
    bounce term (the Y00 coefficient: proportional to the sun colour, the same for every sun direction)."""
    (nx, ny, nz), bmin = grid_for_bounds(lo, hi, spacing)
    cells = np.arange(nx * ny * nz); xyz = cell_xyz(cells, (nx, ny, nz))
    pos = np.asarray(bmin) + (xyz + 0.5) * np.array([spacing[0], spacing[0], spacing[1]])
    n = len(cells); b = np.zeros((n, 6, 4, 3), np.float32)
    if bounce_y00 is not None: b[:, :, 0, :] = np.asarray(bounce_y00, np.float32)
    e = build_entry((nx, ny, nz), (spacing[0], spacing[0], spacing[1]), bmin, cells, pos, np.full((n, 6), sky, np.float32), b)
    e["name"] = ""
    return dict(magic=b"PRT4", entries=[e])


# ------------------------------------------------------------------------------------------------------------------------------ check
def shipped_files(module=None):
    """Every <game>/Modules/*/SceneObj/*/prt_data.bin (of one module if given), smallest first."""
    pat = os.path.join(config.MODULES, module or "*", "SceneObj", "*", "prt_data.bin")
    return sorted(glob.glob(pat), key=os.path.getsize)


def rebuild_entry(e, magic=b"PRT4"):
    """An entry written again from its decoded probe values (probes, mid-step) by build_entry."""
    P = probes(e, magic, mid=True)
    return build_entry(e["dims"], e["spacing"], e["bounds_min"], P["cells"], P["position"], P["sky"], P["bounce"], P["point"],
                       P["occluded"], e["vis_res"], P["inactive"], e["point_max"], e["bounds_max"])


def check_file(path, values=False):
    """Decode and re-encode one file (every PNG decoded to pixels and written again). values=True also rebuilds every
    PRT4 entry from its decoded probe values (rebuild_entry) and compares that. -> dict of counts."""
    t = time.time(); data = open(path, "rb").read()
    r = dict(path=path, size=len(data), legacy=0, entries=0, entries_identical=0, pngs=0, pngs_identical=0, identical=0, error=None,
             rebuilt=0, rebuilt_identical=0)
    try:
        d = parse(data)
        if d["magic"] == LEGACY:
            r["legacy"] = 1; r["identical"] = int(pack(d) == data); return r
        out = dict(magic=d["magic"], entries=[])
        for ent in d["entries"]:
            e = parse_entry(ent["blob"]); raw = parse_entry(ent["blob"], images=False)
            for key, k in (("sky", 6), ("bounce", 3), ("point", 18)):
                new = _sections(e, key, k)
                for z in range(len(new)):
                    for a, b in zip(new[z], raw[key + "_png"][z]): r["pngs"] += 1; r["pngs_identical"] += a == b
            blob = pack_entry(e); r["entries"] += 1; r["entries_identical"] += blob == ent["blob"]
            if values and d["magic"] == b"PRT4":
                r["rebuilt"] += 1; r["rebuilt_identical"] += pack_entry(rebuild_entry(e)) == ent["blob"]
            out["entries"].append(dict(name=ent["name"], blob=blob)); del e, raw
        r["identical"] = int(pack(out) == data)
    except Exception as ex:                                     # reported, not raised: one bad file must not stop the run
        r["error"] = "%s: %s" % (type(ex).__name__, ex)
    r["seconds"] = time.time() - t
    return r


def check(files, jobs=1, values=False, log=None):
    """check_file over many files (jobs worker processes). -> totals and the per-file results."""
    import functools
    tot = dict(files=0, files_identical=0, legacy=0, entries=0, entries_identical=0, pngs=0, pngs_identical=0, rebuilt=0,
               rebuilt_identical=0, bytes=0, errors=[], results=[])
    def add(r):
        tot["files"] += 1; tot["files_identical"] += r["identical"]; tot["legacy"] += r["legacy"]; tot["bytes"] += r["size"]
        for k in ("entries", "entries_identical", "pngs", "pngs_identical", "rebuilt", "rebuilt_identical"): tot[k] += r[k]
        if r["error"]: tot["errors"].append((r["path"], r["error"]))
        tot["results"].append(r)
        if log: log("%s %d bytes: %s, entries %d of %d, PNGs %d of %d%s%s (%.1f s)" % (os.path.relpath(r["path"], config.MODULES), r["size"],
                    "identical" if r["identical"] else "DIFFERENT", r["entries_identical"], r["entries"], r["pngs_identical"], r["pngs"],
                    ", rebuilt from values %d of %d" % (r["rebuilt_identical"], r["rebuilt"]) if values else "",
                    " legacy PRT1" if r["legacy"] else "", r.get("seconds", 0)))
        if r["error"] and log: log("  error: " + r["error"])
    job = functools.partial(check_file, values=values)
    if jobs > 1:
        import multiprocessing
        with multiprocessing.Pool(jobs) as pool:
            for r in pool.imap_unordered(job, files): add(r)
    else:
        for f in files: add(job(f))
    return tot


# ------------------------------------------------------------------------------------------------------------------------------ command line
def info(path):
    d = read(path)
    print("%s: %s" % (path, d["magic"].decode()))
    if d["magic"] == LEGACY:
        print("  legacy PRT1 file (%d bytes): the client's reader does not accept it, the scene loads no probes" % len(d["raw"])); return 0
    for ent in d["entries"]:
        e = parse_entry(ent["blob"]); P = probes(e, d["magic"])
        print("  entry %-22r grid %d x %d x %d, spacing %.2f / %.2f, min (%.2f, %.2f, %.2f), max (%.2f, %.2f, %.2f)" % (
            ent["name"], *e["dims"], e["spacing"][0], e["spacing"][2], *e["bounds_min"], *e["bounds_max"]))
        n = len(P["cells"])
        print("    active probes %d of %d cells, visibility-tested %d, point light scale %.3f, %d bytes" % (
            n, len(e["active"]), int(e["visibility"].sum()), e["point_max"], len(ent["blob"])))
        if n:
            print("    sky visibility by direction (%s): %s" % (" ".join(DIRS), " ".join("%.2f" % v for v in P["sky"].mean(0))))
            print("    bounce Y00 mean rgb: %s" % " ".join("%.3f" % v for v in P["bounce"][:, :, 0, :].mean((0, 1))))
    return 0


def main(argv):
    vals = ("--limit", "--module", "--jobs", "--spacing", "--bounds", "--rays", "--probes", "--out")
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in vals)]
    opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["info"] and len(a) == 2:
        p = a[1]
        if not os.path.exists(p):
            config.require_game(); hits = glob.glob(os.path.join(config.MODULES, "*", "SceneObj", p, "prt_data.bin"))
            if not hits: print("no prt_data.bin for %s" % p); return 1
            p = hits[0]
        return info(p)
    if a[:1] == ["check"]:
        config.require_game()
        files = [f for f in shipped_files(opt("--module")) if len(a) < 2 or a[1] in f]
        if opt("--limit"): files = files[:int(opt("--limit"))]
        t = time.time(); r = check(files, int(opt("--jobs", 1)), "--values" in argv, log=print)
        print("prt_data.bin files %d (%d MB): byte-identical %d of %d (%d legacy PRT1 kept as read); entries %d of %d; PNGs %d of %d" % (
            r["files"], r["bytes"] >> 20, r["files_identical"], r["files"], r["legacy"], r["entries_identical"], r["entries"], r["pngs_identical"], r["pngs"]))
        if "--values" in argv: print("PRT4 entries rebuilt from their decoded probe values: byte-identical %d of %d" % (r["rebuilt_identical"], r["rebuilt"]))
        print("%.0f s" % (time.time() - t))
        return 0 if r["files"] and r["files_identical"] == r["files"] and not r["errors"] else 1
    if a[:1] == ["neutral"] and len(a) == 2:
        b = [float(v) for v in opt("--bounds", "").split(",") if v.strip()]
        if len(b) != 6: print("--bounds x0,y0,z0,x1,y1,z1 is needed"); return 2
        sp = tuple(float(v) for v in opt("--spacing", "2,2.5").split(","))
        d = neutral(b[:3], b[3:], sp); write(a[1], d)
        e = d["entries"][0]; print("wrote %s: grid %d x %d x %d, %d probes" % (a[1], *e["dims"], int(e["active"].sum()))); return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
