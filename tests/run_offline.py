"""Tests that need no game install: codecs, file writers on made-up data, the command line, the MCP server, the scaffold.

    python tests/run_offline.py            exit code 0 when every test passes

These run on GitHub for every push. Everything that compares against the game's own files is in sdk/selftest.py and
`bl compat`, which need an install. No test here reads a game file and no fixture holds game content: every input is
made up in the test.
"""
import json, os, shutil, struct, subprocess, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK = os.path.join(ROOT, "sdk")
sys.path.insert(0, SDK)
# A game folder that does not exist, and a private working root: the tests must not depend on, or touch, a real install.
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
