"""bl: the whole SDK as one command line, made for agents.

    bl <command> [positional values] [--name value ...] [--dry]
    bl commands                       every command with its parameters, as JSON
    bl doctor                         what the SDK found on this machine

Every command prints exactly one JSON object and nothing else:
    {"ok": true,  "command": "scene.info", "result": {...}, "log": ["...", ...]}
    {"ok": false, "command": "scene.info", "error": "...", "hint": "...", "log": [...]}
The exit code is 0 or 1. "log" holds what the wrapped tool printed. Commands that change files take --dry and then only
say what they would do. A command name may be written with a dot or a space: `bl scene.info x` or `bl scene info x`.
Values can also be passed in one piece, which is what the MCP server does: `bl scene.info --args '{"scene": "x"}'`.

Options for every command, before or after the command name:
    --game DIR      the game install (else BANNERLORD_DIR, else the standard Steam folder)
    --module DIR    the module being built (else BANNERLORD_MODULE_DIR, else the project's module under <game>\\Modules)
    --project DIR   a project made with `bl new` (else the folder with bl.json at or above the current one)
    --home DIR      the SDK's own working root (else BANNERLORD_SDK_HOME)
    --pretty        indent the JSON

The same registry feeds the MCP server (sdk/mcp_server.py), so a command added here is a tool there.
"""
import contextlib, io, json, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TESTED_GAME = "v1.4.8"
COMMANDS = {}


class BlError(Exception):
    def __init__(self, message, hint=None):
        Exception.__init__(self, message); self.hint = hint


def P(name, kind="string", help="", required=False, default=None, choices=None):
    """One parameter. kind: string, int, number, bool, list (repeat the option or separate with commas), json."""
    return {"name": name, "kind": kind, "help": help, "required": required, "default": default, "choices": choices}


def command(name, summary, params=(), writes=False, launches=False):
    def deco(f):
        ps = list(params) + ([P("dry", "bool", "only say what would be done")] if writes else [])
        COMMANDS[name] = {"name": name, "summary": summary, "params": ps, "func": f, "writes": writes, "launches": launches}
        return f
    return deco


def schema(cmd):
    """The JSON Schema of a command's arguments (what an MCP client is shown)."""
    kinds = {"string": {"type": "string"}, "int": {"type": "integer"}, "number": {"type": "number"}, "bool": {"type": "boolean"},
             "list": {"type": "array", "items": {"type": "string"}}, "json": {}}
    props = {}
    for p in cmd["params"]:
        d = dict(kinds[p["kind"]]); d["description"] = p["help"]
        if p["choices"]: d["enum"] = list(p["choices"])
        if p["default"] is not None: d["default"] = p["default"]
        props[p["name"]] = d
    return {"type": "object", "properties": props, "required": [p["name"] for p in cmd["params"] if p["required"]], "additionalProperties": False}


# ---------------------------------------------------------------- helpers

def _cfg():
    """The SDK's config module. It reads the environment once, when it is first imported, so the module folder is settled
    before that: --module may be a bare module id, and inside a project the project's module is the default."""
    if "config" not in sys.modules:
        game = os.environ.get("BANNERLORD_DIR") or r"C:\Program Files (x86)\Steam\steamapps\common\Mount & Blade II Bannerlord"
        m = os.environ.get("BANNERLORD_MODULE_DIR")
        import scaffold
        d, pj = scaffold.find_project(os.environ.get("BL_PROJECT") or ".")
        if m and os.sep not in m and "/" not in m: os.environ["BANNERLORD_MODULE_DIR"] = os.path.join(game, "Modules", m)
        elif not m and pj: os.environ["BANNERLORD_MODULE_DIR"] = os.path.join(game, "Modules", pj["module"])
        # a project's packages go into Assets\<module id>, not into the SDK's default folder name
        if pj and not os.environ.get("MODULE_ASSET_SUB"): os.environ["MODULE_ASSET_SUB"] = pj["module"].lower()
    import config
    return config


def _project(args=None):
    import scaffold
    return scaffold.find_project(os.environ.get("BL_PROJECT") or ".")


def _need_project():
    d, pj = _project()
    if d is None: raise BlError("no project here: no bl.json at or above the current folder", "make one with `bl new <Name>`, or pass --project <folder>")
    return d, pj


def _module_dir(must=True):
    """The module folder in the game: --module / BANNERLORD_MODULE_DIR, else <game>\\Modules\\<project's module>."""
    c = _cfg()
    if c.BANNERLORD_MODULE_DIR: return c.BANNERLORD_MODULE_DIR
    d, pj = _project()
    if pj is not None and c.BANNERLORD_DIR: return os.path.join(c.MODULES, pj["module"])
    if must: raise BlError("no module: neither --module, BANNERLORD_MODULE_DIR nor a project (bl.json) names one")
    return None


def _guard_game():
    """Refuse to touch the game folder while the game runs or somebody else holds the lock."""
    c = _cfg()
    if not c.BANNERLORD_DIR or not os.path.isdir(c.NATIVE): raise BlError("the game install was not found", "pass --game <folder> or set BANNERLORD_DIR")
    running = c.game_processes()
    if running: raise BlError("the game is running (%s): nothing changed" % ", ".join(running), "close the game first")
    why = c.lock_refusal(os.environ.get("BL_LOCK_OWNER"))
    if why: raise BlError(why, "the lock is released by whoever took it (harness/game_lock.ps1 release <owner>)")


def _game_version():
    c = _cfg()
    try:
        with open(os.path.join(c.GAME, "bin", "Win64_Shipping_Client", "Version.xml"), encoding="utf-8", errors="replace") as f:
            m = re.search(r'<Singleplayer\s+Value="([^"]+)"', f.read())
        return m.group(1) if m else None
    except OSError:
        return None


def _scene_folder(ref):
    """A scene folder from a path, or from a scene id looked up in the stage, the project, the module and the game."""
    c = _cfg()
    if os.path.isfile(os.path.join(ref, "scene.xscene")): return os.path.abspath(ref)
    places = [os.path.join(c.SDK_STAGE_DIR, "work", "SceneObj"), os.path.join(c.SDK_STAGE_DIR, "scenes")]
    d, pj = _project()
    if d: places.append(os.path.join(d, "SceneObj"))
    m = _module_dir(False)
    if m: places.append(os.path.join(m, "SceneObj"))
    if os.path.isdir(c.MODULES): places += [os.path.join(c.MODULES, x, "SceneObj") for x in sorted(os.listdir(c.MODULES))]
    for pl in places:
        if os.path.isfile(os.path.join(pl, ref, "scene.xscene")): return os.path.join(pl, ref)
    raise BlError("no scene %s: not a folder with scene.xscene and not a scene id of the stage, the project, the module or the game" % ref,
                  "`bl scene.list` names the shipped battle scenes")


def _writable_scene(folder):
    """Scenes inside the game are changed only in the module being built: never in the game's own modules."""
    c = _cfg(); f = os.path.normcase(os.path.abspath(folder)); mods = os.path.normcase(os.path.abspath(c.MODULES))
    if not f.startswith(mods + os.sep): return
    m = _module_dir(False)
    if m and f.startswith(os.path.normcase(os.path.abspath(m)) + os.sep): _guard_game(); return
    raise BlError("%s belongs to a module that is not the one being built: not changing it" % folder,
                  "copy the scene first: `bl scene.copy <id> <new name>` writes it to the stage")


def _plain(o):
    """numpy values and tuples as plain JSON types."""
    try:
        import numpy as np
        if isinstance(o, np.ndarray): return [_plain(x) for x in o.tolist()] if o.size <= 4096 else "<array %s>" % (o.shape,)
        if isinstance(o, np.generic): return o.item()
    except ImportError:
        pass
    if isinstance(o, dict): return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)): return [_plain(x) for x in o]
    if isinstance(o, bytes): return o.hex()
    if isinstance(o, float): return round(o, 6)
    return o


def _floats(v, n, what):
    if v is None: return None
    xs = [float(x) for x in (v if isinstance(v, (list, tuple)) else str(v).replace(",", " ").split())]
    if len(xs) != n: raise BlError("%s takes %d numbers, got %r" % (what, n, v))
    return tuple(xs)


