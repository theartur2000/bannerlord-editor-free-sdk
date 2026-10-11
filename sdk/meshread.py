"""Read the geometry of any shipped mesh by its name, straight from the game's asset packages.

    python sdk/meshread.py info rock_cliff_b          size, triangle count, open edges
    python sdk/meshread.py obj rock_cliff_b out.obj   the highest level of detail as an OBJ file

A package (.tpac) lists its resources; a mesh (a "metamesh") carries one "Mesh edit data" entry per submesh and level of
detail: positions, vertices that point at them, triangles. Nothing is unpacked to disk: the package is mapped and only the
entries of the mesh asked for are decompressed. The first call builds an index of every metamesh name (a few seconds);
it is kept under <SDK home>/cache.

shape(name) joins the submeshes of the highest level of detail and welds equal positions. open_edges(shape) are the
edges that belong to one triangle only: the rim of a mesh that is not closed. A cliff piece is an open shell; where its
rim is above the ground, the player sees through it.
"""
import glob, json, mmap, os, struct, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config, sdk_mesh, tpac
else:
    from . import config, sdk_mesh, tpac

INDEX = os.path.join(config.SDK_HOME, "cache", "metamesh_index.json")


def resources(b):
    """The resource table of a package (bytes or mmap), nothing decompressed: dicts of type, name, rec (offset, end),
    entries [(offset, raw size, stored size, owner, type)]."""
    q = 0x24; size = len(b)
    for _ in range(struct.unpack_from("<I", b, 0x18)[0]):
        if q + 40 > size: return
        ty = bytes(b[q:q + 16]); n = struct.unpack_from("<i", b, q + 36)[0]
        if n < 0 or q + 48 + n > size: return
        name = bytes(b[q + 40:q + 40 + n]).decode("utf-8", "replace"); R = q + 40 + n
        L = struct.unpack_from("<I", b, R)[0]; E = R + 8 + L
        if E + 12 > size: return
        ne = struct.unpack_from("<I", b, E + 8)[0]; q2 = E + 12; ents = []
        if q2 + 69 * ne > size: return
        for _ in range(ne):
            off, raw, st = struct.unpack_from("<QQQ", b, q2)
            ents.append((off, raw, st, bytes(b[q2 + 24:q2 + 40]), bytes(b[q2 + 40:q2 + 56]))); q2 += 69
        yield {"type": ty, "name": name, "rec": (R, E), "entries": ents}
        q = q2 + 4 + 48 * struct.unpack_from("<I", b, q2)[0]      # then a list of 48-byte references (three guids each), usually empty


def _open(path):
    f = open(path, "rb"); return f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


def index(refresh=False, log=None):
    """{metamesh name: package path relative to the game's Modules folder} for every package of every module."""
    if not refresh and os.path.isfile(INDEX):
        with open(INDEX, "r", encoding="utf-8") as f: return json.load(f)
    out = {}
    for p in sorted(glob.glob(os.path.join(config.MODULES, "*", "AssetPackages", "*.tpac"))):
        try:
            f, b = _open(p)
            try:
                if b[:4] != b"TPAC": continue
                for r in resources(b):
                    if r["type"] == sdk_mesh.META_TYPE: out.setdefault(r["name"], os.path.relpath(p, config.MODULES))
            finally:
                b.close(); f.close()
        except Exception as e:
            if log: log("skipped %s: %s" % (p, e))
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    with open(INDEX, "w", encoding="utf-8") as f: json.dump(out, f)
    return out


def read(name, lods=False):
    """[(submesh name, positions (n, 3), triangles (k, 3) as indices into the positions)] of a shipped metamesh."""
    rel = index().get(name)
    if rel is None: raise KeyError("no metamesh named %s in the game's packages" % name)
    f, b = _open(os.path.join(config.MODULES, rel)); out = []
    try:
        for r in resources(b):
            if r["type"] != sdk_mesh.META_TYPE or r["name"] != name: continue
            rec = bytes(b[r["rec"][0]:r["rec"][1]])
            for off, raw, st, owner, ty in r["entries"]:
                if ty != sdk_mesh.EDIT_DATA: continue
                i = rec.find(owner); n = struct.unpack_from("<I", rec, i + 16)[0]; sub = rec[i + 20:i + 20 + n].decode("utf-8", "replace")
                if ".lod" in sub.lower() and not lods: continue
                blob = tpac.lz4_decompress(bytes(b[off:off + st]), raw) if st < raw else bytes(b[off:off + st])
                E = sdk_mesh.parse_edit(blob)
                out.append((sub, np.array(E["P"][:, :3], np.float64), np.asarray(E["V"]["pi"], np.int64)[np.asarray(E["F"], np.int64)]))
            break
    finally:
        b.close(); f.close()
    return out


def shape(name):
    """(positions (n, 3), triangles (k, 3)) of the highest level of detail, submeshes joined, equal positions welded."""
    P = []; F = []; base = 0
    for sub, p, t in read(name):
        P.append(p); F.append(t + base); base += len(p)
    if not P: raise KeyError("metamesh %s has no edit data" % name)
    P = np.concatenate(P); F = np.concatenate(F)
    key, inv = np.unique(np.round(P, 3), axis=0, return_inverse=True)
    F = inv.reshape(-1)[F]; F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    return key, F


def open_edges(P, F):
    """Edges that belong to one triangle only, as an (m, 2) array of position indices: the rim of an open mesh."""
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]); e.sort(axis=1)
    u, c = np.unique(e, axis=0, return_counts=True)
    return u[c == 1]


def to_obj(name, out):
    P, F = shape(name)
    with open(out, "w") as f:
        f.write("o %s\n" % name)
        for p in P: f.write("v %.4f %.4f %.4f\n" % tuple(p))
        for t in F + 1: f.write("f %d %d %d\n" % tuple(t))


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "info":
        for name in a[1:]:
            P, F = shape(name); rim = open_edges(P, F); L = float(np.linalg.norm(P[rim[:, 0]] - P[rim[:, 1]], axis=1).sum()) if len(rim) else 0.0
            print("%-30s %6d triangles  x %.1f..%.1f  y %.1f..%.1f  z %.1f..%.1f  open rim %.0f m (%d edges)" % (
                (name, len(F)) + tuple(np.c_[P.min(0), P.max(0)].reshape(-1)) + (L, len(rim))))
    elif len(a) == 3 and a[0] == "obj": to_obj(a[1], a[2]); print("wrote", a[2])
    elif a and a[0] == "index": print(len(index(refresh=True, log=print)), "metameshes")
    else: print(__doc__)
