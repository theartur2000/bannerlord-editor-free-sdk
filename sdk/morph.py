"""Morph data of Bannerlord's asset packages: face morph keys inside meshes, and morph animations. Read, write, round trip.

    python sdk/morph.py info <mesh name | animation name>      the keys of a mesh (with the skin's deform key names) or a morph animation
    python sdk/morph.py check [--limit N] [--all] [--module M] [package]
                                                               decode and re-encode every shipped morph record, compare byte for byte (N of M)

Two places hold morph data (game v1.4.8; everything below was measured on every package of every official module, read only):

1. Morph keys of a mesh live in its "Mesh edit data" entries (sdk_mesh.parse_edit, pack_edit). After the triangles the entry holds a list of
   key frames: u32 count, per frame a float32 key time, then per frame the positions (u32 count + vec4 each) and the normals (u32 count +
   vec4 each). A frame carries ABSOLUTE positions, one per position of the mesh (not per vertex), and one normal per vertex; frame 0 of a face
   equals the mesh's own positions (the basis), the others are the same mesh with one deform applied. The key time of the other frames is the
   deform key's number: ModuleData/skins.xml names them (<deform_key id="face_width" key_time_point="1" key_min key_max>) and the skin names
   its face mesh (face_meta_mesh="head_male_a"). The game's face generator sets a key's weight between key_min and key_max.
   The mesh's vertex stream (the runtime copy) carries no morph data, and neither does the metamesh record: the count of keys is not in the
   record (its fifth count is 0 on every mesh), so only the edit data tells. A mesh with one key frame is a cloth or vertex-animation mesh.
2. A morph animation is its own resource (type cab5ba7b "Morph animation", 505 resources, 253 distinct guids: morph_anims.tpac and its EmAssetPackages copy)
   with a record of u32 version 0, u8 0 and the guid of its import source, and one data entry "Morph definition" (b77ddd3c): the animation's name,
   u32 frame count and per frame i32 frame number, u32 target count and per target i32 morph target (the key time of the face mesh) and f32 progress
   (the weight, 0..1). So a pose such as "confused" is a list of (frame, key, weight): that is all a face animation is.

Measured over all official modules (`check --all`): 197,072 mesh edit data entries decode and re-encode byte-identical (195,827 have no key frame; 1,245 have 1 to 601 key
frames: 101 on the male and female head, 150 on some, 1 or 2 on armour and cloth, 10 to 27 on flags and sails), and 505 of 505 morph animations (10,568 frames,
114,972 targets) re-encode byte-identical with their record hash, definition hash and entry row.

Not verified, not done:
  - Nothing here was tried in the game: a mesh written with morph frames or a morph animation package is unverified in game.
  - What the engine does when a key is missing, when the frames of a mesh do not have the head's 101 times, or how it blends a pose over the
    frames of an animation is not known; the writers follow the shipped layouts only.
  - The stored (LZ4) form of a written entry uses this SDK's compressor (not the game's stream); raw bytes, hashes and rows are the identical part.
No game content is stored in the repository: the module reads the install at run time.
"""
import glob, mmap, os, struct, sys, time, uuid
import xml.etree.ElementTree as ET

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, sdk_mesh, tpac, tpac_pack, tw_formats
else:
    from . import config, sdk_mesh, tpac, tpac_pack, tw_formats

MORPH_TYPE = uuid.UUID("cab5ba7b-818e-4432-8e37-03e291d9b8c0").bytes_le      # "Morph animation" resource
MORPH_DEF = uuid.UUID("b77ddd3c-afe9-449b-9fe5-99de8b4060c1").bytes_le       # data: "Morph definition"
tpac.DATA_KIND.update({MORPH_DEF: 0})


# ---------------------------------------------------------------------------------------------------------- morph keys in meshes
def frame_summary(E):
    """(number of key frames, their times) of a parsed edit data."""
    return len(E["frames"]), [float(f["time"]) for f in E["frames"]]


def expected_plain_size(sm):
    """Raw size of an edit data entry with no key frame, no unused vec4, no smoothing masks, from the counts in the metamesh record's submesh
    (tw_formats.parse_metamesh_record): positions, vertices, faces, weighted positions. A different size means key frames (or masks)."""
    return 4 + 16 * sm["positions"] + 8 + sdk_mesh.VERT.itemsize * sm["vertices"] + 4 + 12 * sm["faces"] + 8 + sdk_mesh.WEIGHT.itemsize * sm["weighted"] + 4


