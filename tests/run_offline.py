"""Tests that need no game install: codecs, file writers on made-up data, the command line, the MCP server, the scaffold.

    python tests/run_offline.py            exit code 0 when every test passes

These run on GitHub for every push. Everything that compares against the game's own files is in sdk/selftest.py and
`bl compat`, which need an install. No test here reads a game file and no fixture holds game content: every input is
made up in the test. The one exception is the sound test that runs the whole route (sound_build_with_fmod_studio): it
reads the game's FMOD sample project and needs FMOD Studio 2.02.27, and it skips itself when either is missing, as on GitHub.
"""
import json, os, shutil, struct, subprocess, sys, tempfile, traceback
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK = os.path.join(ROOT, "sdk")
sys.path.insert(0, SDK)
# A game folder that does not exist, and a private working root: the tests must not depend on, or touch, a real install.
# (REAL_GAME is the install the environment names, for the one test that skips itself without it.)
_STEAM = r"C:\Program Files (x86)\Steam\steamapps\common\Mount & Blade II Bannerlord"
REAL_GAME = os.environ.get("BANNERLORD_DIR") or (_STEAM if os.path.isdir(_STEAM) else None)
TMP = tempfile.mkdtemp(prefix="bl_tests_")
os.environ["BANNERLORD_DIR"] = os.path.join(TMP, "no_game")
os.environ["BANNERLORD_SDK_HOME"] = os.path.join(TMP, "home")
for k in ("BANNERLORD_MODULE_DIR", "SDK_STAGE_DIR", "BL_PROJECT", "MODULE_ASSET_SUB"): os.environ.pop(k, None)

import numpy as np

TESTS = []
def test(f): TESTS.append(f); return f


def bl_run(*argv, cwd=None):
    r = subprocess.run([sys.executable, os.path.join(SDK, "bl.py")] + list(argv), capture_output=True, text=True, cwd=cwd, encoding="utf-8")
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 1, "bl must print exactly one line of JSON, got %d: %r %r" % (len(lines), r.stdout[:300], r.stderr[:300])
    out = json.loads(lines[0]); assert (r.returncode == 0) == out["ok"], (r.returncode, out)
    return out


XSCENE = ('<?xml version="1.0" encoding="utf-8"?>\n<scene name="made_up" version="1">\n\t<entities>\n\t\t<game_entity name="a" prefab="thing_a">\n'
          '\t\t\t<transform position="1.000, 2.000, 3.000" rotation_euler="0.000, 0.000, 1.000"/>\n\t\t</game_entity>\n\t</entities>\n</scene>\n')


@test
def lz4_round_trip():
    import tpac
    rng = np.random.default_rng(1)
    for data in (b"", b"a", b"abcabcabcabc" * 500, rng.integers(0, 4, 50000, dtype=np.uint8).tobytes(), rng.integers(0, 256, 9000, dtype=np.uint8).tobytes()):
        assert tpac.lz4_decompress(tpac.lz4_compress(data), len(data)) == data


@test
def xxh64_known_values():
    import tpac
    # the reference values of the xxHash specification
    assert tpac.xxh64(b"") == 0xEF46DB3751D8E999
    assert tpac.xxh64(b"abc") == 0x44BC2CF5AD770999


@test
def png_codec_round_trip():
    import terrain
    rng = np.random.default_rng(2)
    for a in (rng.integers(0, 256, (33, 47), dtype=np.uint8), rng.integers(0, 65536, (40, 40), dtype=np.uint16), rng.integers(0, 256, (16, 20, 4), dtype=np.uint8)):
        b = terrain.png_decode(terrain.png_encode(a)); assert b.dtype == a.dtype and np.array_equal(a, b)


