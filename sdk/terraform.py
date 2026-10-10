"""Building blocks for making terrain: filters, detail transfer, slope, texture layer weights by rule.

Nothing here reads the game by itself; the arrays come from sdk/terrain.py and sdk/worldmap.py.

Detail transfer: a height field cut from the world map has one real sample every 20 m or more, so it is smooth. A shipped,
hand-made battlefield has real small relief (gullies, bumps, banks). split_detail() takes that small relief off a donor
terrain (everything finer than a chosen wavelength) and add_detail() lays it over the smooth field, stronger on slopes.
The donor is read from the user's own install at generation time; none of it is stored in this repository.
"""
import numpy as np


def blur(a, sigma):
    """Gaussian blur with reflected borders, sigma in samples (separable, numpy only)."""
    if sigma <= 0: return a.astype(np.float64)
    r = int(np.ceil(sigma * 3)); x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2); k /= k.sum()
    p = np.pad(a.astype(np.float64), r, mode="reflect")
    p = np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 1, p)
    return np.apply_along_axis(lambda v: np.convolve(v, k, mode="valid"), 0, p)


def blur_fast(a, sigma):
    """The same blur through the FFT (periodic borders hidden by reflection padding); much faster for a large sigma."""
    if sigma <= 0: return a.astype(np.float64)
    r = int(np.ceil(sigma * 3))
    p = np.pad(a.astype(np.float64), r, mode="reflect")
    fy = np.fft.fftfreq(p.shape[0])[:, None]; fx = np.fft.rfftfreq(p.shape[1])[None, :]
    g = np.exp(-2.0 * (np.pi * sigma) ** 2 * (fx * fx + fy * fy))
    return np.fft.irfft2(np.fft.rfft2(p) * g, s=p.shape)[r:-r, r:-r]


def slope(z, cell):
    """Rise over run per sample."""
    gy, gx = np.gradient(z, cell)
    return np.hypot(gx, gy)


def split_detail(z, cell, wavelength=40.0):
    """(smooth, detail): the part of z coarser than about `wavelength` metres and the rest."""
    low = blur_fast(z, wavelength / cell / 2.5)
    return low, z - low


def add_detail(base, cell, donor_detail, seed=0, strength=1.0, flat=0.35, steep=1.6):
    """base plus a donor's detail. The donor window is picked, flipped and turned by the seed, so different places draw
    different ground from the same donor. Detail is scaled between `flat` (on level ground) and `steep` (on slopes of
    30 % and more), because banks and gullies belong on slopes."""
    rng = np.random.default_rng(seed)
    d = donor_detail
    if rng.integers(2): d = d[::-1]
    if rng.integers(2): d = d[:, ::-1]
    d = np.rot90(d, int(rng.integers(4)))
    h, w = base.shape
    if d.shape[0] < h or d.shape[1] < w:
        d = np.pad(d, ((0, max(0, h - d.shape[0])), (0, max(0, w - d.shape[1]))), mode="reflect")
    oy = int(rng.integers(d.shape[0] - h + 1)); ox = int(rng.integers(d.shape[1] - w + 1))
    d = d[oy:oy + h, ox:ox + w]
    s = np.clip(slope(blur_fast(base, 6), cell) / 0.30, 0, 1)
    return base + strength * (flat + (steep - flat) * s) * d


class DetailBank:
    """Small relief taken from several donor terrains and quilted into new ground.

    One donor laid over a whole field shows through: its hills and paths are still readable. So only relief finer than
    `wavelength` is kept, outliers (pits, cliffs) are clipped, and a field is assembled from patches of about `patch` metres,
    each from a random donor at a random offset, flip and turn, cross-faded into its neighbours."""

    def __init__(self, cell, wavelength=24.0, clip=4.0, longest=None):
        """wavelength: relief finer than this is kept. longest: when given, a band is kept instead: relief between
        `wavelength` and `longest` metres (whole hillocks, gullies and banks of the donor, without its fine grain)."""
        self.cell = cell; self.wavelength = wavelength; self.clip = clip; self.longest = longest; self.donors = []

    def add(self, z):
        if self.longest:
            d = blur_fast(z, self.wavelength / self.cell / 2.5) - blur_fast(z, self.longest / self.cell / 2.5)
        else:
            _, d = split_detail(z, self.cell, self.wavelength)
        m = int(90.0 / self.cell)            # a terrain's outer strip is often a straight skirt: leave it out
        if min(d.shape) > 4 * m: d = d[m:-m, m:-m]
        lim = self.clip * d.std()
        self.donors.append(np.pad(np.clip(d, -lim, lim), 64, mode="reflect"))
        return self

    def synthesize(self, shape, seed, patch=160.0, sources=None, soft=None):
        """A detail field of the given shape, the same for the same seed. The field is laid from square windows of two
        patches a side that overlap by half and fade into each other (raised cosine); every window is cut from a random
        donor at a random offset, flip and turn. `sources` and `soft` are accepted for older callers and not used."""
        assert self.donors, "add at least one donor"
        rng = np.random.default_rng(seed); h, w = shape
        p = max(8, int(round(patch / self.cell))); win = 2 * p
        small = min(min(d.shape) for d in self.donors)
        if win > small: p = small // 2; win = 2 * p
        k = 0.5 - 0.5 * np.cos(2 * np.pi * (np.arange(win) + 0.5) / win)
        W2 = np.outer(k, k)
        out = np.zeros((h + 2 * win, w + 2 * win)); tot = np.zeros_like(out)
        for y0 in range(0, h + win, p):
            for x0 in range(0, w + win, p):
                d = self.donors[int(rng.integers(len(self.donors)))]
                oy = int(rng.integers(d.shape[0] - win + 1)); ox = int(rng.integers(d.shape[1] - win + 1))
                t = d[oy:oy + win, ox:ox + win]
                if rng.integers(2): t = t[::-1]
                if rng.integers(2): t = t[:, ::-1]
                t = np.rot90(t, int(rng.integers(4)))
                out[y0:y0 + win, x0:x0 + win] += t * W2; tot[y0:y0 + win, x0:x0 + win] += W2
        return (out / np.maximum(tot, 1e-6))[win:win + h, win:win + w]

    def apply(self, base, seed, strength=1.0, flat=0.45, steep=1.5, patch=160.0):
        """base plus quilted detail, scaled between `flat` on level ground and `steep` on slopes of 30 % and more."""
        d = self.synthesize(base.shape, seed, patch)
        s = np.clip(slope(blur_fast(base, 6), self.cell) / 0.30, 0, 1)
        return base + strength * (flat + (steep - flat) * s) * d


