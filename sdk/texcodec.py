# Texture codecs that tpac.py does not carry: BC7 (all eight modes) and BC6H (all fourteen modes) decoders, BC7 and BC6H encoders that
# try several modes per block, a one-mode BC6H encoder, the DXT3/BC2 alpha half. numpy only; every function works on whole arrays of blocks.
# Bit layouts are the published BPTC ones (BC7 modes by their parameter table, BC6H modes by their field lists). Measured against
# an independent decoder on random blocks of every mode: BC7 equal on every pixel, BC6H within one level of its 8 bit output; where
# a BC6H layout from memory disagreed (modes 5 and 6) the independent decoder and real BC6H blocks from the game settled it
# (selftest.py section codecs pins the results).
#   decode_bc7(blk) -> (n, 16, 4) uint8 RGBA        blk = (n, 16) uint8 blocks
#   decode_bc6h(blk) -> (n, 16, 3) float32          unsigned half-float RGB (BC6H_UF16), values are exact half-float values
#   enc_bc7_multi(x, quality) -> (n, 16) uint8      x = (n, 16, 4) float 0..255; quality "normal" or "best": the best of modes 6, 1, 3 (opaque
#                                                   blocks) or 6, 7, 5, 4 (blocks with alpha) per block; "best" adds modes 0, 2 and more search
#   enc_bc6h_multi(x, quality) -> (n, 16) uint8     x = (n, 16, 3) float >= 0; the best of the one region modes 11 to 13 and two region modes 1
#                                                   and 2 per block ("normal"), or of all fourteen ("best")
#   enc_bc6h(x) -> (n, 16) uint8                    x = (n, 16, 3) float >= 0, mode 11 only (one subset, 10 bit endpoints)
#   dxt3_alpha(a) / dxt3_alpha_decode(blk)          the 4 bit alpha half of a DXT3 block
#   bc6h_modes(blk)                                 mode number (1..14) of each block, 0 for a reserved mode
import numpy as np

# 2-subset partitions: bit i set = pixel i is in subset 1 (the first 32 are also BC6H's). 3-subset: two bits per pixel.
PART2_MASK = (
    0xcccc, 0x8888, 0xeeee, 0xecc8, 0xc880, 0xfeec, 0xfec8, 0xec80,
    0xc800, 0xffec, 0xfe80, 0xe800, 0xffe8, 0xff00, 0xfff0, 0xf000,
    0xf710, 0x008e, 0x7100, 0x08ce, 0x008c, 0x7310, 0x3100, 0x8cce,
    0x088c, 0x3110, 0x6666, 0x366c, 0x17e8, 0x0ff0, 0x718e, 0x399c,
    0xaaaa, 0xf0f0, 0x5a5a, 0x33cc, 0x3c3c, 0x55aa, 0x9696, 0xa55a,
    0x73ce, 0x13c8, 0x324c, 0x3bdc, 0x6996, 0xc33c, 0x9966, 0x0660,
    0x0272, 0x04e4, 0x4e40, 0x2720, 0xc936, 0x936c, 0x39c6, 0x639c,
    0x9336, 0x9cc6, 0x817e, 0xe718, 0xccf0, 0x0fcc, 0x7744, 0xee22,
)
PART3_PACKED = (
    0xaa685050, 0x6a5a5040, 0x5a5a4200, 0x5450a0a8, 0xa5a50000, 0xa0a05050, 0x5555a0a0, 0x5a5a5050,
    0xaa550000, 0xaa555500, 0xaaaa5500, 0x90909090, 0x94949494, 0xa4a4a4a4, 0xa9a59450, 0x2a0a4250,
    0xa5945040, 0x0a425054, 0xa5a5a500, 0x55a0a0a0, 0xa8a85454, 0x6a6a4040, 0xa4a45000, 0x1a1a0500,
    0x0050a4a4, 0xaaa59090, 0x14696914, 0x69691400, 0xa08585a0, 0xaa821414, 0x50a4a450, 0x6a5a0200,
    0xa9a58000, 0x5090a0a8, 0xa8a09050, 0x24242424, 0x00aa5500, 0x24924924, 0x24499224, 0x50a50a50,
    0x500aa550, 0xaaaa4444, 0x66660000, 0xa5a0a5a0, 0x50a050a0, 0x69286928, 0x44aaaa44, 0x66666600,
    0xaa444444, 0x54a854a8, 0x95809580, 0x96969600, 0xa85454a8, 0x80959580, 0xaa141414, 0x96960000,
    0xaaaa1414, 0xa05050a0, 0xa0a5a5a0, 0x96000000, 0x40804080, 0xa9a8a9a8, 0xaaaaaa44, 0x2a4a5254,
)
# the pixel whose index drops its top bit in the other subsets (subset 0's is always pixel 0)
ANCHOR2 = (15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 2, 8, 2, 2, 8, 8, 15, 2, 8, 2, 2, 8, 8, 2, 2,
           15, 15, 6, 8, 2, 8, 15, 15, 2, 8, 2, 2, 2, 15, 15, 6, 6, 2, 6, 8, 15, 15, 2, 2, 15, 15, 15, 15, 15, 2, 2, 15)
ANCHOR3_1 = (3, 3, 15, 15, 8, 3, 15, 15, 8, 8, 6, 6, 6, 5, 3, 3, 3, 3, 8, 15, 3, 3, 6, 10, 5, 8, 8, 6, 8, 5, 15, 15,
             8, 15, 3, 5, 6, 10, 8, 15, 15, 3, 15, 5, 15, 15, 15, 15, 3, 15, 5, 5, 5, 8, 5, 10, 5, 10, 8, 13, 15, 12, 3, 3)
ANCHOR3_2 = (15, 8, 8, 3, 15, 15, 3, 8, 15, 15, 15, 15, 15, 15, 15, 8, 15, 8, 15, 3, 15, 8, 15, 8, 3, 15, 6, 10, 15, 15, 10, 8,
             15, 3, 15, 10, 10, 8, 9, 10, 6, 15, 8, 15, 3, 6, 6, 8, 15, 3, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 3, 15, 15, 8)
PART2 = np.array([[m >> i & 1 for i in range(16)] for m in PART2_MASK], np.uint8)
PART3 = np.array([[m >> 2 * i & 3 for i in range(16)] for m in PART3_PACKED], np.uint8)
WEIGHTS = {2: (0, 21, 43, 64), 3: (0, 9, 18, 27, 37, 46, 55, 64), 4: (0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64)}

def _bits(blk):
    return np.unpackbits(np.ascontiguousarray(blk, np.uint8), axis=1, bitorder="little")   # (n, 128), bit i = bit i of the block

def _get(bits, pos, nb):
    return (bits[:, pos:pos + nb].astype(np.int32) << np.arange(nb, dtype=np.int32)).sum(1) if nb else np.zeros(len(bits), np.int32)

