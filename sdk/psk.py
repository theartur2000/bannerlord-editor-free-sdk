"""Read a PSK / PSKX static mesh (the format umodel and most Unreal exporters write) into the SDK's submesh form.

    subs = psk.submeshes("Rock.pskx", "my_rock", scale=0.01)      # [submesh dict, ...], one per material
    sdk_mesh.write_metamesh("my_rock", "my_rock", subs, materials={...}, out_dir=..., rdc_dir=...)

    python sdk/psk.py info Rock.pskx

The file is a list of chunks: 20 bytes of name, type flag, record size, record count (three int32), then the records.
PNTS0000 points (3 float32), VTXW0000 wedges (point index, u, v, material), FACE0000 / FACE3200 triangles of wedges with a
material index and a smoothing-group mask, MATT0000 material names, EXTRAUVS<n> further UV sets.

Unreal is left-handed with z up and works in centimetres. Bannerlord is right-handed with z up, in metres: y is negated
and the default scale is 0.01. The triangle order is kept: negating one axis already turns Unreal's front faces into the
game's (checked on closed rocks: the signed volume comes out positive, as for the game's own meshes; turned the other way
a model is lit from the inside and shows black). Normals are not stored in a PSK: they are computed here,
smooth across triangles that share a point and a smoothing group.
"""
import os, struct, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sdk_mesh
else:
    from . import sdk_mesh


def chunks(path):
    """{chunk name: (record size, count, bytes)}"""
    b = open(path, "rb").read(); o = 0; out = {}
    while o + 32 <= len(b):
        name = b[o:o + 20].split(b"\0")[0].decode("ascii", "replace"); _, size, n = struct.unpack_from("<iii", b, o + 20)
        out[name] = (size, n, b[o + 32:o + 32 + size * n]); o += 32 + size * n
    return out


def read(path):
    """{"points" (n, 3), "wedge_point" (w,), "uv" (w, 2), "faces" (f, 3) wedge indices, "face_mat" (f,), "smooth" (f,),
    "materials" [names]} in the file's own space (Unreal: left-handed, centimetres)."""
    c = chunks(path)
    size, n, d = c["PNTS0000"]; P = np.frombuffer(d, "<f4", 3 * n).reshape(n, 3).astype(np.float64)
    size, w, d = c["VTXW0000"]
    if size == 16 and n > 65536: W = np.frombuffer(d, np.dtype([("p", "<u4"), ("u", "<f4"), ("v", "<f4"), ("m", "<u4")]), w)
    elif size == 16: W = np.frombuffer(d, np.dtype([("p", "<u2"), ("_", "<u2"), ("u", "<f4"), ("v", "<f4"), ("m", "u1"), ("r", "u1"), ("__", "<u2")]), w)
    else: raise ValueError("%s: wedge record of %d bytes" % (path, size))
    if "FACE3200" in c and c["FACE3200"][1]:
        size, f, d = c["FACE3200"]; Fr = np.frombuffer(d, np.dtype([("w", "<u4", 3), ("m", "u1"), ("a", "u1"), ("s", "<u4")]), f)
    else:
        size, f, d = c["FACE0000"]; Fr = np.frombuffer(d, np.dtype([("w", "<u2", 3), ("m", "u1"), ("a", "u1"), ("s", "<u4")]), f)
    mats = []
    if "MATT0000" in c:
        size, m, d = c["MATT0000"]
        mats = [d[k * size:k * size + 64].split(b"\0")[0].decode("ascii", "replace") for k in range(m)]
    extra = []
    for k in range(8):
        key = "EXTRAUVS%d" % k
        if key in c and c[key][1] == w: extra.append(np.frombuffer(c[key][2], "<f4", 2 * w).reshape(w, 2).astype(np.float64))
    return {"points": P, "wedge_point": W["p"].astype(np.int64), "uv": np.c_[W["u"], W["v"]].astype(np.float64), "extra_uv": extra,
            "faces": Fr["w"].astype(np.int64), "face_mat": Fr["m"].astype(np.int64), "smooth": Fr["s"].astype(np.int64), "materials": mats}