def with_frames(E, frames):
    """A copy of parsed edit data E with the given key frames (list of dict(time, P (positions, 4), N (normals, 4)))."""
    n, m = len(E["P"]), len(E["V"])
    for f in frames:
        if len(f["P"]) != n or len(f["N"]) != m: raise ValueError("a key frame needs %d positions and %d normals" % (n, m))
    out = dict(E); out["frames"] = [dict(time=np.float32(f["time"]), P=np.asarray(f["P"], "<f4").reshape(n, 4), N=np.asarray(f["N"], "<f4").reshape(m, 4)) for f in frames]
    return out


def morph_key(E, time_point, offsets, normals=None):
    """A key frame for time_point: the mesh's own positions plus offsets (n, 3) (an (n, 3) array or {position index: (dx, dy, dz)}), normals as
    the mesh's vertex normals unless given (m, 3). The frame's w values are those of the mesh's positions."""
    n, m = len(E["P"]), len(E["V"]); P = np.array(E["P"], "<f4")
    if isinstance(offsets, dict):
        for i, d in offsets.items(): P[i, :3] += np.asarray(d, "<f4")
    else: P[:, :3] += np.asarray(offsets, "<f4").reshape(n, 3)
    N = np.array(E["V"]["n"], "<f4") if normals is None else np.c_[np.asarray(normals, "<f4").reshape(m, 3), np.ones(m, "<f4")]
    return dict(time=np.float32(time_point), P=P, N=N)


def deform_keys(skins_xml=None):
    """{face mesh name: [(key_time_point, id, display name, key_min, key_max)]} from ModuleData/skins.xml of the game (every skin's deform keys)."""
    path = skins_xml or os.path.join(config.NATIVE_DATA, "skins.xml"); out = {}
    for skin in ET.parse(path).getroot().iter("skin"):
        keys = [(int(k.get("key_time_point")), k.get("id"), k.get("name"), float(k.get("key_min", "nan")), float(k.get("key_max", "nan"))) for k in skin.iter("deform_key")]
        if skin.get("face_meta_mesh"): out.setdefault(skin.get("face_meta_mesh"), keys)
    return out


# ---------------------------------------------------------------------------------------------------------- morph animations
# Record: u64 length (21), u32 version (0), u8 0, import source guid. Data "Morph definition": name (u32 length + bytes), u32 frame count, per frame:
# i32 frame number, u32 target count, per target: i32 target (the face mesh's key time), f32 progress.
def parse_definition(b):
    n = struct.unpack_from("<I", b, 0)[0]; name = bytes(b[4:4 + n]).decode("utf-8", "surrogateescape"); o = 4 + n
    nf, = struct.unpack_from("<I", b, o); o += 4; frames = []
    for _ in range(nf):
        fr, nt = struct.unpack_from("<iI", b, o); o += 8
        a = np.frombuffer(b, np.dtype([("target", "<i4"), ("progress", "<f4")]), nt, o); o += 8 * nt
        frames.append((fr, a))
    if o != len(b): raise ValueError("morph definition layout differs")
    return dict(name=name, frames=frames)


def pack_definition(d):
    nm = d["name"].encode("utf-8", "surrogateescape"); out = [struct.pack("<I", len(nm)), nm, struct.pack("<I", len(d["frames"]))]
    for fr, a in d["frames"]:
        a = np.asarray(a, np.dtype([("target", "<i4"), ("progress", "<f4")])); out += [struct.pack("<iI", fr, len(a)), a.tobytes()]
    return b"".join(out)


def parse_record(rec):
    if len(rec) != 29 or struct.unpack_from("<Q", rec, 0)[0] != 21: raise ValueError("morph record layout differs")
    return dict(version=struct.unpack_from("<I", rec, 8)[0], b0=rec[12], source=bytes(rec[13:29]))


def pack_record(r): return struct.pack("<QIB", 21, r["version"], r["b0"]) + r["source"]


def pose(name, frames, source=None):
    """A morph animation from {frame number: {target (face key time point): progress}}."""
    fr = []
    for f in sorted(frames):
        t = sorted(frames[f].items()); fr.append((f, np.array(t, np.dtype([("target", "<i4"), ("progress", "<f4")]))))
    return dict(name=name, frames=fr, record=dict(version=0, b0=0, source=source or uuid.uuid4().bytes_le))


def package_bytes(anim, package=None, guid=None):
    """A one-resource package holding the animation (a morph animation has no import source resource of its own in this package: the record's
    source guid names the editor's FBX import, which the game never reads)."""
    guid = guid or uuid.uuid4().bytes_le
    return tpac.pack_resources(package or uuid.uuid4().bytes_le, [(MORPH_TYPE, guid, 0, anim["name"], pack_record(anim["record"]), [(MORPH_DEF, pack_definition(anim))])])


