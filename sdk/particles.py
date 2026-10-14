"""Particle systems of Bannerlord's asset packages: read, write, round trip.

    python sdk/particles.py info <package> [effect ...]      the emitters of a package's particle effects (or one effect found by name)
    python sdk/particles.py check [--limit N] [--module M] [package]
                                                             decode and re-encode every shipped particle effect, GPU particle record and
                                                             vector field, compare byte for byte (N of M)
    python sdk/particles.py copy <effect> <new name> [--out DIR] [--rate X] [--size X] [--life X] [--material NAME] [--color R,G,B]
                                                             write a one-resource package <new name>_psys.tpac: a shipped effect copied under a new
                                                             name with the given emitter parameters changed (default DIR <SDK_STAGE_DIR>/particles)

Where particle systems live (game v1.4.8, every package of every official module, read only):
  - Resource type 6de14d67 "Particle effect": 676 resources (338 distinct guids; the AssetPackages copy and the EmAssetPackages copy of the same
    package). The record is empty (u64 0, no dependencies). Data entry 326587ce "Particle effect data" holds the emitters (layout below). Data entry
    4c8cf3e5 holds a baked table (154 effects; a B8G8R8A8_UNORM 512 x 512 RGBA image after its header: a lookup the curves are baked into),
    which this module keeps as bytes and does not regenerate.
  - ModuleData/particle_systems*.xml are the editor's export of the same effects (guid="{...}" equals the resource guid for 207 of the 338; names
    differ for some). The XML names every parameter, and was used as the key here: 93 percent of its numbers occur in the binary data. The
    game's own XML loading was not looked at; the packages are what this module reads and writes.
  - Resource type b938ca7b "GPU particle system" (36 resources): record only (about 700 bytes: a flag list, strings such as "sphere", "3d", "normal" and a run
    of numbers for one emitter), no data entries. Kept as bytes; strings_of() lists the names in it.
  - Resource type cc373411 "Procedural vector field" (4 resources, 2 distinct): a small record and a data entry 11d9c4bf of 16 x 16 x 16 vec3, kept as bytes.

Particle effect data, layout (little endian): string sound code, u32 n and n f32 (effect floats), u32 emitter count, then per emitter:
  u32 version (0 .. 9), 4 guids (the second and third zero in the shipped ones; the fourth is the emitter's MATERIAL resource guid, the first the
  emitter's own id), name, u32 0, list of the flags that are on (strings, e.g. "local_emit_dir", "select_random_sprite", "order_by_distance"),
  particle size curve operation ("multiply" / "add"), spawn mode ("particle_spawn"),
  27 words of numbers (emitter life and the multipliers, see NUMBERS), 2 vec4, a block of words (the first emitter parameter and a guid; 12 words
  up to version 2, 14 at 3 and 4, 15 at 5, 20 at 7, 21 at 8 and 9: kept as raw words), strings (collision behaviour, [version 9: one flag byte], emission
  velocity model, [>= 2] colour mode, [>= 4] more: "legacy" ...), [>= 4] 4 words,
  5 parameters (turbulence strength, cone emit velocity, damping, angular damping, particle life), emission rate, emit velocity x, y, z, ...
  A parameter is u32, u32, f32 base, f32 bias and a curve (u32 version, f32 default, f32 multiplier, u32 n and n x 8 f32: n spans, each two
  keys of (time, value, tangent x, tangent y)). The list in PARAMS gives the verified names. Then 4 words (the particle size base and bias),
  the size curves (2 curves), more parameters (see PARAMS), 9 words (the last numbers: initial rotation, ...), billboard type, emit volume type (strings),
  the colour parameter (u32, u32 n and n x (time, r, g, b, w), u32 m and m x (time, alpha)), then a fixed run of vec4 and floats (gravity, fixed
  billboard direction, ...), texture sprite count (2 x i32), frame count, frame rate, the decal material guid list, decal and quad scale vectors, skinned decal
  indices, max alive particle count, emitter sound code and a string (empty), f32 1.0.
Every field this module does not name is kept as raw words or bytes at its place, so decode then encode is the identity.

Verified (run `check`; the shipped counts are in its output): the decode of every effect ends exactly at the end of the data and the re-encode is
byte-identical; for all effects the hashes and entry rows follow the SDK's rule. `copy` writes a new package whose effect decodes back to the
parameters asked for and re-encodes identically.

Not verified, not done:
  - Nothing here was tried in the game: a particle effect written by this module is unverified in game, and so are its materials.
  - The baked table (4c8cf3e5) is not derived from the curves. A copied effect carries the donor's table; changing a curve or a colour does not
    change the table, and what the engine does with a table that disagrees with the curves is unknown. The shipped effects without a table (522 of
    676 resources) show it is optional for them; effects without one are the safe donors.
  - Which of the numbers in a block that is kept raw means what is not known (the names in NUMBERS, PARAMS_A, PARAMS_B and the particle size are the ones the XML confirms).
  - blank_effect() makes a structurally valid effect from scratch (it decodes and re-encodes like a shipped one); its defaults are a guess modelled on
    shipped emitters, so copy a shipped effect for anything that must look right.
No game content is stored in the repository: the module reads the install at run time.
"""
import glob, mmap, os, re, struct, sys, time, uuid

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, tpac, tpac_pack
else:
    from . import config, tpac, tpac_pack

