# Guide for agents

This page is for an AI agent (or a script) that builds a Bannerlord mod with the SDK. Everything goes through one
command, `bl`, or through the same commands as MCP tools.

## The contract

- Every command prints exactly one JSON object and nothing else.
  - Success: `{"ok": true, "command": "...", "result": {...}, "log": [...]}`
  - Failure: `{"ok": false, "command": "...", "error": "...", "hint": "...", "log": [...]}`
- The exit code is 0 or 1.
- `log` holds what the wrapped tool printed. Read `result`, not `log`.
- Every command that changes files accepts `--dry` and then only reports what it would do.
- `bl commands` returns every command with a JSON Schema of its arguments. Read it once at the start of a session.
- Nothing writes into the game folder while the game runs or while the game lock is held by someone else.
- `bl run` starts the game only with `--launch`.

A command name can be written with a dot or a space: `bl scene.info x` and `bl scene info x` are the same.

## Setup

```powershell
pip install -r requirements.txt
bl doctor            # game found, version, module, tools; lists problems
bl compat --quick    # do the file formats still match this game version
```

`bl doctor` reports the game version and whether it is the tested one. After a game update, run `bl compat`: it
rebuilds the game's own files with the SDK's writers and names every format that no longer matches.

## As MCP tools

```json
{"mcpServers": {"bannerlord": {"command": "python", "args": ["<repo>/sdk/mcp_server.py"],
                               "env": {"BANNERLORD_DIR": "<game folder>", "BL_PROJECT": "<project folder>"}}}}
```

Tool names are the command names with an underscore for the dot (`scene.info` is `scene_info`). The answer is the same
JSON object, as text.

## A project from nothing

```powershell
bl new MyMod                 # manifest, C# project, test driver, scene slots, bridge, scene list
cd MyMod
bl build                     # build/MyMod.dll, compiled against the installed game
bl deploy                    # copies the module into <game>\Modules\MyMod (game closed)
bl run --words "tod=12 fight secs=30 quit" --launch     # one unattended test run
```

Inside a project folder (one with `bl.json`), the module defaults to `<game>\Modules\<id>` and asset packages go to
`Assets\<id>`.

## Assets

```powershell
bl asset texture rock_d.png
bl asset material rock --diffuse rock_d --normal rock_n --static
bl asset mesh rock.fbx --material rock      # reports size_m; an FBX from Blender's default export needs --scale 0.01
bl stage verify              # the faults the game turns into crashes
bl install --dry             # what would be copied where
bl install                   # backup, then copy
bl revert                    # undo the newest install
```

Writers stage their output under the SDK's working folder; `install` copies a stage into the module.

UI sprites come from a folder of PNGs, one sub-folder per sprite category (layout in the header of `sdk/sprites.py`):

```powershell
bl ui.sprites art/ui --dry          # checks the folder, names every fault, writes nothing
bl ui.sprites art/ui                # the game's sprite sheet generator on a private copy, then sheets + GUI/<id>SpriteData.xml staged
bl install
```

## Scenes

```powershell
bl scene list --terrain Plain                     # shipped battle scenes: templates to build from
bl scene info battle_terrain_a                    # size, layers, entities by prefab, navmesh, spawn paths
bl scene heightmap battle_terrain_a out.png       # 16-bit PNG and out.png.json (what the grey values mean)
bl scene from-heightmap out.png my_map --to project        # a playable scene from an image
bl scene register my_map --name "My map"          # list it in Custom Battle
bl scene add-entity my_map --prefab rock_cliff_b --position "600 640" --on-ground --rotation "0 0 45"
bl scene preview my_map my_map.png                # top view: relief, layers, rocks, trees, spawn paths
bl scene flora my_map --add tree_high_a --position "600 640" --count 12 --radius 9   # placed trees (flora.bin); --kinds lists them
bl scene check my_map                             # do the files rebuild identically
bl asset collision crate_body --hull crate.obj    # a collision body: capsules, spheres, cooked hulls and meshes
bl prefab add crate --mesh crate --physics crate_body     # a prefab of the project: scenes and `bl live spawn` place it
python sdk/particles.py copy psys_campfire my_fire --color 0.1,1,0.1 --out <stage>/work/Assets
bl prefab add my_fire_prefab --particle my_fire            # a particle effect on a prefab
bl asset material my_flag --diffuse my_flag --flags two_sided
bl asset mesh my_flag.fbx --cloth flag --pin xmax           # cloth: cooked from the mesh, the pole edge held
bl prefab add my_flag_prefab --mesh my_flag --cloth         # placed in a cloth_simulator, as the shipped flags
```

A scene is named by a folder path or by an id. Ids are looked up in the stage, the project, the module and then the
game. Scenes of the game's own modules are never changed: copy one first with `bl scene copy <id> <new name>`.

