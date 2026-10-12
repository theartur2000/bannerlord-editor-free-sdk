// The numeric building blocks of the in-game battlefield generator: grids, blur, noise, landforms, erosion, scatter.
// A port of sdk/terraform.py (the Python reference, which explains each technique at length) written for speed:
// plain float arrays, loops over rows in parallel, nothing from the game engine. Rows are y, columns x.
using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace GenFields.Gen
{
    public sealed class Rng
    {
        readonly Random _r;
        public Rng(int seed) { _r = new Random(seed); }
        public float Uni(float a, float b) => a + (b - a) * (float)_r.NextDouble();
        public double Next() => _r.NextDouble();
        public int Int(int n) => _r.Next(n);                      // 0 .. n - 1
        public int Int(int a, int b) => _r.Next(a, b);            // a .. b - 1
        public float Normal(float mean, float sd)
        {
            double u = 1.0 - _r.NextDouble(), v = _r.NextDouble();
            return mean + sd * (float)(Math.Sqrt(-2.0 * Math.Log(u)) * Math.Cos(2.0 * Math.PI * v));
        }
        public int Fork() => _r.Next();
    }

    public static class G
    {
        public const int N = 1537;                 // samples a side: 6 nodes of 256 cells
        public const float FIELD = 1200f;          // metres a side
        public const float CELL = FIELD / (N - 1);
        public const float C = FIELD / 2;
        public const float PLAY = 330f;            // radius of the walkable area
        public const float SPAWN = 235f;           // the spawn paths reach this far from the middle
        public const float BUMPS = 2.2f;           // most small relief (metres rms) left on gentle ground of the fighting area

        public static float[] New() => new float[N * N];
        public static float Sm(float a, float lo, float hi) { float t = (a - lo) / (hi - lo); t = t < 0 ? 0 : t > 1 ? 1 : t; return t * t * (3 - 2 * t); }
        public static float Clamp(float a, float lo, float hi) => a < lo ? lo : a > hi ? hi : a;
        public static void Rows(Action<int> row) => Parallel.For(0, N, row);
        public static float[] Map(float[] a, Func<float, float> f) { var o = New(); Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) o[k] = f(a[k]); }); return o; }

        // ---- blur: three box passes stand in for a Gaussian of the given sigma (in samples); borders are held
        public static float[] Blur(float[] a, float sigma) => Blur(a, N, N, sigma);

        public static float[] Blur(float[] a, int w, int h, float sigma)
        {
            if (sigma <= 0.3f) return (float[])a.Clone();
            float wi = (float)Math.Sqrt(12.0 * sigma * sigma / 3.0 + 1.0);
            int lo = (int)Math.Floor(wi); if (lo % 2 == 0) lo--; if (lo < 1) lo = 1;
            int hi = lo + 2;
            int m = (int)Math.Round((12.0 * sigma * sigma - 3 * lo * lo - 12 * lo - 9) / (-4.0 * lo - 4.0));
            float[] cur = a, tmp = new float[w * h];
            for (int pass = 0; pass < 3; pass++)
            {
                int r = ((pass < m ? lo : hi) - 1) / 2;
                var o = new float[w * h];
                BoxH(cur, tmp, w, h, r); BoxV(tmp, o, w, h, r); cur = o;
            }
            return cur;
        }

        static void BoxH(float[] s, float[] d, int w, int h, int r)
        {
            if (r <= 0) { Array.Copy(s, d, w * h); return; }
            float inv = 1f / (2 * r + 1);
            Parallel.For(0, h, j =>
            {
                int o = j * w; float first = s[o], last = s[o + w - 1], acc = first * (r + 1);
                for (int i = 0; i < r; i++) acc += s[o + Math.Min(i, w - 1)];
                for (int i = 0; i < w; i++)
                {
                    acc += (i + r < w ? s[o + i + r] : last) - (i - r - 1 >= 0 ? s[o + i - r - 1] : first);
                    d[o + i] = acc * inv;
                }
            });
        }

        static void BoxV(float[] s, float[] d, int w, int h, int r)
        {
            if (r <= 0) { Array.Copy(s, d, w * h); return; }
            float inv = 1f / (2 * r + 1);
            int blocks = Math.Max(1, Environment.ProcessorCount);
            Parallel.For(0, blocks, b =>
            {
                int i0 = (int)((long)w * b / blocks), i1 = (int)((long)w * (b + 1) / blocks);
                var acc = new float[i1 - i0];
                for (int i = i0; i < i1; i++)
                {
                    float a = s[i] * (r + 1);
                    for (int j = 0; j < r; j++) a += s[Math.Min(j, h - 1) * w + i];
                    acc[i - i0] = a;
                }
                for (int j = 0; j < h; j++)
                {
                    int add = Math.Min(j + r, h - 1) * w, sub = Math.Max(j - r - 1, 0) * w, o = j * w;
                    for (int i = i0; i < i1; i++) { float a = acc[i - i0] + s[add + i] - s[sub + i]; acc[i - i0] = a; d[o + i] = a * inv; }
                }
            });
        }

        // rise over run per sample (central differences, one-sided at the border)
        public static float[] Slope(float[] z) { Grad(z, out var gx, out var gy); var o = New(); Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) o[k] = (float)Math.Sqrt(gx[k] * gx[k] + gy[k] * gy[k]); }); return o; }

        public static void Grad(float[] z, out float[] gx, out float[] gy)
        {
            var x = New(); var y = New();
            Rows(j =>
            {
                int k = j * N, ju = j > 0 ? -N : 0, jd = j < N - 1 ? N : 0; float dy = 1f / ((ju != 0 && jd != 0 ? 2 : 1) * CELL);
                for (int i = 0; i < N; i++, k++)
                {
                    int il = i > 0 ? -1 : 0, ir = i < N - 1 ? 1 : 0;
                    x[k] = (z[k + ir] - z[k + il]) / ((ir - il) * CELL);
                    y[k] = (z[k + jd] - z[k + ju]) * dy;
                }
            });
            gx = x; gy = y;
        }

        public static float At(float[] a, float x, float y)
        {
            int i = (int)Math.Round(x / CELL), j = (int)Math.Round(y / CELL);
            i = i < 0 ? 0 : i > N - 1 ? N - 1 : i; j = j < 0 ? 0 : j > N - 1 ? N - 1 : j;
            return a[j * N + i];
        }
        public static bool At(bool[] a, float x, float y)
        {
            int i = (int)Math.Round(x / CELL), j = (int)Math.Round(y / CELL);
            i = i < 0 ? 0 : i > N - 1 ? N - 1 : i; j = j < 0 ? 0 : j > N - 1 ? N - 1 : j;
            return a[j * N + i];
        }

        // terrain height at world x, y, bilinear
        public static float Ground(float[] z, float x, float y)
        {
            float fx = x / CELL, fy = y / CELL;
            fx = fx < 0 ? 0 : fx > N - 1.001f ? N - 1.001f : fx; fy = fy < 0 ? 0 : fy > N - 1.001f ? N - 1.001f : fy;
            int i = (int)fx, j = (int)fy; float u = fx - i, v = fy - j; int k = j * N + i;
            return z[k] * (1 - u) * (1 - v) + z[k + 1] * u * (1 - v) + z[k + N] * (1 - u) * v + z[k + N + 1] * u * v;
        }

        // the value below which the share q of the selected samples lies (a histogram of 4096 bins)
        public static float Quantile(float[] a, Func<int, bool> sel, float q, int step = 1)
        {
            float lo = float.MaxValue, hi = float.MinValue; int n = 0;
            for (int k = 0; k < a.Length; k += step) if (sel == null || sel(k)) { float v = a[k]; if (v < lo) lo = v; if (v > hi) hi = v; n++; }
            if (n == 0 || hi <= lo) return lo == float.MaxValue ? 0 : lo;
            var hist = new int[4096]; float sc = 4095.99f / (hi - lo);
            for (int k = 0; k < a.Length; k += step) if (sel == null || sel(k)) hist[(int)((a[k] - lo) * sc)]++;
            float want = q * n; int acc = 0;
            for (int b = 0; b < 4096; b++) { acc += hist[b]; if (acc >= want) return lo + (b + 0.5f) / sc; }
            return hi;
        }

        // ---- gradient (Perlin) noise in about [-1, 1], bumps about `wavelength` metres across
        public static float[] Noise(float wavelength, Rng rng) => Noise(N, N, CELL, wavelength, rng);

        public static float[] Noise(int w, int h, float cell, float wavelength, Rng rng)
        {
            float fy = h * cell / wavelength, fx = w * cell / wavelength;
            int gh = (int)Math.Ceiling(fy) + 2, gw = (int)Math.Ceiling(fx) + 2;
            var gxs = new float[gh * gw]; var gys = new float[gh * gw];
            for (int k = 0; k < gxs.Length; k++) { double a = rng.Next() * 2.0 * Math.PI; gxs[k] = (float)Math.Cos(a); gys[k] = (float)Math.Sin(a); }
            var o = new float[w * h]; float sy = fy / h, sx = fx / w;
            Parallel.For(0, h, j =>
            {
                float y = j * sy; int y0 = (int)y; float ty = y - y0, v = ty * ty * ty * (ty * (ty * 6 - 15) + 10);
                int r0 = y0 * gw, r1 = r0 + gw, k = j * w;
                for (int i = 0; i < w; i++, k++)
                {
                    float x = i * sx; int x0 = (int)x; float tx = x - x0, u = tx * tx * tx * (tx * (tx * 6 - 15) + 10);
                    float n00 = gxs[r0 + x0] * tx + gys[r0 + x0] * ty, n10 = gxs[r0 + x0 + 1] * (tx - 1) + gys[r0 + x0 + 1] * ty;
                    float n01 = gxs[r1 + x0] * tx + gys[r1 + x0] * (ty - 1), n11 = gxs[r1 + x0 + 1] * (tx - 1) + gys[r1 + x0 + 1] * (ty - 1);
                    o[k] = ((n00 * (1 - u) + n10 * u) * (1 - v) + (n01 * (1 - u) + n11 * u) * v) * 1.5f;
                }
            });
            return o;
        }

        // octaves of noise; ridged: sharp crests; erode: later octaves fade where the ground already slopes
        public static float[] Fbm(float wavelength, int octaves, Rng rng, float gain = 0.5f, bool ridged = false, float erode = 0f)
        {
            var o = New(); float amp = 1, tot = 0, wl = wavelength; float[] ax = erode > 0 ? New() : null, ay = erode > 0 ? New() : null;
            for (int oc = 0; oc < octaves; oc++)
            {
                var n = Noise(wl, rng);
                if (ridged) Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { float v = Clamp(n[k], -1, 1); n[k] = 1f - 2f * Math.Abs(v); } });
                float a = amp, w = wl;
                if (erode > 0)
                {
                    Grad(n, out var dx, out var dy);
                    Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) { ax[k] += dx[k] * w * a; ay[k] += dy[k] * w * a; o[k] += a * n[k] / (1f + erode * (ax[k] * ax[k] + ay[k] * ay[k])); } });
                }
                else Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) o[k] += a * n[k]; });
                tot += amp; amp *= gain; wl /= 2f;
            }
            float inv = 1f / tot; Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) o[k] *= inv; });
            return o;
        }

        // domain warping: every sample of a is read dx, dy metres away from where it is
        public static float[] Warp(float[] a, float[] dx, float[] dy)
        {
            var o = New();
            Rows(j =>
            {
                int k = j * N;
                for (int i = 0; i < N; i++, k++)
                {
                    float sx = Clamp(i + dx[k] / CELL, 0, N - 1.001f), sy = Clamp(j + dy[k] / CELL, 0, N - 1.001f);
                    int x0 = (int)sx, y0 = (int)sy; float tx = sx - x0, ty = sy - y0; int q = y0 * N + x0;
                    o[k] = a[q] * (1 - tx) * (1 - ty) + a[q + 1] * tx * (1 - ty) + a[q + N] * (1 - tx) * ty + a[q + N + 1] * tx * ty;
                }
            });
            return o;
        }

        // a (h x w) grid read at N x N samples, bilinear
        public static float[] Up(float[] g, int w, int h)
        {
            var o = New();
            Rows(j =>
            {
                float fy = (float)j * (h - 1) / (N - 1); int y0 = Math.Min((int)fy, h - 2); float ty = fy - y0; int k = j * N;
                for (int i = 0; i < N; i++, k++)
                {
                    float fx = (float)i * (w - 1) / (N - 1); int x0 = Math.Min((int)fx, w - 2); float tx = fx - x0; int q = y0 * w + x0;
                    o[k] = g[q] * (1 - ty) * (1 - tx) + g[q + 1] * (1 - ty) * tx + g[q + w] * ty * (1 - tx) + g[q + w + 1] * ty * tx;
                }
            });
            return o;
        }

        // Bridson's Poisson-disk sampling in the square of the field: points never closer than radius
        public static List<(float x, float y)> Poisson(float radius, Rng rng, Func<float, float, bool> accept, int tries = 20, int limit = 4000)
        {
            float cs = radius / 1.41421356f; int g = (int)Math.Ceiling(FIELD / cs); var grid = new int[g * g];
            for (int k = 0; k < grid.Length; k++) grid[k] = -1;
            var pts = new List<(float x, float y)>(); var active = new List<int>();
            bool Ok(float x, float y)
            {
                if (!(x >= 0 && x < FIELD && y >= 0 && y < FIELD)) return false;
                int gi = (int)(x / cs), gj = (int)(y / cs);
                for (int j = Math.Max(0, gj - 2); j < Math.Min(g, gj + 3); j++)
                    for (int i = Math.Max(0, gi - 2); i < Math.Min(g, gi + 3); i++)
                    {
                        int k = grid[j * g + i];
                        if (k >= 0) { float dx = pts[k].x - x, dy = pts[k].y - y; if (dx * dx + dy * dy < radius * radius) return false; }
                    }
                return true;
            }
            void Add(float x, float y) { grid[(int)(y / cs) * g + (int)(x / cs)] = pts.Count; pts.Add((x, y)); active.Add(pts.Count - 1); }
            Add(rng.Uni(0, FIELD * 0.999f), rng.Uni(0, FIELD * 0.999f));
            while (active.Count > 0 && pts.Count < limit)
            {
                int k = rng.Int(active.Count); var b = pts[active[k]]; bool placed = false;
                for (int t = 0; t < tries; t++)
                {
                    float a = rng.Uni(0, 6.2831853f), d = rng.Uni(radius, 2 * radius), x = b.x + d * (float)Math.Cos(a), y = b.y + d * (float)Math.Sin(a);
                    if (Ok(x, y)) { Add(x, y); placed = true; break; }
                }
                if (!placed) { active[k] = active[active.Count - 1]; active.RemoveAt(active.Count - 1); }
            }
            var o = new List<(float x, float y)>();
            foreach (var q in pts) if (accept == null || accept(q.x, q.y)) o.Add(q);
            return o;
        }
    }

    // landform shapes drawn on the field, with outlines bent by shared noise fields
    public sealed class Field
    {
        readonly Rng _rng; readonly Dictionary<int, float[]> _wob = new Dictionary<int, float[]>();
        static readonly float[] Keys = { 70f, 110f, 170f, 260f, 400f };
        public Field(Rng rng) { _rng = rng; }

        public float[] Wobble(float wavelength)
        {
            int best = 0; for (int k = 1; k < Keys.Length; k++) if (Math.Abs(Keys[k] - wavelength) < Math.Abs(Keys[best] - wavelength)) best = k;
            if (!_wob.TryGetValue(best, out var f)) { f = G.Noise(Keys[best], _rng); _wob[best] = f; }
            return f;
        }

        // a hill with an irregular outline, combined into dst by addition or as a maximum
        public void Hill(float[] dst, bool max, float x, float y, float radius, float height, float plateau = 0f, float wobble = 0.28f, float sharp = 1.6f)
        {
            int N = G.N; float cell = G.CELL;
            var w1 = Wobble(Math.Max(60f, radius * 0.9f)); var w2 = Wobble(Math.Max(60f, radius * 0.4f));
            float reach = radius * 4f;           // the outline factor never goes below a quarter
            int j0 = Math.Max(0, (int)((y - reach) / cell)), j1 = Math.Min(N - 1, (int)((y + reach) / cell) + 1);
            int i0 = Math.Max(0, (int)((x - reach) / cell)), i1 = Math.Min(N - 1, (int)((x + reach) / cell) + 1);
            if (j1 < j0 || i1 < i0) return;
            Parallel.For(j0, j1 + 1, j =>
            {
                float dy = j * cell - y; int k = j * N + i0;
                for (int i = i0; i <= i1; i++, k++)
                {
                    float dx = i * cell - x, f = 1f + wobble * w1[k] + 0.5f * wobble * w2[k]; if (f < 0.25f) f = 0.25f;
                    float d = (float)Math.Sqrt(dx * dx + dy * dy) / radius * f;
                    if (d >= 1f) continue;
                    float p = (float)Math.Pow(1f - G.Sm(d, 0f, 1f), sharp);
                    if (plateau > 0) p = Math.Min(p, 1f - plateau) / (1f - plateau);
                    float v = height * p;
                    if (max) { if (v > dst[k]) dst[k] = v; } else dst[k] += v;
                }
            });
        }

        // a ridge or hill mass from overlapping hills along a line of points: an uneven crest and outline
        public float[] Chain(IList<(float x, float y)> pts, float radius, float height, IList<(float pos, float depth, float half)> gaps = null, float plateau = 0f, float sharp = 1.6f, float jitter = 0.3f)
        {
            var acc = G.New(); int n = pts.Count;
            for (int k = 0; k < n; k++)
            {
                float t = (float)k / Math.Max(1, n - 1), h = height * _rng.Uni(0.75f, 1.1f);
                if (gaps != null) foreach (var gp in gaps) if (Math.Abs(t - gp.pos) < gp.half) h *= 1f - gp.depth * (1f - Math.Abs(t - gp.pos) / gp.half);
                float r = radius * _rng.Uni(0.8f, 1.25f), x = pts[k].x + _rng.Uni(-1, 1) * radius * jitter, y = pts[k].y + _rng.Uni(-1, 1) * radius * jitter;
                Hill(acc, true, x, y, r, h, plateau, 0.28f, sharp);
            }
            return G.Blur(acc, 7f / G.CELL);
        }
    }

    public static class Erosion
    {
        // Priority-flood: every pit raised just enough that water runs out over the border; the order cells were reached in
        static float[] Fill(float[] z, int w, int h, out int[] order, float eps = 1e-3f)
        {
            var zf = (float[])z.Clone(); var done = new bool[w * h]; var ord = new int[w * h]; int no = 0;
            var heap = new MinHeap(w * h);
            for (int j = 0; j < h; j++) { heap.Push(zf[j * w], j * w); done[j * w] = true; heap.Push(zf[j * w + w - 1], j * w + w - 1); done[j * w + w - 1] = true; }
            for (int i = 1; i < w - 1; i++) { heap.Push(zf[i], i); done[i] = true; heap.Push(zf[(h - 1) * w + i], (h - 1) * w + i); done[(h - 1) * w + i] = true; }
            while (heap.Count > 0)
            {
                heap.Pop(out float v, out int c); ord[no++] = c; int j = c / w, i = c - j * w;
                for (int dj = -1; dj <= 1; dj++)
                    for (int di = -1; di <= 1; di++)
                    {
                        if (dj == 0 && di == 0) continue;
                        int y = j + dj, x = i + di; if (y < 0 || y >= h || x < 0 || x >= w) continue;
                        int q = y * w + x; if (done[q]) continue;
                        done[q] = true; if (zf[q] < v + eps) zf[q] = v + eps;
                        heap.Push(zf[q], q);
                    }
            }
            order = ord; return zf;
        }

        // Stream power erosion on a coarse grid (implicit scheme of Braun and Willett); the change is laid back over the
        // full heights. Returns the eroded heights and the drainage area (square metres) at full size.
        public static float[] Fluvial(float[] z, float strength, int iterations, Rng rng, out float[] drain, int grid = 257, float m = 0.45f, float diffusion = 0.17f)
        {
            int N = G.N, step = Math.Max(1, (N - 1) / (grid - 1)); float c = G.CELL * step;
            var zb = G.Blur(z, step / 2f); int w = (N - 1) / step + 1, h = w;
            var zc = new float[w * h]; for (int j = 0; j < h; j++) for (int i = 0; i < w; i++) zc[j * w + i] = zb[j * step * N + i * step];
            var z0 = (float[])zc.Clone(); float kdt = 0.03f * strength; var area = new float[w * h];
            int[] dj8 = { -1, 1, 0, 0, -1, -1, 1, 1 }, di8 = { 0, 0, -1, 1, -1, 1, -1, 1 };
            var rec = new int[w * h]; var dist = new float[w * h]; var zr = new float[w * h];
            for (int it = 0; it < iterations; it++)
            {
                var r1 = G.Noise(w, h, c, 45f, rng); var r2 = G.Noise(w, h, c, 18f, rng);
                for (int k = 0; k < zr.Length; k++) zr[k] = zc[k] + 0.35f * r1[k] + 0.2f * r2[k] + rng.Normal(0f, 0.03f);
                var zf = Fill(zr, w, h, out int[] order);
                for (int j = 0; j < h; j++)
                    for (int i = 0; i < w; i++)
                    {
                        int k = j * w + i; float best = 0f; int rk = k; float rd = c;
                        for (int d = 0; d < 8; d++)
                        {
                            int y = Math.Min(h - 1, Math.Max(0, j + dj8[d])), x = Math.Min(w - 1, Math.Max(0, i + di8[d]));
                            float L = c * (d >= 4 ? 1.41421356f : 1f), sl = (zf[k] - zf[y * w + x]) / L;
                            if (sl <= 0) continue;
                            float score = sl * (0.5f + (float)rng.Next());        // one of the downhill neighbours, steeper ones more likely
                            if (score > best) { best = score; rk = y * w + x; rd = L; }
                        }
                        rec[k] = rk; dist[k] = rd;
                    }
                for (int k = 0; k < area.Length; k++) area[k] = c * c;
                for (int q = order.Length - 1; q >= 0; q--) { int i = order[q], r = rec[i]; if (r != i) area[r] += area[i]; }
                for (int q = 0; q < order.Length; q++)
                {
                    int i = order[q], r = rec[i];
                    if (r != i) { float f = kdt * (float)Math.Pow(area[i], m) / dist[i]; zc[i] = (zc[i] + f * zc[r]) / (1f + f); }
                }
                var nz = (float[])zc.Clone();
                for (int j = 1; j < h - 1; j++) for (int i = 1; i < w - 1; i++) { int k = j * w + i; nz[k] = zc[k] + diffusion * (zc[k - w] + zc[k + w] + zc[k - 1] + zc[k + 1] - 4 * zc[k]); }
                zc = nz;
            }
            var dc = new float[w * h]; for (int k = 0; k < dc.Length; k++) dc[k] = zc[k] - z0[k];
            var delta = G.Blur(G.Up(dc, w, h), step);
            var o = G.New(); G.Rows(j => { int k = j * N; for (int i = 0; i < N; i++, k++) o[k] = z[k] + delta[k]; });
            drain = G.Up(area, w, h);
            return o;
        }

        // Thermal erosion: ground steeper than talus slides to the lower neighbour
        public static float[] Thermal(float[] z, float talus, int iterations, float rate = 0.2f)
        {
            int N = G.N; float t = talus * G.CELL, f = rate * 0.5f; var a = (float[])z.Clone(); var b = G.New();
            for (int it = 0; it < iterations; it++)
            {
                var s = a; var d = b;
                G.Rows(j =>
                {
                    int k = j * N;
                    for (int i = 0; i < N; i++, k++)
                    {
                        float v = s[k], acc = 0f;
                        if (j > 0) { float df = v - s[k - N]; acc += df > t ? -(df - t) : df < -t ? -(df + t) : 0f; }
                        if (j < N - 1) { float df = v - s[k + N]; acc += df > t ? -(df - t) : df < -t ? -(df + t) : 0f; }
                        if (i > 0) { float df = v - s[k - 1]; acc += df > t ? -(df - t) : df < -t ? -(df + t) : 0f; }
                        if (i < N - 1) { float df = v - s[k + 1]; acc += df > t ? -(df - t) : df < -t ? -(df + t) : 0f; }
                        d[k] = v + f * acc;
                    }
                });
                a = d; b = s;
            }
            return a;
        }
    }

    sealed class MinHeap
    {
        float[] _k; int[] _v; public int Count;
        public MinHeap(int cap) { _k = new float[Math.Max(16, cap / 4)]; _v = new int[_k.Length]; }
        public void Push(float key, int val)
        {
            if (Count == _k.Length) { Array.Resize(ref _k, Count * 2); Array.Resize(ref _v, Count * 2); }
            int i = Count++;
            while (i > 0) { int p = (i - 1) >> 1; if (_k[p] <= key) break; _k[i] = _k[p]; _v[i] = _v[p]; i = p; }
            _k[i] = key; _v[i] = val;
        }
        public void Pop(out float key, out int val)
        {
            key = _k[0]; val = _v[0]; Count--; if (Count == 0) return;
            float k = _k[Count]; int v = _v[Count], i = 0;
            while (true)
            {
                int c = 2 * i + 1; if (c >= Count) break;
                if (c + 1 < Count && _k[c + 1] < _k[c]) c++;
                if (_k[c] >= k) break;
                _k[i] = _k[c]; _v[i] = _v[c]; i = c;
            }
            _k[i] = k; _v[i] = v;
        }
    }
}