def _indices(bits, start, ib, anchors):
    """(n, 16) indices of ib bits stored from bit `start`; the pixels flagged in anchors (n, 16) bool store ib - 1 bits."""
    wd = ib - anchors.astype(np.int32); off = start + np.cumsum(wd, 1) - wd; out = np.zeros(anchors.shape, np.int32)
    for j in range(ib):
        out |= (np.take_along_axis(bits, np.minimum(off + j, 127), 1).astype(np.int32) * (j < wd)) << j
    return out

def _anchor_mask(ns, part):
    n = len(part); m = np.zeros((n, 16), bool); m[:, 0] = True
    if ns >= 2: m[np.arange(n), np.array(ANCHOR2)[part] if ns == 2 else np.array(ANCHOR3_1)[part]] = True
    if ns == 3: m[np.arange(n), np.array(ANCHOR3_2)[part]] = True
    return m

# BC7 modes: subsets, partition bits, rotation bits, index selection bit, colour bits, alpha bits, p-bit per endpoint, p-bit per subset,
# index bits, second index bits
BC7_MODES = ((3, 4, 0, 0, 4, 0, 1, 0, 3, 0), (2, 6, 0, 0, 6, 0, 0, 1, 3, 0), (3, 6, 0, 0, 5, 0, 0, 0, 2, 0), (2, 6, 0, 0, 7, 0, 1, 0, 2, 0),
             (1, 0, 2, 1, 5, 6, 0, 0, 2, 3), (1, 0, 2, 0, 7, 8, 0, 0, 2, 2), (1, 0, 0, 0, 7, 7, 1, 0, 4, 0), (2, 6, 0, 0, 5, 5, 1, 0, 2, 0))

def _expand(v, bits):
    return (v << (8 - bits)) | (v >> (2 * bits - 8)) if bits < 8 else v

def _lerp(e0, e1, w):
    return (e0 * (64 - w) + e1 * w + 32) >> 6

def decode_bc7(blk):
    """(n, 16) uint8 BC7 blocks -> (n, 16, 4) uint8 RGBA, all eight modes (a reserved block, first byte 0, decodes to zeros)."""
    n = len(blk); out = np.zeros((n, 16, 4), np.uint8)
    first = blk[:, 0].astype(np.int32); mode = np.full(n, 8)
    for m in range(7, -1, -1): mode[(first & ((1 << (m + 1)) - 1)) == (1 << m)] = m
    for m in range(8):
        sel = np.flatnonzero(mode == m)
        if not len(sel): continue
        for lo in range(0, len(sel), 1 << 16):   # chunks keep the (n, 128) bit matrix small
            s = sel[lo:lo + (1 << 16)]; out[s] = _bc7_mode(_bits(blk[s]), m)
    return out

def _bc7_mode(bits, m):
    ns, pb, rb, isb, cb, ab, epb, spb, ib, ib2 = BC7_MODES[m]; n = len(bits); pos = m + 1
    part = _get(bits, pos, pb); pos += pb
    rot = _get(bits, pos, rb); pos += rb
    sel = _get(bits, pos, isb); pos += isb
    ne = 2 * ns; col = np.zeros((n, ne, 4), np.int32)
    for ch in range(3):
        for e in range(ne): col[:, e, ch] = _get(bits, pos, cb); pos += cb
    for e in range(ne):
        if ab: col[:, e, 3] = _get(bits, pos, ab); pos += ab
    pbit = None
    if epb: pbit = np.stack([_get(bits, pos + e, 1) for e in range(ne)], 1); pos += ne
    elif spb: pbit = np.repeat(np.stack([_get(bits, pos + s, 1) for s in range(ns)], 1), 2, 1); pos += ns
    cbits, abits = cb, ab
    if pbit is not None:
        col[:, :, :3] = col[:, :, :3] << 1 | pbit[:, :, None]; cbits += 1
        if ab: col[:, :, 3] = col[:, :, 3] << 1 | pbit; abits += 1
    col[:, :, :3] = _expand(col[:, :, :3], cbits)
    col[:, :, 3] = _expand(col[:, :, 3], abits) if ab else 255
    ss = (PART2 if ns == 2 else PART3)[part] if ns > 1 else np.zeros((n, 16), np.uint8)   # subset of each pixel
    anch = _anchor_mask(ns, part)
    i1 = _indices(bits, pos, ib, anch); pos += ib * 16 - ns   # every subset's anchor pixel stores one bit less
    g = lambda x: np.array(WEIGHTS[x], np.int32)
    i0 = (2 * ss.astype(np.int64))[:, :, None].repeat(4, 2)
    e0 = np.take_along_axis(col, i0, 1); e1 = np.take_along_axis(col, i0 + 1, 1)
    if ib2:   # modes 4 and 5: colour and alpha have their own index sets; mode 4's selection bit swaps them
        i2 = _indices(bits, pos, ib2, anch)
        if m == 4:
            sw = sel[:, None] == 1; cw = np.where(sw, g(3)[i2], g(2)[i1]); aw = np.where(sw, g(2)[i1], g(3)[i2])
        else:
            cw, aw = g(2)[i1], g(2)[i2]
        res = np.empty((n, 16, 4), np.int32)
        res[:, :, :3] = _lerp(e0[:, :, :3], e1[:, :, :3], cw[:, :, None]); res[:, :, 3] = _lerp(e0[:, :, 3], e1[:, :, 3], aw)
    else:
        res = _lerp(e0, e1, g(ib)[i1][:, :, None])
    for r in (1, 2, 3):   # rotation swaps alpha with red, green or blue
        k = rot == r
        if k.any(): res[k, :, r - 1], res[k, :, 3] = res[k, :, 3].copy(), res[k, :, r - 1].copy()
    return res.astype(np.uint8)