def _bc_test_blocks(n, alpha, seed):
    """n 4x4 RGBA blocks (8 bit values) that suit different modes: a colour line, two colour regions split by an edge, noise."""
    r = np.random.default_rng(seed); t = r.random((n, 16, 1)); a, b, c = (r.random((n, 1, 4)) * 255 for _ in range(3))
    x = a + (b - a) * t
    split = np.broadcast_to((np.arange(16) % 4 < 2)[None, :, None], (n, 16, 1)) & (np.arange(n) % 3 == 1)[:, None, None]
    x = np.where(split, c + (r.random((n, 16, 4)) - 0.5) * 24, x)
    x[:n // 6] = r.random((n // 6, 16, 4)) * 255
    x = np.rint(np.clip(x, 0, 255)).astype(np.float32)
    if not alpha: x[..., 3] = 255
    return x


@test
def bc7_encoder_modes_decode_to_what_they_report():
    import texcodec
    for mode, alpha in ((0, False), (1, False), (2, False), (3, False), (4, True), (5, True), (6, True), (7, True)):
        x = _bc_test_blocks(120, alpha, mode)
        blk, err = texcodec._bc7_rotated(x, mode, 1) if mode in (4, 5) else texcodec._bc7_subsets(x, mode, 1, k=2)
        dec = texcodec.decode_bc7(blk).astype(np.float64)
        first = blk[:, 0].astype(int); got = np.array([next(k for k in range(8) if f >> k & 1) for f in first])
        assert (got == mode).all(), (mode, np.bincount(got))
        real = ((dec - x) ** 2).sum((1, 2))
        assert np.abs(real - err).max() < 0.5, (mode, np.abs(real - err).max())   # the error the encoder minimised is the error the decoder shows
        assert (np.sqrt(real.mean() / 64) < 40), (mode, real.mean())


@test
def bc7_qualities_never_lose_to_fast_and_pick_modes():
    import texcodec, tpac
    h, w = 64, 64; y, xx = np.mgrid[0:h, 0:w]; r = np.random.default_rng(3)
    img = np.zeros((h, w, 4), np.uint8); img[..., 0] = xx * 4; img[..., 1] = y * 4; img[..., 2] = (xx ^ y) * 4 % 256; img[..., 3] = 255
    img[:32, 32:, :3] = (200, 40, 90); img[:32, 32:, 3] = np.clip(xx[:32, 32:] * 8 % 256, 0, 255)   # a ramp of alpha over a flat colour
    img[..., :3] = np.clip(img[..., :3].astype(int) + r.integers(-4, 5, (h, w, 3)), 0, 255)
    psnr = {}; used = {}
    for q in ("fast", "normal", "best"):
        enc = tpac.encode_mip(img, "BC7", q); assert len(enc) == 16 * 16 * 16
        d = tpac.decode_blocks(enc, w, h, "BC7").astype(np.float64); mse = ((d - img) ** 2).mean(); psnr[q] = 10 * np.log10(255 ** 2 / mse)
        used[q] = {k for k in range(8) if ((np.frombuffer(enc, np.uint8).reshape(-1, 16)[:, 0] & (0xFF >> (7 - k))) == 1 << k).any()}
    assert psnr["normal"] > psnr["fast"] + 0.5 and psnr["best"] >= psnr["normal"], psnr
    assert used["fast"] == {6} and len(used["normal"]) >= 3 and len(used["best"]) >= 5, used   # fast: mode 6 only; the others pick per block


@test
def bc6h_encoder_modes_decode_to_what_they_report():
    import texcodec
    r = np.random.default_rng(5); n = 90
    for m in range(14):
        t = r.random((n, 16, 1)); a, b = np.exp(r.normal(-1, 1.2, (n, 1, 3))), np.exp(r.normal(-1, 1.2, (n, 1, 3)))
        x = a + (b - a) * t
        x[n // 3:2 * n // 3] = a[n // 3:2 * n // 3] * (1 + 0.02 * r.standard_normal((n // 3, 16, 3)))   # nearly flat blocks: the delta modes can hold these
        x, tt = texcodec._bc6h_domain(x.astype(np.float32))
        blk, err = texcodec._bc6h_candidates(x, tt, m, 1, k=2)
        assert (texcodec.bc6h_modes(blk) == m + 1).all(), (m, np.bincount(texcodec.bc6h_modes(blk)))
        real = ((np.sqrt(texcodec.decode_bc6h(blk).astype(np.float64)) - np.sqrt(x)) ** 2).sum((1, 2))
        assert (np.abs(real - err) <= 1e-3 * real + 1e-6).all(), (m, np.abs(real - err).max())   # a delta out of its field would show here


@test
def bc6h_qualities_never_lose_to_fast():
    import tpac
    h, w = 64, 64; y, x = np.mgrid[0:h, 0:w]; r = np.random.default_rng(6)
    sky = np.zeros((h, w, 4), np.float32); sky[..., 0] = 0.2 + y / h; sky[..., 1] = 0.4 + 0.5 * y / h; sky[..., 2] = 1.2 - 0.3 * x / w; sky[..., 3] = 1
    sky[..., :3] *= 1 + 0.01 * r.standard_normal((h, w, 3)); sky[40:50, 20:30, :3] *= 30
    psnr = {}
    for q in ("fast", "normal", "best"):
        enc = tpac.encode_mip(sky, "BC6H_UF16", q); d = tpac.decode_float(enc, w, h, "BC6H_UF16")[..., :3]
        ref = sky[..., :3]; mse = ((d / (1 + d) - ref / (1 + ref)) ** 2).mean(); psnr[q] = 10 * np.log10(1 / mse)
    assert psnr["normal"] > psnr["fast"] + 0.3 and psnr["best"] >= psnr["normal"] - 0.01, psnr


@test
def terrain_nodes_split_and_join():
    import terrain
    full = np.random.default_rng(3).integers(0, 65536, (2 * 256 + 1, 3 * 256 + 1), dtype=np.uint16)
    nodes = terrain.split_nodes(full, 3, 2); assert len(nodes) == 6 and nodes[0].shape == (terrain.NODE, terrain.NODE)
    assert np.array_equal(terrain.join_nodes(nodes, 3, 2), full)


@test
def navmesh_write_and_read():
    import navmesh
    V = np.array([[0, 0, 0], [4, 0, 0], [4, 4, 1], [0, 4, 1], [8, 0, 0], [8, 4, 2]], np.float64)
    nm = navmesh.NavMesh.from_polygons(V, [[0, 1, 2, 3], [1, 4, 5, 2]])
    p = os.path.join(TMP, "navmesh.bin"); nm.save(p)
    back = navmesh.NavMesh(p); st = back.stats()
    assert st["faces"] == 2 and st["vertices"] == 6 and abs(st["area_m2"] - 32.0) < 1e-6, st
    assert back.payload() == nm.payload() and navmesh.check(p)


def _nav_terrain(n=481, cell=0.5):
    """Made-up ground, 240 m square: a plateau, a ramp, rolling hills, a pit, a round walkable area with a block cut out of it,
    and two marked regions. Returns heights, cell, walkable mask, regions {id: mask}."""
    yy, xx = np.mgrid[0:n, 0:n] * cell
    z = np.where(xx < 80, 12.0, np.where(xx < 120, 12.0 + (xx - 80) * 0.3, 24.0 + 1.5 * np.sin(xx / 13.0) * np.cos(yy / 17.0)))
    z = z - 8.0 * np.exp(-(((xx - 180) ** 2 + (yy - 60) ** 2) / 40.0))
    walk = (np.hypot(xx - 120, yy - 120) < 112) & ~((xx > 60) & (xx < 80) & (yy > 100) & (yy < 140))
    return z, cell, walk, {7: xx < 40, 9: (xx > 150) & (xx < 170) & (yy > 150) & (yy < 190)}


def _nav_cover(nm, keep, k, cell):
    """(face index, number of faces) per mesh cell, found by testing one point inside each cell against every face."""
    gy, gx = keep.shape; step = k * cell; cnt = np.zeros((gy, gx), int); who = -np.ones((gy, gx), int); V = nm.vertices.astype(np.float64)
    for n, (vs, _) in enumerate(nm.faces):
        P = V[list(vs)][:, :2]
        j0 = max(0, int(P[:, 1].min() // step)); j1 = min(gy - 1, int(P[:, 1].max() // step)); i0 = max(0, int(P[:, 0].min() // step)); i1 = min(gx - 1, int(P[:, 0].max() // step))
        J, I = np.mgrid[j0:j1 + 1, i0:i1 + 1]; X = (I + 0.37) * step; Y = (J + 0.41) * step; inside = np.ones(X.shape, bool)
        for a in range(len(P)):
            b = (a + 1) % len(P); inside &= (P[b, 0] - P[a, 0]) * (Y - P[a, 1]) - (P[b, 1] - P[a, 1]) * (X - P[a, 0]) > 0
        cnt[j0:j1 + 1, i0:i1 + 1] += inside; who[j0:j1 + 1, i0:i1 + 1][inside] = n
    return who, cnt


@test
def navmesh_polygons_cover_the_grid_area_with_far_fewer_faces():
    import navmesh, scenebuild
    z, cell, walk, regions = _nav_terrain()
    g, keep = scenebuild.grid_navmesh(z, cell, walk)
    p, keep2 = scenebuild.polygon_navmesh(z, cell, walk, height_tol=0.3)
    assert np.array_equal(keep, keep2) and keep.sum() > 1500
    # the same ground: equal area, and a point in every cell is in exactly one face and none outside the cells is in any
    assert abs(p.stats()["area_m2"] - g.stats()["area_m2"]) < 1e-6 * g.stats()["area_m2"], (p.stats(), g.stats())
    who, cnt = _nav_cover(p, keep, 8, cell); assert np.array_equal(cnt == 1, keep) and cnt.max() == 1
    # the grid has one face per cell; the polygons at least three times fewer here
    assert len(g.faces) == keep.sum() and len(p.faces) * 3 <= len(g.faces), (len(p.faces), len(g.faces))
    assert all(len(f[0]) in (3, 4) for f in p.faces)
    # convex and not degenerate, edges shared by whole, consistent adjacency, one connected area
    assert p.problems() == {"not_convex": 0, "bad_edges": 0, "edge_over_two": 0, "edge_same_way": 0, "t_junctions": 0, "duplicate_vertices": 0, "unused_vertices": 0}, p.problems()
    assert p.components() == 1 and g.components() == 1
    # the ground is followed: no worse than the grid where the grid is bad, else within the tolerance and a half
    eg, ep = g.height_error(z, cell), p.height_error(z, cell)
    assert ep["p99"] <= 0.45 and ep["max"] <= max(eg["max"], 0.45) + 1e-9 and ep["mean"] < 0.15, (eg, ep)
    # written and read again byte for byte
    f = os.path.join(TMP, "poly_navmesh.bin"); p.save(f); back = navmesh.NavMesh(f)
    assert navmesh.check(f) and back.payload() == p.payload() and back.stats() == p.stats() and back.problems() == p.problems()


@test
def navmesh_polygons_keep_regions_apart_and_set_their_group_and_flags():
    import scenebuild
    z, cell, walk, regions = _nav_terrain()
    p, keep = scenebuild.polygon_navmesh(z, cell, walk, regions=regions, region_flags={9: 0x12})
    who, cnt = _nav_cover(p, keep, 8, cell)
    ids = np.zeros(z.shape, int)
    for gid, m in regions.items(): ids[m] = gid
    mid = ids[4:keep.shape[0] * 8:8, 4:keep.shape[1] * 8:8]
    assert np.array_equal(np.array(p.face_group)[who[keep]], mid[keep]), "a face spans two regions or carries the wrong id"
    assert sorted(set(p.face_group)) == [0, 7, 9] and {p.face_flags[n] for n, g in enumerate(p.face_group) if g == 9} == {0x12}
    assert {p.face_flags[n] for n, g in enumerate(p.face_group) if g != 9} == {0xFFFFFFFF}
    assert p.problems()["t_junctions"] == 0 and p.components() == 1
    q, _ = scenebuild.polygon_navmesh(z, cell, walk, regions=ids); assert q.face_group == p.face_group, "an id array and a dict of masks mean the same"
    # faces are cut at the regions' borders: more than without regions, never more than one per cell
    assert len(scenebuild.polygon_navmesh(z, cell, walk)[0].faces) < len(p.faces) <= keep.sum()


@test
def navmesh_polygons_edge_cases():
    import navmesh, scenebuild
    # flat ground: faces as large as allowed and no larger
    cell = 1.0; n = 257; z = np.full((n, n), 5.0); walk = np.ones((n, n), bool)
    p, keep = scenebuild.polygon_navmesh(z, cell, walk, step=4.0, max_size=32.0)
    side = max(np.ptp(p.vertices[list(f[0])][:, :2], axis=0).max() for f in p.faces)
    assert len(p.faces) == 64 and abs(side - 32.0) < 1e-6 and p.problems()["t_junctions"] == 0, (len(p.faces), side)
    p2, _ = scenebuild.polygon_navmesh(z, cell, walk, step=4.0, max_size=8.0); assert len(p2.faces) == 32 * 32
    # one cell, and no ground at all
    one, _ = scenebuild.polygon_navmesh(np.zeros((9, 9)), 1.0, np.ones((9, 9), bool), step=8.0); assert len(one.faces) == 1 and not any(one.problems().values())
    none, kn = scenebuild.polygon_navmesh(np.zeros((9, 9)), 1.0, np.zeros((9, 9), bool)); assert len(none.faces) == 0 and not kn.any()
    # ground that is rough everywhere stays at the cell size
    r = np.random.default_rng(5).normal(0, 1.5, (129, 129)); rp, rk = scenebuild.polygon_navmesh(r, 1.0, np.ones((129, 129), bool), max_slope=99.0)
    assert len(rp.faces) == rk.sum() and rp.problems()["t_junctions"] == 0
    try: scenebuild.build(None, os.path.join(TMP, "x"), "x", np.zeros((2, 2)), navmesh="blobs")
    except ValueError as e: assert "polygons" in str(e)
    else: raise AssertionError("an unknown navmesh kind was accepted")
    out = bl_run("scene.from_heightmap", "nothing.png", "x", "--navmesh", "blobs"); assert not out["ok"] and "navmesh" in json.dumps(out), out


@test
def navmesh_convex_pieces_and_the_geometry_checker():
    import navmesh
    # a square with a vertex in the middle of one side, and with one in the middle of every side: cut into pieces of 3 or 4 corners
    # that use those vertices, so that the faces next door meet them along whole edges
    for pts in ([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)], [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (1, 2), (0, 2), (0, 1)]):
        pcs = navmesh.convex_pieces(pts); assert all(3 <= len(q) <= 4 for q in pcs), pcs
        area = sum(abs(sum(pts[q[i]][0] * pts[q[(i + 1) % len(q)]][1] - pts[q[(i + 1) % len(q)]][0] * pts[q[i]][1] for i in range(len(q)))) / 2 for q in pcs)
        assert abs(area - 4.0) < 1e-9, area
        sides = {frozenset((q[i], q[(i + 1) % len(q)])) for q in pcs for i in range(len(q))}
        assert all(frozenset((i, (i + 1) % len(pts))) in sides for i in range(len(pts))), "every side of the outline must be a side of a piece"
    assert len(navmesh.convex_pieces([(0, 0), (1, 0), (2, 0), (2, 2), (0, 2)])) == 2          # a triangle and a quad
    V = np.array([[0, 0, 0], [4, 0, 0], [8, 0, 0], [8, 4, 0], [4, 4, 0], [0, 4, 0], [4, 8, 0], [0, 8, 0]], np.float64)
    good = navmesh.NavMesh.from_polygons(V, [(0, 1, 4, 5), (1, 2, 3, 4), (5, 4, 6, 7)]); assert not any(good.problems().values()) and good.components() == 1
    # a face with a corner (vertex 4) on its side, with the face above ending there: the faces are not joined along the whole edge
    tj = navmesh.NavMesh.from_polygons(V, [(0, 2, 3, 5), (5, 4, 6, 7)]); assert tj.problems()["t_junctions"] >= 1, tj.problems()
    assert navmesh.NavMesh.from_polygons(V, [(0, 5, 4, 1)]).problems()["not_convex"] == 1                      # clockwise
    assert navmesh.NavMesh.from_polygons(V, [(0, 1, 4, 5), (1, 4, 3, 2)]).problems()["edge_same_way"] == 1     # the second face is turned the wrong way
    assert navmesh.NavMesh.from_polygons(V, [(0, 1, 4, 5), (2, 3, 6, 7)]).components() == 2


@test
def scene_file_edits_keep_every_other_byte():
    import scene
    doc = scene.loads(XSCENE.encode("utf-8")); assert doc.tobytes() == XSCENE.encode("utf-8")
    assert [e.prefab for e in doc.entities()] == ["thing_a"] and doc.entities()[0].position == (1.0, 2.0, 3.0)
    doc.add_entity(prefab="thing_b", position=(5.0, 6.0, 7.0)); assert len(doc.entities()) == 2
    doc.remove_entity(doc.entities()[1]); assert doc.tobytes() == XSCENE.encode("utf-8")


@test
def preview_of_a_cube_and_an_open_box():
    import preview
    P = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)], np.float64)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]       # outward facing
    F = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])
    st = preview.mesh_stats(P, F); assert st["closed"] and st["triangles"] == 12 and abs(st["signed_volume_m3"] - 1.0) < 1e-9, st
    for _, yaw, pitch in preview.VIEWS:
        img = preview.render(P, F, 96, yaw, pitch)
        back = np.all(np.abs(img / np.maximum(img.max(axis=2, keepdims=True), 1e-9) - preview.BACK / preview.BACK.max()) < 0.02, axis=2)
        assert (img != preview.BG).any() and not back.any(), "a closed mesh shows no face from behind"
    st = preview.mesh_stats(P, F[2:]); assert not st["closed"] and st["open_edges"] == 4, st             # one side removed
    out = os.path.join(TMP, "cube.png"); preview.mesh_sheet(P, F, out, 64, "cube"); assert os.path.getsize(out) > 200


