"""Cloth simulation data of Bannerlord's meshes: read, write what can be written, round trip.

    python sdk/cloth.py info <mesh name>                       a mesh's cloth records: proxy mesh, clothing materials, cloth map, cloth cook
    python sdk/cloth.py check [--limit N] [--module M] [package]
                                                               decode and re-encode every shipped cloth record, compare byte for byte (N of M)

Cloth is spread over four places (game v1.4.8, measured on every package of every official module, read only):

1. The metamesh record. Per submesh the record carries a "clothing material" (below) and, in the submesh's flag list, "uses_cloth_simulation"
   (the submesh that is simulated) or "force_enable_cloth". The metamesh itself carries `cloth_mesh`, the guid of the cloth PROXY metamesh (a
   coarse mesh of the same garment, "clo_<name>"; 161 positions for a helmet that has 6465 vertices), and a string (the body region: "head",
   "human_body", "arms", "legs", "cape_body" ...). tw_formats.parse_metamesh_record reads both.
   The clothing material is the block at the end of each submesh (parse_clothing / pack_clothing): f32 (1 for most meshes), the material name
   (empty on most), nine f32 (bending, shearing, stretching, anchor stiffness, damping, gravity, linear inertia, air drag multiplier, wind),
   an i32 (120 for most, 240 for a few), two bytes, f32 maximum linear velocity (-1 = none), f32 linear velocity multiplier, one byte. The
   names and values of the ones with a name are listed in ModuleData/cloth_materials.xml (simulation: stretching_stiffness, anchor_stiffness,
   bending_stiffness, shearing_stiffness_, damping, gravity, linear_inertia, max_linear_velocity, linear_velocity_multiplier, wind,
   air_drag_multiplier); ModuleData/cloth_bodies.xml lists the collision capsules of the bodies cloth collides with.
2. "Cloth map" (af41613e, 1572 entries, owner = the simulated submesh): how the submesh's vertices follow the proxy. u32 count n = 3 x vertices
   and n u16 (three proxy position indices per vertex: a triangle of the proxy), u32 count m = 4 x vertices and m f32 (per vertex a vec4: three
   weights that add up to 1, then a small offset). It is a binding of every render vertex to a triangle of the proxy.
3. "Cloth cook" (b97e0f10, 898 entries, version 1, owner = a banner or flag submesh, whose own positions are the simulated particles): the
   engine's cooked cloth fabric. Header: u32 hash, u32 constraints (c), u32 particles (equal to the submesh's position count), three more u32
   counts, bounds min and max as vec4 (w = 1, equal to the submesh's bounding box); then arrays, each a u32 count and that many 4-byte values:
   rest lengths (c f32), the constraints packed (c u32), and up to seven short arrays (phases and sets of the solver); then a tail of per-particle
   data (kept as bytes here).
4. Nothing else: the vertex stream carries no cloth data.

Measured over all official modules (`check`): 197,072 clothing blocks, 1,572 cloth maps and 898 cloth cooks decode and re-encode byte-identical. Their own rules hold with
exceptions in the shipped data: 4 maps have more vertices than the submesh they belong to (e.g. battania_light_armor_c: 9,890 against 8,390), 3 cooks disagree with their submesh (bounds on 3,
particle count on 1: ship_roof_a_l3).

Not verified, not done:
  - Nothing here was tried in the game.
  - A cloth map can be read, kept and written back, and its structure is checked (counts, indices inside the proxy, weights adding up to 1), but
    the rule that makes the weights (which proxy triangle a vertex binds to, and what the weights and the offset are relative to) is not known:
    a plain barycentric reconstruction of the vertex from the proxy's rest positions is 0.2 m off on average. A new garment would copy a
    shipped map (same topology) or needs that rule.
  - A cloth cook is NvCloth-style cooked fabric data. Reading its header and arrays is checked, the tail is bytes. Writing a new one needs the
    fabric cooker (constraint graph from the mesh, phase colouring, tether data) and the engine's serialisation of it; neither is in the install
    (the game ships PhysX 4.1 DLLs, no cloth library), so a writer is not possible from here. A cook can be copied byte-identical for a mesh that
    has the same particles.
  - The stored (LZ4) form of a written entry uses this SDK's compressor; raw bytes, hashes and rows are the identical part.
No game content is stored in the repository: the module reads the install at run time.
"""
import glob, mmap, os, struct, sys, time, uuid

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, sdk_mesh, tpac, tpac_pack, tw_formats
else:
    from . import config, sdk_mesh, tpac, tpac_pack, tw_formats

