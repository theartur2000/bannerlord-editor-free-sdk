// The scene files the generator writes: terrain.bin (PNG images in LZ4 blocks), navmesh.bin and the scene text.
// Formats as documented in sdk/terrain.py and sdk/navmesh.py. Written for speed, not for byte identity with the editor:
// PNG rows use the Sub filter and the fastest deflate, which the game reads like any other PNG.
using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Text;
using System.Threading.Tasks;

namespace GenFields.Gen
{
    public static class Files
    {
        static readonly uint[] Crc = MakeCrc();
        static uint[] MakeCrc() { var t = new uint[256]; for (uint n = 0; n < 256; n++) { uint c = n; for (int k = 0; k < 8; k++) c = (c & 1) != 0 ? 0xEDB88320u ^ (c >> 1) : c >> 1; t[n] = c; } return t; }

        static void BE(Stream s, uint v) { s.WriteByte((byte)(v >> 24)); s.WriteByte((byte)(v >> 16)); s.WriteByte((byte)(v >> 8)); s.WriteByte((byte)v); }

        static void Chunk(Stream s, string tag, byte[] body, int len)
        {
            BE(s, (uint)len); var t = Encoding.ASCII.GetBytes(tag); s.Write(t, 0, 4); s.Write(body, 0, len);
            uint c = 0xFFFFFFFFu; foreach (byte b in t) c = Crc[(c ^ b) & 0xFF] ^ (c >> 8);
            for (int i = 0; i < len; i++) c = Crc[(c ^ body[i]) & 0xFF] ^ (c >> 8);
            BE(s, c ^ 0xFFFFFFFFu);
        }

        // rows: h rows of `stride` bytes (samples big-endian); bitDepth 8 or 16; colorType 0 grey or 6 RGBA; bpp bytes per pixel
        public static byte[] Png(byte[] rows, int w, int h, int stride, int bitDepth, int colorType, int bpp)
        {
            var raw = new byte[h * (stride + 1)];
            for (int j = 0; j < h; j++)
            {
                int o = j * (stride + 1), s = j * stride; raw[o] = 1;                           // filter 1: Sub
                for (int i = 0; i < stride; i++) raw[o + 1 + i] = (byte)(rows[s + i] - (i >= bpp ? rows[s + i - bpp] : 0));
            }
            uint a = 1, b = 0; for (int i = 0; i < raw.Length; i++) { a += raw[i]; if (a >= 65521) a -= 65521; b += a; if (b >= 65521) b -= 65521; }
            byte[] z;
            using (var ms = new MemoryStream())
            {
                ms.WriteByte(0x78); ms.WriteByte(0x01);
                using (var ds = new DeflateStream(ms, CompressionLevel.Fastest, true)) ds.Write(raw, 0, raw.Length);
                BE(ms, (b << 16) | a); z = ms.ToArray();
            }
            using (var ms = new MemoryStream(z.Length + 100))
            {
                ms.Write(new byte[] { 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A }, 0, 8);
                var ih = new byte[13]; ih[0] = (byte)(w >> 24); ih[1] = (byte)(w >> 16); ih[2] = (byte)(w >> 8); ih[3] = (byte)w; ih[4] = (byte)(h >> 24); ih[5] = (byte)(h >> 16); ih[6] = (byte)(h >> 8); ih[7] = (byte)h;
                ih[8] = (byte)bitDepth; ih[9] = (byte)colorType;
                Chunk(ms, "IHDR", ih, 13); Chunk(ms, "IDAT", z, z.Length); Chunk(ms, "IEND", new byte[0], 0);
                return ms.ToArray();
            }
        }

        // an LZ4 block: greedy matches found through a hash of four bytes (any LZ4 decoder reads it)
        public static byte[] Lz4(byte[] src)
        {
            int n = src.Length; var dst = new byte[n + n / 255 + 32]; int o = 0, anchor = 0, i = 0;
            var table = new int[1 << 16]; for (int k = 0; k < table.Length; k++) table[k] = -1;
            void Emit(int litStart, int litLen, int mlen, int off)
            {
                int tok = o++; dst[tok] = (byte)((Math.Min(litLen, 15) << 4) | (mlen > 0 ? Math.Min(mlen - 4, 15) : 0));
                if (litLen >= 15) { int r = litLen - 15; while (r >= 255) { dst[o++] = 255; r -= 255; } dst[o++] = (byte)r; }
                Buffer.BlockCopy(src, litStart, dst, o, litLen); o += litLen;
                if (mlen > 0)
                {
                    dst[o++] = (byte)off; dst[o++] = (byte)(off >> 8);
                    if (mlen - 4 >= 15) { int r = mlen - 19; while (r >= 255) { dst[o++] = 255; r -= 255; } dst[o++] = (byte)r; }
                }
            }
            if (n >= 13)
                while (i <= n - 12)
                {
                    uint v = (uint)(src[i] | src[i + 1] << 8 | src[i + 2] << 16 | src[i + 3] << 24); int hsh = (int)((v * 2654435761u) >> 16);
                    int j = table[hsh]; table[hsh] = i;
                    if (j < 0 || i - j > 65535 || src[j] != src[i] || src[j + 1] != src[i + 1] || src[j + 2] != src[i + 2] || src[j + 3] != src[i + 3]) { i++; continue; }
                    int e = i + 4, jj = j + 4; while (e < n - 5 && src[e] == src[jj]) { e++; jj++; }
                    if (dst.Length - o < (i - anchor) + 32) Array.Resize(ref dst, dst.Length * 2);
                    Emit(anchor, i - anchor, e - i, i - j); anchor = i = e;
                }
            if (dst.Length - o < (n - anchor) + (n - anchor) / 255 + 32) Array.Resize(ref dst, o + (n - anchor) + (n - anchor) / 255 + 32);
            Emit(anchor, n - anchor, 0, 0);
            Array.Resize(ref dst, o); return dst;
        }