PARTICLE_TYPE = uuid.UUID("6de14d67-dd9a-45be-9463-0281c3d8dd51").bytes_le      # "Particle effect" resource
EFFECT_DATA = uuid.UUID("326587ce-bb0c-4c22-8782-97e20cf03c5e").bytes_le        # data: emitters
BAKED = uuid.UUID("4c8cf3e5-9358-42d2-8a66-9ed2b2fe5f35").bytes_le              # data: baked table
GPU_TYPE = uuid.UUID("b938ca7b-e08e-497c-b30c-07314931809a").bytes_le           # "GPU particle system" resource (record only)
FIELD_TYPE = uuid.UUID("cc373411-785f-4ef0-95a2-d5cf54b24536").bytes_le         # "Procedural vector field" resource
FIELD_DATA = uuid.UUID("11d9c4bf-a5b9-42be-b6c1-0d02c03fa81a").bytes_le         # data: samples
tpac.DATA_KIND.update({EFFECT_DATA: 1, BAKED: 0, FIELD_DATA: 0})                # the u32 after each entry in the table

U32, I32, F32 = struct.Struct("<I"), struct.Struct("<i"), struct.Struct("<f")

# The 27 words after the spawn mode, by index. Confirmed with the XML (the values of shipped effects equal the XML's): the others are w<N>.
NUMBERS = {0: "emitter_life", 11: "scale_with_emitter_velocity_coef", 12: "inherit_emitter_velocity", 14: "fadeout_distance", 15: "fadeout_coef",
           16: "camera_fadeout_coef", 17: "backlight_multiplier", 18: "diffuse_multiplier", 19: "emissive_multiplier"}
# The parameters (u32, u32, base, bias, curve) in file order, as parameter number -> name. Confirmed with the XML on 297 emitters of versions 0 to 3
# (the name with the most matching values): 'a' is the parameter in the block of raw words (not named), then
PARAMS_A = ("turbulence_strength", "cone_emit_velocity", "damping", "angular_damping", "particle_life")       # the five after the strings
PARAMS_B = ("emission_rate", "emit_velocity_x", "emit_velocity_y", "emit_velocity_z", "emit_rotation_speed", "wind_effect", "extra")   # after the size curves


# ---------------------------------------------------------------------------------------------------------- byte helpers
class Reader:
    def __init__(s, b, o=0): s.b = b; s.o = o
    def need(s, n):
        if n < 0 or s.o + n > len(s.b): raise ValueError("truncated at %d (+%d of %d)" % (s.o, n, len(s.b)))
    def u32(s): s.need(4); v, = U32.unpack_from(s.b, s.o); s.o += 4; return v
    def f32(s): s.need(4); v, = F32.unpack_from(s.b, s.o); s.o += 4; return v
    def raw(s, n): s.need(n); v = bytes(s.b[s.o:s.o + n]); s.o += n; return v
    def str(s):
        n = s.u32()
        if n > 4096: raise ValueError("string length %d at %d" % (n, s.o))
        return s.raw(n).decode("utf-8", "surrogateescape")
    def strs(s): return [s.str() for _ in range(s.u32())]
    def done(s): return s.o == len(s.b)

