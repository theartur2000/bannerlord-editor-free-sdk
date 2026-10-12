// One battlefield, invented from a few rules and written as scene files: the in-game port of
// demos/battlefields/procgen.py (design + write). The Python file is the reference and carries the reasons for every
// number; this one keeps the steps in the same order so the two can be read side by side.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;

namespace GenFields.Gen
{
    public sealed class FieldRules
    {
        public string Biome = "Plain", Kind, Water;     // Kind null: picked from the rules. Water: null, "river", "sea"
        public float Rugged = 0.4f, Forest = 0.2f, WaterDir;
        public int Seed;
    }

    public sealed class FieldResult
    {
        public string Kind, Timings; public double Seconds; public int Rocks, NavFaces, Entities, Paths;
        public float Relief, Steep, ForestShare, WaterShare;
        public List<string> Views = new List<string>();
    }

    public static class Generator
    {
        public static readonly string[] Kinds = { "ridge", "valley", "hill", "crossing", "clearing", "pass", "bluff", "broken", "mesa", "ledge" };
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        static string F3(float v) => v.ToString("F3", Inv);
        static string V3(float x, float y, float z) => F3(x) + ", " + F3(y) + ", " + F3(z);

        public static string PickKind(FieldRules r, Rng rng)
        {
            float g = r.Rugged;
            var w = new Dictionary<string, float> { ["ridge"] = 1f + g, ["valley"] = 0.8f + 1.2f * g, ["hill"] = 1.2f, ["broken"] = 0.5f + g, ["pass"] = 2.2f * Math.Max(0f, g - 0.35f),
                ["clearing"] = 3f * Math.Max(0f, r.Forest - 0.25f), ["ledge"] = 1.6f * Math.Max(0f, g - 0.55f) };
            if (r.Biome == "Desert") { w["mesa"] = 1.2f + g; w["clearing"] = 0f; }
            if (r.Water == "river") w["crossing"] = 3f;
            if (r.Water == "sea") w["bluff"] = 4f;
            double t = rng.Next() * w.Values.Sum();
            foreach (var kv in w) { t -= kv.Value; if (t <= 0 && kv.Value > 0) return kv.Key; }
            return "hill";
        }

        // weights like the template's own: the score decides where, the template's distribution how strong
        static byte[] LikeTemplate(float[] score, float[] qIn, float[] qOut, bool[] inner)
        {
            var o = new byte[score.Length];
            for (int zone = 0; zone < 2; zone++)
            {
                bool want = zone == 0; var q = want ? qIn : qOut;
                float lo = float.MaxValue, hi = float.MinValue; int n = 0;
                for (int k = 0; k < score.Length; k++) if (inner[k] == want) { float v = score[k]; if (v < lo) lo = v; if (v > hi) hi = v; n++; }
                if (n == 0) continue;
                if (hi <= lo) hi = lo + 1e-6f;
                var hist = new int[65536]; float sc = 65535.99f / (hi - lo);
                for (int k = 0; k < score.Length; k++) if (inner[k] == want) hist[(int)((score[k] - lo) * sc)]++;
                var lut = new byte[65536]; long acc = 0;
                for (int b = 0; b < 65536; b++)
                {
                    float mid = (acc + hist[b] * 0.5f) / n * 1024f; acc += hist[b];
                    lut[b] = (byte)Math.Max(0, Math.Min(255, (int)Math.Round(q[(int)Math.Round(mid)])));
                }
                Parallel.For(0, G.N, j => { int k = j * G.N; for (int i = 0; i < G.N; i++, k++) if (inner[k] == want) o[k] = lut[(int)((score[k] - lo) * sc)]; });
            }
            return o;
        }

        static byte[] M8(float[] a) { var o = new byte[a.Length]; G.Rows(j => { int k = j * G.N; for (int i = 0; i < G.N; i++, k++) { float v = a[k]; v = v < 0 ? 0 : v > 1 ? 1 : v; o[k] = (byte)Math.Round(v * 255f); } }); return o; }

