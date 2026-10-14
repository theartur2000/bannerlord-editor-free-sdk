"""UI sprites: the sprite sheets of a module's Gauntlet UI, made from a folder of PNGs with the game's own generator.

    bl ui.sprites <folder> [--stage work] [--dry]        then   bl install

Input layout (the folder you pass is the module's `SpriteParts` folder of the Modding Kit):

    <folder>/<category>/<sprite>.png            a sprite. The category folder names the sheets: <category>_1, <category>_2, ...
    <folder>/<category>/<group>/<sprite>.png    nested folders are part of the sprite name: "<group>\\<sprite>" (4 levels at most)
    <folder>/<category>/<sprite>.xml            optional, next to the PNG: also makes a nine-region sprite from it
        <NineRegionSprite><Name>btn</Name><LeftWidth>12</LeftWidth><RightWidth>12</RightWidth><TopHeight>12</TopHeight><BottomHeight>12</BottomHeight></NineRegionSprite>
        (its Name must differ from the PNG's: the PNG stays a sprite of its own)
    <folder>/Config.xml                         optional packing settings per category, with no <?xml ?> line:
        <Config><Category Name="ui_mymod"><EdgeSize Value="2"/><SingleChannel/></Category></Config>
        children: EdgeSize Value=n (padding around each sprite, default 4), SingleChannel (one grey channel, for masks and fonts),
        NoAlphaChannel (opaque RGB), PackAllSpritesToUniqueTextures (one sheet per sprite, the sprite's own size), AlwaysLoad.
    A PNG straight in <folder> is refused: the generator would look for it in a folder "Default" and crash.

Sprite and category names are what the game looks up (brush and prefab XML name a sprite; the engine loads the texture
"<category>_<n>"), so they must not repeat a name any other module's GUI/*SpriteData.xml already defines. Prefix them with your module.

What it stages (a stage is a folder `bl install` copies into the module):
    Assets/<category>_<n>_tex.tpac + RuntimeDataCache/*.rdc    one texture per sheet, 1 mip, the import flags the game's sheets carry
    GUI/<module id>SpriteData.xml                              where the sprites sit on the sheets; the game reads every GUI/*SpriteData.xml
The module id is the folder name of the module being built (--module, or the project's module).

How the sheets are made: TaleWorlds.TwoDimension.SpriteSheetGenerator.exe ends in Console.ReadKey(), which throws when stdin is not a
console, and it never closes its output files, so killed or crashed it leaves empty PNGs. The SDK therefore loads the same
TaleWorlds.TwoDimension.SpriteSheetGenerator.Library.dll in a Windows PowerShell and calls the exe's entry point
(SpriteEditorDomain.Start) with the exe's arguments (CollectionType=SingleModule, OutputType=Engine), then forces the finalizers
that flush the files. Measured and read from its code: it touches only the source module folder and that folder's PARENT
(AssetSources\\GauntletUI is emptied and refilled there), reads no registry and no game folder, and needs no native DLL. So the
SDK always runs it on a private copy, <SDK_STAGE_DIR>/_sprites/<stage>/gen/<module id>, never on a real module or on the game's
Modules folder; run_generator refuses any other place.
Sheet format: BC7 with alpha, DXT1 for NoAlphaChannel, R8 for SingleChannel; a sheet whose size is not a multiple of 4 (only the
unique-texture packing makes those) is stored as uncompressed RGBA8.
"""
import glob, json, os, re, shutil, struct, subprocess, sys, xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LIB = "TaleWorlds.TwoDimension.SpriteSheetGenerator.Library.dll"
BIN = os.path.join("bin", "Win64_Shipping_wEditor")          # where the game keeps the generator
IMPORT_FLAGS = ("dont_degrade", "dont_delay_loading")        # the import flags of the game's own sheet textures
MAX_SHEET = 4096                                             # the generator's sheet size before it shrinks to what is used
CATEGORY = re.compile(r"^[A-Za-z0-9_]+$"); NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# The exe's own steps without its ReadKey: the same configuration struct, the same entry point. The collections are made
# unreachable and the finalizers run, because the generator never disposes the FileStreams it writes the PNGs with.
GEN_PS1 = r"""$ErrorActionPreference = "Stop"
Add-Type -Path (Join-Path $env:SPRITEGEN_BIN "TaleWorlds.TwoDimension.SpriteSheetGenerator.Library.dll")
$L = "TaleWorlds.TwoDimension.SpriteSheetGenerator.Library"
$c = New-Object "$L.SpriteEditorDomainConfiguration"
$c.SourceDirectoryPath = $env:SPRITEGEN_SRC
$c.OutputType = [Enum]::Parse([TaleWorlds.TwoDimension.SpriteSheetGenerator.Library.SpriteSheetOutputType], $env:SPRITEGEN_OUT)
$c.CollectionType = [TaleWorlds.TwoDimension.SpriteSheetGenerator.Library.SpriteSheetCollectionType]::SingleModule
[Console]::WriteLine($c.ToString())
$ok = [TaleWorlds.TwoDimension.SpriteSheetGenerator.Library.SpriteEditorDomain]::Start($c)
[GC]::Collect(); [GC]::WaitForPendingFinalizers()
[Console]::WriteLine("GENERATOR RETURNED $ok")
"""