def w_str(x): x = x.encode("utf-8", "surrogateescape"); return U32.pack(len(x)) + x
def w_strs(l): return U32.pack(len(l)) + b"".join(w_str(x) for x in l)
def _ident(b, q):
    """Length of the plausible name string (printable, 2 to 40 characters) that starts at q, else 0."""
    if q + 4 > len(b): return 0
    n, = U32.unpack_from(b, q)
    return n if 2 <= n <= 40 and q + 4 + n <= len(b) and re.fullmatch(rb"[A-Za-z0-9_ .&-]+", bytes(b[q + 4:q + 4 + n])) else 0


# ---------------------------------------------------------------------------------------------------------- curves and parameters
def read_curve(r):
    c = dict(version=r.u32(), default=r.f32(), multiplier=r.f32()); n = r.u32()
    if n > 4096: raise ValueError("curve with %d spans" % n)
    c["keys"] = np.frombuffer(r.raw(32 * n), "<f4").reshape(n, 8).copy(); return c

def write_curve(c):
    k = np.asarray(c["keys"], "<f4").reshape(-1, 8)
    return U32.pack(c["version"]) + F32.pack(c["default"]) + F32.pack(c["multiplier"]) + U32.pack(len(k)) + k.tobytes()

def read_param(r): return dict(u0=r.u32(), u1=r.u32(), base=r.f32(), bias=r.f32(), curve=read_curve(r))
def write_param(p): return U32.pack(p["u0"]) + U32.pack(p["u1"]) + F32.pack(p["base"]) + F32.pack(p["bias"]) + write_curve(p["curve"])

def curve(keys=(), default=1.0, multiplier=1.0, version=0):
    """A curve from spans: each a pair of keys ((time, value, tangent x, tangent y), (time, value, tangent x, tangent y))."""
    return dict(version=version, default=default, multiplier=multiplier, keys=np.array(keys, "<f4").reshape(-1, 8))

def param(base, bias=0.0, curve_=None):
    return dict(u0=0, u1=0, base=base, bias=bias, curve=curve_ or curve())


# ---------------------------------------------------------------------------------------------------------- emitter
def pre_size(version):
    """Words in the block between the two vec4 and the first string, by emitter version (measured; see parse_emitter, which also checks it)."""
    return {0: 12, 1: 12, 2: 12, 3: 14, 4: 14, 5: 15, 7: 20, 8: 21, 9: 21}.get(version)

def string_count(version): return 2 if version < 2 else 3 if version < 4 else 4

def parse_emitter(r):
    e = dict(version=r.u32()); v = e["version"]
    e["guids"] = [r.raw(16) for _ in range(4)]; e["name"] = r.str(); e["u0"] = r.u32(); e["flags"] = r.strs()
    e["size_op"] = r.str(); e["spawn"] = r.str(); e["numbers"] = np.frombuffer(r.raw(4 * 27), "<u4").copy(); e["vec4"] = r.raw(32)
    a = r.o; q = a                                                  # the block of words: its size is where the first string starts
    while q < a + 4 * 40 and not _ident(r.b, q): q += 4
    if q >= a + 4 * 40 or (q - a) % 4: raise ValueError("no strings after the numbers at %d" % a)
    e["pre"] = r.raw(q - a)
    e["collision"] = r.str(); e["flag_byte"] = r.raw(1) if v >= 9 else None
    e["velocity_model"] = r.str(); e["mode"] = r.str() if v >= 2 else None; e["mode2"] = r.str() if v >= 4 else None
    e["words4"] = r.raw(16) if v >= 4 else None
    e["params_a"] = [read_param(r) for _ in range(5)]; e["size_words"] = r.raw(16); e["curve7"] = read_curve(r); e["size_curve"] = read_curve(r)
    e["params_b"] = [read_param(r) for _ in range(6 + (v >= 1))]; e["scalars"] = r.raw(36)
    e["billboard"] = r.str(); e["volume"] = r.str()
    e["color_u"] = r.u32(); e["colors"] = np.frombuffer(r.raw(20 * r.u32()), "<f4").reshape(-1, 5).copy(); e["alphas"] = np.frombuffer(r.raw(8 * r.u32()), "<f4").reshape(-1, 2).copy()
    e["vectors"] = r.raw(16 + 16 + 80 + 4); e["sprites"] = (struct.unpack("<ii", r.raw(8))); e["frame_count"] = r.u32(); e["frame_rate"] = r.f32()
    e["decals"] = [r.raw(16) for _ in range(r.u32())]; e["decal_quad"] = r.raw(32); e["skinned"] = struct.unpack("<ii", r.raw(8)); e["max_alive"] = r.u32()
    e["sound"] = r.str(); e["s11"] = r.str(); e["f34"] = r.f32()
    return e