# BC6H field lists, in stream order. M = mode bits, D = partition; RW..BZ = the four endpoints' components (w, x, y, z); "A-B" runs
# from bit A to bit B (either direction). Modes 1..14 have mode values 0, 1, 2, 6, 10, 14, 18, 22, 26, 30, 3, 7, 11, 15.
BC6H_SPEC = (
    "M0-1 GY4 BY4 BZ4 RW0-9 GW0-9 BW0-9 RX0-4 GZ4 GY0-3 GX0-4 BZ0 GZ0-3 BX0-4 BZ1 BY0-3 RY0-4 BZ2 RZ0-4 BZ3 D0-4",
    "M0-1 GY5 GZ4 GZ5 RW0-6 BZ0 BZ1 BY4 GW0-6 BY5 BZ2 GY4 BW0-6 BZ3 BZ5 BZ4 RX0-5 GY0-3 GX0-5 GZ0-3 BX0-5 BY0-3 RY0-5 RZ0-5 D0-4",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-4 RW10 GY0-3 GX0-3 GW10 BZ0 GZ0-3 BX0-3 BW10 BZ1 BY0-3 RY0-4 BZ2 RZ0-4 BZ3 D0-4",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-3 RW10 GZ4 GY0-3 GX0-4 GW10 GZ0-3 BX0-3 BW10 BZ1 BY0-3 RY0-3 BZ0 BZ2 RZ0-3 GY4 BZ3 D0-4",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-3 RW10 BY4 GY0-3 GX0-3 GW10 BZ0 GZ0-3 BX0-4 BW10 BY0-3 RY0-3 BZ1 BZ2 RZ0-3 BZ4 BZ3 D0-4",
    "M0-4 RW0-8 BY4 GW0-8 GY4 BW0-8 BZ4 RX0-4 GZ4 GY0-3 GX0-4 BZ0 GZ0-3 BX0-4 BZ1 BY0-3 RY0-4 BZ2 RZ0-4 BZ3 D0-4",
    "M0-4 RW0-7 GZ4 BY4 GW0-7 BZ2 GY4 BW0-7 BZ3 BZ4 RX0-5 GY0-3 GX0-4 BZ0 GZ0-3 BX0-4 BZ1 BY0-3 RY0-5 RZ0-5 D0-4",
    "M0-4 RW0-7 BZ0 BY4 GW0-7 GY5 GY4 BW0-7 GZ5 BZ4 RX0-4 GZ4 GY0-3 GX0-5 GZ0-3 BX0-4 BZ1 BY0-3 RY0-4 BZ2 RZ0-4 BZ3 D0-4",
    "M0-4 RW0-7 BZ1 BY4 GW0-7 BY5 GY4 BW0-7 BZ5 BZ4 RX0-4 GZ4 GY0-3 GX0-4 BZ0 GZ0-3 BX0-5 BY0-3 RY0-4 BZ2 RZ0-4 BZ3 D0-4",
    "M0-4 RW0-5 GZ4 BZ0 BZ1 BY4 GW0-5 GY5 BY5 BZ2 GY4 BW0-5 GZ5 BZ3 BZ5 BZ4 RX0-5 GY0-3 GX0-5 GZ0-3 BX0-5 BY0-3 RY0-5 RZ0-5 D0-4",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-9 GX0-9 BX0-9",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-8 RW10 GX0-8 GW10 BX0-8 BW10",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-7 RW11 RW10 GX0-7 GW11 GW10 BX0-7 BW11 BW10",
    "M0-4 RW0-9 GW0-9 BW0-9 RX0-3 RW15-10 GX0-3 GW15-10 BX0-3 BW15-10",
)
BC6H_MODE_VALUE = (0, 1, 2, 6, 10, 14, 18, 22, 26, 30, 3, 7, 11, 15)   # the mode field of modes 1..14 (2 bits for the first two)
BC6H_PREC = ((10, 5, 5, 5), (7, 6, 6, 6), (11, 5, 4, 4), (11, 4, 5, 4), (11, 4, 4, 5), (9, 5, 5, 5), (8, 6, 5, 5), (8, 5, 6, 5), (8, 5, 5, 6),
             (6, 6, 6, 6), (10, 10, 10, 10), (11, 9, 9, 9), (12, 8, 8, 8), (16, 4, 4, 4))   # w bits, then the x/y/z delta bits per R, G, B
BC6H_NO_DELTA = (9, 10)   # mode indices (0 based) whose endpoints are stored as they are

def _bc6h_fields(spec):
    out = []
    for tok in spec.split():
        name = tok.rstrip("0123456789-"); rng = tok[len(name):]
        a, _, b = rng.partition("-"); a = int(a); b = int(b) if b else a
        out += [(name, k) for k in (range(a, b + 1) if a <= b else range(a, b - 1, -1))]
    return out

BC6H_FIELDS = [_bc6h_fields(s) for s in BC6H_SPEC]
for _m, _f in enumerate(BC6H_FIELDS): assert len(_f) == (82 if _m < 10 else 65), _m

def bc6h_modes(blk):
    """Mode (1..14) of each (n, 16) block; 0 for the four reserved mode values."""
    v = blk[:, 0].astype(np.int32); out = np.zeros(len(blk), np.int32)
    out[(v & 3) == 0] = 1; out[(v & 3) == 1] = 2
    for k in range(2, 14): out[(v & 31) == BC6H_MODE_VALUE[k]] = k + 1
    return out

def _half_from_u(u):
    return ((u * 31) >> 6).astype(np.uint16)   # BC6H_UF16's last step: the 16 bit interpolation domain to half-float bits

def decode_bc6h(blk):
    """(n, 16) uint8 BC6H_UF16 blocks -> (n, 16, 3) float32 (reserved modes decode to zero)."""
    n = len(blk); out = np.zeros((n, 16, 3), np.float32); mode = bc6h_modes(blk)
    for m in range(1, 15):
        sel = np.flatnonzero(mode == m)
        for lo in range(0, len(sel), 1 << 16):
            s = sel[lo:lo + (1 << 16)]; out[s] = _bc6h_mode(_bits(blk[s]), m - 1)
    return out

def _bc6h_mode(bits, m):
    n = len(bits); fields = BC6H_FIELDS[m]; v = {}
    for p, (name, bit) in enumerate(fields):
        if name != "M" and name != "D": v[name] = v.get(name, 0) + (bits[:, p].astype(np.int32) << bit)
    part = sum(bits[:, p].astype(np.int32) << bit for p, (name, bit) in enumerate(fields) if name == "D") if m < 10 else np.zeros(n, np.int32)
    wb, *db = BC6H_PREC[m]; two = m < 10; ne = 4 if two else 2
    ep = np.zeros((n, ne, 3), np.int64); mask = (1 << wb) - 1
    for c, ch in enumerate("RGB"):
        base = np.asarray(v[ch + "W"], np.int64); ep[:, 0, c] = base
        for e, tag in enumerate("XYZ"[:ne - 1]):
            d = np.asarray(v[ch + tag], np.int64)
            if m not in BC6H_NO_DELTA:
                sb = db[c]; d = (d ^ (1 << (sb - 1))) - (1 << (sb - 1))   # sign extend the delta
                d = (base + d) & mask
            ep[:, e + 1, c] = d
    u = np.where(ep == 0, 0, np.where(ep == mask, 0xFFFF, ((ep << 15) + 0x4000) >> (wb - 1))) if wb < 15 else ep
    ib = 3 if two else 4
    ss = PART2[part] if two else np.zeros((n, 16), np.uint8)
    anch = np.zeros((n, 16), bool); anch[:, 0] = True
    if two: anch[np.arange(n), np.array(ANCHOR2)[part]] = True
    idx = _indices(bits, 82 if two else 65, ib, anch)
    w = np.array(WEIGHTS[ib], np.int64)[idx]
    e0 = np.take_along_axis(u, (2 * ss.astype(np.int64))[:, :, None].repeat(3, 2), 1)
    e1 = np.take_along_axis(u, (2 * ss.astype(np.int64) + 1)[:, :, None].repeat(3, 2), 1)
    res = _half_from_u(_lerp(e0, e1, w[:, :, None]))
    return res.view(np.float16).astype(np.float32)

