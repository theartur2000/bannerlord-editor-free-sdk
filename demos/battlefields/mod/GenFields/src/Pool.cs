// The pool of generated battlefields a campaign draws from (made by demos/battlefields/pool.py, installed by install.py --pool).
//
// ModuleData/genfields_pool.txt lists them: scene name, biome (Plain, Steppe, Desert), layout, one per line.
// For every field battle the pool is asked for a scene that fits the spot on the world map:
//   - the biome comes from the terrain type under the party (desert and dunes, steppe, everything else is "Plain");
//   - the layout is weighted by what lies around the party (the map's own terrain types within about 20 map units):
//     mountains, canyons and cliffs favour passes, shelves, valleys and ridges; forest favours clearings; rivers and fords
//     bring river crossings; lakes and the sea bring the coastal bluff;
//   - the last five fields used are left out, so the same one does not come twice in a row.
// A file Modules/GenFields/genfields_off.txt switches the pool off (the game's own scenes are used again).
// In a battle on a pool scene, F10 writes the player's position and facing to genfields_log.txt and takes a screenshot:
// a way to point at a spot ("this rock floats") without describing where it is.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using TaleWorlds.CampaignSystem;
using TaleWorlds.CampaignSystem.Party;
using TaleWorlds.Core;
using TaleWorlds.Engine;
using TaleWorlds.InputSystem;
using TaleWorlds.Library;
using TaleWorlds.ModuleManager;
using TaleWorlds.MountAndBlade;
using Path = System.IO.Path;

namespace GenFields
{
    internal static class Pool
    {
        class Field { public string Name, Biome, Kind; }

        static List<Field> _fields;
        static readonly List<string> Recent = new List<string>();
        static readonly Random Rng = new Random();

        static string ModulePath => ModuleHelper.GetModuleFullPath(SubModule.ModuleId);

        public static bool Off => File.Exists(Path.Combine(ModulePath, "genfields_off.txt"));

        static List<Field> Fields()
        {
            if (_fields != null) return _fields;
            _fields = new List<Field>();
            string f = Path.Combine(ModulePath, "ModuleData", "genfields_pool.txt");
            if (File.Exists(f))
                foreach (string line in File.ReadAllLines(f))
                {
                    string[] w = line.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                    if (w.Length < 3 || w[0].StartsWith("#")) continue;
                    if (!File.Exists(Path.Combine(ModulePath, "SceneObj", w[0], "scene.xscene"))) { Log.Write("pool: " + w[0] + " is listed but not installed, left out"); continue; }
                    _fields.Add(new Field { Name = w[0], Biome = w[1], Kind = w[2] });
                }
            Log.Write("pool: " + _fields.Count + " fields");
            return _fields;
        }

        public static bool Has(string scene) => Fields().Any(f => f.Name == scene);

        public static string Describe(string scene)
        {
            Field f = Fields().FirstOrDefault(x => x.Name == scene);
            return f == null ? scene : f.Biome.ToLowerInvariant() + " " + f.Kind + " (" + f.Name + ")";
        }

        static bool IsLand(TerrainType t) => t == TerrainType.Plain || t == TerrainType.Desert || t == TerrainType.Dune || t == TerrainType.Steppe || t == TerrainType.Forest
                                             || t == TerrainType.Snow || t == TerrainType.Mountain || t == TerrainType.Canyon || t == TerrainType.Swamp;

        static string BiomeOf(TerrainType t) => t == TerrainType.Desert || t == TerrainType.Dune ? "Desert" : t == TerrainType.Steppe ? "Steppe" : "Plain";

