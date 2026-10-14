"""Scratch scene slots: scene folders of a module that can be filled while the game runs.

Two engine rules (docs: README "Engine behavior"):
  - a scene folder that did not exist when the game started cannot be loaded; zero-byte scene.xscene, terrain.bin and
    navmesh.bin are enough for the folder to count;
  - after a scene's first load the engine keeps its terrain.bin open until the game exits.
So a module that wants to load new scenes without a restart carries empty folders, fills one and loads it, and takes the
next one for the next scene. A slot is a folder SceneObj/<prefix>_<n> with a marker file slot.txt, the atmosphere and flora
files of a template scene (those are read at startup) and the three empty files.

    create(module, prefix, count, template)   make the folders (game closed: new folders count only after a restart)
    state(module, prefix)                     every slot as "empty", "filled" or "held" (open in the running game)
    fill(module, slot, scene_folder)          copy a scene into a slot; fails on a held one
    reset(module, prefix)                     empty every filled slot (game closed)
The C# side is src/SceneSlots.cs of a project made with `bl new`.
"""
import os, shutil

FILES = ("scene.xscene", "terrain.bin", "navmesh.bin")      # what a scene writer produces; left empty in a new slot
KEPT = ("atmosphere.xml", "flora.bin")                       # read when the game starts: copied from the template
MARK = "slot.txt"


def folder(module, slot): return os.path.join(module, "SceneObj", slot)


def names(module, prefix):
    out = []; n = 1
    while os.path.isfile(os.path.join(folder(module, "%s_%d" % (prefix, n)), MARK)): out.append("%s_%d" % (prefix, n)); n += 1
    return out


def held(module, slot):
    """True while another process (the game) has the slot's terrain file open: it shares the file for reading only."""
    p = os.path.join(folder(module, slot), "terrain.bin")
    try:
        with open(p, "r+b"): return False
    except PermissionError: return True


def state(module, prefix):
    out = {}
    for s in names(module, prefix):
        size = os.path.getsize(os.path.join(folder(module, s), "terrain.bin"))
        out[s] = "held" if held(module, s) else "filled" if size else "empty"
    return out


def create(module, prefix, count, template, dry=False):
    """Adds slots until the prefix has `count`. template: a scene folder whose atmosphere.xml and flora.bin are copied.
    Returns the names of the new slots."""
    for fn in KEPT:
        if not os.path.isfile(os.path.join(template, fn)): raise ValueError("the template %s has no %s" % (template, fn))
    have = names(module, prefix); new = ["%s_%d" % (prefix, n) for n in range(len(have) + 1, count + 1)]
    for s in new:
        d = folder(module, s)
        if os.path.exists(d): raise ValueError("%s exists and is not a slot (no %s in it)" % (d, MARK))
        if dry: continue
        os.makedirs(d)
        for fn in KEPT: shutil.copyfile(os.path.join(template, fn), os.path.join(d, fn))
        for fn in FILES: open(os.path.join(d, fn), "wb").close()
        with open(os.path.join(d, MARK), "w", encoding="utf-8", newline="\n") as f: f.write("scratch scene slot; template %s\n" % os.path.basename(os.path.normpath(template)))
    return new


def fill(module, slot, scene_folder):
    """Copies a scene's three files into a slot and renames the scene to the slot's name. The slot must not be held."""
    d = folder(module, slot)
    if not os.path.isfile(os.path.join(d, MARK)): raise ValueError("%s is not a slot" % d)
    if held(module, slot): raise ValueError("%s is held by the running game: a slot takes one scene a session" % slot)
    for fn in FILES:
        if not os.path.isfile(os.path.join(scene_folder, fn)): raise ValueError("%s has no %s" % (scene_folder, fn))
    import scene
    doc = scene.load(os.path.join(scene_folder, "scene.xscene"))
    doc.root.set("name", slot)                               # a scene is loaded by the name of its folder; the two must agree
    doc.save(os.path.join(d, "scene.xscene"))
    for fn in FILES[1:]: shutil.copyfile(os.path.join(scene_folder, fn), os.path.join(d, fn))
    return d


def reset(module, prefix):
    """Empties every filled slot. Returns how many were emptied; a held slot raises."""
    n = 0
    for s, st in state(module, prefix).items():
        if st == "held": raise ValueError("%s is held by the running game" % s)
        if st == "filled":
            for fn in FILES: open(os.path.join(folder(module, s), fn), "wb").close()
            n += 1
    return n