Sieges, villages and lord's halls start from a shipped scene of that kind:

```powershell
bl scene copy battania_castle_a my_castle --to project
bl scene add-entity my_castle --prefab my_tower --position "738 748 49.5"
bl scene register my_castle --kind siege          # or village, lords_hall
bl run --words "type=Siege scene=my_castle tod=12 fight secs=60 quit" --launch
```

The campaign map's ground is changed with bumps, kept in the project (`worldmap/edits.json`) and staged as the map's
two height textures:

```powershell
bl live settlement --values id=town_S1                       # a position to start from (gate_position)
bl worldmap.bump --x 374 --y 548 --radius 10 --height 10     # height < 0 for a dip; --list, --clear
bl install                                                   # then a new campaign
bl live mapheight --values x=374,y=548                       # the party's ground there
```

The map's heights stay within the shipped range (about -0.7 to 23); the command reports clipped pixels. Without
`--trees` the map's trees keep their heights, so raised ground buries them; with it the map scene is written into the
project (`SceneObj/Main_map`, flora moved, `bl deploy` installs it) and later bumps keep it up to date.

```powershell
bl worldmap.move --id town_S1 --x 375 --y 530     # position, gate and the town's model; check the spot with bl worldmap.at
bl worldmap.terrain --x 395 --y 520 --radius 6 --type Desert   # the party navmesh's terrain type: speed and passability
bl worldmap.look --mode painted                   # draw the map from one colour texture (terrain types + relief), or --image my.png
bl worldmap.continent --image heights.png --sea 0.3   # a new continent: heights, navmesh, forests, settlements on land
```

A move is a data patch (posX, posY, gate) plus the settlement's model moved in the map scene copy. The party navmesh is
not changed, so put a settlement where `worldmap.at` reports land (Plain, Forest ...).

The map scene copy carries `TileSets/WorldMap.gts` (4 MB): the engine opens the first .gts in the module's own
`SceneObj/Main_map/TileSets`, and this copy's page names lead to SandBox's 2.1 GB of pages, by a path relative to
`<game>/Modules/<module>`. A module installed elsewhere (a Steam Workshop folder) needs that path made for its own place.

`worldmap.look --mode painted` leaves the shipped renderer: the map is drawn from `terrain.bin` (with the bumps in it)
and one colour texture, `<module>_worldmap_colour`, painted from the terrain types and the relief or taken from your
image (row 0 = the far edge, the largest y). Edited land then gets its own colours, which is what a new continent needs;
the shipped map's fine detail is gone. `--mode shipped` goes back.