# ---- BC6H encoder: mode 11 only (one subset, two 10 bit endpoints, 4 bit indices) ----
def _unq10(q):
    q = np.asarray(q, np.int64)
    return np.where(q == 0, 0, np.where(q == 1023, 0xFFFF, ((q << 15) + 0x4000) >> 9))

def _pack_fields(fields, n):
    lo = np.zeros(n, np.uint64); hi = np.zeros(n, np.uint64); pos = 0
    for val, nb in fields:
        val = np.asarray(val).astype(np.uint64)
        if pos + nb <= 64: lo |= val << np.uint64(pos)
        elif pos >= 64: hi |= val << np.uint64(pos - 64)
        else: lo |= (val << np.uint64(pos)) & np.uint64(0xFFFFFFFFFFFFFFFF); hi |= val >> np.uint64(64 - pos)
        pos += nb
    return np.concatenate([lo.astype("<u8").view(np.uint8).reshape(n, 8), hi.astype("<u8").view(np.uint8).reshape(n, 8)], 1)

def _pal6(a, b):
    """(n, 3) endpoints in the 16 bit domain -> (n, 16, 3) float32 palette as the decoder reconstructs it."""
    W = np.array(WEIGHTS[4], np.int64)[None, :, None]
    u = _lerp(a[:, None, :].astype(np.int64), b[:, None, :].astype(np.int64), W)
    return _half_from_u(u).view(np.float16).astype(np.float32)

def _enc_bc6h_chunk(x):
    n = len(x)
    x = np.clip(np.nan_to_num(x, nan=0.0, posinf=65504.0), 0, 65504).astype(np.float32)
    h = np.minimum(x.astype(np.float16).view(np.uint16).astype(np.float64), 0x7BFF)
    t = h * 64 / 31                                           # the half-float bit pattern in the interpolation domain
    m = t.mean(1); d = t - m[:, None]
    cov = np.einsum("nki,nkj->nij", d, d)
    v = np.take_along_axis(d, (d ** 2).sum(2).argmax(1)[:, None, None], 1)[:, 0] + 1e-6
    for _ in range(8):
        v = np.einsum("nij,nj->ni", cov, v); v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
    pr = np.einsum("nki,ni->nk", d, v)
    q = lambda c: np.clip(np.rint((c - 32) / 64), 0, 1023).astype(np.int64)
    qa, qb = q(m + v * pr.min(1)[:, None]), q(m + v * pr.max(1)[:, None])
    def fit(qa, qb):
        pal = _pal6(_unq10(qa), _unq10(qb))
        err = ((x[:, :, None, :] - pal[:, None, :, :]) ** 2).sum(-1)
        idx = err.argmin(2); return idx, np.take_along_axis(err, idx[..., None], 2)[..., 0].sum(1)
    idx, err = fit(qa, qb)
    Wt = np.array(WEIGHTS[4], np.float64) / 64
    for _ in range(1):   # least squares endpoint refit in the interpolation domain, kept only when the value error drops (more passes gain < 0.4 dB)
        w = Wt[idx]; u = 1 - w
        A, B, C = (u * u).sum(1), (u * w).sum(1), (w * w).sum(1); det = A * C - B * B; ok = np.abs(det) > 1e-9; det = np.where(ok, det, 1)
        X, Y = (u[..., None] * t).sum(1), (w[..., None] * t).sum(1)
        na = (C[:, None] * X - B[:, None] * Y) / det[:, None]; nb = (A[:, None] * Y - B[:, None] * X) / det[:, None]
        ra, rb = q(na), q(nb); nidx, nerr = fit(ra, rb); bt = ok & (nerr < err)
        qa, qb = np.where(bt[:, None], ra, qa), np.where(bt[:, None], rb, qb); idx = np.where(bt[:, None], nidx, idx); err = np.where(bt, nerr, err)
    sw = idx[:, 0] >= 8   # pixel 0 stores 3 index bits: swap the endpoints and mirror the indices
    idx = np.where(sw[:, None], 15 - idx, idx); qa, qb = np.where(sw[:, None], qb, qa), np.where(sw[:, None], qa, qb)
    f = [(np.full(n, 3), 5)] + [(qa[:, c], 10) for c in range(3)] + [(qb[:, c], 10) for c in range(3)] + [(idx[:, 0], 3)] + [(idx[:, k], 4) for k in range(1, 16)]
    return _pack_fields(f, n)

def enc_bc6h(x):
    """(n, 16, 3) float >= 0 (linear, 1.0 = white) -> (n, 16) uint8 BC6H_UF16 blocks, mode 11 only. Values above 65504 clamp."""
    out = np.empty((len(x), 16), np.uint8)
    for lo in range(0, len(x), 4096): out[lo:lo + 4096] = _enc_bc6h_chunk(np.asarray(x[lo:lo + 4096], np.float32))
    return out

# ---- BC7 encoder: several modes per block, the best kept ----
# Everything works on "groups": a group is the pixels of one subset of one candidate (block, partition, mode), fitted with one pair of
# endpoints. A fit = the ends of the principal axis, quantised (p-bits included), the nearest index per pixel, then least squares
# refits of the endpoints from those indices, each kept only when the squared error drops.
_WT = {k: np.array(v, np.int32) for k, v in WEIGHTS.items()}
_SUBSETS = {1: np.zeros((1, 16), np.uint8), 2: PART2, 3: PART3}
_ANCHOR_TABLES = {2: (ANCHOR2,), 3: (ANCHOR3_1, ANCHOR3_2)}

def _qv(c, bits, pm, p):
    """Float targets c (g, ch) 0..255 -> (q on `bits` bits, the 8 bit value the decoder expands it to, squared error per row); p = the p-bit value if pm."""
    eb = bits + (1 if pm else 0); vt = c * (((1 << eb) - 1) / 255.0)
    q = np.clip(np.rint((vt - p) / 2 if pm else vt), 0, (1 << bits) - 1).astype(np.int32)
    ex = _expand(q * 2 + p if pm else q, eb)
    return q, ex, ((ex - c) ** 2).sum(1)