def fbm_field(shape, rng, base=5, octaves=3):
    """Smooth noise in about [-1, 1] from a random generator (coarse grids blurred up)."""
    h, w = shape; out = np.zeros(shape); amp = 1.0; tot = 0.0; n = base
    for _ in range(octaves):
        g = rng.standard_normal((n + 1, n + 1))
        yi = np.linspace(0, n, h); xi = np.linspace(0, n, w)
        y0 = np.clip(yi.astype(int), 0, n - 1); x0 = np.clip(xi.astype(int), 0, n - 1)
        ty = (yi - y0)[:, None]; tx = (xi - x0)[None, :]
        ty = ty * ty * (3 - 2 * ty); tx = tx * tx * (3 - 2 * tx)
        v = (g[y0][:, x0] * (1 - ty) * (1 - tx) + g[y0][:, x0 + 1] * (1 - ty) * tx + g[y0 + 1][:, x0] * ty * (1 - tx) + g[y0 + 1][:, x0 + 1] * ty * tx)
        out += amp * v; tot += amp; amp *= 0.5; n *= 2
    return out / tot / 1.5


def smoothstep(a, lo, hi):
    t = np.clip((a - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def layer_weights(z, cell, rules, noise=None):
    """Texture layer weights from rules, as uint8 arrays that sum to 255 per sample.

    rules: list of (layer name, function(height, slope, noise) -> weight >= 0); the first rule is the base layer and
    takes what the others leave. Returns {name: uint8 array}."""
    s = slope(blur_fast(z, 1.5), cell)
    n = noise if noise is not None else np.zeros_like(z)
    names = [r[0] for r in rules]
    w = [np.clip(np.asarray(r[1](z, s, n), np.float64), 0, 1) if r[1] is not None else None for r in rules]
    rest = np.ones_like(z)
    out = {}
    for name, wi in list(zip(names, w))[:0:-1]:      # later rules lie on top
        take = wi * rest; out[name] = take; rest = rest - take
    out[names[0]] = rest
    q = {k: np.rint(v * 255).astype(np.int32) for k, v in out.items()}
    err = 255 - sum(q.values())
    q[names[0]] = np.clip(q[names[0]] + err, 0, 255)
    return {k: q[k].astype(np.uint8) for k in names}


# ---------------------------------------------------------------- noise and landforms

def _cubic(n, m):
    """Sample positions and Catmull-Rom weights to read m samples across the n cells of an (n + 3)-point grid."""
    x = np.linspace(1.0, n + 1.0, m)
    i = np.minimum(np.floor(x).astype(np.int64), n); t = x - i
    w = np.stack([((-t + 2) * t - 1) * t / 2, ((3 * t - 5) * t * t + 2) / 2, ((-3 * t + 4) * t + 1) * t / 2, (t - 1) * t * t / 2], axis=1)
    return np.stack([i - 1, i, i + 1, i + 2], axis=1), w


def noise(shape, cell, wavelength, rng):
    """Smooth noise in about [-1, 1] whose bumps are about `wavelength` metres across."""
    h, w = shape
    ny = max(1, int(round(h * cell / wavelength))); nx = max(1, int(round(w * cell / wavelength)))
    g = rng.standard_normal((ny + 3, nx + 3)) * 0.6
    iy, wy = _cubic(ny, h); ix, wx = _cubic(nx, w)
    rows = (g[iy] * wy[:, :, None]).sum(axis=1)
    return (rows[:, ix] * wx[None, :, :]).sum(axis=2)


def fbm(shape, cell, wavelength, octaves, rng, gain=0.5, ridged=False, erode=0.0):
    """Octaves of noise, the first with bumps of `wavelength` metres, each next one half as wide.
    ridged: sharp crests instead of round bumps. erode > 0: later octaves fade where the ground already slopes, which leaves
    smooth valley floors and detailed slopes the way running water does (a cheap stand-in for erosion)."""
    out = np.zeros(shape); amp = 1.0; tot = 0.0; gx = np.zeros(shape); gy = np.zeros(shape); wl = float(wavelength)
    for _ in range(octaves):
        n = noise(shape, cell, wl, rng)
        if ridged: n = 1.0 - 2.0 * np.abs(np.clip(n, -1, 1))
        if erode > 0:
            dy, dx = np.gradient(n, cell)
            gx += dx * wl * amp; gy += dy * wl * amp
            n = n / (1.0 + erode * (gx * gx + gy * gy))
        out += amp * n; tot += amp; amp *= gain; wl /= 2.0
    return out / tot


class Field:
    """Coordinates of a square field in metres and the landform shapes drawn on it. Everything returns arrays of the
    field's shape; heights are metres to add."""

    def __init__(self, samples, size, rng):
        self.n = samples; self.size = float(size); self.cell = self.size / (samples - 1); self.rng = rng
        ax = np.linspace(0.0, self.size, samples)
        self.X, self.Y = np.meshgrid(ax, ax)          # row = y
        self.shape = (samples, samples)
        self._wob = {}

    def wobble(self, wavelength=120.0):
        """A noise field shared by shapes so their outlines are not geometric."""
        key = min((70.0, 110.0, 170.0, 260.0, 400.0), key=lambda k: abs(k - wavelength))      # a few shared fields, not one per shape
        if key not in self._wob: self._wob[key] = noise(self.shape, self.cell, key, self.rng)
        return self._wob[key]

    def dist(self, x, y): return np.hypot(self.X - x, self.Y - y)

    def hill(self, x, y, radius, height, plateau=0.0, wobble=0.28, sharp=1.6):
        """A hill with an irregular outline. plateau 0..0.8 flattens the top; sharp > 1 steepens the flanks."""
        d = self.dist(x, y) / radius * (1.0 + wobble * self.wobble(max(60.0, radius * 0.9)) + 0.5 * wobble * self.wobble(max(60.0, radius * 0.4)))
        p = (1.0 - smoothstep(d, 0.0, 1.0)) ** sharp
        if plateau > 0: p = np.minimum(p, 1.0 - plateau) / (1.0 - plateau)
        return height * p

    def segment(self, x0, y0, x1, y1):
        """(distance to the segment, position along it 0..1)."""
        dx, dy = x1 - x0, y1 - y0; L2 = dx * dx + dy * dy
        t = np.clip(((self.X - x0) * dx + (self.Y - y0) * dy) / L2, 0.0, 1.0)
        return np.hypot(self.X - (x0 + t * dx), self.Y - (y0 + t * dy)), t

    def ridge(self, x0, y0, x1, y1, width, height, saddles=(), wobble=0.3, sharp=1.3, plateau=0.0):
        """A ridge between two points. saddles: [(position 0..1, depth 0..1, half-length 0..1)] lower the crest there."""
        d, t = self.segment(x0, y0, x1, y1)
        d = d / width * (1.0 + wobble * self.wobble(max(60.0, width * 1.2)))
        p = (1.0 - smoothstep(d, 0.0, 1.0)) ** sharp
        if plateau > 0: p = np.minimum(p, 1.0 - plateau) / (1.0 - plateau)
        crest = 0.8 + 0.2 * self.wobble(max(80.0, width * 2.0))
        crest = crest * smoothstep(t, 0.0, 0.12) * (1.0 - smoothstep(t, 0.88, 1.0)) + 0.0
        for pos, depth, half in saddles:
            crest = crest * (1.0 - depth * (1.0 - smoothstep(np.abs(t - pos), 0.0, half)))
        return height * p * crest

    def chain(self, points, radius, height, gaps=(), plateau=0.0, sharp=1.6, jitter=0.3):
        """A ridge or a hill mass built from overlapping hills along a line of points [(x, y), ...], so it has an uneven
        crest and outline instead of a straight bar. gaps: [(position 0..1, depth 0..1, half-length 0..1)] lower it there."""
        acc = np.zeros(self.shape); n = len(points)
        for k, (x, y) in enumerate(points):
            t = k / max(1, n - 1); h = height * float(self.rng.uniform(0.75, 1.1))
            for pos, depth, half in gaps:
                if abs(t - pos) < half: h *= (1.0 - depth * (1.0 - abs(t - pos) / half))
            r = radius * float(self.rng.uniform(0.8, 1.25))
            x += float(self.rng.uniform(-1, 1)) * radius * jitter; y += float(self.rng.uniform(-1, 1)) * radius * jitter
            acc = np.maximum(acc, self.hill(x, y, r, h, plateau=plateau, sharp=sharp))
        return blur_fast(acc, 7.0 / self.cell)
