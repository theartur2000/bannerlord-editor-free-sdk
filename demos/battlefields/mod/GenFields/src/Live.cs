// Live generation: a new battlefield for every battle, made when the battle starts.
//
// The engine only loads scene folders it saw at startup, but one it saw can be rewritten while the game runs. So the
// module ships one slot per biome (SceneObj/gf_live_plain, _steppe, _desert: a slot keeps its template's texture layers,
// because the game caches the terrain shader per scene name) and the generator (src/gen) rewrites three files in the slot:
// scene.xscene, terrain.bin, navmesh.bin. What the generator needs from the game's own data sits in GenData/<Biome>,
// written once by demos/battlefields/livedata.py.
//
// The rules of a field come from the world map around the party: the biome under it, how much mountain, forest, river
// and sea lie around it, and in which direction the water is.
// A file Modules/GenFields/genfields_nolive.txt switches this off (the pool of ready-made fields is used instead).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using GenFields.Gen;
using TaleWorlds.CampaignSystem;
using TaleWorlds.CampaignSystem.Party;
using TaleWorlds.Core;
using TaleWorlds.Library;
using TaleWorlds.ModuleManager;
using Path = System.IO.Path;

namespace GenFields
{
    internal static class Live
    {
        static readonly Dictionary<string, BiomeData> Data = new Dictionary<string, BiomeData>();
        static readonly Dictionary<string, string> Last = new Dictionary<string, string>();     // slot -> what is in it now
        static readonly Random Rng = new Random();

        static string ModulePath => ModuleHelper.GetModuleFullPath(SubModule.ModuleId);
        // The slots of a biome: gf_live_plain, gf_live_plain_2, ... Once the engine has loaded a scene it keeps that
        // scene's terrain file open for the rest of the session (measured: still held 90 s and three battles later, and it
        // shares the file for reading only), so a slot can take one field a session. The module therefore carries many
        // slots a biome. The first PlayableSlots hold a real field and are listed in Custom Battle; the others are
        // placeholders: empty scene, terrain and navmesh files, which is all the engine needs to know the folder at
        // startup. Every battle writes into the next slot not used this session; at the next start of the game the used
        // placeholders are emptied again.
        public const int PlayableSlots = 4;
        public static string Slot(string biome, int n = 0) => "gf_live_" + biome.ToLowerInvariant() + (n == 0 ? "" : "_" + (n + 1));
        static readonly HashSet<string> Used = new HashSet<string>();

        static int SlotCount(string biome) { int n = 0; while (Directory.Exists(Path.Combine(ModulePath, "SceneObj", Slot(biome, n)))) n++; return n; }

