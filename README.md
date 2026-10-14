# Bannerlord editor-free SDK

Python tools for making Mount & Blade II: Bannerlord mods without opening the Modding Kit editor.

They write the game's own files directly: asset packages (`.tpac`), runtime caches (`.rdc`) and whole scenes
(terrain, navmesh, entities). You give them PNG, FBX, glTF or PSK files, or plain arrays for a terrain, and you get
files the game loads from your module.

![A generated battle map running in the game](docs/images/valley.jpg)

*A battle map made entirely by these tools, running in the unmodded game. It comes from my
[Procedural Battlefields](https://github.com/theartur2000/bannerlord-procedural-battlefields) mod.*

## Why

I was porting Borderlands 2 content into Bannerlord and ended up with about 34,000 assets. The editor imports them
one at a time, by hand, and each one takes anywhere from seconds to minutes. So I worked out the file formats and
scripted the whole thing. A script does an asset in seconds and nobody has to sit there clicking.

There are no game files in this repo. Everything is read from your own install when you run it.

## What it can do

- Write textures, materials, static and skinned meshes, skeletons with ragdolls, animations and animation clips.
- Write a whole scene from arrays: heights, ground texture layers, entities, navmesh, spawn paths. It borrows the
  texture layers and lighting from a scene the game ships.
- Read what the game ships: any mesh by name, every battle scene and where it puts its rocks, and the campaign map
  as a height and terrain grid.
- Put your own models and textures into a scene.
- Check assets for the mistakes that crash or freeze the game (too many bones, clip names that are too long, and
  so on) before the game ever sees them.
- Install into your module with a backup and a one-command undo. It refuses to run while the game is open.
- Merge thousands of small packages into a few. On my mod that took loading from 25.7 s (280 s on a cold start)
  down to 5.6 s.
- Test itself: `python sdk/selftest.py` rebuilds the game's own packages and checks the result is byte-identical.

There are also two folders of extras: `harness/` (PowerShell scripts that run the game unattended for testing) and
`dump/` (helpers for reading crash dumps and for Ghidra).

## Quick start

```powershell
pip install -r requirements.txt
python sdk/selftest.py                                  # reads your install, changes nothing
$env:BANNERLORD_MODULE_DIR = "<game>\Modules\MyMod"
python sdk/install.py texture rock_d.png                # stage a texture
python sdk/install.py verify stage                      # check it
python sdk/install.py install --dry                     # see what would be copied where
```

## Status

Written for Bannerlord v1.4.8.119303 (Steam, Windows, single player). I haven't tried other versions, and a game
update can move things.

The formats are checked by rebuilding the game's own files byte for byte (see "Self-test"). The scene tools have run
in the unmodded game: generated battle maps, 200 against 200 battles, custom textures and models. My original asset
scripts built a large mod that runs in game. The asset commands in this cleaned-up copy pass the self-test, but I
have not yet run their output in the game.

## Scenes and the battlefield mod

The first thing I built on the scene tools is
[Procedural Battlefields](https://github.com/theartur2000/bannerlord-procedural-battlefields): a mod that generates a
new battle map for every campaign battle. It has its own repo and uses this one as a submodule.

| | |
|---|---|
| ![Steppe](docs/images/steppe.jpg) | ![River crossing](docs/images/crossing.jpg) |
| ![Mountain shelf in winter](docs/images/snow.jpg) | ![Desert](docs/images/desert.jpg) |

It's a good example of what the scene tools do:

- `sdk/scenebuild.py` writes the scene folder (`scene.xscene`, `terrain.bin`, `navmesh.bin`) from arrays.
- `sdk/terraform.py` has the terrain building blocks: noise, warping, erosion, scattering.
- `sdk/catalog.py` measures how the game's own maps use each rock and paint each ground layer.
- `sdk/meshread.py` and `sdk/rockfit.py` read a rock's real shape and sit it into the ground.
- `sdk/worldmap.py` reads the campaign map: height, terrain type and water at any spot.
- `sdk/assetpack.py` and `sdk/psk.py` bring in your own textures and models.

That last part is how this map was made. It uses only Borderlands 2 models and textures, read from my own copy of
that game:

![A map built only from Borderlands 2 assets](docs/images/bl2field.jpg)

### Engine quirks I ran into

Every one of these cost me a crash or a black screen. The tools handle them now, but if you write scenes yourself
you will want to know.

- A terrain node needs 16 layer weight slots, even if the scene has fewer layers. With fewer, the game sometimes
  crashes on load.
- A scene folder that didn't exist when the game started can't be loaded. One that did exist can be rewritten while
  the game runs. Empty `scene.xscene`, `terrain.bin` and `navmesh.bin` files are enough at startup.
- Once a scene has been loaded, the game keeps its `terrain.bin` open until it exits. You can rewrite a scene before
  its first load in a session, but not after.
- The game only reads a module's `Assets` folder if the module also has an `AssetSources` folder.
- The game caches the terrain shader per module and scene name under `ProgramData`. If you change a scene's layers,
  delete that cache or the ground goes black or gets the wrong textures.
- A terrain layer with no area map has to say `none`. An empty name paints the terrain black.
- Most of the game's rocks are open shells with no back. The game's own maps turn cliff pieces the same way on a
  slope (yaw = downhill + 90 degrees) and still leave about a third of the rim hanging in the air where you can't
  see it.
- If terrain sits above a rock's solid surface, soldiers can walk under the ground.
- A mesh from Unreal, mirrored into Bannerlord's coordinate system, keeps its triangle order.
- In a campaign battle the game picks the spawn path that best matches the direction the two parties met from, then
  slides both armies along it depending on where the party stands in the map tile.

## Where this is going

I want this to grow into a full headless Bannerlord editor: scenes, terrain, a bridge into the running engine, and
one command-line tool that an AI agent can drive. The plan is in [docs/ROADMAP.md](docs/ROADMAP.md).

## Background reading

I wrote up the formats and the engine rules in the
[universal-modder](https://github.com/rehan-remade/universal-modder) knowledge base:
[editor-free assets](https://github.com/rehan-remade/universal-modder/blob/main/knowledge/techniques/editor-free-bannerlord-assets.md),
[custom races and creatures](https://github.com/rehan-remade/universal-modder/blob/main/knowledge/techniques/bannerlord-custom-races-and-creatures.md),
[native crashes and hangs](https://github.com/rehan-remade/universal-modder/blob/main/knowledge/techniques/bannerlord-native-crashes-and-hangs.md),
[combat, damage and guns](https://github.com/rehan-remade/universal-modder/blob/main/knowledge/techniques/bannerlord-combat-damage-and-guns.md) and
[the Borderlands 2 conversion this code came from](https://github.com/rehan-remade/universal-modder/blob/main/knowledge/games/mount-and-blade-ii-bannerlord/bannerlord-borderlands-2-total-conversion.md).

## Requirements

- Python 3.10+ and `numpy` (`pip install -r requirements.txt`). `Pillow` is only needed for pictures: terrain
  previews, and `sdk/assetpack.py`, which resizes and converts textures.
- Bannerlord on Windows. You do **not** need the Modding Kit.
- For `dump/`: `pip install minidump`. Ghidra and JDK 21 only if you use `RE.java`.
- For `harness/`: PowerShell 5.1 and a small C# part in your own module (see "Harness").

## Setup

The SDK gets its paths from these environment variables (`sdk/config.py` reads them):

| variable | meaning | default |
|---|---|---|
| `BANNERLORD_DIR` | the game install (holds `bin\` and `Modules\`) | the standard Steam path, if it exists |
| `BANNERLORD_MODULE_DIR` | the module you are building, `<game>\Modules\<YourModule>`; needed by install, revert and pack | none |
| `MODULE_ASSET_SUB` | sub-folder of the module's `Assets` that holds this SDK's packages | `mymod` |
| `BANNERLORD_SDK_HOME` | where the SDK keeps everything it writes (see "Where files go") | `%LOCALAPPDATA%\bannerlord-sdk`, else `~/.bannerlord-sdk` |
| `SDK_STAGE_DIR` | where output is staged before install | `<SDK home>\stage` |
| `SDK_BACKUP_DIR` | where install keeps backups | `<SDK home>\backup` |
| `SDK_LOCK_FILE` | install, revert and pack refuse to run while someone else holds this lock | `<SDK home>\GAME_LOCK` |

```powershell
$env:BANNERLORD_MODULE_DIR = "C:\Program Files (x86)\Steam\steamapps\common\Mount & Blade II Bannerlord\Modules\MyMod"
python sdk/config.py        # prints the paths it found
python sdk/selftest.py      # needs only BANNERLORD_DIR
```

Scripts run from any folder (`python sdk/tpac.py ...`). Run one with no arguments to see its commands.

## Where files go

Nothing is written to the current folder. Everything goes under one per-user folder, the SDK home:
`%LOCALAPPDATA%\bannerlord-sdk`, or `~/.bannerlord-sdk` if `LOCALAPPDATA` isn't set. Set `BANNERLORD_SDK_HOME` to
move it. `python sdk/config.py` shows what's in use.

| folder under the SDK home | holds | written by |
|---|---|---|
| `stage` | staged packages and caches, `_selftest`, `_native_check`, `_cache` | `sdk/` tools |
| `backup` | backups of module files that `install` replaced (these are converted game content) | `sdk/install.py`, `sdk/tpac_pack.py` |
| `GAME_LOCK` | the lock file | `harness/game_lock.ps1`, read by `sdk/` |
| `out` | test run results, logs and moved-aside saves | `harness/` |
| `config_backup` | copies of your game's launcher and engine configs during a test run | `harness/bannerlord_test_config.ps1` |
| `re_out`, `re_proj` | Ghidra output (decompiled game code) and Ghidra projects (which hold a copy of the game's DLL) | `dump/re.ps1`, `dump/RE.java` |

You can move single folders with `SDK_STAGE_DIR`, `SDK_BACKUP_DIR`, `SDK_LOCK_FILE`, `SDK_CONFIG_BACKUP`,
`RE_OUT_DIR`, `RE_DECOMP_DIR`, `RE_PROJECT_DIR`, or the `-OutDir` parameter of the harness scripts. Some of these
folders end up holding copies of the game's content or code, so keep them out of any git repo. The `.gitignore` here
lists the same names as a backstop.

## Commands

`sdk/install.py` is the one you'll use most. The writers put their output in `<SDK home>\stage\<name>` (default
`work`), and `install` copies a stage into your module.

```powershell
python sdk/install.py texture rock_d.png                       # BC1/BC3 by alpha; _n becomes BC5, _s BC1, _h BC4
python sdk/install.py material rock --diffuse rock_d --normal rock_n --skinned
python sdk/install.py skeleton creature.gltf --name creature_skeleton     # max 64 bones, minimal ragdoll; FBX works too
python sdk/install.py mesh creature.fbx --material rock        # skinned bones are named bip01_<name>_<index>
python sdk/install.py anim creature_walk.fbx                   # single-take FBX -> skeleton animation
python sdk/install.py owner "<SDK home>\stage\work\Assets\creature_walk_geo.tpac" --skeleton <skeleton guid>
python sdk/install.py clip creature_walk_clip creature_walk 31 # clip on that animation, 31 frames at 30 fps
python sdk/install.py clip-cache "creature_walk_clip"          # its RuntimeDataCache entry, no editor load needed
python sdk/install.py verify stage                             # run the checks on the stage
python sdk/install.py install --dry                            # what would be copied where
python sdk/install.py install                                  # backup, then copy
python sdk/install.py revert                                   # undo the newest install
python sdk/install.py status
```

| script | what it is |
|---|---|
| `sdk/install.py` | stage writers, `verify`, `install`, `revert`, `status`, `inspect` |
| `sdk/tpac.py` | the `.tpac` / `.rdc` reader and writer: textures (BC1, BC3, BC4, BC5, BC7 mode 6, RGBA8), materials, animations from FBX, clips, clip caches; `texture`, `tex`, `material`, `mtl`, `retex`, `anim`, `clip`, `clip-cache`, `optanim`, `owner`, `anims` |
| `sdk/sdk_mesh.py` | FBX to metamesh packages and vertex-stream caches; `mesh`, `obj` |
| `sdk/sdk_skeleton.py` | skeleton packages from FBX or glTF, Native skeleton readers |
| `sdk/scene.py` | `scene.xscene` and other game XML read and written without changing a byte that was not edited; entities listed, added, moved, removed; `check`, `list` |
| `sdk/terrain.py` | `terrain.bin` reader and writer: heights, normals, layer weights and index maps, physics materials; `info`, `check`, `relief` |
| `sdk/worldmap.py` | the campaign map as data: height field, settlements, the patch of land around a map position; `patch` |
| `sdk/navmesh.py` | `navmesh.bin` reader and writer: vertices, edges, faces with their group, value and flags; `info`, `check` |
| `sdk/terraform.py` | building blocks for making terrain: filters, slope, small relief quilted from donor terrains, layer masks |
| `sdk/catalog.py` | what the installed game offers to build from: its battle scenes by terrain type, their prefabs, and how they place each rock |
| `sdk/scenebuild.py` | a complete scene folder from arrays, with a shipped scene as the template for layers and atmosphere; `edit_layer` changes a layer's textures, detail meshes and flags |
| `sdk/meshread.py` | the geometry of any shipped mesh by name, straight from the game's packages, and its open edges; `info`, `obj`, `index` |
| `sdk/rockfit.py` | fits a rock to the ground from its real shape: least rim in the air, earth banked over the rest, ground raised under a custom model as its collision |
| `sdk/psk.py` | PSK / PSKX static meshes (umodel, Unreal exporters) into the SDK's submesh form; `info` |
| `sdk/assetpack.py` | a stage of custom textures, materials and models for a scene, and its install into a module |
| `sdk/tpac_pack.py` | merge many packages into a few and prove the merge; `scan`, `pack`, `verify`, `install`, `unpack`, `native-check`, `logcheck` |
| `sdk/validate.py` | validators: stage, skeleton, clip, `project.mbproj` registration, monster bones |
| `sdk/clip_partners.py` | the clip partner rule against Native's clips |
| `sdk/ik_hit_bones.py`, `sdk/hit_fit.py` | the hit-IK freeze rule, and hit capsules fitted to a skinned mesh |
| `sdk/tw_formats.py` | engine-exact details (data type versions, cache rules, size trailers) and a second reader |
| `sdk/templates.py`, `sdk/config.py`, `sdk/pngio.py`, `sdk/fbx_bones.py` | templates from Native, paths, PNG, FBX reader |
| `sdk/selftest.py` | the self-test below |
| `harness/` | PowerShell scripts that drive the game unattended |
| `dump/` | minidump helpers and the headless Ghidra batch script |

## Templates

Two of the writers don't build a record from nothing. They copy a plain one from the game and patch it. Both read
it from your own Native module at run time and never write it back.

- Clips (`clip`): a plain Native clip with no sound, voice, facial, partner, follow-up or combat settings. On my
  install it picks `chicken_run_gait` from `animations_animals`.
- Materials (`material`): the head, parameters and alpha reference of a plain Native `pbr_shading` material. On my
  install it picks `weapons1.lod` from `EmAssetPackages\mat0\materials`. The SDK then sets the vertex layout, blend
  mode and shader flags itself, so it doesn't matter which one was picked.

`python sdk/templates.py` shows what it picked on your install. If a game update changes the shape and nothing fits,
the command stops and asks for `--template <package>`: a one-clip package or a material package made by the editor
or by this SDK.

## Self-test

`python sdk/selftest.py` only needs your install and only writes to `<SDK home>\stage\_selftest`. This is what I get
on game v1.4.8.119303 with default settings (42 s, shortened):

```
PASS packages   200 of 200 sampled Native packages rebuild byte-identical (18495 items; AssetPackages 50/50, EmAssetPackages 150/150)
PASS textures   24 Native textures: record re-pack identical 24, size rule 24, pixel hash rule 20; re-encode PSNR min: BC4 46.9, BC5 51.2, DXT1 48.7, DXT5 52.6 dB, RGBA8 exact
PASS caches     297 Native clip caches: size trailer rule 297; 226 read by the SDK, 226 re-pack byte-identical; 71 are layout variants it does not read
PASS skeletons  3 of 3 Native skeletons re-pack byte-identical
PASS synthetic  14 of 14 checks
SUMMARY: 5 PASS, 0 FAIL
```

- `packages`: each sampled Native `.tpac` is read and written again with the SDK's writer, and the bytes have to
  match the file. `--full --max-package-mb 100000` does all of them: 1188 of 1188 (91,394 items, 86 s).
- `textures`: BC4, BC5, BC7, DXT1, DXT5 and RGBA8 textures with inline pixels. The pixel-hash rule fails on 4 of 24
  (BC4 and BC5 maps) and I don't know why yet, so the check only asks for 80 percent. There's no BC7 decoder here, so
  for BC7 only the record, hash and size are checked. Native also uses DXT3, BC6H, R8 and float formats. The SDK
  doesn't write those and the test skips them.
- `caches`: Native clips with no root channel (45) or with raw key channels (26) use layouts the SDK doesn't
  produce. They are counted in the output, not hidden.
- `synthetic`: a made-up skeleton, animation, clip, clip cache, texture, material and mesh are written, read back
  and checked. Then four deliberate mistakes (65 bones, the IK rule, a 64 character clip name, a made-up
  `project.mbproj` id) each have to be caught.

## What's checked and what isn't

Checked, on my machine, for game v1.4.8.119303 only:

- The container and cache formats, textures, materials, skeletons and clip caches rebuild byte for byte (above). The
  editor-free assets note under "Background reading" has more: 161 of 166 editor textures, 103 of 103 editor clip
  caches, all 150 Native asset packages.
- I compared this cleaned-up copy against my original scripts on a clip, a movement clip, two materials, a mesh and
  an animation. Same bytes, apart from random ids and the source-path string, and the same decoded mesh and
  animation data.
- Two small differences from the originals. The clip record hash is now the correct xxh64 (the originals kept the
  template's old one, which the engine doesn't check). Texture mips are a plain area average, which is within one
  level of the originals for opaque power-of-two textures and a bit further off for other sizes and for alpha.

Not checked:

- Other game versions. Offsets and layouts can move, so treat every address in the notes as stale after an update.
- The asset commands of this copy running in the game (the originals did).
- The harness scripts against a module other than my own.
- Multiplayer and anti-cheat. This is single player only.

Things that are specific to how I work and you may need to change: skinned FBX bones must be named
`bip01_<name>_<index>`, the ragdoll that `skeleton` writes is a minimal starting point, and clip caches are only
written for clips that have a root position channel.

## Harness

`harness/` runs the game unattended so a test doesn't need anyone at the keyboard. It needs a small C# part in the
module you are testing. The [Procedural Battlefields](https://github.com/theartur2000/bannerlord-procedural-battlefields)
mod has a complete one (`mod/GenFields/src/AutoTest.cs`) that you can copy from.

How it works:

- The game is started with `/singleplayer _MODULES_*Native*SandBoxCore*CustomBattle*<YourModule>*_MODULES_`, so the
  mod list saved in your launcher is left alone.
- `night_test.ps1` writes test words (like `plain fight quit`) to an autostart file. Your module reads that file at
  startup, runs the scenario, writes an end marker (default `AUTOTEST_DONE`) to its log and quits.
- The harness waits for the marker, collects the log lines and any Windows crash events, and tries once more if the
  first run failed. It can record video through a script you pass with `-Record`.
- `bannerlord_test_config.ps1` switches the game to a silent windowed setup for the run and puts your own settings
  back afterwards.
- `game_lock.ps1` holds a lock file so two test sessions never start the game at the same time. The SDK's install
  and pack commands respect the same lock.
- `dialogs.ps1` answers the three popups that would stall a run: "enable safe mode" after a crash (No), the shader
  cache notice (OK) and the crash report prompt (No). It sends no other input.
- `player_checks.ps1` runs named checks from a JSON file (`checks.example.json`) and prints PASS and FAIL lines.

```powershell
powershell -ExecutionPolicy Bypass -File harness\night_test.ps1 -Module MyMod -Words "plain fight quit" `
    -LogFile "$env:BANNERLORD_MODULE_DIR\mymod_log.txt" -DryRun       # checks the harness without starting the game
```

The harness only ever stops the game process it started itself. If a run creates new save files, it moves them aside
and tells you. It never deletes them.

## Crash dumps and reverse engineering

`dump/` has helpers for reading minidumps (`threads.py`, `unwind.py`, `loc.py`, `rd.py`, `huge.py`, `vt.py`) and
`RE.java` with `re.ps1`, a batch script for headless Ghidra. Usage is in `dump/README.txt`.

**Careful with decompiled game code.** The `RE.java` commands `decomp`, `decompall` and `insnall` write decompiled
game code to disk. That output is TaleWorlds' code, not yours. Keep it outside any git repo and don't commit, publish
or share it. `decomp` writes to `<SDK home>\re_out\decomp` by default, which is outside any repo. For `decompall`
and `insnall` you pass the path yourself, so pick one that is too. If you want to write about what you found,
describe the logic in your own words.

## Files

```
sdk/            the library and command-line tools
docs/           ROADMAP.md, images/
harness/        scripts that run the game unattended
dump/           crash dump helpers, RE.java, re.ps1
```

## Credits

- TaleWorlds Entertainment make Bannerlord and the Modding Kit. The game and everything in it is theirs. I worked
  out the formats from the files and binaries in my own install.
- [TpacTool](https://github.com/szszss/TpacTool) by szszss showed that `.tpac` could be read at all. The reader and
  writer here were written from scratch against the editor's output and Native's packages.
- LZ4 and xxHash are by Yann Collet. The versions here are small independent implementations of the published
  formats.
- Ghidra, ILSpy and the Python `minidump` package did a lot of the digging.
- I built this with Claude Code. I decide what to build and test it in the game, and it writes most of the code.

## License

MIT.
