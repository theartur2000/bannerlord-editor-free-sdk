"""Physics shapes (collision bodies) of Bannerlord's asset packages: read, write, round trip, build, cook.

    python sdk/physics.py info <package> [shape ...]      the shapes of a package and their parts
    python sdk/physics.py check [--limit N] [--module M] [--stored] [package]
                                                          decode and re-encode every shape, compare byte for byte (N of M)
    python sdk/physics.py rebuild [--limit N] [--module M] [--cook] [package]
                                                          rebuild every shape with build() from its own description, compare;
                                                          --cook: with hulls and meshes cooked here instead of taken from the shape
    python sdk/physics.py cookcheck [--limit N] [--module M] [package]
                                                          cook every manifold with the game's PhysX library, compare with the stored cooks
    python sdk/physics.py cook hull|mesh <file.obj> [--name N] [--material M] [--midphase bvh33|bvh34] [--out DIR] [--force]
                                                          a one-resource package <name>_phys.tpac (default DIR <SDK_STAGE_DIR>/physics)
                                                          holding a hull body (the convex hull of the points) or a triangle-mesh body

A physics shape is one resource (type e8528e0e-64b6-4e61-bae0-7569c0452aea) with a record and three data entries, in this order:
the description (86a5d575: name, capsules, spheres, manifolds = triangle soups with vertices, material names, faces), the dynamic cook
(df27d12f: the primitives again and PhysX convex hulls, each manifold's hull 8 times mirrored) and the static cook (8283cbbb: the primitives
again and PhysX triangle meshes). The record repeats the primitives and carries a summary per manifold and the bounds. The layouts are in
the comments above each parse_/pack_ pair. Every field the module does not understand is kept as a value (the "unknown" u32 fields, the
16 source bytes) or as raw bytes (the cooked PhysX blobs), so nothing is lost. The hulls and meshes are cooked with the PhysX 4.1.2
cooking library the game ships (physx_cook.py loads it with ctypes; Windows, game installed).

Verified (game v1.4.8, every .tpac under Modules/*/AssetPackages and EmAssetPackages, read-only; run `check`, `rebuild`, `rebuild --cook`
and `cookcheck` to repeat):
  - decode then encode gives the same bytes for the record and all three data blobs, and the record hash and the three data hashes are
    xxh64 of the bytes, and the entry rows follow one rule (type value 0 / 3 / 3, last byte 1 only when the stored copy is LZ4):
    17451 of 17451 shapes in 518 of 1260 packages (the others hold no shapes).
  - build() rebuilds every one of those shapes byte-identical from its own description, record material, source bytes and flag lists,
    with the cooked PhysX blobs taken from the shape: 17451 of 17451. That proves the derivations: record primitives and manifold
    summaries, the dominant material index (the most used per-face material index, the lowest on a tie: 16701 of 16701 manifolds), bounds
    (float32, sign of zero included), the primitives of both cooks (a primitive without a material takes the record's), the dynamic cook's
    name per manifold, the static cook's per-mesh numbers, the offsets table.
  - The game's PhysX cooking library, called from here (cook_manifolds), reproduces the stored cooks from each manifold's own vertices
    and triangles: all 16655 hulls byte-identical (all 133240 copies: 8 mirrored cooks per manifold, MIRROR), and all 16701 meshes:
    16278 with the defaults (BVH33 "RTRE" tree, material index per face from the fourth face integer), 402 with the BVH34 "BV4 " tree,
    21 with all-zero material indices. With cooking done by build() itself (`rebuild --cook`, the stored mesh's mid-phase passed on)
    17433 of 17451 shapes are byte-identical; the 18 others hold those 21 zero-material meshes and differ in the static cook only.
    The cooking parameters are in physx_cook's docstring (4.1 defaults, eCOMPUTE_CONVEX, and a second quantized cook appended when a hull
    reaches 255 polygons, which 113 game hulls do).
  - mesh_cook() / hull_cook() read the front of the cooked blobs (vertices, triangles, material indices, remap; hull vertices): all 16701
    meshes and all 133240 hulls parse, and every mesh's remaining bytes start with its "RTRE" or "BV4 " tree. A mesh holds its manifold's
    vertices unchanged in 15534 of 16701 (the cooker alters the others; cooking them here alters them the same way).

Not verified, not done:
  - Nothing here was tried in the game. A shape written by write_package() is unverified in game, including the primitive-only ones and
    every hull or mesh body cooked here: the bytes follow the game's own cooks, but the engine loading them was not run.
  - The mid-phase of a new mesh is a choice (midphase=, default "bvh33", what 16299 of 16701 game meshes use); which one the engine picks
    for a shape is not known. Likewise nothing in a shape tells when its meshes were cooked with all-zero material indices (21 meshes).
  - Cooking needs the game's PhysX DLLs in the Python process (one PhysX foundation per process); without them build() leaves the cooks
    out and encode() refuses a shape with manifolds, as before. cook=False keeps them out on purpose.
  - The 16 "source" bytes of the record (the same on shapes of one family) are random in a new shape; what they refer to is unknown.
  - A primitive with flags (the game uses bf_ names) needs flag_lists=: the record and both cooks carry long lists for it that differ by
    file (24, 23 or 22 names) and cannot be derived from the description's flags.
  - The stored (LZ4) form: this SDK's compressor is greedy, the game's streams are smaller (this one's output is 18 percent larger on a sample of 66 blobs), so a package written
    here stores the same raw bytes in a different LZ4 stream (0 of 1500 sampled blobs identical; `check --stored`). Raw bytes, hashes
    and rows are the identical part.
  - 46 shapes carry a dynamic cook of one byte (no hulls); they are kept and rebuilt (build(dynamic_cook=False)), their meaning is unknown.
  - Material names are not validated against the game's physics_materials.xml.
No game content is stored in the repository: the module reads the install at run time.
"""
import glob, mmap, os, struct, sys, time, uuid

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, physx_cook, tpac, tpac_pack
else:
    from . import config, physx_cook, tpac, tpac_pack

SHAPE_TYPE = uuid.UUID("e8528e0e-64b6-4e61-bae0-7569c0452aea").bytes_le   # "Physics shape" resource
DESC = uuid.UUID("86a5d575-5dc1-4542-a52f-3865aa8fb4fa").bytes_le         # data: description (name, capsules, spheres, manifolds)
DYN = uuid.UUID("df27d12f-d074-45b8-a4ed-8068c0f4988d").bytes_le          # data: dynamic cook (convex hulls)
STAT = uuid.UUID("8283cbbb-3a79-40ea-aead-7f5a2ceb747f").bytes_le         # data: static cook (triangle meshes)
tpac.DATA_KIND.update({DESC: 0, DYN: 3, STAT: 3})                         # the u32 after each entry in the table: 0 / 3 / 3 on every game shape
DATA_ORDER = (DESC, DYN, STAT)
HULL_SLOTS = 8                                                            # every dynamic cook holds its hulls 8 times (see the dyn layout)