@test
def heightmap_images_load():
    import heightmap, terrain
    a = (np.linspace(0, 65535, 20 * 30).reshape(20, 30)).astype(np.uint16)
    p = os.path.join(TMP, "h.png"); open(p, "wb").write(terrain.png_encode(a))
    z, absolute = heightmap.load(p); assert not absolute and z.shape == (20, 30) and abs(z.max() - 1.0) < 1e-9 and z.min() == 0.0
    np.save(os.path.join(TMP, "h.npy"), np.full((5, 5), 12.5)); z, absolute = heightmap.load(os.path.join(TMP, "h.npy")); assert absolute and z[0, 0] == 12.5


@test
def scaffold_makes_a_complete_project():
    import scaffold
    fs = scaffold.files("MyMod", "My Mod")
    assert {"bl.json", "SubModule.xml", "src/MyMod.csproj", "src/SubModule.cs", "src/AutoTest.cs", "src/SceneSlots.cs", "ModuleData/mymod_custom_battle_scenes.xml"} <= set(fs)
    for rel, text in fs.items(): assert "__MOD" not in text and "__NAME__" not in text and "__VERSION__" not in text, rel
    assert 'Id value="MyMod"' in fs["SubModule.xml"] and "namespace MyMod" in fs["src/SubModule.cs"]
    for bad in ("1abc", "a", "has space", "x" * 60):
        try: scaffold.files(bad)
        except ValueError: pass
        else: raise AssertionError("accepted the id %r" % bad)
    d = os.path.join(TMP, "proj"); scaffold.create(d, "MyMod")
    assert scaffold.find_project(os.path.join(d, "src"))[0] == d
    assert scaffold.register_scene(d, "MyMod", "map_a", "Map A") == ["map_a"]
    assert scaffold.register_scene(d, "MyMod", "map_b") == ["map_a", "map_b"]
    assert scaffold.register_scene(d, "MyMod", "map_a") == ["map_b", "map_a"]                           # listed once, moved to the end
    assert scaffold.register_scene(d, "MyMod", "map_b", remove=True) == ["map_a"]
    assert scaffold.add_prefab(d, "MyMod", "crate", mesh="crate_mesh", physics="crate_body") == ["crate"]
    assert scaffold.add_prefab(d, "MyMod", "wall", physics="wall_body", scale=(2, 1, 1)) == ["crate", "wall"]
    assert scaffold.add_prefab(d, "MyMod", "crate", mesh="crate_mesh_2") == ["wall", "crate"]
    xml = open(os.path.join(d, "Prefabs", "mymod_prefabs.xml"), encoding="utf-8").read()
    import xml.dom.minidom as minidom; minidom.parseString(xml)
    assert '<physics shape="wall_body"/>' in xml and 'scale="2.000, 1.000, 1.000"' in xml and "crate_mesh_2" in xml and "crate_body" not in xml
    assert scaffold.add_prefab(d, "MyMod", "wall", remove=True) == ["crate"]


@test
def scene_slots_life_cycle():
    import slots
    mod = os.path.join(TMP, "Module"); tpl = os.path.join(TMP, "tpl"); src = os.path.join(TMP, "src_scene")
    for d in (tpl, src): os.makedirs(d)
    for fn in slots.KEPT: open(os.path.join(tpl, fn), "wb").write(b"kept")
    open(os.path.join(src, "scene.xscene"), "w", encoding="utf-8", newline="").write(XSCENE)
    for fn in ("terrain.bin", "navmesh.bin"): open(os.path.join(src, fn), "wb").write(b"\x01\x02\x03")
    assert slots.create(mod, "s", 3, tpl) == ["s_1", "s_2", "s_3"] and slots.create(mod, "s", 3, tpl) == []
    assert slots.create(mod, "s", 4, tpl) == ["s_4"] and slots.state(mod, "s") == {"s_%d" % n: "empty" for n in (1, 2, 3, 4)}
    slots.fill(mod, "s_2", src)
    assert slots.state(mod, "s")["s_2"] == "filled" and b'name="s_2"' in open(os.path.join(slots.folder(mod, "s_2"), "scene.xscene"), "rb").read()
    assert slots.reset(mod, "s") == 1 and slots.state(mod, "s")["s_2"] == "empty"
    try: slots.fill(mod, "nope", src)
    except ValueError: pass
    else: raise AssertionError("filled a folder that is not a slot")


@test
def bl_answers_with_one_json_object():
    out = bl_run("commands"); cmds = out["result"]["commands"]
    assert len(cmds) >= 30 and len({c["name"] for c in cmds}) == len(cmds)
    for c in cmds:
        s = c["arguments"]; assert s["type"] == "object" and set(s["required"]) <= set(s["properties"]), c["name"]
        if c["writes"]: assert "dry" in s["properties"], "%s changes files and has no dry run" % c["name"]
    assert bl_run("doctor")["result"]["game_found"] is False
    bad = bl_run("no_such_command"); assert not bad["ok"] and "hint" in bad
    bad = bl_run("scene.info"); assert not bad["ok"] and bad["command"] == "scene.info"
    bad = bl_run("mesh.preview", "x", "--size", "big", "--out", "o.png"); assert not bad["ok"] and "whole number" in bad["error"]
    bad = bl_run("run", "--words", "quit", "--module_name", "X"); assert not bad["ok"] and "--launch" in bad["error"]      # never starts the game by accident


@test
def bl_new_then_register_in_a_project():
    d = os.path.join(TMP, "work"); os.makedirs(d)
    dry = bl_run("new", "DryMod", "--dry", cwd=d); assert dry["ok"] and not os.path.exists(os.path.join(d, "DryMod"))
    out = bl_run("new", "Demo", "--name", "A Demo", cwd=d); assert out["ok"] and os.path.isfile(os.path.join(d, "Demo", "bl.json"))
    again = bl_run("new", "Demo", cwd=d); assert not again["ok"]
    reg = bl_run("scene", "register", "field_1", "--name", "Field one", cwd=os.path.join(d, "Demo", "src")); assert reg["result"]["listed"] == ["field_1"]
    doc = bl_run("doctor", cwd=os.path.join(d, "Demo")); assert doc["result"]["project_module"] == "Demo" and doc["result"]["module"].endswith(os.path.join("Modules", "Demo"))
    # the same values in one piece, as the MCP server passes them
    reg = bl_run("scene.register", "--args", json.dumps({"scene_id": "field_1", "remove": True}), cwd=os.path.join(d, "Demo")); assert reg["result"]["listed"] == []


@test
def mcp_server_speaks_the_protocol():
    import bl, mcp_server
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert init["result"]["capabilities"]["tools"] is not None and init["result"]["serverInfo"]["name"]
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert len(tools) == len(bl.COMMANDS)
    import re
    for t in tools: assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", t["name"]) and t["inputSchema"]["type"] == "object", t["name"]
    call = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "commands", "arguments": {}}})["result"]
    assert call["isError"] is False and json.loads(call["content"][0]["text"])["ok"]
    call = mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "scene_info", "arguments": {"scene": "nowhere"}}})["result"]
    assert call["isError"] is True and json.loads(call["content"][0]["text"])["ok"] is False
    assert "error" in mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "no/such"})
    # and over real pipes
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
    r = subprocess.run([sys.executable, os.path.join(SDK, "mcp_server.py")], input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, encoding="utf-8")
    answers = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]; assert [a["id"] for a in answers] == [1, 2], r.stdout[:300]


@test
def bridge_request_and_answer():
    import bridge, threading, time
    mod = os.path.join(TMP, "BridgeModule"); assert bridge.enable(mod) and os.path.isfile(os.path.join(mod, "bridge", "on"))
    seen = {}
    def game():                                                    # what Bridge.cs does: take the request, write the answer
        box = os.path.join(mod, "bridge", "in"); end = time.time() + 5
        while time.time() < end:
            for fn in os.listdir(box):
                if not fn.endswith(".txt"): continue
                lines = open(os.path.join(box, fn), encoding="utf-8").read().splitlines(); os.remove(os.path.join(box, fn))
                seen["cmd"] = lines[0]; seen["args"] = dict(l.split("=", 1) for l in lines[1:]); rid = fn[:-4]
                ok = lines[0] != "spawn"
                body = {"id": rid, "ok": True, "result": {"screen": "MissionScreen"}} if ok else {"id": rid, "ok": False, "error": "no prefab named x"}
                with open(os.path.join(mod, "bridge", "out", rid + ".tmp"), "w", encoding="utf-8") as f: json.dump(body, f)
                os.replace(os.path.join(mod, "bridge", "out", rid + ".tmp"), os.path.join(mod, "bridge", "out", rid + ".json"))
                return
            time.sleep(0.02)
    t = threading.Thread(target=game); t.start()
    assert bridge.send(mod, "teleport", {"x": 600, "y": 640.5}, timeout=5, poll=0.02) == {"screen": "MissionScreen"}; t.join()
    assert seen == {"cmd": "teleport", "args": {"x": "600", "y": "640.5"}}
    t = threading.Thread(target=game); t.start()
    try: bridge.send(mod, "spawn", {"prefab": "x", "x": 1, "y": 2}, timeout=5, poll=0.02)
    except RuntimeError as e: assert "no prefab" in str(e)
    else: raise AssertionError("a refusal did not raise")
    t.join()
    try: bridge.send(mod, "ping", timeout=0.3, poll=0.05)
    except TimeoutError: pass
    else: raise AssertionError("no answer did not time out")
    assert os.listdir(os.path.join(mod, "bridge", "in")) == [], "an unanswered request was left behind"
    assert bridge.enable(mod, False) is False


