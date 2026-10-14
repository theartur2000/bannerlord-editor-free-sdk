# Self-test: does this SDK read and write Bannerlord's formats correctly on YOUR game files? It needs only your own install
# (BANNERLORD_DIR); nothing is written to the game folder, scratch output goes to <SDK_STAGE_DIR>/_selftest.
#   python selftest.py [--limit 200] [--max-package-mb 300] [--textures 4] [--caches 300] [--physics 8] [--skip a,b] [--full]
# Sections (each prints PASS or FAIL with counts):
#   packages   parse each sampled Native .tpac and write it again with the SDK's package writer; the rebuilt bytes must equal
#              the file (sha1). The sample is spread over AssetPackages and EmAssetPackages. --full: every package up to
#              --max-package-mb (all 150 AssetPackages rebuild byte-identical on game v1.4.8).
#   textures   Native texture records with inline pixels, every format the SDK writes (DXT1, DXT3, DXT5, BC4, BC5, BC6H, BC7, RGBA8, R8,
#              R16, R16G16F, R16G16B16A16F): the record re-packs byte-identical, the size rule holds (cubemap faces included), the pixel
#              hash rule holds (up to 3 MB of pixels), the import settings round trip, and a decoded top mip re-encoded by the SDK's
#              encoder decodes within a PSNR bound (the uncompressed formats must give back the stored bytes). Textures not tested
#              (too big, no inline pixels, format not supported) are named with counts in the output.
#   codecs     the BC7, BC6H and DXT3 decoders against pinned results on seeded random blocks, and every format the texture writer
#              offers written from a synthetic picture, read back and checked (hashes, size rule, PSNR)
#   caches     Native clip caches ("Optimized animation"), with and without a root channel, with and without raw key channels: parse and
#              re-pack byte-identical, size trailer follows the engine rule; any class that still fails is named with its count
#   skeletons  Native human / horse / camel skeleton definition and user data re-pack byte-identical
#   physics    Native physics shapes (collision bodies): up to --physics shapes per package decode and re-encode byte-identical (record,
#              description, dynamic cook, static cook), their hashes and entry rows follow the SDK's rule, and build() rebuilds each of them
#              from its description byte-identical (cooked PhysX data taken from the game shape); every manifold of those shapes cooked with
#              the game's own PhysX cooking library from its description must equal the stored hulls (all 8 mirrored copies) and mesh; plus
#              a made-up capsule and sphere shape written to a package and read back, a made-up hull and mesh body that physics.encode()
#              must refuse with cook=False, and the same two bodies cooked, written, read back and parsed
#   synthetic  a tiny made-up skeleton, animation, clip, clip cache, texture, material and mesh written to a scratch folder,
#              read back, and run through the validators, which must pass them and must catch four planted faults
# Exit code 0 only when every section passes.
import collections, glob, hashlib, math, os, random, shutil, struct, sys, time
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import numpy as np
import config, tpac, tw_formats, tpac_pack, sdk_skeleton, sdk_mesh, pngio, physics

RESULTS = []


def report(name, ok, detail, notes=()):
    RESULTS.append((name, ok))
    print("%s %-10s %s" % ("PASS" if ok else "FAIL", name, detail), flush=True)
    for n in list(notes)[:6]:
        print("       " + str(n))


# ----------------------------------------------------------------------------------------------------------------- packages
class _Sink:
    def __init__(self): self.h = hashlib.sha1(); self.n = 0
    def write(self, b): self.h.update(b); self.n += len(b)


def native_files():
    return sorted(glob.glob(os.path.join(config.NATIVE, "**", "*.tpac"), recursive=True))