def write_package(anim, out_dir=None, force=False, package=None, guid=None):
    out = os.path.join(out_dir or tpac.ASSETS, anim["name"] + "_morph.tpac")
    if os.path.exists(out) and not force: raise FileExistsError(out + " exists (use force)")
    data = package_bytes(anim, package, guid)
    if out_dir: os.makedirs(out_dir, exist_ok=True)
    with open(out, "wb") as f: f.write(data)
    return out


# ---------------------------------------------------------------------------------------------------------- measurement
def packages(module=None):
    pats = [os.path.join(config.MODULES, module or "*", d, "**", "*.tpac") for d in ("AssetPackages", "EmAssetPackages")]
    return sorted(set(p for pat in pats for p in glob.glob(pat, recursive=True)))


def _read(m, e):
    st = bytes(m[e["off"]:e["off"] + e["stored"]])
    return tpac.lz4_decompress(st, e["raw"]) if e["stored"] < e["raw"] else st


def check_meshes(paths=None, limit=None, everything=False, per_package=2, log=None):
    """Edit data round trip. Every entry whose size differs from expected_plain_size (key frames or masks: the interesting ones) is decoded and
    re-encoded; with everything=True all of them are, otherwise per_package plain ones per package are added as a control. Returns dict(entries
    (all edit data entries seen), special (size differs), decoded, identical, by_frames {frame count: entries}, failures {message: (count, example)},
    cloth_like (one frame), faces (101 frames))."""
    out = dict(entries=0, special=0, decoded=0, identical=0, by_frames={}, failures={}, plain_checked=0); t0 = time.time()
    for p in (paths or packages()):
        with open(p, "rb") as f:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                if m[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta]); plain = 0
                for it in pk["items"]:
                    if it["type"] != sdk_mesh.META_TYPE: continue
                    try: subs = {s["guid"]: s for s in tw_formats.parse_metamesh_record(it["record"])["submeshes"]}
                    except Exception as ex:
                        c, e0 = out["failures"].get("record: %s" % str(ex)[:40], (0, "%s:%s" % (os.path.basename(p), it["name"].decode("utf-8", "replace")))); out["failures"]["record: %s" % str(ex)[:40]] = (c + 1, e0); continue
                    for e in it["entries"]:
                        if bytes(e["rest"][16:32]) != sdk_mesh.EDIT_DATA: continue
                        out["entries"] += 1; sm = subs.get(bytes(e["rest"][0:16]))
                        special = sm is None or e["raw"] != expected_plain_size(sm); out["special"] += special
                        if not special:
                            if not everything and plain >= per_package: continue
                            plain += 1
                        if limit and out["decoded"] >= limit: return out
                        out["decoded"] += 1; where = "%s:%s" % (os.path.basename(p), it["name"].decode("utf-8", "replace"))
                        try:
                            d = _read(m, e); E = sdk_mesh.parse_edit(d); same = sdk_mesh.pack_edit(E) == d
                            k = len(E["frames"]); out["by_frames"][k] = out["by_frames"].get(k, 0) + 1
                            if not special: out["plain_checked"] += 1
                            if same: out["identical"] += 1
                            else:
                                c, e0 = out["failures"].get("repack differs", (0, where)); out["failures"]["repack differs"] = (c + 1, e0)
                        except Exception as ex:
                            key = "decode: %s" % str(ex)[:50]; c, e0 = out["failures"].get(key, (0, where)); out["failures"][key] = (c + 1, e0)
            finally:
                m.close()
        if log: log("%-48s %d decoded (%.0f s)" % (os.path.basename(p), out["decoded"], time.time() - t0))
    return out