U32, I32, F32 = struct.Struct("<I"), struct.Struct("<i"), struct.Struct("<f")


# ---------------------------------------------------------------------------------------------------------- byte helpers
class Reader:
    def __init__(s, b, o=0): s.b = b; s.o = o
    def need(s, n):
        if n < 0 or s.o + n > len(s.b): raise ValueError("truncated at %d (+%d of %d)" % (s.o, n, len(s.b)))
    def u8(s): s.need(1); v = s.b[s.o]; s.o += 1; return v
    def u32(s): s.need(4); v, = U32.unpack_from(s.b, s.o); s.o += 4; return v
    def i32(s): s.need(4); v, = I32.unpack_from(s.b, s.o); s.o += 4; return v
    def raw(s, n): s.need(n); v = bytes(s.b[s.o:s.o + n]); s.o += n; return v
    def f32(s): return np.frombuffer(s.raw(4), "<f4")[0]
    def vec4(s): return np.frombuffer(s.raw(16), "<f4").copy()
    def str(s): n = s.u32(); return s.raw(n).decode("utf-8", "surrogateescape")
    def strs(s): return [s.str() for _ in range(s.u32())]
    def rest(s): return bytes(s.b[s.o:])
    def done(s): return s.o == len(s.b)


def w_str(x): x = x.encode("utf-8", "surrogateescape"); return U32.pack(len(x)) + x
def w_strs(l): return U32.pack(len(l)) + b"".join(w_str(x) for x in l)
def w_f32(x): return np.float32(x).tobytes()
def w_vec4(v): return np.asarray(v, "<f4").reshape(4).tobytes()
def vec4(x, y, z, w=1.0): return np.array([x, y, z, w], "<f4")


# ---------------------------------------------------------------------------------------------------------- primitives
# A capsule and a sphere are written the same way in the description, the record and both cooks (a capsule: u32 0, radius,
# two end points as vec4, u32 0, flag names, material name; a sphere: u32 0, radius, centre vec4, u32 0, flags, material).
# The two u32 are unknown to TpacTool (the open-source reader) and are 0 on every game shape that has them.
def read_capsule(r):
    return dict(u0=r.u32(), radius=r.f32(), p0=r.vec4(), p1=r.vec4(), u1=r.u32(), flags=r.strs(), material=r.str())

def write_capsule(c):
    return U32.pack(c["u0"]) + w_f32(c["radius"]) + w_vec4(c["p0"]) + w_vec4(c["p1"]) + U32.pack(c["u1"]) + w_strs(c["flags"]) + w_str(c["material"])

def read_sphere(r):
    return dict(u0=r.u32(), radius=r.f32(), center=r.vec4(), u1=r.u32(), flags=r.strs(), material=r.str())

def write_sphere(s):
    return U32.pack(s["u0"]) + w_f32(s["radius"]) + w_vec4(s["center"]) + U32.pack(s["u1"]) + w_strs(s["flags"]) + w_str(s["material"])

def read_list(r, fn): return [fn(r) for _ in range(r.u32())]
def write_list(l, fn): return U32.pack(len(l)) + b"".join(fn(x) for x in l)


def capsule(p0, p1, radius, material="", flags=()):
    """A capsule from two end points (x, y, z) and a radius."""
    return dict(u0=0, radius=np.float32(radius), p0=vec4(*p0), p1=vec4(*p1), u1=0, flags=list(flags), material=material)

def sphere(center, radius, material="", flags=()):
    return dict(u0=0, radius=np.float32(radius), center=vec4(*center), u1=0, flags=list(flags), material=material)


# ---------------------------------------------------------------------------------------------------------- description data
# Name, then three lists: capsules, spheres, manifolds. A manifold is a triangle soup: u32 (unknown, 0), u32 vertex count,
# vertices as vec4 (w = 1), material names, u32 face count, then per face: i32 (kept as "face_material", the open-source reader's
# material index, but 0 on every game face), flag names, three i32 vertex indices and a fourth i32 ("face_extra"; the open-source reader
# calls it a fourth index). The fourth i32 is the face's index into the material names, -1 without materials: it is what the engine
# cooks into the triangle mesh's material table and what decides the dominant material (dominant_of, material_index_of). Every
# manifold of a game shape is cooked twice: as a triangle mesh (static cook) and as a convex hull (dynamic cook).
FACE_FIXED = 24                                                           # material i32 + flag count u32 + 4 x i32, when no face has flag names

def read_manifold(r):
    m = dict(u0=r.u32()); n = r.u32(); m["vertices"] = np.frombuffer(r.raw(16 * n), "<f4").reshape(n, 4).copy(); m["materials"] = r.strs()
    nf = r.u32(); o = r.o
    if o + FACE_FIXED * nf <= len(r.b):
        a = np.frombuffer(r.b, "<i4", 6 * nf, o).reshape(nf, 6)
        if not nf or not a[:, 1].any():                                   # the fast path: no face carries flag names
            r.o = o + FACE_FIXED * nf; m["face_material"] = a[:, 0].copy(); m["face_index"] = a[:, 2:5].copy(); m["face_extra"] = a[:, 5].copy(); m["face_flags"] = None
            return m
    m["face_material"] = np.zeros(nf, "<i4"); m["face_index"] = np.zeros((nf, 3), "<i4"); m["face_extra"] = np.zeros(nf, "<i4"); m["face_flags"] = []
    for i in range(nf):
        m["face_material"][i] = r.i32(); m["face_flags"].append(r.strs()); m["face_index"][i] = [r.i32() for _ in range(3)]; m["face_extra"][i] = r.i32()
    return m

def write_manifold(m):
    nf = len(m["face_index"]); out = [U32.pack(m["u0"]), U32.pack(len(m["vertices"])), np.asarray(m["vertices"], "<f4").tobytes(), w_strs(m["materials"]), U32.pack(nf)]
    fm = np.asarray(m["face_material"], "<i4"); fi = np.asarray(m["face_index"], "<i4").reshape(nf, 3); fx = np.asarray(m["face_extra"], "<i4")
    if m["face_flags"] is None:
        a = np.zeros((nf, 6), "<i4"); a[:, 0] = fm; a[:, 2:5] = fi; a[:, 5] = fx; out.append(a.tobytes())
    else:
        for i in range(nf): out.append(I32.pack(int(fm[i])) + w_strs(m["face_flags"][i]) + fi[i].tobytes() + I32.pack(int(fx[i])))
    return b"".join(out)

def parse_desc(b):
    r = Reader(b); d = dict(name=r.str()); d["capsules"] = read_list(r, read_capsule); d["spheres"] = read_list(r, read_sphere)
    d["manifolds"] = read_list(r, read_manifold); d["tail"] = r.rest(); return d