class SpriteError(Exception):
    def __init__(self, message, problems=(), hint=None):
        Exception.__init__(self, message); self.problems = list(problems); self.hint = hint


def _inside(path, root):
    p, r = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(root))
    return p.startswith(r.rstrip(os.sep) + os.sep)


def png_size(path, complete=False):
    """(width, height) from the IHDR chunk; raises SpriteError for a file that is not a PNG, and with complete=True for one that ends
    before its IEND chunk (what a generator that died before flushing leaves)."""
    with open(path, "rb") as f: head = f.read(24); f.seek(max(0, os.path.getsize(path) - 12)); tail = f.read(12)
    if head[:8] != PNG_MAGIC or head[12:16] != b"IHDR": raise SpriteError("%s is not a PNG" % path)
    if complete and tail[4:8] != b"IEND": raise SpriteError("%s is cut short (no PNG end chunk)" % path)
    return struct.unpack(">II", head[16:24])


# ---------------------------------------------------------------- the input folder

def read_config(path):
    """({category: settings}, problems) of a Config.xml, read the way the generator reads it. That is not forgiving: a file with
    an XML declaration, a category without Name or a bad EdgeSize is dropped as a whole with only a console line."""
    out, problems = {}, []
    if not os.path.isfile(path): return out, problems
    text = open(path, encoding="utf-8-sig").read()
    if re.match(r"\s*<\?xml", text):
        return out, ["Config.xml starts with an <?xml ?> line: the generator then reads that line as the document and ignores every setting (remove the line)"]
    try: root = ET.fromstring(text)
    except ET.ParseError as e: return out, ["Config.xml is not XML (the generator would ignore it): %s" % e]
    for cat in root:
        name = cat.get("Name")
        if not name: problems.append("Config.xml: a child of the root without a Name attribute (the generator drops the whole file)"); continue
        s = out[name] = {"unique": False, "single": False, "noalpha": False, "always": False, "edge": 4}
        for ch in cat:
            if ch.tag == "PackAllSpritesToUniqueTextures": s["unique"] = True
            elif ch.tag == "SingleChannel": s["single"] = True
            elif ch.tag == "NoAlphaChannel": s["noalpha"] = True
            elif ch.tag == "AlwaysLoad": s["always"] = True
            elif ch.tag == "EdgeSize":
                try: s["edge"] = int(ch.get("Value"))
                except (TypeError, ValueError): problems.append("Config.xml: EdgeSize of %s needs Value=<whole number>" % name)
    return out, problems


def read_nine_region(path, part):
    """(name, left, right, top, bottom) of a nine-region XML, or a problem text. The generator swallows every fault in this file and
    silently makes a plain sprite, so a faulty file is an error here."""
    try:
        root = ET.parse(path).getroot()
        if root.tag != "NineRegionSprite": return "%s: the root element must be NineRegionSprite (anything else is ignored)" % path
        name = root.findtext("Name").strip(); v = [int(root.findtext(k)) for k in ("LeftWidth", "RightWidth", "TopHeight", "BottomHeight")]
    except (ET.ParseError, AttributeError, TypeError, ValueError, OSError) as e:
        return "%s: unreadable nine-region sprite (Name, LeftWidth, RightWidth, TopHeight, BottomHeight needed): %s" % (path, e)
    if not NAME.match(name): return "%s: Name %r may use letters, digits, _ . - only" % (path, name)
    if name == part: return "%s: Name %r is the PNG's own sprite name: the generator adds both and crashes on the duplicate" % (path, name)
    return (name,) + tuple(v)