def write_emitter(e):
    v = e["version"]; out = [U32.pack(v), b"".join(e["guids"]), w_str(e["name"]), U32.pack(e["u0"]), w_strs(e["flags"]), w_str(e["size_op"]), w_str(e["spawn"]),
                              np.asarray(e["numbers"], "<u4").tobytes(), e["vec4"], e["pre"], w_str(e["collision"])]
    if v >= 9: out.append(e["flag_byte"])
    out.append(w_str(e["velocity_model"]))
    if v >= 2: out.append(w_str(e["mode"]))
    if v >= 4: out += [w_str(e["mode2"]), e["words4"]]
    out += [b"".join(write_param(p) for p in e["params_a"]), e["size_words"], write_curve(e["curve7"]), write_curve(e["size_curve"]), b"".join(write_param(p) for p in e["params_b"]), e["scalars"],
            w_str(e["billboard"]), w_str(e["volume"]), U32.pack(e["color_u"]), U32.pack(len(e["colors"])), np.asarray(e["colors"], "<f4").tobytes(),
            U32.pack(len(e["alphas"])), np.asarray(e["alphas"], "<f4").tobytes(), e["vectors"], struct.pack("<ii", *e["sprites"]), U32.pack(e["frame_count"]), F32.pack(e["frame_rate"]),
            U32.pack(len(e["decals"])), b"".join(e["decals"]), e["decal_quad"], struct.pack("<ii", *e["skinned"]), U32.pack(e["max_alive"]), w_str(e["sound"]), w_str(e["s11"]), F32.pack(e["f34"])]
    return b"".join(out)

def parse_effect(b):
    """dict(sound, floats, emitters) of a "Particle effect data" blob."""
    r = Reader(b); d = dict(sound=r.str()); d["floats"] = np.frombuffer(r.raw(4 * r.u32()), "<f4").copy(); d["emitters"] = [parse_emitter(r) for _ in range(r.u32())]
    if not r.done(): raise ValueError("%d bytes left after the last emitter" % (len(b) - r.o))
    return d

def write_effect(d):
    return w_str(d["sound"]) + U32.pack(len(d["floats"])) + np.asarray(d["floats"], "<f4").tobytes() + U32.pack(len(d["emitters"])) + b"".join(write_emitter(e) for e in d["emitters"])


# ---------------------------------------------------------------------------------------------------------- reading emitters by name
def number(e, name):
    """A named number of the emitter's 27 words (see NUMBERS)."""
    i = {v: k for k, v in NUMBERS.items()}[name]; return float(np.asarray(e["numbers"], "<u4")[i:i + 1].view("<f4")[0])

def set_number(e, name, value):
    i = {v: k for k, v in NUMBERS.items()}[name]; a = np.array(e["numbers"], "<u4"); a[i:i + 1] = np.array([value], "<f4").view("<u4"); e["numbers"] = a

def named_params(e):
    """{name: parameter dict} of the emitter's parameters that have a verified name."""
    out = dict(zip(PARAMS_A, e["params_a"])); out.update(zip(PARAMS_B, e["params_b"])); return out