def pack_desc(d):
    return w_str(d["name"]) + write_list(d["capsules"], write_capsule) + write_list(d["spheres"], write_sphere) + write_list(d["manifolds"], write_manifold) + d["tail"]


# ---------------------------------------------------------------------------------------------------------- resource record
# u64 length, then: u32 1, string (a material name, often empty), u8 0, 16 bytes (the same on shapes of one family: kept raw),
# capsules, spheres (as above), manifold summaries, bounds. A manifold summary: u32 (the description's u32), material names,
# i32 (index of the dominant material, -1 without materials), u32 face count, u32 vertex count, u32 0.
# Bounds: box minimum, box maximum, centre (all vec4 with w = 1) and f32 radius.
def parse_record(b):
    if len(b) < 8 or struct.unpack_from("<Q", b, 0)[0] != len(b) - 8: raise ValueError("record length")
    r = Reader(b, 8); d = dict(version=r.u32(), material=r.str(), b0=r.u8(), source=r.raw(16))
    d["capsules"] = read_list(r, read_capsule); d["spheres"] = read_list(r, read_sphere)
    n = r.u32(); d["u0"] = r.u32()                      # one u32 (0 on every game shape) follows the manifold count, even when there is none
    d["manifolds"] = [dict(materials=r.strs(), dominant=r.i32(), faces=r.u32(), vertices=r.u32(), u1=r.u32()) for _ in range(n)]
    d["bounds"] = dict(min=r.vec4(), max=r.vec4(), center=r.vec4(), radius=r.f32()); d["tail"] = r.rest(); return d

def pack_record(d):
    body = U32.pack(d["version"]) + w_str(d["material"]) + bytes([d["b0"]]) + d["source"] + write_list(d["capsules"], write_capsule) + write_list(d["spheres"], write_sphere)
    body += U32.pack(len(d["manifolds"])) + U32.pack(d["u0"])
    body += b"".join(w_strs(m["materials"]) + I32.pack(m["dominant"]) + U32.pack(m["faces"]) + U32.pack(m["vertices"]) + U32.pack(m["u1"]) for m in d["manifolds"])
    bd = d["bounds"]; body += w_vec4(bd["min"]) + w_vec4(bd["max"]) + w_vec4(bd["center"]) + w_f32(bd["radius"]) + d["tail"]
    return struct.pack("<Q", len(body)) + body


# ---------------------------------------------------------------------------------------------------------- dynamic cook
# u8 1, spheres, capsules (lists as above), then one material name per manifold (u32 count + names), then 8 x u64 offsets
# and the hulls: for every manifold a PhysX convex mesh ("NXS" 1 "CVXM", PhysX cooked data, kept raw), stored as u32 size +
# bytes, manifold after manifold, 8 times in a row: group k is the hull cooked from the vertices mirrored by MIRROR[k] (group 0 all
# three axes negated, group 7 as authored). The 8 offsets are where each group starts counted in hull bytes (size prefixes not counted).
# With no manifold the 8 offsets are 0 and nothing follows. A shape whose cook is a lone byte (23 occluders) keeps just that byte.
def parse_dyn(b):
    if len(b) == 1: return dict(version=b[0], short=True)
    r = Reader(b); d = dict(version=r.u8(), short=False); d["spheres"] = read_list(r, read_sphere); d["capsules"] = read_list(r, read_capsule)
    d["materials"] = r.strs(); n = len(d["materials"]); off = [struct.unpack("<Q", r.raw(8))[0] for _ in range(HULL_SLOTS)]
    d["groups"] = [[r.raw(r.u32()) for _ in range(n)] for _ in range(HULL_SLOTS if n else 0)]; d["tail"] = r.rest()
    d["offsets_raw"] = off if off != hull_offsets(d["groups"]) else None   # None: the offsets are the derived ones
    return d

def hull_offsets(groups):
    out = []; pos = 0
    for k in range(HULL_SLOTS):
        out.append(pos)
        if groups: pos += sum(len(h) for h in groups[k])
    return out

def pack_dyn(d):
    if d["short"]: return bytes([d["version"]])
    off = d["offsets_raw"] if d.get("offsets_raw") is not None else hull_offsets(d["groups"])
    out = bytes([d["version"]]) + write_list(d["spheres"], write_sphere) + write_list(d["capsules"], write_capsule) + w_strs(d["materials"])
    out += b"".join(struct.pack("<Q", o) for o in off)
    return out + b"".join(U32.pack(len(h)) + h for g in d["groups"] for h in g) + d["tail"]


# ---------------------------------------------------------------------------------------------------------- static cook
# spheres, capsules, then the triangle meshes: u32 count and per mesh u32 size + a PhysX triangle mesh ("NXS" 1 "MESH", cooked
# data, kept raw), u32 (0) and the mesh's material names. Nothing follows the last mesh.
def parse_stat(b):
    r = Reader(b); d = dict(spheres=read_list(r, read_sphere), capsules=read_list(r, read_capsule))
    d["meshes"] = read_list(r, lambda r: dict(cook=r.raw(r.u32()), u0=r.u32(), materials=r.strs())); d["tail"] = r.rest(); return d

def pack_stat(d):
    return write_list(d["spheres"], write_sphere) + write_list(d["capsules"], write_capsule) + write_list(d["meshes"], lambda m: U32.pack(len(m["cook"])) + m["cook"] + U32.pack(m["u0"]) + w_strs(m["materials"])) + d["tail"]


# ---------------------------------------------------------------------------------------------------------- PhysX blobs
# The hulls and meshes are PhysX 4.1 cooked data (the game ships PhysX 4.1.2; physx_cook.py cooks them with its library). Only their
# front is decoded here; the rest (polygons, edges, the mid-phase tree) stays bytes. A hull whose first cook hit the 255 polygon limit
# holds a second, quantized cook right after the first (see physx_cook); hull_cook() reads the first.
#   mesh: "NXS" 1 "MESH", u32 version (15), u32 0, u32 serial flags (1 material indices, 2 face remap, 4 8-bit indices, 8 16-bit), u32 vertex
#         count, u32 triangle count, vertices as 3 f32, indices (u8 / u16 / u32 by the flags), u16 material index per triangle when flag 1,
#         face remap when flag 2 (u32 largest id, then the ids as u8 / u16 / u32), then the "RTRE" tree page data and local bounds.
#   hull: "NXS" 1 "CVXM", u32 version (13), u32 0, "ICE" 1 "CLHL", u32 version (9), u32 vertices, edges, polygons, polygon vertex references,
#         vertices as 3 f32, then polygons, edges and the Gauss map.
def mesh_cook(b):
    """Front of a cooked triangle mesh: dict(version, flags, vertices (n, 3), triangles (k, 3), material_index or None, remap or None, rest_at)."""
    if b[:8] != b"NXSMESH": raise ValueError("not a cooked triangle mesh")
    r = Reader(b, 8); d = dict(version=r.u32()); r.u32(); d["flags"] = f = r.u32(); nv, nt = r.u32(), r.u32()
    d["vertices"] = np.frombuffer(r.raw(12 * nv), "<f4").reshape(nv, 3).copy()
    dt = "<u1" if f & 4 else "<u2" if f & 8 else "<u4"; w = np.dtype(dt).itemsize
    d["triangles"] = np.frombuffer(r.raw(3 * nt * w), dt).reshape(nt, 3).astype(np.uint32)
    d["material_index"] = np.frombuffer(r.raw(2 * nt), "<u2").copy() if f & 1 else None; d["remap"] = None
    if f & 2:
        mx = r.u32(); dt = "<u1" if mx <= 0xff else "<u2" if mx <= 0xffff else "<u4"; d["remap"] = np.frombuffer(r.raw(nt * np.dtype(dt).itemsize), dt).astype(np.uint32)
    d["rest_at"] = r.o; return d