def read_layout(folder):
    """The sprites under folder: {"categories": {cat: [sprite dicts]}, "config": {...}, "files": [(path, relative path)],
    "names": every sprite name, "problems": [...], "warnings": [...]}. A problem is anything that makes the generator crash, leaves a
    sprite out without a word or repeats a name."""
    if not os.path.isdir(folder): raise SpriteError("no folder " + folder)
    cfg, problems = read_config(os.path.join(folder, "Config.xml")); warnings = []
    cats, files, names = {}, [], {}
    if os.path.isfile(os.path.join(folder, "Config.xml")): files.append((os.path.join(folder, "Config.xml"), "Config.xml"))
    for dirpath, dirs, fns in os.walk(folder):
        dirs.sort(); rel = os.path.relpath(dirpath, folder); parts = [] if rel == "." else rel.split(os.sep)
        have = {os.path.splitext(f)[0] for f in fns if f.lower().endswith(".png")}
        for fn in sorted(fns):
            p = os.path.join(dirpath, fn); stem, ext = os.path.splitext(fn); ext = ext.lower(); shown = os.path.relpath(p, folder)
            if not parts:
                if ext == ".png": problems.append("%s sits straight in the folder: put it in a category folder (the generator looks for loose PNGs in a folder \"Default\" and crashes)" % fn)
                elif fn != "Config.xml": warnings.append("%s is ignored (only Config.xml and category folders belong here)" % fn)
                continue
            cat = parts[0]
            if ext == ".xml":
                if stem not in have: warnings.append("%s has no PNG next to it: ignored" % shown)
                continue
            if ext != ".png": warnings.append("%s is ignored (only .png and nine-region .xml files count)" % shown); continue
            if len(parts) + 1 > 5: problems.append("%s is too deep: the generator allows the category, 3 nested folders and the file" % shown); continue
            if not CATEGORY.match(cat): problems.append("category folder %r: letters, digits and _ only (it becomes the texture name %s_1)" % (cat, cat)); continue
            name = "\\".join(parts[1:] + [stem])
            if not all(NAME.match(x) for x in parts[1:] + [stem]): problems.append("%s: names may use letters, digits, _ . - only" % shown); continue
            try: w, h = png_size(p)
            except SpriteError as e: problems.append(str(e)); continue
            s = {"name": name, "category": cat, "path": p, "width": w, "height": h, "nine": None}
            c = cfg.get(cat, {}); edge = c.get("edge", 4)
            if not c.get("unique") and (w + 2 * edge > MAX_SHEET or h + 2 * edge > MAX_SHEET):
                problems.append("%s is %dx%d: with %d pixels of edge it does not fit a %d sheet (use PackAllSpritesToUniqueTextures for it)" % (shown, w, h, edge, MAX_SHEET))
            nx = os.path.join(dirpath, stem + ".xml")
            if os.path.isfile(nx):
                nr = read_nine_region(nx, name)
                if isinstance(nr, str): problems.append(nr)
                else:
                    s["nine"] = nr
                    if nr[1] + nr[2] > w or nr[3] + nr[4] > h: problems.append("%s: the four widths add up to more than the %dx%d sprite" % (os.path.relpath(nx, folder), w, h))
                    files.append((nx, os.path.relpath(nx, folder)))
            for n in [name] + ([s["nine"][0]] if s["nine"] else []):
                if n in names: problems.append("sprite name %r is used twice (%s and %s): names are global to the module" % (n, names[n], shown))
                names[n] = shown
            cats.setdefault(cat, []).append(s); files.append((p, shown))
    for cat in cfg:
        if cat not in cats: warnings.append("Config.xml names category %s, which has no sprites" % cat)
    if not cats and not problems: problems.append("no sprites: expected <category>/<sprite>.png under " + folder)
    return {"categories": cats, "config": cfg, "files": files, "names": sorted(names), "problems": problems, "warnings": warnings}


def game_names(modules_dir, own_id):
    """(category names, sprite names) that the other modules' GUI/*SpriteData.xml files define. The module's own file is skipped:
    it is the one this tool rewrites."""
    cats, sprites = set(), set()
    for p in glob.glob(os.path.join(modules_dir, "*", "GUI", "*SpriteData.xml")):
        if os.path.basename(os.path.dirname(os.path.dirname(p))) == own_id: continue
        try: root = ET.parse(p).getroot()
        except (ET.ParseError, OSError): continue
        cats.update(x.findtext("Name") for x in root.iter("SpriteCategory"))
        sprites.update(x.findtext("Name") for x in list(root.iter("GenericSprite")) + list(root.iter("NineRegionSprite")))
    return cats, sprites


def clashes(layout, taken):
    """Problems for categories and sprite names that another module already defines (taken = game_names())."""
    cats, sprites = taken
    return ["category %s exists in another module (its sheets would be %s_1: the game's own would be replaced or shadowed)" % (c, c) for c in layout["categories"] if c in cats] + \
           ["sprite name %s exists in another module: yours would replace it for the whole game" % n for n in layout["names"] if n in sprites]