def material_guid(e): return e["guids"][3]

def particle_size(e):
    """(base, bias, curve) of the emitter's particle size: base and bias are the last two floats of the 4 words before the size curves."""
    b, bi = struct.unpack_from("<ff", e["size_words"], 8); return b, bi, e["size_curve"]

def strings_of(record):
    """The printable names in a byte string (a GPU particle record): every length-prefixed string of 2 to 40 characters, in order."""
    out = []; q = 0
    while q + 4 <= len(record):
        n = _ident(record, q)
        if n: out.append(bytes(record[q + 4:q + 4 + n]).decode()); q += 4 + n
        else: q += 1
    return out


# ---------------------------------------------------------------------------------------------------------- from scratch
def blank_emitter(version=2, material=None, name="Emitter"):
    """A structurally valid emitter of a shipped version (0 to 9 except 6) with neutral numbers: a box volume, one white colour key, a fade-in and fade-out alpha,
    10 particles a second living 1 second. Defaults are modelled on the shipped emitters, not read from the game."""
    if pre_size(version) is None: raise ValueError("no emitter layout known for version %s" % version)
    numbers = np.zeros(27, "<u4"); numbers[5] = 1; numbers[6] = numbers[7] = 0xffffffff
    one = np.array([1.0], "<f4").view("<u4")[0]
    for i in (18, 19, 20): numbers[i] = one                      # diffuse, emissive, heatmap multipliers
    pre = write_param(param(0.0)) + bytes(16)                      # the first parameter and a guid
    pre += bytes(4 * pre_size(version) - len(pre))
    flat = np.array([0, 0, 0, 1], "<f4").tobytes()
    vectors = flat + np.array([1, 0, 1, 1], "<f4").tobytes() + b"".join(flat for _ in range(5)) + bytes(4)
    return dict(version=version, guids=[uuid.uuid4().bytes_le, bytes(16), bytes(16), bytes(material) if material else bytes(16)], name=name, u0=0, flags=[], size_op="multiply",
                spawn="particle_spawn", numbers=numbers, vec4=bytes(32), pre=pre, collision="normal", flag_byte=bytes(1) if version >= 9 else None, velocity_model="random_velocity_components",
                mode="color_over_time" if version >= 2 else None, mode2="legacy" if version >= 4 else None, words4=bytes(16) if version >= 4 else None,
                params_a=[param(0.0), param(1.0), param(0.0), param(0.0), param(1.0)], size_words=struct.pack("<4f", 0, 0, 1.0, 0), curve7=curve(), size_curve=curve(),
                params_b=[param(10.0)] + [param(0.0) for _ in range(5 + (version >= 1))], scalars=bytes(36), billboard="3d", volume="box", color_u=0,
                colors=np.array([[0, 1, 1, 1, 1]], "<f4"), alphas=np.array([[0, 0], [0.1, 1], [1, 0]], "<f4"), vectors=vectors, sprites=(1, 1), frame_count=1, frame_rate=1.0,
                decals=[], decal_quad=np.array([1, 1, 1, 1, 1, 1, 0, 0], "<f4").tobytes(), skinned=(-1, -1), max_alive=0, sound="", s11="", f34=1.0)

def blank_effect(emitters=1, version=2, material=None):
    """An effect with `emitters` blank emitters (see blank_emitter)."""
    return dict(sound="", floats=np.zeros(1, "<f4"), emitters=[blank_emitter(version, material, "Emitter" if i == 0 else "Emitter %d" % i) for i in range(emitters)])


