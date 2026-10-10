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

    def __init__(self, cell, wavelength=24.0, clip=4.0):
        self.cell = cell; self.wavelength = wavelength; self.clip = clip; self.donors = []

    def add(self, z):
        _, d = split_detail(z, self.cell, self.wavelength)
        m = int(90.0 / self.cell)            # a terrain's outer strip is often a straight skirt: leave it out
        if min(d.shape) > 4 * m: d = d[m:-m, m:-m]
        lim = self.clip * d.std()
        self.donors.append(np.pad(np.clip(d, -lim, lim), 64, mode="reflect"))
        return self

    def synthesize(self, shape, seed, patch=160.0, sources=10):
        """A detail field of the given shape (metres), the same for the same seed."""
        assert self.donors, "add at least one donor"
        rng = np.random.default_rng(seed); h, w = shape
        gy = max(2, int(np.ceil(h * self.cell / patch)) + 1); gx = max(2, int(np.ceil(w * self.cell / patch)) + 1)
        labels = rng.integers(sources, size=(gy, gx))
        # jittered nearest-patch map, then soft borders
        yy = (np.arange(h) / h * (gy - 1))[:, None] + 0.35 * (fbm_field((h, w), rng, 5))
        xx = (np.arange(w) / w * (gx - 1))[None, :] + 0.35 * (fbm_field((h, w), rng, 5))
        lab = labels[np.clip(np.rint(yy).astype(int), 0, gy - 1), np.clip(np.rint(xx).astype(int), 0, gx - 1)]
        out = np.zeros(shape); total = np.zeros(shape)
        soft = max(2.0, 0.12 * patch / self.cell)
        for s in range(sources):
            m = (lab == s)
            if not m.any(): continue
            wgt = blur_fast(m.astype(np.float64), soft)
            d = self.donors[int(rng.integers(len(self.donors)))]
            if rng.integers(2): d = d[::-1]
            if rng.integers(2): d = d[:, ::-1]
            d = np.rot90(d, int(rng.integers(4)))
            if d.shape[0] < h or d.shape[1] < w:
                d = np.pad(d, ((0, max(0, h - d.shape[0])), (0, max(0, w - d.shape[1]))), mode="reflect")
            oy = int(rng.integers(d.shape[0] - h + 1)); ox = int(rng.integers(d.shape[1] - w + 1))
            out += wgt * d[oy:oy + h, ox:ox + w]; total += wgt
        return out / np.maximum(total, 1e-6)

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