# ---------------------------------------------------------------- the generator

def run_generator(bin_dir, module_dir, work_root, output="Engine", timeout=300):
    """Runs the game's sprite sheet generator (SingleModule) on module_dir, a private copy under work_root. Engine output goes to
    <module_dir>\\..\\AssetSources\\GauntletUI, Standalone to <module_dir>\\SpriteSheets\\<category>. Returns the generator's console lines."""
    lib = os.path.join(bin_dir, LIB)
    if not os.path.isfile(lib): raise SpriteError("no %s in %s" % (LIB, bin_dir), hint="the generator ships with the game in " + BIN)
    if output not in ("Engine", "Standalone"): raise SpriteError("output must be Engine or Standalone")
    mod, root = os.path.abspath(module_dir), os.path.abspath(work_root); container = os.path.dirname(mod)
    # the generator empties and rewrites AssetSources\GauntletUI in the folder above the module: that folder must be inside the work root
    if not os.path.isdir(mod) or not _inside(container, root) or _inside(root, os.path.dirname(os.path.dirname(bin_dir))):
        raise SpriteError("refusing to run the generator on %s: it must be a folder inside %s, outside the game folder" % (mod, root))
    if os.name != "nt": raise SpriteError("the sprite sheet generator is a Windows program: run this on the machine with the game")
    script = os.path.join(root, "gen_sprites.ps1")
    with open(script, "w", encoding="utf-8") as f: f.write(GEN_PS1)
    env = dict(os.environ, SPRITEGEN_BIN=bin_dir, SPRITEGEN_SRC=mod, SPRITEGEN_OUT=output)
    ps = shutil.which("powershell") or "powershell.exe"
    try:
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script], stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout, cwd=root, env=env, creationflags=0x08000000)
    except subprocess.TimeoutExpired:
        raise SpriteError("the generator did not finish in %d seconds" % timeout, hint="raise --timeout for very large sprite sets")
    lines = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")).splitlines()
    fault = [l for l in lines if l.startswith(("Exception:", "Unhandled Exception"))][:4]
    if r.returncode != 0 or "!!ERROR!!" in lines or not any(l.startswith("GENERATOR RETURNED") for l in lines):
        raise SpriteError("the generator failed: " + (" | ".join(l.strip() for l in fault) or "its output is in the problems list"), problems=lines[-12:])
    return [l for l in lines if l.strip()]


def read_result(container, module_id, output_png_dir=None):
    """What a finished generator run left: ({category: {"always": bool, "sheets": {id: (png path, width, height)}}}, sprite names, xml path).
    Every sheet is checked to exist, to be a complete PNG of the size the XML states and every sprite to be placed on a sheet."""
    xml = os.path.join(container, module_id, module_id + "SpriteData.xml")
    if not os.path.isfile(xml): raise SpriteError("the generator wrote no %s" % os.path.basename(xml))
    png_dir = output_png_dir or os.path.join(container, "AssetSources", "GauntletUI")
    root = ET.parse(xml).getroot(); cats = {}
    for c in root.find("SpriteCategories"):
        name = c.findtext("Name"); n = int(c.findtext("SpriteSheetCount")); sheets = {}
        for z in c.findall("SpriteSheetSize"):
            i, w, h = int(z.get("ID")), int(z.get("Width")), int(z.get("Height")); p = os.path.join(png_dir, "%s_%d.png" % (name, i))
            if not os.path.isfile(p): raise SpriteError("the generator wrote no sheet %s_%d.png" % (name, i))
            if png_size(p, True) != (w, h): raise SpriteError("sheet %s_%d.png is %dx%d but the XML says %dx%d" % ((name, i) + png_size(p) + (w, h)))
            sheets[i] = (p, w, h)
        if len(sheets) != n or not n: raise SpriteError("category %s: %d sheets listed, %d expected" % (name, len(sheets), n))
        cats[name] = {"always": c.find("AlwaysLoad") is not None, "sheets": sheets}
    for sp in root.find("SpriteParts"):
        if int(sp.findtext("SheetID")) < 1: raise SpriteError("sprite %s is on no sheet (too big for it)" % sp.findtext("Name"))
    names = sorted(x.findtext("Name") for x in root.find("Sprites"))
    return cats, names, xml


# ---------------------------------------------------------------- the stage

def sheet_format(conf, w, h):
    """The texture format of a sheet from its category's settings and size."""
    conf = conf or {}
    if conf.get("single"): return "r8"
    if w % 4 or h % 4: return "rgba8"          # block compression wants whole 4x4 blocks
    return "bc1" if conf.get("noalpha") else "bc7"