        static void U32(Stream s, uint v) { s.WriteByte((byte)v); s.WriteByte((byte)(v >> 8)); s.WriteByte((byte)(v >> 16)); s.WriteByte((byte)(v >> 24)); }
        static void U64(Stream s, ulong v) { U32(s, (uint)v); U32(s, (uint)(v >> 32)); }

        // terrain.bin from per-node parts. heights: 16-bit samples of the whole map; normals: RGBA of the whole map;
        // weights[layer]: bytes of the whole map or null; phys[season]: bytes of the whole map. nx x ny nodes of 257 samples.
        public static byte[] Terrain(ushort[] h16, byte[] normals, byte[][] weights, byte[][] phys, int nx, int ny, out bool[][] present)
        {
            int N = nx * 256 + 1, nodes = nx * ny, L = weights.Length; const int S = 257;
            var hght = new byte[nodes][]; var nrml = new byte[nodes][]; var wght = new byte[nodes][]; var phym = new byte[nodes][]; var pres = new bool[nodes][];
            Parallel.For(0, nodes, k =>
            {
                int jy = k / nx, ix = k % nx, j0 = jy * 256, i0 = ix * 256;
                var rows = new byte[S * S * 2];
                for (int j = 0; j < S; j++) for (int i = 0; i < S; i++) { ushort v = h16[(j0 + j) * N + i0 + i]; rows[(j * S + i) * 2] = (byte)(v >> 8); rows[(j * S + i) * 2 + 1] = (byte)v; }
                hght[k] = Blob(Png(rows, S, S, S * 2, 16, 0, 2));
                var nr = new byte[S * S * 4];
                for (int j = 0; j < S; j++) Buffer.BlockCopy(normals, ((j0 + j) * N + i0) * 4, nr, j * S * 4, S * 4);
                nrml[k] = Blob(Png(nr, S, S, S * 4, 8, 6, 4));
                pres[k] = new bool[L];
                using (var ms = new MemoryStream())
                {
                    var wr = new byte[S * S];
                    for (int l = 0; l < 16; l++)
                    {
                        bool has = false;
                        if (l < L && weights[l] != null)
                        {
                            for (int j = 0; j < S; j++) Buffer.BlockCopy(weights[l], (j0 + j) * N + i0, wr, j * S, S);
                            if (l == 0) has = true; else for (int q = 0; q < wr.Length; q++) if (wr[q] != 0) { has = true; break; }
                        }
                        if (l < L) pres[k][l] = has;
                        if (!has) { U32(ms, 0); continue; }
                        U32(ms, 1); var png = Png(wr, S, S, S, 8, 0, 1); U32(ms, (uint)png.Length); ms.Write(png, 0, png.Length);
                    }
                    wght[k] = ms.ToArray();
                }
                var pb = new byte[4 * (8 + S * S)];
                for (int s = 0; s < 4; s++)
                {
                    int o = s * (8 + S * S); pb[o] = (byte)s; int sz = S * S; pb[o + 4] = (byte)sz; pb[o + 5] = (byte)(sz >> 8); pb[o + 6] = (byte)(sz >> 16);
                    for (int j = 0; j < S; j++) Buffer.BlockCopy(phys[s], (j0 + j) * N + i0, pb, o + 8 + j * S, S);
                }
                phym[k] = pb;
            });
            present = pres;
            var blankRows = new byte[ny * 256 * nx * 256]; for (int q = 0; q < blankRows.Length; q++) blankRows[q] = 255;
            var blank = Blob(Png(blankRows, nx * 256, ny * 256, nx * 256, 8, 0, 1));
            byte[] midx; using (var ms = new MemoryStream()) { for (int q = 0; q < 4; q++) ms.Write(blank, 0, blank.Length); midx = ms.ToArray(); }
            string[] tags = { "MIDX", "HGHT", "NRML", "WGHT", "PHYM" };
            byte[][][] parts = { new[] { midx }, hght, nrml, wght, phym };
            var bodies = new byte[5][]; var stored = new uint[5]; var rawLen = new uint[5];
            Parallel.For(0, 5, t =>
            {
                long total = 0; foreach (var p in parts[t]) total += p.Length;
                var raw = new byte[total]; int o = 0; var offs = new uint[parts[t].Length];
                for (int q = 0; q < parts[t].Length; q++) { offs[q] = (uint)o; Buffer.BlockCopy(parts[t][q], 0, raw, o, parts[t][q].Length); o += parts[t][q].Length; }
                var z = Lz4(raw);
                using (var ms = new MemoryStream())
                {
                    if (t != 0) foreach (uint v in offs) U32(ms, v);
                    U64(ms, 3); U64(ms, (ulong)raw.Length); U64(ms, (ulong)(40 + z.Length)); ms.Write(new byte[16], 0, 16); ms.Write(z, 0, z.Length);
                    bodies[t] = ms.ToArray();
                }
                stored[t] = (uint)(40 + z.Length); rawLen[t] = (uint)raw.Length;
            });
            using (var ms = new MemoryStream())
            {
                ms.Write(Encoding.ASCII.GetBytes("ZGR6RTRN"), 0, 8); U32(ms, 2);
                uint off = 12 + 20 * 5;
                for (int t = 0; t < 5; t++) { ms.Write(Encoding.ASCII.GetBytes(tags[t]), 0, 4); U32(ms, 1); U32(ms, off); U32(ms, stored[t]); U32(ms, rawLen[t]); off += (uint)bodies[t].Length; }
                for (int t = 0; t < 5; t++) ms.Write(bodies[t], 0, bodies[t].Length);
                return ms.ToArray();
            }
        }

