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
bl scene check my_map                             # do the files rebuild identically
bl asset collision crate_body --hull crate.obj    # a collision body: capsules, spheres, cooked hulls and meshes
bl prefab add crate --mesh crate --physics crate_body     # a prefab of the project: scenes and `bl live spawn` place it
```

A scene is named by a folder path or by an id. Ids are looked up in the stage, the project, the module and then the
game. Scenes of the game's own modules are never changed: copy one first with `bl scene copy <id> <new name>`.

Positions are metres with z up. `--rotation` is in degrees and is applied as Rz . Rx . Ry, so the third number is the
turn around the vertical.

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
`fixed=1`). They do not see the triangle-mesh bodies of entities loaded with the scene, shipped ones included, so a
ray cast cannot confirm such a body.

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
