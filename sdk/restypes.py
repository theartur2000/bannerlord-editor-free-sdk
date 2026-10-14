"""The map of what the game's asset packages hold: every resource type guid, how often it occurs, and what this SDK does with it.

    python sdk/restypes.py survey [--module M]       the table: resource types with counts, their data entry types, and SDK support
    python sdk/restypes.py name <guid>               what a resource or data type guid is called here

A package (.tpac) is a list of resources; each resource has a type guid, a record, and data entries that have their own type guid. The
survey reads only the metadata of every package of the official modules (AssetPackages and EmAssetPackages), nothing is decompressed, and
needs the game install (BANNERLORD_DIR). The names are this SDK's; a type without one is printed as "unnamed". Support is what the code
in this repository does today, and "in game" is never claimed here: see each module's own notes for what was tried.

Game v1.4.8, official modules (Native, SandBox, NavalDLC, ...): 1260 packages and 13 resource types, 96,836 resources (the same
resource often sits in two packages: the AssetPackages and the EmAssetPackages copy). No other resource type occurs.
"""
import collections, glob, os, struct, sys, uuid

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, tpac_pack
else:
    from . import config, tpac_pack

def _g(s): return uuid.UUID(s).bytes_le

# name, SDK support (module and what it can do), one-line note
RESOURCE_TYPES = {
    _g("a08f8b97-197c-4bea-b95b-53846cae834e"): ("Metamesh", "read + write: sdk_mesh.py, meshread.py; morph keys: morph.py; cloth: cloth.py",
                                                  "a mesh with levels of detail; one submesh per material"),
    _g("e8528e0e-64b6-4e61-bae0-7569c0452aea"): ("Physics shape", "read + write: physics.py", "collision body: primitives, hulls, triangle meshes"),
    _g("c974cbcb-5f1c-49f6-9a32-2b5b6c92c2e8"): ("Texture", "read + write: tpac.py, texcodec.py", "pixels, import settings, source info"),
    _g("506509c8-e563-4ca4-b166-a53b92e913a7"): ("Animation clip", "read + write: tpac.py (clips), clip_partners.py", "named clip over a skeletal animation, with its cache"),
    _g("bafab007-7e3f-453f-bac6-e7640043112b"): ("Skeletal animation", "read + write: tpac.py", "keys per bone"),
    _g("1db01393-6902-4f19-83ba-b37a39830717"): ("Material", "read + write: tpac.py", "shader and its textures and parameters; record only"),
    _g("3eba3679-debd-4c7a-8634-f121f6325e33"): ("Import source", "read + written with the mesh and animation packages", "the editor's record of an FBX or PNG import; record only"),
    _g("c635a3d5-eabb-45dd-883e-aa57e4196113"): ("Skeleton", "read + write: sdk_skeleton.py", "bones, and physics user data"),
    _g("6de14d67-dd9a-45be-9463-0281c3d8dd51"): ("Particle effect", "read + write: particles.py", "emitters with curves, colours, materials; a baked table"),
    _g("b46528df-0344-4ff9-80f2-e30cd6c4fcc3"): ("Shader", "found by name or guid only (tpac.py); not decoded", "record only"),
    _g("cab5ba7b-818e-4432-8e37-03e291d9b8c0"): ("Morph animation", "read + write: morph.py", "face poses: key weights per frame"),
    _g("b938ca7b-e08e-497c-b30c-07314931809a"): ("GPU particle system", "read + write (record): particles.py", "record only, parameters as in gpu_particle_systems.xml"),
    _g("cc373411-785f-4ef0-95a2-d5cf54b24536"): ("Procedural vector field", "record + data bytes kept: particles.py", "a 16 x 16 x 16 field of vec3 for particles"),
}

