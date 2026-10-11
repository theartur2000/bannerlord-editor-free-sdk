"""Installs the GenFields demo module into the game: the DLL, SubModule.xml and the scenes to test.

    python demos/battlefields/install.py --dll <folder with GenFields.dll> [--scene gen_field_varagos ...] [--campaign gen_field_varagos]
    python demos/battlefields/install.py --remove

  --scene NAME     copy <SDK stage>/scenes/NAME into Modules/GenFields/SceneObj and list it in Custom Battle
                   (the plain test: does the game accept a generated scene at all)
  --late NAME      list NAME in Custom Battle but do NOT copy it: the autotest word late=NAME copies it from the stage folder
                   at the main menu, which tests loading a scene folder that did not exist when the game started
  --campaign NAME  write genfield.txt so every campaign field battle goes to NAME. The scene is NOT copied: the module
                   copies it from the stage folder at the first battle, which tests loading a scene folder that did not
                   exist when the game started
  --assets STAGE   copy a stage of custom assets (sdk/assetpack.py: textures, materials, models) into the module, for
                   scenes built from them (demos/bl2field)
  --pool           install every staged field of pool.py and list them in ModuleData/genfields_pool.txt: with that
                   file every campaign field battle on land goes to a pool field (Pool.cs)
  --remove         delete Modules/GenFields

Refuses while the game runs. Touches nothing outside Modules/GenFields.
"""
import argparse, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sdk"))
import config   # noqa: E402

MODULE = os.path.join(config.MODULES, "GenFields")
SRC = os.path.join(HERE, "mod", "GenFields")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dll"); ap.add_argument("--scene", action="append", default=[]); ap.add_argument("--campaign"); ap.add_argument("--late", action="append", default=[])
    ap.add_argument("--remove", action="store_true"); ap.add_argument("--assets"); ap.add_argument("--pool", action="store_true")
    a = ap.parse_args()
    config.require_game()
    running = config.game_processes()
    if running: sys.exit("the game is running (%s): nothing changed" % ", ".join(str(p) for p in running))
    if a.remove:
        if os.path.isdir(MODULE): shutil.rmtree(MODULE); print("removed", MODULE)
        else: print("not installed")
        return
    os.makedirs(os.path.join(MODULE, "ModuleData"), exist_ok=True)
    shutil.copyfile(os.path.join(SRC, "SubModule.xml"), os.path.join(MODULE, "SubModule.xml"))
    if a.dll:
        dll = os.path.join(a.dll, "GenFields.dll")
        if not os.path.isfile(dll): sys.exit("no GenFields.dll in " + a.dll)
        for sub in ("Win64_Shipping_Client", "Win64_Shipping_wEditor"):
            d = os.path.join(MODULE, "bin", sub); os.makedirs(d, exist_ok=True); shutil.copyfile(dll, os.path.join(d, "GenFields.dll"))
        print("DLL installed")
    elif not os.path.isfile(os.path.join(MODULE, "bin", "Win64_Shipping_Client", "GenFields.dll")):
        print("warning: no DLL installed yet (pass --dll); the module will not load without it")
    if a.assets:
        import assetpack
        print(assetpack.Stage(a.assets).install(MODULE, "custom"), "custom asset packages installed")
    pool_rows = []
    if a.pool:
        import pool
        for r in pool.POOL:
            if os.path.isfile(os.path.join(config.SDK_STAGE_DIR, "scenes", r[0], "scene.xscene")): a.scene.append(r[0]); pool_rows.append(r)
            else: print("pool field", r[0], "is not staged (run pool.py): left out")
    names = []
    for n in a.scene:
        src = os.path.join(config.SDK_STAGE_DIR, "scenes", n)
        if not os.path.isfile(os.path.join(src, "scene.xscene")): sys.exit("no staged scene " + src)
        dst = os.path.join(MODULE, "SceneObj", n)
        if os.path.isdir(dst): shutil.rmtree(dst)
        shutil.copytree(src, dst); names.append(n); print("scene", n, "installed")
        # The game keeps the terrain shader it compiled for a scene, by module and scene name. A scene whose texture layers
        # changed (other textures, other flags) would go on using the old one: black or wrongly mapped ground.
        cache = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Mount and Blade II Bannerlord", "Shaders", "TerrainShaders", "GenFields", n)
        if os.path.isdir(cache): shutil.rmtree(cache); print("terrain shader cache of", n, "cleared")
    for n in a.late:
        dst = os.path.join(MODULE, "SceneObj", n)
        if os.path.isdir(dst): shutil.rmtree(dst)
        if not os.path.isfile(os.path.join(config.SDK_STAGE_DIR, "scenes", n, "scene.xscene")): sys.exit("no staged scene " + n)
        print("scene", n, "listed but not installed (late copy test)")
    have = sorted(set([d for d in (os.listdir(os.path.join(MODULE, "SceneObj")) if os.path.isdir(os.path.join(MODULE, "SceneObj")) else [])] + a.late))
    xml = ['<?xml version="1.0" encoding="utf-8"?>', "<CustomBattleScenes>"]
    for n in have: xml += ["\t<Scene", '\t\tid="%s"' % n, '\t\tname="{=!}Generated: %s"' % n, '\t\tterrain="Plain"></Scene>']
    xml.append("</CustomBattleScenes>")
    with open(os.path.join(MODULE, "ModuleData", "genfields_custom_battle_scenes.xml"), "w", encoding="utf-8-sig", newline="\r\n") as f: f.write("\n".join(xml) + "\n")
    print("Custom Battle lists", len(have), "generated scene(s)")
    if a.pool:
        with open(os.path.join(MODULE, "ModuleData", "genfields_pool.txt"), "w", encoding="utf-8") as f:
            f.write("# scene biome layout: the fields campaign battles are sent to (Pool.cs)\n" + "".join("%s %s %s\n" % (r[0], r[1], r[2]) for r in pool_rows))
        print("pool of", len(pool_rows), "fields: every campaign field battle on land goes to one of them")
    ctl = os.path.join(MODULE, "genfield.txt")
    if a.campaign:
        with open(ctl, "w", encoding="utf-8") as f: f.write("# every campaign field battle goes to this scene (GenFields)\n" + a.campaign + "\n")
        print("campaign battles go to", a.campaign)
    elif os.path.isfile(ctl): os.remove(ctl)
    print("installed in", MODULE, "- enable GenFields in the launcher")


if __name__ == "__main__":
    main()
