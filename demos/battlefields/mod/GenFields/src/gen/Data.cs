// What the generator knows about a biome: read once from the folder demos/battlefields/livedata.py wrote
// (the template's texture layers and how it paints them, the rocks and their shapes, props, small relief of shipped fields).
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace GenFields.Gen
{
    public sealed class BiomeData
    {
        public string Biome, Skeleton, WaterLevel, WaterExists;
        public int Nx, Ny, L;
        public string[] Role, LayerName; public bool[][] Enabled;   // Enabled[season][layer]; seasons: summer, fall, winter, spring
        public bool[] Meshes, Flora;
        public float[][] QIn, QOut;                                 // per layer: weight at 1025 quantiles, fighting area and outskirts
        public List<RockKind> Rocks = new List<RockKind>();
        public List<(string name, float weight)> Props = new List<(string, float)>();
        public short[][] Fine, Mid; public int[] FineW, FineH, MidW, MidH;   // millimetres; Mid is a quarter of the samples

        static float F(string s) => float.Parse(s, CultureInfo.InvariantCulture);

        public static BiomeData Load(string dir)
        {
            var d = new BiomeData { Skeleton = File.ReadAllText(Path.Combine(dir, "skeleton.xscene")) };
            var layers = new List<string[]>();
            foreach (string line in File.ReadAllLines(Path.Combine(dir, "template.txt")))
            {
                string[] w = line.Split(' ');
                switch (w[0])
                {
                    case "biome": d.Biome = w[1]; break;
                    case "nodes": d.Nx = int.Parse(w[1]); d.Ny = int.Parse(w[2]); break;
                    case "layers": d.L = int.Parse(w[1]); break;
                    case "water_level": d.WaterLevel = w[1]; break;
                    case "water_exists": d.WaterExists = w[1]; break;
                    case "layer": layers.Add(w); break;
                }
            }
            if (d.Nx != 6 || d.Ny != 6) throw new InvalidDataException("the generator is written for 6 x 6 nodes");
            d.Role = new string[d.L]; d.LayerName = new string[d.L]; d.Meshes = new bool[d.L]; d.Flora = new bool[d.L];
            d.Enabled = new[] { new bool[d.L], new bool[d.L], new bool[d.L], new bool[d.L] };
            foreach (var w in layers)
            {
                int i = int.Parse(w[1]); d.Role[i] = w[2]; d.LayerName[i] = w[3];
                for (int s = 0; s < 4; s++) d.Enabled[s][i] = w[4 + s] == "1";
                d.Meshes[i] = w[8] == "1"; d.Flora[i] = w[9] == "1";
            }
            var pb = File.ReadAllBytes(Path.Combine(dir, "paint.bin"));
            d.QIn = new float[d.L][]; d.QOut = new float[d.L][];
            for (int i = 0; i < d.L; i++)
            {
                d.QIn[i] = new float[1025]; d.QOut[i] = new float[1025];
                Buffer.BlockCopy(pb, i * 2 * 1025 * 4, d.QIn[i], 0, 1025 * 4); Buffer.BlockCopy(pb, (i * 2 + 1) * 1025 * 4, d.QOut[i], 0, 1025 * 4);
            }
            var rb = File.ReadAllBytes(Path.Combine(dir, "rocks.bin")); int o = 0;
            foreach (string line in File.ReadAllLines(Path.Combine(dir, "rocks.txt")))
            {
                string[] w = line.Split(' '); if (w.Length < 15) continue;
                var r = new RockKind { Name = w[0], Kind = w[1], N = F(w[2]), Scale = F(w[3]), ScaleHi = F(w[4]), Dz = F(w[5]), DzLo = F(w[6]), DzHi = F(w[7]), Tilt = F(w[8]),
                                       Facing = F(w[9]), FacingR = F(w[10]), Lean = F(w[11]) };
                int nr = int.Parse(w[12]), nb = int.Parse(w[13]), nt = int.Parse(w[14]);
                r.Rim = new float[nr * 3]; Buffer.BlockCopy(rb, o, r.Rim, 0, nr * 12); o += nr * 12;
                r.Body = new float[nb * 3]; Buffer.BlockCopy(rb, o, r.Body, 0, nb * 12); o += nb * 12;
                r.Tris = new int[nt * 3]; Buffer.BlockCopy(rb, o, r.Tris, 0, nt * 12); o += nt * 12;
                r.Prepare(); d.Rocks.Add(r);
            }
            foreach (string line in File.ReadAllLines(Path.Combine(dir, "props.txt")))
            {
                string[] w = line.Split(' '); if (w.Length >= 2) d.Props.Add((w[0], F(w[1])));
            }
            using (var f = new BinaryReader(File.OpenRead(Path.Combine(dir, "detail.bin"))))
            {
                f.ReadBytes(4); int n = f.ReadInt32(); f.ReadSingle();
                d.Fine = new short[n][]; d.Mid = new short[n][]; d.FineW = new int[n]; d.FineH = new int[n]; d.MidW = new int[n]; d.MidH = new int[n];
                for (int k = 0; k < n; k++)
                {
                    d.FineH[k] = f.ReadInt32(); d.FineW[k] = f.ReadInt32(); d.MidH[k] = f.ReadInt32(); d.MidW[k] = f.ReadInt32();
                    d.Fine[k] = new short[d.FineH[k] * d.FineW[k]]; var b = f.ReadBytes(d.Fine[k].Length * 2); Buffer.BlockCopy(b, 0, d.Fine[k], 0, b.Length);
                    d.Mid[k] = new short[d.MidH[k] * d.MidW[k]]; b = f.ReadBytes(d.Mid[k].Length * 2); Buffer.BlockCopy(b, 0, d.Mid[k], 0, b.Length);
                }
            }
            return d;
        }

        // Small relief quilted from the donors: square windows of two patches a side, overlapping by half and fading into
        // each other (raised cosine); every window is cut from a random donor at a random offset, flip and turn.
        // Returns w x h samples in metres.
        public static float[] Quilt(short[][] donors, int[] dw, int[] dh, int w, int h, int patch, Rng rng)
        {
            int small = int.MaxValue; for (int k = 0; k < donors.Length; k++) small = Math.Min(small, Math.Min(dw[k], dh[k]));
            int p = Math.Max(8, patch), win = 2 * p; if (win > small) { p = small / 2; win = 2 * p; }
            var kk = new float[win]; for (int i = 0; i < win; i++) kk[i] = 0.5f - 0.5f * (float)Math.Cos(2 * Math.PI * (i + 0.5) / win);
            int W = w + 2 * win, H = h + 2 * win; var acc = new float[W * H]; var tot = new float[W * H];
            for (int y0 = 0; y0 < h + win; y0 += p)
                for (int x0 = 0; x0 < w + win; x0 += p)
                {
                    int dnr = rng.Int(donors.Length); var d = donors[dnr]; int sw = dw[dnr];
                    int oy = rng.Int(dh[dnr] - win + 1), ox = rng.Int(dw[dnr] - win + 1);
                    bool fy = rng.Int(2) == 1, fx = rng.Int(2) == 1; int rot = rng.Int(4);
                    for (int j = 0; j < win; j++)
                    {
                        int o = (y0 + j) * W + x0;
                        for (int i = 0; i < win; i++)
                        {
                            // (j, i) of the window after the turn and the flips, read back from the donor
                            int a = j, b = i, t;
                            for (int q = 0; q < rot; q++) { t = a; a = b; b = win - 1 - t; }
                            if (fy) a = win - 1 - a; if (fx) b = win - 1 - b;
                            float wt = kk[j] * kk[i];
                            acc[o + i] += d[(oy + a) * sw + ox + b] * wt; tot[o + i] += wt;
                        }
                    }
                }
            var res = new float[w * h];
            for (int j = 0; j < h; j++) for (int i = 0; i < w; i++) { int o = (j + win) * W + i + win; res[j * w + i] = acc[o] / Math.Max(tot[o], 1e-6f) * 0.001f; }
            return res;
        }
    }
}
