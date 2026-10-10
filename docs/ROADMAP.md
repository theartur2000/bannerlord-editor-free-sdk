# Roadmap: a headless Bannerlord editor

Goal: a full headless Bannerlord editor for doing anything in the game through agentic programming, ending with
Bannerlord used as a game engine in its own right. This repository grows into it; the asset SDK is the first part.

Status: a plan, written 2026-10-10. Nothing here is built yet except where it says "have".

The SDK comes first. Its first test is a separate, standalone tech-demo mod made only with it: battlefields generated from
the spot on the world map where a battle starts ([BATTLEFIELDS.md](BATTLEFIELDS.md)). The demo is not part of the SDK. It
decides which SDK pieces get built first, and it is finished when it needs nothing that the SDK does not provide.

## 1. What the official editor turned out to offer

Checked on this machine, game v1.4.8.119303, by reading the editor build (`bin\Win64_Shipping_wEditor`) and our decompiled
managed assemblies. **Nothing below was run.** These are names found in the binaries; step M0 is to try them.

**Command-line switches in the editor's native DLL**
- `/open_scene`, `/scene`, `/editor_only`: start straight into a scene or the editor.
- `/nativeTest <name>` with built-in job names: `OpenSceneOnStartup`, `MissionTest`, `SiegeMissionTest`, `CampaignScenesTest`,
  `MultiplayerScenesTest`, `GenerateTerrainShaderHeaders`, `CompileTerrainShaders`, `CrfPackRun`, `editor_test`.
  These look like TaleWorlds' own unattended build and test jobs.
- `/runTest`, `/suppress_message_boxes`, `/dont_save_cache`, `/forced_config`, `/LogOutputPath`, `/maxThreadCount`.
- `/consoleHost`: the engine can take console commands from a host. Unknown protocol.
- `/take` with `/screen /final /albedo /normals /depth /specularity /occlusion`: scripted screenshots of render passes.

**Editor commands inside the native DLL**
- `do_batch_bake`, `check_for_problems`, `pack_nested_prefabs`, `export_selected_entites`, `export_meta_mesh_as_obj`,
  `import_meshes`, `import_skeletons`, `import_skeletal_animations`, `import_physics_shapes`, `import_morph_animations`.

**The engine API a C# module can call (the biggest find)**
- 306 scene methods, 276 entity methods, 149 utility methods are exposed to managed code. Among them:
  `create_new_scene`, `create_empty`, `create_from_prefab`, `create_and_add_script_component`, `optimize_scene`,
  navmesh calls (`load_nav_mesh_prefab`, `save_nav_mesh_prefab_with_frame`, `set_nav_mesh_region_map`, face queries),
  automated light bakes (`do_full_bake_all_levels_automated`, `did_automated_gi_bake_finished`),
  terrain queries (`get_terrain_data`, `get_terrain_height`), `compile_terrain_shaders_dist`, `take_screenshot_from_string_path`,
  and `execute_command_line_command` (run any console command from code).
- Not found: a managed "save scene" call. Saving has to be our own writer, or a native command we drive.

**What does not exist**
- No headless or null-renderer mode in the editor build. Anything that runs the engine needs a window and a GPU.
  It does not need a person.
- The dedicated server (a separate Steam tool, not installed here) is the engine without rendering. The managed code has
  its switches (`/dedicatedcustomserver` and others). It may give us render-free simulation. Untested.