# ---------------------------------------------------------------------------------------------------------- changing a copy
def modify(effect, emission_rate=None, size=None, life=None, material=None, color=None, alpha_scale=None, emitter=None):
    """A copy of a parsed effect with parameters changed on every emitter (or only emitter number `emitter`):
    emission_rate: particles per second (the emission_rate parameter's base); size: the particle size base (the size_words, f32 at +8) multiplied
    by `size`; life: particle_life base set to `life`; material: a 16-byte material resource guid for the emitter; color: (r, g, b) set on every colour key;
    alpha_scale: multiplies every alpha key."""
    out = dict(effect); out["emitters"] = []
    for i, e in enumerate(effect["emitters"]):
        e = dict(e); e["params_a"] = [dict(p) for p in e["params_a"]]; e["params_b"] = [dict(p) for p in e["params_b"]]
        if emitter is None or emitter == i:
            if emission_rate is not None: e["params_b"][0]["base"] = float(emission_rate)
            if life is not None: e["params_a"][4]["base"] = float(life)
            if size is not None:
                sw = bytearray(e["size_words"]); b0, b1 = struct.unpack_from("<ff", sw, 8); struct.pack_into("<ff", sw, 8, b0 * size, b1 * size); e["size_words"] = bytes(sw)
            if material is not None: g = list(e["guids"]); g[3] = bytes(material); e["guids"] = g
            if color is not None and len(e["colors"]): c = np.array(e["colors"], "<f4"); c[:, 1:4] = np.asarray(color, "<f4"); e["colors"] = c
            if alpha_scale is not None and len(e["alphas"]): a = np.array(e["alphas"], "<f4"); a[:, 1] = np.clip(a[:, 1] * alpha_scale, 0, 1); e["alphas"] = a
        out["emitters"].append(e)
    return out


# ---------------------------------------------------------------------------------------------------------- packages
def packages(module=None):
    pats = [os.path.join(config.MODULES, module or "*", d, "**", "*.tpac") for d in ("AssetPackages", "EmAssetPackages")]
    return sorted(set(p for pat in pats for p in glob.glob(pat, recursive=True)))

def _read(m, e):
    st = bytes(m[e["off"]:e["off"] + e["stored"]])
    return tpac.lz4_decompress(st, e["raw"]) if e["stored"] < e["raw"] else st