def stage_result(cats, xml, config, stage_dir, module_id, log=print):
    """Writes the sheets as texture packages and the SpriteData XML into stage_dir (Assets, RuntimeDataCache, GUI). Packages staged by
    an earlier run that this run no longer makes are removed. Returns the staged file names."""
    import tpac
    A, R, G = (os.path.join(stage_dir, x) for x in ("Assets", "RuntimeDataCache", "GUI"))
    for d in (A, R, G): os.makedirs(d, exist_ok=True)
    made, staged = [], []
    for cat in sorted(cats):
        for i, (png, w, h) in sorted(cats[cat]["sheets"].items()):
            name = "%s_%d" % (cat, i); fmt = sheet_format(config.get(cat), w, h)
            out, rp, r = tpac.write_texture(png, name=name, fmt=fmt, usage="albedo", mips=False, flags=IMPORT_FLAGS, force=True, out_dir=A, rdc_dir=R)
            log("staged %s (%s, %dx%d)" % (out, r["format"], w, h)); made += [out, rp]; staged.append(os.path.basename(out))
    dst = os.path.join(G, module_id + "SpriteData.xml"); shutil.copyfile(xml, dst); made.append(dst); staged.append("GUI/" + os.path.basename(dst))
    log("staged %s" % dst)
    record = os.path.join(stage_dir, "sprites_made.json")
    try: old = json.load(open(record))
    except (OSError, ValueError): old = []
    for p in old:
        if p not in made and os.path.isfile(p) and _inside(p, stage_dir): os.remove(p); log("removed stale %s" % p)
    json.dump(made, open(record, "w"))
    return staged


def build(folder, module_id, stage_dir, work_root, bin_dir, modules_dir=None, dry=False, timeout=300, generate=None, log=print):
    """Folder of PNGs -> staged sheets. generate(container, module_id) replaces the generator run (tests). Returns a summary dict;
    raises SpriteError with .problems when the folder cannot be used. dry reads and checks only and writes nothing."""
    if not CATEGORY.match(module_id or ""): raise SpriteError("module id %r: letters, digits and _ only (the SpriteData file is named after it)" % module_id)
    lay = read_layout(folder); problems = list(lay["problems"]); warnings = list(lay["warnings"])
    if modules_dir and os.path.isdir(modules_dir): problems += clashes(lay, game_names(modules_dir, module_id))
    else: warnings.append("the game's Modules folder was not found: names were not checked against the game's own")
    if problems: raise SpriteError("%d problems in %s" % (len(problems), folder), problems, "fix them and run again; nothing was written")
    summary = {"module": module_id, "folder": folder, "stage": stage_dir, "warnings": warnings, "names": lay["names"],
               "categories": {c: {"sprites": len(s), "nine_region": sum(1 for x in s if x["nine"]), "config": lay["config"].get(c)} for c, s in lay["categories"].items()},
               "would_stage": ["GUI/%sSpriteData.xml" % module_id, "Assets/<category>_<n>_tex.tpac and RuntimeDataCache/*.rdc for every sheet"]}
    if dry: return summary
    container = os.path.join(work_root, "gen")
    shutil.rmtree(container, ignore_errors=True)            # the generator's folders next to the module copy, from an earlier run
    mod = os.path.join(container, module_id)
    for src, rel in lay["files"]:
        dst = os.path.join(mod, "SpriteParts", rel); os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copyfile(src, dst)
    for l in (generate or (lambda c, m: run_generator(bin_dir, os.path.join(c, m), work_root, "Engine", timeout)))(container, module_id) or []: log(l)
    cats, placed, xml = read_result(container, module_id)
    if set(cats) != set(lay["categories"]): raise SpriteError("the generator made categories %s, expected %s" % (sorted(cats), sorted(lay["categories"])))
    if placed != lay["names"]: raise SpriteError("the generator's sprites differ from the folder's: missing %s, extra %s" % (sorted(set(lay["names"]) - set(placed)), sorted(set(placed) - set(lay["names"]))))
    summary["staged"] = stage_result(cats, xml, lay["config"], stage_dir, module_id, log)
    summary["sheets"] = {c: {i: {"width": w, "height": h, "format": sheet_format(lay["config"].get(c), w, h)} for i, (_, w, h) in v["sheets"].items()} for c, v in cats.items()}
    summary["next"] = "bl install" + ("" if os.path.basename(stage_dir) == "work" else " --stage " + os.path.basename(stage_dir))
    return summary