        // a pool scene for the main party's spot on the map, or null (pool empty or off)
        public static string Pick()
        {
            if (Off || Fields().Count == 0) return null;
            TerrainType here = TerrainType.Plain; var around = new List<TerrainType>();
            try { around = Campaign.Current.MapSceneWrapper.GetEnvironmentTerrainTypesCount(MobileParty.MainParty.Position, out here); }
            catch (Exception e) { Log.Write("pool: terrain lookup failed: " + e.Message); }
            float n = Math.Max(1, around.Count);
            float rough = around.Count(t => t == TerrainType.Mountain || t == TerrainType.Canyon || t == TerrainType.Cliff) / n;
            float wood = around.Count(t => t == TerrainType.Forest) / n;
            float river = around.Count(t => t == TerrainType.River || t == TerrainType.Fording || t == TerrainType.Bridge || t == TerrainType.NonNavigableRiver || t == TerrainType.UnderBridge) / n;
            float sea = around.Count(t => t == TerrainType.Water || t == TerrainType.Lake || t == TerrainType.CoastalSea || t == TerrainType.OpenSea || t == TerrainType.Beach) / n;
            // the biome under the party; when that says nothing about the land (farmland, water, a bridge), the commonest
            // land type around it
            string biome = BiomeOf(here);
            if (!IsLand(here))
            {
                var land = around.Where(IsLand).ToList();
                if (land.Count > 0) biome = land.Select(BiomeOf).GroupBy(b => b).OrderByDescending(g => g.Count()).First().Key;
            }
            List<Field> all = Fields().Where(f => f.Biome == biome).ToList();
            if (all.Count == 0) all = Fields();
            List<Field> fresh = all.Where(f => !Recent.Contains(f.Name)).ToList();
            if (fresh.Count == 0) fresh = all;
            var weights = new List<float>();
            foreach (Field f in fresh)
            {
                float w;
                switch (f.Kind)
                {
                    case "crossing": w = river > 0.03f ? 6f : 0f; break;        // no river on the map here, no river on the field
                    case "bluff": w = sea > 0.08f ? 6f : 0f; break;
                    case "pass": case "ledge": w = 0.5f + 8f * rough; break;
                    case "valley": case "ridge": case "mesa": w = 1.2f + 3f * rough; break;
                    case "clearing": w = 0.4f + 6f * wood; break;
                    default: w = 1f; break;
                }
                weights.Add(w);
            }
            if (weights.Sum() <= 0f) for (int q = 0; q < weights.Count; q++) weights[q] = 1f;
            double r = Rng.NextDouble() * weights.Sum(); int k = 0;
            for (; k < fresh.Count - 1; k++) { r -= weights[k]; if (r <= 0 && weights[k] > 0f) break; }
            while (k > 0 && weights[k] <= 0f) k--;
            Field pick = fresh[k];
            Recent.Add(pick.Name); if (Recent.Count > 5) Recent.RemoveAt(0);
            Log.Write("pool: terrain here " + here + ", around " + string.Join(" ", around.GroupBy(t => t).OrderByDescending(g => g.Count()).Select(g => g.Key + "x" + g.Count()))
                      + " -> biome " + biome + ", rough " + rough.ToString("F2", CultureInfo.InvariantCulture) + " wood " + wood.ToString("F2", CultureInfo.InvariantCulture)
                      + " river " + river.ToString("F2", CultureInfo.InvariantCulture) + " sea " + sea.ToString("F2", CultureInfo.InvariantCulture) + " -> " + pick.Name);
            return pick.Name;
        }
    }

    // in a battle on a pool scene: says which field this is, and marks spots on F10
    public class FieldNoteLogic : MissionLogic
    {
        bool _said; float _t, _nextCheck; int _marks, _lifted;

        public override void OnMissionTick(float dt)
        {
            _t += dt;
            try
            {
                if (!_said && _t > 2f)
                {
                    _said = true;
                    InformationManager.DisplayMessage(new InformationMessage("Battlefield: " + Pool.Describe(Mission.SceneName) + "   F10 marks a spot"));
                    Log.Write("battle on pool scene " + Mission.SceneName);
                }
                // Nobody stays under the ground. A generated field has no caves or bridges, so an agent more than 1.5 m below
                // the terrain has got under it (a rock's solid body can lead there) and is put back on the surface.
                if (_t > _nextCheck)
                {
                    _nextCheck = _t + 0.5f;
                    foreach (Agent a in Mission.Agents)
                    {
                        if (!a.IsActive() || a.RiderAgent != null && !a.IsMount) continue;
                        if (a.IsHuman && a.MountAgent != null) continue;          // a rider moves with the mount
                        Vec3 p = a.Position; float g = Mission.Scene.GetTerrainHeight(p.AsVec2);
                        if (p.z < g - 1.5f)
                        {
                            a.TeleportToPosition(new Vec3(p.x, p.y, g + 0.1f));
                            if (_lifted++ < 20) Log.Write("lifted " + (a.IsMount ? "a mount" : a.Name) + " from " + (g - p.z).ToString("F1", CultureInfo.InvariantCulture) + " m under the ground at "
                                                          + p.x.ToString("F1", CultureInfo.InvariantCulture) + " " + p.y.ToString("F1", CultureInfo.InvariantCulture) + " on " + Mission.SceneName);
                        }
                    }
                }
                if (Input.IsKeyPressed(InputKey.F10) && Mission.MainAgent != null)
                {
                    Vec3 p = Mission.MainAgent.Position; Vec3 look = Mission.MainAgent.LookDirection;
                    _marks++;
                    string home = Environment.GetEnvironmentVariable("BANNERLORD_SDK_HOME");
                    if (string.IsNullOrEmpty(home)) home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "bannerlord-sdk");
                    string dir = Path.Combine(home, "out", "genfields_marks"); Directory.CreateDirectory(dir);
                    string shot = Path.Combine(dir, Mission.SceneName + "_" + DateTime.Now.ToString("MMdd_HHmmss") + ".bmp");
                    Utilities.TakeScreenshot(shot);
                    Log.Write("MARK " + Mission.SceneName + " at " + p.x.ToString("F1", CultureInfo.InvariantCulture) + " " + p.y.ToString("F1", CultureInfo.InvariantCulture) + " " + p.z.ToString("F1", CultureInfo.InvariantCulture)
                              + " looking " + look.x.ToString("F2", CultureInfo.InvariantCulture) + " " + look.y.ToString("F2", CultureInfo.InvariantCulture) + " " + look.z.ToString("F2", CultureInfo.InvariantCulture) + " shot " + shot);
                    InformationManager.DisplayMessage(new InformationMessage("Spot " + _marks + " marked"));
                }
            }
            catch (Exception e) { Log.Write("field note failed: " + e.Message); }
        }
    }
}