def _quant_pair(ca, cb, bits, pm):
    """Two endpoints (g, ch) -> (qa, qb, pa, pb, exa, exb). pm: 0 no p-bit, 1 one per endpoint, 2 one shared by both endpoints."""
    if not pm:
        (qa, ea, _), (qb, eb, _) = _qv(ca, bits, 0, 0), _qv(cb, bits, 0, 0); z = np.zeros(len(ca), np.int32)
        return qa, qb, z, z, ea, eb
    A = [_qv(ca, bits, 1, p) for p in (0, 1)]; B = [_qv(cb, bits, 1, p) for p in (0, 1)]
    if pm == 1: pa, pb = A[1][2] < A[0][2], B[1][2] < B[0][2]
    else: pa = pb = A[1][2] + B[1][2] < A[0][2] + B[0][2]
    pick = lambda R, s: (np.where(s[:, None], R[1][0], R[0][0]), np.where(s[:, None], R[1][1], R[0][1]))
    (qa, ea), (qb, eb) = pick(A, pa), pick(B, pb)
    return qa, qb, pa.astype(np.int32), pb.astype(np.int32), ea, eb

def _axis_ends(X, Mf):
    """Ends of the principal axis of each group's points: X (g, 16, ch), Mf (g, 16) 1.0 for the pixels in the group -> (a, b) (g, ch)
    (one channel: the smallest and largest value)."""
    g, _, ch = X.shape
    if ch == 1: return np.where(Mf[..., None] > 0, X, 1e9).min(1), np.where(Mf[..., None] > 0, X, -1e9).max(1)
    m = (X * Mf[..., None]).sum(1) / np.maximum(Mf.sum(1), 1)[:, None]; d = (X - m[:, None]) * Mf[..., None]
    cov = np.matmul(d.transpose(0, 2, 1), d)
    v = d[np.arange(g), (d ** 2).sum(2).argmax(1)] + 1e-3
    for _ in range(4): v = np.einsum("gij,gj->gi", cov, v); v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
    t = np.einsum("gki,gi->gk", d, v)
    return m + v * np.where(Mf > 0, t, 1e9).min(1)[:, None], m + v * np.where(Mf > 0, t, -1e9).max(1)[:, None]

def _ls_ends(X, Mf, w, top):
    """Least squares endpoints (a, b) (g, ch), clipped to 0..top, for the pixels X (g, 16, ch) at weights w (g, 16) in 0..1 of the way from a to b
    (Mf (g, 16) 1.0 for the pixels in the group). ok = False where all pixels share one weight (no unique answer)."""
    w = w * Mf; u = Mf - w
    A, B, C = (u * u).sum(1), (u * w).sum(1), (w * w).sum(1); det = A * C - B * B; ok = np.abs(det) > 1e-4; det = np.where(ok, det, 1)
    Xs, Ys = (u[..., None] * X).sum(1), (w[..., None] * X).sum(1)
    return np.clip((C[:, None] * Xs - B[:, None] * Ys) / det[:, None], 0, top), np.clip((A[:, None] * Ys - B[:, None] * Xs) / det[:, None], 0, top), ok

def _group_fit(X, M, bits, pm, ib, anc, iters):
    """X (g, 16, ch) float32 pixels, M (g, 16) bool = the pixels of each group (None: all), bits = endpoint bits, pm = p-bit mode, ib = index
    bits, anc (g,) = anchor pixel. -> q0, q1 (g, ch), p0, p1 (g,), idx (g, 16), err (g,). The anchor's index comes out with its top bit clear."""
    g, _, ch = X.shape; Wt = _WT[ib]; Mf = np.ones((g, 16), np.float32) if M is None else M.astype(np.float32)
    a, b = _axis_ends(X, Mf); a, b = np.clip(a, 0, 255), np.clip(b, 0, 255)
    rows = np.arange(g)[:, None]; mids = (Wt[:-1] + Wt[1:]) / 2   # the index of a pixel: where its projection on the endpoint line falls among the weights
    def evaluate(a, b):
        qa, qb, pa, pb, ea, eb = _quant_pair(a, b, bits, pm)
        pal = ((ea[:, None, :] * (64 - Wt)[None, :, None] + eb[:, None, :] * Wt[None, :, None] + 32) >> 6).astype(np.float32)
        dv = (eb - ea).astype(np.float32); dd = (dv * dv).sum(1)
        t = np.einsum("gkc,gc->gk", X - ea[:, None, :], dv) * (64 / np.maximum(dd, 1e-6))[:, None]
        idx = np.where(dd[:, None] > 0, np.searchsorted(mids, t), 0)
        return qa, qb, pa, pb, idx, (((X - pal[rows, idx]) ** 2).sum(-1) * Mf).sum(1)
    qa, qb, pa, pb, idx, err = evaluate(a, b); Wf = Wt.astype(np.float32) / 64; L = 1 << ib
    for _ in range(iters):
        na, nb, ok = _ls_ends(X, Mf, Wf[idx], 255)
        ra, rb, rpa, rpb, nidx, nerr = evaluate(na, nb); bt = ok & (nerr < err); s = bt[:, None]
        qa, qb, pa, pb = np.where(s, ra, qa), np.where(s, rb, qb), np.where(bt, rpa, pa), np.where(bt, rpb, pb)
        idx = np.where(s, nidx, idx); err = np.where(bt, nerr, err)
    sw = idx[np.arange(g), anc] >= L // 2   # the anchor stores one bit less: swap the endpoints, mirror the indices
    return (np.where(sw[:, None], qb, qa), np.where(sw[:, None], qa, qb), np.where(sw, pb, pa), np.where(sw, pa, pb),
            np.where(sw[:, None], L - 1 - idx, idx), err)

class _BitWriter:
    """(n, 128) bit matrix filled field by field in stream order, then packed to (n, 16) bytes."""
    def __init__(self, n): self.b = np.zeros((n, 128), np.uint8); self.pos = 0
    def put(self, val, nb):
        val = np.asarray(val, np.int64)
        for j in range(nb): self.b[:, self.pos + j] = (val >> j) & 1
        self.pos += nb
    def indices(self, idx, ib, anchors):   # the pixels flagged in anchors (n, 16) store ib - 1 bits
        wd = ib - anchors.astype(np.int64); off = self.pos + np.cumsum(wd, 1) - wd
        for j in range(ib):
            r, c = np.nonzero(j < wd); self.b[r, off[r, c] + j] = (idx[r, c] >> j) & 1
        self.pos += 16 * ib - int(anchors[0].sum())
    def blocks(self):
        assert self.pos == 128, self.pos
        return np.packbits(self.b, axis=1, bitorder="little")