        static bool Free(string slot)
        {
            string f = Path.Combine(ModulePath, "SceneObj", slot, "terrain.bin");
            if (!File.Exists(f)) return false;
            try { using (new FileStream(f, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) return true; } catch (IOException) { return false; } catch (UnauthorizedAccessException) { return false; }
        }

        // how many slots of every biome the engine holds right now, as text for the log
        public static string Held()
        {
            var parts = new List<string>();
            foreach (string b in new[] { "Plain", "Steppe", "Desert" }) { int n = SlotCount(b), held = 0; for (int q = 0; q < n; q++) if (!Free(Slot(b, q))) held++; if (n > 0) parts.Add(b + " " + held + " of " + n); }
            return string.Join(", ", parts);
        }

        // At the start of the game: the placeholder slots a past session filled are emptied again (the files stay, with
        // nothing in them). Nothing is held yet at that moment.
        public static void EmptyPlaceholders()
        {
            try
            {
                int emptied = 0;
                foreach (string b in new[] { "Plain", "Steppe", "Desert" })
                    for (int n = PlayableSlots; n < SlotCount(b); n++)
                        foreach (string fn in new[] { "scene.xscene", "terrain.bin", "navmesh.bin" })
                        {
                            string f = Path.Combine(ModulePath, "SceneObj", Slot(b, n), fn);
                            if (File.Exists(f) && new FileInfo(f).Length > 0) { File.WriteAllBytes(f, new byte[0]); emptied++; }
                        }
                if (emptied > 0) Log.Write("live: " + emptied + " files of slots used last session emptied");
            }
            catch (Exception e) { Log.Write("live: emptying the slots failed: " + e.Message); }
        }

        public static bool IsSlot(string scene) => scene != null && scene.StartsWith("gf_live_", StringComparison.Ordinal);
        public static string Describe(string scene) => Last.TryGetValue(scene, out string d) ? d : scene;

        public static bool Has(string biome) =>
            File.Exists(Path.Combine(ModulePath, "GenData", biome, "skeleton.xscene")) && File.Exists(Path.Combine(ModulePath, "SceneObj", Slot(biome), "scene.xscene"));

        public static bool On => !File.Exists(Path.Combine(ModulePath, "genfields_nolive.txt")) && (Has("Plain") || Has("Steppe") || Has("Desert"));

        static BiomeData DataOf(string biome)
        {
            if (!Data.TryGetValue(biome, out var d)) { var t0 = DateTime.UtcNow; d = BiomeData.Load(Path.Combine(ModulePath, "GenData", biome)); Data[biome] = d; Log.Write("live: data of " + biome + " read in " + (DateTime.UtcNow - t0).TotalMilliseconds.ToString("F0") + " ms"); }
            return d;
        }

        // generates a field into the biome's slot; the slot's scene name, or null when it could not be done
        public static string Generate(FieldRules rules, bool listedOnly = false)
        {
            try
            {
                if (!Has(rules.Biome)) { Log.Write("live: no data or slot for " + rules.Biome); return null; }
                // the next slot not used this session: placeholders first in a campaign, the listed ones in a Custom Battle
                string slot = null; int count = SlotCount(rules.Biome), first = listedOnly ? 0 : Math.Min(PlayableSlots, count), last = listedOnly ? Math.Min(PlayableSlots, count) : count;
                for (int pass = 0; pass < 2 && slot == null; pass++)
                {
                    for (int n = first; n < last && slot == null; n++) { string sn = Slot(rules.Biome, n); if (!Used.Contains(sn) && Free(sn)) slot = sn; }
                    if (slot == null && !listedOnly) { first = 0; last = Math.Min(PlayableSlots, count); }      // placeholders used up: the listed ones
                }
                if (slot == null) { Log.Write("live: no free slot left for " + rules.Biome + " this session (held: " + Held() + ")"); return null; }
                Used.Add(slot);
                var res = Generator.Generate(DataOf(rules.Biome), rules, Path.Combine(ModulePath, "SceneObj", slot), slot);
                string what = rules.Biome.ToLowerInvariant() + " " + res.Kind + ", seed " + rules.Seed;
                Last[slot] = what;
                Log.Write("live: slot " + slot + " (" + Used.Count + " used this session)");
                Log.Write(string.Format(CultureInfo.InvariantCulture, "live: {0} (rugged {1:F2}, forest {2:F2}, water {3}) made in {4:F2} s: relief {5:F0} m, {6} rocks, {7} nav faces, {8} paths; ms: {9}",
                    what, rules.Rugged, rules.Forest, rules.Water ?? "none", res.Seconds, res.Relief, res.Rocks, res.NavFaces, res.Paths, res.Timings));
                return slot;
            }
            catch (Exception e) { Log.Write("live: generation failed: " + e); return null; }
        }

        // the rules for the main party's spot on the world map
        public static FieldRules RulesHere()
        {
            var r = new FieldRules { Seed = Rng.Next(1, int.MaxValue) };
            try
            {
                var map = Campaign.Current.MapSceneWrapper; CampaignVec2 at = MobileParty.MainParty.Position;
                TerrainType here = map.GetFaceTerrainType(at.Face);
                int n = 0, rough = 0, wood = 0, river = 0, sea = 0; float wx = 0, wy = 0; var land = new Dictionary<string, int>();
                for (int dir = 0; dir < 16; dir++)
                {
                    float a = dir * (float)Math.PI / 8f, cx = (float)Math.Cos(a), cy = (float)Math.Sin(a);
                    for (int step = 1; step <= 6; step++)
                    {
                        var p = at + new CampaignVec2(new Vec2(cx * step * 3f, cy * step * 3f), true);
                        if (!p.Face.IsValid()) continue;
                        TerrainType t = map.GetFaceTerrainType(p.Face); n++;
                        if (t == TerrainType.Mountain || t == TerrainType.Canyon || t == TerrainType.Cliff) rough++;
                        if (t == TerrainType.Forest) wood++;
                        if (t == TerrainType.River || t == TerrainType.Fording || t == TerrainType.Bridge || t == TerrainType.NonNavigableRiver || t == TerrainType.UnderBridge) { river++; }
                        if (t == TerrainType.Water || t == TerrainType.Lake || t == TerrainType.CoastalSea || t == TerrainType.OpenSea || t == TerrainType.Beach) { sea++; wx += cx / step; wy += cy / step; }
                        string b = Biome(t); if (b != null) land[b] = (land.TryGetValue(b, out int c) ? c : 0) + 1;
                    }
                }
                r.Biome = Biome(here) ?? (land.Count > 0 ? land.OrderByDescending(kv => kv.Value).First().Key : "Plain");
                float nn = Math.Max(1, n), fr = rough / nn, fw = wood / nn, fv = river / nn, fs = sea / nn;
                if (here == TerrainType.Mountain || here == TerrainType.Canyon) fr = Math.Max(fr, 0.4f);
                if (here == TerrainType.Forest) fw = Math.Max(fw, 0.3f);
                r.Rugged = MathF.Clamp(0.22f + 1.3f * fr + ((float)Rng.NextDouble() - 0.4f) * 0.35f, 0.1f, 1f);
                r.Forest = MathF.Clamp(0.08f + 1.4f * fw + ((float)Rng.NextDouble() - 0.5f) * 0.2f, 0.02f, 0.9f);
                if (r.Biome == "Desert") r.Forest = Math.Min(r.Forest, 0.06f); else if (r.Biome == "Steppe") r.Forest = Math.Min(r.Forest, 0.25f);
                if (fv > 0.03f && fv >= fs * 0.3f) r.Water = "river"; else if (fs > 0.1f) { r.Water = "sea"; r.WaterDir = (float)Math.Atan2(wy, wx); }
                Log.Write(string.Format(CultureInfo.InvariantCulture, "live: map terrain here {0}; around: mountain {1:P0}, forest {2:P0}, river {3:P0}, sea {4:P0} -> biome {5}", here, fr, fw, fv, fs, r.Biome));
            }
            catch (Exception e) { Log.Write("live: map lookup failed: " + e.Message); }
            return r;
        }

        static string Biome(TerrainType t)
        {
            switch (t)
            {
                case TerrainType.Desert: case TerrainType.Dune: return "Desert";
                case TerrainType.Steppe: return "Steppe";
                case TerrainType.Plain: case TerrainType.Forest: case TerrainType.Snow: case TerrainType.Mountain: case TerrainType.Canyon: case TerrainType.Swamp: return "Plain";
                default: return null;           // farmland, water, bridges say nothing about the land
            }
        }
    }
}