def check_animations(paths=None, limit=None):
    """Morph animation round trip: record, definition, record hash, definition hash and entry row. Returns dict(animations, identical, unique, frames, targets,
    failures)."""
    out = dict(animations=0, identical=0, guids=set(), frames=0, targets=0, failures={})
    for p in (paths or packages()):
        with open(p, "rb") as f:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                if m[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
                for it in pk["items"]:
                    if it["type"] != MORPH_TYPE: continue
                    if limit and out["animations"] >= limit: return out
                    out["animations"] += 1; out["guids"].add(it["guid"]); bad = []; where = "%s:%s" % (os.path.basename(p), it["name"].decode("utf-8", "replace"))
                    try:
                        if pack_record(parse_record(it["record"])) != it["record"]: bad.append("record")
                        if struct.pack("<Q", tpac.xxh64(it["record"])) != it["hash"]: bad.append("record hash")
                        if len(it["entries"]) != 1 or it["entries"][0]["rest"][16:32] != MORPH_DEF: bad.append("entries")
                        else:
                            e = it["entries"][0]; raw = _read(m, e); d = parse_definition(raw)
                            if pack_definition(d) != raw: bad.append("definition")
                            if d["name"].encode() != it["name"]: bad.append("name")
                            if struct.pack("<Q", tpac.xxh64(raw)) != e["rest"][32:40]: bad.append("definition hash")
                            if e["rest"][40:] != struct.pack("<IB", 0, e["stored"] < e["raw"]): bad.append("row")
                            out["frames"] += len(d["frames"]); out["targets"] += sum(len(a) for _, a in d["frames"])
                    except Exception as ex:
                        bad.append("decode: %s" % str(ex)[:50])
                    if not bad: out["identical"] += 1
                    for k in set(bad): c, e0 = out["failures"].get(k, (0, where)); out["failures"][k] = (c + 1, e0)
            finally:
                m.close()
    return out


# ---------------------------------------------------------------------------------------------------------- command line
def _meshread():
    if __package__ in (None, ""):
        import meshread
    else:
        from . import meshread
    return meshread


def info_mesh(name):
    """Print the key frames of a shipped mesh by name (all its edit data entries that have any)."""
    meshread = _meshread()
    rel = meshread.index().get(name)
    if rel is None: return False
    with open(os.path.join(config.MODULES, rel), "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            names = {}
            for skin, keys in deform_keys().items(): names[skin] = {k[0]: k for k in keys}
            for r in meshread.resources(m):
                if r["type"] != sdk_mesh.META_TYPE or r["name"] != name: continue
                rec = bytes(m[r["rec"][0]:r["rec"][1]])
                for off, raw, st, owner, ty in r["entries"]:
                    if ty != sdk_mesh.EDIT_DATA: continue
                    i = rec.find(owner); n = struct.unpack_from("<I", rec, i + 16)[0]; sub = rec[i + 20:i + 20 + n].decode("utf-8", "replace")
                    blob = tpac.lz4_decompress(bytes(m[off:off + st]), raw) if st < raw else bytes(m[off:off + st]); E = sdk_mesh.parse_edit(blob)
                    k, times = frame_summary(E)
                    print("%-30s %6d positions %6d vertices %6d triangles, %d key frames" % (sub, len(E["P"]), len(E["V"]), len(E["F"]), k))
                    for f in E["frames"][:200]:
                        moved = int((np.abs(f["P"][:, :3] - E["P"][:, :3]).max(1) > 1e-6).sum()); nm = names.get(name, {}).get(int(f["time"]))
                        if k > 1: print("    key %-5g moves %5d positions   %s" % (f["time"], moved, "%s (%g .. %g)" % (nm[1], nm[3], nm[4]) if nm else ""))
                return True
        finally:
            m.close()
    return False


def info_animation(name):
    for p in packages("Native"):
        with open(p, "rb") as f:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                if m[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
                for it in pk["items"]:
                    if it["type"] == MORPH_TYPE and it["name"].decode("utf-8", "replace") == name:
                        d = parse_definition(_read(m, it["entries"][0])); print("%s: %d frames" % (name, len(d["frames"])))
                        for fr, a in d["frames"]: print("   frame %4d: %s" % (fr, ", ".join("%d=%.3f" % (t, g) for t, g in a)))
                        return True
            finally:
                m.close()
    return False


def main(argv):
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in ("--limit", "--module"))]; opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["info"] and len(a) == 2:
        config.require_game()
        if info_mesh(a[1]) or info_animation(a[1]): return 0
        print("no mesh or morph animation named", a[1]); return 1
    if a[:1] == ["check"]:
        config.require_game(); paths = [a[1]] if len(a) > 1 else packages(opt("--module")); lim = int(opt("--limit", 0)) or None
        r = check_animations(paths, lim)
        print("morph animations %d (%d distinct), %d frames, %d targets: record, definition, hashes and rows byte-identical %d of %d" % (r["animations"], len(r["guids"]), r["frames"], r["targets"], r["identical"], r["animations"]))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  fails %-22s %6d  e.g. %s" % (k, c, ex))
        bad = r["identical"] != r["animations"] or not r["animations"]
        r = check_meshes(paths, lim, "--all" in argv, log=print if "--verbose" in argv else None)
        print("mesh edit data entries %d, of which %d differ from the plain size (key frames or masks); decoded %d (%d plain as a control), byte-identical %d of %d"
              % (r["entries"], r["special"], r["decoded"], r["plain_checked"], r["identical"], r["decoded"]))
        print("  by number of key frames: " + ", ".join("%d: %d" % kv for kv in sorted(r["by_frames"].items())))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  fails %-30s %6d  e.g. %s" % (k, c, ex))
        return 1 if bad or r["identical"] != r["decoded"] or not r["decoded"] else 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