        static byte[] Blob(byte[] png) { var o = new byte[png.Length + 4]; o[0] = (byte)png.Length; o[1] = (byte)(png.Length >> 8); o[2] = (byte)(png.Length >> 16); o[3] = (byte)(png.Length >> 24); Buffer.BlockCopy(png, 0, o, 4, png.Length); return o; }

        // navmesh.bin (NMG9) of square faces: vertices xyz, quads as four vertex indices counter-clockwise from above
        public static byte[] NavMesh(List<float> verts, List<int> quads)
        {
            var edgeIndex = new Dictionary<long, int>(); var edges = new List<int>(); int nf = quads.Count / 4; var faceEdges = new int[quads.Count];
            for (int f = 0; f < nf; f++)
                for (int i = 0; i < 4; i++)
                {
                    int a = quads[f * 4 + i], b = quads[f * 4 + (i + 1) % 4]; long key = a < b ? ((long)a << 32) | (uint)b : ((long)b << 32) | (uint)a;
                    if (!edgeIndex.TryGetValue(key, out int e)) { e = edges.Count / 2; edgeIndex[key] = e; edges.Add(a); edges.Add(b); }
                    faceEdges[f * 4 + i] = e;
                }
            byte[] pay;
            using (var ms = new MemoryStream())
            using (var w = new BinaryWriter(ms))
            {
                w.Write(Encoding.ASCII.GetBytes("NMG9")); w.Write((uint)(verts.Count / 3));
                foreach (float v in verts) w.Write(v);
                w.Write((uint)(edges.Count / 2));
                for (int e = 0; e < edges.Count; e += 2) { w.Write(-1); w.Write(edges[e]); w.Write(edges[e + 1]); w.Write(-1); w.Write(-1); w.Write(0); }
                w.Write((uint)nf);
                for (int f = 0; f < nf; f++)
                {
                    w.Write(4); for (int i = 0; i < 4; i++) w.Write(quads[f * 4 + i]); for (int i = 0; i < 4; i++) w.Write(faceEdges[f * 4 + i]);
                    w.Write(2); w.Write(0); w.Write(0); w.Write(0f); w.Write(0xFFFFFFFFu); w.Write((byte)0);
                }
                w.Write((uint)32); w.Write(new byte[256]);
                w.Flush(); pay = ms.ToArray();
            }
            var z = Lz4(pay);
            using (var ms = new MemoryStream())
            {
                ms.Write(Encoding.ASCII.GetBytes("RNM1"), 0, 4); U32(ms, (uint)(40 + z.Length)); U64(ms, 3); U64(ms, (ulong)pay.Length); U64(ms, (ulong)(40 + z.Length));
                ms.Write(new byte[16], 0, 16); ms.Write(z, 0, z.Length);
                return ms.ToArray();
            }
        }
    }
}