        public static FieldResult Generate(BiomeData data, FieldRules rules, string outDir, string sceneName)
        {
            var sw = Stopwatch.StartNew(); var times = new StringBuilder(); long last = 0;
            void Lap(string what) { long t = sw.ElapsedMilliseconds; times.Append(what).Append(' ').Append(t - last).Append("  "); last = t; }
            int N = G.N; float cell = G.CELL, C = G.C, PLAY = G.PLAY, SPAWN = G.SPAWN;
            var rng = new Rng(rules.Seed); float r = rules.Rugged;
            string kind = rules.Kind ?? PickKind(rules, rng);
            if (kind == "mesa" && rules.Biome != "Desert") kind = "broken";
            var F = new Field(rng);
            float axis = rng.Uni(0f, (float)Math.PI);
            if (kind == "bluff") axis = rules.WaterDir + (float)Math.PI / 2;
            float ca = (float)Math.Cos(axis), sa = (float)Math.Sin(axis);
            (float x, float y) Pt(float u, float v) => (C + u * ca - v * sa, C + u * sa + v * ca);
            float Uni(float a, float b) => rng.Uni(a, b);
            float Uof(int i, int j) => (i * cell - C) * ca + (j * cell - C) * sa;
            float Vof(int i, int j) => -(i * cell - C) * sa + (j * cell - C) * ca;
            var rr = G.New(); G.Rows(j => { int k = j * N; float dy = j * cell - C; for (int i = 0; i < N; i++, k++) { float dx = i * cell - C; rr[k] = (float)Math.Sqrt(dx * dx + dy * dy); } });
            var feats = new List<(string typ, float x, float y, float rad, float face)>();

            var z = G.Fbm(520f, 3, rng, 0.5f, false, 0.4f); { float a = 5f + 12f * r; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) z[k] *= a; }); }
            bool river = false, sea = false, ring = false, ledge = false;
            float rvU0 = 0, rvAmp = 0, rvLam = 1, rvPhi = 0; var fords = new List<float>();
            float seaShore = 0, seaBluff = 0, ringRadius = 0, ledgeHalf = 0, ledgeEdge = 0, ledgeDrop = 0, ledgeWall = 0;
            void Add(float[] a) { G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) z[k] += a[k]; }); }
            List<(float x, float y)> Line(float u0, float v0, float u1, float v1, int count)
            { var l = new List<(float, float)>(); for (int k = 0; k < count; k++) l.Add(Pt(u0 + (u1 - u0) * k / (count - 1), v0 + (v1 - v0) * k / (count - 1))); return l; }

            switch (kind)
            {
                case "ridge":
                {
                    float u0 = Uni(-70, 70), half = Uni(340, 430), bend = Uni(-70, 70), h = 17 + 26 * r;
                    var sad = new List<(float pos, float depth, float half)> { (Uni(0.4f, 0.6f), Uni(0.55f, 0.8f), Uni(0.1f, 0.16f)) };
                    if (rng.Next() < 0.5) sad.Add((rng.Next() < 0.5 ? Uni(0.14f, 0.26f) : Uni(0.74f, 0.86f), Uni(0.4f, 0.7f), Uni(0.08f, 0.12f)));
                    var pts = Line(u0 + bend, -half, u0 - bend, half, 9);
                    Add(F.Chain(pts, Uni(85, 120), h, sad, 0f, 1.6f, 0.35f));
                    foreach (var s in sad) { var k = pts[(int)Math.Round(s.pos * 8)]; feats.Add(("choke", k.x, k.y, 22, axis)); }
                    foreach (float pos in new[] { 0.25f, 0.75f }) { var k = pts[(int)Math.Round(pos * 8)]; feats.Add(("high", k.x, k.y, 30, axis)); }
                    break;
                }
                case "valley":
                {
                    float off = Uni(155, 195), h = 30 + 40 * r;
                    foreach (int sgn in new[] { -1, 1 })
                    {
                        var gaps = rng.Next() < 0.25 ? new List<(float, float, float)> { (Uni(0.3f, 0.7f), Uni(0.3f, 0.5f), Uni(0.06f, 0.1f)) } : null;
                        var pts = Line(-430, sgn * (off + Uni(-25, 25)), 430, sgn * (off + Uni(-25, 25)), 9);
                        Add(F.Chain(pts, Uni(85, 115), h * Uni(0.85f, 1.15f), gaps, Uni(0f, 0.3f), 1.5f, 0.4f));
                        var m = Pt(Uni(-120, 120), sgn * (off - 55)); feats.Add(("high", m.x, m.y, 30, axis + (sgn < 0 ? 1 : -1) * (float)Math.PI / 2));
                    }
                    for (int q = rng.Int(1, 3); q > 0; q--) { var k = Pt(Uni(-150, 150), Uni(-60, 60)); F.Hill(z, false, k.x, k.y, Uni(35, 55), Uni(4, 8)); }
                    break;
                }
                case "hill":
                {
                    int sgn = rng.Next() < 0.5 ? 1 : -1;
                    var p = Pt(Uni(-60, 60), sgn * Uni(70, 150)); float R = Uni(150, 210);
                    F.Hill(z, false, p.x, p.y, R, 24 + 30 * r, Uni(0.3f, 0.45f)); feats.Add(("high", p.x, p.y, 40, axis));
                    var q = Pt(Uni(-200, 200), -sgn * Uni(180, 260)); F.Hill(z, false, q.x, q.y, Uni(80, 120), Uni(10, 18)); feats.Add(("high", q.x, q.y, 25, axis));
                    break;
                }
                case "crossing":
                {
                    river = true; rvU0 = Uni(-40, 40); rvAmp = Uni(25, 60); rvLam = Uni(110, 190); rvPhi = Uni(0, 6.28f);
                    float[] all = { Uni(-250, -130), Uni(-70, 70), Uni(130, 250) }; int nf = rng.Int(2, 4); for (int q = 0; q < nf; q++) fords.Add(all[q]);
                    float a = 6 + 10 * r, u0 = rvU0;
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) z[k] += a * G.Sm(Math.Abs(Uof(i, j) - u0), 40, 320); });
                    break;
                }
                case "clearing":
                {
                    ring = true; ringRadius = Uni(245, 285);
                    for (int q = rng.Int(3, 6); q > 0; q--) { var k = Pt(Uni(-180, 180), Uni(-180, 180)); F.Hill(z, false, k.x, k.y, Uni(40, 80), Uni(3, 6 + 5 * r)); }
                    break;
                }
                case "pass":
                {
                    float w0 = Uni(60, 90), h = 42 + 46 * r;
                    foreach (int sgn in new[] { -1, 1 })
                    {
                        float rad = Uni(120, 160);
                        var pts = Line(-420, sgn * (w0 + rad + Uni(40, 110)), 420, sgn * (w0 + rad + Uni(40, 110)), 11);
                        pts[5] = Pt(Uni(-50, 50), sgn * (w0 + rad * 0.95f));
                        Add(F.Chain(pts, rad, h * Uni(0.85f, 1.1f), null, 0f, 1.7f, 0.45f));
                        var b = Pt(Uni(-150, 150), sgn * (w0 + rad + 230)); F.Hill(z, false, b.x, b.y, Uni(230, 310), h * 0.9f);
                        var m = Pt(Uni(-80, 80), sgn * (w0 + rad * 0.6f)); feats.Add(("high", m.x, m.y, 30, axis + (sgn < 0 ? 1 : -1) * (float)Math.PI / 2));
                    }
                    feats.Add(("choke", C, C, 30, axis));
                    break;
                }
                case "bluff": sea = true; seaShore = Uni(170, 235); seaBluff = 9 + 11 * r; break;
                case "ledge":
                {
                    ledge = true; ledgeHalf = Uni(62, 82); ledgeEdge = Uni(95, 125); ledgeDrop = 38 + 32 * r; ledgeWall = 45 + 45 * r;
                    float half = ledgeHalf, wall = ledgeWall;
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { float v = Vof(i, j); z[k] += wall * G.Sm(-v - 0.85f * half, 0, 150) + 0.03f * Math.Max(-v - half - 150, 0); } });
                    for (int q = rng.Int(2, 5); q > 0; q--) { var k = Pt(Uni(-260, 260), Uni(-40, 40)); F.Hill(z, false, k.x, k.y, Uni(30, 55), Uni(2, 5)); }
                    var m = Pt(Uni(-90, 90), -ledgeHalf - 35); feats.Add(("high", m.x, m.y, 25, axis + (float)Math.PI / 2));
                    feats.Add(("choke", C, C, 30, axis));
                    break;
                }
                case "broken":
                {
                    int K = (int)(12 + 10 * r), made = 0, tries = 0;
                    while (made < K && tries < 400)
                    {
                        tries++; float u = Uni(-420, 420), v = Uni(-420, 420);
                        if (Math.Sqrt(u * u + v * v) > 430 || Math.Sqrt((Math.Abs(u) - SPAWN) * (Math.Abs(u) - SPAWN) + v * v) < 85) continue;
                        var p = Pt(u, v); F.Hill(z, false, p.x, p.y, Uni(28, 75), Uni(5, 9 + 9 * r), Uni(0, 0.4f), 0.28f, 2.0f); made++;
                        if (Math.Sqrt(u * u + v * v) < 300 && rng.Next() < 0.3) feats.Add(("high", p.x, p.y, 18, axis));
                    }
                    break;
                }
                case "mesa":
                {
                    int want = rng.Int(3, 6), made = 0, tries = 0;
                    while (made < want && tries < 200)
                    {
                        tries++; float u = Uni(-380, 380), v = Uni(120, 380) * (rng.Next() < 0.5 ? 1 : -1);
                        var p = Pt(u, v); F.Hill(z, false, p.x, p.y, Uni(90, 160), Uni(16, 30), Uni(0.55f, 0.7f), 0.55f, 3.0f); made++;
                        if (Math.Sqrt(u * u + v * v) < 300) feats.Add(("high", p.x, p.y, 35, axis));
                    }
                    break;
                }
            }
            Lap("landforms");

            if (rules.Biome == "Desert") { var dn = G.Fbm(95f, 2, rng, 0.5f, true); float a = 1.5f + 2.5f * r; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) z[k] += a * dn[k]; }); }
            // bend the drawn landforms (domain warping)
            {
                var a1 = G.Noise(270f, rng); var a2 = G.Noise(90f, rng); var b1 = G.Noise(270f, rng); var b2 = G.Noise(90f, rng);
                var wx = G.New(); var wy = G.New();
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { wx[k] = 42f * a1[k] + 14f * a2[k]; wy[k] = 42f * b1[k] + 14f * b2[k]; } });
                z = G.Warp(z, wx, wy);
            }
            // the frame of hills or mountains around the play area
            float[] dshore = null, calm = null;
            {
                var w300 = F.Wobble(300f); var f1 = G.Fbm(380f, 4, rng, 0.5f, true, 0.5f); var f2 = G.Fbm(70f, 3, rng, 0.5f, true);
                var frame = G.New(); float amp = 12 + 95 * r;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) frame[k] = G.Sm(rr[k] * (1 + 0.12f * w300[k]), 400, 590) * amp * (0.55f + 0.45f * f1[k]) * (0.86f + 0.28f * f2[k]); });
                if (sea)
                {
                    var w220 = F.Wobble(220f); dshore = G.New(); calm = G.New(); float shore = seaShore, bluff = seaBluff; var zz = z; var ds = dshore; var cm = calm;
                    G.Rows(j =>
                    {
                        int k = j * N;
                        for (int i = 0; i < N; i++, k++)
                        {
                            float d = Vof(i, j) + shore + 28f * w220[k]; ds[k] = d; cm[k] = G.Sm(d, 10, 80);
                            float land = G.Sm(d, 40, 170), prof = d < 0 ? Math.Max(d * 0.06f, -6f) : 0f;
                            prof += 1.6f * G.Sm(d, 0, 26) + bluff * G.Sm(d, 26, 62) + 0.02f * Math.Max(d - 62, 0);
                            zz[k] = prof + zz[k] * land * 0.8f + frame[k] * land;
                        }
                    });
                    var m = Pt(Uni(-60, 60), -seaShore + 90); feats.Add(("high", m.x, m.y, 30, axis));
                }
                else Add(frame);
            }
            Lap("warp+frame");

            // small relief of shipped battlefields, quilted: first the band of hillocks and gullies
            {
                int n4 = (N - 1) / 4 + 1;
                var m4 = BiomeData.Quilt(data.Mid, data.MidW, data.MidH, n4, n4, (int)Math.Round(260f / cell / 4f), new Rng(rules.Seed + 1));
                var mid = G.Up(m4, n4, n4); float a = 0.5f + 0.9f * r; var cm = calm; var zz = z;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) zz[k] += a * mid[k] * (cm == null ? 1f : cm[k]); });
            }
            Lap("relief");
            // running water, then scree under the steepest faces
            float[] drain;
            {
                var z0 = z; var ze = Erosion.Fluvial(z, 4f + 6f * r, 9, rng, out drain);
                if (sea) { var cm = calm; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) ze[k] = z0[k] + (ze[k] - z0[k]) * cm[k]; }); }
                z = ze;
            }
            Lap("fluvial");
            if (r > 0.5f || kind == "pass" || kind == "mesa") { z = Erosion.Thermal(z, 1.15f, 14); Lap("thermal"); }
            {
                var fine = BiomeData.Quilt(data.Fine, data.FineW, data.FineH, N, N, (int)Math.Round(160f / cell), new Rng(rules.Seed + 2));
                var s6 = G.Slope(G.Blur(z, 6f)); var cm = calm; var zz = z;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { float s = G.Clamp(s6[k] / 0.30f, 0, 1); zz[k] += (0.45f + (1.5f - 0.45f) * s) * fine[k] * (cm == null ? 1f : cm[k]); } });
            }
            Lap("grain");
            // level ground where the armies deploy
            var zb = G.Blur(z, 22f / cell);
            foreach (int sgn in new[] { -1, 1 })
            {
                var p = Pt(sgn * SPAWN, 0); var zz = z;
                G.Rows(j => { int k = j * N; float dy = j * cell - p.y; for (int i = 0; i < N; i++, k++) { float dx = i * cell - p.x; float w = 0.75f * (1 - G.Sm((float)Math.Sqrt(dx * dx + dy * dy), 35, 90)); if (w > 0) zz[k] = zz[k] * (1 - w) + zb[k] * w; } });
            }
            // small mounds on gentle ground of the fighting area scaled down towards what the shipped maps have
            {
                var sl0 = G.Slope(G.Blur(z, 6f / cell)); var b40 = G.Blur(z, 40f / cell); double sum = 0; long cnt = 0; var zz = z;
                var gentle = G.New();
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) gentle[k] = (1 - G.Sm(sl0[k], 0.18f, 0.38f)) * (1 - G.Sm(rr[k], PLAY + 20, PLAY + 120)); });
                for (int k = 0; k < zz.Length; k++) if (gentle[k] > 0.9f && rr[k] < PLAY) { float b = zz[k] - b40[k]; sum += b * b; cnt++; }
                float rms = cnt > 0 ? (float)Math.Sqrt(sum / cnt) : 0f;
                if (rms > G.BUMPS) { float f = 1 - G.BUMPS / rms; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) zz[k] -= f * (zz[k] - b40[k]) * gentle[k]; }); }
            }
            var offlimits = new bool[N * N];
            if (ledge)
            {
                var w130 = F.Wobble(130f); var w38 = F.Wobble(38f); float edge = ledgeEdge, drop = ledgeDrop; var zz = z;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { float over = Vof(i, j) - (edge + 14f * w130[k] + 5f * w38[k]); zz[k] -= drop * G.Sm(over, 0f, 7f) + 0.05f * G.Clamp(over - 7f, 0f, 400f); offlimits[k] = over > -2.5f; } });
            }
            for (int f = 0; f < feats.Count; f++)
            {
                var ft = feats[f]; if (ft.typ != "high") continue;
                int j0 = (int)Math.Max(0, (ft.y - 60) / cell), j1 = (int)Math.Min(N, (ft.y + 60) / cell), i0 = (int)Math.Max(0, (ft.x - 60) / cell), i1 = (int)Math.Min(N, (ft.x + 60) / cell);
                float best = float.MinValue; int bi = -1, bj = -1;
                for (int j = j0; j < j1; j++) for (int i = i0; i < i1; i++) if (zb[j * N + i] > best) { best = zb[j * N + i]; bi = i; bj = j; }
                if (bi >= 0) feats[f] = (ft.typ, bi * cell, bj * cell, ft.rad, ft.face);
            }
            Lap("shape");

            // water
            bool hasLevel = false; float level = 0f; var under = new bool[N * N]; var deep = new bool[N * N];
            if (river)
            {
                var w260 = F.Wobble(260f); var dr = G.New(); var ford = G.New(); float u0 = rvU0, amp = rvAmp, lam = rvLam, phi = rvPhi;
                G.Rows(j =>
                {
                    int k = j * N;
                    for (int i = 0; i < N; i++, k++)
                    {
                        float v = Vof(i, j); dr[k] = Math.Abs(Uof(i, j) - (u0 + amp * (float)Math.Sin(v / lam + phi) + 22f * w260[k]));
                        float fd = 0f; foreach (float vf in fords) fd = Math.Max(fd, 1 - G.Sm(Math.Abs(v - vf), 14, 32));
                        ford[k] = fd;
                    }
                });
                level = G.Quantile(z, k => dr[k] < 9f, 0.25f) - 0.3f; hasLevel = true; float lv = level; var zz = z;
                G.Rows(j =>
                {
                    int k = j * N;
                    for (int i = 0; i < N; i++, k++)
                    {
                        float W = 9f + 5f * ford[k], d = dr[k], v = zz[k];
                        v = Math.Min(v, lv + 0.6f + 0.05f * (float)Math.Pow(Math.Max(d - W, 0), 1.2));
                        if (d > W) v = Math.Max(v, lv + 0.6f);
                        float inside = 1 - G.Sm(d, 0.6f * W, 1.15f * W);
                        v = v * (1 - inside) + (lv - (2.3f * (1 - ford[k]) + 0.4f * ford[k]) * (1 - G.Sm(d, 0.3f * W, W))) * inside;
                        zz[k] = Math.Max(v, lv - 2.5f);
                    }
                });
                foreach (float vf in fords) if (Math.Abs(vf) < 250) { var p = Pt(rvU0 + rvAmp * (float)Math.Sin(vf / rvLam + rvPhi), vf); feats.Add(("choke", p.x, p.y, 20, axis)); }
            }
            if (sea)
            {
                level = 0f; hasLevel = true; var zz = z; var ds = dshore;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) if (ds[k] > 30) zz[k] = Math.Max(zz[k], 0.9f + 0.01f * Math.Min(ds[k], 200)); });
            }
            if (hasLevel) for (int k = 0; k < z.Length; k++) { under[k] = z[k] < level - 0.05f; deep[k] = z[k] < level - 0.9f; }
            { float mn = float.MaxValue; for (int k = 0; k < z.Length; k++) if (z[k] < mn) mn = z[k]; float off = 40f - mn; var zz = z; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) zz[k] += off; }); if (hasLevel) level += off; }
            Lap("water");

            // ---- rocks, fitted to the ground and banked in
            var sl = G.Slope(G.Blur(z, 1.5f));
            var usage = data.Rocks;
            List<RockKind> OfKind(string kd) => usage.Where(u => u.Kind == kd).ToList();
            var walls = OfKind("wall"); if (walls.Count == 0) walls = usage.ToList();
            var towers = OfKind("tower"); if (towers.Count == 0) towers = walls;
            var boulders = OfKind("boulder"); if (boulders.Count == 0) boulders = OfKind("slab"); if (boulders.Count == 0) boulders = walls;
            var slabs = OfKind("slab"); if (slabs.Count == 0) slabs = boulders;
            RockKind Pick(List<RockKind> names) { double t = rng.Next() * names.Sum(q => q.N); foreach (var q in names) { t -= q.N; if (t <= 0) return q; } return names[names.Count - 1]; }
            var spawnPts = new[] { Pt(-SPAWN, 0), Pt(SPAWN, 0) };
            bool Clear(float x, float y, float d = 75f) { foreach (var s in spawnPts) if (Math.Sqrt((x - s.x) * (x - s.x) + (y - s.y) * (y - s.y)) <= d) return false; return true; }
            var steep = new bool[N * N]; var steepF = G.New(); for (int k = 0; k < steep.Length; k++) { steep[k] = sl[k] > 0.45f; steepF[k] = steep[k] ? 1f : 0f; }
            G.Grad(G.Blur(z, 3f / cell), out var gxs, out var gys);
            var nearCliff = G.Blur(steepF, 14f / cell);
            bool Usable(float x, float y, float reach = 545f) => Math.Sqrt((x - C) * (x - C) + (y - C) * (y - C)) < reach && Clear(x, y) && !G.At(under, x, y);
            var cliffs = new List<(float x, float y)>(); var screes = new List<(float x, float y)>(); var lumps = new List<(float x, float y)>();
            if (usage.Count > 0)
            {
                foreach (var p in G.Poisson(17f, rng, (x, y) => Usable(x, y) && G.At(steep, x, y))) cliffs.Add(p);
                foreach (var p in G.Poisson(11f, rng, (x, y) => { if (!Usable(x, y, 420f) || G.At(steep, x, y)) return false; float nc = G.At(nearCliff, x, y); return nc > 0.12f && nc < 0.5f; }))
                    if (rng.Next() < 0.35) screes.Add(p);
                foreach (var ft in feats)
                {
                    if (ft.typ != "high") continue;
                    for (int q = rng.Int(1, 3); q > 0; q--) { float a = Uni(0, 6.28f), d = Uni(4, ft.rad * 0.6f), x = ft.x + d * (float)Math.Cos(a), y = ft.y + d * (float)Math.Sin(a); if (Usable(x, y)) lumps.Add((x, y)); }
                }
                var lp = G.Poisson(70f, rng, (x, y) => Usable(x, y, 400f) && G.At(sl, x, y) < 0.35f);
                for (int q = 0; q < Math.Min(lp.Count, (int)(10 + 16 * r)); q++) lumps.Add(lp[q]);
            }
            List<(float x, float y)> Nearest(List<(float x, float y)> lst, int n)
            {
                if (lst.Count <= n) return lst;
                var keyed = lst.Select((q, idx) => (key: rng.Next() + 0.9 * Math.Sqrt((q.x - C) * (q.x - C) + (q.y - C) * (q.y - C)) / 545.0, idx)).OrderBy(t => t.key).Take(n).Select(t => t.idx).OrderBy(t => t).ToList();
                return keyed.Select(i => lst[i]).ToList();
            }
            var spots = new List<(float x, float y, int why)>();          // why: 0 cliff, 1 lump, 2 scree
            foreach (var p in Nearest(cliffs, 130)) spots.Add((p.x, p.y, 0));
            foreach (var p in lumps) spots.Add((p.x, p.y, 1));
            foreach (var p in Nearest(screes, 70)) spots.Add((p.x, p.y, 2));
            Lap("rock spots");
            var ents = new StringBuilder(1 << 16); int nEnt = 0; var blocked = new bool[N * N]; var rocks = new List<(float x, float y, float sc, int why)>();
            bool[] wet = null;
            if (hasLevel) { var uf = G.New(); for (int k = 0; k < uf.Length; k++) uf[k] = under[k] ? 1f : 0f; var wb = G.Blur(uf, 6f / cell); wet = new bool[N * N]; for (int k = 0; k < wet.Length; k++) wet[k] = wb[k] > 0.02f; }
            var rocktop = G.New(); for (int k = 0; k < rocktop.Length; k++) rocktop[k] = -1e9f;
            var zBefore = (float[])z.Clone();
            foreach (var sp in spots)
            {
                RockKind u = sp.why == 0 ? Pick(rng.Next() < 0.12 ? towers : walls) : sp.why == 2 ? Pick(boulders) : Pick(rng.Next() < 0.3 && G.At(sl, sp.x, sp.y) < 0.18f ? slabs : boulders);
                float sc = rng.Next() < 0.85 ? u.Scale * Uni(0.85f, 1.15f) : Uni(u.Scale, Math.Max(u.Scale, u.ScaleHi));
                float hiDz = Math.Min(0.2f, u.DzHi), sink = u.DzLo < hiDz ? Uni(u.DzLo, hiDz) : Math.Min(0f, u.Dz);
                float zlo = u.ZLo, h0 = u.Height; bool big = h0 * u.Scale > 6f;
                float Bury(float share) => -zlo * sc - share * h0 * sc;
                if (u.Kind == "tower") sink = Math.Min(sink, Bury(0.1f));
                if (sp.why == 1) { sc *= u.Kind == "boulder" && big ? Uni(0.45f, 0.85f) : Uni(0.8f, 1.2f); sink = u.Kind == "boulder" ? Bury(Uni(0.12f, 0.25f)) : Math.Min(sink, Bury(Uni(0.2f, 0.35f))); }
                if (sp.why == 2) { sc *= big ? Uni(0.25f, 0.45f) : Uni(0.8f, 1.3f); sink = Bury(Uni(0.15f, 0.3f)); }
                float tilt = u.Kind == "slab" ? 0f : u.Kind == "tower" ? 0.1f : Math.Min(0.5f, u.Tilt);
                float[] yaws, leans, rolls;
                if (u.FacingR > 0.65f)
                {
                    float down = (float)Math.Atan2(-G.At(gys, sp.x, sp.y), -G.At(gxs, sp.x, sp.y)) + u.Facing * (float)Math.PI / 180f;
                    yaws = new[] { down + rng.Normal(0f, 0.15f), down - 0.35f, down + 0.35f }; leans = new[] { u.Lean * 0.3f, u.Lean * 0.8f, u.Lean * 1.3f, u.Lean * 1.8f }; rolls = new[] { Uni(-0.04f, 0.04f) };
                }
                else
                {
                    yaws = new float[6]; for (int q = 0; q < 6; q++) yaws[q] = Uni(-3.14f, 3.14f);
                    leans = new[] { Uni(-tilt, tilt) / 2, 0f }; rolls = new[] { Uni(-tilt, tilt) / 2, 0f };
                }
                float tall = h0 * sc; float[] qs = { 0.06f, 0f, -0.06f, -0.12f, -0.2f, -0.32f, -0.45f }; var sinks = new float[qs.Length]; for (int q = 0; q < qs.Length; q++) sinks[q] = sink + qs[q] * tall;
                var f = Rocks.Fit(z, u, sp.x, sp.y, yaws, leans, sinks, sc, rolls, sp.why == 2 ? 0.2f : 0.3f, 3.0f);
                if (f == null) continue;
                Rocks.TopSurface(rocktop, f); Rocks.Cover(z, f, rocktop, wet); Rocks.Footprint(z, f, blocked);
                ents.Append("<game_entity prefab=\"").Append(u.Name).Append("\"><transform position=\"").Append(V3(f.X, f.Y, f.Z)).Append("\" rotation_euler=\"").Append(V3(f.Lean, f.Roll, f.Yaw))
                    .Append("\" scale=\"").Append(V3(sc, sc, sc)).Append("\"/></game_entity>\n"); nEnt++;
                rocks.Add((sp.x, sp.y, sc, sp.why));
            }
            { var zz = z; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) if (rocktop[k] > -1e8f) zz[k] = Math.Min(zz[k], Math.Max(zBefore[k], rocktop[k] + 0.35f)); }); }
            Lap("rocks");

            // ---- ground textures, each layer with the template's own distribution of strengths
            sl = G.Slope(G.Blur(z, 1.5f));
            var z10 = G.Blur(z, 10f); var uff = G.New(); for (int k = 0; k < uff.Length; k++) uff[k] = under[k] ? 1f : 0f;
            var wetground = hasLevel ? G.Blur(uff, 3f / cell) : G.New();
            var inner = new bool[N * N]; for (int k = 0; k < inner.Length; k++) inner[k] = rr[k] < 370f;
            float[] Patchy(float b, float m, float f, float coarse)
            {
                var a = G.Noise(b, rng); var a2 = G.Noise(m, rng); var a3 = G.Noise(f, rng); var a4 = coarse > 0 ? G.Noise(260f, rng) : null;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) a[k] += 0.6f * a2[k] + 0.3f * a3[k] + (a4 != null ? coarse * a4[k] : 0f); });
                return a;
            }
            var masks = new byte[data.L][]; masks[0] = new byte[N * N]; for (int k = 0; k < masks[0].Length; k++) masks[0][k] = 255;
            var bd = G.Blur(G.Map(drain, v => (float)Math.Log10(Math.Max(v, 1f))), 2.5f / cell);
            int gk = 0, sk = 0, rk = 0, nk = 0;
            float[] forest = null;
            for (int i = 1; i < data.L; i++)
            {
                string role = data.Role[i];
                if (role == "grass")
                {
                    float[] bases = { 60f, 45f, 80f, 50f }; bool mesh = data.Meshes[i];
                    var sc = mesh ? Patchy(11f, 5.5f, 3f, 0.2f) : Patchy(bases[gk % 4], 24f, 10f, 0.3f); gk++;
                    G.Rows(j => { int k = j * N; for (int q = 0; q < N; q++, k++) { float flat = 1 - G.Sm(sl[k], 0.25f, 0.5f); sc[k] -= 4f * (1 - flat) + (mesh ? 4f * G.Sm(wetground[k], 0.05f, 0.3f) : 0f); } });
                    masks[i] = LikeTemplate(sc, data.QIn[i], data.QOut[i], inner);
                }
                else if (role == "soil")
                {
                    var sc = Patchy(sk % 2 == 0 ? 110f : 60f, 40f, 15f, 0f); sk++; var zz = z;
                    G.Rows(j =>
                    {
                        int k = j * N;
                        for (int q = 0; q < N; q++, k++)
                        {
                            float s = sl[k], flat = 1 - G.Sm(s, 0.25f, 0.5f), hollow = G.Sm(-(zz[k] - z10[k]), 0.25f, 0.9f) * G.Sm(s, 0.08f, 0.25f);
                            float runoff = G.Sm(bd[k], 3.6f, 4.5f) * G.Sm(s, 0.07f, 0.16f) * (1 - G.Sm(s, 0.5f, 0.8f));
                            sc[k] = 1.5f * hollow + 1.5f * runoff + 2f * G.Sm(wetground[k], 0.15f, 0.6f) + 0.7f * sc[k] - 2f * (1 - flat);
                        }
                    });
                    masks[i] = LikeTemplate(sc, data.QIn[i], data.QOut[i], inner);
                }
                else if (role == "rock")
                {
                    int kk = rk++; float lo = kk == 0 ? 0.38f : 0.7f + 0.08f * (kk - 1), hi = kk == 0 ? 0.58f : 1.0f + 0.08f * (kk - 1);
                    masks[i] = M8(G.Map(sl, v => G.Sm(v, lo, hi)));
                }
                else if (role == "snow")
                {
                    int kk = nk++; var nz = kk == 0 ? null : G.Noise(70f, rng); var a = G.New();
                    G.Rows(j => { int k = j * N; for (int q = 0; q < N; q++, k++) a[k] = (1 - G.Sm(sl[k], 0.45f, 0.7f)) * (nz == null ? 1f : G.Sm(nz[k], -0.2f, 0.4f)); });
                    masks[i] = M8(a);
                }
            }
            Lap("paint");
            // forest
            {
                var fn = G.Fbm(230f, 4, rng); float frac = G.Clamp(rules.Forest * 1.1f + 0.03f, 0.03f, 0.65f); if (rules.Biome == "Desert") frac = Math.Max(frac, 0.2f);
                forest = G.New(); var fo = forest;
                if (ring)
                {
                    var w170 = F.Wobble(170f); float rad = ringRadius;
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) fo[k] = Math.Max(G.Sm(rr[k] * (1 + 0.22f * w170[k]), rad - 30, rad + 25), G.Sm(fn[k], 0.42f, 0.52f) * 0.9f * G.Sm(rr[k], 90, 150)); });
                }
                else
                {
                    float thr = G.Quantile(fn, k => rr[k] < 520f, 1 - frac, 3);
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) fo[k] = G.Sm(fn[k], thr - 0.03f, thr + 0.05f) * (0.25f + 0.75f * G.Sm(Math.Abs(Vof(i, j)), 55, 150)); });
                }
                if (rules.Biome != "Desert")
                {
                    var n120 = G.Noise(120f, rng);
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) fo[k] = Math.Max(fo[k], G.Sm(rr[k], 440, 520) * G.Sm(n120[k], -0.25f, 0.05f)); });
                }
                else
                {
                    var c1 = G.Noise(55f, rng); var c2 = G.Noise(22f, rng); var cn = G.New();
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) cn[k] = c1[k] + 0.7f * c2[k]; });
                    float q90 = G.Quantile(cn, null, 0.90f, 7), q94 = G.Quantile(cn, null, 0.94f, 7); if (q94 <= q90) q94 = q90 + 1e-3f;
                    G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) fo[k] = Math.Max(fo[k] * 0.6f, G.Sm(cn[k], q90, q94)); });
                }
                var spA = spawnPts[0]; var spB = spawnPts[1];
                G.Rows(j =>
                {
                    int k = j * N;
                    for (int i = 0; i < N; i++, k++)
                    {
                        float x = i * cell, y = j * cell, v = fo[k] * (1 - G.Sm(sl[k], 0.5f, 0.75f)) * (1 - G.Sm(wetground[k], 0.02f, 0.2f));
                        v *= G.Sm((float)Math.Sqrt((x - spA.x) * (x - spA.x) + (y - spA.y) * (y - spA.y)), 55, 95) * G.Sm((float)Math.Sqrt((x - spB.x) * (x - spB.x) + (y - spB.y) * (y - spB.y)), 55, 95);
                        fo[k] = v;
                    }
                });
                var fb = G.Blur(forest, 2f / cell); var bf = G.New(); for (int k = 0; k < bf.Length; k++) bf[k] = blocked[k] ? 1f : 0f; var bb = G.Blur(bf, 1.5f / cell);
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) fo[k] = bb[k] < 0.05f ? G.Sm(fb[k], 0.42f, 0.52f) : 0f; });
                for (int i = 1; i < data.L; i++)
                {
                    if (data.Role[i] == "flora") masks[i] = M8(forest);
                    else if (data.Role[i] == "grasscover")
                    {
                        var sc = Patchy(22f, 10f, 5f, 0.25f);
                        G.Rows(j => { int k = j * N; for (int q = 0; q < N; q++, k++) { float flat = 1 - G.Sm(sl[k], 0.25f, 0.5f); sc[k] -= 4f * (1 - flat) + 4f * G.Sm(wetground[k], 0.05f, 0.3f); } });
                        masks[i] = LikeTemplate(sc, data.QIn[i], data.QOut[i], inner);
                    }
                }
            }
            Lap("forest");

            // fallen trees, logs, bones
            if (data.Props.Count > 0)
            {
                var wts = data.Props.Select(p => (float)Math.Sqrt(p.weight)).ToArray(); float wsum = wts.Sum();
                var edge = G.Blur(forest, 8f / cell);
                bool Lies(float x, float y) => Math.Sqrt((x - C) * (x - C) + (y - C) * (y - C)) < 440 && Clear(x, y, 35f) && !G.At(under, x, y) && !G.At(blocked, x, y) && G.At(sl, x, y) < 0.3f;
                var pp = G.Poisson(32f, rng, Lies);
                for (int q = 0; q < Math.Min(60, pp.Count); q++)
                {
                    double t = rng.Next() * wsum; int pi = 0; for (; pi < wts.Length - 1; pi++) { t -= wts[pi]; if (t <= 0) break; }
                    string name = data.Props[pi].name; bool woody = name.Contains("tree") || name.Contains("log") || name.Contains("root");
                    float e = G.At(edge, pp[q].x, pp[q].y);
                    if (woody && rng.Next() > (e > 0.05f && e < 0.9f ? 0.9 : 0.2)) continue;
                    if (!woody && rng.Next() > 0.35) continue;
                    ents.Append("<game_entity prefab=\"").Append(name).Append("\"><transform position=\"").Append(V3(pp[q].x, pp[q].y, G.At(z, pp[q].x, pp[q].y) - 0.05f))
                        .Append("\" rotation_euler=\"").Append(V3(0, 0, Uni(-3.14f, 3.14f))).Append("\"/></game_entity>\n"); nEnt++;
                }
            }
            var forestCells = new List<int>(); for (int k = 0; k < forest.Length; k += 3) if (forest[k] > 0.6f && rr[k] < PLAY - 40) forestCells.Add(k);
            if (forestCells.Count > 700 && rules.Biome != "Desert")
                for (int q = 0; q < 2; q++)
                {
                    int k = forestCells[rng.Int(forestCells.Count)];
                    ents.Append("<game_entity prefab=\"detail_birds_forest_sound\"><transform position=\"").Append(V3((k % N) * cell, (k / N) * cell, z[k] + 6f)).Append("\" rotation_euler=\"0.000, 0.000, 0.000\"/></game_entity>\n"); nEnt++;
                }
            // markers for the battle AI
            foreach (var ft in feats)
            {
                if (Math.Sqrt((ft.x - C) * (ft.x - C) + (ft.y - C) * (ft.y - C)) > PLAY - 20 || G.At(under, ft.x, ft.y)) continue;
                ents.Append("<game_entity prefab=\"TacticalPosition\"><transform position=\"").Append(V3(ft.x, ft.y, G.At(z, ft.x, ft.y))).Append("\" rotation_euler=\"").Append(V3(0, 0, ft.face))
                    .Append("\"/><scripts><script name=\"TacticalPosition\"><variables><variable name=\"_width\" value=\"").Append(F3(Math.Max(15f, ft.rad)))
                    .Append("\"/><variable name=\"_tacticalPositionType\" value=\"").Append(ft.typ == "high" ? "HighGround" : "ChokePoint")
                    .Append("\"/><variable name=\"_tacticalRegionMembership\" value=\"Opening\"/></variables></script></scripts></game_entity>\n"); nEnt++;
            }
            void Region(float x, float y, float zz, string radius, string type)
            {
                ents.Append("<game_entity prefab=\"TacticalRegion\"><transform position=\"").Append(V3(x, y, zz)).Append("\" rotation_euler=\"0.000, 0.000, 0.000\"/><scripts><script name=\"TacticalRegion\"><variables><variable name=\"radius\" value=\"")
                    .Append(radius).Append("\"/><variable name=\"tacticalRegionType\" value=\"").Append(type).Append("\"/></variables></script></scripts></game_entity>\n"); nEnt++;
            }
            Region(C, C, G.At(z, C, C), "45.000", "Opening");
            if (forestCells.Count > 1300) for (int q = 0; q < 2; q++) { int k = forestCells[rng.Int(forestCells.Count)]; Region((k % N) * cell, (k / N) * cell, z[k], "35.000", "Forest"); }

            // ---- border, spawn paths, walkable ground
            (float bad, List<(float x, float y, float z)> pts) PathAt(float angle)
            {
                var pts = new List<(float, float, float)>(); float bad = 0; float c = (float)Math.Cos(angle), s = (float)Math.Sin(angle);
                for (int q = 0; q < 7; q++)
                {
                    float t = -SPAWN / PLAY + q * (2 * SPAWN / PLAY) / 6f, x = C + PLAY * t * c, y = C + PLAY * t * s;
                    pts.Add((x, y, G.At(z, x, y)));
                    bad += (G.At(deep, x, y) && Math.Abs(t) > 0.3f ? 3f : 0f) + G.At(forest, x, y) + (G.At(sl, x, y) > 0.5f ? 2f : 0f) + (G.At(offlimits, x, y) ? 6f : 0f);
                }
                bad += 6f * ((G.At(deep, pts[0].Item1, pts[0].Item2) ? 1 : 0) + (G.At(deep, pts[6].Item1, pts[6].Item2) ? 1 : 0));
                return (bad, pts);
            }
            bool narrow = kind == "valley" || kind == "pass" || kind == "ledge" || kind == "crossing" || kind == "bluff";
            var basePath = PathAt(axis);
            var others = new List<(float bad, List<(float x, float y, float z)> pts, int k)>();
            foreach (int k in narrow ? new[] { 1, 7 } : new[] { 1, 2, 3, 4, 5, 6, 7 }) { var p = PathAt(axis + (float)Math.PI * k / 8f); others.Add((p.bad, p.pts, k)); }
            var paths = new List<List<(float x, float y, float z)>> { basePath.pts };
            foreach (var o in others.OrderBy(t => t.bad).Where(t => t.bad <= Math.Max(2.5f, basePath.bad + 0.5f)).Take(3).OrderBy(t => t.k)) paths.Add(o.pts);

            // ---- heights as the game will see them (16 bits between the lowest and the highest point)
            float zmin = float.MaxValue, zmax = float.MinValue; for (int k = 0; k < z.Length; k++) { if (z[k] < zmin) zmin = z[k]; if (z[k] > zmax) zmax = z[k]; }
            if (zmax - zmin < 1e-3f) zmax = zmin + 1f;
            var h16 = new ushort[N * N]; { float sc = 65535f / (zmax - zmin), back = (zmax - zmin) / 65535f; var zz = z; G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { int v = (int)Math.Round((zz[k] - zmin) * sc); v = v < 0 ? 0 : v > 65535 ? 65535 : v; h16[k] = (ushort)v; zz[k] = zmin + v * back; } }); }
            G.Grad(z, out var ngx, out var ngy);
            var normals = new byte[N * N * 4];
            G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { float nx = -ngx[k], ny = -ngy[k], inv = 1f / (float)Math.Sqrt(nx * nx + ny * ny + 1f); normals[4 * k] = (byte)G.Clamp((float)Math.Round(nx * inv * 127.5f + 127.5f), 0, 255); normals[4 * k + 1] = (byte)G.Clamp((float)Math.Round(ny * inv * 127.5f + 127.5f), 0, 255); normals[4 * k + 2] = 63; } });
            var phys = new byte[4][];
            for (int s = 0; s < 4; s++)
            {
                var ph = new byte[N * N]; phys[s] = ph; int ss = s;
                G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { byte top = 0; for (int l = 1; l < data.L; l++) if (masks[l] != null && data.Enabled[ss][l] && masks[l][k] >= 128) top = (byte)l; ph[k] = top; } });
            }
            Lap("pack");
            byte[] terrainBin = Files.Terrain(h16, normals, masks, phys, data.Nx, data.Ny, out bool[][] present);
            Lap("terrain.bin");

            // ---- navmesh: square faces of about 4 m over walkable ground, the largest connected area
            int navFaces;
            byte[] navBin;
            {
                int kk = Math.Max(1, (int)Math.Round(4f / cell)); var s2 = G.Slope(G.Blur(z, 2f)); float inR = PLAY * (float)Math.Cos(Math.PI / 32);
                bool Ok(int j, int i)
                {
                    int k = j * N + i; if (deep[k] || blocked[k] || offlimits[k] || s2[k] > 0.75f) return false;
                    float dx = i * cell - C, dy = j * cell - C, th = (float)Math.Atan2(dy, dx), sec = 2f * (float)Math.PI / 32f;
                    float ph = th - (float)Math.Floor(th / sec) * sec - sec / 2;
                    return (float)Math.Sqrt(dx * dx + dy * dy) * (float)Math.Cos(ph) <= inR;
                }
                int g = (N - 1) / kk; var corner = new bool[(g + 1) * (g + 1)];
                Parallel.For(0, g + 1, j => { for (int i = 0; i <= g; i++) corner[j * (g + 1) + i] = Ok(j * kk, i * kk); });
                var cells = new bool[g * g];
                Parallel.For(0, g, j => { for (int i = 0; i < g; i++) cells[j * g + i] = corner[j * (g + 1) + i] && corner[(j + 1) * (g + 1) + i] && corner[j * (g + 1) + i + 1] && corner[(j + 1) * (g + 1) + i + 1] && Ok(kk / 2 + j * kk, kk / 2 + i * kk); });
                var lab = new int[g * g]; int cur = 0, bestN = 0, bestL = 0; var stack = new Stack<int>();
                for (int c0 = 0; c0 < cells.Length; c0++)
                {
                    if (!cells[c0] || lab[c0] != 0) continue;
                    cur++; int n = 0; stack.Push(c0); lab[c0] = cur;
                    while (stack.Count > 0)
                    {
                        int c1 = stack.Pop(); n++; int y = c1 / g, x = c1 % g;
                        if (y > 0 && cells[c1 - g] && lab[c1 - g] == 0) { lab[c1 - g] = cur; stack.Push(c1 - g); }
                        if (y < g - 1 && cells[c1 + g] && lab[c1 + g] == 0) { lab[c1 + g] = cur; stack.Push(c1 + g); }
                        if (x > 0 && cells[c1 - 1] && lab[c1 - 1] == 0) { lab[c1 - 1] = cur; stack.Push(c1 - 1); }
                        if (x < g - 1 && cells[c1 + 1] && lab[c1 + 1] == 0) { lab[c1 + 1] = cur; stack.Push(c1 + 1); }
                    }
                    if (n > bestN) { bestN = n; bestL = cur; }
                }
                var vid = new int[(g + 1) * (g + 1)]; for (int q = 0; q < vid.Length; q++) vid[q] = -1;
                var verts = new List<float>(bestN * 4); var quads = new List<int>(bestN * 4);
                int V(int j, int i) { int q = j * (g + 1) + i; if (vid[q] < 0) { vid[q] = verts.Count / 3; verts.Add(i * kk * cell); verts.Add(j * kk * cell); verts.Add(z[j * kk * N + i * kk]); } return vid[q]; }
                for (int j = 0; j < g; j++) for (int i = 0; i < g; i++) if (lab[j * g + i] == bestL && bestL != 0) { quads.Add(V(j, i)); quads.Add(V(j, i + 1)); quads.Add(V(j + 1, i + 1)); quads.Add(V(j + 1, i)); }
                navFaces = quads.Count / 4; navBin = Files.NavMesh(verts, quads);
            }
            Lap("navmesh");

            // ---- the scene text
            var border = new StringBuilder();
            for (int q = 0; q < 32; q++)
            {
                float a = q * 2f * (float)Math.PI / 32f, x = C + PLAY * (float)Math.Cos(a), y = C + PLAY * (float)Math.Sin(a);
                border.Append("<game_entity name=\"border_soft\" old_prefab_name=\"\"><visibility_masks><visibility_mask name=\"visible_only_when_editing\" value=\"true\"/></visibility_masks><tags><tag name=\"walk_area_vertex\"/></tags><transform position=\"")
                    .Append(V3(x, y, G.At(z, x, y))).Append("\" rotation_euler=\"0.000, 0.000, 0.000\"/></game_entity>\n"); nEnt++;
            }
            var pth = new StringBuilder();
            for (int q = 0; q < paths.Count; q++)
            {
                pth.Append("<path name=\"spawn_path_").Append(q.ToString("00")).Append("\" interpolation_type=\"linear\" scene_upgrade_level_mask_=\"4294967295\" season_mask_=\"15\"><flags/><points>");
                foreach (var p in paths[q])
                    pth.Append("<point position=\"").Append(V3(p.x, p.y, p.z)).Append("\" scale=\"1.000, 1.000, 1.000\" color=\"1.000, 1.000, 1.000, 1.000\" rotation=\"0.00000, 0.00000, 0.00000, 1.00000\"><first_tangent position=\"")
                        .Append(V3(p.x, p.y + 2f, p.z)).Append("\"/><second_tangent position=\"").Append(V3(p.x, p.y - 2f, p.z)).Append("\"/></point>");
                pth.Append("</points></path>\n");
            }
            var text = new StringBuilder(data.Skeleton, data.Skeleton.Length + ents.Length + 20000);
            text.Replace("@NAME@", sceneName).Replace("@TMIN@", F3(zmin)).Replace("@TMAX@", F3(zmax));
            text.Replace("@WATER_LEVEL@", hasLevel ? F3(level) : data.WaterLevel).Replace("@WATER_EXISTS@", hasLevel ? "true" : data.WaterExists);
            var sFull = G.Slope(z); string[] season = { "Su", "Fa", "Wi", "Sp" };
            for (int k = 0; k < data.Nx * data.Ny; k++)
            {
                int jy = k / data.Nx, ix = k % data.Nx; float mn = float.MaxValue, mx = float.MinValue; double ssum = 0;
                for (int j = jy * 256; j <= jy * 256 + 256; j++) for (int i = ix * 256; i <= ix * 256 + 256; i++) { float v = z[j * N + i]; if (v < mn) mn = v; if (v > mx) mx = v; ssum += sFull[j * N + i]; }
                for (int s = 0; s < 4; s++) { long m = 0; for (int l = 0; l < data.L; l++) if (present[k][l] && data.Enabled[s][l]) m |= 1L << l; text.Replace("@N" + k + "_M" + season[s] + "@", m.ToString(Inv)); }
                text.Replace("@N" + k + "_HC@", F3(Math.Min(1f, 1.1f * (float)(ssum / (257.0 * 257.0))))).Replace("@N" + k + "_MIN@", F3(mn)).Replace("@N" + k + "_MAX@", F3(mx));
            }
            text.Replace("@ENTITIES@", "\n" + border + ents).Replace("@PATHS@", "\n" + pth);
            Lap("scene text");

            Directory.CreateDirectory(outDir);
            File.WriteAllBytes(Path.Combine(outDir, "terrain.bin"), terrainBin);
            File.WriteAllBytes(Path.Combine(outDir, "navmesh.bin"), navBin);
            File.WriteAllText(Path.Combine(outDir, "scene.xscene"), text.ToString(), new UTF8Encoding(false));
            var res = new FieldResult { Kind = kind, Rocks = rocks.Count, NavFaces = navFaces, Entities = nEnt, Paths = paths.Count };
            // viewpoints for the test camera: name, eye x y z, target x y z
            {
                float Gd(float x, float y) => G.At(z, x, y);
                (float x, float y) Toward((float x, float y) p, (float x, float y) q, float back, float side)
                { float dx = q.x - p.x, dy = q.y - p.y, L = (float)Math.Sqrt(dx * dx + dy * dy); if (L < 1e-6f) L = 1f; return (p.x - dx / L * back - dy / L * side, p.y - dy / L * back + dx / L * side); }
                void View(string nm, (float x, float y) e, float tx, float ty, float tz) => res.Views.Add(string.Format(Inv, "{0} {1:F2} {2:F2} 0.00 {3:F2} {4:F2} {5:F2}", nm, e.x, e.y, tx, ty, tz));
                View("spawnA", Toward(spawnPts[0], (C, C), 20f, 0f), C, C, Gd(C, C)); View("spawnB", Toward(spawnPts[1], (C, C), 20f, 0f), C, C, Gd(C, C));
                View("centre", (C, C), spawnPts[0].x, spawnPts[0].y, Gd(spawnPts[0].x, spawnPts[0].y)); View("flank", (C - 170 * sa, C + 170 * ca), C, C, Gd(C, C));
                foreach (var ft in feats.Take(3)) View(ft.typ, Toward((ft.x, ft.y), (C, C), -30f, 12f), ft.x, ft.y, Gd(ft.x, ft.y) + 1f);
                foreach (var q in rocks.Where(q => q.why != 2 && Math.Sqrt((q.x - C) * (q.x - C) + (q.y - C) * (q.y - C)) < PLAY).Take(2)) View("rock", Toward((q.x, q.y), (C, C), -24f, 8f), q.x, q.y, Gd(q.x, q.y) + 1.5f);
                File.WriteAllLines(Path.Combine(outDir, "views.txt"), res.Views);
            }
            Lap("write");
            long inPlay = 0, steepN = 0, forestN = 0, underN = 0; float pmin = float.MaxValue, pmax = float.MinValue;
            for (int k = 0; k < z.Length; k++) if (rr[k] < PLAY) { inPlay++; if (z[k] < pmin) pmin = z[k]; if (z[k] > pmax) pmax = z[k]; if (sl[k] > 0.5f) steepN++; if (forest[k] > 0.5f) forestN++; if (under[k]) underN++; }
            res.Relief = pmax - pmin; res.Steep = (float)steepN / inPlay; res.ForestShare = (float)forestN / inPlay; res.WaterShare = (float)underN / inPlay;
            res.Seconds = sw.Elapsed.TotalSeconds; res.Timings = times.ToString();
            return res;
        }
    }
}