def _sdk_tool(module, argv):
    """Runs an existing SDK command (its main(argv)); what it prints becomes the log. A SystemExit is an error."""
    mod = __import__(module)
    try:
        rc = mod.main(argv)
    except SystemExit as e:
        rc = e.code
    if isinstance(rc, str): raise BlError(rc)
    if rc not in (None, 0): raise BlError("%s %s failed with exit code %s (see the log)" % (module, argv[0] if argv else "", rc))


def _staged(log):
    return [m.group(1) for line in log for m in [re.search(r"staged (.+?\.(?:tpac|rdc))", line)] if m]


LOG = []          # lines printed while the current command ran (filled by run())
CURRENT = [None]  # the command being run, known as soon as its name is parsed (for error answers)


# ---------------------------------------------------------------- the machine and the project

@command("doctor", "What the SDK found on this machine: game, version, module, tools, locks.")
def doctor(a):
    c = _cfg(); d, pj = _project(); gv = _game_version()
    dotnet = _dotnet()
    out = {"game": c.BANNERLORD_DIR, "game_found": bool(c.BANNERLORD_DIR and os.path.isdir(c.NATIVE)), "game_version": gv,
           "tested_game_version": TESTED_GAME, "version_matches": bool(gv and gv.startswith(TESTED_GAME)),
           "game_running": c.game_processes(), "lock": c.lock_refusal(os.environ.get("BL_LOCK_OWNER")),
           "module": _module_dir(False), "project": d, "project_module": pj["module"] if pj else None,
           "sdk_home": c.SDK_HOME, "stage": c.SDK_STAGE_DIR, "python": sys.version.split()[0], "dotnet": dotnet}
    for lib in ("numpy", "PIL"):
        try: out[lib] = __import__(lib).__version__
        except ImportError: out[lib] = None
    problems = []
    if not out["game_found"]: problems.append("the game was not found: pass --game or set BANNERLORD_DIR")
    elif not out["version_matches"]: problems.append("game version %s: the SDK is tested on %s; run `bl compat`" % (gv, TESTED_GAME))
    if out["numpy"] is None: problems.append("numpy is missing: pip install -r requirements.txt")
    if out["PIL"] is None: problems.append("Pillow is missing: previews and custom textures need it")
    if dotnet is None: problems.append("dotnet was not found: `bl build` needs the .NET SDK (or BL_DOTNET pointing at dotnet.exe)")
    out["problems"] = problems
    return out


@command("commands", "Every command with its parameters (the same list the MCP server offers as tools).")
def commands(a):
    return {"commands": [{"name": c["name"], "summary": c["summary"], "writes": c["writes"], "launches_game": c["launches"], "arguments": schema(c)}
                         for c in COMMANDS.values()]}


@command("compat", "Checks every file format against this install by rebuilding the game's own files; names what no longer holds.",
         [P("quick", "bool", "smaller samples"), P("scenes", "int", "how many shipped scenes to round-trip (default 6, with quick 2)")])
def compat(a):
    import selftest, catalog, scene, terrain, navmesh
    c = _cfg(); c.require_game()
    rows = []; orig = selftest.report
    def rec(name, ok, detail, notes=()):
        rows.append({"format": name, "ok": bool(ok), "detail": detail, "notes": list(notes)}); return orig(name, ok, detail, notes)
    selftest.report = rec
    try:
        quick = bool(a.get("quick"))
        selftest.section_packages(40 if quick else 200, 300, False)
        selftest.section_textures(4)                              # fewer per format and the 80 percent hash rule misfires
        selftest.section_caches(60 if quick else 300)
        selftest.section_skeletons()
        if not quick: selftest.section_synthetic()
        for extra in ("section_codecs", "section_physics"):      # sections only some versions of the self-test have
            if hasattr(selftest, extra): getattr(selftest, extra)()
    finally:
        selftest.report = orig
    recs = catalog.load(log=lambda *x: None).records[:max(0, int(a.get("scenes") or (2 if a.get("quick") else 6)))]
    good = {"scene.xscene": 0, "terrain.bin": 0, "navmesh.bin": 0}; bad = []
    for r in recs:
        for fn, check in (("scene.xscene", scene.check), ("navmesh.bin", navmesh.check), ("terrain.bin", lambda p: terrain.check(p, log=lambda *x: None)[0])):
            p = os.path.join(r["dir"], fn)
            try: ok = bool(check(p))
            except Exception as e: ok = False; bad.append("%s/%s: %s" % (r["id"], fn, e))
            good[fn] += ok
            if not ok: bad.append("%s/%s" % (r["id"], fn))
    for fn in good: rows.append({"format": fn, "ok": good[fn] == len(recs), "detail": "%d of %d shipped scenes rebuild identical" % (good[fn], len(recs)), "notes": [b for b in bad if b.split(":")[0].endswith(fn)][:5]})
    return {"game_version": _game_version(), "tested_game_version": TESTED_GAME, "formats": rows, "all_ok": all(r["ok"] for r in rows),
            "broken": [r["format"] for r in rows if not r["ok"]]}


@command("new", "A new mod project: manifest, C# project with a test driver and scene slots, scene list. Builds as it is.",
         [P("id", help="the module id, also the folder and namespace: a letter, then letters, digits or _", required=True),
          P("name", help="the name the launcher shows (default: the id)"), P("folder", help="where to make it (default: ./<id>)"),
          P("version", default="v0.1.0")], writes=True)
def new(a):
    import scaffold
    folder = os.path.abspath(a.get("folder") or a["id"])
    gv = _game_version() or TESTED_GAME
    try: files = scaffold.create(folder, a["id"], a.get("name"), a.get("version") or "v0.1.0", ".".join(gv.split(".")[:3]), dry=bool(a.get("dry")))
    except ValueError as e: raise BlError(str(e))
    return {"project": folder, "module": a["id"], "files": files, "next": ["cd " + folder, "bl build", "bl deploy", 'bl run --words "tod=12 fight secs=30 quit" --launch']}


def _dotnet():
    for cand in (os.environ.get("BL_DOTNET"), os.path.join(os.environ.get("DOTNET_ROOT", ""), "dotnet.exe") if os.environ.get("DOTNET_ROOT") else None,
                 shutil.which("dotnet")):
        # a dotnet with only the runtime cannot build: the SDKs are the folders under <dotnet>\sdk
        sdks = os.path.join(os.path.dirname(os.path.realpath(cand)), "sdk") if cand and os.path.isfile(cand) else None
        if sdks and os.path.isdir(sdks) and os.listdir(sdks): return cand
    return None


@command("build", "Compiles the project's C# into build/<id>.dll against the installed game.",
         [P("configuration", default="Release", choices=("Release", "Debug"))])
def build(a):
    d, pj = _need_project(); c = _cfg(); c.require_game()
    dn = _dotnet()
    if dn is None: raise BlError("dotnet was not found", "install the .NET SDK, or set BL_DOTNET to dotnet.exe")
    env = dict(os.environ); env.setdefault("DOTNET_ROOT", os.path.dirname(dn)); env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"; env["DOTNET_NOLOGO"] = "1"
    proj = os.path.join(d, "src", pj["module"] + ".csproj"); out = os.path.join(d, "build")
    t = time.time()
    r = subprocess.run([dn, "build", proj, "-c", a.get("configuration") or "Release", "-nologo", "-p:BannerlordDir=" + c.BANNERLORD_DIR, "-o", out],
                       capture_output=True, text=True, env=env)
    lines = (r.stdout + r.stderr).splitlines()
    errors = sorted(set(l.strip() for l in lines if re.search(r": error ", l))); warnings = sorted(set(l.strip() for l in lines if re.search(r": warning ", l)))
    dll = os.path.join(out, pj["module"] + ".dll")
    if r.returncode != 0 or not os.path.isfile(dll):
        LOG.extend(lines[-40:]); e = BlError("the build failed with %d errors" % len(errors), "the errors are in the result of this call"); e.result = {"errors": errors[:40]}; raise e
    return {"dll": dll, "bytes": os.path.getsize(dll), "seconds": round(time.time() - t, 1), "warnings": warnings[:20], "errors": []}


def _shader_cache(module_name, scene_id):
    return os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"), "Mount and Blade II Bannerlord", "Shaders", "TerrainShaders", module_name, scene_id)