`worldmap.continent` replaces the land itself. From a grey heightmap (row 0 = the far edge) it writes the base every
later edit starts from (`worldmap/continent` in the project): heights, a party navmesh with terrain types (Plain,
Forest from noise, Mountain and Snow from slope and height), trees in the forests, the old map's rocks, cliffs, bridges,
lakes and decals taken out, and every settlement moved onto land with its gate on the navmesh (a gate off it crashes the
engine's path finding). The AI also needs the settlement distance cache of the new map, built by the game itself:

```powershell
bl install; bl deploy; bl live.enable
bl run --campaign --words "campaign near=town_S1" --launch --timeout 3600   # in the background
bl live navcache --timeout 3000          # about 15 minutes: writes <module>/ModuleData/DistanceCaches/...
# copy that file into the project's ModuleData/DistanceCaches, so `bl deploy` keeps it
bl live time --values mode=fast          # the campaign clock; `bl live time` reports the date and the moving parties
```

Interiors (lord's halls, taverns, arenas, keeps) are not Custom Battle maps. They are opened in a campaign through the
bridge, from the campaign map:

```powershell
bl scene copy sturgia_castle_keep_a_l2_interior my_hall --to project
bl scene add-entity my_hall --prefab my_prefab --position "11.5 25 2.7"
bl deploy
bl live.enable
bl run --campaign --words "campaign near=town_S1" --launch     # in the background
bl live state                                                  # wait for game_state MapState
bl live indoor --values settlement=town_S1,location=lordshall,scene=my_hall   # enters the town
bl live indoor --values settlement=town_S1,location=lordshall,scene=my_hall   # opens the hall
```

Without `scene=` the settlement's own scene for that location opens. `indoor` refuses until the campaign map is up:
opening a mission during character creation hangs the game.

`--on-ground` takes the height of the terrain. In towns and castles the walking surface is often a model above the
terrain (a road, a ramp, a courtyard floor): give z yourself there, or measure it in game with `bl live raycast`.

Positions are metres with z up. `--rotation` is in degrees and is applied as Rz . Rx . Ry, so the third number is the
turn around the vertical.

## Game data

Objects the game or another module defines (settlements, items, troops, kingdoms, clans, cultures) are changed with a
patch, not by copying their files:

```powershell
bl data.set Settlements Settlement town_S1 --values "name={=!}New Town"
bl data.set Items Item imperial_sword_t2 --values "value=500"
bl run --campaign --words "campaign near=town_S1" --launch       # a campaign, unattended; then
bl live settlement --values id=town_S1                            # name, position, culture, owner in the running game
```

`data.set` writes an XSLT next to an empty XML of that kind in the project's ModuleData and registers it in
SubModule.xml; the game applies it to the merged data when it loads.

## Sound

New sound events from WAV files, and Native events replaced by the module's own sounds, without FMOD Studio's window.
`bl sound.build` drives Studio's command line tool (`fmodstudiocl.exe`) on a project it generates.

Needed: FMOD Studio **2.02.27** exactly (fmod.com, Download, older versions; a newer Studio writes banks the game's FMOD
refuses). It is found in `C:\Program Files\FMOD SoundSystem\FMOD Studio 2.02.27`, or set `FMOD_STUDIO_CL` to its
`fmodstudiocl.exe`. The game's `modding_resources\FMOD Sample Project` supplies the mixer. `vgmstream-cli` (`BL_VGMSTREAM`
or the PATH) is optional: with it the check also decodes samples and compares them with the WAV files.

```powershell
bl sound.list --native "combat impact"           # Native events: 2D or 3D, length, parameters, the mixing a clone would take
bl sound.add event:/mymod/combat/hit hit_a.wav hit_b.wav --like event:/mission/combat/blunt/med   # one of the files per play
bl sound.add event:/mymod/ui/click click.wav --like event:/ui/default
bl sound.add event:/mission/combat/cut/med cut.wav          # a Native path: replaces that event
bl sound.build               # bank + ModuleData files staged, bank checked offline (--dry first to see the plan)
bl sound.info --decode 6     # the check again; samples decoded and compared with the WAV files
bl install                   # game closed
bl live.enable               # before the game starts
bl live play --values event=event:/mymod/ui/click                       # as the player hears it (2D)
bl live play --values event=event:/mymod/combat/hit,x=600,y=640         # at a position in the mission or on the campaign map
bl live stop                 # ends what play started, loops included
```

`sound.add` copies the WAV files into the project (`Sounds/src/<event path>/`) and records the event in `sounds.json`.
Adding a path again replaces its entry. `sound.remove` takes an event out. Mono or stereo WAV (8, 16, 24, 32 bit or float)
at any rate; FMOD Studio encodes them to the 48 kHz Vorbis of Native's banks.

**`like`** names a Native event. The new event copies its engine attributes (`max_distance`, `parameter_indices`,
`priority_multiplier`, `bassgid`, send flags) and takes its mixing from the sample-project event closest to it (output bus,
spatializer, occlusion, voice limits, parameters; loop or one-shot). Pick the Native event that is used the way the new sound
is: a click like `event:/ui/default`, a melee hit like `event:/mission/combat/blunt/med`, a gunshot like a missile release. Without
`like` a new event is a 2D one-shot on the UI bus (`event:/mod/ui`). `--loop true|false` overrides the loop of the `like` event.
`sound.build` reports the mixing template each event got and says when its attributes (2D or 3D) and its template disagree.
A replaced event keeps its own attributes: the engine sets its parameters by index.

**A module gets**, staged for `bl install`:

| File | Purpose |
|---|---|
| `Sounds/PC/<Module>.bank` | the events and samples; FMOD bank format 0x8e/0x8c like Native's |
| `ModuleData/soundfiles.xml` | lists the bank; the engine loads module banks after Native's |
| `ModuleData/<module>_sound_event_data.xml` | one row per event: path, GUID, length, distance, parameters; the engine reads it as raw XML |
| `ModuleData/project.mbproj` | the entries `soln_sound_files` and `soln_sound_event_data` that name those two files; other entries of an existing file stay |

`install` records all four in its manifest, so `revert` removes them. Rules the build keeps:

- Every file `project.mbproj` names must exist: the native sound loader crashes on a missing one. The stage ships them together
  and `install` checks the registration again.
- A module bank never reuses a Native GUID. GUIDs are `uuid5` of module id and path, so a rebuild keeps them.
- The game resolves an event by its GUID and a path through `sound_event_data`. A path that Native has is overwritten by the
  module's row (the loader replaces an existing path and appends a new one); a second `sound_event_data.xml` merged by the
  managed code, or an XSLT, is not used.
- `bl deploy` copies the project's `ModuleData`. If the project has its own `ModuleData/project.mbproj`, add the two sound lines
  of the staged file to it, or the deploy overwrites the installed registration.

Replacing a Native event changes it everywhere the game plays it. Native often has one event per surface, weapon class or
armour type; replace each one that should change. Loading such a module (a bank of about 1,600 events, most of them Native
paths) ran in the game without FMOD errors; hearing the replaced sounds in game is not yet confirmed for this SDK's build.

**In-game check.** `bl live play` answers `event_index` (-1: the engine does not know the path, so the `sound_event_data` row did
not load), `mode` (2D or 3D), `started`, and how many sounds from `play` are still going; the module's log
(`<module>_log.txt`) gets a line per play. The engine's log (`rgl_log_<pid>.txt` in
`ProgramData\Mount and Blade II Bannerlord\logs`) shows the loading: `Loading xml file: $BASE/Modules/<Module>/ModuleData/soundfiles.xml`
and `.../<module>_sound_event_data.xml` under "reading sound_files xml files" and "reading sound_event_data xml files". A bank the
FMOD runtime refused shows as `FMOD error! (13)` with the bank's name. About 60 `FMOD error! (74)` lines, "event, parameter,
bus or vca could not be found", appear in every run of the unmodified game.