def sample(files, limit, max_mb, full):
    ok = [f for f in files if os.path.getsize(f) <= max_mb << 20]
    skipped = len(files) - len(ok)
    if full or len(ok) <= limit:
        return ok, skipped
    asset = [f for f in ok if os.sep + "AssetPackages" + os.sep in f]
    em = [f for f in ok if os.sep + "EmAssetPackages" + os.sep in f]
    pick = []
    for group, n in ((asset, limit // 4), (em, limit - limit // 4)):
        step = max(1, len(group) // n)
        pick += group[::step][:n]
    return pick, skipped


def rebuild_identical(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        hd = f.read(0x24); meta, = struct.unpack_from("<Q", hd, 0x1c)
        pk = tpac_pack.parse_package(hd + f.read(meta), size)
        sink = _Sink()
        def blob(it, e): f.seek(e["off"]); return f.read(e["stored"])
        tpac_pack.write_package(sink, pk["guid"], pk["items"], blob, pk["version"])
        h = hashlib.sha1(); f.seek(0)
        for b in iter(lambda: f.read(1 << 24), b""): h.update(b)
    return sink.h.digest() == h.digest() and sink.n == size, len(pk["items"])


def section_packages(limit, max_mb, full):
    t0 = time.time(); files, skipped = sample(native_files(), limit, max_mb, full)
    ok = {"AssetPackages": [0, 0], "EmAssetPackages": [0, 0]}; items = 0; bad = []
    for p in files:
        kind = "EmAssetPackages" if os.sep + "EmAssetPackages" + os.sep in p else "AssetPackages"
        try: same, n = rebuild_identical(p)
        except Exception as ex: same, n = False, 0; bad.append("%s: %s" % (os.path.relpath(p, config.NATIVE), ex))
        ok[kind][1] += 1; ok[kind][0] += same; items += n
        if not same and not any(os.path.relpath(p, config.NATIVE) in b for b in bad): bad.append(os.path.relpath(p, config.NATIVE) + ": rebuilt bytes differ")
    tot = sum(v[0] for v in ok.values()); n = sum(v[1] for v in ok.values())
    report("packages", tot == n and n > 0, "%d of %d sampled Native packages rebuild byte-identical (%d items; AssetPackages %d/%d, EmAssetPackages %d/%d; %d larger than %d MB skipped; %.0f s)"
           % (tot, n, items, *ok["AssetPackages"], *ok["EmAssetPackages"], skipped, max_mb, time.time() - t0), bad)


# ---------------------------------------------------------------------------------------------------------------- textures
def _read_entry(f, e):
    f.seek(e["off"]); d = f.read(e["stored"])
    return tpac.lz4_decompress(d, e["raw"]) if e["stored"] < e["raw"] else d


def _psnr(a, b, ch):
    x, y = a.astype(float)[..., ch], b.astype(float)[..., ch]
    mse = ((x - y) ** 2).mean()
    return 99.0 if mse < 1e-9 else 10 * math.log10(255 ** 2 / mse)


LOSSLESS = {"R8G8B8A8_UNORM", "R8_UNORM", "R16_UNORM", "R16G16F", "R16G16B16A16F"}   # the encoder must give back the stored bytes
HASH_MAX = 3_000_000   # bytes of pixel data up to which the pixel hash rule is checked
SAMPLE_MAX = 12_000_000   # bytes of pixel data: larger textures are named in the output, not tested


def _psnr_f(a, b, ch):
    """PSNR of float images clamped to 0..1 (what a display shows of an HDR texture)."""
    x, y = np.clip(a, 0, 1)[..., ch].astype(np.float64), np.clip(b, 0, 1)[..., ch].astype(np.float64)
    mse = ((x - y) ** 2).mean()
    return 99.0 if mse < 1e-12 else 10 * math.log10(1 / mse)


def _bc7_modes(blk):
    b = np.frombuffer(blk, np.uint8).reshape(-1, 16)[:, 0].astype(np.int32); m = np.full(len(b), 8)
    for k in range(7, -1, -1): m[(b & ((1 << (k + 1)) - 1)) == (1 << k)] = k
    return m


def section_textures(per_format):
    import texcodec
    t0 = time.time(); cands = collections.defaultdict(list); skipped = collections.defaultdict(collections.Counter); total = collections.Counter()
    supported = {v[0] for v in tpac.TEX_FORMATS.values()}
    for p in sorted(glob.glob(os.path.join(config.NATIVE, "EmAssetPackages", "**", "*.tpac"), recursive=True)):
        try: items = tw_formats.read_package_index(p)
        except Exception: skipped["package index not read"]["?"] += 1; continue
        for it in items:
            if it["type"] != tpac.TEX_TYPE: continue
            try: r, _ = tpac.parse_tex_record(bytes(it["record"]), 0)
            except Exception: skipped["record layout not read"]["?"] += 1; continue
            fmt = r["format"]; total[fmt] += 1; px = [e for e in it["entries"] if e["type"] == tpac.TEX_PIXELS]
            if fmt not in supported: skipped["format not supported"][fmt] += 1
            elif not px: skipped["no inline pixels"][fmt] += 1
            elif px[0]["raw"] > SAMPLE_MAX: skipped["over %d MB" % (SAMPLE_MAX // 1_000_000)][fmt] += 1
            else: cands[fmt].append((p, it, r, px[0]))
    stats = collections.Counter(); psnr = collections.defaultdict(list); notes = []; modes = collections.defaultdict(collections.Counter)
    for fmt, lst in sorted(cands.items()):
        random.Random(7).shuffle(lst)
        for p, it, r, px in lst[:per_format]:
            stats["textures"] += 1; stats["tex " + fmt] += 1
            rec = bytes(it["record"]); w, h = r["w"], r["h"]
            with open(p, "rb") as f:
                pix = _read_entry(f, px)
                st = [e for e in it["entries"] if e["type"] == tpac.TEX_SETTINGS]
                settings = _read_entry(f, st[0]) if st else None
            try:
                if tpac.pack_tex_record(r) == rec: stats["record identical"] += 1
                elif len(notes) < 3: notes.append("%s %s: record re-pack differs" % (it["name"], fmt))
                if len(pix) <= HASH_MAX:   # the hash is pure Python here: big textures are left out of this one check
                    stats["pixel hash tested"] += 1
                    if tw_formats.texture_pixel_hash(pix) == r["unk8"]: stats["pixel hash"] += 1
                    elif len(notes) < 3: notes.append("%s %s: pixel hash rule differs" % (it["name"], fmt))
                if len(pix) == tpac.tex_bytes(r): stats["size"] += 1
                elif len(notes) < 3: notes.append("%s %s: size rule differs" % (it["name"], fmt))
                if settings is not None:
                    stats["settings seen"] += 1
                    if tw_formats.pack_tex_import_settings(tw_formats.parse_tex_import_settings(settings)) == settings: stats["settings identical"] += 1
                top = pix[:tpac.mip_bytes(w, h, fmt)]
                if fmt == "BC7": modes[fmt].update(int(m) for m in _bc7_modes(top))
                if fmt == "BC6H_UF16": modes[fmt].update(int(m) for m in texcodec.bc6h_modes(np.frombuffer(top, np.uint8).reshape(-1, 16)))
                ch = tpac.PSNR_CHANNELS.get(fmt, [0, 1, 2, 3])
                if fmt in tpac.FLOAT_FORMATS or fmt == "R16_UNORM":   # keep the stored values, not the 8 bit view of them
                    img = tpac.decode_mip_float(r, pix, 0)
                    if not np.isfinite(img).all(): stats["non-finite values"] += 1
                    if fmt in LOSSLESS: enc = tpac.encode_mip(img, fmt); ps = _psnr_f(img, tpac.decode_float(enc, w, h, fmt), ch)
                    else:   # the encoders are slow on big pictures: the lossy ones are measured on a crop of at most 512 x 512
                        c = img[:min(h, 512) // 4 * 4 or h, :min(w, 512) // 4 * 4 or w]; ch_, cw_ = c.shape[:2]
                        ps = _psnr_f(c, tpac.decode_float(tpac.encode_mip(c, fmt), cw_, ch_, fmt), ch)
                else:
                    img = tpac.decode_mip(r, pix, 0)
                    if fmt in LOSSLESS: enc = tpac.encode_mip(img, fmt); ps = _psnr(img, tpac.decode_blocks(enc, w, h, fmt), ch)
                    else:
                        c = img[:min(h, 512) // 4 * 4 or h, :min(w, 512) // 4 * 4 or w]; ch_, cw_ = c.shape[:2]
                        ps = _psnr(c, tpac.decode_blocks(tpac.encode_mip(c, fmt), cw_, ch_, fmt), ch)
                psnr[fmt].append(ps); stats["decoded"] += 1
                if fmt in LOSSLESS:
                    stats["lossless"] += 1
                    if enc == top: stats["lossless exact"] += 1
                    elif len(notes) < 3: notes.append("%s %s: re-encoded bytes differ from the stored ones" % (it["name"], fmt))
                if r["faces"] > 1 and r["mips"] > 1 and w % 2 == 0 and h % 2 == 0:   # cubemap: each face holds its whole mip chain
                    stats["cube"] += 1
                    a0, a1 = tpac.decode_mip_float(r, pix, 0), tpac.decode_mip_float(r, pix, 1)
                    down = a0.reshape(h // 2, 2, w // 2, 2, 4).mean((1, 3)); o = r["faces"] * tpac.mip_bytes(w, h, fmt)
                    alt = tpac.decode_float(pix[o:o + tpac.mip_bytes(w // 2, h // 2, fmt)], w // 2, h // 2, fmt)   # mip 1 if the layout were mip-major
                    stats["cube layout face-major"] += bool(np.abs(down - a1).mean() < np.abs(down - alt).mean())
            except Exception as ex:
                notes.append("%s %s: %s" % (it["name"], fmt, ex))
    # the encoders are lossy: a decode of what they wrote must stay close to the decode of the original
    bound = {"DXT1": 30, "DXT3": 28, "DXT5": 28, "BC4": 38, "BC5": 34, "BC6H_UF16": 25, "BC7": 25, "R8G8B8A8_UNORM": 90, "R8_UNORM": 90, "R16_UNORM": 90,
             "R16G16F": 90, "R16G16B16A16F": 90}
    low = [(f, round(min(v), 1)) for f, v in psnr.items() if min(v) < bound.get(f, 25)]
    # the pixel hash is the engine's rule for textures the editor imported; a Native texture whose pixels were touched after
    # import keeps its old hash, so most (not all) must match
    ok = (stats["textures"] > 0 and stats["record identical"] == stats["textures"] and stats["pixel hash"] >= 0.8 * stats["pixel hash tested"]
          and stats["size"] == stats["textures"] and stats["settings identical"] == stats["settings seen"] and not low and stats["decoded"] == stats["textures"]
          and stats["lossless exact"] == stats["lossless"] and stats["cube layout face-major"] == stats["cube"] and not stats["non-finite values"]
          and not skipped.get("record layout not read") and not skipped.get("format not supported") and set(cands) == supported & set(total))
    per = ", ".join("%s %s (%d)" % (f, "exact" if min(v) >= 99 else "min %.1f dB" % min(v), len(v)) for f, v in sorted(psnr.items()))
    skip = "; ".join("%s: %s" % (why, ", ".join("%s %d" % kv for kv in sorted(c.items()))) for why, c in sorted(skipped.items())) or "none"
    seen = "; ".join("%s modes seen %d of %d" % (f, len(m), 8 if f == "BC7" else 14) for f, m in sorted(modes.items()))
    report("textures", ok, "%d Native textures (%s): record re-pack identical %d, pixel hash rule %d of %d, size rule %d, import settings %d/%d, cubemap layout %d/%d; re-encode PSNR: %s; %s; not tested (%d of %d textures): %s (%.0f s)"
           % (stats["textures"], ", ".join("%s %d" % (f, stats["tex " + f]) for f in sorted(cands)), stats["record identical"], stats["pixel hash"], stats["pixel hash tested"], stats["size"],
              stats["settings identical"], stats["settings seen"], stats["cube layout face-major"], stats["cube"], per or "none decoded", seen or "no mode counts",
              sum(sum(c.values()) for c in skipped.values()), sum(total.values()), skip, time.time() - t0),
           notes + ["below bound: %s" % low] * bool(low))


# --------------------------------------------------------------------------------------------------------------------- codecs
def _stream(seed, n):
    """n deterministic pseudo-random bytes (sha256 in counter mode, the same on every Python and numpy)."""
    out = bytearray(); i = 0
    while len(out) < n: out += hashlib.sha256(seed + b"%d" % i).digest(); i += 1
    return bytes(out[:n])


def _pin_blocks(kind, n):
    """n random 16 byte blocks with the mode bits set so that every mode appears evenly (BC7: 8 modes, BC6H: 14)."""
    import texcodec
    b = np.frombuffer(_stream(kind, n * 16), np.uint8).reshape(n, 16).copy(); k = np.arange(n)
    if kind == b"bc7":
        for mode in range(8): b[k % 8 == mode, 0] = (b[k % 8 == mode, 0] & (0xFF << (mode + 1) & 0xFF)) | (1 << mode)
    else:
        for mode in range(14): b[k % 14 == mode, 0] = (b[k % 14 == mode, 0] & (0xFC if mode < 2 else 0xE0)) | texcodec.BC6H_MODE_VALUE[mode]
    return b


# SHA-256 of what the decoders give on the seeded blocks above. When these values were pinned, an independent decoder gave the same
# BC7 value for every pixel of these 16384 blocks, the same DXT3 alpha and colours within one level on these 4096 blocks, and the
# same BC6H value within one level on these 16800 blocks (the other decoder returns 8 bit pixels). A test fails if a later edit
# changes any decoded value.
PINS = {"bc7": "79818c0fab7c23ffa037568681b1a13452d5fe3e0c06103a2156432094921e90",
        "bc6h": "c8263922e2b2cc45a34ac6b9f74f0cf3aa6b651c2947a36f32f3ee396e7fd6ae",
        "dxt3": "6ff30251b0de3694b8bab98df491c47eeb66f3108049be5d781c52ad2a7aa01d"}


def section_codecs():
    import texcodec
    t0 = time.time(); checks = []; notes = []
    def check(name, ok, why=""):
        checks.append((name, bool(ok)))
        if not ok: notes.append("%s %s" % (name, why))
    got = {"bc7": hashlib.sha256(texcodec.decode_bc7(_pin_blocks(b"bc7", 16384)).tobytes()).hexdigest(),
           "bc6h": hashlib.sha256(texcodec.decode_bc6h(_pin_blocks(b"bc6h", 16800)).astype("<f4").tobytes()).hexdigest(),
           "dxt3": hashlib.sha256(tpac.decode_blocks(_stream(b"dxt3", 4096 * 16), 4096 * 4, 4, "DXT3").tobytes()).hexdigest()}
    for k, v in got.items(): check("%s decoder pin" % k, v == PINS[k], "%s != %s" % (v, PINS[k]))
    # every format end to end through the texture writer: a synthetic picture, written, read back, hashes, size rule and PSNR
    root = os.path.join(config.SDK_STAGE_DIR, "_selftest", "codecs")
    if os.path.isdir(root): shutil.rmtree(root)
    A, R = os.path.join(root, "Assets"), os.path.join(root, "RuntimeDataCache"); os.makedirs(A); os.makedirs(R)
    h, w = 70, 130; y, x = np.mgrid[0:h, 0:w]; rng = np.random.RandomState(5)
    img = np.zeros((h, w, 4), np.uint8)
    img[..., 0] = x * 255 // w; img[..., 1] = y * 255 // h; img[..., 2] = (x + y) * 3 % 256; img[..., 3] = np.where((x // 16 + y // 16) % 2 == 0, 255, 100)
    img[..., :3] = np.clip(img[..., :3].astype(int) + rng.randint(-6, 7, (h, w, 3)), 0, 255)
    png = os.path.join(root, "codec_d.png"); pngio.write_png(png, img)
    floor = {"bc1": 30, "bc2": 30, "bc3": 30, "bc4": 40, "bc5": 40, "bc6h": 30, "bc7": 30, "rgba8": 99, "r8": 99, "r16": 99, "rg16f": 99, "rgba16f": 99}
    chan = {"bc1": [0, 1, 2], "bc2": [0, 1, 2, 3], "bc3": [0, 1, 2, 3], "bc4": [0], "bc5": [0, 1], "bc6h": [0, 1, 2], "bc7": [0, 1, 2, 3], "rgba8": [0, 1, 2, 3],
            "r8": [0], "r16": [0], "rg16f": [0, 1], "rgba16f": [0, 1, 2, 3]}
    res = {}
    for key, (efmt, _) in tpac.TEX_FORMATS.items():
        try:
            tp, rp, r = tpac.write_texture(png, "codec%s_d" % key, key, out_dir=A, rdc_dir=R)
            r2, pix = tpac.tex_pixels(tp, R)
            good = (r2["format"] == efmt and r2["hash_ok"] and all(e["hash_ok"] for e in r2["entries"]) and r2.get("rdc_h1_ok", True) and r2.get("rdc_h2_ok", True)
                    and tw_formats.texture_pixel_hash(pix) == r2["unk8"] and len(pix) == tpac.tex_bytes(r2))
            res[key] = _psnr(img, tpac.decode_mip(r2, pix, 0), chan[key])
            check("%s written and read back" % key, good)
            check("%s PSNR >= %d" % (key, floor[key]), res[key] >= floor[key], "PSNR %.1f" % res[key])
        except Exception as ex:
            check("%s write" % key, False, repr(ex))
    # the one-mode BC6H encoder on a high dynamic range picture: it decodes, stays near the input, and writes mode 11 only
    hdr = (np.exp(np.random.RandomState(2).normal(-1, 1, (64, 64, 1))) * np.linspace(0.1, 4, 64)[None, :, None] * np.ones((1, 1, 3))).astype(np.float32)
    e = tpac.encode_mip(np.concatenate([hdr, np.ones((64, 64, 1), np.float32)], 2), "BC6H_UF16"); dec = tpac.decode_float(e, 64, 64, "BC6H_UF16")[..., :3]
    rel = float(np.median(np.abs(dec - hdr) / np.maximum(hdr, 1e-3)))
    check("BC6H HDR picture decodes finite, median relative error %.3f < 0.15" % rel, np.isfinite(dec).all() and rel < 0.15)
    check("BC6H encoder writes mode 11 only", set(texcodec.bc6h_modes(np.frombuffer(e, np.uint8).reshape(-1, 16)).tolist()) == {11})
    good = sum(1 for _, ok in checks if ok)
    report("codecs", good == len(checks) and checks != [], "%d of %d checks (%s); PSNR of the formats written from a synthetic picture: %s (%.0f s)" % (
        good, len(checks), ", ".join(n for n, ok in checks if not ok) or "all passed", ", ".join("%s %.1f" % kv for kv in res.items()), time.time() - t0), notes)


# ------------------------------------------------------------------------------------------------------------------ caches
def section_caches(limit):
    t0 = time.time(); stats = collections.Counter(); notes = []; bones = collections.Counter(); classes = collections.Counter(); unread = collections.Counter()
    pkgs = sorted(glob.glob(os.path.join(config.NATIVE, "EmAssetPackages", "animations", "*", "*.tpac")))
    per = max(1, limit // max(1, len(pkgs)))
    for p in pkgs:
        n = 0
        try: items = tw_formats.read_package_index(p)
        except Exception: continue
        with open(p, "rb") as f:
            for it in items:
                if it["type"] != tpac.CLIP_TYPE: continue
                for e in it["entries"]:
                    if e["type"] != tpac.OPT_ANIM or n >= per: continue
                    n += 1; stats["caches"] += 1
                    d = _read_entry(f, e)
                    L = None
                    try:
                        L = tw_formats.optanim_layout(d)
                        if L["trailer"] == L["size_in_bytes"] and L["consumed"] == L["length"]: stats["size rule"] += 1
                        elif len(notes) < 4: notes.append("%s: size rule differs" % it["name"])
                    except Exception as ex:
                        if len(notes) < 4: notes.append("%s: layout walk: %s" % (it["name"], ex))
                    # layout class of this cache by the engine walk: with / without the root position channel, with / without raw key channels
                    cls = ("no root channel" if not L["root"][0] else "root channel") + (" + raw keys" if L["raw_keys"] else "") if L else "layout walk failed"
                    classes[cls] += 1
                    try:
                        r = tpac.parse_optanim(d); bones[len(r["bones"])] += 1; stats["parsed"] += 1
                        if tpac.pack_optanim(r) == d: stats["re-pack identical"] += 1
                        else:
                            unread["re-pack differs: " + cls] += 1
                            if len(notes) < 4: notes.append("%s (%d bones): re-pack differs" % (it["name"], len(r["bones"])))
                    except Exception as ex:
                        unread["not read: " + cls] += 1
                        if len(notes) < 4: notes.append("%s: %s" % (it["name"], ex))
    ok = stats["parsed"] > 0 and stats["parsed"] == stats["caches"] and stats["re-pack identical"] == stats["parsed"] and stats["size rule"] == stats["caches"] and not unread
    report("caches", ok, "%d Native clip caches: size trailer rule %d; %d read by the SDK (bone counts %s), %d of them re-pack byte-identical; layout classes %s; still failing: %s (%.0f s)"
           % (stats["caches"], stats["size rule"], stats["parsed"], ", ".join("%d:%d" % kv for kv in sorted(bones.items())[:6]),
              stats["re-pack identical"], ", ".join("%s %d" % kv for kv in sorted(classes.items())),
              ", ".join("%s %d" % kv for kv in sorted(unread.items())) or "none", time.time() - t0), notes)


def section_skeletons():
    stats = collections.Counter(); notes = []
    for nm in sdk_skeleton.NATIVE_SKELETONS:
        try:
            x = sdk_skeleton.native(nm); d, u = x[sdk_skeleton.SKEL_DEF], x[sdk_skeleton.USER_DATA]
            name, bones = sdk_skeleton.parse_definition(d); U = sdk_skeleton.parse_userdata(u)
            stats["skeletons"] += 1
            stats["definition identical"] += sdk_skeleton.pack_definition(name, bones, keep_pad=True) == d
            stats["user data identical"] += sdk_skeleton.pack_userdata(U) == u
            stats["tw_formats identical"] += (tw_formats.pack_skeleton_definition(*tw_formats.parse_skeleton_definition(d)) == d
                                              and tw_formats.pack_skeleton_user_data(tw_formats.parse_skeleton_user_data(u)) == u)
        except Exception as ex:
            notes.append("%s: %s" % (nm, ex))
    n = stats["skeletons"]
    report("skeletons", n > 0 and all(stats[k] == n for k in ("definition identical", "user data identical", "tw_formats identical")),
           "%d of %d Native skeletons re-pack byte-identical (definition %d, user data %d, second implementation %d)"
           % (n, len(sdk_skeleton.NATIVE_SKELETONS), stats["definition identical"], stats["user data identical"], stats["tw_formats identical"]), notes)


# ------------------------------------------------------------------------------------------------------------------ physics
def section_physics(per_package):
    t0 = time.time(); checks = []; notes = []
    def check(name, ok, why=""):
        checks.append((name, bool(ok)))
        if not ok: notes.append("%s %s" % (name, why))
    native = physics.native_packages("Native")
    r = physics.check(native, per_package=per_package); b = physics.check_build(native, per_package=per_package)
    ok = r["shapes"] > 0 and r["identical"] == r["shapes"] and b["shapes"] == r["shapes"] and b["identical"] == b["shapes"]
    notes += ["round trip %s" % (r["failures"],)] * bool(r["failures"]) + ["rebuild %s" % (b["failures"],)] * bool(b["failures"])
    try:
        root = os.path.join(config.SDK_STAGE_DIR, "_selftest", "physics"); os.makedirs(root, exist_ok=True)
        sh = physics.build("selftest_prop", [physics.capsule((0, 0, 0.3), (0, 0, 1.5), 0.25, "wood")], [physics.sphere((0.5, 0, 0.5), 0.2)], material="wood", source=bytes(range(16)))
        out = physics.write_package(sh, root, force=True); res = list(physics.resources(out)); back = physics.decode_resource(res[0]) if len(res) == 1 else None
        bad = physics.check_resource(res[0], stored=True) if res else ["no resource"]
        check("made-up capsule and sphere written and read back", back is not None and back["name"] == "selftest_prop" and len(back["desc"]["capsules"]) == 1
              and len(back["desc"]["spheres"]) == 1 and physics.encode(back)[0] == physics.encode(sh)[0] and not bad, bad)
        bd = back["record"]["bounds"] if back else None
        check("made-up bounds", bd is not None and np.allclose(bd["min"][:3], [-0.25, -0.25, 0.05]) and np.allclose(bd["max"][:3], [0.7, 0.25, 1.75]))
        cube = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float); pts = np.vstack([cube, [[0, 0, 0], [0.5, 0.2, 0.1]]]); F = physics.hull_triangles(pts)
        e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1); _, cnt = np.unique(e, axis=0, return_counts=True)
        vol = sum(np.dot(pts[t[0]], np.cross(pts[t[1]], pts[t[2]])) for t in F) / 6
        check("hull of a cube is closed, outward, volume 8", len(F) == 12 and (cnt == 2).all() and abs(vol - 8) < 1e-5, (len(F), vol))
        hm = physics.convex_hull(cube, ["wood"]); mm = physics.triangle_mesh(hm["vertices"][:, :3], hm["face_index"], ["wood"])
        hb = physics.build("selftest_hull", manifolds=[hm], cook=False); mb = physics.build("selftest_mesh", manifolds=[mm], cook=False)
        check("hull and mesh bodies are refused without cooked data (cook=False)", all(physics.needs_cook(x) for x in (hb, mb)) and all(_raises(physics.encode, x) for x in (hb, mb)))
        # cooked with the game's PhysX library: a hull and a mesh body written, read back, round trip; the cooks parse and the mirror rule holds
        cooked = []
        for nm, man in (("selftest_hull", hm), ("selftest_mesh", mm)):
            s = physics.build(nm, manifolds=[man], source=bytes(range(16))); out = physics.write_package(s, root, force=True); res = list(physics.resources(out))
            back = physics.decode_resource(res[0]) if len(res) == 1 else None; cooked.append(back)
            check("made-up %s body cooked, written and read back" % nm[9:], back is not None and not physics.check_resource(res[0], stored=True) and physics.encode(back) == physics.encode(s)
                  and back["stat"]["meshes"][0]["cook"] == physics.build(nm, manifolds=[man])["stat"]["meshes"][0]["cook"])
        hb = cooked[0]; hs = [physics.hull_cook(hb["dyn"]["groups"][k][0])["vertices"] for k in range(physics.HULL_SLOTS)] if hb else None
        me = physics.mesh_cook(hb["stat"]["meshes"][0]["cook"]) if hb else None
        check("cooked cube: 8 hull corners in all 8 mirrored copies, mesh of 8 vertices and 12 triangles", hs is not None and all(len(h) == 8 for h in hs)
              and all(np.allclose(np.sort(h * physics.MIRROR[k], axis=0), np.sort(hs[7], axis=0)) for k, h in enumerate(hs)) and len(me["vertices"]) == 8 and len(me["triangles"]) == 12)
    except Exception as ex:
        import traceback
        notes.append(traceback.format_exc().strip().splitlines()[-1]); checks.append(("exception", False))
    # the cooker against the game: every manifold of the sampled shapes cooked from its own description, compared with the stored cooks
    k = physics.check_cook(native, per_package=per_package); hk, mk = k["hulls_identical"], k["manifolds"] - k["meshes"].get("none", 0)
    notes += ["cook: mesh classes %s, failures %s" % (k["meshes"], k["failures"])] * bool(k["failures"])
    good = sum(1 for _, x in checks if x); ok = ok and hk == k["hulls"] and mk == k["manifolds"] and k["manifolds"] > 0
    report("physics", ok and good == len(checks) and checks != [], "%d Native physics shapes in %d packages (up to %d per package): round trip byte-identical %d, rebuilt from their description %d; "
           "cooked with the game's PhysX from their description: hulls %d of %d identical (8 copies each), meshes %d of %d (%s); %d of %d made-up checks (%s) (%.0f s)"
           % (r["shapes"], r["with_shapes"], per_package, r["identical"], b["identical"], hk, k["hulls"], mk, k["manifolds"], ", ".join("%s %d" % x for x in sorted(k["meshes"].items())),
              good, len(checks), ", ".join(n for n, x in checks if not x) or "all passed", time.time() - t0), notes)


def _raises(fn, *a):
    try: fn(*a)
    except ValueError: return True
    return False


# --------------------------------------------------------------------------------------------------------------- synthetic
def _quat(axis, ang):
    a = np.asarray(axis, float); a /= np.linalg.norm(a)
    return np.r_[math.cos(ang / 2), math.sin(ang / 2) * a]   # w, x, y, z


def section_synthetic():
    import validate, templates
    t0 = time.time(); root = os.path.join(config.SDK_STAGE_DIR, "_selftest")
    if os.path.isdir(root): shutil.rmtree(root)
    A, R = os.path.join(root, "Assets"), os.path.join(root, "RuntimeDataCache"); os.makedirs(A); os.makedirs(R)
    os.environ["TPAC_STAGE"] = A
    checks = []; notes = []
    def check(name, ok, why=""):
        checks.append((name, bool(ok)))
        if not ok: notes.append("%s %s" % (name, why))
    try:
        # skeleton: a four-bone chain, Z up, with the SDK's minimal ragdoll
        bones = [dict(name="root", parent=-1, R=np.eye(3), o=np.zeros(3))] + \
                [dict(name="b%d" % i, parent=i - 1, R=np.eye(3), o=np.array([0.0, 0.0, 0.3])) for i in range(1, 4)]
        U = sdk_skeleton.auto_userdata(bones)
        sp, sguid = sdk_skeleton.write_skeleton("selftest_skeleton", bones, U, A)
        name, guid, rb, rU = sdk_skeleton.read_skeleton(sp)
        check("skeleton round trip", name == "selftest_skeleton" and guid == sguid and len(rb) == 4 and len(rU["joints"]) == len(U["joints"]))
        check("skeleton passes", not validate.validate_skeleton(name, rb, rU), validate.validate_skeleton(name, rb, rU))
        # animation: 20 frames of a swinging chain, owner = the synthetic skeleton
        frames = 20; t = np.arange(frames, dtype=float)
        rot = []
        for b in range(4):
            q = np.array([_quat([0, 1, 0], 0.4 * math.sin(2 * math.pi * f / frames + b)) for f in range(frames)])
            rot.append((t, q))
        root_ch = (t, np.c_[np.zeros(frames), np.zeros(frames), 0.01 * np.sin(t), np.zeros(frames)])
        ap = tpac.write_anim_takes("selftest_anims", [("selftest_swing", rot, root_ch)], sguid, A)
        a = tpac.anims(ap)
        check("animation written", "selftest_swing" in a and a["selftest_swing"][2] == sguid)
        # clip + cache
        cp, dur, ek = tpac.write_clip("selftest_clip", "selftest_swing", frames, out_dir=A)
        rp, ro, n = tpac.write_clip_cache(cp, R)
        ent = tpac.read_rdc(rp)[0]; blob = ent["blob"] if ent["stored"] >= ent["raw"] else tpac.lz4_decompress(ent["blob"], ent["raw"])
        check("clip cache re-packs", tpac.pack_optanim(tpac.parse_optanim(blob)) == blob)
        q, rt = tpac.optanim_values(tpac.parse_optanim(blob))
        err = max(min(np.abs(v - rot[b][1][int(k)]).max(), np.abs(v + rot[b][1][int(k)]).max()) for (b, k), v in q.items() if int(k) < frames)
        check("clip cache values", err < 0.01, "max error %.4f" % err)
        # texture + material
        img = np.zeros((64, 48, 4), np.uint8); img[..., 0] = np.arange(48)[None, :] * 5; img[..., 1] = np.arange(64)[:, None] * 3; img[..., 3] = 255
        png = os.path.join(root, "selftest_d.png"); pngio.write_png(png, img)
        check("png round trip", (pngio.read_png(png)["rgba"] == img).all())
        tp, trp, tr = tpac.write_texture(png, out_dir=A, rdc_dir=R)
        r2, pix = tpac.tex_pixels(tp, R)
        check("texture hashes", r2["hash_ok"] and all(e["hash_ok"] for e in r2["entries"]) and r2.get("rdc_h1_ok", True) and r2.get("rdc_h2_ok", True)
              and tw_formats.texture_pixel_hash(pix) == r2["unk8"])
        mp, mr, mt = tpac.write_material("selftest_mtl", "pbr_shading", {"diffuse": "selftest_d"}, skinning=True, out_dir=A)
        m2 = tpac.read_mtl_file(mp)
        check("material", m2["vlayout"] == ["skinning"] and m2["tex"] == [(0, tr["resource"])] and m2["shader"] == tpac.find_shader("pbr_shading"))
        # mesh: one quad skinned to the four bones
        P = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], float); F = np.array([[0, 1, 2], [0, 2, 3]], np.uint32)
        N = np.tile([0.0, -1.0, 0.0], (4, 1)); UV = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float); pi = np.arange(4, dtype=np.uint32)
        T, B = sdk_mesh.tangents(P, pi, N, UV, F)
        Wt = np.zeros(4, sdk_mesh.WEIGHT); Wt["w"][:, 0] = 1.0; Wt["b"][:, 0] = [0, 1, 2, 3]
        sub = dict(name="selftest_quad", material="selftest_mtl", P=P, pi=pi, N=N, nbones=4, T=T, B=B, uv0=UV, uv1=np.zeros((4, 2)),
                   c0=np.full(4, 0xffffffff, np.uint32), F=F, W=Wt)
        mo, mrp, info = sdk_mesh.write_metamesh("selftest_quad", "selftest_quad", [sub], b"synthetic", A, R, None, False, None, A)
        subs = sdk_mesh.read_mesh(mo, R)
        check("mesh round trip", len(subs) == 1 and len(subs[0][1]["P"]) == 4 and len(subs[0][1]["F"]) == 2 and subs[0][3] is not None)
        # the validators on the whole stage: no hard problems
        errs = [e for e in validate.validate(root) if not e.startswith("WARN")]
        check("stage passes the validators", not errs, errs[:3])
        # planted faults the validators must catch
        many = [dict(name="n%d" % i, parent=i - 1, R=np.eye(3), o=np.array([0.0, 0.0, 0.1])) for i in range(65)]
        check("catches 65 bones", any("> 64" in e for e in validate.validate_skeleton("big", many, sdk_skeleton.auto_userdata(many))))
        bad = sdk_skeleton.auto_userdata(bones)
        for bd in bad["bodies"]: bd["cap2"] = ((0.0, 0, 0, 1), (0.0, 0, 0.2, 1), 0.05)   # hit capsules on every bone ...
        bad["joints"].append(dict(flag=0, type="ik", name="ik_b1", child="b1", parent="root", frame=(1.0, 0, 0, 0, 0, 0, 0, 1),
                                  motions=[], limits=(0.0,) * 5))                     # ... and an ik joint on b1 only: b2 hangs the solver
        check("catches the ik rule", any("ik rule" in e for e in validate.validate_skeleton("ik", bones, bad)))
        long_name = "x" * 64
        lp, _, _ = tpac.write_clip(long_name, "selftest_swing", frames, out_dir=A)
        check("catches a 64 character clip name", any("63 max" in e for e in validate.validate_clip_file(lp, None)))
        os.remove(lp)
        mb = os.path.join(root, "project.mbproj")
        open(mb, "w").write('<project><file id="my_made_up_id" name="x.xml" type="Test"/></project>')
        check("catches a made-up project.mbproj id", any("not an id Native registers" in e for e in validate.validate_registration(mb)))
    except Exception as ex:
        import traceback
        notes.append(traceback.format_exc().strip().splitlines()[-1]); checks.append(("exception", False))
    good = sum(1 for _, ok in checks if ok)
    report("synthetic", good == len(checks) and checks != [], "%d of %d checks (%s) (%.0f s)" % (
        good, len(checks), ", ".join(n for n, ok in checks if not ok) or "all passed", time.time() - t0), notes)


# ------------------------------------------------------------------------------------------------------------------------ main
def main(argv):
    opt = lambda k, d: argv[argv.index(k) + 1] if k in argv else d
    if "-h" in argv or "--help" in argv:
        print(open(__file__, encoding="utf-8").read().split("\nimport collections")[0]); return 0
    config.require_game()
    skip = set(opt("--skip", "").split(","))
    limit, max_mb, full = int(opt("--limit", 200)), int(opt("--max-package-mb", 300)), "--full" in argv
    print("Bannerlord:", config.BANNERLORD_DIR)
    print("clip template:     ", templates_source("clip"))
    print("material template: ", templates_source("material"))
    if "packages" not in skip: section_packages(limit, max_mb, full)
    if "textures" not in skip: section_textures(int(opt("--textures", 4)))
    if "codecs" not in skip: section_codecs()
    if "caches" not in skip: section_caches(int(opt("--caches", 300)))
    if "skeletons" not in skip: section_skeletons()
    if "physics" not in skip: section_physics(int(opt("--physics", 8)))
    if "synthetic" not in skip: section_synthetic()
    bad = [n for n, ok in RESULTS if not ok]
    print("\nSUMMARY: %d PASS, %d FAIL%s" % (len(RESULTS) - len(bad), len(bad), (" (" + ", ".join(bad) + ")") if bad else ""))
    return 1 if bad else 0


def templates_source(kind):
    import templates
    return templates.clip_template()["source"] if kind == "clip" else templates.material_template(False)[1]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