CLOTH_MAP = uuid.UUID("af41613e-bd4d-46ec-8284-debb56c042dc").bytes_le     # data: "Cloth map"
CLOTH_COOK = uuid.UUID("b97e0f10-df49-4666-8f1d-ada6630d4b4e").bytes_le    # data: "Cloth cook"
tpac.DATA_KIND.update({CLOTH_MAP: 0, CLOTH_COOK: 1})


# ---------------------------------------------------------------------------------------------------------- clothing material (record)
MATERIAL_FIELDS = ("bending_stiffness", "shearing_stiffness", "stretching_stiffness", "anchor_stiffness", "damping", "gravity", "linear_inertia", "air_drag", "wind")

def parse_clothing(p):
    """The block tw_formats.parse_metamesh_record returns as a submesh's "params": dict(factor, name, <MATERIAL_FIELDS>, i0, b0, b1,
    max_linear_velocity, linear_velocity_multiplier, b2)."""
    o = 0; d = {}
    d["factor"], n = struct.unpack_from("<fI", p, o); o += 8; d["name"] = bytes(p[o:o + n]).decode("utf-8", "surrogateescape"); o += n
    for k, v in zip(MATERIAL_FIELDS, struct.unpack_from("<9f", p, o)): d[k] = v
    o += 36; d["i0"], = struct.unpack_from("<i", p, o); o += 4; d["b0"], d["b1"] = p[o], p[o + 1]; o += 2
    d["max_linear_velocity"], d["linear_velocity_multiplier"] = struct.unpack_from("<2f", p, o); o += 8; d["b2"] = p[o]; o += 1
    if o != len(p): raise ValueError("clothing block layout differs")
    return d

def pack_clothing(d):
    nm = d["name"].encode("utf-8", "surrogateescape")
    return struct.pack("<fI", d["factor"], len(nm)) + nm + struct.pack("<9f", *[d[k] for k in MATERIAL_FIELDS]) + struct.pack("<iBB", d["i0"], d["b0"], d["b1"]) \
        + struct.pack("<2fB", d["max_linear_velocity"], d["linear_velocity_multiplier"], d["b2"])


# ---------------------------------------------------------------------------------------------------------- cloth map
def parse_map(b):
    """dict(index (v, 3) u16, weight (v, 4) f32) of a cloth map entry."""
    n, = struct.unpack_from("<I", b, 0); idx = np.frombuffer(b, "<u2", n, 4); o = 4 + 2 * n
    m, = struct.unpack_from("<I", b, o); w = np.frombuffer(b, "<f4", m, o + 4); o += 4 + 4 * m
    if o != len(b) or n % 3 or m % 4 or n // 3 != m // 4: raise ValueError("cloth map layout differs")
    return dict(index=idx.reshape(-1, 3), weight=w.reshape(-1, 4))

def pack_map(d):
    i = np.asarray(d["index"], "<u2").reshape(-1, 3); w = np.asarray(d["weight"], "<f4").reshape(-1, 4)
    if len(i) != len(w): raise ValueError("one index triple and one weight vec4 per vertex")
    return struct.pack("<I", i.size) + i.tobytes() + struct.pack("<I", w.size) + w.tobytes()

def map_problems(d, vertices=None, proxy_positions=None):
    """Structure checks of a cloth map: vertex count, indices inside the proxy, weights adding up to 1. Returns a list of short strings."""
    bad = []
    if vertices is not None and len(d["index"]) != vertices: bad.append("vertex count")
    if proxy_positions is not None and len(d["index"]) and int(d["index"].max()) >= proxy_positions: bad.append("index outside the proxy")
    if len(d["weight"]) and float(np.abs(d["weight"][:, :3].sum(1) - 1).max()) > 1e-3: bad.append("weights do not add up to 1")
    return bad


# ---------------------------------------------------------------------------------------------------------- cloth cook
def parse_cook(b, arrays=9):
    """dict(hash, counts (5 u32), bounds (min vec4, max vec4), arrays [raw bytes of each count-prefixed 4-byte array], tail bytes). The arrays
    are read while their count fits the file, at most `arrays` of them: the first two have the constraint count, the later ones are short."""
    if len(b) < 56: raise ValueError("cloth cook too short")
    h, c0, c1, c2, c3, c4 = struct.unpack_from("<6I", b, 0); bounds = np.frombuffer(b, "<f4", 8, 24).reshape(2, 4); o = 56; arr = []
    while len(arr) < arrays and o + 4 <= len(b):
        c, = struct.unpack_from("<I", b, o)
        if len(arr) < 2 and c != c0: raise ValueError("cloth cook arrays differ from the header")
        if o + 4 + 4 * c > len(b) or (len(arr) >= 2 and c > 4 * c0 + 64): break
        arr.append(bytes(b[o:o + 4 + 4 * c])); o += 4 + 4 * c
    return dict(hash=h, counts=(c0, c1, c2, c3, c4), bounds=bounds.copy(), arrays=arr, tail=bytes(b[o:]))

