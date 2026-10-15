"""flora.bin of a scene: the placed plants and trees (on the campaign map: every tree of the map). Read, write, round trip.

    python sdk/flora.py info <scene folder or flora.bin>     instance count by kind, extent
    python sdk/flora.py check                                 decode and re-encode every shipped flora.bin, compare byte for byte

Layout (game v1.4.8, all 722 shipped files): "FLR2", u32 size of the rest, u32 count, then per instance: u32 name length +
name (a flora kind of the flora kinds XML, for example map_pine_c), u32 (a small number, 0 for 57 % of the 2.77 million
shipped instances and up to about 8 for the rest: read as the variant of the kind, not verified), a 4 x 4 transform in rows
(three axis rows with the scale in them, then the position row x, y, z, 1), and four f32 (a colour factor r, g, b between 0
and 1, then 1). Both are kept as read. A scene with no placed flora has an empty list (12 bytes).
"""
import glob, os, struct, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
else:
    from . import config

MAGIC = b"FLR2"
REC = np.dtype([("u0", "<u4"), ("m", "<f4", (4, 4)), ("tail", "<f4", 4)])


def parse(b):
    """dict(names=[str], rec=REC array): the instances in file order."""
    if b[:4] != MAGIC: raise ValueError("not a flora.bin")
    size, n = struct.unpack_from("<II", b, 4)
    if size != len(b) - 8: raise ValueError("flora.bin size field %d, file %d" % (size, len(b)))
    o = 12; names = []; rec = np.zeros(n, REC)
    for k in range(n):
        L, = struct.unpack_from("<I", b, o); names.append(b[o + 4:o + 4 + L].decode("utf-8", "surrogateescape")); o += 4 + L
        rec[k] = np.frombuffer(b, REC, 1, o)[0]; o += REC.itemsize
    if o != len(b): raise ValueError("flora.bin: %d bytes left after the last instance" % (len(b) - o))
    return dict(names=names, rec=rec)


def pack(d):
    body = b"".join(struct.pack("<I", len(n.encode("utf-8", "surrogateescape"))) + n.encode("utf-8", "surrogateescape") + r.tobytes()
                    for n, r in zip(d["names"], d["rec"]))
    return MAGIC + struct.pack("<II", len(body) + 4, len(d["names"])) + body


def read(path):
    if os.path.isdir(path): path = os.path.join(path, "flora.bin")
    with open(path, "rb") as f: return parse(f.read())


def write(path, d):
    if os.path.isdir(path): path = os.path.join(path, "flora.bin")
    with open(path, "wb") as f: f.write(pack(d))


def positions(d):
    """(n, 3) positions of the instances."""
    return d["rec"]["m"][:, 3, :3].astype(np.float64)


def instance(name, x, y, z, yaw_deg=0.0, scale=1.0):
    """One new instance: upright, turned by yaw around z, uniform scale."""
    c, s = np.cos(np.radians(yaw_deg)), np.sin(np.radians(yaw_deg))
    r = np.zeros(1, REC)
    r["m"][0] = [[c * scale, s * scale, 0, 0], [-s * scale, c * scale, 0, 0], [0, 0, scale, 0], [x, y, z, 1]]
    r["tail"][0] = 1.0
    return name, r[0]


def shipped():
    return sorted(glob.glob(os.path.join(config.MODULES, "*", "SceneObj", "*", "flora.bin")))


def check(paths=None, log=print):
    paths = paths or shipped(); same = 0; kinds = set(); n = 0
    for p in paths:
        b = open(p, "rb").read(); d = parse(b)
        same += pack(d) == b; n += len(d["names"]); kinds.update(d["names"])
    log("%d of %d flora.bin files re-encode byte-identical; %d instances of %d kinds" % (same, len(paths), n, len(kinds)))
    return same == len(paths)


def main(argv):
    if argv[:1] == ["check"]: return 0 if check() else 1
    if argv[:1] == ["info"] and len(argv) == 2:
        d = read(argv[1]); P = positions(d); kinds = {}
        for nm in d["names"]: kinds[nm] = kinds.get(nm, 0) + 1
        print("%d instances" % len(d["names"]) + (", x %.1f..%.1f y %.1f..%.1f z %.1f..%.1f" % (P[:, 0].min(), P[:, 0].max(), P[:, 1].min(), P[:, 1].max(), P[:, 2].min(), P[:, 2].max()) if len(P) else ""))
        for k, v in sorted(kinds.items(), key=lambda x: -x[1]): print("  %6d %s" % (v, k))
        return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
