# Generated battlefields: research and design

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