@command("deploy", "Copies the project into <game>\\Modules\\<id>: manifest, DLL, ModuleData, SceneObj. Refuses while the game runs.",
         [P("scenes", "bool", "also copy the project's SceneObj folder (default true)", default=True)], writes=True)
def deploy(a):
    d, pj = _need_project(); c = _cfg(); mod = pj["module"]; dst = os.path.join(c.MODULES, mod); dry = bool(a.get("dry"))
    if not dry: _guard_game()
    plan = [("SubModule.xml", os.path.join(dst, "SubModule.xml"))]
    dll = os.path.join(d, "build", mod + ".dll")
    if os.path.isfile(dll): plan += [(dll, os.path.join(dst, "bin", sub, mod + ".dll")) for sub in ("Win64_Shipping_Client", "Win64_Shipping_wEditor")]
    for top in ("ModuleData", "Prefabs", "GUI") + (("SceneObj",) if a.get("scenes", True) else ()):
        for root, _, files in os.walk(os.path.join(d, top)):
            for fn in files:
                src = os.path.join(root, fn); plan.append((src, os.path.join(dst, os.path.relpath(src, d))))
    plan = [(s if os.path.isabs(s) else os.path.join(d, s), t) for s, t in plan]
    scenes = sorted(x for x in os.listdir(os.path.join(d, "SceneObj"))) if a.get("scenes", True) and os.path.isdir(os.path.join(d, "SceneObj")) else []
    cleared = []
    if not dry:
        for s, t in plan:
            os.makedirs(os.path.dirname(t), exist_ok=True); shutil.copyfile(s, t)
        for sc in scenes:                                        # the game keeps the terrain shader it compiled per module and scene name
            cache = _shader_cache(mod, sc)
            if os.path.isdir(cache): shutil.rmtree(cache); cleared.append(sc)
    return {"module_folder": dst, "files": len(plan), "dll": os.path.isfile(dll), "scenes": scenes, "terrain_shader_caches_cleared": cleared,
            "warning": None if os.path.isfile(dll) else "no build/%s.dll yet: run `bl build` (the module does not load without it)" % mod}


# ---------------------------------------------------------------- assets (the stage, then the module)

STAGE = P("stage", help="staging folder name", default="work")


@command("asset.texture", "Stages a texture package from a PNG.",
         [P("png", required=True), P("name"), P("format", help="default: bc1 or bc3 by alpha; a name ending in _n becomes bc5, _s bc1, _h bc4"),
          P("usage"), P("no_mips", "bool"), STAGE, P("force", "bool", "rewrite a staged file")], writes=True)
def asset_texture(a):
    argv = ["texture", a["png"]] + _opts(a, ("name", "format", "usage", "stage")) + (["--no-mips"] if a.get("no_mips") else []) + (["--force"] if a.get("force") else [])
    return _stage_cmd(a, argv)


@command("asset.material", "Stages a material that uses staged or installed textures.",
         [P("name", required=True), P("diffuse", required=True), P("normal"), P("specular"), P("shader", default="pbr_shading"), P("blend"),
          P("skinned", "bool", "for skinned meshes (default: decided by the template)"), P("static", "bool"), STAGE, P("force", "bool")], writes=True)
def asset_material(a):
    argv = ["material", a["name"]] + _opts(a, ("diffuse", "normal", "specular", "shader", "blend", "stage")) + [f for k, f in (("skinned", "--skinned"), ("static", "--static"), ("force", "--force")) if a.get(k)]
    return _stage_cmd(a, argv)


@command("asset.mesh", "Stages a static or skinned mesh from an FBX, and reports its size so a wrong unit shows at once.",
         [P("fbx", required=True), P("name"), P("material", help="material names, comma separated, one per submesh"),
          P("scale", "number", help="multiply positions: 0.01 for an FBX from Blender's default export, which comes out 100 times too big", default=1.0),
          STAGE, P("force", "bool")], writes=True)
def asset_mesh(a):
    import sdk_mesh
    P_ = [s["P"] for _, subs in sdk_mesh.submeshes(a["fbx"], float(a.get("scale") or 1.0)) for s in subs]
    import numpy as np
    P_ = np.concatenate(P_) if P_ else np.zeros((1, 3)); size = (P_.max(0) - P_.min(0)).round(3).tolist()
    res = _stage_cmd(a, ["mesh", a["fbx"]] + _opts(a, ("name", "material", "scale", "stage")) + (["--force"] if a.get("force") else []))
    res["size_m"] = size
    big = max(size)
    if big > 60: res["warning"] = "the mesh is %.0f m across: if it should be smaller, the FBX unit is off (Blender's default export needs --scale 0.01)" % big
    elif big < 0.05: res["warning"] = "the mesh is %.3f m across: if it should be bigger, the FBX unit is off (try --scale 100)" % big
    return res


@command("asset.skeleton", "Stages a skeleton (at most 64 bones, a minimal ragdoll) from an FBX or glTF.",
         [P("file", required=True), P("name"), P("type", default="other", choices=("other", "human", "horse")), P("no_physics", "bool"), STAGE, P("force", "bool")], writes=True)
def asset_skeleton(a):
    return _stage_cmd(a, ["skeleton", a["file"]] + _opts(a, ("name", "type", "stage")) + (["--no-physics"] if a.get("no_physics") else []) + (["--force"] if a.get("force") else []))


@command("asset.anim", "Stages a skeletal animation from a single-take FBX; with --skeleton it is attached to that skeleton (the owner the game checks).",
         [P("fbx", required=True), P("skeleton", help="a skeleton name, staged or installed (default: the human skeleton)"), STAGE, P("force", "bool")], writes=True)
def asset_anim(a):
    res = _stage_cmd(a, ["anim", a["fbx"]] + _opts(a, ("stage",)) + (["--force"] if a.get("force") else []))
    if a.get("dry"): return res
    take = next((m.group(1) for line in LOG for m in [re.search(r"\(animation (.+?), \d+ keys\)", line)] if m), None)
    res["animation"] = take
    if a.get("skeleton"):
        import tpac, tpac_pack, sdk_skeleton
        c = _cfg(); name = a["skeleton"]
        cands = [os.path.join(c.SDK_STAGE_DIR, a.get("stage") or "work", "Assets", name + "_geo.tpac"), os.path.join(c.ASSETS, name + "_geo.tpac")]
        guid = None
        for p_ in cands:
            if os.path.isfile(p_):
                for it in tpac_pack.read_package(p_, data=False)["items"]:
                    if bytes(it["type"]) == sdk_skeleton.SKEL_TYPE and it["name"].decode("utf-8", "replace") == name: guid = bytes(it["guid"])
            if guid: break
        if guid is None: raise BlError("no skeleton named %s in the stage or the module" % name, "stage it first with `bl asset skeleton`")
        res["owner_set"] = tpac.set_owner(res["staged"][0], guid); res["skeleton"] = name
    return res


@command("asset.clip", "Stages an animation clip on a staged animation, and its runtime cache.",
         [P("name", required=True), P("animation", required=True), P("frames", "int", required=True), P("fps", "number", default=30.0), STAGE,
          P("cache", "bool", "also write the clip's runtime cache entry (default true)", default=True)], writes=True)
def asset_clip(a):
    res = _stage_cmd(a, ["clip", a["name"], a["animation"], str(int(a["frames"])), str(float(a.get("fps") or 30.0))] + _opts(a, ("stage",)))
    if a.get("cache", True) and not a.get("dry"):
        before = len(LOG); _sdk_tool("install", ["clip-cache", a["name"]] + _opts(a, ("stage",))); res["cache_log"] = LOG[before:]
    return res


@command("asset.collision", "Stages a collision body (physics shape) of capsules, spheres, convex hulls and triangle meshes, for scene entities (`scene.add_entity --physics`).",
         [P("name", help="the shape's name, which entities refer to", required=True),
          P("capsule", "list", help='"x0 y0 z0 x1 y1 z1 radius" in metres, one per capsule (repeat, or separate with commas)'),
          P("sphere", "list", help='"x y z radius" in metres, one per sphere'),
          P("hull", "list", help="an .obj (or .psk) file per convex hull: the hull of its points is cooked with the game's PhysX library"),
          P("mesh", "list", help="an .obj (or .psk) file per triangle mesh body, cooked as it is (static bodies only)"),
          P("material", help="physics material name (stone, wood, adobe, default, straw, fabric, metal ...)", default="stone"), STAGE, P("force", "bool")], writes=True)
