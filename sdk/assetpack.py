"""Custom models and textures for a scene, from plain source files to a folder a module can load.

    st = assetpack.Stage(r"C:\\work\\my_stage")
    ground = st.terrain_set("my_sand", "sand_diffuse.png")                 # diffuse + derived normal, specular, height
    st.material("my_rock_mat", "rock_d.png", normal="rock_n.png")
    st.psk_mesh("my_rock", "Rock.pskx", ["my_rock_mat"])                   # or st.mesh("my_rock", submeshes, [...])
    st.install(r"<game>\\Modules\\MyMod")                                   # Assets/custom/*.tpac + RuntimeDataCache/*.rdc

A stage is a folder with Assets (one package per texture, material and mesh), RuntimeDataCache (their pixel and vertex
data, as the editor writes it) and AssetSources (the PNG files the packages were made from). Nothing in it comes from the
game: the writers are sdk/tpac.py and sdk/sdk_mesh.py. Scene files then use the assets by name:
    terrain layer textures     scenebuild.edit_layer(layer, textures=ground)
    a model standing somewhere scene.add_entity(mesh="my_rock", position=...)
"""
import json, os, shutil, sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import psk, sdk_mesh, templates, tpac
else:
    from . import psk, sdk_mesh, templates, tpac

TERRAIN_MAPS = (("diffusemap", "_d", "bc1", "albedo", ()), ("normalmap", "_n", "bc5", "normalmap", ("is_bumpmap",)),
                ("specularmap", "_s", "bc1", "specularmap", ("is_specularmap",)), ("displacementmap", "_h", "bc4", "heightmap", ()))


def _image(src):
    from PIL import Image
    return src if isinstance(src, Image.Image) else Image.open(src)


def derive_maps(diffuse, bump=2.5, gloss=0.22):
    """(normal, specular, height) pictures guessed from a diffuse one, for sources that come without them.
    height: the brightness, smoothed. normal: the slope of that height. specular in the game's terrain layout: red = metal
    (none), green = gloss (low, a little higher on bright spots), blue = occlusion (darker in the hollows)."""
    from PIL import Image, ImageFilter
    lum = np.asarray(_image(diffuse).convert("L").filter(ImageFilter.GaussianBlur(1.2)), np.float64) / 255.0
    h = (lum - lum.min()) / max(float(np.ptp(lum)), 1e-6)
    gx = (np.roll(h, -1, 1) - np.roll(h, 1, 1)) * bump; gy = (np.roll(h, -1, 0) - np.roll(h, 1, 0)) * bump
    n = np.dstack([-gx, gy, np.ones_like(h) / 4.0]); n /= np.linalg.norm(n, axis=2, keepdims=True)
    normal = Image.fromarray(np.clip(np.rint((n * 0.5 + 0.5) * 255), 0, 255).astype(np.uint8), "RGB")
    wide = np.asarray(Image.fromarray(np.uint8(h * 255)).filter(ImageFilter.GaussianBlur(6)), np.float64) / 255.0
    occ = np.clip(0.75 + 1.2 * (h - wide), 0.35, 1.0)
    spec = np.dstack([np.zeros_like(h), np.clip(gloss * (0.7 + 0.6 * h), 0, 1), occ])
    return normal, Image.fromarray(np.uint8(np.rint(spec * 255)), "RGB"), Image.fromarray(np.uint8(np.rint(h * 255)), "L")


