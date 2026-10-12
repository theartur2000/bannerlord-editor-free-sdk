// Rocks fitted to the ground (a port of sdk/rockfit.py): the pose with the least open rim in the air, earth banked over
// what is left, never more than a hand above any rock's own surface, and the cells a rock blocks.
using System;
using System.Collections.Generic;

namespace GenFields.Gen
{
    public sealed class RockKind
    {
        public string Name, Kind;                       // kind: wall, tower, boulder, slab
        public float N, Scale, ScaleHi, Dz, DzLo, DzHi, Tilt, Facing, FacingR, Lean;
        public float[] Rim, Body; public int[] Tris;    // xyz triples; triangles as vertex indices
        public float ZLo, Height; public bool[] Foot;   // lowest z and height of the body; the underside (lowest eighth)

        public void Prepare()
        {
            int n = Body.Length / 3; var zs = new float[n]; ZLo = float.MaxValue; float hi = float.MinValue;
            for (int k = 0; k < n; k++) { float z = Body[3 * k + 2]; zs[k] = z; if (z < ZLo) ZLo = z; if (z > hi) hi = z; }
            Height = hi - ZLo; var sorted = (float[])zs.Clone(); Array.Sort(sorted);
            float cut = sorted[Math.Min(n - 1, (int)(0.12f * (n - 1)))];
            Foot = new bool[n]; for (int k = 0; k < n; k++) Foot[k] = zs[k] <= cut;
        }
    }

    public sealed class Placed
    {
        public RockKind Rock; public float X, Y, Z, Lean, Roll, Yaw, Scale, Fill, High, Shown;
        public float[] Rim, Body;                       // world points
    }

    public static class Rocks
    {
        // the rotation a scene applies for rotation_euler = (x, y, z): Rz . Rx . Ry, as nine numbers row by row
        public static float[] Euler(float rx, float ry, float rz)
        {
            float cx = (float)Math.Cos(rx), sx = (float)Math.Sin(rx), cy = (float)Math.Cos(ry), sy = (float)Math.Sin(ry), cz = (float)Math.Cos(rz), sz = (float)Math.Sin(rz);
            // Rx . Ry
            float a00 = cy, a01 = 0, a02 = sy, a10 = sx * sy, a11 = cx, a12 = -sx * cy, a20 = -cx * sy, a21 = sx, a22 = cx * cy;
            return new[] { cz * a00 - sz * a10, cz * a01 - sz * a11, cz * a02 - sz * a12,
                           sz * a00 + cz * a10, sz * a01 + cz * a11, sz * a02 + cz * a12,
                           a20, a21, a22 };
        }

        static void Turn(float[] src, float[] dst, float[] m, float scale)
        {
            for (int k = 0; k < src.Length; k += 3)
            {
                float x = src[k] * scale, y = src[k + 1] * scale, z = src[k + 2] * scale;
                dst[k] = m[0] * x + m[1] * y + m[2] * z; dst[k + 1] = m[3] * x + m[4] * y + m[5] * z; dst[k + 2] = m[6] * x + m[7] * y + m[8] * z;
            }
        }

        static float Percentile(float[] a, int n, float q)
        {
            if (n == 0) return 0f;
            Array.Sort(a, 0, n); float pos = q * (n - 1); int i = (int)pos; float t = pos - i;
            return i + 1 < n ? a[i] * (1 - t) + a[i + 1] * t : a[i];
        }

        // Tries every pose; the one whose rim needs the least earth, among those that still show `show` of the rock and
        // have their underside in the ground. Null when none stands or the rim stays more than maxFill metres up.
        public static Placed Fit(float[] z, RockKind r, float x, float y, IList<float> yaws, IList<float> leans, IList<float> sinks, float scale, IList<float> rolls, float show, float maxFill)
        {
            float g0 = G.Ground(z, x, y); int nr = r.Rim.Length / 3, nb = r.Body.Length / 3;
            var rim = new float[r.Rim.Length]; var body = new float[r.Body.Length]; var gr = new float[nr]; var gb = new float[nb];
            var h = new float[nr]; var hf = new float[nb];
            float bestCost = float.MaxValue; Placed best = null;
            foreach (float yaw in yaws) foreach (float lean in leans) foreach (float roll in rolls)
            {
                var m = Euler(lean, roll, yaw);
                Turn(r.Rim, rim, m, scale); Turn(r.Body, body, m, scale);
                for (int k = 0; k < nr; k++) gr[k] = G.Ground(z, rim[3 * k] + x, rim[3 * k + 1] + y);
                for (int k = 0; k < nb; k++) gb[k] = G.Ground(z, body[3 * k] + x, body[3 * k + 1] + y);
                foreach (float sink in sinks)
                {
                    int shownN = 0, nf = 0;
                    for (int k = 0; k < nb; k++) { float hb = body[3 * k + 2] + g0 + sink - gb[k]; if (hb > 0.3f) shownN++; if (r.Foot[k]) hf[nf++] = hb; }
                    float shown = (float)shownN / nb;
                    if (shown < show || Percentile(hf, nf, 0.9f) > 0f) continue;
                    float fill = 0f;
                    for (int k = 0; k < nr; k++) { float v = rim[3 * k + 2] + g0 + sink - gr[k]; h[k] = v; if (v > 0) fill += v; }
                    if (nr > 0) fill /= nr;
                    float high = Percentile(h, nr, 0.95f);
                    float cost = fill + 0.35f * Math.Max(high, 0f) - 1.5f * Math.Min(shown, 0.6f);
                    if (cost < bestCost) { bestCost = cost; best = new Placed { Rock = r, X = x, Y = y, Z = g0 + sink, Lean = lean, Roll = roll, Yaw = yaw, Scale = scale, Fill = fill, High = high, Shown = shown }; }
                }
            }
            if (best == null || best.High > maxFill) return null;
            var bm = Euler(best.Lean, best.Roll, best.Yaw);
            best.Rim = new float[r.Rim.Length]; best.Body = new float[r.Body.Length];
            Turn(r.Rim, best.Rim, bm, scale); Turn(r.Body, best.Body, bm, scale);
            for (int k = 0; k < best.Rim.Length; k += 3) { best.Rim[k] += x; best.Rim[k + 1] += y; best.Rim[k + 2] += best.Z; }
            for (int k = 0; k < best.Body.Length; k += 3) { best.Body[k] += x; best.Body[k + 1] += y; best.Body[k + 2] += best.Z; }
            return best;
        }

