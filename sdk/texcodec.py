# Texture codecs that tpac.py does not carry: BC7 (all eight modes) and BC6H (all fourteen modes) decoders, a one-mode BC6H
# encoder, the DXT3/BC2 alpha half. numpy only; every function works on whole arrays of blocks.
# Bit layouts are the published BPTC ones (BC7 modes by their parameter table, BC6H modes by their field lists). Measured against
# an independent decoder on random blocks of every mode: BC7 equal on every pixel, BC6H within one level of its 8 bit output; where
# a BC6H layout from memory disagreed (modes 5 and 6) the independent decoder and real BC6H blocks from the game settled it
# (selftest.py section codecs pins the results).
#   decode_bc7(blk) -> (n, 16, 4) uint8 RGBA        blk = (n, 16) uint8 blocks
#   decode_bc6h(blk) -> (n, 16, 3) float32          unsigned half-float RGB (BC6H_UF16), values are exact half-float values
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

# ---- DXT3 alpha: 16 x 4 bits, row major, low nibble first, in the first 8 bytes of the block ----
def dxt3_alpha(a):
    """(n, 16) alpha 0..255 -> (n, 8) uint8."""
    q = np.clip(np.rint(np.asarray(a, np.float32) / 17), 0, 15).astype(np.uint8)
    return (q[:, 0::2] | q[:, 1::2] << 4).astype(np.uint8)

def dxt3_alpha_decode(blk):
    """(n, >= 8) uint8 blocks -> (n, 16) alpha 0..255."""
    b = blk[:, :8]; return (np.stack([b & 15, b >> 4], 2).reshape(len(b), 16).astype(np.int32) * 17).astype(np.uint8)