class Stage:
    def __init__(self, folder, inline=False):
        self.folder = folder; self.inline = inline
        self.assets = os.path.join(folder, "Assets"); self.rdc = os.path.join(folder, "RuntimeDataCache"); self.sources = os.path.join(folder, "AssetSources")
        for d in (self.assets, self.rdc, self.sources): os.makedirs(d, exist_ok=True)
        self.made = {"textures": [], "materials": [], "meshes": []}

    def _png(self, src, name, mode=None, size=None):
        """The source picture as a PNG in AssetSources (converted, resized to a power of two no larger than `size`)."""
        from PIL import Image
        im = _image(src)
        if mode: im = im.convert(mode)
        elif im.mode not in ("RGB", "RGBA", "L"): im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        w, h = im.size; cap = size or 2048
        tw = min(cap, 1 << max(2, (w - 1).bit_length())); th = min(cap, 1 << max(2, (h - 1).bit_length()))
        if (tw, th) != (w, h): im = im.resize((tw, th), Image.LANCZOS)
        p = os.path.join(self.sources, name + ".png"); im.save(p)
        return p

    def texture(self, name, src, kind="diffusemap", terrain=False, cutout=False, size=None, inline=None):
        """One texture package. kind: diffusemap, normalmap, specularmap or displacementmap. terrain=True marks it for
        use by terrain layers. cutout=True keeps the alpha channel of a diffuse (leaves, grass cards). inline=True puts the
        pixels into the package itself instead of the cache folder (default: the stage's setting)."""
        key, suffix, fmt, usage, flags = next(m for m in TERRAIN_MAPS if m[0] == kind)
        flags = list(flags) + (["for_terrain"] if terrain else [])
        mode = "L" if kind == "displacementmap" else "RGBA" if cutout else "RGB"
        png = self._png(src, name, mode, size)
        if cutout: fmt = "bc3"
        tpac.write_texture(png, name, fmt, usage, mips=True, flags=flags, force=True, out_dir=self.assets, rdc_dir=self.rdc,
                           tex_flags=["has_alpha"] if cutout else [], inline=self.inline if inline is None else inline)
        self.made["textures"].append(name)
        return name

    def terrain_set(self, name, diffuse, normal=None, specular=None, height=None, size=1024, inline=None):
        """The four textures of one terrain layer, named <name>_d, _n, _s, _h. Missing maps are derived from the diffuse.
        Returns {"diffusemap": ..., "normalmap": ..., "specularmap": ..., "displacementmap": ...} for scenebuild.edit_layer."""
        dn, ds, dh = derive_maps(diffuse) if None in (normal, specular, height) else (None, None, None)
        src = {"diffusemap": diffuse, "normalmap": normal or dn, "specularmap": specular or ds, "displacementmap": height or dh}
        return {kind: self.texture(name + suffix, src[kind], kind, terrain=True, size=size if kind != "displacementmap" else min(size, 512), inline=inline)
                for kind, suffix, _, _, _ in TERRAIN_MAPS}

    def material(self, name, diffuse, normal=None, specular=None, cutout=False, size=1024, inline=None):
        """A material for static models with its textures (<name>_d, _n, _s). cutout=True: alpha tested and two-sided."""
        tex = {"diffuse": self.texture(name + "_d", diffuse, "diffusemap", cutout=cutout, size=size, inline=inline)}
        if normal is not None: tex["normal"] = self.texture(name + "_n", normal, "normalmap", size=size, inline=inline)
        if specular is not None: tex["specular"] = self.texture(name + "_s", specular, "specularmap", size=size, inline=inline)
        if cutout:
            t = templates.material_template(False)[0]
            tpac.write_material(name, "pbr_shading", tex, force=True, out_dir=self.assets, alpha_ref=0.5,
                                flags=list(t["mflags"]) + [f for f in t["sflags"] if f != "alpha_test"] + ["alpha_test", "two_sided"])
        else:
            tpac.write_material(name, "pbr_shading", tex, force=True, out_dir=self.assets)
        self.made["materials"].append(name)
        return name

    def mesh(self, name, subs, materials):
        """A static model from prepared submeshes (sdk_mesh.submeshes for FBX, psk.submeshes for PSK); materials: one
        material name per submesh, made with material() before."""
        sdk_mesh.write_metamesh(name, name, subs, name.encode(), self.assets, self.rdc, materials=list(materials), force=True, mtl_dir=self.assets)
        self.made["meshes"].append(name)
        return name

    def psk_mesh(self, name, path, materials, scale=0.01, ground=True, rotation=None):
        """A static model from a PSK / PSKX file. Returns (positions, triangles) of the model as written, for fitting it
        to the ground (rockfit.register)."""
        subs = psk.submeshes(path, name, scale=scale, rotation=rotation, ground=ground)
        P = []; F = []; base = 0
        for s in subs:
            P.append(np.asarray(s["P"], np.float64)); F.append(np.asarray(s["pi"], np.int64)[np.asarray(s["F"], np.int64)] + base); base += len(s["P"])
        self.mesh(name, subs, [materials[min(i, len(materials) - 1)] for i in range(len(subs))])
        return np.concatenate(P), np.concatenate(F)

    def install(self, module_dir, sub="custom"):
        """Copies the stage into a module: Assets/<sub>/, RuntimeDataCache/ and AssetSources/<sub>/. The game must be closed.
        The game reads a module's Assets folder and its caches only when the module also has an AssetSources folder
        (otherwise it looks for AssetPackages with all data inline): without it custom textures show black."""
        a = os.path.join(module_dir, "Assets", sub); r = os.path.join(module_dir, "RuntimeDataCache"); src = os.path.join(module_dir, "AssetSources", sub)
        for d in (a, src):
            if os.path.isdir(d): shutil.rmtree(d)
        os.makedirs(a, exist_ok=True); os.makedirs(r, exist_ok=True); os.makedirs(src, exist_ok=True); n = 0
        for f in os.listdir(self.sources): shutil.copyfile(os.path.join(self.sources, f), os.path.join(src, f))
        for f in os.listdir(self.assets): shutil.copyfile(os.path.join(self.assets, f), os.path.join(a, f)); n += 1
        for f in os.listdir(self.rdc): shutil.copyfile(os.path.join(self.rdc, f), os.path.join(r, f))
        with open(os.path.join(a, "_stage.json"), "w", encoding="utf-8") as f: json.dump(self.made, f)
        return n