def hull_cook(b):
    """Front of a cooked convex hull: dict(version, vertices (n, 3), polygons, edges, rest_at)."""
    if b[:8] != b"NXSCVXM" or b[16:24] != b"ICECLHL": raise ValueError("not a cooked convex hull")
    r = Reader(b, 8); d = dict(version=r.u32()); r.u32(); r.raw(8); r.u32(); nv, ne, npoly, nref = r.u32(), r.u32(), r.u32(), r.u32()
    d["vertices"] = np.frombuffer(r.raw(12 * nv), "<f4").reshape(nv, 3).copy(); d.update(edges=ne, polygons=npoly, references=nref, rest_at=r.o); return d


# ---------------------------------------------------------------------------------------------------------- whole shape
def decode(name, record, blobs):
    """{name, record, desc, dyn, stat} from a resource's record and its three raw data blobs ({data type guid: bytes})."""
    return dict(name=name, record=parse_record(record), desc=parse_desc(blobs[DESC]), dyn=parse_dyn(blobs[DYN]), stat=parse_stat(blobs[STAT]))

def encode(shape):
    """(record bytes, {data type guid: raw bytes}) of a decoded shape."""
    if needs_cook(shape): raise ValueError("this shape has manifolds but no cooked PhysX data: build() it with cook=None where the game's PhysX library loads (%s), or pass cook="
                                           % (physx_cook.why_not() or "it loads here"))
    return pack_record(shape["record"]), {DESC: pack_desc(shape["desc"]), DYN: pack_dyn(shape["dyn"]), STAT: pack_stat(shape["stat"])}

def needs_cook(shape):
    return not shape["dyn"].get("short") and (shape["dyn"]["groups"] is None or shape["stat"]["meshes"] is None)


# ---------------------------------------------------------------------------------------------------------- building a shape
# What is derived (and checked against the game's own shapes by check_build): the record's primitive and manifold summaries and its
# bounds, the primitives of both cooks (a primitive without a material takes the record's), the dynamic cook's one name per
# manifold, the static cook's per-mesh numbers, the dominant material index (dominant_of). The cooked PhysX hulls and meshes are made
# by the game's PhysX library (cook_manifolds, checked by check_cook); the mesh mid-phase (midphase=) is a choice, not derived. Not
# derived: the record's 16 "source" bytes (a random guid unless given) and the long flag lists the record and the cooks carry for a
# primitive that has flags (flag_lists=).
def bounds_of(capsules, spheres, manifolds):
    """The record's bounds as the engine writes them: box over capsule ends and sphere centres grown by the radius, and over the
    manifold vertices; centre = box middle; radius = distance from the centre to a box corner (float32 throughout). Where the extreme
    is 0 the sign comes from the first candidate that reaches it (candidates in the order capsules, spheres, manifold vertices)."""
    f = np.float32; LO = []; HI = []
    for c in capsules:
        for q in (c["p0"], c["p1"]): LO.append((q[:3] - f(c["radius"]))[None]); HI.append((q[:3] + f(c["radius"]))[None])
    for c in spheres: LO.append((c["center"][:3] - f(c["radius"]))[None]); HI.append((c["center"][:3] + f(c["radius"]))[None])
    for m in manifolds:
        if len(m["vertices"]): LO.append(m["vertices"][:, :3]); HI.append(m["vertices"][:, :3])
    if not LO: lo = hi = np.zeros(3, "<f4")
    else:
        L = np.concatenate(LO).astype("<f4"); H = np.concatenate(HI).astype("<f4"); lo = L[np.argmax(L == L.min(0), axis=0), range(3)]; hi = H[np.argmax(H == H.max(0), axis=0), range(3)]
    cen = (lo + hi) * f(0.5); one = np.ones(1, "<f4")
    return dict(min=np.r_[lo, one].astype("<f4"), max=np.r_[hi, one].astype("<f4"), center=np.r_[cen, one].astype("<f4"), radius=f(np.sqrt(((hi - cen) ** 2).sum(dtype="<f4"))))

def manifold(vertices, faces, materials=(), face_material=None, face_extra=None):
    """A manifold (triangle soup) from vertices (n, 3) and triangles (k, 3). materials: physics material names; face_material: index into
    them per triangle (default 0, or -1 without materials). It is stored where the game stores it: in the fourth i32 of each face
    ("face_extra"), which is what the engine cooks into the mesh's material table and counts for the dominant material; the first i32
    ("face_material" in the decoded dict) is 0 on every game manifold (16701 of 16701) and is written 0. face_extra= overrides the index."""
    V = np.asarray(vertices, "<f4").reshape(-1, 3); F = np.asarray(faces, "<i4").reshape(-1, 3)
    if len(F) and (F.min() < 0 or F.max() >= len(V)): raise ValueError("a face points outside the vertices")
    idx = face_extra if face_extra is not None else face_material
    fx = np.full(len(F), 0 if materials else -1, "<i4") if idx is None else np.asarray(idx, "<i4").reshape(len(F))
    if len(fx) and (fx.min() < -1 or (fx.max() >= len(materials))) and not (not materials and (fx == -1).all()): raise ValueError("a face material index is outside the materials")
    return dict(u0=0, vertices=np.c_[V, np.ones(len(V), "<f4")].astype("<f4"), materials=list(materials), face_material=np.zeros(len(F), "<i4"), face_index=F, face_extra=fx, face_flags=None)

def dominant_of(m):
    """The record's dominant material index of a manifold: the most used per-face material index (face_extra) among the manifold's
    materials, the lowest on a tie, -1 when no face has one. Matches the stored index on 16701 of 16701 game manifolds."""
    fx = np.asarray(m["face_extra"]); fx = fx[(fx >= 0) & (fx < len(m["materials"]))]
    if not len(fx): return -1
    v, n = np.unique(fx, return_counts=True); return int(v[np.argmax(n)])

