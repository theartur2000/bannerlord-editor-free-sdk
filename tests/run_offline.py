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
