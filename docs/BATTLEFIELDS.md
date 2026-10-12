# Generated battlefields: research and design for the first tech demo

This is the design of a standalone mod, a tech demo built with the SDK. It is not an SDK feature. It lives here until the
demo has its own home, because it sets the order in which the SDK grows.

Written 2026-10-10. Question: can a unique battlefield be generated right before each battle, quickly, from the spot on
the world map where the armies met, so the battle feels like it happens in that place?

Short answer: yes in principle, and nobody appears to have done it for Bannerlord. Three things have to be proven first
(section 5). Nothing here is built yet.

## 1. What the game does today

Read from the game's own code and data, v1.4.8.119303.

- The world map carries a "battle scene index map": a grid where every cell holds a scene index. When a battle starts the
  game reads the cell under the player (`MapPatchData`: a scene index and the normalised position inside the patch).
- `SceneModel.GetBattleSceneForMapPatch` turns that into a scene name by looking in `sp_battle_scenes.xml`, where each
  hand-made scene lists the map indices it stands for, its terrain type and its forest density.
- The mission then loads that scene with the time of day, season and weather from the campaign, and with the patch
  position and the direction the armies met from, which pick the spawn sides.
- There are 98 `battle_terrain_*` scenes. So the map is covered by reusing them. TaleWorlds said publicly that Bannerlord
  has no randomly generated battle scenes, unlike Warband.
- `SceneModel` is a replaceable game model. A mod can return any scene name from it. **This is the hook: no engine patch
  is needed to send a battle to our own scene.**

Sizes, from the files:
| | World map | One battlefield |
|---|---|---|
| Terrain grid | 16 x 16 nodes of 53 units = 848 units across | 6 x 6 nodes of 200 m = 1,200 m across |
| Height range | -0.7 to 23 units | for example 41 to 181 m |
| `terrain.bin` | 27.6 MB | 11.5 MB |
| `navmesh.bin` | 0.8 MB | 1.0 MB |
| `scene.xscene` | 19.9 MB | 0.16 MB |
| Baked GI | | none in any shipped battlefield |

The engine API gives a mod the world map's height and normal at any point, the terrain type of any face, the season and the
snow grid. The map's own trees are in its `flora.bin`.

## 2. How other games solved it

- **Rome: Total War and Medieval II** built the battlefield from the campaign map's own height data, scaled up, so the hill
  you camped on was the hill you fought on. This is from players and modders, not from an official paper.
- **Total War from Rome II on** uses tiles: each tile is a hand-made piece (height, texture blend, trees, rocks, buildings),
  the battlefield is assembled from the tiles around the battle position, and one low-frequency height map runs across
  all of them to give the large shapes. This is documented in Creative Assembly's own tool pages.
- **Far Cry 5** fills its world with rule-driven tools: biomes, terrain texturing, rivers, cliffs.
- **Horizon Zero Dawn** places vegetation, rocks and more at run time on the GPU from density maps and rules, as the player moves.
- **Terrain texturing by rule** (height, slope, noise curves per layer) is standard and fast.
- **Navigation meshes** are routinely built at run time in tiles (Recast). Reported build times vary from a fraction of a
  second to minutes per tile depending on settings, so it has to be measured, not assumed.

## 3. The design

A battlefield is made of four layers, coarse to fine. Everything is seeded by the map position, so the same place always
gives the same field.

| Layer | Source | What it gives |
|---|---|---|
| 1. Macro shape | the world map's height field around the battle position, scaled up (about 100 times across; height by its own factor) | the hills, valleys, coast and river of that exact place |
| 2. Features | prebuilt modular pieces ("stamps") chosen by what the map has there: river crossing, ridge, forest edge, road, farm, ruin, village outskirts, shoreline | blended into the heights, each bringing its own props, navmesh hints and tactical markers |
| 3. Detail | generated: erosion-style noise below the map's resolution, texture layers by height, slope and biome, trees and rocks from density rules that follow the map's own forests | ground that does not look like a stretched map |
| 4. Surroundings | a low-detail ring outside the playable square, shaped from the wider map, with distant settlements as simple silhouettes | the horizon matches the map: that mountain range, that castle, the sea on the correct side |

One map sample covers roughly 30 to 50 m of battlefield after scaling, so layer 1 alone would look smooth and empty.
Layers 2 and 3 are what make it a place.

**What makes it fast**
- Prebake everything that does not depend on the position: the stamps, the texture layer sets per biome with their terrain
  shader data, the prop kits per culture, tree sets per biome and season.