def material_index_of(m):
    """The u16 material index per triangle the engine cooks into a manifold's triangle mesh: face_extra, -1 as 0. Reproduces the
    stored mesh on all but 21 of 16701 game meshes; those 21 were cooked with all-zero indices (the first i32 of the faces), which
    nothing in the shape predicts, so pass material_index= for them."""
    return np.maximum(np.asarray(m["face_extra"], np.int64), 0).astype("<u2")

def triangle_mesh(vertices, faces, materials=(), **kw):
    """A triangle-mesh body: the description's manifold. build() cooks it into the static cook (and a hull into the dynamic cook, as the engine does for every manifold)."""
    return manifold(vertices, faces, materials, **kw)

def hull_triangles(points):
    """Triangles (k, 3) of the convex hull of points (n, 3), outward wound, by incremental insertion (numpy; fine for a few thousand points).
    The points are nudged by 1e-7 of their size (fixed seed) for the insertion so that coplanar corners (a cube's face) do not stall it; the
    indices refer to the original points. Raises when the points are flat."""
    P0 = np.asarray(points, np.float64).reshape(-1, 3); sc = max(float(np.abs(P0).max()), 1e-9)
    P = P0 + np.random.RandomState(7).uniform(-1, 1, P0.shape) * 1e-7 * sc
    i0 = 0; i1 = int(np.argmax(np.linalg.norm(P - P[0], axis=1))); i2 = int(np.argmax(np.linalg.norm(np.cross(P - P[i0], P[i1] - P[i0]), axis=1)))
    nrm = np.cross(P[i1] - P[i0], P[i2] - P[i0]); i3 = int(np.argmax(np.abs((P - P[i0]) @ nrm)))
    if np.linalg.norm(nrm) < 1e-6 * sc * sc or abs((P[i3] - P[i0]) @ nrm) < 1e-6 * sc ** 3 * 1e-3: raise ValueError("the points are flat")
    c = P[[i0, i1, i2, i3]].mean(0); faces = []
    for a_, b_, c_ in ((i0, i1, i2), (i0, i1, i3), (i0, i2, i3), (i1, i2, i3)):
        faces.append((a_, b_, c_) if np.dot(np.cross(P[b_] - P[a_], P[c_] - P[a_]), P[a_] - c) >= 0 else (a_, c_, b_))
    for p in range(len(P)):
        if p in (i0, i1, i2, i3): continue
        F = np.array(faces); N = np.cross(P[F[:, 1]] - P[F[:, 0]], P[F[:, 2]] - P[F[:, 0]])
        vis = ((P[p] - P[F[:, 0]]) * N).sum(1) > 0
        if not vis.any(): continue
        edges = set()
        for f in F[vis]: edges |= {(f[0], f[1]), (f[1], f[2]), (f[2], f[0])}
        faces = [tuple(f) for f in F[~vis]] + [(a_, b_, p) for (a_, b_) in edges if (b_, a_) not in edges]
    return np.array(faces, np.int32)

def convex_hull(points, materials=()):
    """A convex-hull body from points (n, 3): the description's manifold holds the hull's triangles. The engine's PhysX cooks it into the
    dynamic cook (8 mirrored cooks of the hull, build() makes them) and into the static cook as a triangle mesh."""
    P = np.asarray(points, np.float64).reshape(-1, 3); F = hull_triangles(P); used = np.unique(F); remap = -np.ones(len(P), np.int32); remap[used] = np.arange(len(used))
    return manifold(P[used], remap[F], materials)

# The 8 hull groups of the dynamic cook: group k is the manifold's vertices scaled by MIRROR[k] and cooked (x is negated when bit 2 of k is
# clear, y bit 1, z bit 0: group 0 is (-x, -y, -z), group 7 the vertices as they are). Found by cooking all 8 sign combinations of game
# manifolds and matching them to the stored groups; every reproduced hull follows this order.
MIRROR = np.array([[1 if k & 4 else -1, 1 if k & 2 else -1, 1 if k & 1 else -1] for k in range(HULL_SLOTS)], "<f4")

def cook_manifolds(manifolds, dynamic_cook=True, midphase="bvh33", material_index=None):
    """The cooked PhysX data of manifolds, made by the game's own PhysX cooking library (physx_cook): dict(hulls=[8 groups, each one hull
    per manifold], meshes=[one triangle mesh per manifold]), the cook= of build(). midphase: "bvh33" (the "RTRE" tree, 16299 of the 16701
    game meshes) or "bvh34" ("BV4 ", the other 402). material_index: per manifold the u16 per triangle (default material_index_of)."""
    mans = list(manifolds); mi = [material_index_of(m) if material_index is None or material_index[i] is None else material_index[i] for i, m in enumerate(mans)]
    meshes = [physx_cook.mesh(m["vertices"][:, :3], m["face_index"], x, midphase) for m, x in zip(mans, mi)]
    hulls = [[physx_cook.hull(m["vertices"][:, :3] * MIRROR[k]) for m in mans] for k in range(HULL_SLOTS)] if dynamic_cook and mans else None
    return dict(hulls=hulls, meshes=meshes)

def _flags(own, lists, where):
    if not own: return []
    if not lists or where not in lists: raise ValueError("a primitive has flags: pass flag_lists={'record': [...], 'dyn': [...], 'stat': [...]}, the long lists a game shape carries for the same flags")
    return list(lists[where])

def resolve_material(materials, dominant, default):
    """(physics material name the dynamic cook stores for a manifold, u32 the static cook stores): the shape's default material (the record's)
    when it has one, with u32 0; else the manifold's dominant material with the dominant index as u32 (0xffffffff when there is none)."""
    if default: return default, 0
    return (materials[dominant] if materials and 0 <= dominant < len(materials) else ""), dominant & 0xffffffff

