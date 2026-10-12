// GenCli <data folder of one biome> <out folder> <scene name> [kind=valley] [rugged=0.6] [forest=0.2] [water=river|sea] [seed=7] [repeat=1]
// Runs the mod's generator outside the game and prints what it made and how long each step took (milliseconds).
using System;
using System.Globalization;
using GenFields.Gen;

static class Program
{
    static int Main(string[] a)
    {
        if (a.Length < 3) { Console.WriteLine("GenCli <data folder> <out folder> <scene name> [kind=] [rugged=] [forest=] [water=] [seed=] [repeat=]"); return 2; }
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var data = BiomeData.Load(a[0]);
        Console.WriteLine("data of " + data.Biome + " loaded in " + sw.ElapsedMilliseconds + " ms: " + data.L + " layers, " + data.Rocks.Count + " rocks");
        var r = new FieldRules { Biome = data.Biome, Seed = 7 }; int repeat = 1;
        for (int i = 3; i < a.Length; i++)
        {
            string[] kv = a[i].Split('='); if (kv.Length != 2) continue;
            switch (kv[0])
            {
                case "kind": r.Kind = kv[1]; break;
                case "rugged": r.Rugged = float.Parse(kv[1], CultureInfo.InvariantCulture); break;
                case "forest": r.Forest = float.Parse(kv[1], CultureInfo.InvariantCulture); break;
                case "water": r.Water = kv[1]; break;
                case "seed": r.Seed = int.Parse(kv[1]); break;
                case "repeat": repeat = int.Parse(kv[1]); break;
            }
        }
        for (int n = 0; n < repeat; n++)
        {
            var res = Generator.Generate(data, r, a[1], a[2]);
            Console.WriteLine(string.Format(CultureInfo.InvariantCulture, "{0} {1} seed {2}: relief {3:F0} m, steep {4:P0}, forest {5:P0}, water {6:P0}, {7} rocks, {8} nav faces, {9} entities, {10} paths, {11:F2} s",
                data.Biome, res.Kind, r.Seed, res.Relief, res.Steep, res.ForestShare, res.WaterShare, res.Rocks, res.NavFaces, res.Entities, res.Paths, res.Seconds));
            Console.WriteLine("  ms: " + res.Timings);
            r.Seed++;
        }
        return 0;
    }
}