def asset_collision(a):
    import physics, preview
    caps = [_floats(c, 7, "--capsule") for c in a.get("capsule") or ()]; sph = [_floats(s_, 4, "--sphere") for s_ in a.get("sphere") or ()]
    mat = a.get("material") or "stone"; mans = []
    for f in a.get("hull") or (): P_, F_, _ = preview.load_mesh(f); mans.append(physics.convex_hull(P_, [mat]))
    for f in a.get("mesh") or (): P_, F_, _ = preview.load_mesh(f); mans.append(physics.triangle_mesh(P_, F_, [mat]))
    if not caps and not sph and not mans: raise BlError("give at least one --capsule, --sphere, --hull or --mesh")
    if mans:
        import physx_cook
        if not physx_cook.available(): raise BlError("hulls and meshes are cooked with the game's PhysX library, which did not load: %s" % physx_cook.why_not())
    shape = physics.build(a["name"], capsules=[physics.capsule(c[:3], c[3:6], c[6]) for c in caps], spheres=[physics.sphere(s_[:3], s_[3]) for s_ in sph], manifolds=mans, material=mat)
    c = _cfg(); out = os.path.join(c.SDK_STAGE_DIR, a.get("stage") or "work", "Assets")
    if a.get("dry"): return {"would_write": os.path.join(out, a["name"] + "_phys.tpac"), "capsules": len(caps), "spheres": len(sph)}
    try: p = physics.write_package(shape, out, bool(a.get("force")))
    except FileExistsError as e: raise BlError(str(e), "pass --force to replace it")
    return {"staged": [p], "capsules": len(caps), "spheres": len(sph), "hulls_and_meshes": len(mans)}


def _opts(a, keys): return [x for k in keys if a.get(k) not in (None, "") for x in ("--" + k.replace("_", "-"), str(a[k]))]


def _stage_cmd(a, argv):
    c = _cfg(); c.require_game()
    if a.get("dry"): return {"would_run": "install.py " + " ".join(argv), "stage": os.path.join(c.SDK_STAGE_DIR, a.get("stage") or "work")}
    before = len(LOG); _sdk_tool("install", argv)
    return {"staged": _staged(LOG[before:]), "stage": os.path.join(c.SDK_STAGE_DIR, a.get("stage") or "work")}


@command("stage.list", "What a stage holds and where each file would go in the module.", [STAGE])
def stage_list(a):
    import install
    s = install._stage_path(a.get("stage") or "work"); rows = []
    for sub, dest in install.TARGETS:
        for root, _, files in os.walk(os.path.join(s, sub)):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), os.path.join(s, sub)); t = os.path.join(dest, rel)
                rows.append({"file": sub + "/" + rel.replace(os.sep, "/"), "bytes": os.path.getsize(os.path.join(root, fn)), "state": "replace" if os.path.exists(t) else "new"})
    return {"stage": s, "files": rows, "module": _cfg().MODULE}


@command("stage.verify", "Runs the validators on a stage: the faults the game turns into crashes or freezes.", [STAGE])
def stage_verify(a):
    import validate
    errs = validate.validate(a.get("stage") or "work", [])
    hard = [e for e in errs if not e.startswith("WARN")]
    return {"ok_to_install": not hard, "errors": hard, "warnings": [e for e in errs if e.startswith("WARN")]}


@command("install", "Copies a stage into the module, with a backup and a manifest. Refuses while the game runs.", [STAGE, P("verify", "bool", "run the validators first")], writes=True)
def install_(a):
    _cfg().require_module(); argv = ["install"] + _opts(a, ("stage",)) + (["--dry"] if a.get("dry") else []) + (["--verify"] if a.get("verify") else [])
    before = len(LOG); _sdk_tool("install", argv)
    return {"done": not a.get("dry"), "lines": LOG[before:][-30:]}


@command("revert", "Undoes the newest install (or the one named by a manifest).", [P("manifest")], writes=True)
def revert(a):
    _cfg().require_module()
    if a.get("dry"): return {"would_run": "install.py revert " + (a.get("manifest") or "")}
    before = len(LOG); _sdk_tool("install", ["revert"] + ([a["manifest"]] if a.get("manifest") else []))
    return {"lines": LOG[before:][-30:]}


@command("status", "Game, module, packed or loose, stages, the last install.")
def status(a):
    before = len(LOG); _sdk_tool("install", ["status"])
    return {"lines": LOG[before:]}


# ---------------------------------------------------------------- UI

@command("ui.sprites", "Sprite sheets for the module's Gauntlet UI from a folder of PNGs (<category>/<sprite>.png), made by the game's own generator "
         "on a private copy; stages them (sheets, SpriteData XML) for `bl install`. Nothing in the game or the module is touched.",
         [P("folder", required=True, help="a folder with one sub-folder per sprite category; the layout is in the header of sdk/sprites.py"), STAGE,
          P("timeout", "int", "seconds the generator may take", default=300)], writes=True)
def ui_sprites(a):
    import sprites
    c = _cfg(); mod = _module_dir(); stage = a.get("stage") or "work"; dry = bool(a.get("dry"))
    if not dry: c.require_game()
    try:
        res = sprites.build(os.path.abspath(a["folder"]), os.path.basename(os.path.normpath(mod)), os.path.join(c.SDK_STAGE_DIR, stage),
                            os.path.join(c.SDK_STAGE_DIR, "_sprites", stage), os.path.join(c.GAME, sprites.BIN), c.MODULES, dry=dry, timeout=int(a.get("timeout") or 300), log=print)
    except sprites.SpriteError as e:
        err = BlError(str(e), e.hint); err.result = {"problems": e.problems}; raise err
    if os.path.isdir(mod) and not os.path.isdir(os.path.join(mod, "AssetSources")):
        res["warnings"].append("the module has no AssetSources folder, and the game reads a module's Assets folder only if it has one")
    return res


# ---------------------------------------------------------------- scenes

SCENE = P("scene", help="a scene folder, or the id of a scene in the stage, the project, the module or the game", required=True)


@command("scene.list", "The battle scenes the game ships, by terrain type: templates to build from.", [P("terrain", help="Plain, Steppe, Desert, Swamp"), P("limit", "int", default=60)])
def scene_list(a):
    import catalog
    cat = catalog.load(log=lambda *x: None)
    rows = [{"id": r["id"], "terrain": r["terrain"], "forest": r["forest"], "nodes": [r["nx"], r["ny"]], "node_size": r["node_size"], "layers": len(r["layers"])}
            for r in cat.records if not a.get("terrain") or str(r["terrain"]).lower() == a["terrain"].lower()]
    return {"count": len(rows), "scenes": rows[:int(a.get("limit") or 60)]}


@command("scene.info", "What a scene holds: size, heights, texture layers, entities by prefab, navmesh, spawn paths.", [SCENE])
def scene_info(a):
    import collections, scene, terrain, navmesh
    f = _scene_folder(a["scene"]); doc = scene.load(os.path.join(f, "scene.xscene"))
    es = doc.entities(True); info = terrain.scene_terrain_info(os.path.join(f, "scene.xscene"))
    by = collections.Counter(e.prefab or ("(mesh) " + (e.name or "")) for e in es)
    paths = doc.root.find("Paths")
    out = {"folder": f, "name": doc.root.get("name"), "entities": len(es), "top_level_entities": len(doc.entities()),
           "prefabs": [{"prefab": k, "count": v} for k, v in by.most_common(25)], "paths": [p.get("name") for p in paths.elements("path")] if paths is not None else [],
           "files": {fn: os.path.getsize(os.path.join(f, fn)) for fn in sorted(os.listdir(f)) if os.path.isfile(os.path.join(f, fn))}}
    if info:
        out["terrain"] = {"size_m": [info["nx"] * info["node_size"], info["ny"] * info["node_size"]], "nodes": [info["nx"], info["ny"]],
                          "heights_m": [info["min"], info["max"]], "layers": info["layers"]}
    nm = os.path.join(f, "navmesh.bin")
    if os.path.isfile(nm) and os.path.getsize(nm): out["navmesh"] = navmesh.NavMesh(nm).stats()
    return out