def build(name, capsules=(), spheres=(), manifolds=(), material="", source=None, dominant=None, flag_lists=None, cook=None, dynamic_cook=True, midphase="bvh33"):
    """A decoded shape from its primitives (capsule(), sphere()) and manifolds (triangle_mesh(), convex_hull()). material: the record's default
    physics material name. source: 16 bytes (default random). dominant: per manifold the dominant material index (default dominant_of).
    flag_lists: see _flags.
    cook: dict(hulls=[8 groups, each a list of one cooked hull per manifold], meshes=[one cooked triangle mesh per manifold]). None: cooked
    here by cook_manifolds() with the game's PhysX library (midphase= passed on) when the library loads, else left out; False: left out. A shape with
    manifolds and no cooked data is returned but encode() refuses it. dynamic_cook=False: a lone-byte dynamic cook (no hulls, as 46 game
    shapes have), so only meshes are needed."""
    caps = [dict(c, flags=list(c["flags"])) for c in capsules]; sphs = [dict(c, flags=list(c["flags"])) for c in spheres]; mans = list(manifolds)
    dom = [dominant_of(m) for m in mans] if dominant is None else list(dominant)
    if cook is None and mans and physx_cook.available(): cook = cook_manifolds(mans, dynamic_cook, midphase)
    if len(dom) != len(mans): raise ValueError("one dominant index per manifold")
    def res(lst, where): return [dict(c, material=c["material"] or material, flags=_flags(c["flags"], flag_lists, where)) for c in lst]
    def keep(lst, where): return [dict(c, flags=_flags(c["flags"], flag_lists, where)) for c in lst]
    desc = dict(name=name, capsules=caps, spheres=sphs, manifolds=mans, tail=b"")
    rec = dict(version=1, material=material, b0=0, source=source or uuid.uuid4().bytes, capsules=keep(caps, "record"), spheres=keep(sphs, "record"), u0=0,
               manifolds=[dict(materials=list(m["materials"]), dominant=d, faces=len(m["face_index"]), vertices=len(m["vertices"]), u1=0) for m, d in zip(mans, dom)],
               bounds=bounds_of(caps, sphs, mans), tail=b"")
    cook = cook or {}; groups = [] if not mans else cook.get("hulls"); meshes = [] if not mans else cook.get("meshes")
    rs = [resolve_material(m["materials"], d, material) for m, d in zip(mans, dom)]
    dyn = dict(version=0, short=True) if not dynamic_cook else dict(version=1, short=False, spheres=res(sphs, "dyn"), capsules=res(caps, "dyn"), offsets_raw=None, tail=b"",
                                                                    groups=groups, materials=[r[0] for r in rs])
    stat = dict(spheres=res(sphs, "stat"), capsules=res(caps, "stat"), tail=b"",
                meshes=None if meshes is None else [dict(cook=c, u0=r[1], materials=[material] if material else list(m["materials"])) for c, m, r in zip(meshes, mans, rs)])
    return dict(name=name, record=rec, desc=desc, dyn=dyn, stat=stat)


# ---------------------------------------------------------------------------------------------------------- packages
# A physics shape is one resource with three data entries in this order: description, dynamic cook, static cook. In a package the entry
# table row holds u32 0 / 3 / 3 after the hash (set through tpac.DATA_KIND above); every data hash is xxh64 of the raw bytes and the record
# hash is xxh64 of the record (both verified on all game shapes by check()).
def resources(path):
    """Yield dict(name, guid, record, hash, entries=[dict(type, raw, stored, rest)]) for every physics shape of a package (mapped, not loaded)."""
    with open(path, "rb") as f:
        b = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if b[:4] != b"TPAC": return
            meta, = struct.unpack_from("<Q", b, 0x1c); pk = tpac_pack.parse_package(b[:0x24 + meta])
            for it in pk["items"]:
                if it["type"] != SHAPE_TYPE: continue
                ents = []
                for e in it["entries"]:
                    st = bytes(b[e["off"]:e["off"] + e["stored"]])
                    ents.append(dict(type=bytes(e["rest"][16:32]), stored=st, raw=tpac.lz4_decompress(st, e["raw"]) if e["stored"] < e["raw"] else st, rest=e["rest"]))
                yield dict(name=it["name"].decode("utf-8", "replace"), guid=it["guid"], record=it["record"], hash=it["hash"], entries=ents)
        finally:
            b.close()

def decode_resource(res):
    """decode() of a resources() item."""
    return decode(res["name"], res["record"], {e["type"]: e["raw"] for e in res["entries"]})

def pack_one(package, guid, name, record, entries):
    """One-resource package. entries = [(data type guid, raw bytes)]. Like tpac.pack_package, except that the u8 after each entry's type value is
    1 only when the stored copy is LZ4 (the game's physics shapes keep small blobs plain with 0; tpac.pack_package always writes 1)."""
    nm = name.encode("utf-8", "surrogateescape"); head = b"TPAC" + struct.pack("<I", 2) + package + struct.pack("<II", 1, 0) + bytes(4) + SHAPE_TYPE + guid + bytes(4)         + U32.pack(len(nm)) + nm + record + struct.pack("<Q", tpac.xxh64(record)) + U32.pack(len(entries))
    meta = len(head) + 69 * len(entries) + 4; table = b""; data = b""
    for ty, raw in entries:
        c = tpac.lz4_compress(raw); c = c if len(c) < len(raw) else raw
        table += struct.pack("<QQQ", meta + len(data), len(raw), len(c)) + guid + ty + struct.pack("<Q", tpac.xxh64(raw)) + struct.pack("<IB", tpac.DATA_KIND.get(ty, 0), len(c) < len(raw)); data += c
    out = bytearray(head + table + bytes(4) + data); struct.pack_into("<I", out, 0x1c, meta - 0x24)   # payload length = metadata only
    return bytes(out)

def package_bytes(shape, package=None, guid=None):
    """The bytes of a new one-resource package holding the shape (the layout of tpac.pack_package, the SDK's other one-resource writer)."""
    rec, blobs = encode(shape)
    return pack_one(package or uuid.uuid4().bytes, guid or uuid.uuid4().bytes, shape["name"], rec, [(t, blobs[t]) for t in DATA_ORDER])

def write_package(shape, out_dir=None, force=False, package=None, guid=None):
    """Write <shape name>_phys.tpac (the suffix is this SDK's choice, the game scans every .tpac) into out_dir (default the module's Assets
    folder, as the other writers). Returns the path."""
    out = os.path.join(out_dir or tpac.ASSETS, shape["name"] + "_phys.tpac")
    if os.path.exists(out) and not force: raise FileExistsError(out + " exists (use force)")
    data = package_bytes(shape, package, guid)
    if out_dir: os.makedirs(out_dir, exist_ok=True)
    with open(out, "wb") as f: f.write(data)
    return out


# ---------------------------------------------------------------------------------------------------------- measurement
PARTS = ("record", "desc", "dyn", "stat")