def _rank_partitions(x, ns, nparts, k):
    """(n, k) the partitions (of the first nparts) whose subsets lie closest to a straight line in colour space, best first. x (n, 16, ch).
    The measure of one subset is 2 (l1 l2 + l1 l3 + l2 l3) / (l1 + l2 + l3) for the eigenvalues l of its colour covariance: zero for a line,
    from the trace and the squared norm of the matrix alone (no eigenvectors)."""
    n, _, ch = x.shape; tab = _SUBSETS[ns][:nparts]; x = x - x.mean(1, keepdims=True)
    mk = (tab[:, None, :] == np.arange(ns)[None, :, None]).reshape(nparts * ns, 16).astype(np.float32)
    cnt = np.maximum(mk.sum(1), 1)[None, :, None, None]; S1 = np.matmul(mk, x)
    S2 = np.matmul(mk, (x[:, :, :, None] * x[:, :, None, :]).reshape(n, 16, ch * ch)).reshape(n, -1, ch, ch)
    cov = S2 - S1[..., :, None] * S1[..., None, :] / cnt
    tr = np.einsum("nqii->nq", cov); res = (tr * tr - (cov * cov).sum((2, 3))) / np.maximum(tr, 1e-6)
    return np.argsort(res.reshape(n, nparts, ns).sum(2), axis=1)[:, :k]

def _bc7_subsets(x, mode, iters, k=1, parts=None):
    """Modes 0, 1, 2, 3, 6, 7 (one field set for colour and alpha) on (n, 16, 4) float32 blocks: the best of k partitions per block
    -> ((n, 16) blocks, (n,) squared error). parts (n, k) = the partitions to try (default: the k best ranked)."""
    ns, pb, _, _, cb, ab, epb, spb, ib, _ = BC7_MODES[mode]; n = len(x); ch = 4 if ab else 3; pm = 1 if epb else 2 if spb else 0
    if ns == 1: parts = np.zeros((n, 1), np.int64)
    elif parts is None: parts = _rank_partitions(x[..., :ch], ns, 1 << pb, k)
    k = parts.shape[1]; sub = _SUBSETS[ns][parts].astype(np.int64)   # (n, k, 16)
    anc = np.zeros((n, k, ns), np.int64)
    for s in range(1, ns): anc[:, :, s] = np.array(_ANCHOR_TABLES[ns][s - 1])[parts]
    sel = sub[:, :, None, :] == np.arange(ns)[None, None, :, None]   # (n, k, ns, 16)
    X = np.broadcast_to(x[:, None, None, :, :ch], (n, k, ns, 16, ch)).reshape(-1, 16, ch)
    q0, q1, p0, p1, idx, err = _group_fit(X, sel.reshape(-1, 16), cb, pm, ib, anc.reshape(-1), iters)
    E = err.reshape(n, k, ns).sum(2); best = E.argmin(1); r = np.arange(n)
    gi = (r * k + best)[:, None] * ns + np.arange(ns)[None, :]   # (n, ns) rows of the winning groups
    part = parts[r, best]; ss = sub[r, best]
    pix = np.take_along_axis(idx.reshape(n, k, ns, 16)[r, best], ss[:, None, :], 1)[:, 0]   # (n, 16) each pixel's index in its own subset
    return _bc7_pack(mode, part, q0[gi], q1[gi], p0[gi], p1[gi], pix, ns, ch), E[r, best]

def _bc7_pack(mode, part, q0, q1, p0, p1, pix, ns, ch):
    """Fields of modes 0..3, 6, 7. q0, q1 (n, ns, ch) endpoints, p0, p1 (n, ns) p-bits, pix (n, 16) indices."""
    _, pb, _, _, cb, ab, epb, spb, ib, _ = BC7_MODES[mode]; w = _BitWriter(len(part)); w.put(1 << mode, mode + 1); w.put(part, pb)
    for c in range(3):
        for s in range(ns): w.put(q0[:, s, c], cb); w.put(q1[:, s, c], cb)
    for s in range(ns):
        if ab: w.put(q0[:, s, 3], ab); w.put(q1[:, s, 3], ab)
    for s in range(ns):
        if epb: w.put(p0[:, s], 1); w.put(p1[:, s], 1)
    if spb:
        for s in range(ns): w.put(p0[:, s], 1)
    w.indices(pix, ib, _anchor_mask(ns, part))
    return w.blocks()

def _bc7_rotated(x, mode, iters, rots=(0, 1, 2, 3)):
    """Modes 4 and 5: colour and alpha with their own endpoints and index sets, one channel swapped into alpha (rotation). Mode 4 also picks
    which index set (2 or 3 bits) the colour takes. -> ((n, 16) blocks, (n,) squared error)."""
    _, _, _, isb, cb, ab, _, _, ib, ib2 = BC7_MODES[mode]; n = len(x); R = len(rots); z = np.zeros(n * R, np.int64)
    xr = np.empty((R, n, 16, 4), np.float32)
    for j, r in enumerate(rots):
        xr[j] = x
        if r: xr[j, :, :, r - 1], xr[j, :, :, 3] = x[:, :, 3], x[:, :, r - 1]
    xr = xr.reshape(-1, 16, 4)
    # options: (colour index bits, alpha index bits) = (ib, ib2 or ib) and, for mode 4's selection bit, the other way round
    opts = [(ib, ib2)] + ([(ib2, ib)] if isb else [])
    fits = []
    for o, (ci, ai) in enumerate(opts):
        c = _group_fit(np.ascontiguousarray(xr[..., :3]), None, cb, 0, ci, z, iters); a = _group_fit(np.ascontiguousarray(xr[..., 3:]), None, ab, 0, ai, z, iters)
        fits.append((c, a))
    tot = np.stack([(c[5] + a[5]).reshape(R, n) for c, a in fits])   # (opts, R, n)
    flat = tot.reshape(len(opts) * R, n).argmin(0); o, j = flat // R, flat % R; r = np.arange(n)
    E = tot.reshape(-1, n)[flat, r]
    def pick(t, i):   # field i of the winning fit's tuple (c or a), rows of the winning rotation
        return np.stack([f[t][i].reshape((R, n) + f[t][i].shape[1:]) for f in fits])[o, j, r]
    w = _BitWriter(n); w.put(1 << mode, mode + 1); w.put(np.array(rots)[j], 2)
    if isb: w.put(o, 1)
    for c in range(3): w.put(pick(0, 0)[:, c], cb); w.put(pick(0, 1)[:, c], cb)
    w.put(pick(1, 0)[:, 0], ab); w.put(pick(1, 1)[:, 0], ab)
    anch = _anchor_mask(1, np.zeros(n, np.int64)); ci, ai = pick(0, 4), pick(1, 4)
    # the first stored index set has ib bits, the second ib2: option 0 puts the colour in the first, option 1 in the second
    first = np.where((o == 0)[:, None], ci, ai); second = np.where((o == 0)[:, None], ai, ci)
    w.indices(first, ib, anch); w.indices(second, ib2, anch)
    return w.blocks(), E