def _entity_row(i, e):
    import math
    r = e.rotation
    return {"index": i, "prefab": e.prefab, "name": e.name, "position": e.position, "rotation_deg": [round(math.degrees(x), 2) for x in r] if r else None,
            "scale": e.scale, "tags": list(e.tags)}


@command("scene.entities", "The entities of a scene, optionally only those whose prefab or name contains a text.",
         [SCENE, P("match", help="text in the prefab or the name"), P("limit", "int", default=100)])
def scene_entities(a):
    import scene
    doc = scene.load(os.path.join(_scene_folder(a["scene"]), "scene.xscene")); m = (a.get("match") or "").lower()
    rows = [_entity_row(i, e) for i, e in enumerate(doc.entities()) if not m or m in ((e.prefab or "") + " " + (e.name or "")).lower()]
    return {"count": len(rows), "entities": rows[:int(a.get("limit") or 100)]}


def _ground(folder, x, y):
    import terrain
    info = terrain.scene_terrain_info(os.path.join(folder, "scene.xscene"))
    z = terrain.Terrain(os.path.join(folder, "terrain.bin")).heightmap(info); cell = info["node_size"] / 256.0
    j = min(z.shape[0] - 1, max(0, int(round(y / cell)))); i = min(z.shape[1] - 1, max(0, int(round(x / cell))))
    return float(z[j, i])


@command("scene.ground", "The terrain height at a spot of a scene, in metres.", [SCENE, P("x", "number", required=True), P("y", "number", required=True)])
def scene_ground(a):
    f = _scene_folder(a["scene"]); return {"x": a["x"], "y": a["y"], "z": round(_ground(f, float(a["x"]), float(a["y"])), 3)}


@command("scene.add_entity", "Places a prefab (or a mesh by name) in a scene.",
         [SCENE, P("prefab", help="a prefab name (see scene.info of a shipped scene for names in use)"), P("mesh", help="instead of a prefab: a metamesh name"),
          P("position", help="x y z in metres; with --on-ground only x y", required=True), P("on_ground", "bool", "take z from the terrain (plus the z given, if any)"),
          P("rotation", help="x y z in degrees; applied as Rz . Rx . Ry, so the third number is the turn around the vertical"), P("scale", help="x y z"), P("name"), P("tags", "list"),
          P("physics", help="a collision shape for the entity (a shipped one, or one staged with asset.collision)")], writes=True)
def scene_add_entity(a):
    import scene
    if not a.get("prefab") and not a.get("mesh") and not a.get("physics"): raise BlError("give --prefab, --mesh or --physics")
    f = _scene_folder(a["scene"]); _writable_scene(f) if not a.get("dry") else None
    pos = [float(x) for x in str(a["position"]).replace(",", " ").split()] if not isinstance(a["position"], (list, tuple)) else [float(x) for x in a["position"]]
    if a.get("on_ground"):
        if len(pos) not in (2, 3): raise BlError("--position takes x y (and an optional height above the ground) with --on-ground")
        pos = [pos[0], pos[1], _ground(f, pos[0], pos[1]) + (pos[2] if len(pos) == 3 else 0.0)]
    elif len(pos) != 3: raise BlError("--position takes x y z")
    doc = scene.load(os.path.join(f, "scene.xscene"))
    import math
    rot = _floats(a.get("rotation"), 3, "--rotation") or (0.0, 0.0, 0.0)
    kw = {"position": tuple(pos), "rotation": tuple(math.radians(x) for x in rot), "tags": tuple(a.get("tags") or ())}     # the file holds radians
    if a.get("scale"): kw["scale"] = _floats(a["scale"], 3, "--scale")
    if a.get("name"): kw["name"] = a["name"]
    if a.get("physics"): kw["physics"] = a["physics"]
    if a.get("mesh"): kw["mesh"] = a["mesh"]; kw.setdefault("name", a["mesh"])
    elif a.get("prefab"): kw["prefab"] = a["prefab"]
    doc.add_entity(**kw)
    if not a.get("dry"): doc.save()
    return {"scene": f, "added": {"prefab": a.get("prefab"), "mesh": a.get("mesh"), "position": [round(x, 3) for x in pos]}, "entities": len(doc.entities(True)), "saved": not a.get("dry")}


@command("scene.remove_entities", "Removes the top-level entities whose prefab or name contains a text.", [SCENE, P("match", required=True)], writes=True)
def scene_remove_entities(a):
    import scene
    f = _scene_folder(a["scene"]); _writable_scene(f) if not a.get("dry") else None
    doc = scene.load(os.path.join(f, "scene.xscene")); m = a["match"].lower()
    hit = [e for e in doc.entities() if m in ((e.prefab or "") + " " + (e.name or "")).lower()]
    for e in hit: doc.remove_entity(e)
    if not a.get("dry"): doc.save()
    return {"scene": f, "removed": len(hit), "entities": len(doc.entities(True)), "saved": not a.get("dry")}


@command("scene.copy", "Copies a scene (a shipped one, for example) to the stage under a new name, ready to change and install.",
         [SCENE, P("name", help="the new scene id", required=True), P("to", help="'stage' (default) or 'project'", default="stage", choices=("stage", "project"))], writes=True)
def scene_copy(a):
    import scene
    src = _scene_folder(a["scene"]); c = _cfg()
    dst = os.path.join(_need_project()[0], "SceneObj", a["name"]) if a.get("to") == "project" else os.path.join(c.SDK_STAGE_DIR, "work", "SceneObj", a["name"])
    if os.path.exists(dst): raise BlError("%s exists already" % dst)
    if not a.get("dry"):
        shutil.copytree(src, dst)
        doc = scene.load(os.path.join(dst, "scene.xscene")); doc.root.set("name", a["name"]); doc.save()
    return {"from": src, "folder": dst, "copied": not a.get("dry")}


@command("scene.check", "Rebuilds a scene's files from their decoded content and compares: is the folder something the SDK fully understands.", [SCENE])
def scene_check(a):
    import scene, terrain, navmesh
    f = _scene_folder(a["scene"]); out = {"folder": f}
    out["scene.xscene"] = bool(scene.check(os.path.join(f, "scene.xscene")))
    tb = os.path.join(f, "terrain.bin")
    if os.path.isfile(tb) and os.path.getsize(tb):
        ok, det = terrain.check(tb, log=lambda *x: LOG.append(" ".join(str(y) for y in x)))
        out["terrain.bin"] = bool(ok); out["terrain_detail"] = {k: list(v) for k, v in det.items()}
    nm = os.path.join(f, "navmesh.bin")
    if os.path.isfile(nm) and os.path.getsize(nm): out["navmesh.bin"] = bool(navmesh.check(nm))
    out["all_identical"] = all(v for k, v in out.items() if k.endswith((".xscene", ".bin")))
    return out


@command("scene.preview", "A top view of a scene as a PNG: relief, texture layers, rocks, trees, spawn paths, the walkable area.",
         [SCENE, P("out", help="the PNG to write", required=True), P("size", "int", default=1024), P("season", default="summer", choices=("summer", "fall", "winter", "spring"))])
def scene_preview(a):
    import preview
    res = preview.scene_map(_scene_folder(a["scene"]), a["out"], int(a.get("size") or 1024), a.get("season") or "summer"); res["png"] = os.path.abspath(a["out"]); return res


@command("scene.heightmap", "Writes a scene's heights as a 16-bit PNG with a .json that says what the grey values mean.", [SCENE, P("out", required=True)])
def scene_heightmap(a):
    import heightmap
    res = heightmap.export(_scene_folder(a["scene"]), a["out"]); res["png"] = os.path.abspath(a["out"]); return res


@command("scene.from_heightmap", "A playable scene from a heightmap image (PNG, .npy, .r16): terrain, navmesh, spawn paths, walkable area.",
         [P("image", required=True), P("name", help="the scene id", required=True), P("template", help="a shipped scene id or a scene folder: layers, atmosphere and plants come from it", default="battle_terrain_a"),
          P("low", "number", help="height of black in metres (default: the image's .json, else 0)"), P("high", "number", help="height of white in metres (default: the .json, else low + 80)"),
          P("rock_slope", "number", help="slope (rise over run) above which the rock layer is painted", default=0.75), P("play_radius", "number", help="radius of the walkable area in metres"),
          P("water_level", "number"), P("smooth", "number", help="blur radius in metres", default=0.0),
          P("cover", "number", help="share of the gentle ground under grass and plants, 0..1 (default: as much as the template's own middle; 0 for none)"),
          P("seed", "int", help="the grass patches' random seed", default=1),
          P("to", help="'stage' (default: <stage>/work/SceneObj/<name>, picked up by `bl install`), 'project' (the project's SceneObj, picked up by `bl deploy`) or a folder", default="stage")], writes=True)