def _sprite_png(path, w, h, color, rgba=True):
    import terrain
    a = np.zeros((h, w, 4), np.uint8); a[..., :3] = color; a[..., 3] = 255
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f: f.write(terrain.png_encode(a if rgba else a[..., 0]))


def _fake_generator(container, module_id):
    """What the game's generator does, in the simplest way: one row of sprites per category on a sheet, the sheet written to
    <container>/AssetSources/GauntletUI, the SpriteData XML next to the module's SpriteParts. A single-channel category gets a grey PNG."""
    import sprites, terrain, pngio
    root = os.path.join(container, module_id, "SpriteParts"); cats = {}
    cfg, _ = sprites.read_config(os.path.join(root, "Config.xml"))
    for cat in sorted(os.listdir(root)):
        if os.path.isdir(os.path.join(root, cat)): cats[cat] = sorted(f for f in os.listdir(os.path.join(root, cat)) if f.endswith(".png"))
    out = os.path.join(container, "AssetSources", "GauntletUI"); os.makedirs(out, exist_ok=True)
    cx, px, sx = [], [], []
    for cat, fns in cats.items():
        sizes = [sprites.png_size(os.path.join(root, cat, f)) for f in fns]
        W, H = sum(w for w, _ in sizes), max(h for _, h in sizes)
        if not cfg.get(cat, {}).get("unique"): W, H = (W + 3) // 4 * 4, (H + 3) // 4 * 4        # unique packing keeps the sprite's own size
        sheet = np.zeros((H, W, 4), np.uint8); x = 0
        for f, (w, h) in zip(fns, sizes):
            sheet[:h, x:x + w] = pngio.read_png(os.path.join(root, cat, f))["rgba"]
            px.append("<SpritePart><SheetID>1</SheetID><Name>%s</Name><Width>%d</Width><Height>%d</Height><SheetX>%d</SheetX><SheetY>0</SheetY><CategoryName>%s</CategoryName></SpritePart>" % (f[:-4], w, h, x, cat))
            sx.append("<GenericSprite><Name>%s</Name><SpritePartName>%s</SpritePartName></GenericSprite>" % (f[:-4], f[:-4])); x += w
            nx = os.path.join(root, cat, f[:-4] + ".xml")
            if os.path.isfile(nx): sx.append("<NineRegionSprite><Name>%s</Name></NineRegionSprite>" % sprites.read_nine_region(nx, f[:-4])[0])
        with open(os.path.join(out, "%s_1.png" % cat), "wb") as fh: fh.write(terrain.png_encode(sheet[..., 0] if cfg.get(cat, {}).get("single") else sheet))
        cx.append('<SpriteCategory><Name>%s</Name><SpriteSheetCount>1</SpriteSheetCount><SpriteSheetSize ID="1" Width="%d" Height="%d" /></SpriteCategory>' % (cat, W, H))
    with open(os.path.join(container, module_id, module_id + "SpriteData.xml"), "w", encoding="utf-8") as f:
        f.write("<SpriteData><SpriteCategories>%s</SpriteCategories><SpriteParts>%s</SpriteParts><Sprites>%s</Sprites></SpriteData>" % ("".join(cx), "".join(px), "".join(sx)))
    return ["fake generator ran"]


@test
def sprites_folder_checks_name_every_fault():
    import sprites
    d = os.path.join(TMP, "spr_bad"); shutil.rmtree(d, ignore_errors=True)
    _sprite_png(os.path.join(d, "loose.png"), 8, 8, 1)                        # straight in the folder: the generator crashes on it
    _sprite_png(os.path.join(d, "ui_a", "coin.png"), 8, 8, 1); _sprite_png(os.path.join(d, "ui_b", "coin.png"), 8, 8, 2)   # one name twice
    _sprite_png(os.path.join(d, "ui_a", "btn.png"), 16, 16, 3)
    open(os.path.join(d, "ui_a", "btn.xml"), "w").write("<NineRegionSprite><Name>btn</Name><LeftWidth>4</LeftWidth><RightWidth>4</RightWidth><TopHeight>4</TopHeight><BottomHeight>4</BottomHeight></NineRegionSprite>")
    _sprite_png(os.path.join(d, "bad cat", "x.png"), 8, 8, 1)
    _sprite_png(os.path.join(d, "ui_a", "g1", "g2", "g3", "g4", "deep.png"), 8, 8, 1)
    _sprite_png(os.path.join(d, "ui_c", "huge.png"), 4095, 4, 1)
    open(os.path.join(d, "ui_a", "orphan.xml"), "w").write("<x/>"); open(os.path.join(d, "Config.xml"), "w").write('<?xml version="1.0"?><Config/>')
    lay = sprites.read_layout(d); text = "\n".join(lay["problems"])
    for part in ("loose.png sits straight", "used twice", "is the PNG's own sprite name", "bad cat", "too deep", "does not fit a 4096 sheet", "<?xml ?> line"):
        assert part in text, (part, text)
    assert any("orphan.xml has no PNG" in w for w in lay["warnings"])
    good = os.path.join(TMP, "spr_good"); shutil.rmtree(good, ignore_errors=True)
    _sprite_png(os.path.join(good, "ui_m", "coin.png"), 8, 8, 1); _sprite_png(os.path.join(good, "ui_m", "grp", "star.png"), 8, 8, 2)
    lay = sprites.read_layout(good); assert not lay["problems"] and lay["names"] == ["coin", "grp\\star"], lay
    # names another module's SpriteData already defines are refused
    mods = os.path.join(TMP, "spr_modules", "Other", "GUI"); os.makedirs(mods, exist_ok=True)
    open(os.path.join(mods, "OtherSpriteData.xml"), "w").write("<SpriteData><SpriteCategories><SpriteCategory><Name>ui_m</Name></SpriteCategory></SpriteCategories><Sprites><GenericSprite><Name>coin</Name></GenericSprite></Sprites></SpriteData>")
    modules = os.path.dirname(os.path.dirname(mods))
    got = sprites.clashes(lay, sprites.game_names(modules, "Mine")); assert len(got) == 2, got
    assert sprites.clashes(lay, sprites.game_names(modules, "Other")) == []     # the module's own file does not count


@test
def sprites_stage_the_sheets_with_the_generator_stubbed():
    import sprites, tpac, install, pngio
    d = os.path.join(TMP, "spr_in"); shutil.rmtree(d, ignore_errors=True)
    _sprite_png(os.path.join(d, "ui_m", "coin.png"), 12, 8, (255, 200, 0)); _sprite_png(os.path.join(d, "ui_m", "gem.png"), 20, 8, (0, 200, 255))
    _sprite_png(os.path.join(d, "ui_mask", "disc.png"), 8, 8, (255, 255, 255))
    _sprite_png(os.path.join(d, "ui_odd", "odd.png"), 5, 3, (9, 9, 9))
    open(os.path.join(d, "ui_m", "coin.xml"), "w").write("<NineRegionSprite><Name>coin_box</Name><LeftWidth>2</LeftWidth><RightWidth>2</RightWidth><TopHeight>2</TopHeight><BottomHeight>2</BottomHeight></NineRegionSprite>")
    open(os.path.join(d, "Config.xml"), "w").write('<Config><Category Name="ui_mask"><SingleChannel/></Category><Category Name="ui_odd"><PackAllSpritesToUniqueTextures/></Category></Config>')
    stage = os.path.join(TMP, "spr_stage"); work = os.path.join(TMP, "spr_work"); shutil.rmtree(stage, ignore_errors=True); shutil.rmtree(work, ignore_errors=True)
    dry = sprites.build(d, "Mine", stage, work, os.path.join(TMP, "no_game_bin"), dry=True)
    assert not os.path.exists(stage) and not os.path.exists(work) and sorted(dry["categories"]) == ["ui_m", "ui_mask", "ui_odd"] and dry["categories"]["ui_m"]["nine_region"] == 1
    res = sprites.build(d, "Mine", stage, work, os.path.join(TMP, "no_game_bin"), generate=_fake_generator, log=lambda *x: None)
    assert res["sheets"]["ui_mask"][1]["format"] == "r8" and res["sheets"]["ui_m"][1]["format"] == "bc7" and res["sheets"]["ui_odd"][1]["format"] == "rgba8", res["sheets"]
    assert sorted(os.listdir(os.path.join(stage, "Assets"))) == ["ui_m_1_tex.tpac", "ui_mask_1_tex.tpac", "ui_odd_1_tex.tpac"]
    assert open(os.path.join(stage, "GUI", "MineSpriteData.xml")).read().count("<GenericSprite>") == 4 and len(os.listdir(os.path.join(stage, "RuntimeDataCache"))) == 3
    for cat, fmt in (("ui_m", "BC7"), ("ui_mask", "R8_UNORM"), ("ui_odd", "R8G8B8A8_UNORM")):
        r, pix = tpac.tex_pixels(os.path.join(stage, "Assets", cat + "_1_tex.tpac"), os.path.join(stage, "RuntimeDataCache"))
        assert r["name"] == cat + "_1" and r["format"] == fmt and r["mips"] == 1 and r["import_flags"] == ["dont_degrade", "dont_delay_loading"], r
        assert r["hash_ok"] and all(e["hash_ok"] for e in r["entries"]) and len(pix) == tpac.tex_bytes(r)
        sheet = pngio.read_png(os.path.join(work, "gen", "AssetSources", "GauntletUI", cat + "_1.png"))["rgba"]
        assert (r["w"], r["h"]) == (sheet.shape[1], sheet.shape[0])
        if fmt != "BC7": assert np.array_equal(np.asarray(tpac.decode_mip(r, pix)).reshape(sheet.shape[0], sheet.shape[1], -1)[..., 0], sheet[..., 0]), cat     # the exact formats keep every pixel
    # a stage `bl install` copies: the SpriteData XML goes to <module>/GUI
    plan = {os.path.basename(s): t for s, t in install.plan_for(stage)}
    assert plan["MineSpriteData.xml"] == os.path.join(install.config.MODULE, "GUI", "MineSpriteData.xml") and len(plan) == 7, plan
    # a rerun without a category takes its staged package out again, and a generator that wrote nothing is caught
    shutil.rmtree(os.path.join(d, "ui_odd")); sprites.build(d, "Mine", stage, work, "x", generate=_fake_generator, log=lambda *x: None)
    assert sorted(os.listdir(os.path.join(stage, "Assets"))) == ["ui_m_1_tex.tpac", "ui_mask_1_tex.tpac"] and len(os.listdir(os.path.join(stage, "RuntimeDataCache"))) == 2
    try: sprites.build(d, "Mine", stage, work, "x", generate=lambda c, m: [], log=lambda *x: None)
    except sprites.SpriteError as e: assert "no MineSpriteData.xml" in str(e)
    else: raise AssertionError("a generator that wrote nothing was accepted")