def check_resource(res, stored=False):
    """Failure classes (a list of short strings, empty when the resource is byte-identical) of one resources() item: the four decoded parts
    re-encoded must equal the originals, the record hash and the three data hashes must be the xxh64 the SDK would write, and with
    stored=True the stored (LZ4 or plain) form the SDK's writer picks must equal the file's."""
    bad = []
    try:
        s = decode_resource(res); rec, blobs = pack_record(s["record"]), {DESC: pack_desc(s["desc"]), DYN: pack_dyn(s["dyn"]), STAT: pack_stat(s["stat"])}
    except Exception as e:
        return ["decode: %s" % str(e)[:60]]
    if rec != res["record"]: bad.append("record")
    if tuple(e["type"] for e in res["entries"]) != DATA_ORDER: bad.append("entry order")
    for e in res["entries"]:
        k = {DESC: "desc", DYN: "dyn", STAT: "stat"}.get(e["type"], "other")
        if blobs.get(e["type"]) != e["raw"]: bad.append(k)
        if struct.pack("<Q", tpac.xxh64(e["raw"])) != e["rest"][32:40]: bad.append(k + " hash")
        if e["rest"][40:] != struct.pack("<IB", tpac.DATA_KIND.get(e["type"], 0), len(e["stored"]) < len(e["raw"])): bad.append(k + " row")
        if stored:
            c = tpac.lz4_compress(e["raw"]); c = c if len(c) < len(e["raw"]) else e["raw"]
            if c != e["stored"]: bad.append(k + " stored")
    if struct.pack("<Q", tpac.xxh64(res["record"])) != res["hash"]: bad.append("record hash")
    return bad

def native_packages(module=None):
    """Every .tpac under <game>/Modules/<module>/AssetPackages and EmAssetPackages (all modules when module is None), sorted."""
    return sorted(glob.glob(os.path.join(config.MODULES, module or "*", "AssetPackages", "**", "*.tpac"), recursive=True)
                  + glob.glob(os.path.join(config.MODULES, module or "*", "EmAssetPackages", "**", "*.tpac"), recursive=True))

def check(paths=None, limit=None, stored=False, log=None, per_package=None):
    """Round-trip every physics shape of the given packages (default: every package of every module that holds any). Returns dict(packages,
    with_shapes, shapes, identical, failures={class: (count, first example 'package:shape')})."""
    files = list(paths) if paths else native_packages(); out = dict(packages=0, with_shapes=0, shapes=0, identical=0, failures={}); t0 = time.time()
    for p in files:
        out["packages"] += 1; n = 0
        try:
            for res in resources(p):
                if (limit and out["shapes"] >= limit) or (per_package and n >= per_package): break
                n += 1; out["shapes"] += 1; bad = check_resource(res, stored)
                if not bad: out["identical"] += 1
                for k in set(bad):
                    c, ex = out["failures"].get(k, (0, "%s:%s" % (os.path.basename(p), res["name"]))); out["failures"][k] = (c + 1, ex)
        except Exception as e:
            c, ex = out["failures"].get("package: %s" % str(e)[:50], (0, os.path.basename(p))); out["failures"]["package: %s" % str(e)[:50]] = (c + 1, ex)
        out["with_shapes"] += bool(n)
        if log and n: log("%-48s %5d shapes  (%.0f s)" % (os.path.basename(p), n, time.time() - t0))
        if limit and out["shapes"] >= limit: break
    return out

def check_build(paths=None, limit=None, per_package=None, cooked=False):
    """Rebuild every game shape with build() from its own description, record material, source and flag lists, and compare the four parts.
    The cooked data is taken from the game shape, or with cooked=True made by build() itself with the game's PhysX library (the mid-phase
    of the stored meshes passed on, as it cannot be derived). Returns dict(shapes, identical, by_kind={kind: [n, identical]},
    failures={class: (count, example)}). Kinds: 'primitives' (no manifold), 'manifolds' (with manifolds, one real material or none),
    'manifolds, several materials'. A shape that differs is classed by the parts that differ."""
    files = list(paths) if paths else native_packages(); out = dict(shapes=0, identical=0, by_kind={}, failures={})
    for p in files:
        n = 0
        for res in resources(p):
            if limit and out["shapes"] >= limit: return out
            if per_package and n >= per_package: break
            n += 1; s = decode_resource(res); D, R, Y, S = s["desc"], s["record"], s["dyn"], s["stat"]; out["shapes"] += 1
            kind = "primitives" if not D["manifolds"] else "manifolds" if all(len(m["materials"]) <= 1 for m in D["manifolds"]) else "manifolds, several materials"
            if Y["short"]: kind = "manifolds, no dynamic cook"
            k = out["by_kind"].setdefault(kind, [0, 0]); k[0] += 1
            fl = {"record": next((c["flags"] for c in R["capsules"] + R["spheres"] if c["flags"]), None), "dyn": next((c["flags"] for c in ([] if Y["short"] else Y["capsules"] + Y["spheres"]) if c["flags"]), None),
                  "stat": next((c["flags"] for c in S["capsules"] + S["spheres"] if c["flags"]), None)}
            fl = {a: b for a, b in fl.items() if b is not None}
            try:
                mid = "bvh34" if any(m["cook"][mesh_cook(m["cook"])["rest_at"]:][:4] == b"BV4 " for m in S["meshes"]) else "bvh33"
                b = build(D["name"], D["capsules"], D["spheres"], D["manifolds"], R["material"], R["source"], flag_lists=fl, midphase=mid,       # dominant: derived (dominant_of)
                          cook=None if cooked else dict(hulls=None if Y["short"] else Y["groups"], meshes=[m["cook"] for m in S["meshes"]]), dynamic_cook=not Y["short"])
                rec, blobs = encode(b); bad = [n for n, a, c in (("record", rec, res["record"]), ("desc", blobs[DESC], res["entries"][0]["raw"]), ("dyn", blobs[DYN], res["entries"][1]["raw"]),
                                                              ("stat", blobs[STAT], res["entries"][2]["raw"])) if a != c]
            except Exception as e:
                bad = ["exception: %s" % str(e)[:50]]
            if not bad: out["identical"] += 1; k[1] += 1
            else:
                key = "+".join(bad); c, ex = out["failures"].get((kind, key), (0, "%s:%s" % (os.path.basename(p), res["name"]))); out["failures"][(kind, key)] = (c + 1, ex)
    return out

MESH_VARIANTS = (("bvh33", None), ("bvh34", None), ("bvh33", "zero"), ("bvh34", "zero"))   # mid-phase, material indices (None: material_index_of)