def scene_from_heightmap(a):
    import heightmap
    to = a.get("to") or "stage"; c = _cfg(); c.require_game()
    out = os.path.join(c.SDK_STAGE_DIR, "work", "SceneObj", a["name"]) if to == "stage" else os.path.join(_need_project()[0], "SceneObj", a["name"]) if to == "project" else os.path.abspath(to)
    if a.get("dry"): return {"would_write": out, "template": a.get("template") or "battle_terrain_a"}
    try:
        return heightmap.to_scene(a["image"], a["name"], a.get("template") or "battle_terrain_a", a.get("low"), a.get("high"), out, float(a.get("rock_slope") or 0.75),
                                  a.get("play_radius"), a.get("water_level"), float(a.get("smooth") or 0.0), a.get("cover"), int(a.get("seed") or 1),
                                  log=lambda *x: LOG.append(" ".join(str(y) for y in x)))
    except ValueError as e: raise BlError(str(e))


@command("prefab.add", "Defines a prefab in the project (Prefabs/<id>_prefabs.xml): a model, a collision body, or both. Scenes and `bl live spawn` can then place it.",
         [P("name", help="the prefab's name", required=True), P("mesh", help="a model (metamesh) name"), P("physics", help="a collision shape name"),
          P("scale", help="x y z"), P("skeleton", help="a skeleton name: the model is then skinned to it and the prefab can play clips"), P("remove", "bool")], writes=True)
def prefab_add(a):
    import scaffold
    d, pj = _need_project()
    if not a.get("remove") and not a.get("mesh") and not a.get("physics"): raise BlError("give --mesh, --physics or both")
    if a.get("dry"): return {"would_edit": os.path.join(d, "Prefabs", pj["module"].lower() + "_prefabs.xml")}
    return {"prefabs": scaffold.add_prefab(d, pj["module"], a["name"], a.get("mesh"), a.get("physics"), _floats(a.get("scale"), 3, "--scale"), bool(a.get("remove")), a.get("skeleton")),
            "note": "`bl deploy` copies it; the game reads it at startup"}


@command("data.set", "Changes attributes of an object the game (or another module) defines: a settlement, an item, a troop, a kingdom ... as an XSLT patch in the project.",
         [P("kind", help="the data kind (the root element): Settlements, Items, NPCCharacters, Kingdoms, Clans, Cultures ...", required=True),
          P("element", help="the object's element: Settlement, Item, NPCCharacter, Kingdom ...", required=True), P("id", help="the object's id", required=True),
          P("values", "list", help="attribute=value pairs, for example name={=!}New Name (separate several with commas; a value cannot hold a comma)", required=True),
          P("unset", "bool", help="remove these attributes from the patch instead (values may then be bare names)")], writes=True)
def data_set(a):
    import datapatch
    d, pj = _need_project(); attrs = {}
    for kv in a["values"]:
        k, _, v = kv.partition("=")
        if not k.strip() or (not _ and not a.get("unset")): raise BlError("--values takes attribute=value pairs, got %r" % kv)
        attrs[k.strip()] = None if a.get("unset") else v
    if a.get("dry"): return {"would_patch": "%s %s" % (a["element"], a["id"]), "attributes": attrs}
    try: res = datapatch.set_attrs(d, pj["module"], a["kind"], a["element"], a["id"], attrs)
    except ValueError as e: raise BlError(str(e))
    res["note"] = "`bl deploy` copies it; the game applies it when it loads the data, at the start of a campaign or battle"
    return res


@command("scene.register", "Adds a scene to (or removes it from) the project's Custom Battle list, so it can be picked and test-run.",
         [P("scene_id", required=True), P("name", help="the name shown in the list"), P("terrain", default="Plain"),
          P("kind", help="battle, siege (castle or town walls), village, lords_hall", default="battle", choices=("battle", "siege", "village", "lords_hall")), P("remove", "bool")], writes=True)
def scene_register(a):
    import scaffold
    d, pj = _need_project()
    if a.get("dry"): return {"would_edit": os.path.join(d, "ModuleData", pj["module"].lower() + "_custom_battle_scenes.xml")}
    return {"listed": scaffold.register_scene(d, pj["module"], a["scene_id"], a.get("name"), a.get("terrain") or "Plain", bool(a.get("remove")), a.get("kind") or "battle")}


# ---------------------------------------------------------------- meshes

@command("mesh.find", "Names of shipped meshes that contain a text (14,000 of them: rocks, trees, props, armour ...).", [P("text", required=True), P("limit", "int", default=60)])
def mesh_find(a):
    import meshread
    idx = meshread.index(log=lambda *x: None); t = a["text"].lower()
    hit = sorted(n for n in idx if t in n.lower())
    return {"count": len(hit), "names": hit[:int(a.get("limit") or 60)], "of": len(idx)}


MESH = P("mesh", help="a shipped mesh name, or an .obj / .fbx / .psk / .pskx file", required=True)


@command("mesh.info", "Size, triangle count and how open a mesh is (most shipped rocks are one-sided shells).", [MESH])
def mesh_info(a):
    import preview
    try: Pn, F, title = preview.load_mesh(a["mesh"])
    except KeyError as e: raise BlError(str(e).strip("'\""), "`bl mesh.find <text>` lists names")
    st = preview.mesh_stats(Pn, F); st["name"] = title; return st


@command("mesh.preview", "Six views of a mesh on one PNG sheet; faces seen from behind are red.", [MESH, P("out", required=True), P("size", "int", help="pixels per view", default=384)])
def mesh_preview(a):
    import preview
    try: Pn, F, title = preview.load_mesh(a["mesh"])
    except KeyError as e: raise BlError(str(e).strip("'\""), "`bl mesh.find <text>` lists names")
    st = preview.mesh_sheet(Pn, F, a["out"], int(a.get("size") or 384), title); st["png"] = os.path.abspath(a["out"]); return st


@command("mesh.export", "Writes a shipped mesh as an .obj file (positions and triangles).", [P("mesh", required=True), P("out", required=True)])
def mesh_export(a):
    import meshread
    try: meshread.to_obj(a["mesh"], a["out"])
    except KeyError as e: raise BlError(str(e).strip("'\""), "`bl mesh.find <text>` lists names")
    return {"obj": os.path.abspath(a["out"]), "bytes": os.path.getsize(a["out"])}


@command("physics.info", "The collision bodies (physics shapes) in a .tpac package: capsules, spheres, and triangle manifolds with their sizes.",
         [P("package", help="a .tpac file", required=True), P("match", help="only shapes whose name contains this"), P("limit", "int", default=50)])
def physics_info(a):
    import physics
    if not os.path.isfile(a["package"]): raise BlError("no file %s" % a["package"])
    rows = []; m = (a.get("match") or "").lower()
    for res in physics.resources(a["package"]):
        if m and m not in res["name"].lower(): continue
        d = physics.decode_resource(res)["desc"]
        mans = d.get("manifolds") or []
        def count(x, k):
            v = x.get(k) if isinstance(x, dict) else None
            return len(v) if v is not None else None
        rows.append({"name": res["name"], "capsules": len(d.get("capsules") or []), "spheres": len(d.get("spheres") or []),
                     "manifolds": [{"vertices": count(x, "vertices"), "faces": count(x, "face_index"), "materials": x.get("materials") if isinstance(x, dict) else None} for x in mans]})
        if len(rows) >= int(a.get("limit") or 50): break
    return {"package": os.path.abspath(a["package"]), "shapes": rows,
            "note": "shapes of capsules and spheres can be written from scratch; hulls and triangle meshes need PhysX cooking, which the SDK cannot do yet"}


# ---------------------------------------------------------------- the campaign map

@command("worldmap.at", "What the campaign map has at a position: height, terrain type, the mix of terrain around it.",
         [P("x", "number", required=True), P("y", "number", required=True), P("size", "number", help="side of the square looked at, in map units", default=12.0)])