@test
def sprite_generator_runs_only_on_a_private_copy():
    import sprites
    bin_dir = os.path.join(TMP, "gbin", "bin", "Win64_Shipping_wEditor"); os.makedirs(bin_dir, exist_ok=True)
    open(os.path.join(bin_dir, sprites.LIB), "wb").close()
    work = os.path.join(TMP, "gwork"); os.makedirs(os.path.join(work, "gen", "M"), exist_ok=True)
    for bad in (os.path.join(TMP, "gbin", "Modules", "Native"), os.path.join(TMP, "elsewhere"), os.path.join(work, "M")):
        os.makedirs(bad, exist_ok=True)
        try: sprites.run_generator(bin_dir, bad, work)
        except sprites.SpriteError as e: assert "refusing" in str(e), e
        else: raise AssertionError("the generator would have run on " + bad)


@test
def bl_ui_sprites_checks_and_stages_nothing_on_a_dry_run():
    d = os.path.join(TMP, "spr_cli"); shutil.rmtree(d, ignore_errors=True); mod = os.path.join(TMP, "no_game", "Modules", "CliMod")
    _sprite_png(os.path.join(d, "ui_cli", "coin.png"), 8, 8, 1)
    out = bl_run("ui.sprites", d, "--module", mod, "--dry"); r = out["result"]
    assert out["ok"] and r["module"] == "CliMod" and r["categories"]["ui_cli"]["sprites"] == 1 and r["names"] == ["coin"], out
    assert not os.path.exists(os.path.join(TMP, "home", "stage")), "a dry run staged something"
    _sprite_png(os.path.join(d, "loose.png"), 8, 8, 1)
    bad = bl_run("ui.sprites", d, "--module", mod, "--dry"); assert not bad["ok"] and "loose.png" in bad["result"]["problems"][0] and "hint" in bad
    assert not bl_run("ui.sprites", os.path.join(TMP, "nothing_here"), "--module", mod, "--dry")["ok"]


@test
def data_patches_are_valid_xslt():
    import datapatch, scaffold
    import xml.dom.minidom as minidom
    d = os.path.join(TMP, "patchproj"); scaffold.create(d, "PatchMod")
    r = datapatch.set_attrs(d, "PatchMod", "Settlements", "Settlement", "town_X1", {"name": "{=!}A & B <town>", "posX": "10.5"})
    r = datapatch.set_attrs(d, "PatchMod", "Settlements", "Settlement", "town_X2", {"name": "Other"})
    r = datapatch.set_attrs(d, "PatchMod", "Settlements", "Settlement", "town_X1", {"posX": None})
    x = open(r["xslt"], encoding="utf-8").read(); minidom.parseString(x)
    assert "town_X1" in x and "town_X2" in x and "posX" not in x and "A &amp; B &lt;town&gt;" in x
    sub = open(os.path.join(d, "SubModule.xml"), encoding="utf-8").read(); minidom.parseString(sub.encode("utf-8"))
    assert sub.count('path="patchmod_settlements"') == 1
    datapatch.set_attrs(d, "PatchMod", "Settlements", "Settlement", "town_X3", {"name": "Third"})
    assert open(os.path.join(d, "SubModule.xml"), encoding="utf-8").read().count('path="patchmod_settlements"') == 1
@test
def morph_keys_in_a_made_up_mesh_and_a_pose_package():
    import morph, sdk_mesh, tpac
    n, v = 5, 6; rng = np.random.RandomState(1)
    s = dict(P=rng.rand(n, 3), pi=np.array([0, 1, 2, 3, 4, 4], "<u4"), N=rng.rand(v, 3), T=rng.rand(v, 3), B=rng.rand(v, 3), uv0=rng.rand(v, 2), uv1=rng.rand(v, 2), c0=np.zeros(v, "<u4"), F=np.array([[0, 1, 2], [3, 4, 5]], "<u4"), W=None)
    plain = sdk_mesh.edit_blob(s); E = sdk_mesh.parse_edit(plain)
    assert E["frames"] == [] and sdk_mesh.pack_edit(E) == plain
    E3 = morph.with_frames(E, [morph.morph_key(E, t, {1: (0.1, 0, 0)}) for t in (0, 1, 2)]); blob = sdk_mesh.pack_edit(E3); back = sdk_mesh.parse_edit(blob)
    assert sdk_mesh.pack_edit(back) == blob and [float(f["time"]) for f in back["frames"]] == [0, 1, 2] and abs(back["frames"][1]["P"][1, 0] - E["P"][1, 0] - 0.1) < 1e-6
    assert np.array_equal(back["frames"][2]["P"][0], E["P"][0]) and len(back["frames"][0]["N"]) == v
    try: morph.with_frames(E, [dict(time=0, P=np.zeros((n, 4)), N=np.zeros((v + 1, 4)))])
    except ValueError: pass
    else: raise AssertionError("a key frame with the wrong number of normals was accepted")
    an = morph.pose("smile", {0: {95: 0.25}, 6: {95: 1.0, 96: 0.5}}, source=bytes(range(16)))
    data = morph.package_bytes(an, package=bytes(16), guid=bytes(range(16, 32)))
    p = os.path.join(TMP, "smile_morph.tpac"); open(p, "wb").write(data)
    r = morph.check_animations([p]); assert r["animations"] == 1 and r["identical"] == 1 and r["frames"] == 2 and r["targets"] == 3 and not r["failures"], r
    pk = morph.tpac_pack.read_package(p); it = pk["items"][0]
    e = it["entries"][0]; d = morph.parse_definition(tpac.lz4_decompress(e["data"], e["raw"]) if e["stored"] < e["raw"] else e["data"])
    assert d["name"] == "smile" and [f for f, _ in d["frames"]] == [0, 6] and list(d["frames"][1][1]["target"]) == [95, 96] and np.allclose(d["frames"][1][1]["progress"], [1.0, 0.5])
    assert morph.parse_record(it["record"])["source"] == bytes(range(16))


@test
def cloth_map_cook_and_clothing_block_round_trip():
    import cloth, sdk_mesh, tw_formats
    rng = np.random.RandomState(2); V = 9
    mp = dict(index=rng.randint(0, 20, (V, 3)).astype("<u2"), weight=np.c_[rng.dirichlet([1, 1, 1], V), np.zeros(V)].astype("<f4"))
    b = cloth.pack_map(mp); back = cloth.parse_map(b)
    assert cloth.pack_map(back) == b and len(b) == 4 + 2 * 3 * V + 4 + 16 * V and not cloth.map_problems(back, V, 20)
    assert cloth.map_problems(back, V + 1, 20) == ["vertex count"] and cloth.map_problems(back, V, 3) == ["index outside the proxy"]
    for bad in (b[:-1], b + b"\0", struct.pack("<I", 4) + bytes(8) + struct.pack("<I", 4) + bytes(16)):
        try: cloth.parse_map(bad)
        except (ValueError, struct.error): pass
        else: raise AssertionError("a broken cloth map was accepted")
    c = 5; ck = dict(hash=7, counts=(c, 4, 1, 1, 1), bounds=np.array([[0, 0, 0, 1], [2, 2, 2, 1]], "<f4"),
                     arrays=[struct.pack("<I", c) + bytes(4 * c), struct.pack("<I", c) + bytes(4 * c), struct.pack("<I", 1) + bytes(4)], tail=bytes(range(30)))
    kb = cloth.pack_cook(ck); kk = cloth.parse_cook(kb)
    assert cloth.pack_cook(kk) == kb and kk["counts"] == ck["counts"] and len(kk["arrays"]) >= 2 and cloth.cook_problems(kk, 4, [[0, 0, 0], [2, 2, 2]]) == [] and cloth.cook_problems(kk, 5) == ["particle count"]
    sub = dict(guid=bytes(range(16)), name="cape", material="m", material_guid=bytes(16), P=rng.rand(3, 3), pi=np.arange(3, dtype="<u4"), F=np.array([[0, 1, 2]], "<u4"), W=None)
    sm = tw_formats.parse_metamesh_record(sdk_mesh.meta_record(bytes(16), [sub]))["submeshes"][0]
    cl = cloth.parse_clothing(sm["params"]); assert cloth.pack_clothing(cl) == sm["params"] and sorted(k for k in cl if k in cloth.MATERIAL_FIELDS) == sorted(cloth.MATERIAL_FIELDS)
    cl.update(name="banner", wind=3.5, bending_stiffness=0.25); again = cloth.parse_clothing(cloth.pack_clothing(cl))
    assert again["name"] == "banner" and again["wind"] == 3.5 and again["bending_stiffness"] == 0.25 and again["gravity"] == cl["gravity"]
    try: cloth.parse_clothing(sm["params"] + b"\0")
    except ValueError: pass
    else: raise AssertionError("a clothing block with a trailing byte was accepted")