        // the highest point of the placed rock above every cell it covers is written into top (a map of all rocks so far)
        public static void TopSurface(float[] top, Placed p)
        {
            int N = G.N; float cell = G.CELL; var W = p.Body; var T = p.Rock.Tris;
            for (int t = 0; t < T.Length; t += 3)
            {
                int a = 3 * T[t], b = 3 * T[t + 1], c = 3 * T[t + 2];
                float ax = W[a], ay = W[a + 1], az = W[a + 2], bx = W[b], by = W[b + 1], bz = W[b + 2], cx = W[c], cy = W[c + 1], cz = W[c + 2];
                int ia = Math.Max(0, (int)Math.Ceiling(Math.Min(ax, Math.Min(bx, cx)) / cell)), ib = Math.Min(N - 1, (int)(Math.Max(ax, Math.Max(bx, cx)) / cell));
                int ja = Math.Max(0, (int)Math.Ceiling(Math.Min(ay, Math.Min(by, cy)) / cell)), jb = Math.Min(N - 1, (int)(Math.Max(ay, Math.Max(by, cy)) / cell));
                if (ia > ib || ja > jb) continue;
                float d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy); if (Math.Abs(d) < 1e-9f) continue;
                float inv = 1f / d;
                for (int j = ja; j <= jb; j++)
                {
                    float Y = j * cell;
                    for (int i = ia; i <= ib; i++)
                    {
                        float X = i * cell;
                        float w0 = ((by - cy) * (X - cx) + (cx - bx) * (Y - cy)) * inv, w1 = ((cy - ay) * (X - cx) + (ax - cx) * (Y - cy)) * inv, w2 = 1f - w0 - w1;
                        if (w0 < -1e-6f || w1 < -1e-6f || w2 < -1e-6f) continue;
                        float zz = w0 * az + w1 * bz + w2 * cz; int k = j * N + i;
                        if (zz > top[k]) top[k] = zz;
                    }
                }
            }
        }

        // banks earth over the rim: every rim point ends `margin` under the ground, the earth falling away at slope talus;
        // never more than margin above the highest rock surface (top), never where keep is set (water)
        public static void Cover(float[] z, Placed p, float[] top, bool[] keep, float margin = 0.35f, float talus = 0.75f)
        {
            int N = G.N; float cell = G.CELL; var rim = p.Rim;
            for (int q = 0; q < rim.Length; q += 3)
            {
                float px = rim[q], py = rim[q + 1], pz = rim[q + 2] + margin;
                float hgt = pz - G.Ground(z, px, py); if (hgt <= 0) continue;
                float reach = hgt / talus + 2 * cell;
                int j0 = Math.Max(0, (int)((py - reach) / cell)), j1 = Math.Min(N - 1, (int)((py + reach) / cell) + 1);
                int i0 = Math.Max(0, (int)((px - reach) / cell)), i1 = Math.Min(N - 1, (int)((px + reach) / cell) + 1);
                for (int j = j0; j <= j1; j++)
                {
                    float dy = j * cell - py; int k = j * N + i0;
                    for (int i = i0; i <= i1; i++, k++)
                    {
                        if (keep != null && keep[k]) continue;
                        float dx = i * cell - px, d = (float)Math.Sqrt(dx * dx + dy * dy) - cell; if (d < 0) d = 0;
                        float need = pz - talus * d; if (need <= z[k]) continue;
                        float t = top[k];
                        if (t > -1e8f) { float cap = Math.Max(z[k], t + margin); if (need > cap) need = cap; }
                        if (need > z[k]) z[k] = need;
                    }
                }
            }
        }

        // the cells where the rock stands more than `clear` metres out of the ground: nothing walks there
        public static void Footprint(float[] z, Placed p, bool[] mask, float clear = 0.6f)
        {
            int N = G.N; float cell = G.CELL; var b = p.Body;
            for (int q = 0; q < b.Length; q += 3)
            {
                if (b[q + 2] - G.Ground(z, b[q], b[q + 1]) <= clear) continue;
                int i = (int)Math.Round(b[q] / cell), j = (int)Math.Round(b[q + 1] / cell);
                i = i < 1 ? 1 : i > N - 2 ? N - 2 : i; j = j < 1 ? 1 : j > N - 2 ? N - 2 : j;
                for (int dj = -1; dj <= 1; dj++) for (int di = -1; di <= 1; di++) mask[(j + dj) * N + i + di] = true;
            }
        }
    }
}