def check_cook(paths=None, limit=None, per_package=None, log=None):
    """Cook every manifold of the game's shapes with the game's PhysX library from the shape's own description (cook_manifolds) and compare
    with the stored cooks. A hull counts when all 8 mirrored copies are identical. A mesh is tried with the defaults (BVH33, material_index_of),
    then BVH34, then all-zero material indices; the first variant that is identical names its class. Returns dict(shapes, manifolds, hulls
    (manifolds with a dynamic cook), hulls_identical, hull_copies, hull_copies_identical, meshes={variant or 'none': count}, failures)."""
    files = list(paths) if paths else native_packages(); out = dict(shapes=0, manifolds=0, hulls=0, hulls_identical=0, hull_copies=0, hull_copies_identical=0, meshes={}, failures={})
    for p in files:
        n = 0
        for res in resources(p):
            if limit and out["shapes"] >= limit: return out
            if per_package and n >= per_package: break
            s = decode_resource(res); D, Y, S = s["desc"], s["dyn"], s["stat"]
            if not D["manifolds"]: continue
            n += 1; out["shapes"] += 1; where = "%s:%s" % (os.path.basename(p), res["name"])
            for i, m in enumerate(D["manifolds"]):
                out["manifolds"] += 1; V = m["vertices"][:, :3]; got = "none"
                for mid, mat in MESH_VARIANTS:
                    if physx_cook.mesh(V, m["face_index"], material_index_of(m) if mat is None else np.zeros(len(m["face_index"]), "<u2"), mid) == S["meshes"][i]["cook"]:
                        got = mid + ("" if mat is None else ", zero materials"); break
                out["meshes"][got] = out["meshes"].get(got, 0) + 1
                if got == "none": c, ex = out["failures"].get("mesh", (0, where)); out["failures"]["mesh"] = (c + 1, ex)
                if Y["short"]: continue
                ok = sum(physx_cook.hull(V * MIRROR[k]) == Y["groups"][k][i] for k in range(HULL_SLOTS))
                out["hulls"] += 1; out["hulls_identical"] += ok == HULL_SLOTS; out["hull_copies"] += HULL_SLOTS; out["hull_copies_identical"] += ok
                if ok < HULL_SLOTS: c, ex = out["failures"].get("hull", (0, where)); out["failures"]["hull"] = (c + 1, ex)
        if log and n: log("%-48s %5d shapes with manifolds" % (os.path.basename(p), n))
    return out


# ---------------------------------------------------------------------------------------------------------- command line
def _v(v): return [round(float(x), 3) for x in np.asarray(v).reshape(-1)[:3]]

def _fl(f): return f if len(f) <= 3 else "%s ... (%d names)" % (f[:2], len(f))

def describe(shape):
    D, R, Y, S = shape["desc"], shape["record"], shape["dyn"], shape["stat"]; bd = R["bounds"]
    lines = ["%s  (default material %r, bounds %s .. %s)" % (shape["name"], R["material"], _v(bd["min"]), _v(bd["max"]))]
    for c in D["capsules"]: lines.append("   capsule  radius %.4g  %s -> %s  material %r  flags %s" % (c["radius"], _v(c["p0"]), _v(c["p1"]), c["material"], _fl(c["flags"])))
    for c in D["spheres"]: lines.append("   sphere   radius %.4g  at %s  material %r  flags %s" % (c["radius"], _v(c["center"]), c["material"], _fl(c["flags"])))
    for i, m in enumerate(D["manifolds"]):
        t = "   manifold %d: %d vertices, %d triangles, materials %s" % (i, len(m["vertices"]), len(m["face_index"]), m["materials"])
        if i < len(S["meshes"]): t += "; cooked: mesh %d bytes" % len(S["meshes"][i]["cook"])
        if not Y["short"] and Y["groups"]: t += ", hull %d bytes x %d" % (len(Y["groups"][0][i]), len(Y["groups"]))
        lines.append(t)
    if Y["short"]: lines.append("   dynamic cook: %d byte (no hulls)" % 1)
    return "\n".join(lines)

def main(argv):
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in ("--limit", "--module", "--name", "--material", "--midphase", "--out"))]; opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["info"] and len(a) >= 2:
        for res in resources(a[1]):
            if len(a) > 2 and res["name"] not in a[2:]: continue
            print(describe(decode_resource(res)))
        return 0
    if a[:1] == ["check"]:
        config.require_game(); lim = int(opt("--limit", 0)) or None
        r = check(paths=[a[1]] if len(a) > 1 else native_packages(opt("--module")), limit=lim, stored="--stored" in argv, log=print if "--verbose" in argv else None)
        print("packages scanned %d, with physics shapes %d, shapes %d, byte-identical %d of %d" % (r["packages"], r["with_shapes"], r["shapes"], r["identical"], r["shapes"]))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  fails %-22s %6d  e.g. %s" % (k, c, ex))
        return 0 if r["identical"] == r["shapes"] and r["shapes"] else 1
    if a[:1] == ["rebuild"]:
        config.require_game(); r = check_build(paths=[a[1]] if len(a) > 1 else native_packages(opt("--module")), limit=int(opt("--limit", 0)) or None, cooked="--cook" in argv)
        print("shapes %d, rebuilt from their description%s byte-identical %d" % (r["shapes"], " (cooked here)" if "--cook" in argv else "", r["identical"]))
        for k, (n, i) in sorted(r["by_kind"].items()): print("  %-30s %6d shapes, %6d identical" % (k, n, i))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  differs %-45s %6d  e.g. %s" % (k, c, ex))
        return 0
    if a[:1] == ["cookcheck"]:
        config.require_game(); physx_cook.cooker()
        r = check_cook(paths=[a[1]] if len(a) > 1 else native_packages(opt("--module")), limit=int(opt("--limit", 0)) or None, log=print if "--verbose" in argv else None)
        print("shapes with manifolds %d, manifolds %d; hulls identical %d of %d (copies %d of %d); meshes identical %d of %d"
              % (r["shapes"], r["manifolds"], r["hulls_identical"], r["hulls"], r["hull_copies_identical"], r["hull_copies"], r["manifolds"] - r["meshes"].get("none", 0), r["manifolds"]))
        for k, c in sorted(r["meshes"].items(), key=lambda x: -x[1]): print("  mesh %-24s %6d" % (k, c))
        for k, (c, ex) in sorted(r["failures"].items()): print("  differs %-6s %6d  e.g. %s" % (k, c, ex))
        return 0
    if a[:1] == ["cook"] and len(a) >= 3 and a[1] in ("hull", "mesh"):
        P, F = read_obj(a[2]); name = opt("--name") or os.path.splitext(os.path.basename(a[2]))[0]; mats = [opt("--material")] if opt("--material") else []
        man = convex_hull(P, mats) if a[1] == "hull" else triangle_mesh(P, F, mats)
        sh = build(name, manifolds=[man], cook=cook_manifolds([man], midphase=opt("--midphase", "bvh33")))
        out = write_package(sh, opt("--out") or os.path.join(config.SDK_STAGE_DIR, "physics"), force="--force" in argv)
        print(describe(sh)); print("written", out); return 0
    print(__doc__); return 2

def read_obj(path):
    """(vertices (n, 3), triangles (k, 3)) of an .obj file: v lines, and f lines fanned into triangles (1-based or negative indices)."""
    P = []; F = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            w = line.split()
            if not w: continue
            if w[0] == "v": P.append([float(x) for x in w[1:4]])
            elif w[0] == "f":
                idx = [int(x.split("/")[0]) for x in w[1:]]; idx = [i - 1 if i > 0 else len(P) + i for i in idx]
                F += [[idx[0], idx[k], idx[k + 1]] for k in range(1, len(idx) - 1)]
    return np.array(P, np.float64).reshape(-1, 3), np.array(F, np.int64).reshape(-1, 3)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