def worldmap_at(a):
    import worldmap
    w = worldmap.WorldMap(); x, y = float(a["x"]), float(a["y"])
    t = int(w.terrain_type_at(x, y))
    return {"x": x, "y": y, "height": w.height_at(x, y), "terrain": worldmap.TERRAIN_TYPES.get(t, str(t)), "around": w.terrain_mix((x, y), float(a.get("size") or 12.0))}


@command("worldmap.settlements", "Settlements of the campaign map with their positions.", [P("match", help="text in the id or name"), P("limit", "int", default=80)])
def worldmap_settlements(a):
    import worldmap
    rows = worldmap.WorldMap().settlements(); m = (a.get("match") or "").lower()
    rows = [r for r in rows if not m or m in json.dumps(_plain(r)).lower()]
    return {"count": len(rows), "settlements": rows[:int(a.get("limit") or 80)]}


# ---------------------------------------------------------------- scratch scene slots

PREFIX = P("prefix", help="the slots are SceneObj/<prefix>_1, <prefix>_2, ...", default="slot")


@command("slots.create", "Makes empty scene folders in the module that can be filled while the game runs (one scene a slot a session).",
         [PREFIX, P("count", "int", required=True), P("template", help="a shipped scene id or a scene folder: its atmosphere and flora files are copied", default="battle_terrain_a")], writes=True)
def slots_create(a):
    import slots, heightmap
    m = _module_dir()
    if not a.get("dry"): _guard_game()
    try: new = slots.create(m, a.get("prefix") or "slot", int(a["count"]), heightmap.template_folder(a.get("template") or "battle_terrain_a"), bool(a.get("dry")))
    except ValueError as e: raise BlError(str(e))
    return {"module": m, "new": new, "total": len(slots.names(m, a.get("prefix") or "slot")) + (len(new) if a.get("dry") else 0),
            "note": "new folders count after the next start of the game; list one in Custom Battle with scene.register to test it by hand"}


@command("slots.state", "Every slot as empty, filled, or held by the running game.", [PREFIX])
def slots_state(a):
    import slots
    m = _module_dir(); st = slots.state(m, a.get("prefix") or "slot")
    return {"module": m, "slots": st, "free": [s for s, v in st.items() if v != "held"], "held": [s for s, v in st.items() if v == "held"]}


@command("slots.fill", "Copies a scene into a slot. Works while the game runs, as long as the slot has not been loaded this session.",
         [P("slot", required=True), SCENE], writes=True)
def slots_fill(a):
    import slots
    m = _module_dir(); f = _scene_folder(a["scene"])
    if a.get("dry"): return {"would_fill": slots.folder(m, a["slot"]), "from": f, "held": slots.held(m, a["slot"]) if os.path.isdir(slots.folder(m, a["slot"])) else None}
    try: d = slots.fill(m, a["slot"], f)
    except ValueError as e: raise BlError(str(e))
    cache = _shader_cache(os.path.basename(os.path.normpath(m)), a["slot"])
    if os.path.isdir(cache) and not _cfg().game_running(): shutil.rmtree(cache)
    return {"slot": d, "from": f, "load_as": a["slot"]}


@command("slots.reset", "Empties every filled slot (game closed).", [PREFIX], writes=True)
def slots_reset(a):
    import slots
    m = _module_dir()
    if a.get("dry"): return {"would_empty": [s for s, v in slots.state(m, a.get("prefix") or "slot").items() if v == "filled"]}
    _guard_game()
    try: return {"emptied": slots.reset(m, a.get("prefix") or "slot")}
    except ValueError as e: raise BlError(str(e))


# ---------------------------------------------------------------- publishing

@command("workshop.config", "Writes the file the game's own Steam Workshop uploader takes (a new item, or an update of one). Does not upload.",
         [P("description", help="the item's description text", required=True), P("image", help="the preview picture (png or jpg)", required=True),
          P("tags", "list", help="Workshop tags, for example Singleplayer,Maps"), P("visibility", default="Private", choices=("Private", "FriendsOnly", "Public")),
          P("change_notes", default=""), P("item_id", help="the id of an existing Workshop item to update (without it a new item is created)"),
          P("out", help="the file to write (default: <project>/workshop/upload.xml)")], writes=True)
def workshop_config(a):
    from xml.sax.saxutils import quoteattr
    c = _cfg(); d, pj = _project(); m = _module_dir()
    out = os.path.abspath(a.get("out") or os.path.join(d or ".", "workshop", "upload.xml"))
    img = os.path.abspath(a["image"])
    if not os.path.isfile(img): raise BlError("no picture at %s" % img)
    # The uploader takes the tasks from the first node of the file, so the file must not start with an XML declaration.
    first = '\t<GetItem>\n\t\t<ItemId Value=%s/>\n\t</GetItem>\n' % quoteattr(str(a["item_id"])) if a.get("item_id") else "\t<CreateItem/>\n"
    tags = "".join("\t\t\t<Tag Value=%s/>\n" % quoteattr(t) for t in (a.get("tags") or ()))
    text = ("<Tasks>\n" + first + "\t<UpdateItem>\n\t\t<ModuleFolder Value=%s/>\n\t\t<ItemDescription Value=%s/>\n\t\t<Tags>\n%s\t\t</Tags>\n"
            "\t\t<Image Value=%s/>\n\t\t<ChangeNotes Value=%s/>\n\t\t<Visibility Value=%s/>\n\t</UpdateItem>\n</Tasks>\n") % (
        quoteattr(m), quoteattr(a["description"]), tags, quoteattr(img), quoteattr(a.get("change_notes") or ""), quoteattr(a.get("visibility") or "Private"))
    if not a.get("dry"):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f: f.write(text)
    exe = os.path.join(c.GAME, "bin", "Win64_Shipping_Client", "TaleWorlds.MountAndBlade.SteamWorkshop.exe")
    return {"file": out, "written": not a.get("dry"), "module_folder": m, "module_installed": os.path.isfile(os.path.join(m, "SubModule.xml")),
            "creates_new_item": not a.get("item_id"), "visibility": a.get("visibility") or "Private",
            "to_upload": 'run "%s" "%s" yourself, with Steam running: it publishes under the logged-in Steam account; the item takes its title from the module name' % (exe, out)}


# ---------------------------------------------------------------- the running game

@command("live", "Sends one command to the running game through the project's bridge (Bridge.cs) and returns the answer. "
         "Commands: ping, state, ground, raycast, teleport, look, screenshot, spawn, remove, animate, bone, settlement, sprite, say, quit.",
         [P("cmd", help="the command", required=True), P("values", "list", help="key=value pairs, for example prefab=rock_cliff_b,x=600,y=640"),
          P("timeout", "number", default=10.0)])
def live(a):
    import bridge
    if a["cmd"] not in bridge.COMMANDS: raise BlError("no bridge command %s" % a["cmd"], "it knows: " + ", ".join(bridge.COMMANDS))
    m = _module_dir(); vals = {}
    for kv in a.get("values") or ():
        if "=" not in kv: raise BlError("--values takes key=value pairs, got %r" % kv)
        k, v = kv.split("=", 1); vals[k.strip()] = v
    if not _cfg().game_processes(): raise BlError("the game is not running", "enable the bridge (`bl live.enable`), then start the game with the module")
    try: return bridge.send(m, a["cmd"], vals, float(a.get("timeout") or 10.0))
    except TimeoutError as e: raise BlError(str(e), "is the bridge on (`bl live.enable` before the game starts), and is the game past its loading screen?")
    except (RuntimeError, ValueError) as e: raise BlError(str(e))


@command("live.enable", "Makes the module listen for `bl live` commands from its next start on (or stops it with --off).", [P("off", "bool")], writes=True)
def live_enable(a):
    import bridge
    m = _module_dir()
    if a.get("dry"): return {"would_set": os.path.join(bridge.folder(m), "on"), "to": not a.get("off")}
    if not os.path.isdir(m): raise BlError("%s does not exist yet" % m, "`bl deploy` first")
    return {"listening_from_next_start": bridge.enable(m, not a.get("off")), "folder": bridge.folder(m)}


# ---------------------------------------------------------------- a test run in the game