def submeshes(path, name, scale=0.01, rotation=None, offset=(0.0, 0.0, 0.0), materials=None, ground=False):
    """A PSK as the submesh dicts sdk_mesh.write_metamesh takes, one per material used.
    scale: file units to metres. rotation: an optional 3 x 3 matrix applied after the change of handedness.
    materials: {psk material name or index: Bannerlord material name}; the psk names are used otherwise.
    ground=True moves the mesh so its lowest point is at z = 0 and its footprint is centred on the origin."""
    m = read(path)
    P = m["points"] * np.array([1.0, -1.0, 1.0]) * scale                      # left-handed to right-handed
    if rotation is not None: P = P @ np.asarray(rotation, np.float64).T
    if ground: P = P - np.array([(P[:, 0].min() + P[:, 0].max()) / 2, (P[:, 1].min() + P[:, 1].max()) / 2, P[:, 2].min()])
    P = P + np.asarray(offset, np.float64)
    F = m["faces"]                         # the mirror alone turns Unreal's clockwise fronts into the counter-clockwise ones the game wants
    wp = m["wedge_point"]; tri = P[wp[F]]
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])                # area-weighted face normals
    # a normal per (point, smoothing group): the same point seen from two groups is a hard edge
    groups = np.where(m["smooth"] == 0, -1 - np.arange(len(F)), m["smooth"])   # group 0 = faceted
    key = {}; acc = []
    corner_n = np.zeros((len(F), 3), np.int64)
    for f in range(len(F)):
        for j in range(3):
            k = (int(wp[F[f, j]]), int(groups[f]))
            i = key.get(k)
            if i is None: i = key[k] = len(acc); acc.append(np.zeros(3))
            acc[i] += fn[f]; corner_n[f, j] = i
    acc = np.array(acc); ln = np.linalg.norm(acc, axis=1, keepdims=True); N_all = np.where(ln > 1e-12, acc / np.maximum(ln, 1e-20), [0.0, 0.0, 1.0])
    uv = m["uv"]; uv1 = m["extra_uv"][0] if m["extra_uv"] else None
    names = m["materials"]; out = []
    for slot in sorted(set(m["face_mat"].tolist())):
        sel = np.nonzero(m["face_mat"] == slot)[0]
        used = sorted(set(wp[F[sel]].reshape(-1).tolist())); local = {p: i for i, p in enumerate(used)}
        vkey = {}; vp = []; vn = []; vu = []; vu1 = []; faces = []
        for f in sel:
            ids = []
            for j in range(3):
                wdg = int(F[f, j]); k = (local[int(wp[wdg])], int(corner_n[f, j]), float(uv[wdg, 0]), float(uv[wdg, 1]))
                i = vkey.get(k)
                if i is None:
                    i = vkey[k] = len(vp); vp.append(k[0]); vn.append(N_all[corner_n[f, j]]); vu.append((uv[wdg, 0], 1.0 - uv[wdg, 1]))
                    vu1.append((uv1[wdg, 0], 1.0 - uv1[wdg, 1]) if uv1 is not None else (0.0, 0.0))
                ids.append(i)
            faces.append(ids)
        Pl = P[used]; pi = np.array(vp, np.uint32); Nl = np.array(vn); UV = np.array(vu); Fl = np.array(faces, np.uint32)
        T, B = sdk_mesh.tangents(Pl, pi, Nl, UV, Fl)
        src = names[slot] if slot < len(names) else "material_%d" % slot
        mat = (materials or {}).get(src, (materials or {}).get(slot, src))
        n_used = len(set(m["face_mat"].tolist()))
        out.append(dict(name=name if n_used == 1 else "%s.%d" % (name, len(out)), material=mat, P=Pl, pi=pi, N=Nl, nbones=0, T=T, B=B,
                        uv0=UV, uv1=np.array(vu1), c0=np.full(len(pi), 0xffffffff, np.uint32), F=Fl, W=None))
    return out


def bounds(path, scale=0.01):
    P = read(path)["points"] * np.array([1.0, -1.0, 1.0]) * scale
    return P.min(0), P.max(0)


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "info":
        for f in a[1:]:
            m = read(f); lo, hi = bounds(f)
            print("%s: %d points, %d triangles, materials %s, size %.2f x %.2f x %.2f m" % (os.path.basename(f), len(m["points"]), len(m["faces"]), m["materials"], *(hi - lo)))
    else: print(__doc__)