# quality: skip = blocks whose mode 6 squared error is below this keep mode 6; k = partitions tried per two and three subset mode (the k best of
# the ranking); k3 = the same for modes 0 and 2 (0: not tried); iters = least squares refits per fit (iters6 for mode 6); rots = rotations tried
# by modes 4 and 5; plain = also try modes 4 and 5 on opaque blocks
BC7_QUALITY = {"normal": dict(skip=32.0, k=2, k3=0, iters=1, iters6=2, rots=(0, 1, 2, 3), plain=False),
               "best": dict(skip=0.5, k=6, k3=3, iters=3, iters6=3, rots=(0, 1, 2, 3), plain=True)}

def _enc_bc7_chunk(x, q):
    n = len(x); blk = np.zeros((n, 16), np.uint8); err = np.full(n, np.inf)
    def offer(rows, b, e):
        lt = e < err[rows]; blk[rows[lt]] = b[lt]; err[rows[lt]] = e[lt]
    allr = np.arange(n); offer(allr, *_bc7_subsets(x, 6, q.get("iters6", q["iters"])))
    todo = np.flatnonzero(err > q["skip"])
    if not len(todo): return blk
    al = (x[todo, :, 3] < 255).any(1); rows_a, rows_o = todo[al], todo[~al]
    if len(rows_o):
        xo = x[rows_o]; parts = _rank_partitions(xo[..., :3], 2, 64, q["k"])
        for m in (1, 3): offer(rows_o, *_bc7_subsets(xo, m, q["iters"], parts=parts))
        if q["k3"]:
            for m in (0, 2): offer(rows_o, *_bc7_subsets(xo, m, q["iters"], k=q["k3"]))
        if q["plain"]:
            for m in (4, 5): offer(rows_o, *_bc7_rotated(xo, m, q["iters"], q["rots"]))
    if len(rows_a):
        xa = x[rows_a]; offer(rows_a, *_bc7_subsets(xa, 7, q["iters"], k=q["k"]))
        for m in (5, 4): offer(rows_a, *_bc7_rotated(xa, m, q["iters"], q["rots"]))
    return blk

def enc_bc7_multi(x, quality="normal"):
    """(n, 16, 4) float 0..255 -> (n, 16) uint8 BC7 blocks. quality "normal" or "best": mode 6 on every block, then modes 1 and 3 (opaque
    blocks) or 4, 5 and 7 (blocks with alpha) on the blocks mode 6 left with a visible error; "best" tries more partitions, adds the three
    subset modes 0 and 2 and the rotated modes on opaque blocks, and refits more often. Every block is the best of what was tried."""
    q = BC7_QUALITY[quality]; out = np.empty((len(x), 16), np.uint8)
    for lo in range(0, len(x), 2048): out[lo:lo + 2048] = _enc_bc7_chunk(np.asarray(x[lo:lo + 2048], np.float32), q)
    return out

# ---- BC6H encoder, every mode: the same field lists as the decoder, run backwards ----
# A candidate is a mode (0 based index m into BC6H_SPEC) and, for the two region modes, a partition. Endpoints are fitted in the
# interpolation domain (half-float bit pattern * 64 / 31, what the decoder interpolates in), quantised to the mode's precision with the
# delta modes' limit on how far the other endpoints may lie from the first. Each pixel takes the palette entry nearest in the square root
# of linear light, and the modes compete on that error: a display-like weighting (squared error in linear light spends the bits on the
# brightest pixel of a block, and made some pictures worse than the one mode encoder above).
def _unq(q, wb):
    """Quantised endpoint -> the 16 bit value the decoder interpolates from, for wb bit endpoints."""
    q = np.asarray(q, np.int64)
    return q if wb >= 15 else np.where(q == 0, 0, np.where(q == (1 << wb) - 1, 0xFFFF, ((q << 15) + 0x4000) >> (wb - 1)))

def _quant6(E, m):
    """E (nc, ns, 2, 3) float endpoints in the interpolation domain -> (nc, ns, 2, 3) int64 quantised values. Delta modes: the endpoints other
    than the first are held within the signed delta field around it."""
    wb, *db = BC6H_PREC[m]; top = (1 << wb) - 1
    q = np.clip(np.rint(E / (1 << (16 - wb)) - 0.5) if wb < 16 else np.rint(E), 0, top).astype(np.int64)
    if m not in BC6H_NO_DELTA:
        half = 1 << (np.array(db, np.int64) - 1); base = q[:, 0, 0, :]
        q = np.clip(q, np.maximum(base - half, 0)[:, None, None, :], np.minimum(base + half - 1, top)[:, None, None, :])
    return q