def pack_cook(d):
    c = d["counts"]
    return struct.pack("<6I", d["hash"], *c) + np.asarray(d["bounds"], "<f4").tobytes() + b"".join(d["arrays"]) + d["tail"]

def cook_problems(d, positions=None, bounds=None):
    bad = []
    if positions is not None and d["counts"][1] != positions: bad.append("particle count")
    if bounds is not None and not np.allclose(d["bounds"][:, :3], np.asarray(bounds, np.float32).reshape(2, -1)[:, :3], atol=1e-4): bad.append("bounds")
    return bad


# ---------------------------------------------------------------------------------------------------------- measurement
def packages(module=None):
    pats = [os.path.join(config.MODULES, module or "*", d, "**", "*.tpac") for d in ("AssetPackages", "EmAssetPackages")]
    return sorted(set(p for pat in pats for p in glob.glob(pat, recursive=True)))

def _read(m, e):
    st = bytes(m[e["off"]:e["off"] + e["stored"]])
    return tpac.lz4_decompress(st, e["raw"]) if e["stored"] < e["raw"] else st

def proxy_index(paths):
    """{metamesh guid: (package path, item name, submesh position counts)} over the given packages."""
    out = {}
    for p in paths:
        try:
            with open(p, "rb") as f:
                h = f.read(0x24)
                if h[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", h, 0x1c); pk = tpac_pack.parse_package(h + f.read(meta))
        except Exception: continue
        for it in pk["items"]:
            if it["type"] == sdk_mesh.META_TYPE and it["guid"] not in out:
                try: out[it["guid"]] = (p, it["name"].decode("utf-8", "replace"), [s["positions"] for s in tw_formats.parse_metamesh_record(it["record"])["submeshes"]])
                except Exception: pass
    return out

def check(paths=None, limit=None, log=None):
    """Round-trip every cloth map, cloth cook and clothing-material block of the given packages (default: all of them). Returns a dict with the
    counts: submeshes and clothing blocks (blocks identical), maps (identical, structure problems, bound to a proxy found), cooks (identical,
    header problems), failures {class: (count, example)}."""
    files = list(paths) if paths else packages(); proxies = proxy_index(files) if files else {}
    out = dict(submeshes=0, clothing=0, clothing_identical=0, simulated=0, maps=0, maps_identical=0, maps_checked_against_proxy=0, map_problems=0,
               cooks=0, cooks_identical=0, cook_problems=0, named_materials=0, failures={}); t0 = time.time()
    def fail(k, where): c, e0 = out["failures"].get(k, (0, where)); out["failures"][k] = (c + 1, e0)
    for p in files:
        with open(p, "rb") as f:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                if m[:4] != b"TPAC": continue
                meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
                for it in pk["items"]:
                    if it["type"] != sdk_mesh.META_TYPE: continue
                    where = "%s:%s" % (os.path.basename(p), it["name"].decode("utf-8", "replace"))
                    try: rec = tw_formats.parse_metamesh_record(it["record"])
                    except Exception as ex: fail("record: %s" % str(ex)[:40], where); continue
                    subs = {s["guid"]: s for s in rec["submeshes"]}; px = proxies.get(rec["cloth_mesh"]) if rec["cloth_mesh"] != bytes(16) else None
                    for s in rec["submeshes"]:
                        out["submeshes"] += 1; out["clothing"] += 1; out["simulated"] += "uses_cloth_simulation" in s["flags"]
                        try: out["clothing_identical"] += pack_clothing(parse_clothing(s["params"])) == s["params"]; out["named_materials"] += bool(parse_clothing(s["params"])["name"])
                        except Exception as ex: fail("clothing: %s" % str(ex)[:40], where)
                    for e in it["entries"]:
                        ty = bytes(e["rest"][16:32])
                        if ty not in (CLOTH_MAP, CLOTH_COOK): continue
                        if limit and out["maps"] + out["cooks"] >= limit: return out
                        s = subs.get(bytes(e["rest"][0:16])); raw = _read(m, e)
                        if struct.pack("<Q", tpac.xxh64(raw)) != e["rest"][32:40]: fail("hash", where)
                        if e["rest"][40:] != struct.pack("<IB", 0 if ty == CLOTH_MAP else 1, e["stored"] < e["raw"]): fail("row", where)
                        try:
                            if ty == CLOTH_MAP:
                                out["maps"] += 1; d = parse_map(raw); same = pack_map(d) == raw; out["maps_identical"] += same
                                if not same: fail("map repack", where)
                                npos = None
                                if px: npos = max(px[2]) if px[2] else None; out["maps_checked_against_proxy"] += 1
                                bad = map_problems(d, s["vertices"] if s else None, npos)
                                if bad: out["map_problems"] += 1; [fail("map: " + b_, where) for b_ in bad]
                            else:
                                out["cooks"] += 1; d = parse_cook(raw); same = pack_cook(d) == raw; out["cooks_identical"] += same
                                if not same: fail("cook repack", where)
                                bad = cook_problems(d, s["positions"] if s else None, [s["bbox_min"], s["bbox_max"]] if s else None)
                                if bad: out["cook_problems"] += 1; [fail("cook: " + b_, where) for b_ in bad]
                        except Exception as ex:
                            fail("%s: %s" % ("map" if ty == CLOTH_MAP else "cook", str(ex)[:40]), where)
            finally:
                m.close()
        if log: log("%-48s maps %d cooks %d (%.0f s)" % (os.path.basename(p), out["maps"], out["cooks"], time.time() - t0))
    return out


# ---------------------------------------------------------------------------------------------------------- command line
def info(name):
    meshread = _meshread(); rel = meshread.index().get(name)
    if rel is None: print("no metamesh named", name); return 1
    with open(os.path.join(config.MODULES, rel), "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            meta, = struct.unpack_from("<Q", m, 0x1c); pk = tpac_pack.parse_package(m[:0x24 + meta])
            for it in pk["items"]:
                if it["type"] != sdk_mesh.META_TYPE or it["name"].decode("utf-8", "replace") != name: continue
                rec = tw_formats.parse_metamesh_record(it["record"]); subs = {s["guid"]: s for s in rec["submeshes"]}
                print("%s: proxy mesh %s, region %r" % (name, uuid.UUID(bytes_le=rec["cloth_mesh"]) if rec["cloth_mesh"] != bytes(16) else "none", rec["s0"]))
                for s in rec["submeshes"]:
                    c = parse_clothing(s["params"])
                    print("  %-34s flags %s  clothing %s" % (s["name"], s["flags"], ", ".join("%s %.3g" % (k.replace("_stiffness", ""), c[k]) for k in MATERIAL_FIELDS) + (" name %r" % c["name"] if c["name"] else "")))
                for e in it["entries"]:
                    ty = bytes(e["rest"][16:32])
                    if ty not in (CLOTH_MAP, CLOTH_COOK): continue
                    s = subs.get(bytes(e["rest"][0:16])); raw = _read(m, e)
                    if ty == CLOTH_MAP:
                        d = parse_map(raw); print("  cloth map of %s: %d vertices bound to proxy triangles (largest index %d)" % (s["name"] if s else "?", len(d["index"]), d["index"].max() if len(d["index"]) else -1))
                    else:
                        d = parse_cook(raw); print("  cloth cook of %s: %d constraints, %d particles, %d arrays, tail %d bytes" % (s["name"] if s else "?", d["counts"][0], d["counts"][1], len(d["arrays"]), len(d["tail"])))
                return 0
        finally:
            m.close()
    return 1

def _meshread():
    if __package__ in (None, ""):
        import meshread
    else:
        from . import meshread
    return meshread

def main(argv):
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] in ("--limit", "--module"))]; opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    if a[:1] == ["info"] and len(a) == 2:
        config.require_game(); return info(a[1])
    if a[:1] == ["check"]:
        config.require_game(); r = check([a[1]] if len(a) > 1 else packages(opt("--module")), int(opt("--limit", 0)) or None, log=print if "--verbose" in argv else None)
        print("submeshes %d: clothing material blocks byte-identical %d of %d (%d simulated, %d with a material name)" % (r["submeshes"], r["clothing_identical"], r["clothing"], r["simulated"], r["named_materials"]))
        print("cloth maps %d: byte-identical %d of %d, structure problems %d (proxy found for %d)" % (r["maps"], r["maps_identical"], r["maps"], r["map_problems"], r["maps_checked_against_proxy"]))
        print("cloth cooks %d: byte-identical %d of %d, header problems %d" % (r["cooks"], r["cooks_identical"], r["cooks"], r["cook_problems"]))
        for k, (c, ex) in sorted(r["failures"].items(), key=lambda x: -x[1][0]): print("  fails %-30s %6d  e.g. %s" % (k, c, ex))
        ok = r["clothing_identical"] == r["clothing"] and r["maps_identical"] == r["maps"] and r["cooks_identical"] == r["cooks"] and not r["failures"]
        return 0 if ok and (r["maps"] or r["cooks"]) else 1
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