def resources(path, types=(PARTICLE_TYPE, GPU_TYPE, FIELD_TYPE)):
    """Yield dict(type, name, guid, flag, record, hash, entries=[dict(type, owner, raw, stored, rest)]) for the particle resources of a package."""
    with open(path, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if m[:4] != b"TPAC": return
            meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
            for it in pk["items"]:
                if it["type"] not in types: continue
                ents = []
                for e in it["entries"]:
                    st = bytes(m[e["off"]:e["off"] + e["stored"]]); raw = tpac.lz4_decompress(st, e["raw"]) if e["stored"] < e["raw"] else st
                    ents.append(dict(type=bytes(e["rest"][16:32]), owner=bytes(e["rest"][0:16]), raw=raw, stored=st, rest=e["rest"], size=e["raw"]))
                yield dict(type=it["type"], name=it["name"].decode("utf-8", "replace"), guid=it["guid"], flag=it["flag"], record=it["record"], hash=it["hash"], entries=ents, deps=it["deps"])
        finally:
            m.close()

def pack_one(package, guid, name, entries, record=None, rtype=PARTICLE_TYPE):
    """A one-resource package. entries = [(data type guid, raw bytes)]. The record is the empty one (u64 0) unless given. Like tpac.pack_resources,
    except that the u8 after each entry's type value is 1 only when the stored copy is LZ4 (as in the shipped particle packages)."""
    nm = name.encode("utf-8", "surrogateescape"); record = record if record is not None else bytes(8)
    head = b"TPAC" + struct.pack("<I", 2) + package + struct.pack("<II", 1, 0) + bytes(4) + rtype + guid + bytes(4) + U32.pack(len(nm)) + nm + record         + struct.pack("<Q", tpac.xxh64(record)) + U32.pack(len(entries))
    meta = len(head) + 69 * len(entries) + 4; table = b""; data = b""
    for ty, raw in entries:
        c = tpac.lz4_compress(raw); c = c if len(c) < len(raw) else raw
        table += struct.pack("<QQQ", meta + len(data), len(raw), len(c)) + guid + ty + struct.pack("<Q", tpac.xxh64(raw)) + struct.pack("<IB", tpac.DATA_KIND.get(ty, 0), len(c) < len(raw)); data += c
    out = bytearray(head + table + bytes(4) + data); struct.pack_into("<I", out, 0x1c, meta - 0x24)
    return bytes(out)

def package_bytes(effect, baked=None, name="effect", package=None, guid=None):
    """The bytes of a new package holding the effect (a dict from parse_effect / modify) and optionally the baked table (bytes)."""
    ents = [(EFFECT_DATA, write_effect(effect))] + ([(BAKED, baked)] if baked is not None else [])
    g = guid or uuid.uuid4().bytes_le
    return pack_one(package or uuid.uuid4().bytes_le, g, name, ents)

def write_package(effect, name, baked=None, out_dir=None, force=False, package=None, guid=None):
    out = os.path.join(out_dir or os.path.join(config.SDK_STAGE_DIR, "particles"), name + "_psys.tpac")
    if os.path.exists(out) and not force: raise FileExistsError(out + " exists (use force)")
    data = package_bytes(effect, baked, name, package, guid); os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f: f.write(data)
    return out

def find_material(name, module="Native"):
    """The guid (16 bytes) of a shipped material resource by name."""
    mt = bytes.fromhex("9313b01d0269194f83bab37a39830717")
    for p in packages(module):
        with open(p, "rb") as f:
            h = f.read(0x24)
            if h[:4] != b"TPAC": continue
            meta, = struct.unpack_from("<Q", h, 0x1c); pk = tpac_pack.parse_package(h + f.read(meta))
        for it in pk["items"]:
            if it["type"] == mt and it["name"].decode("utf-8", "replace") == name: return it["guid"]
    raise KeyError("no material named %s" % name)

def find_effect(name, module="Native"):
    """(effect dict, baked table bytes or None, package path) of a shipped effect by name."""
    for p in packages(module):
        for res in resources(p, (PARTICLE_TYPE,)):
            if res["name"] == name:
                eff = parse_effect(next(e["raw"] for e in res["entries"] if e["type"] == EFFECT_DATA))
                return eff, next((e["raw"] for e in res["entries"] if e["type"] == BAKED), None), p
    raise KeyError("no particle effect named %s" % name)


# ---------------------------------------------------------------------------------------------------------- measurement
def check_resource(res):
    """Failure classes (empty when byte-identical) of one resources() item: data re-encodes to the same bytes, record is the shipped empty one (or kept),
    hashes and rows follow the SDK's rule."""
    bad = []
    try:
        for e in res["entries"]:
            k = {EFFECT_DATA: "effect", BAKED: "baked", FIELD_DATA: "field"}.get(e["type"], "other")
            if e["type"] == EFFECT_DATA:
                if write_effect(parse_effect(e["raw"])) != e["raw"]: bad.append("effect")
            if struct.pack("<Q", tpac.xxh64(e["raw"])) != e["rest"][32:40]: bad.append(k + " hash")
            if e["rest"][40:] != struct.pack("<IB", tpac.DATA_KIND.get(e["type"], 0), len(e["stored"]) < len(e["raw"])): bad.append(k + " row")
        if res["type"] == PARTICLE_TYPE and res["record"] != bytes(8): bad.append("record")
        if struct.pack("<Q", tpac.xxh64(res["record"])) != res["hash"]: bad.append("record hash")
    except Exception as ex:
        bad.append("decode: %s" % str(ex)[:60])
    return bad

def check(paths=None, limit=None, log=None):
    """Round-trip every particle resource of the given packages. Returns dict(packages, resources, by_type {type: [n, identical]}, emitters,
    versions {v: emitters}, with_baked, identical, failures {class: (count, example)})."""
    files = list(paths) if paths else packages(); out = dict(packages=0, resources=0, identical=0, by_type={}, emitters=0, versions={}, with_baked=0, failures={}); t0 = time.time()
    names = {PARTICLE_TYPE: "particle effect", GPU_TYPE: "GPU particle system", FIELD_TYPE: "vector field"}
    for p in files:
        out["packages"] += 1
        try:
            for res in resources(p):
                if limit and out["resources"] >= limit: return out
                out["resources"] += 1; bad = check_resource(res); k = out["by_type"].setdefault(names[res["type"]], [0, 0]); k[0] += 1
                if res["type"] == PARTICLE_TYPE and not bad:
                    for e in res["entries"]:
                        if e["type"] == EFFECT_DATA:
                            for em in parse_effect(e["raw"])["emitters"]: out["emitters"] += 1; out["versions"][em["version"]] = out["versions"].get(em["version"], 0) + 1
                    out["with_baked"] += any(e["type"] == BAKED for e in res["entries"])
                if not bad: out["identical"] += 1; k[1] += 1
                for c in set(bad):
                    n, ex = out["failures"].get(c, (0, "%s:%s" % (os.path.basename(p), res["name"]))); out["failures"][c] = (n + 1, ex)
        except Exception as ex:
            n, e0 = out["failures"].get("package: %s" % str(ex)[:50], (0, os.path.basename(p))); out["failures"]["package: %s" % str(ex)[:50]] = (n + 1, e0)
        if log: log("%-48s (%.0f s)" % (os.path.basename(p), time.time() - t0))
    return out


# ---------------------------------------------------------------------------------------------------------- command line
def describe(eff, name=""):
    lines = ["%s: %d emitters, sound %r" % (name, len(eff["emitters"]), eff["sound"])]
    for i, e in enumerate(eff["emitters"]):
        P = named_params(e)
        lines.append("  emitter %d %r (version %d): material %s, %d flags (%s), life %.3g rate %.3g size x %.3g/%.3g, %d colour keys, %d alpha keys, sprites %dx%d"
                     % (i, e["name"], e["version"], uuid.UUID(bytes_le=material_guid(e)), len(e["flags"]), ", ".join(e["flags"][:3]), P["particle_life"]["base"], P["emission_rate"]["base"],
                        *struct.unpack_from("<ff", e["size_words"], 8), len(e["colors"]), len(e["alphas"]), *e["sprites"]))
    return "\n".join(lines)

def main(argv):
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in ("--limit", "--module", "--out", "--rate", "--size", "--life", "--material", "--color"))]
    opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["info"] and len(a) >= 2:
        config.require_game()
        if os.path.isfile(a[1]):
            for res in resources(a[1], (PARTICLE_TYPE,)):
                if len(a) > 2 and res["name"] not in a[2:]: continue
                print(describe(parse_effect(next(e["raw"] for e in res["entries"] if e["type"] == EFFECT_DATA)), res["name"]))
        else:
            eff, baked, p = find_effect(a[1]); print(describe(eff, a[1]), "\n  in", os.path.basename(p), "baked table" if baked else "no baked table")
        return 0
    if a[:1] == ["check"]:
        config.require_game(); r = check([a[1]] if len(a) > 1 else packages(opt("--module")), int(opt("--limit", 0)) or None, log=print if "--verbose" in argv else None)
        print("packages scanned %d, particle resources %d, byte-identical %d of %d" % (r["packages"], r["resources"], r["identical"], r["resources"]))
        for k, (n, i) in sorted(r["by_type"].items()): print("  %-22s %5d resources, %5d identical" % (k, n, i))
        print("  emitters %d by version %s; effects with a baked table %d" % (r["emitters"], ", ".join("%d: %d" % kv for kv in sorted(r["versions"].items())), r["with_baked"]))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  fails %-24s %6d  e.g. %s" % (k, c, ex))
        return 0 if r["identical"] == r["resources"] and r["resources"] else 1
    if a[:1] == ["copy"] and len(a) == 3:
        config.require_game(); eff, baked, p = find_effect(a[1])
        mat = find_material(opt("--material")) if opt("--material") else None
        col = tuple(float(x) for x in opt("--color").split(",")) if opt("--color") else None
        new = modify(eff, emission_rate=float(opt("--rate")) if opt("--rate") else None, size=float(opt("--size")) if opt("--size") else None,
                     life=float(opt("--life")) if opt("--life") else None, color=col, material=mat)
        out = write_package(new, a[2], baked, opt("--out"), force="--force" in argv)
        print(describe(new, a[2])); print("written", out); return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