@test
def cloth_fabric_cooker_and_map_writer_on_a_made_up_flag():
    import cloth, sdk_mesh
    # rsqrtss estimate: within the documented 1.5 x 2^-12 relative error, and one Newton step brings it to float precision
    x = np.exp(np.random.RandomState(4).uniform(-30, 30, 5000)).astype(np.float32); exact = 1 / np.sqrt(x.astype(np.float64))
    assert np.abs(cloth.rsqrt_estimate(x) / exact - 1).max() <= 1.5 * 2 ** -12 and np.abs(cloth._rsqrt(x) / exact - 1).max() < 1e-6
    # a 6 x 5 flag in the XZ plane hanging from its top row (vertex colour alpha 0 = fixed, 255 = simulated)
    nx, nz = 6, 5; P = np.array([(i * 0.2, 0, -j * 0.2, 1) for j in range(nz) for i in range(nx)], np.float32); n = len(P)
    F = np.array([t for j in range(nz - 1) for i in range(nx - 1) for t in ((j * nx + i, (j + 1) * nx + i, j * nx + i + 1), (j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1))], np.uint32)
    c0 = np.where(np.arange(n) < nx, 0x00ffffff, 0xffffffff).astype(np.uint32)
    assert cloth.cook_fabric(P, np.arange(n), np.full(n, 0x00ffffff, np.uint32), F) is None          # nothing simulated, nothing to cook
    fab = cloth.cook_fabric(P, np.arange(n), c0, F); b = cloth.pack_fabric(fab); d = cloth.parse_fabric(b)
    again = struct.pack("<6I", *d["header"]) + d["bounds"].astype("<f4").tobytes() + b"".join(struct.pack("<I", len(d["buffers"][k]) // cloth._FAB_SIZE.get(k, 4)) + d["buffers"][k] for k in cloth.FAB_BUFFERS)
    assert again == b and cloth.recook_matches(b, fab) == ("editor", [])
    scale, ncons, parts, first_sim, first_normal, corners = d["header"]
    assert parts == n and first_sim == nx and first_normal == 0 and corners == 3 * len(F) and np.allclose(d["bounds"], [[0, 0, -0.8, 1], [1, 0, 0, 1]])
    pairs = np.frombuffer(d["buffers"]["pairs"], "<u2").reshape(-1, 2).astype(int); rest = np.frombuffer(d["buffers"]["rest"], "<f4")
    sets = np.frombuffer(d["buffers"]["sets"], "<u4").reshape(-1, 8); Pn = fab["positions"]
    assert len(pairs) == ncons == len(rest) and np.allclose(rest, np.linalg.norm(Pn[pairs[:, 0]] - Pn[pairs[:, 1]], axis=1))
    assert (pairs.max(1) >= first_sim).all()                                                          # every constraint moves a simulated particle
    deg = np.bincount(pairs.reshape(-1)).max(); assert len(sets) <= deg + 1                           # Misra-Gries: at most max degree + 1 colours
    for s in sets:                                                                                    # a set is a matching: no particle twice
        idx = np.concatenate([pairs[s[2 * k]:s[2 * k] + s[2 * k + 1]].reshape(-1) for k in range(4)])
        assert len(idx) == len(set(idx.tolist()))
    assert sets[:, 1::2].sum() == ncons and sets[:, 7].sum() == 0 and sets[:, 1].sum() > 0 and sets[:, 3].sum() > 0 and sets[:, 5].sum() > 0
    anc = np.frombuffer(d["buffers"]["anchor"], "<u4"); a_idx, a_q = anc >> 16, anc & 0xffff
    assert (a_idx[first_sim:] < first_sim).all() and (a_idx[:first_sim] == np.arange(first_sim)).all() and a_q[-1] > a_q[first_sim] > 0
    assert np.isclose(np.frombuffer(struct.pack("<I", scale), "<f4")[0] * 65536, 0.8, rtol=1e-4)    # the farthest particle is 4 edges below the top row
    inv = np.frombuffer(d["buffers"]["invmass"], "<f4"); assert np.all(inv > 0) and np.isfinite(inv).all()
    assert cloth.pack_fabric(fab, "div_div_notail") != b and len(cloth.pack_fabric(fab, "div_div_notail")) == len(b)
    # the render mesh: the same flag 1 cm in front, mapped on the flag as proxy
    V = np.zeros(n, sdk_mesh.VERT); V["pi"] = np.arange(n); V["n2"] = (0, 1, 0, 0); V["n"] = (0, 1, 0, 0); V["c0"] = c0
    sim = dict(P=P, V=V, F=F, W=np.zeros(0, sdk_mesh.WEIGHT)); render = dict(P=P + np.float32([0, 0.01, 0, 0]), V=V, F=F, W=sim["W"])
    m = cloth.map_cloth(render, sim, b); mb = cloth.pack_map(m); back = cloth.parse_map(mb)
    assert cloth.pack_map(back) == mb and not cloth.map_problems(back, n, n)
    assert np.allclose(back["weight"][:, 3], 0.01, atol=1e-5) and np.allclose(back["weight"][:, :3].sum(1), 1, atol=1e-5)
    corner = np.frombuffer(d["buffers"]["corner"], "<u4")                                              # every vertex is bound to a triangle that has it as a corner
    assert all(corner[v] in back["index"][v][np.isclose(back["weight"][v, :3], 1, atol=1e-4)] for v in range(n))


@test
def particle_effects_blank_copy_change_and_package():
    import particles, tpac
    for v in (0, 1, 2, 3, 4, 5, 7, 8, 9):
        e = particles.blank_effect(2, v); b = particles.write_effect(e); d = particles.parse_effect(b)
        assert particles.write_effect(d) == b and len(d["emitters"]) == 2 and len(d["emitters"][1]["pre"]) == 4 * particles.pre_size(v) and d["emitters"][1]["name"] == "Emitter 1", v
    try: particles.blank_emitter(6)
    except ValueError: pass
    else: raise AssertionError("an unknown emitter version was accepted")
    mat = bytes(range(1, 17)); eff = particles.parse_effect(particles.write_effect(particles.blank_effect(1, 4)))
    new = particles.modify(eff, emission_rate=25, size=3, life=2.5, material=mat, color=(0.5, 0.25, 1.0), alpha_scale=0.5)
    e0 = new["emitters"][0]; P = particles.named_params(e0)
    assert P["emission_rate"]["base"] == 25 and P["particle_life"]["base"] == 2.5 and e0["guids"][3] == mat and np.allclose(e0["colors"][0, 1:4], [0.5, 0.25, 1.0]) and np.allclose(e0["alphas"][:, 1], [0, 0.5, 0])
    assert particles.particle_size(e0)[0] == 3.0 and eff["emitters"][0]["params_b"][0]["base"] == 10.0, "modify must not change its input"
    baked = bytes(range(256)) * 8
    data = particles.package_bytes(new, baked, "blank_fx", package=bytes(16), guid=bytes(range(16, 32)))
    p = os.path.join(TMP, "blank_fx_psys.tpac"); open(p, "wb").write(data)
    res = list(particles.resources(p)); assert len(res) == 1 and res[0]["name"] == "blank_fx" and not particles.check_resource(res[0]), particles.check_resource(res[0])
    assert [e["type"] for e in res[0]["entries"]] == [particles.EFFECT_DATA, particles.BAKED] and res[0]["entries"][1]["raw"] == baked
    assert particles.write_effect(particles.parse_effect(res[0]["entries"][0]["raw"])) == particles.write_effect(new)
    assert tpac.xxh64(res[0]["entries"][0]["raw"]) == struct.unpack("<Q", res[0]["entries"][0]["rest"][32:40])[0]
    try: particles.parse_effect(particles.write_effect(new) + b"\0")
    except ValueError: pass
    else: raise AssertionError("trailing bytes after the last emitter were accepted")
    assert particles.strings_of(b"\x00\x00\x06\x00\x00\x00sphere\x02\x00\x00\x003d") == ["sphere", "3d"]


@test
def resource_type_table_names_every_type_the_sdk_knows():
    import restypes
    names = {v[0] for v in restypes.RESOURCE_TYPES.values()}
    assert len(restypes.RESOURCE_TYPES) == 13 and {"Metamesh", "Physics shape", "Texture", "Particle effect", "Morph animation", "GPU particle system"} <= names
    assert restypes.describe(next(iter(restypes.RESOURCE_TYPES))).startswith("resource type: ") and restypes.describe(bytes(16)) == "unnamed"
    assert restypes.describe(uuid_bytes("af41613e-bd4d-46ec-8284-debb56c042dc")) == "data type: Cloth map (cloth.py)"


@test
def cloth_flag_mesh_carries_flags_clothing_and_cook():
    import cloth, sdk_mesh, tpac, tpac_pack, tw_formats
    nx, nz = 9, 6                                                 # a 1.6 x 1.0 m grid standing in the xz plane
    xs, zs = np.meshgrid(np.linspace(0, 1.6, nx), np.linspace(0, 1.0, nz))
    P = np.c_[xs.ravel(), np.zeros(nx * nz), zs.ravel()]; n = len(P)
    F = [(j * nx + i, j * nx + i + 1, (j + 1) * nx + i + 1) for j in range(nz - 1) for i in range(nx - 1)]
    F += [(j * nx + i, (j + 1) * nx + i + 1, (j + 1) * nx + i) for j in range(nz - 1) for i in range(nx - 1)]
    one = lambda v: np.tile(np.asarray(v, float), (n, 1))
    sub = dict(name="test_flag", material="none", P=P, pi=np.arange(n, dtype=np.uint32), N=one((0, -1, 0)), T=one((1, 0, 0)), B=one((0, 0, 1)),
               uv0=np.c_[xs.ravel() / 1.6, zs.ravel()], uv1=np.zeros((n, 2)), c0=np.full(n, 0xffffffff, np.uint32), F=np.array(F, np.uint32), W=None)
    sub["c0"], pinned = cloth.pin_colours(P, sub["pi"], sub["c0"], "xmin")
    assert pinned == nz and int((sub["c0"] >> 24 == 0).sum()) == nz
    sub["flags"] = ["uses_cloth_simulation"]; sub["clothing"] = cloth.clothing_block("big_flag", wind=10.0)
    out, rdc, _ = sdk_mesh.write_metamesh("test_flag", "test_flag", [sub], out_dir=TMP, rdc_dir=TMP, force=True)
    pk = tpac_pack.read_package(out)
    it = next(i for i in pk["items"] if i["type"] == sdk_mesh.META_TYPE)
    s = tw_formats.parse_metamesh_record(it["record"])["submeshes"][0]
    c = cloth.parse_clothing(s["params"])
    assert s["flags"] == ["uses_cloth_simulation"] and c["name"] == "big_flag" and c["wind"] == 10.0 and c["factor"] == 5.0
    cooks = [e for e in it["entries"] if bytes(e["rest"][16:32]) == cloth.CLOTH_COOK]
    assert len(cooks) == 1 and bytes(cooks[0]["rest"][0:16]) == s["guid"]
    raw = cooks[0]["data"] if cooks[0]["stored"] == cooks[0]["raw"] else tpac.lz4_decompress(cooks[0]["data"], cooks[0]["raw"])
    assert raw == cloth.cook_submesh(sub) and cloth.parse_fabric(raw)
    sub["c0"] = sub["c0"] & np.uint32(0x00ffffff)                  # nothing simulated: refused
    try: sdk_mesh.write_metamesh("test_flag", "test_flag", [sub], out_dir=TMP, rdc_dir=TMP, force=True)
    except ValueError: pass
    else: raise AssertionError("a cloth submesh without a simulated vertex was written")


@test
def flora_round_trip_and_new_instances():
    import flora
    empty = flora.pack(dict(names=[], rec=np.zeros(0, flora.REC)))
    assert empty == b"FLR2" + struct.pack("<II", 4, 0) and flora.parse(empty)["names"] == []
    a = flora.instance("map_pine_c", 10.0, 20.0, 3.5, yaw_deg=90, scale=2.0); b = flora.instance("bush_a", -1.0, 0.5, 0.25)
    d = dict(names=[a[0], b[0]], rec=np.array([a[1], b[1]], flora.REC)); raw = flora.pack(d); back = flora.parse(raw)
    assert flora.pack(back) == raw and back["names"] == ["map_pine_c", "bush_a"] and len(raw) == 12 + 2 * (4 + 84) + len("map_pine_c") + len("bush_a")
    assert np.allclose(flora.positions(back), [[10, 20, 3.5], [-1, 0.5, 0.25]]) and np.allclose(back["rec"]["m"][0, :3, :3], [[0, 2, 0], [-2, 0, 0], [0, 0, 2]], atol=1e-6)
    try: flora.parse(raw + b"\0")
    except ValueError: pass
    else: raise AssertionError("a size field that disagrees with the file was accepted")


def _wav_bytes(frames, rate=48000, bits=16, tag=1, extensible=False):
    """A WAV file of float samples in [-1, 1] shaped [n] or [n, channels]; tag 1 = PCM, 3 = float."""
    x = np.asarray(frames, float); x = x.reshape(len(x), 1) if x.ndim == 1 else x
    ch = x.shape[1]
    if tag == 3: raw = x.astype("<f4").tobytes(); bits = 32
    elif bits == 8: raw = (x * 127 + 128).astype(np.uint8).tobytes()
    elif bits == 24: raw = b"".join(struct.pack("<i", int(v * 8388607))[:3] for v in x.reshape(-1))
    else: raw = (x * 32767).astype("<i2").tobytes()
    if extensible:
        fmt = struct.pack("<HHIIHHHHIH14s", 0xFFFE, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits, 22, bits, 0, tag, b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71")
    else: fmt = struct.pack("<HHIIHH", tag, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _sweep(secs, f0=300.0, f1=3000.0, rate=48000):
    t = np.arange(int(secs * rate)) / rate
    return 0.5 * np.sin(2 * np.pi * (f0 * t + (f1 - f0) * t * t / (2 * secs))) * np.exp(-3 * t / secs)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f: f.write(data)
    return path


@test
def sound_reads_wav_files_in_every_form_fmod_takes():
    import sound
    x = _sweep(0.1)
    for kw in (dict(bits=8), dict(bits=16), dict(bits=24), dict(tag=3), dict(bits=16, extensible=True), dict(tag=3, extensible=True), dict(rate=22050)):
        p = _write(os.path.join(TMP, "w", "a.wav"), _wav_bytes(x, **kw)); i = sound.wav_info(p); y, rate = sound.read_wav(p)
        assert i["frames"] == len(x) and rate == kw.get("rate", 48000) and y.shape == (len(x), 1), kw
        assert np.abs(y[:, 0] - x).max() < (0.02 if kw.get("bits") == 8 else 1e-3), kw
        assert i["float"] == (kw.get("tag") == 3)
    st = _write(os.path.join(TMP, "w", "st.wav"), _wav_bytes(np.c_[x, -x])); assert sound.wav_info(st)["channels"] == 2 and np.allclose(sound.read_wav(st)[0][:, 1], -x, atol=1e-3)
    for name, data, why in (("junk.wav", b"not a wav", "RIFF"), ("empty.wav", _wav_bytes(np.zeros(0)), "no audio"),
                            ("adpcm.wav", _wav_bytes(x).replace(b"\x01\x00\x01\x00", b"\x02\x00\x01\x00", 1), "format tag"), ("six.wav", _wav_bytes(np.zeros((10, 6))), "channels")):
        p = _write(os.path.join(TMP, "w", name), data)
        try: sound.check_wav(p)
        except sound.SoundError as e: assert why in str(e), (name, str(e))
        else: raise AssertionError(name + " was accepted")


@test
def sound_events_are_kept_in_the_project():
    import scaffold
    d = os.path.join(TMP, "sndproj"); scaffold.create(d, "SndMod")
    a = _write(os.path.join(TMP, "snd_in", "a.wav"), _wav_bytes(_sweep(0.2))); b = _write(os.path.join(TMP, "snd_in", "b", "a.wav"), _wav_bytes(_sweep(0.3)))
    dry = bl_run("sound.add", "event:/sndmod/hit", a, "--dry", cwd=d); assert dry["ok"] and not os.path.exists(os.path.join(d, "sounds.json"))
    out = bl_run("sound.add", "event:/sndmod/hit", a, b, "--like", "event:/mission/combat/blunt/med", cwd=d)
    assert out["ok"] and out["result"]["replaces_native"] is False
    e = json.load(open(os.path.join(d, "sounds.json")))["events"]["event:/sndmod/hit"]
    assert e["like"] == "event:/mission/combat/blunt/med" and len(e["wavs"]) == 2 and e["wavs"][0] != e["wavs"][1]             # two files named a.wav do not overwrite each other
    assert all(os.path.isfile(os.path.join(d, *w.split("/"))) for w in e["wavs"])
    bl_run("sound.add", "event:/sndmod/hit", "--wavs", a, cwd=d); e = json.load(open(os.path.join(d, "sounds.json")))["events"]["event:/sndmod/hit"]
    assert len(e["wavs"]) == 1 and "like" not in e and len(os.listdir(os.path.join(d, "Sounds", "src", "sndmod", "hit"))) == 1       # added again: replaced, its old copies gone
    bl_run("sound.add", "event:/sndmod/loop", a, "--loop", cwd=d)
    assert json.load(open(os.path.join(d, "sounds.json")))["events"]["event:/sndmod/loop"]["loop"] is True
    ls = bl_run("sound.list", cwd=d)["result"]; assert [x["event"] for x in ls["events"]] == ["event:/sndmod/hit", "event:/sndmod/loop"] and not ls["bank_staged"]
    for bad_path in ("sndmod/hit", "event:/", "event:/a b", "event:/a//b", "event:/a/"):
        r = bl_run("sound.add", bad_path, a, cwd=d); assert not r["ok"] and "event path" in r["error"], bad_path
    r = bl_run("sound.add", "event:/sndmod/x", os.path.join(TMP, "nothing.wav"), cwd=d); assert not r["ok"] and "no file" in r["error"]
    assert not bl_run("sound.remove", "event:/sndmod/nope", cwd=d)["ok"]
    assert bl_run("sound.remove", "event:/sndmod/loop", cwd=d)["ok"] and list(json.load(open(os.path.join(d, "sounds.json")))["events"]) == ["event:/sndmod/hit"]
    assert not os.path.exists(os.path.join(d, "Sounds", "src", "sndmod", "loop"))
    r = bl_run("sound.build", cwd=d); assert not r["ok"] and ("game" in r["error"] or "FMOD" in r["error"]), r      # no game here: refused before anything is written


@test
def sound_moduledata_files_register_the_bank_and_keep_other_entries():
    import sound
    sf = ET.fromstring(sound.soundfiles_xml("SndMod"))
    assert sf.get("type") == "sound" and [f.get("name") for f in sf.iter("file")] == ["SndMod.bank"] and sf.find("asset_files/file").get("decompress_samples") == "false"
    rows = [dict(guid="{0e6181f0-6f4d-5193-b9ab-7dbdba49071b}", path="event:/sndmod/a&b", length="0.6", max_distance="450", priority_multiplier="1", bassgid="-1",
                 parameter_indices="isPlayer,Armor Type", send_is_player="true", send_occlusion="false", ignored="x")]
    ev = ET.fromstring(sound.event_data_xml(rows)); e = ev.find("events/event")
    assert ev.get("type") == "sound_event_data" and e.get("path") == "event:/sndmod/a&b" and e.get("parameter_indices") == "isPlayer,Armor Type" and e.get("ignored") is None and e.get("length") == "0.6"
    new = sound.mbproj_text("SndMod")
    assert [(f.get("id"), f.get("name"), f.get("type")) for f in ET.fromstring(new).iter("file")] == [
        ("soln_sound_files", "ModuleData/soundfiles.xml", "sound_files"), ("soln_sound_event_data", "ModuleData/sndmod_sound_event_data.xml", "sound_event_data")]
    base = new.replace("</base>", '  <file id="soln_skins" name="ModuleData/skins.xml" type="skin" />\n</base>').replace("ModuleData/soundfiles.xml", "ModuleData/old.xml")
    again = sound.mbproj_text("SndMod", base); ids = [f.get("id") for f in ET.fromstring(again).iter("file")]
    assert sorted(ids) == ["soln_skins", "soln_sound_event_data", "soln_sound_files"] and "old.xml" not in again and sound.mbproj_text("SndMod", again) == again       # the module's other entries stay; a rerun changes nothing
    try: sound.mbproj_text("SndMod", "<base>")
    except sound.SoundError: pass
    else: raise AssertionError("a project.mbproj without </base> was extended")


def _fake_bank(events):
    """A made-up FMOD bank shaped like a built one: an FMT chunk, event objects that name WAV objects, SNDH and an FSB5 with one mono
    Vorbis-looking sample per WAV. events: [(event guid string, number of samples)]. Returns the bytes."""
    import sound, uuid
    def chunk(cid, body): return cid + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")
    nsamp = sum(n for _, n in events); lists = b""; wavs = b""; k = 0
    for g, n in events:
        ids = []
        for _ in range(n):
            wid = uuid.uuid5(uuid.NAMESPACE_URL, "wav%d" % k).bytes_le; ids.append(wid)
            wavs += chunk(b"WAV ", wid + bytes(6) + struct.pack("<I", k) + bytes(6)); k += 1
        inner = chunk(b"EVTD", uuid.UUID(g).bytes_le + b"".join(ids))
        lists += b"LIST" + struct.pack("<I", 4 + len(inner)) + b"EVTX" + inner
    heads, data, off = b"", b"", 0
    for i in range(nsamp):
        pk = b"".join(struct.pack("<H", 5) + b"\x03abcd" for _ in range(3)); pk += bytes(-len(pk) % 32)
        vorbis = struct.pack("<II", sound.SETUP_CRC[1], 0)
        heads += struct.pack("<Q", 1 | (9 << 1) | ((off >> 5) << 7) | (1000 << 34)) + struct.pack("<I", (11 << 25) | (len(vorbis) << 1)) + vorbis
        data += pk; off += len(pk)
    names = b"".join(b"s%d\0" % i for i in range(nsamp))
    table = b"".join(struct.pack("<I", 4 * nsamp + sum(len(b"s%d\0" % j) for j in range(i))) for i in range(nsamp))
    fsb = b"FSB5" + struct.pack("<6I", 1, nsamp, len(heads), len(table + names), len(data), 15) + bytes(32) + heads + table + names + data
    head = b"FMT " + struct.pack("<III", 8, 0x8E, 0x8C) + lists + wavs
    fsb_at = 12 + len(head) + 8 + 12 + 8
    body = head + b"SNDH" + struct.pack("<I", 12) + struct.pack("<III", 0, fsb_at, len(fsb)) + b"SND " + struct.pack("<I", len(fsb)) + fsb
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"FEV " + body


@test
def sound_bank_check_reads_a_made_up_bank():
    import sound
    ga, gb = "{0e6181f0-6f4d-5193-b9ab-7dbdba49071b}", "{c387ebb0-904c-5627-87d3-3b810c6ad63f}"
    data = _fake_bank([(ga, 2), (gb, 1)]); p = _write(os.path.join(TMP, "fake", "Sounds", "PC", "Fake.bank"), data)
    r = sound.check_bank(p, [dict(guid=ga, path="event:/fake/a", files=2), dict(guid=gb, path="event:/fake/b", files=1)])
    assert r["ok"] and r["format"] == "0x8e/0x8c" and r["samples"] == 3 and r["mono"] == 3 and r["setups"] == {"0x571c7954": 3}, r
    assert [e["ok"] for e in r["events"]] == [True, True] and [len(e["samples"]) for e in r["events"]] == [2, 1]
    r = sound.check_bank(p, [dict(guid=ga, path="event:/fake/a", files=3)]); assert not r["ok"] and "reaches 2 samples" in r["problems"][0]
    r = sound.check_bank(p, [dict(guid="{00000000-0000-0000-0000-000000000001}", path="event:/fake/c", files=1)]); assert not r["ok"] and "not in the bank" in r["problems"][0]
    bad = bytearray(data); struct.pack_into("<I", bad, bytes(bad).find(b"FMT ") + 12, 0x90)
    r = sound.check_bank(_write(os.path.join(TMP, "fake", "bad.bank"), bytes(bad))); assert not r["ok"] and "0x90" in r["problems"][0]
    try: sound.check_bank(_write(os.path.join(TMP, "fake", "junk.bank"), b"RIFF" + bytes(20)))
    except sound.SoundError: pass
    else: raise AssertionError("a file that is not a bank was checked")
    # the event file in the stage beside a bank names its events
    _write(os.path.join(TMP, "fake", "ModuleData", "fake_sound_event_data.xml"), sound.event_data_xml([dict(guid=ga, path="event:/fake/a", max_distance="100000", length="1"),
                                                                                                   dict(guid=gb, path="event:/fake/b", max_distance="20", length="1")]).encode())
    r = sound.info(p); assert r["ok"] and [e["path"] for e in r["events"]] == ["event:/fake/a", "event:/fake/b"], r


@test
def sound_stage_goes_into_the_module_and_is_checked():
    import sound, install
    ga = "{0e6181f0-6f4d-5193-b9ab-7dbdba49071b}"
    stage = os.path.join(TMP, "sndstage")
    _write(os.path.join(stage, "Sounds", "PC", "Fake.bank"), _fake_bank([(ga, 1)])); _write(os.path.join(stage, "ModuleData", "soundfiles.xml"), sound.soundfiles_xml("Fake").encode())
    _write(os.path.join(stage, "ModuleData", "fake_sound_event_data.xml"), sound.event_data_xml([dict(guid=ga, path="event:/fake/a", max_distance="100000", length="1")]).encode())
    _write(os.path.join(stage, "ModuleData", "project.mbproj"), sound.mbproj_text("Fake").encode())
    plan = {os.path.relpath(t, install.config.MODULE).replace(os.sep, "/"): os.path.basename(s) for s, t in install.plan_for(stage)}
    assert plan == {"Sounds/PC/Fake.bank": "Fake.bank", "ModuleData/soundfiles.xml": "soundfiles.xml", "ModuleData/fake_sound_event_data.xml": "fake_sound_event_data.xml",
                    "ModuleData/project.mbproj": "project.mbproj"}, plan
    assert sound.verify_stage(stage) == []
    def broken(edit, why, rel):                                    # each way the engine would misread the stage is named
        copy = os.path.join(TMP, "sndstage_bad"); shutil.rmtree(copy, ignore_errors=True); shutil.copytree(stage, copy)
        p = os.path.join(copy, *rel.split("/"))
        if edit == "delete": os.remove(p)
        else:
            raw = open(p, "rb").read(); open(p, "wb").write(edit(raw))
        errs = sound.verify_stage(copy); assert any(why in e for e in errs), (why, errs)
    broken("delete", "neither staged nor in the module", "Sounds/PC/Fake.bank")
    broken(lambda b: b.replace(b'name="Fake.bank"', b'name="Other.bank"'), "not listed", "ModuleData/soundfiles.xml")
    broken(lambda b: b.replace(b' max_distance="100000"', b""), "max_distance", "ModuleData/fake_sound_event_data.xml")
    broken(lambda b: b.replace(ga.encode(), b"{nope}"), "valid guid", "ModuleData/fake_sound_event_data.xml")
    broken(lambda b: b[:b.find(b"FMT ") + 12] + struct.pack("<I", 0x90) + b[b.find(b"FMT ") + 16:], "0x90", "Sounds/PC/Fake.bank")


@test
def bridge_commands_are_in_the_template_the_list_and_the_summary():
    import bridge, bl
    tpl = open(os.path.join(SDK, "scaffold", "src", "Bridge.cs.tpl"), encoding="utf-8").read()
    summary = bl.COMMANDS["live"]["summary"].split("Commands: ")[1].rstrip(".").split(", ")
    assert summary == list(bridge.COMMANDS), "the summary of `bl live` and bridge.COMMANDS differ"
    for c in bridge.COMMANDS:
        assert 'case "%s":' % c in tpl, c + " has no case in Bridge.cs"
        assert ("\n//   " + c + " ") in tpl or ("\n//   " + c + "\n") in tpl, c + " is not described in the header of Bridge.cs"
    assert "SoundEvent.GetEventIdFromString" in tpl and {"play", "stop"} <= set(bridge.COMMANDS)


@test
def sound_build_with_fmod_studio():
    """The whole route on made-up sweeps. Needs the game's Modding Kit FMOD sample project and FMOD Studio 2.02.27 (it reads, never writes,
    the game folder; everything is built under the test's private root); skipped without them, as on GitHub."""
    game = REAL_GAME
    studio = os.environ.get("FMOD_STUDIO_CL") or os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "FMOD SoundSystem", "FMOD Studio 2.02.27", "fmodstudiocl.exe")
    sample = os.path.join(game or "", "modding_resources", "FMOD Sample Project", "FMODModdingProjectExample", "Metadata")
    if not (game and os.path.isdir(sample) and os.path.isfile(studio)):
        print("  skipped: needs the game's FMOD sample project and FMOD Studio 2.02.27"); return
    import scaffold
    env = dict(os.environ, BANNERLORD_DIR=game, BANNERLORD_SDK_HOME=os.path.join(TMP, "home_studio"))
    d = os.path.join(TMP, "studioproj"); scaffold.create(d, "SndMod")
    hit = [_write(os.path.join(TMP, "studio_in", "hit%d.wav" % i), _wav_bytes(_sweep(0.4 + 0.1 * i, 900, 150))) for i in range(2)]
    click = _write(os.path.join(TMP, "studio_in", "click.wav"), _wav_bytes(np.c_[_sweep(0.1, 2000, 3500, 44100), _sweep(0.1, 2500, 4000, 44100)], rate=44100))
    def run(*argv):
        r = subprocess.run([sys.executable, os.path.join(SDK, "bl.py")] + list(argv), capture_output=True, text=True, cwd=d, env=env, encoding="utf-8")
        out = json.loads([l for l in r.stdout.splitlines() if l.strip()][-1]); assert out["ok"], out
        return out["result"]
    run("sound.add", "event:/sndmod/combat/hit", *hit, "--like", "event:/mission/combat/blunt/med")
    run("sound.add", "event:/sndmod/ui/click", click, "--like", "event:/ui/default")
    r = run("sound.build", "--decode", "4")
    assert r["ok"] and r["check"]["format"] == "0x8e/0x8c" and r["check"]["samples"] == 3 and r["check"]["stereo"] == 1 and not r["check"]["problems"], r
    assert [e["files"] for e in r["events"]] == [2, 1] and all(e["template"] for e in r["events"])
    dec = r["check"]["decode"]; assert "skipped" in dec or dec["matched"] == dec["tried"] == 3, dec
    stage = os.path.join(env["BANNERLORD_SDK_HOME"], "stage", "work")
    for f in ("Sounds/PC/SndMod.bank", "ModuleData/soundfiles.xml", "ModuleData/sndmod_sound_event_data.xml", "ModuleData/project.mbproj"): assert os.path.isfile(os.path.join(stage, *f.split("/"))), f
    rows = list(ET.parse(os.path.join(stage, "ModuleData", "sndmod_sound_event_data.xml")).getroot().iter("event"))
    assert [(x.get("path"), x.get("max_distance")) for x in rows] == [("event:/sndmod/combat/hit", "450"), ("event:/sndmod/ui/click", "100000")]
    again = run("sound.build"); assert [e["guid"] for e in again["events"]] == [e["guid"] for e in r["events"]]                        # a rebuild keeps the GUIDs
    assert run("sound.info", "--decode", "3")["ok"]


def uuid_bytes(s):
    import uuid
    return uuid.UUID(s).bytes_le


def main():
    failed = []
    for t in TESTS:
        try: t(); print("PASS", t.__name__)
        except Exception:
            failed.append(t.__name__); print("FAIL", t.__name__); traceback.print_exc()
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n%d of %d passed%s" % (len(TESTS) - len(failed), len(TESTS), "" if not failed else "; failed: " + ", ".join(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