Not done: more than one bank per module; events with markers, several tracks or snapshots. One event is one multi
instrument over its files, picked at random.

## Looking without the game

```powershell
bl mesh find cliff                       # names of shipped meshes (about 14,000)
bl mesh info rock_cliff_d                # size, triangles, open edges
bl mesh preview rock_cliff_d rock.png    # six views on one sheet; faces seen from behind are red
bl scene preview <scene> out.png
```

A render takes about a second. Use these before starting the game: most placement and scale mistakes show here.

## The running game

```powershell
bl live.enable                            # before the game starts: the module listens from its next start
bl live state                             # screen, scene, player position, troops alive
bl live teleport --values x=600,y=640
bl live look --values bearing=90,elevation=-10
bl live screenshot --values path=C:\out\shot.bmp
bl live spawn --values prefab=rock_cliff_b,x=600,y=640,yaw=45
bl live remove --values id=all
bl live raycast --values x=600,y=640      # what a vertical ray hits first, and at what height
bl live sprite --values name=my_icon      # does the game know a UI sprite, and does its sheet load
bl live animate --values id=0,clip=my_clip   # play a clip on a spawned prefab with a skeleton
bl live bone --values id=0               # where its last bone is and which way it points now
bl live play --values event=event:/mymod/ui/click    # a sound event (see Sound); `bl live stop` ends it
bl live quit                              # quits; under `bl run` the run then counts as passed
```

The bridge is part of a project made with `bl new` (`src/Bridge.cs`). Every command has been run in the game. A
test session that is driven from outside looks like this:

```powershell
bl run --words "menu=30 scene=slot_1 tod=12 secs=900" --launch      # in the background: waits 30 s at the menu
bl live ping                              # repeat until it answers
bl slots fill slot_1 my_map               # while the game waits at the menu
bl live state                             # repeat until "scene": "slot_1" with a player
bl live screenshot --values path=...      # look, change, look again
bl live quit
```

Ray casts see capsule and sphere bodies (the module's own included) and prefabs spawned while the game runs (with
`fixed=1`), and convex hulls on entities placed in the scene file. They do not see the triangle-mesh bodies of entities
loaded with the scene, shipped ones included. To check such a body, drop the player on it: `bl live teleport` with a z
above it, then `bl live state` shows the height the player stands at.

## Scenes without a restart

The engine loads a scene folder only if the folder existed when the game started, and it keeps a loaded scene's
terrain file open until it exits. Scratch slots work around both:

```powershell
bl slots create --count 20               # empty scene folders in the module (game closed)
bl slots state                           # empty, filled, or held by the running game
bl slots fill slot_3 my_map              # works while the game runs, on a slot not loaded this session
```

Load the scene by the slot's name. A slot takes one scene per game session. `src/SceneSlots.cs` does the same from C#.

## Rules that save a test run

- A terrain node holds 16 layer weight slots, whatever the number of layers.
- A terrain layer without an area map names it `none`.
- After changing a scene's texture layers, the terrain shader cache for that module and scene name must be deleted.
  `bl deploy` and `bl slots fill` do that.
- A module's `Assets` folder is read only if the module also has an `AssetSources` folder.
- Most shipped rocks are open shells: place them with the open side in the ground (`bl mesh preview` shows it in red).
- Terrain above a rock's solid surface lets agents walk under the ground.

The full list is in the README under "Engine behavior".

## Adding a command

A command is one decorated function in `sdk/bl.py`:

```python
@command("scene.count", "How many entities a scene holds.", [SCENE])
def scene_count(a):
    import scene
    return {"entities": len(scene.load(os.path.join(_scene_folder(a["scene"]), "scene.xscene")).entities(True))}
```

It is then on the command line, in `bl commands` and in the MCP server. Return plain data, raise `BlError(message,
hint)` for a refusal, and pass `writes=True` for anything that changes files (that adds `--dry`).
`python tests/run_offline.py` checks that every command has a valid schema and that every writing command has a dry run.