**How scenes are stored** (`Modules\<module>\SceneObj\<scene>\`)
- `scene.xscene`: XML. Entities, transforms, scripts, tags, levels, terrain layers, environment. We can write this.
- `atmosphere.xml`: XML.
- `terrain.bin`: a tagged chunk container (magic `ZGR6 RTRN`, version 2, chunks `MIDX`, `HGHT`, `NRML`, `WGHT`, `PHYM`, ...).
  Same style as the formats we already cracked.
- `navmesh.bin` (magic `RNM1`), `flora.bin`, a `ShaderCache` folder.
- `SceneEditData\<scene>\terrain_ed.bin`: the editor's source data for terrain (heightmap and layer painting).

**Conclusion: build two halves, not one**
- **Offline writers** (Python, no game running) for everything that is a file. Fast, testable by byte comparison, truly headless.
- **An engine bridge** (a C# module plus a command channel) for everything the engine computes better than we could
  reimplement: navmesh generation, light bakes, terrain shader compilation, physics cooking, the editor's own problem
  check, screenshots, playtests. The engine becomes our build server.

This avoids reimplementing a lighting baker and a navmesh generator, which would be most of the cost of "cloning the editor".

## 2. Architecture

```
agent (Claude Code, any MCP client)
   |
   |  one CLI + one MCP server: "bl" (JSON in, JSON out, dry-run on everything)
   |
   +-- offline layer (Python)            reads and writes files, no game
   |     assets:  textures, materials, meshes, skeletons, animations, clips   [have]
   |     scenes:  xscene, atmosphere, terrain.bin, navmesh.bin, flora.bin     [to build]
   |     data:    ModuleData XML (items, troops, settlements, cultures)       [partly, in the BL2 project]
   |     pack / validate / install / revert                                   [have]
   |
   +-- engine layer (C# module "Bridge" running inside the game or editor build)
   |     build jobs:  navmesh, light bake, terrain shaders, problem check
   |     live edit:   spawn, move, delete, query entities; raycast; screenshot
   |     test:        start a mission or campaign, run a script, report
   |
   +-- Blender layer (through its MCP server)
         sculpt terrain, place and model props, paint layers; export heightmap + placements + meshes
```

Rules that keep it trustworthy:
- Every writer proves itself by rebuilding the game's own files byte for byte (the existing self-test, extended per format).
- Every engine job is unattended, has a timeout, and reports in a log the CLI parses.
- Every game patch: run the self-test first. A failing format is reported by name instead of crashing a mod.
- Read their binaries to learn behaviour; write our own code. No decompiled code in any repository.

## 3. Workstreams

Sizes: S = days, M = a week or two, L = several weeks. They are guesses.

### A. Finish the asset side (mostly S)
| Item | Size | Done when |
|---|---|---|
| Skinned FBX and glTF with any bone names (drop the `bip01_<name>_<index>` rule) | S | a Mixamo or Blender rig imports unchanged |
| Missing texture formats (DXT3, BC6H HDR, R8, float) | S | self-test covers them |
| Clips without a root channel, raw-key clips (71 Native clips we cannot read) | M | 297 of 297 Native clip caches round-trip |
| Physics shapes (collision bodies for props and items) | M | a new prop blocks agents and missiles without the editor |
| Morph animations (faces), cloth | M each | on demand |
| Particle systems, decals, prefabs with scripts | M | on demand |
| Sound (FMOD banks) | have in the BL2 project | move into the SDK |
| Run the published SDK in game once, with a screenshot | S | README no longer says "not run in game" |

### B. Scenes (L, the main new work)
| Step | What | Size | Done when |
|---|---|---|---|
| B1 | Read and write `scene.xscene` and `atmosphere.xml` | S | all 79 + 202 + 195 shipped scenes round-trip |
| B2 | Edit an existing scene offline: add, move, remove entities, spawn points, tags | S | a shipped battle map with our props loads and plays |
| B3 | Decode `terrain.bin` (heights, normals, layer weights, physics materials) | M | every shipped `terrain.bin` rebuilds byte-identical |
| B4 | Write terrain from a heightmap and layer masks | M | a Blender-sculpted terrain loads with correct collision and textures |
| B5 | Navmesh: first through the engine bridge, later decode `navmesh.bin` | M | AI walks and fights on a generated scene |
| B6 | Flora scatter (`flora.bin`), water, light bake through the bridge | M | a generated scene looks finished, not flat-lit |
| B7 | Scene kinds: battle, siege (walls, gates, ladders, machines), town, village, hideout, arena | L | one generated scene of each kind passes its mission test |
| B8 | The campaign world map scene | L | a new continent with settlements loads in a campaign |

Start with B1 to B2: they are small and already make procedural level work possible on existing terrain.

### C. The loop (M, in parallel with everything)
| Item | Done when |
|---|---|
| One CLI, `bl`, wrapping everything, with `--json` and `--dry` | an agent never parses free text |
| MCP server over the same commands | Claude and other agents call tools, not shell lines |
| Bridge module (C#) with a file or socket command channel | "spawn this prefab at x,y" works on a running game |
| Project layout and `bl new` (module skeleton, SubModule.xml, C# project, test words) | a new mod builds and boots in one command |
| `bl run` (launch, wait for marker, collect log, crash events, screenshots) | from the existing harness, generalised |
| Game-version check and compatibility report | after a patch, one command says which formats still hold |
| CI for everything that needs no game | synthetic tests run on GitHub |
| Worked example mod (original content) | the first `examples/` entry for the knowledge-base repo |

### D. Everything else, on demand
Gauntlet UI (XML prefabs, sprite sheets: generators already ship with the game), shaders and post effects, banners and
icons, cutscene and camera scripting, save-game reading, multiplayer and dedicated server content. Each gets decoded when a
real project needs it.

### E. Bannerlord as an engine (the end goal)
The engine does not need Calradia. A module can add its own main-menu entry and start its own game type; Custom Battle is
itself a module that does this, and the editor has an `EditorGame`.
| Step | What |
|---|---|
| E1 | "Engine template": a module that boots to its own menu and into its own mission, with no campaign and no vanilla content loaded beyond the core |
| E2 | A small game-rules layer in C#: own win conditions, waves, scoring, spawning, UI, save data |
| E3 | Own world map (B8) or no map at all |
| E4 | A complete small game made only with the pipeline, as the proof |

## 4. Things this makes possible (general Bannerlord, not BL2)

1. **A unique battlefield for every spot on the map.** The game reuses about 70 battle terrains. Generate the battlefield
   from the campaign map's own terrain where the armies met: that hill, that river, that forest.
2. **Every settlement different.** Towns, castles and villages assembled from the culture's prop kits with working walls,
   gates, siege machines and navmesh. 300 settlements, none repeated.
3. **A new continent per campaign.** Procedural world map with settlements, roads and cultures placed by rules. A
   Bannerlord campaign that is different every time.
4. **Real-world battlefields.** Elevation data of a real place in, a playable battle scene out. Hastings, Agincourt, your town.
5. **Blender as the level editor.** Export any scene to Blender, sculpt and place there through its MCP server, import back.
   The official editor's job, with better tools and an agent driving.
6. **Live editing of a running game.** The bridge lets an agent place a tower, look at a screenshot, move it, and run a
   skirmish there, without leaving the mission.
7. **Faction packs from a description.** Culture, troop tree, armour and weapons (generated models, auto-rigged), banner,
   building kit, settlements. One prompt, one faction.
8. **A balance lab.** Thousands of unattended AI battles to tune troop trees and weapons with numbers, not opinions.
9. **Dungeons and scripted encounters.** Hideouts and interiors generated with patrols, loot and a boss.
10. **Cinematics.** Scripted cameras and agents for trailers and cutscenes.
11. **Patch survival.** After a game update, a report of what broke in a mod and where.
12. **Standalone games on the engine.** Siege defence, an arena roguelike, a mass-battle sandbox, a tactics game with no campaign.

## 5. Order of work

| Milestone | Contents | Proof |
|---|---|---|
| M0 | Try the editor switches and the engine API from a test module: `/open_scene`, `/nativeTest`, `execute_command_line_command`, `create_new_scene`, automated bake, navmesh calls | a table of what works, what does not |
| M1 | B1 + B2, bridge module v0, published SDK run in game | a shipped map with generated props, played, with a screenshot |
| M2 | B3 + B4 with Blender heightmap export | a sculpted terrain you can ride across |
| M3 | B5 + B6 | AI armies fight on a generated, lit, planted scene |
| M4 | `bl` CLI + MCP server + `bl new` + worked example | the example mod made start to finish by an agent |
| M5 | B7 scene kinds, asset gaps from stream A as they bite | a generated siege that plays |
| M6 | B8 world map | a new continent in a campaign |
| M7 | Stream E | a small standalone game |

M0 decides how much of B5 and B6 is "call the engine" and how much is "decode the file", so it comes first and it is short.

## 6. Risks

- **Game patches.** Layouts move. Mitigation: byte-comparison self-tests per format, a version report, pinned tags per game build.
- **The engine half needs a GPU and a window.** No fully headless bake or navmesh unless the dedicated server build can do
  it. A CI machine cannot run those jobs; a home PC can.
- **The editor switches may not work in the shipped build.** They are TaleWorlds' internal jobs. M0 finds out.
- **Terrain is the hard format.** Compressed chunks, level-of-detail data, shader caches per scene. If it resists, the
  fallback is scenes built on existing terrains, which is still most of the value.
- **Licence and rules.** Formats learned from the user's own install, own code only, no game files or decompiled code
  published. The Modding Kit's terms should be read before anything about its internals is published.
- **Scope.** "Anything in the game" is open-ended. The order above delivers something usable at every milestone.

## 7. Doing the engine's computation ourselves

The two-halves design uses the engine as a build server. The longer aim is to need it for nothing. What each piece takes:

| Computation | What the game ships that helps | Our own version | Size |
|---|---|---|---|
| Navmesh generation | 19 of 98 shipped battle terrains are marked auto-generated, so the editor has a generator | Our own algorithm; it only has to produce a valid file and good paths, not match theirs. Terrain navmesh is the easy case: walkable by slope and water depth, then merge grid cells into polygons | M, after the `navmesh.bin` format |
| Physics cooking | `PhysXCooking_64.dll` ships in `bin` | Call the same cooking library, or let the engine cook at load | S to M |
| Terrain normals, levels of detail, material maps | all inside `terrain.bin` | Computed by us once the chunks are decoded | M |
| Terrain shaders | 848 shader source files under `Shaders\Sources` (terrain ones included) and `d3dcompiler_47.dll`; the per-scene `ShaderCache` is a 28 KB header file, not compiled shaders | Generate the header ourselves; compile with the shipped compiler if needed | M |
| Light and GI bake | none of the 67 shipped battle terrains that carry the flag has baked GI | Not needed for battlefields. For towns and interiors: our own probe baker, the largest single item | L or more |
| Scene optimisation, occlusion, flora billboards | native only | On demand | M each |

Method: static reading in Ghidra (two analysed projects exist) plus the running editor as an oracle. Give the editor a
known input (a flat terrain, one hill, one box), capture what it writes, and compare with our output. Hooks on the running
process (Frida or a debugger) can dump the buffers at function boundaries when a file alone does not explain itself. This
is single-player software on the owner's machine; nothing here touches multiplayer or anti-cheat.

## 8. Decisions taken (2026-10-10)

- The SDK and its expansion live in this repository.
- The SDK is built first. The generated-battlefield mod is a standalone tech demo made with it, in its own mod folder, not SDK code.
- SDK pieces the demo needs, in order: scene files (B1, B2), a world-map reader, terrain (B3, B4), navmesh (B5), flora (B6),
  and a C# runtime library so a mod can generate at battle time without Python.
- The Bannerlord dedicated server (Steam app 1863440) will be installed to test render-free simulation.