DATA_TYPES = {
    _g("3d41985f-24d2-4fc1-82e4-6a6e0da3f4e2"): "Mesh edit data (sdk_mesh.py, morph.py)",
    _g("bb1df897-584f-4770-abf2-663fe449f247"): "Mesh vertex stream (sdk_mesh.py)",
    _g("644030f6-8a42-4c86-b935-9b9daa9391c2"): "Metamesh submesh names (sdk_mesh.py)",
    _g("af41613e-bd4d-46ec-8284-debb56c042dc"): "Cloth map (cloth.py)",
    _g("b97e0f10-df49-4666-8f1d-ada6630d4b4e"): "Cloth cook (cloth.py)",
    _g("86a5d575-5dc1-4542-a52f-3865aa8fb4fa"): "Physics description (physics.py)",
    _g("df27d12f-d074-45b8-a4ed-8068c0f4988d"): "Physics dynamic cook (physics.py)",
    _g("8283cbbb-3a79-40ea-aead-7f5a2ceb747f"): "Physics static cook (physics.py)",
    _g("70ee4e2c-79e4-4b2d-8d54-d53ecd2a559c"): "Texture pixel data (tpac.py)",
    _g("7c475be2-7c8d-431d-b0e1-53218d047fb9"): "Texture import settings (tw_formats.py)",
    _g("8ab981a7-6ba0-4908-9606-91ad341d19a9"): "Texture source info (tpac.py)",
    _g("6c1e136f-d0b7-41ff-bb7a-275f3630db25"): "Optimized animation (tpac.py)",
    _g("8fc3fc2a-4bc9-46c5-8525-f33a6e257b72"): "Shortened optimized animation (tpac.py)",
    _g("d07d816d-3ced-421c-a6af-b793e75dc2be"): "Animation definition (tpac.py)",
    _g("11d07d37-e720-406b-ab67-c846f96a8771"): "Skeleton definition (sdk_skeleton.py)",
    _g("9b6ac06d-a546-40af-a555-40d301ab4b2f"): "Skeleton user data (sdk_skeleton.py)",
    _g("326587ce-bb0c-4c22-8782-97e20cf03c5e"): "Particle effect data (particles.py)",
    _g("4c8cf3e5-9358-42d2-8a66-9ed2b2fe5f35"): "Particle baked table (particles.py)",
    _g("b77ddd3c-afe9-449b-9fe5-99de8b4060c1"): "Morph definition (morph.py)",
    _g("11d9c4bf-a5b9-42be-b6c1-0d02c03fa81a"): "Vector field samples (particles.py)",
    _g("54d56503-9944-4140-ad17-55bbd9ce9e39"): "unnamed, on textures (6945 entries; not decoded)",
    _g("4481676e-effe-402f-a093-de091115c2d4"): "unnamed, on textures (258 entries; not decoded)",
    _g("b6d3f417-dcc4-4384-bd79-26fe525da192"): "unnamed, on textures (258 entries; not decoded)",
}

def packages(module=None):
    pats = [os.path.join(config.MODULES, module or "*", d, "**", "*.tpac") for d in ("AssetPackages", "EmAssetPackages")]
    return sorted(set(p for pat in pats for p in glob.glob(pat, recursive=True)))

def survey(paths=None):
    """dict(packages, resources, types={type guid: dict(items, guids (set), packages, data {data type: entries}, raw {data type: raw bytes}, example)})."""
    out = dict(packages=0, resources=0, types={})
    for p in (paths or packages()):
        with open(p, "rb") as f:
            h = f.read(0x24)
            if h[:4] != b"TPAC": continue
            meta, = struct.unpack_from("<Q", h, 0x1c); pk = tpac_pack.parse_package(h + f.read(meta))
        out["packages"] += 1; seen = set()
        for it in pk["items"]:
            t = out["types"].setdefault(it["type"], dict(items=0, guids=set(), packages=0, data=collections.Counter(), raw=collections.Counter(), example=None))
            t["items"] += 1; t["guids"].add(it["guid"]); out["resources"] += 1
            if it["type"] not in seen: t["packages"] += 1; seen.add(it["type"])
            if t["example"] is None: t["example"] = "%s:%s" % (os.path.basename(p), it["name"].decode("utf-8", "replace"))
            for e in it["entries"]:
                dt = bytes(e["rest"][16:32]); t["data"][dt] += 1; t["raw"][dt] += e["raw"]
    return out

def describe(guid):
    g = bytes(guid)
    if g in RESOURCE_TYPES: return "resource type: " + RESOURCE_TYPES[g][0]
    if g in DATA_TYPES: return "data type: " + DATA_TYPES[g]
    return "unnamed"

def main(argv):
    a = [x for i, x in enumerate(argv) if not x.startswith("--") and not (i and argv[i - 1] == "--module")]
    if a[:1] == ["survey"]:
        config.require_game(); mod = argv[argv.index("--module") + 1] if "--module" in argv else None
        r = survey(packages(mod))
        print("%d packages, %d resources, %d resource types" % (r["packages"], r["resources"], len(r["types"])))
        print("%-36s %-24s %7s %7s %5s  %s" % ("type guid", "name", "items", "unique", "pkgs", "SDK today"))
        for g, t in sorted(r["types"].items(), key=lambda x: -x[1]["items"]):
            nm, sup, note = RESOURCE_TYPES.get(g, ("unnamed", "none", ""))
            print("%-36s %-24s %7d %7d %5d  %s" % (uuid.UUID(bytes_le=g), nm, t["items"], len(t["guids"]), t["packages"], sup))
            print("    %s; e.g. %s" % (note or "-", t["example"]))
            for dt, c in sorted(t["data"].items(), key=lambda x: -x[1]):
                print("    data %s  %7d entries %9.1f MB raw  %s" % (uuid.UUID(bytes_le=dt), c, t["raw"][dt] / 1e6, DATA_TYPES.get(dt, "unnamed")))
        return 0
    if a[:1] == ["name"] and len(a) == 2: print(describe(uuid.UUID(a[1]).bytes_le)); return 0
    print(__doc__); return 2

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