@command("run", "One unattended test run in the game: writes the test words, starts the game silently, waits for the end marker, collects the log.",
         [P("words", help='the test words the module understands, for example "scene=my_map tod=12 fight secs=30 quit"', required=True),
          P("launch", "bool", "really start the game (without it, and without --dry, nothing happens)"), P("campaign", "bool", "add the Sandbox module"),
          P("timeout", "int", default=420), P("tag", help="the name of the result folder under <SDK home>\\out"), P("log_file", help="the module's log (default: <module>\\<id>_log.txt)"),
          P("module_name", help="the module id (default: the project's)")], writes=True, launches=True)
def run_(a):
    c = _cfg(); d, pj = _project(); mod = a.get("module_name") or (pj["module"] if pj else None) or (os.path.basename(os.path.normpath(c.BANNERLORD_MODULE_DIR)) if c.BANNERLORD_MODULE_DIR else None)
    if not mod: raise BlError("no module name: pass --module_name or run inside a project")
    if not a.get("dry") and not a.get("launch"):
        raise BlError("this starts the game: pass --launch to do that, or --dry to check the harness without the game")
    if not a.get("dry"): _guard_game()
    log = a.get("log_file") or os.path.join(c.MODULES, mod, mod.lower() + "_log.txt")
    tag = a.get("tag") or "%s_%s" % (mod.lower(), time.strftime("%m%d_%H%M%S"))
    ps = ["powershell", "-ExecutionPolicy", "Bypass", "-File", os.path.join(os.path.dirname(HERE), "harness", "night_test.ps1"), "-Module", mod, "-Words", a["words"],
          "-LogFile", log, "-Tag", tag, "-TimeoutSec", str(int(a.get("timeout") or 420)), "-NoRetry"]
    if a.get("campaign"): ps.append("-Campaign")
    if a.get("dry"): ps.append("-DryRun")
    r = subprocess.run(ps, capture_output=True, text=True)
    LOG.extend((r.stdout + r.stderr).splitlines()[-40:])
    out_dir = os.path.join(c.SDK_HOME, "out", tag); res = {}
    try:
        with open(os.path.join(out_dir, "result.json"), encoding="utf-8-sig") as f: res = json.load(f)
    except (OSError, ValueError):
        pass
    return {"tag": tag, "folder": out_dir, "exit_code": r.returncode, "passed": r.returncode == 0, "dry": bool(a.get("dry")), "result": res,
            "note": "a dry run ends in 'fail' by design: it checks the harness, not the game" if a.get("dry") else None}


# ---------------------------------------------------------------- the command line

GLOBAL = {"--game": "BANNERLORD_DIR", "--module": "BANNERLORD_MODULE_DIR", "--home": "BANNERLORD_SDK_HOME", "--project": "BL_PROJECT", "--stage-dir": "SDK_STAGE_DIR"}


def parse(argv):
    """(command name, argument dict, pretty). Raises BlError on anything unclear."""
    argv = list(argv); pretty = False; rest = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t in GLOBAL:
            if i + 1 >= len(argv): raise BlError("%s needs a value" % t)
            os.environ[GLOBAL[t]] = os.path.abspath(argv[i + 1]) if t != "--module" or os.sep in argv[i + 1] or "/" in argv[i + 1] else argv[i + 1]; i += 2
        elif t == "--pretty": pretty = True; i += 1
        else: rest.append(t); i += 1
    if not rest or rest[0] in ("-h", "--help", "help"): return None, {}, pretty
    name = None
    if len(rest) >= 2 and rest[0] + "." + rest[1].replace("-", "_") in COMMANDS: name = rest[0] + "." + rest[1].replace("-", "_"); rest = rest[2:]
    elif rest[0].replace("-", "_") in COMMANDS: name = rest[0].replace("-", "_"); rest = rest[1:]
    else: raise BlError("unknown command %s" % " ".join(rest[:2]), "`bl commands` lists them")
    CURRENT[0] = name
    cmd = COMMANDS[name]; by = {p["name"]: p for p in cmd["params"]}; args = {}; pos = [p for p in cmd["params"] if p["required"]]
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--args" or t == "--args-file":
            if i + 1 >= len(rest): raise BlError("%s needs a value" % t)
            raw = rest[i + 1] if t == "--args" else open(rest[i + 1], encoding="utf-8-sig").read()
            try: given = json.loads(raw)
            except ValueError as e: raise BlError("--args is not JSON: %s" % e)
            for k, v in given.items():
                if k not in by: raise BlError("%s has no argument %s" % (name, k), "it takes: " + ", ".join(by))
                args[k] = v
            i += 2
        elif t.startswith("--"):
            k = t[2:].replace("-", "_")
            if k not in by: raise BlError("%s has no option %s" % (name, t), "it takes: " + ", ".join("--" + x for x in by))
            p = by[k]
            if p["kind"] == "bool":
                if i + 1 < len(rest) and rest[i + 1].lower() in ("true", "false"): args[k] = rest[i + 1].lower() == "true"; i += 2
                else: args[k] = True; i += 1
                continue
            if i + 1 >= len(rest): raise BlError("%s needs a value" % t)
            v = rest[i + 1]; i += 2
            if p["kind"] == "list": args.setdefault(k, []).extend(x for x in v.split(",") if x)
            else: args[k] = v
        else:
            free = [p for p in pos if p["name"] not in args]
            if not free: raise BlError("%s: unexpected value %r" % (name, t), "it takes: " + ", ".join(("" if p["required"] else "--") + p["name"] for p in cmd["params"]))
            args[free[0]["name"]] = t; i += 1
    for p in cmd["params"]:
        k = p["name"]
        if k not in args:
            if p["required"]: raise BlError("%s needs %s" % (name, k), p["help"] or None)
            if p["default"] is not None: args[k] = p["default"]
            continue
        v = args[k]
        try:
            if p["kind"] == "int": args[k] = int(v)
            elif p["kind"] == "number": args[k] = float(v)
            elif p["kind"] == "bool": args[k] = v if isinstance(v, bool) else str(v).lower() == "true"
            elif p["kind"] == "json" and isinstance(v, str): args[k] = json.loads(v)
            elif p["kind"] == "list" and isinstance(v, str): args[k] = [x for x in v.split(",") if x]
        except ValueError:
            raise BlError("%s: %s is not %s: %r" % (name, k, {"int": "a whole number", "number": "a number", "json": "JSON"}.get(p["kind"], p["kind"]), v))
        if p["choices"] and args[k] not in p["choices"]: raise BlError("%s: %s is one of %s" % (name, k, ", ".join(map(str, p["choices"]))))
    return name, args, pretty


class _Tee(io.TextIOBase):
    """Collects what the wrapped tools print, line by line, into LOG."""
    def __init__(self): self.buf = ""
    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip(): LOG.append(line.rstrip())
        return len(s)
    def flush(self): pass


def run(argv):
    """Runs one command. Returns (exit code, the JSON-ready answer)."""
    del LOG[:]; name = None; CURRENT[0] = None
    try:
        name, args, pretty = parse(argv)
        if name is None: return 0, {"ok": True, "command": "help", "result": {"usage": __doc__.strip().splitlines(), "commands": {c["name"]: c["summary"] for c in COMMANDS.values()}}, "log": []}, True
        tee = _Tee()
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
            _cfg()                                            # settle the paths before any SDK module reads them
            try: res = COMMANDS[name]["func"](args)
            except SystemExit as e:                            # library code that gives up with sys.exit("why")
                raise BlError(str(e.code) if isinstance(e.code, str) else "the tool stopped with exit code %s" % e.code)
        return 0, {"ok": True, "command": name, "result": _plain(res), "log": list(LOG)}, pretty
    except BlError as e:
        out = {"ok": False, "command": name or CURRENT[0], "error": str(e), "log": list(LOG)}
        if e.hint: out["hint"] = e.hint
        if getattr(e, "result", None): out["result"] = _plain(e.result)
        return 1, out, "--pretty" in argv
    except Exception as e:                                     # a bug or an unreadable file: still one JSON object
        import traceback
        return 1, {"ok": False, "command": name, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc().splitlines()[-6:], "log": list(LOG)}, "--pretty" in argv


def main(argv=None):
    code, out, pretty = run(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(json.dumps(out, indent=2 if pretty else None, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