- At battle time only compute: sample the map, blend stamps, add detail, write the files. This is array arithmetic over about
  1.4 million height samples.
- Start generating before the player needs it. The encounter menu already holds the player for seconds. Better: keep the
  field for the current map position warm in the background while the party travels.
- Cache generated fields on disk, newest kept, oldest dropped. Seeds make any field reproducible, so the cache can be small.
- No light bake: shipped battlefields have none.

**What makes it a battle and not a landscape**
- Spawn sides from the direction the armies met (the game already passes this in).
- Tactical markers placed by terrain analysis: high ground, chokepoints, forest edges, fords. The battle AI reads such
  markers in hand-made scenes; generated ones make it use the generated ground.
- Navmesh from the height field: walkable by slope and water depth.
- Time of day, season, weather and snow from the campaign.

**Later, same system**
- Villages and castles nearby appear on the field, not only on the horizon.
- The field remembers: a battle fought twice in one place shows the first one.
- Sieges and settlement scenes from the same macro shape.

## 4. Where the generator runs

Two options, and the choice matters for anyone else who installs the mod.

| Option | Good | Bad |
|---|---|---|
| Python process started by the mod | fastest to build, reuses this SDK directly | every player needs Python; slower start |
| Generator inside the mod (C#, with native helpers if needed) | ships as a normal mod, no dependencies, can run on a background thread while the party travels | the writers have to be ported from Python |

Plan: prove it with Python, then port the battlefield path to C# once the formats are settled.

## 5. What has to be proven first

1. **The engine loads a scene folder that did not exist when the game started.** `Scene.Read(name, module)` reads by name.
   Whether it scans folders once at startup is unknown. If it does, the fallback is a fixed pool of scene folders whose
   files are rewritten before each battle.
2. **`terrain.bin` can be written.** It is a tagged chunk container like the formats already decoded, but it is the
   largest unknown. Until then, battlefields can only reuse shipped terrains with different props, which already gives
   variety but not the map's own shape.
3. **Generation time.** The target is under five seconds on a normal PC, hidden behind the encounter menu. Unmeasured.

Also open: the per-scene terrain shader header (28 KB in `ShaderCache`), whether the navmesh the battle AI expects needs
region and id data beyond the polygons, and how much the tactical markers matter to the AI in practice.

## 5a. Results so far (2026-10-10, game v1.4.8.119303)

Run with the unmodified game plus only the GenFields demo module, unattended, through the SDK harness.

| Question | Answer | Evidence |
|---|---|---|
| Does the game accept a scene the SDK wrote from arrays (terrain, navmesh, scene file)? | Yes | Two generated fields (the land around Varagos Castle and around Zeonica) loaded in Custom Battle; 200 against 200 troops spawned on the generated spawn paths, stood on the ground (the engine's ground height under the player equals the written height), the engine reported the written navmesh face count, both sides marched and fought; no crash |
| Does the engine load a scene folder created after the game started? | No | Copying a new scene folder into the module at the main menu and starting a battle on it crashed in the engine's XML loader (+0x730550, a missing-file read). The same scene installed before startup works |
| Can a scene folder that existed at startup be rewritten while the game runs? | Yes | One scene was installed, its files were overwritten at the main menu with another field, and the battle loaded the new one (the navmesh face count was the swapped-in field's) |
| Does the SDK's LZ4 output, which differs from the game's, load? | Yes | Same runs |
| Is a light bake needed? | No | The fields are lit and shadowed correctly at noon and at dusk without one |

So the design uses a pool: a fixed set of scene folders installed with the mod, rewritten before each battle.

Not answered yet: generation time inside the game (the Python prototype takes 20 to 45 s), rivers and lakes (the map's
rivers come out as dry valleys), trees and bushes (the flora layer is not painted yet), tactical markers for the battle AI,
and a campaign battle (only Custom Battle was run).

## 5b. Change of design (2026-10-10): the map is the rule book, not the shape

Fields copied from the map's heights turned out open and gently rolling: a battle covers so little of the map that the
patch is usually one slope. They also had no designed features. The demo now invents each field
(`demos/battlefields/procgen.py`); `generate.py`, the literal version, stays as the earlier experiment.

| The map decides (rules) | The generator invents, new for every battle |
|---|---|
| biome: plain, steppe, desert, swamp | the layout: ridge, valley, hill, river crossing, forest clearing, pass, coastal bluff, broken ground, mesas |
| how rugged the land is | where the high ground, the chokepoints and the open lanes are |
| whether there is a river, a lake or the sea, and on which side | the river's course and its fords |
| how much forest | where the woods and the clearings are |

How a field is composed:
1. A layout is picked, weighted by the rules, and its landforms are drawn from chains of overlapping hills with uneven outlines.
2. A frame of hills or mountains rises outside the play area; on a coast the sea side stays open.
3. Real landforms and real small relief are lifted from shipped battlefields of the same biome and quilted over it.
4. The ground where the armies deploy is levelled a little.
5. Water: the river is cut with a flat bed, shallow at the fords, and nothing else is left below its level.
6. Textures are painted by job (rock on steep ground, soil in hollows and on banks, grass variants, snow for winter), whatever the
   template scene calls its layers.
7. Forest is grown from the template's tree layer, kept off the deployment ground, cliffs and water.
8. Rocks are placed the way the shipped battlefields place them: which rock, at what scale, how deep in the ground, how tilted
   and turned which way is measured from the shipped scenes of the biome (`catalog.rock_usage`). Designers bury them: three
   metres is typical.
9. High ground and chokepoints get the markers the battle AI reads (TacticalPosition, TacticalRegion).

Making it look natural (`sdk/terraform.py`):
- The landforms are bent by domain warping (the coordinates are displaced by noise before the shape is evaluated), so no
  outline is a circle or a straight bar.
- Running water shapes the result: a stream-power erosion pass (implicit scheme, priority-flood fill, steepest-descent
  routing over a slightly roughened surface) cuts gullies and leaves ridges between them; a thermal pass lets the steepest
  faces slump.
- Where the flow gathers on sloping ground, bare earth and gravel are painted.
- Rocks and trees are spread by Poisson-disk sampling: even spacing without a grid.

What the rocks are (`demos/battlefields/lineup.py` puts every rock prefab alone on flat ground to look at):
- Boulders (`terrain_green_rock_*`, `cave_rock_*`) are closed shapes and are turned any way in the shipped scenes.
- The cliff pieces (`rock_cliff_a/b/c`, `rock_desert_cliff_*`, `cliff_*`) are one-sided faces. The shipped scenes turn every
  one of them the same way on a slope: yaw = downhill direction + 90 degrees, within about 20 degrees, laid back into the
  slope by 0.1 to 0.5 rad. Turned at random, or standing on a crest with no ground behind them, their open backs show as
  shards hanging in the air. The generator copies the measured turn and lean and skips spots with no higher ground behind.

In game (unmodified game plus GenFields): ridge, river crossing, pass and mesa fields loaded and were fought on, 200 against
200; rocks, trees, tall grass and the river's water render. What followed is in 5c.

## 5c. Fitted rocks, ground cover, a cliff shelf, custom assets (2026-10-10)

**Rocks fitted from their real meshes.** `sdk/meshread.py` reads any shipped mesh from the game's packages. Almost every
rock is an open shell: `rock_cliff_a` is 37 m wide, 33 m deep and 30 m tall with 297 m of open rim; of the rocks the
plains use only the small ones (`cave_rock_*`, `terrain_green_rock_a/b/c`, `rock_new_green_*`) are closed. Where a rim is
above the ground the player sees through the rock.
- The shipped scenes do not bury the rims either: over 143 placements of `rock_cliff_b`, a third of the rim (median)
  stands more than 0.3 m in the air, its 95th percentile 4 m up. They hide it behind other rocks and hand-made terrain.
- The same measurement fixes how a scene turns an entity: of the six orders of `rotation_euler`, R = Rz . Rx . Ry
  buries the shipped rims deepest.
- `sdk/rockfit.py` therefore makes the ground meet the rock. For each spot it tries the measured turn, lean and sink
  and a few around them, takes the pose that needs the least earth, refuses poses whose underside is not in the
  ground (a floating boulder), banks earth over what rim is left (slope 0.75) and marks the cells the rock stands on
  for the navmesh. Rocks come before the painting, since they change the ground.
- Budget per job: up to 130 cliff pieces, the outcrops in the open, up to 70 fallen blocks, nearest the fighting first.

**Ground cover.** The grass, plants and small stones of a shipped scene are not entities: each texture layer lists
detail meshes, and the engine scatters them where the layer is painted at more than half strength. The plains template
paints its grass layer on 61 % of the map, the steppe one on 38 %, the desert's bushes on 9 %. The generator painted
far less, so most ground was bare. It now paints each such layer as widely as its template does, in patches that are
nearly on or off, never on steep or wet ground. Dead trees, logs, roots and bones are scattered as the shipped scenes
of the biome use them.

**Cliffs without stripes.** A terrain texture is laid on from above and stretches into stripes on steep ground. 28 of
the shipped battlefields set `use_six_side_uv_mapping` on their rock layers; the generator now does (`scenebuild.edit_layer`).

**A shelf with a drop.** Layout "ledge": a mountain wall on one side, 60 to 80 m away; on the other a sheer drop of 40 to
70 m, 95 to 125 m away, cut after the erosion so it stays vertical. The face and the ground below are kept off the
navmesh. Falling is the game's own: in the first run, with the edge nearer, one army lost 43 of 200 men over it before
contact. Winter is a parameter of the battle, not of the scene: the same scene shows its snow layers and snowy trees.

**The load crash at client +0x53f8ee.** Seen now and then since the first generated scenes. The dump shows the engine's
image resize writing to a null buffer of 65535 x 65535: a weight map of width 0 less one. The game reads 16 layer slots
per terrain node whatever the scene's layer count; the writer wrote one per layer (6 in the desert), so the game read
on past the node into whatever followed. `terrain.build` now always writes 16, `terrain.check` reports nodes that do not.

**Custom assets in a scene** (`sdk/assetpack.py`, `sdk/psk.py`, `demos/bl2field`). Found on the way, each by a black
screen:
- The game loads a module's `Assets` folder (with `RuntimeDataCache`) only when the module also has an `AssetSources`
  folder. Without it nothing of the module loads and nothing is logged.
- The terrain shader compiled for a scene is kept under `ProgramData\Mount and Blade II Bannerlord\Shaders\TerrainShaders\<module>\<scene>`.
  A scene whose layers changed keeps the old shader until that folder is deleted. For the pool of scene folders this
  means one pool per template: a slot must not change its layer set.
- A layer without an area map must name it `none`. An empty name paints the whole terrain black.
- A PSK mesh mirrored into the game's space (y negated) keeps its triangle order. Turned as well, the model is lit from
  the inside and shows black. Check: the signed volume of a closed mesh is positive, as for the game's own.
- A custom model has no collision shape (those are made in the editor). `rockfit.support` raises the ground under it to
  just below its upper surface, inset by two cells, so the terrain is what agents collide with.

**Runs** (unmodified game plus GenFields, 200 against 200, 40 s of fighting each, no crash in 21 runs after the slot
fix): plains valley, mountain shelf and mountain pass in winter, desert hill and mesas, steppe valley and broken
ground, river crossing, forest clearing, coastal bluff; and one field built only from custom models and textures.

**Open.** Generation takes 20 to 40 s in Python, about half of it erosion and rock fitting; the target is 10 s in the
loading screen, which means the C# port. Spawn sides are not checked for fairness. The test shows ground-level views only.

## 5d. The first playtest and the in-game generator (2026-10-11)

**What a player found in the first campaign hour, and what it was** (marked in game with F10, which logs the position):
- Bare, shiny ground without grass: the grass layer with models covered 40 % of a valley floor (shipped 63 %), in
  patches of 24 to 37 m (shipped 8 m: brush dabs), and two or three grass textures were blended at half strength.
  Fix: every ground layer gets the template's own distribution of strengths, separately for the fighting area and the
  outskirts (`Template.paint_stats`), and the grass with models is laid in dabs.
- Too few bushes: plant density among the grass tripled (`edit_layer(mesh_density=...)`).
- Lumpy riding: small relief on gentle ground was 4.7 m rms against 1.5 to 2.5 m in shipped maps; scaled down.
- Fell through the map: the rider was 5.3 m under a bank of earth piled over a rock's rim. The rock's solid body goes on
  under the bank, and an agent on it walks on under the terrain. Earth is now never banked more than 0.35 m above any
  rock's surface (`rockfit.top_surface`), and the module lifts agents found 1.5 m under the terrain.
- A river field with no river on the map: river and coast layouts only where the map has them.

**In a campaign the game chooses where the armies form up** (`BattleSpawnPathSelector`): of the scene's spawn paths the one
that best matches the direction the parties met from, and on it a pivot nearest to where the party stands in the map
tile; both sides are then set at equal distances from the pivot, shifted to stay on the path. So every path a scene
offers is a line the armies may really stand on: only clean bearings are offered, and layouts with one way through
offer only bearings near their axis.

**The generator in C#** (`mod/GenFields/src/gen`: Core, Rocks, Data, Files, Generator; 1,600 lines, no engine calls).
Same steps as `procgen.py`. Per field on the author's machine, all thirty biome and layout combinations: 1.25 to 1.81 s.
A typical run in milliseconds: landforms 80, warp and frame 80, relief 10, fluvial erosion 175, thermal 25, grain 80,
shaping 45, rock spots 90, rocks 195, paint 195, forest 60, packing 60, terrain.bin 160, navmesh 270, scene text 45.
The Python reference took 40 s for the same field under the profiler: 9 s in its pure-Python LZ4, 8 s in noise, 7.5 s
in FFT blurs, 6 s rasterising rock triangles, 3.5 s decoding donor terrains (now prepared ahead by `livedata.py`).
Output checked with the Python readers (scene, terrain, navmesh all valid) and by measurement against the Python field
of the same rules: grass with models 61.8 % against 62.7 %, bumps 3.22 m against 3.21 m.

**Live generation** (`Live.cs`): at `GetBattleSceneForMapPatch` the module samples the world map's terrain types on
sixteen bearings out to 18 map units, derives biome, ruggedness, forest, river or coast and the direction of the water,
generates, and returns the slot's scene name. In game: 1.6 to 1.8 s; five campaign battles on live fields (plains,
desert, steppe with a river) loaded and were fought.

**The engine holds a loaded scene's terrain file.** After a battle the slot's `terrain.bin` could not be rewritten:
sharing violation. Measured over four battles in one session with 20 s pauses: every slot loaded once was still held
90 s and three other scene loads later; the engine's own `ClearOldResourcesAndObjects` at mission end does not release
it. So a slot takes one field a session. The module carries 64 slots a biome; 60 are placeholders with zero-byte
`scene.xscene`, `terrain.bin` and `navmesh.bin`, which is enough for the engine to know the folder at startup (proven:
a campaign battle loaded a field written into such a placeholder). Used placeholders are emptied at the next start.

## 6. Steps

| Step | What | Proof |
|---|---|---|
| F0 | A mod that overrides the scene model and sends every field battle to one chosen shipped scene | battles load there, spawns correct |
| F1 | Copy a shipped battlefield to a new scene folder while the game runs, change its props, load it | answers question 1 |
| F2 | Read the world map: heights, terrain types, forests, around a position; export as images | a picture of "the battlefield's macro shape" for any map position |
| F3 | Decode and rewrite `terrain.bin` | all 98 shipped terrains rebuild byte-identical |
| F4 | Write a terrain from layer 1 + layer 3, navmesh, spawn markers | a battle on the map's own hill |
| F5 | Stamps, flora, tactical markers, surroundings ring | it looks and plays like a made scene |
| F6 | Background generation, cache, C# port | no visible wait before a battle |

F0 to F2 need no new format work and can start now.

## Sources

- Game files and decompiled managed assemblies of the owner's install (scene model, map scene interface, scene files).
- [TaleWorlds on the battle terrain system](https://www.thegamer.com/mount-blade-2-bannerlord-new-multiplayer-duel-mode-battle-terrain-system/),
  [development update](https://gamebanshee.com/xdk3d), [Future Plans](https://www.gamebanshee.com/aywyd),
  [developer Q&A stating there are no random battle scenes](https://gamebanshee.com/k48fk)
- [Total War tile maps](https://wiki.totalwar.com/w/Terry_-_Creating_a_Battle_Tile_Map),
  [low-frequency height map](https://wiki.totalwar.com/w/Terry_-_Low_Frequency_Map),
  [player discussion of Rome's campaign-matched battlefields](https://steamcommunity.com/app/1142710/discussions/0/4521135313690233108),
  [TW Center on shared map data](https://www.twcenter.net/threads/heightmap-calculation.437174/)
- [Procedural World Generation of Far Cry 5 (GDC)](https://gdcvault.com/play/1025557/Procedural-World-Generation-of-Far),
  [GPU-Based Run-Time Procedural Placement in Horizon Zero Dawn (GDC)](https://www.gdcvault.com/play/1024120/GPU-Based-Run-Time-Procedural)
- [Recast graph tiles and run-time updates](https://arongranberg.com/astar/docs/createrecast.html),
  [reported tile build times](https://hub.jmonkeyengine.org/t/navmesh-cell-size/39052/8)