def _bc6h_candidates(x, t, m, iters, k=1, parts=None):
    """Mode index m on x (n, 16, 3) linear float32 pixels, t (n, 16, 3) the same in the interpolation domain: the best of k partitions per block
    -> ((n, 16) blocks, (n,) squared error of the square roots of the values). parts (n, k) = partitions to try (default: the k best ranked of the 32 a mode has)."""
    n = len(x); two = m < 10; ns = 2 if two else 1; ib = 3 if two else 4; L = 1 << ib; wb = BC6H_PREC[m][0]
    if not two: parts = np.zeros((n, 1), np.int64)
    elif parts is None: parts = _rank_partitions(t.astype(np.float64), 2, 32, k)
    k = parts.shape[1]; sub = _SUBSETS[ns][parts].astype(np.int64); nc = n * k; G = nc * ns
    anc = np.zeros((n, k, ns), np.int64)
    if two: anc[:, :, 1] = np.array(ANCHOR2)[parts]
    anc = anc.reshape(-1); Mf = (sub[:, :, None, :] == np.arange(ns)[None, None, :, None]).reshape(G, 16).astype(np.float32)
    X = np.broadcast_to(np.sqrt(x)[:, None, None], (n, k, ns, 16, 3)).reshape(G, 16, 3); T = np.broadcast_to(t[:, None, None], (n, k, ns, 16, 3)).reshape(G, 16, 3)
    ta = T[np.arange(G), anc]   # the anchor pixel of each group
    xm = (X * Mf[..., None]).sum(1) / np.maximum(Mf.sum(1), 1)[:, None]; Xc = X - xm[:, None, :]
    Wt = _WT[ib][None, :, None].astype(np.int64); Wf = _WT[ib].astype(np.float32) / 64
    def evaluate(a, b):
        d = b - a; sw = ((ta - a) * d).sum(1) > 0.5 * (d * d).sum(1)   # the anchor pixel's index must be in the lower half: it lies nearer a
        a, b = np.where(sw[:, None], b, a), np.where(sw[:, None], a, b)
        q = _quant6(np.stack([a, b], 1).reshape(nc, ns, 2, 3), m); u = _unq(q, wb).reshape(G, 2, 3)
        hb = _half_from_u(_lerp(u[:, 0:1, :], u[:, 1:2, :], Wt))
        pal = np.sqrt(hb.view(np.float16).astype(np.float32))   # (G, L, 3)
        pal -= xm[:, None, :]   # nearest entry from |p|^2 - 2 x.p, about the group mean so that bright blocks lose no digits
        idx = ((pal * pal).sum(-1)[:, None, :] - 2 * np.matmul(Xc, pal.transpose(0, 2, 1))).argmin(2)
        r = np.arange(G); idx[r, anc] = np.minimum(idx[r, anc], L // 2 - 1)
        return a, b, q, idx, (((Xc - np.take_along_axis(pal, idx[..., None], 1)) ** 2).sum(-1) * Mf).sum(1)
    a, b = _axis_ends(T, Mf); a, b, q, idx, err = evaluate(np.clip(a, 0, 65535), np.clip(b, 0, 65535))
    for _ in range(iters):   # a refit is kept per candidate (the delta limit ties its subsets together) and only when its error drops
        na, nb, ok = _ls_ends(T, Mf, Wf[idx], 65535)
        na, nb = np.where(ok[:, None], na, a), np.where(ok[:, None], nb, b)
        ra, rb, nq, nidx, nerr = evaluate(na, nb); bt = nerr.reshape(nc, ns).sum(1) < err.reshape(nc, ns).sum(1); bg = np.repeat(bt, ns)
        a, b = np.where(bg[:, None], ra, a), np.where(bg[:, None], rb, b); q = np.where(bt[:, None, None, None], nq, q)
        idx = np.where(bg[:, None], nidx, idx); err = np.where(bg, nerr, err)
    E = err.reshape(n, k, ns).sum(2); best = E.argmin(1); r = np.arange(n)
    part = parts[r, best]; qb = q[r * k + best]   # (n, ns, 2, 3)
    ss = sub[r, best]; pix = np.take_along_axis(idx.reshape(n, k, ns, 16)[r, best], ss[:, None, :], 1)[:, 0]
    return _bc6h_pack(m, part, qb, pix), E[r, best]

def _bc6h_pack(m, part, q, pix):
    """Mode index m, partition (n,), q (n, ns, 2, 3) quantised endpoints (subset, endpoint, R/G/B), pix (n, 16) indices -> (n, 16) blocks."""
    n = len(part); wb, *db = BC6H_PREC[m]; two = m < 10; v = {}
    for c, ch in enumerate("RGB"):
        v[ch + "W"] = q[:, 0, 0, c]
        for e, tag in enumerate("XYZ"[:3 if two else 1]):
            qq = q[:, (e + 1) // 2, (e + 1) % 2, c]
            v[ch + tag] = qq if m in BC6H_NO_DELTA else (qq - q[:, 0, 0, c]) & ((1 << db[c]) - 1)
    bits = np.zeros((n, 128), np.uint8)
    for p, (name, bit) in enumerate(BC6H_FIELDS[m]):
        val = (BC6H_MODE_VALUE[m] >> bit) & 1 if name == "M" else (part >> bit) & 1 if name == "D" else (v[name] >> bit) & 1
        bits[:, p] = val
    anch = np.zeros((n, 16), bool); anch[:, 0] = True
    if two: anch[np.arange(n), np.array(ANCHOR2)[part]] = True
    w = _BitWriter(n); w.b = bits; w.pos = 82 if two else 65; w.indices(pix, 3 if two else 4, anch)
    return w.blocks()

# quality: modes = mode indices tried (0 based), k = partitions tried per two region mode, iters = least squares refits per fit;
# skip = blocks that the first mode left with a squared error below skip * (sum of the block's values) keep it (both of square roots)
BC6H_QUALITY = {"normal": dict(modes=(10, 11, 12, 1, 0), k=1, iters=1, skip=1e-6),
                "best": dict(modes=(10, 11, 12, 13) + tuple(range(10)), k=3, iters=2, skip=0.0)}

def _bc6h_domain(x):
    """(n, 16, 3) float -> the same clamped to what a half float holds (linear light), and in the interpolation domain."""
    x = np.clip(np.nan_to_num(x, nan=0.0, posinf=65504.0), 0, 65504).astype(np.float32)
    h = np.minimum(x.astype(np.float16).view(np.uint16).astype(np.float64), 0x7BFF)
    return x, (h * 64 / 31).astype(np.float32)

def _enc_bc6h_multi_chunk(x, q):
    x, t = _bc6h_domain(x)
    n = len(x); blk = np.zeros((n, 16), np.uint8); err = np.full(n, np.inf)
    def offer(rows, b, e):
        lt = e < err[rows]; blk[rows[lt]] = b[lt]; err[rows[lt]] = e[lt]
    rows = np.arange(n); offer(rows, *_bc6h_candidates(x, t, q["modes"][0], q["iters"]))
    rows = np.flatnonzero(err > q["skip"] * x.astype(np.float64).sum((1, 2)))
    if len(rows):
        xs, ts = x[rows], t[rows]; parts = None
        for m in q["modes"][1:]:
            if m < 10:
                if parts is None: parts = _rank_partitions(ts.astype(np.float64), 2, 32, q["k"])
                offer(rows, *_bc6h_candidates(xs, ts, m, q["iters"], parts=parts))
            else: offer(rows, *_bc6h_candidates(xs, ts, m, q["iters"]))
    return blk

def enc_bc6h_multi(x, quality="normal"):
    """(n, 16, 3) float >= 0 -> (n, 16) uint8 BC6H_UF16 blocks, the best of several modes per block. "normal": the one region modes 11 (10 bit
    endpoints), 12 and 13 (11 and 12 bit first endpoint, the second stored as a delta of it: smooth blocks get more precision) and the
    two region modes 1 and 2 with a partition search. "best": all fourteen modes, three partitions per mode, more refits."""
    q = BC6H_QUALITY[quality]; out = np.empty((len(x), 16), np.uint8)
    for lo in range(0, len(x), 2048): out[lo:lo + 2048] = _enc_bc6h_multi_chunk(np.asarray(x[lo:lo + 2048], np.float32), q)
    return out

# ---- DXT3 alpha: 16 x 4 bits, row major, low nibble first, in the first 8 bytes of the block ----
def dxt3_alpha(a):
    """(n, 16) alpha 0..255 -> (n, 8) uint8."""
    q = np.clip(np.rint(np.asarray(a, np.float32) / 17), 0, 15).astype(np.uint8)
    return (q[:, 0::2] | q[:, 1::2] << 4).astype(np.uint8)

def dxt3_alpha_decode(blk):
    """(n, >= 8) uint8 blocks -> (n, 16) alpha 0..255."""
    b = blk[:, :8]; return (np.stack([b & 15, b >> 4], 2).reshape(len(b), 16).astype(np.int32) * 17).astype(np.uint8)
