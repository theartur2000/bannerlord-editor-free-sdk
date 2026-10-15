"""Sound: WAV files in, an FMOD bank and the sound events the game can play out, from the command line.

The game plays sound through FMOD. A module adds sound by shipping a bank (Sounds/PC/<Module>.bank) and telling the engine
about its events (ModuleData/<module>_sound_event_data.xml). FMOD Studio builds banks; this module drives its command line
tool (fmodstudiocl, version 2.02.27 exactly: a newer Studio writes banks the game's FMOD 2.02 refuses), so no Studio window
and no Studio project are involved.

    sounds.json                  in the project: the events (path, WAV files, the Native event to take its mixing from)
    Sounds/src/<event path>/     the project's copies of the WAV files
    <home>/sound/<Module>/       the generated FMOD project and the build (rebuilt every time, safe to delete)
    <stage>/Sounds/PC/<Module>.bank
    <stage>/ModuleData/soundfiles.xml                 registers the bank (the engine loads module banks after Native's)
    <stage>/ModuleData/<module>_sound_event_data.xml  the events: path, GUID, length, distance, parameters
    <stage>/ModuleData/project.mbproj                 the two entries that point the engine at those files

How the events work (the route is the one in docs/AGENTS.md "Sound"):
  - The generated FMOD project is the Modding Kit's sample project without its events. It carries Native's mixer (buses, VCAs,
    returns, effect and parameter presets with Native's GUIDs), so the new events play through Native's buses and the game's
    volume sliders still work. The bank has its own GUID.
  - Each event is a multi instrument holding the WAV files (one picked at random per play). Its mixing (output bus, master
    effects, spatializer, parameters, voice limits) is cloned from the sample event closest to the event named by `like`,
    and its engine attributes (max_distance, parameter_indices ...) are copied from that Native event.
  - An event whose path is a Native path replaces it: the engine's loader overwrites an existing path with the module's row
    (new GUID, new bank). Every other path is added.
  - The game resolves an event by GUID; a module bank never reuses a Native GUID. All GUIDs are uuid5 of module and path, so a
    rebuild keeps them.
  - project.mbproj rule: every file it names must exist. The native sound loader writes through a null pointer when one is
    missing, so the stage ships the files first and the entries are only written beside them (install checks it again).
"""
import difflib, glob, hashlib, json, os, re, shutil, struct, subprocess, sys, uuid, zlib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config

STUDIO_VERSION = "2.02.27"
SPEC_FILE = "sounds.json"
SRC_SUB = "Sounds/src"
DEFAULT_LIKE = "event:/mod/ui"              # a 2D one-shot on the UI bus: TaleWorlds' own template for a modder's UI sound
DISTANCE_2D = "100000"                      # Native's max_distance for events that are not placed in the world
STREAM_SEC = 20.0                           # longer files stream from disk instead of loading
NS = uuid.UUID("0d5c61b2-47e1-4f3c-9a6e-5b1b2f7c8e40")   # namespace of every generated GUID
MASTER_EVENT_FOLDER = "{5be898cb-8569-49d8-9fb9-8dfff6905d48}"
MASTER_BANK_FOLDER = "{7556c034-3ca3-48e4-90c8-43d82096a832}"
MASTER_ASSET_FOLDER = "{b6479c9e-7ccd-4767-8111-a7d2a8925708}"
SAMPLE_SOUND_BANK = "{7d4941b2-09af-4406-8ebb-1cd2d87d250e}"   # the sample's ModuleSoundBank: shared by every mod built from it
# sample metadata folders copied as they are (mixer, presets, platform); events, audio, profiler data are not
COPY_DIRS = ("BankFolder", "EffectPreset", "EffectPresetFolder", "EncodingSetting", "Group", "MixerView",
             "ParameterPreset", "ParameterPresetFolder", "Platform", "Port", "ProfilerFolder", "Return",
             "SandboxFolder", "Snapshot", "SnapshotGroup", "VCA")
COPY_FILES = ("Master.xml", "Mixer.xml", "Tags.xml")
# pitch/volume randomisation: TaleWorlds' own amounts on the multi instruments of these kinds of events
RANDOMISE = ("event:/mission/combat/", "event:/mission/movement/footsteps/", "event:/physics/", "event:/mission/siege/")
EVENT_PATH = re.compile(r"event:/[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*")
ROW_KEYS = ("guid", "path", "length", "max_distance", "priority_multiplier", "bassgid", "parameter_indices", "send_is_player", "send_occlusion")
FORMAT = (0x8E, 0x8C)                       # bank format version / compatible version of FMOD 2.02 (every Native bank)
SETUP_CRC = {1: 0x571C7954, 2: 0xBE82E3B1}  # the two Vorbis setups of Native's banks, mono and stereo


class SoundError(Exception):
    """Something the caller can fix; bl turns it into an error answer."""
    def __init__(self, message, hint=None):
        Exception.__init__(self, message); self.hint = hint


def gid(*parts):
    return "{%s}" % uuid.uuid5(NS, "|".join(parts))


# ------------------------------------------------------------------------------------------------ the tools
def find_studio():
    """fmodstudiocl.exe of FMOD Studio 2.02.27: FMOD_STUDIO_CL names it, else the installer's folder. None when absent."""
    cands = [os.environ.get("FMOD_STUDIO_CL")]
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"):
        if root: cands.append(os.path.join(root, "FMOD SoundSystem", "FMOD Studio " + STUDIO_VERSION, "fmodstudiocl.exe"))
    return next((c for c in cands if c and os.path.isfile(c)), None)


def find_vgmstream():
    """vgmstream-cli (decodes the samples of a bank for the check): BL_VGMSTREAM, else the PATH. None when absent."""
    c = os.environ.get("BL_VGMSTREAM")
    if c and os.path.isfile(c): return c
    return shutil.which("vgmstream-cli")


def sample_project():
    """The Modding Kit's FMOD sample project (the folder with Metadata/), or None."""
    p = os.path.join(config.GAME, "modding_resources", "FMOD Sample Project", "FMODModdingProjectExample")
    return p if os.path.isdir(os.path.join(p, "Metadata")) else None


def native_bank_dir():
    return os.path.join(config.NATIVE, "Sounds", "PC")


def native_events():
    """event path -> attribute dict, in file order, from Native's sound_event_data.gen.xml ({} without a game)."""
    p = os.path.join(config.NATIVE_DATA, "sound_event_data.gen.xml")
    if not os.path.isfile(p): return {}
    return {e.get("path"): dict(e.attrib) for e in ET.parse(p).getroot().iter("event")}


# ------------------------------------------------------------------------------------------------ WAV files
def _wav_chunks(b):
    if b[:4] != b"RIFF" or b[8:12] != b"WAVE": raise SoundError("not a WAV file (no RIFF/WAVE header)")
    p = 12
    while p + 8 <= len(b):
        cid, sz = b[p:p + 4], struct.unpack_from("<I", b, p + 4)[0]
        yield cid, p + 8, min(sz, len(b) - p - 8)
        p += 8 + sz + (sz & 1)


def wav_header(b):
    """(format tag, channels, rate, bits, data offset, data size) of WAV bytes. Tags 1 (PCM), 3 (float) and the extensible
    form of both are read; anything else is refused."""
    fmt = data = None
    for cid, off, sz in _wav_chunks(b):
        if cid == b"fmt " and fmt is None: fmt = struct.unpack_from("<HHIIHH", b, off)[0:6] + (b[off:off + sz],)
        elif cid == b"data" and data is None: data = (off, sz)
    if fmt is None or data is None: raise SoundError("WAV without a fmt or data chunk")
    tag, ch, rate, _, _, bits, raw = fmt
    if tag == 0xFFFE and len(raw) >= 26: tag = struct.unpack_from("<H", raw, 24)[0]
    if tag not in (1, 3): raise SoundError("WAV format tag %d: only PCM and float are read" % tag)
    if tag == 1 and bits not in (8, 16, 24, 32) or tag == 3 and bits not in (32, 64): raise SoundError("WAV with %d bits per sample" % bits)
    return tag, ch, rate, bits, data[0], data[1]


def wav_info(path):
    with open(path, "rb") as f: b = f.read()
    tag, ch, rate, bits, off, size = wav_header(b)
    frames = size // (ch * bits // 8) if ch else 0
    return {"frames": frames, "rate": rate, "channels": ch, "bits": bits, "float": tag == 3, "seconds": frames / float(rate) if rate else 0.0}


def read_wav(path):
    """(float32 samples shaped [frames, channels], rate)."""
    with open(path, "rb") as f: b = f.read()
    tag, ch, rate, bits, off, size = wav_header(b)
    n = size // (ch * bits // 8) * ch
    raw = b[off:off + n * bits // 8]
    if tag == 3: a = np.frombuffer(raw, "<f%d" % (bits // 8)).astype(np.float32)
    elif bits == 8: a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
    elif bits == 16: a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        u = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = u[:, 0] | (u[:, 1] << 8) | (u[:, 2] << 16)
        a = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / 8388608.0
    else: a = np.frombuffer(raw, "<i4").astype(np.float32) / 2147483648.0
    return a.reshape(-1, ch), rate


def check_wav(path):
    """The WAV's facts, or SoundError. FMOD Studio encodes mono and stereo to the bank's 48 kHz Vorbis."""
    if not os.path.isfile(path): raise SoundError("no file " + path)
    try: i = wav_info(path)
    except (SoundError, struct.error) as e: raise SoundError("%s: %s" % (path, e))
    if i["channels"] not in (1, 2): raise SoundError("%s has %d channels: mono or stereo only" % (path, i["channels"]))
    if i["frames"] < 1: raise SoundError("%s holds no audio" % path)
    return i


# ------------------------------------------------------------------------------------------------ the project's events
def spec_path(project): return os.path.join(project, SPEC_FILE)


def load_spec(project):
    p = spec_path(project)
    if not os.path.isfile(p): return {"events": {}}
    with open(p, encoding="utf-8") as f: s = json.load(f)
    s.setdefault("events", {})
    return s


def save_spec(project, spec):
    with open(spec_path(project), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"events": dict(sorted(spec["events"].items()))}, f, indent=2); f.write("\n")


def event_dir(path): return path[len("event:/"):]


def check_path(path):
    if not EVENT_PATH.fullmatch(path or ""):
        raise SoundError("%r is not an event path" % path, "an event path is event:/ then folders and a name of letters, digits, _ . -  (for example event:/mymod/ui/click)")


def like_hint(like, native):
    near = difflib.get_close_matches(like, list(native), n=5, cutoff=0.6)
    return "`bl sound.list --native <text>` finds Native event paths" + (" (near: %s)" % ", ".join(near) if near else "")


def add_event(project, path, wavs, like=None, loop=None, dry=False):
    """Records an event in the project: copies the WAV files to Sounds/src/<event path>/ (the event's folder is replaced) and
    writes the entry to sounds.json. Returns the entry. An event that is already recorded is replaced."""
    check_path(path)
    if not wavs: raise SoundError("an event needs at least one WAV file")
    infos = [check_wav(w) for w in wavs]
    native = native_events()
    if like is not None:
        check_path(like)
        if native and like not in native: raise SoundError("%s is not a Native event" % like, like_hint(like, native))
    dest = os.path.join(project, SRC_SUB, *event_dir(path).split("/"))
    names, seen = [], set()
    for w in wavs:                                   # plain file names (FMOD Studio's project files are XML); the same name twice would overwrite itself: a number goes in front
        n = re.sub(r"[^A-Za-z0-9_.\-]", "_", os.path.basename(w))
        if n.lower() in seen: n = "%02d_%s" % (len(names), n)
        seen.add(n.lower()); names.append(n)
    rel = [(SRC_SUB + "/" + event_dir(path) + "/" + n) for n in names]
    entry = {"wavs": rel}
    if like: entry["like"] = like
    if loop is not None: entry["loop"] = bool(loop)
    replaces = path in native
    if dry: return {"entry": entry, "replaces_native": replaces, "files": [dict(i, file=n) for i, n in zip(infos, names)]}
    if os.path.isdir(dest): shutil.rmtree(dest)
    os.makedirs(dest)
    for w, n in zip(wavs, names): shutil.copyfile(w, os.path.join(dest, n))
    spec = load_spec(project); spec["events"][path] = entry; save_spec(project, spec)
    return {"entry": entry, "replaces_native": replaces, "files": [dict(i, file=n) for i, n in zip(infos, names)]}


def remove_event(project, path, dry=False):
    spec = load_spec(project)
    if path not in spec["events"]: raise SoundError("%s is not in %s" % (path, SPEC_FILE), "`bl sound.list` shows the events")
    if dry: return {"would_remove": path}
    del spec["events"][path]; save_spec(project, spec)
    d = os.path.join(project, SRC_SUB, *event_dir(path).split("/"))
    if os.path.isdir(d): shutil.rmtree(d)
    return {"removed": path}


def event_files(project, entry):
    out = []
    for w in entry["wavs"]:
        p = w if os.path.isabs(w) else os.path.join(project, *w.split("/"))
        out.append(p)
    return out


# ------------------------------------------------------------------------------------------------ FMOD metadata model
class Obj:
    """One object of an FMOD Studio project file: class, id, properties and relationships."""
    __slots__ = ("cls", "id", "props", "rels")

    def __init__(self, cls, id_, props=None, rels=None):
        self.cls, self.id = cls, id_
        self.props = props if props is not None else []   # [(name, [values])]
        self.rels = rels if rels is not None else []      # [(name, [ids])]

    def prop(self, name, default=None):
        for n, v in self.props:
            if n == name: return v[0] if v else default
        return default

    def rel(self, name):
        for n, v in self.rels:
            if n == name: return v
        return []

    def set_prop(self, name, value):
        value = [value] if not isinstance(value, list) else value
        for i, (n, _) in enumerate(self.props):
            if n == name: self.props[i] = (name, value); return
        self.props.append((name, value))

    def set_rel(self, name, ids):
        for i, (n, _) in enumerate(self.rels):
            if n == name:
                if ids: self.rels[i] = (name, ids)
                else: del self.rels[i]
                return
        if ids: self.rels.append((name, ids))


def read_objs(path):
    out = []
    for o in ET.parse(path).getroot().findall("object"):
        props = [(p.get("name"), [v.text or "" for v in p.findall("value")]) for p in o.findall("property")]
        rels = [(r.get("name"), [d.text for d in r.findall("destination")]) for r in o.findall("relationship")]
        out.append(Obj(o.get("class"), o.get("id"), props, rels))
    return out


def fmt(v):
    if isinstance(v, bool): return "true" if v else "false"
    if isinstance(v, float): return repr(round(v, 6))
    return str(v)


def write_objs(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = ['<?xml version="1.0" encoding="UTF-8"?>', '<objects serializationModel="Studio.02.02.00">']
    for o in objs:
        if not o.props and not o.rels:
            L.append('\t<object class="%s" id="%s" />' % (o.cls, o.id)); continue
        L.append('\t<object class="%s" id="%s">' % (o.cls, o.id))
        for n, vals in o.props:
            L.append('\t\t<property name="%s">' % n)
            for v in vals: L.append("\t\t\t<value>%s</value>" % escape(fmt(v)))
            L.append("\t\t</property>")
        for n, ids in o.rels:
            if not ids: continue
            L.append('\t\t<relationship name="%s">' % n)
            for d in ids: L.append("\t\t\t<destination>%s</destination>" % d)
            L.append("\t\t</relationship>")
        L.append("\t</object>")
    L.append("</objects>")
    with open(path, "w", encoding="utf-8", newline="\n") as f: f.write("\n".join(L) + "\n")


class Sample:
    """The sample project's events (the templates), parameter presets and effect presets."""

    def __init__(self, root):
        meta = os.path.join(root, "Metadata")
        self.root = root
        self.folders = {}
        for f in glob.glob(os.path.join(meta, "EventFolder", "*.xml")):
            for o in read_objs(f): self.folders[o.id] = o
        self.events = {}                                   # path -> (Event id, {id: Obj})
        for f in glob.glob(os.path.join(meta, "Event", "*.xml")):
            objs = read_objs(f); local = {o.id: o for o in objs}
            ev = objs[0]
            parent = ev.rel("folder")[0] if ev.rel("folder") else None
            if parent in local or parent not in self.folders: continue   # nested event inside another event
            self.events[self.path_of(ev)] = (ev.id, local)
        self.param_by_name = {}                            # preset name -> GameParameter id
        for f in glob.glob(os.path.join(meta, "ParameterPreset", "*.xml")):
            objs = read_objs(f)
            pre = [o for o in objs if o.cls == "ParameterPreset"][0]
            self.param_by_name[pre.prop("name")] = pre.rel("parameter")[0]
        self.spatial_presets = set()                       # effect presets that hold a spatializer
        for f in glob.glob(os.path.join(meta, "EffectPreset", "*.xml")):
            objs = read_objs(f)
            if any(o.cls in ("SpatialiserEffect", "ObjectSpatialiserEffect") for o in objs):
                self.spatial_presets.add(objs[0].id)

    def path_of(self, ev):
        parts = [ev.prop("name")]; f = ev.rel("folder")[0]
        while f in self.folders and self.folders[f].cls != "MasterEventFolder":
            parts.append(self.folders[f].prop("name")); f = self.folders[f].rel("folder")[0]
        return "event:/" + "/".join(reversed(parts))

    def template_for(self, path):
        """(sample event path, common prefix depth). An event of the sample itself is its own template. Otherwise the
        event with the longest common path prefix, a same leaf name breaking ties; helper events (reverb, names that
        start with _), music and the sample's mod/, Extra/ and video events are never chosen by similarity."""
        if path in self.events: return path, len(path.split("/")) - 1
        want = path[len("event:/"):].split("/")
        best, key = None, None
        for p in self.events:
            segs = p[len("event:/"):].split("/")
            if segs[0] in ("mod", "Extra", "video") or segs == ["music"] or "reverb" in segs or segs[-1].startswith("_"):
                continue
            n = 0
            while n < min(len(segs), len(want)) and segs[n] == want[n]: n += 1
            k = (n, segs[-1] == want[-1], -abs(len(segs) - len(want)), p)
            if key is None or k[:3] > key[:3] or (k[:3] == key[:3] and p < key[3]): best, key = p, k
        return best, key[0]


# DROP_RELS: relationships that are rebuilt, external, or point at content that is replaced
DROP_RELS = {"groupTracks", "returnTracks", "modules", "markers", "automationTracks", "folder", "banks", "items",
             "tags", "references", "sandboxEmitters", "defaultEvent", "clonedEvents", "selectables", "commandSounds",
             "profilerTrackers", "profilerGraphs", "uiLastParameterSelection", "eventSelector", "selector"}


def clone_event(module, path, tmpl_local, tmpl_id, sample, param_names, wav_info_, audio_ids, folder_id, bank_id, loop, randomise):
    """New Event objects for path: the template's mixing, one multi instrument with the files. Returns
    (objects, event id, length, parameters the sample has no preset for, whether the clone has a spatializer)."""
    # keep set: everything the event owns except tracks, instruments and markers
    keep, todo = set(), [tmpl_id]
    while todo:
        i = todo.pop()
        if i in keep or i not in tmpl_local: continue
        keep.add(i); o = tmpl_local[i]
        for n, ids in o.rels:
            if n in DROP_RELS: continue
            if n == "automationCurves" and o.cls != "Automator": continue
            todo.extend(d for d in ids if d in tmpl_local)
    # sends to returns inside dropped tracks, and automation tracks whose automator went, are dropped too
    changed = True
    while changed:
        changed = False
        for i in list(keep):
            o = tmpl_local[i]
            if o.cls == "MixerSend" and any(d in tmpl_local and d not in keep for d in o.rel("mixerReturn")):
                keep.discard(i); changed = True
    for i in list(keep):
        o = tmpl_local[i]
        if o.cls == "MasterTrack":
            for t in o.rel("automationTracks"):
                if t in tmpl_local and all(a in keep for a in tmpl_local[t].rel("automator")): keep.add(t)
    # action sheets replace the timeline (the template's timeline is then disabled): ours plays from the timeline
    for i in list(keep):
        if tmpl_local[i].cls == "ActionSheet": keep.discard(i)
    remap = {i: gid(module, path, i) for i in keep}
    remap[tmpl_id] = gid(module, "event", path)
    objs = []
    for i in [tmpl_id] + sorted(keep - {tmpl_id}):
        o = tmpl_local[i]
        n = Obj(o.cls, remap[i], [(a, list(b)) for a, b in o.props], [])
        for rn, ids in o.rels:
            if o.cls == "Event" and rn in ("folder", "banks"): continue
            new = []
            for d in ids:
                if d in tmpl_local:
                    if d in remap: new.append(remap[d])
                else:
                    if rn in DROP_RELS: continue
                    new.append(d)              # external: bus, preset, return, parameter preset
            n.set_rel(rn, new)
        objs.append(n)
    by = {o.id: o for o in objs}
    ev = by[remap[tmpl_id]]
    ev.set_prop("name", path.rsplit("/", 1)[1])
    ev.set_rel("folder", [folder_id]); ev.set_rel("banks", [bank_id])
    ev.props = [(a, b) for a, b in ev.props if a not in ("note", "color")]

    # parameters the engine sets on this event (parameter_indices) must exist on it
    have = set()
    for p in ev.rel("parameters"):
        po = by.get(p)
        if po is not None and po.cls == "ParameterProxy": have.update(po.rel("preset"))
    missing = []
    for name in param_names:
        g = sample.param_by_name.get(name)
        if g is None: missing.append(name); continue
        if g in have: continue
        proxy = Obj("ParameterProxy", gid(module, path, "param", name), [], [("preset", [g])])
        objs.append(proxy); ev.set_rel("parameters", ev.rel("parameters") + [proxy.id]); have.add(g)

    # the instrument
    length = max(w["len"] for w in wav_info_)
    multi = Obj("MultiSound", gid(module, path, "multi"), [("start", ["0"]), ("length", [fmt(round(length, 6))]), ("name", [module])], [])
    if loop: multi.set_prop("looping", "true")
    singles = [Obj("SingleSound", gid(module, path, "single", w["asset"]), [], [("audioFile", [audio_ids[w["asset"]]])]) for w in wav_info_]
    multi.set_rel("sounds", [s.id for s in singles])
    mods = []
    if randomise:
        for prop, amount in (("pitch", "12.5"), ("volume", "4")):
            mods.append(Obj("RandomizerModulator", gid(module, path, "rand", prop), [("nameOfPropertyBeingModulated", [prop]), ("amount", [amount])], []))
        multi.set_rel("modulators", [m.id for m in mods])
    objs += [multi] + singles + mods
    master = [o for o in objs if o.cls == "MasterTrack"][0]
    timeline = [o for o in objs if o.cls == "Timeline"][0]
    master.set_rel("modules", [multi.id]); timeline.set_rel("modules", [multi.id])
    timeline.props = [(n, v) for n, v in timeline.props if n != "isProxyEnabled"]
    if loop:
        mt = [o for o in objs if o.cls == "MarkerTrack"][0]
        lr = Obj("LoopRegion", gid(module, path, "loop"), [("position", ["0"]), ("length", [fmt(round(length, 6))])],
                 [("timeline", [timeline.id]), ("markerTrack", [mt.id])])
        objs.append(lr); timeline.set_rel("markers", [lr.id])

    # 3D check: Native plays an event in 3D when max_distance < 100000
    spatial = any(o.cls in ("SpatialiserEffect", "ObjectSpatialiserEffect") for o in objs) or \
        any(o.cls == "ProxyEffect" and set(o.rel("preset")) & sample.spatial_presets for o in objs)
    return objs, ev.id, length, missing, spatial


# ------------------------------------------------------------------------------------------------ the FMOD project
def link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst): os.remove(dst)
    try: os.link(src, dst)
    except OSError: shutil.copy2(src, dst)


def work_dir(module): return os.path.join(config.SDK_HOME, "sound", module)


def resolve_events(project, spec, native):
    """The spec's events with everything the build needs decided: files, the Native event the attributes come from, the
    one the mixing comes from. Raises SoundError for a missing file or a `like` that is not a Native event."""
    out = []
    for path, e in sorted(spec["events"].items()):
        check_path(path)
        files = event_files(project, e); infos = [check_wav(f) for f in files]
        if len({os.path.basename(f).lower() for f in files}) != len(files): raise SoundError("%s: two of its files have the same name" % path, "`bl sound.add` renames copies that clash")
        like = e.get("like")
        if like is not None and like not in native: raise SoundError("%s: like %s is not a Native event" % (path, like), like_hint(like, native))
        replaces = path in native
        attrs_ref = path if replaces else (like or DEFAULT_LIKE)     # a replaced event keeps its own parameters: the engine sets them by index
        mix_ref = like or attrs_ref
        if attrs_ref not in native: raise SoundError("%s is not a Native event: no default to take the attributes from" % attrs_ref)
        loop = bool(e["loop"]) if e.get("loop") is not None else native[mix_ref].get("length") == "-1"
        out.append({"path": path, "files": files, "infos": infos, "replaces": replaces, "attrs_ref": attrs_ref, "mix_ref": mix_ref, "loop": loop})
    return out


def generate(module, events, native, sample, work, build_dir):
    """Writes the FMOD project into work/project and returns (manifest rows, notes, dangling references dropped)."""
    proj = os.path.join(work, "project"); meta = os.path.join(proj, "Metadata"); assets = os.path.join(proj, "Assets")
    for d in (meta, assets):
        if os.path.isdir(d): shutil.rmtree(d)
    smeta = os.path.join(sample.root, "Metadata")
    for d in COPY_DIRS:
        if os.path.isdir(os.path.join(smeta, d)): shutil.copytree(os.path.join(smeta, d), os.path.join(meta, d))
    for f in COPY_FILES: shutil.copy2(os.path.join(smeta, f), os.path.join(meta, f))
    for sub, fid in (("EventFolder", MASTER_EVENT_FOLDER), ("Asset", MASTER_ASSET_FOLDER)):
        os.makedirs(os.path.join(meta, sub), exist_ok=True)
        shutil.copy2(os.path.join(smeta, sub, fid + ".xml"), os.path.join(meta, sub, fid + ".xml"))
    # banks: the sample's master bank (needed to build, never shipped) and ours with its own GUID
    os.makedirs(os.path.join(meta, "Bank"))
    for f in glob.glob(os.path.join(smeta, "Bank", "*.xml")):
        if SAMPLE_SOUND_BANK not in f: shutil.copy2(f, os.path.join(meta, "Bank"))
    bank_id = gid(module, "bank")
    write_objs(os.path.join(meta, "Bank", bank_id + ".xml"), [Obj("Bank", bank_id, [("name", [module])], [("folder", [MASTER_BANK_FOLDER])])])
    ws = read_objs(os.path.join(smeta, "Workspace.xml"))      # workspace: the build goes to our folder
    ws[0].set_prop("builtBanksOutputDirectory", build_dir.replace("\\", "/"))
    write_objs(os.path.join(meta, "Workspace.xml"), ws)
    with open(os.path.join(proj, module + ".fspro"), "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<objects serializationModel="Studio.02.02.00" />\n')

    folders = {}
    def folder(segs):
        key = "/".join(segs)
        if key in folders: return folders[key]
        parent = folder(segs[:-1]) if len(segs) > 1 else MASTER_EVENT_FOLDER
        fid = gid(module, "folder", key); folders[key] = fid
        write_objs(os.path.join(meta, "EventFolder", fid + ".xml"), [Obj("EventFolder", fid, [("name", [segs[-1]])], [("folder", [parent])])])
        return fid

    manifest, notes = [], []
    for ev in events:
        path = ev["path"]; rel = event_dir(path)
        attrs = native[ev["attrs_ref"]]
        tpath, depth = sample.template_for(ev["mix_ref"])
        tid, tlocal = sample.events[tpath]
        wav_info_, audio_ids, audio_objs = [], {}, []
        for w, i in zip(ev["files"], ev["infos"]):
            asset = rel + "/" + os.path.basename(w)
            link(w, os.path.join(assets, *asset.split("/")))
            m = {"len": i["seconds"], "ch": i["channels"], "sr": i["rate"], "asset": asset}; wav_info_.append(m)
            aid = gid(module, "audio", asset); audio_ids[asset] = aid
            a = Obj("AudioFile", aid, [("assetPath", [asset]), ("frequencyInKHz", [fmt(m["sr"] / 1000.0)]), ("channelCount", [str(m["ch"])]),
                                       ("length", [fmt(round(m["len"], 6))])], [("masterAssetFolder", [MASTER_ASSET_FOLDER])])
            if m["len"] > STREAM_SEC: a.set_prop("isStreaming", "true")
            audio_objs.append(a)
        top = [module.lower()] + rel.split("/")[:-1]
        fid = folder(top)
        params = [x for x in attrs.get("parameter_indices", "").split(",") if x]
        objs, eid, length, missing, spatial = clone_event(module, path, tlocal, tid, sample, params, wav_info_, audio_ids, fid, bank_id, ev["loop"],
                                                          ev["mix_ref"].startswith(RANDOMISE))
        write_objs(os.path.join(meta, "Event", eid + ".xml"), objs)
        for a in audio_objs: write_objs(os.path.join(meta, "AudioFile", a.id + ".xml"), [a])
        is3d = attrs.get("max_distance", DISTANCE_2D) != DISTANCE_2D
        if missing: notes.append("%s: the sample project has no parameter preset for %s: the game sets them by name and finds nothing" % (path, ", ".join(missing)))
        if is3d != spatial:
            notes.append("%s: its attributes are %s but the mixing template %s is %s: `like` names the event both come from, pick one that is %s" % (
                path, "3D" if is3d else "2D", tpath, "3D" if spatial else "2D", "3D" if is3d else "2D"))
        row = dict(attrs); row.update(guid=eid, path=path, length="-1" if ev["loop"] else str(round(length, 4)))
        manifest.append({"path": path, "guid": eid, "replaces_native": ev["replaces"], "attributes_from": ev["attrs_ref"], "mixing_from": ev["mix_ref"],
                         "template": tpath, "template_depth": depth, "loop": ev["loop"], "length": -1 if ev["loop"] else round(length, 4),
                         "files": [w["asset"] for w in wav_info_], "row": {k: row[k] for k in ROW_KEYS if k in row}})
    # references to objects the project does not have (sample objects that were left out) are dropped
    defined, files = set(), glob.glob(os.path.join(meta, "**", "*.xml"), recursive=True)
    cache = {f: read_objs(f) for f in files}
    for objs in cache.values(): defined.update(o.id for o in objs)
    dangling = 0
    for f, objs in cache.items():
        dirty = False
        for o in objs:
            for n, ids in list(o.rels):
                keep_ids = [d for d in ids if d in defined]
                if len(keep_ids) != len(ids): o.set_rel(n, keep_ids); dangling += len(ids) - len(keep_ids); dirty = True
        if dirty: write_objs(f, objs)
    return manifest, notes, dangling


def run_studio(studio, fspro, module, log):
    p = subprocess.run([studio, "-build", "-banks", module, "-platforms", "Desktop", fspro], capture_output=True, text=True, timeout=3600)
    out = p.stdout + "\n" + p.stderr
    with open(log, "w", encoding="utf-8") as f: f.write(out)
    return p.returncode, out


# ------------------------------------------------------------------------------------------------ the ModuleData files
def soundfiles_xml(module):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<!-- %s: banks in Sounds/PC, loaded after Native\'s. Written by bl sound.build. -->\n'
            '<base xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" type="sound">\n'
            '  <asset_files>\n    <file name="%s.bank" decompress_samples="false" />\n  </asset_files>\n</base>\n' % (module, module))


def event_data_name(module): return "%s_sound_event_data.xml" % module.lower()


def event_data_xml(rows):
    """The module's rows for the engine's sound loader: it reads this file as raw XML, overwrites a Native path with the row
    and appends a new path (docs/AGENTS.md "Sound")."""
    L = ['<?xml version="1.0" encoding="utf-8"?>', '<base type="sound_event_data">', '\t<events>']
    for r in rows:
        L.append("\t\t<event " + " ".join("%s=%s" % (k, quoteattr(str(r[k]))) for k in ROW_KEYS if k in r) + " />")
    L += ["\t</events>", "</base>"]
    return "\n".join(L) + "\n"


MBPROJ_LINES = ('  <file id="soln_sound_files" name="ModuleData/soundfiles.xml" type="sound_files" />',
                '  <file id="soln_sound_event_data" name="ModuleData/%s" type="sound_event_data" />')
MBPROJ_TAG = re.compile(r'id="(soln_sound_files|soln_sound_event_data)"')
MBPROJ_NEW = ('<?xml version="1.0" encoding="utf-8"?>\n<!-- %s: engine-side XML (merged with Native\'s by the engine). -->\n'
              '<base xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" type="solution">\n'
              '  <outputDirectory>../MBModule/MBModule/</outputDirectory>\n  <XMLDirectory>../WOTS/Modules/%s/</XMLDirectory>\n'
              '  <ModuleAssemblyDirectory>../WOTS/bin/</ModuleAssemblyDirectory>\n</base>\n')


def mbproj_text(module, base=None):
    """project.mbproj with the module's two sound entries: `base` (an existing file's text) keeps its other entries, else a
    new file. The entries are written last, before </base>."""
    lines = (base if base is not None else MBPROJ_NEW % (module, module)).splitlines()
    lines = [l for l in lines if not MBPROJ_TAG.search(l)]
    add = [MBPROJ_LINES[0], MBPROJ_LINES[1] % event_data_name(module)]
    out = []
    for l in lines:
        if re.match(r"\s*</base>", l): out += add
        out.append(l)
    if not any(l in add for l in out): raise SoundError("the project.mbproj to extend has no </base>")
    return "\n".join(out) + "\n"


def existing_mbproj(project, stage_dir):
    """The text the stage's project.mbproj extends: the one already staged, else the project's own ModuleData one, else the
    module's installed one, else None."""
    for p in (os.path.join(stage_dir, "ModuleData", "project.mbproj"), os.path.join(project, "ModuleData", "project.mbproj"),
              os.path.join(config.MODULE_DATA, "project.mbproj")):
        if os.path.isfile(p):
            with open(p, encoding="utf-8-sig") as f: return f.read(), p
    return None, None


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f: f.write(text)


# ------------------------------------------------------------------------------------------------ banks
def riff_chunks(d, off=0, end=None, path=()):
    """Yields (id, header offset, body offset, size, enclosing LIST types) for every chunk; LIST/RIFF are yielded too."""
    end = len(d) if end is None else end
    while off + 8 <= end:
        cid = bytes(d[off:off + 4]); sz = struct.unpack_from("<I", d, off + 4)[0]; body = off + 8
        yield cid, off, body, sz, path
        if cid in (b"RIFF", b"LIST"):
            yield from riff_chunks(d, body + 4, body + sz, path + (bytes(d[body:body + 4]),))
        off = body + sz + (sz & 1)


def bank_layout(d):
    """Format versions, and the offsets of the SNDH and SND chunks and of the FSB5 they hold."""
    if bytes(d[:4]) != b"RIFF" or bytes(d[8:12]) != b"FEV ": raise SoundError("not an FMOD bank (no RIFF/FEV header)")
    fmt_at = bytes(d).find(b"FMT ")
    if fmt_at < 0: raise SoundError("bank without an FMT chunk")
    ver, compat = struct.unpack_from("<II", d, fmt_at + 8)
    lay = dict(format_version=ver, compat_version=compat)
    for cid, off, body, sz, path in riff_chunks(d):
        if cid == b"SNDH": lay["sndh"] = body; lay["sndh_vals"] = struct.unpack_from("<III", d, body)
        if cid == b"SND ": lay["snd"] = off; lay["snd_size"] = sz
    if "sndh_vals" in lay: lay["fsb_off"], lay["fsb_size"] = lay["sndh_vals"][1], lay["sndh_vals"][2]
    return lay


def bank_format(path):
    """(format version, compatible version) from the head of a bank file; a large bank is not read whole."""
    with open(path, "rb") as f: head = f.read(1 << 16)
    if head[:4] != b"RIFF" or head[8:12] != b"FEV ": raise SoundError("not an FMOD bank (no RIFF/FEV header)")
    i = head.find(b"FMT ")
    if i < 0: raise SoundError("bank without an FMT chunk near its start")
    return struct.unpack_from("<II", head, i + 8)


FREQ = {1: 8000, 2: 11000, 3: 11025, 4: 16000, 5: 22050, 6: 24000, 7: 32000, 8: 44100, 9: 48000}
CH_CHANNELS, CH_FREQ, CH_VORBIS = 1, 2, 11


def parse_fsb(d, j):
    """The FSB5 at offset j: header fields and one dict per sample (rate, channels, sample count, chunks, name, data view)."""
    if bytes(d[j:j + 4]) != b"FSB5": raise SoundError("no FSB5 at %#x" % j)
    ver, ns, shs, nts, ds, mode = struct.unpack_from("<6I", d, j + 4)
    hdr = 0x3C if ver == 1 else 0x40
    p = j + hdr; subs = []
    for k in range(ns):
        v = struct.unpack_from("<Q", d, p)[0]; p += 8
        s = dict(idx=k, rate=FREQ.get((v >> 1) & 0xF), ch=[1, 2, 6, 8][(v >> 5) & 3], off=((v >> 7) & 0x7FFFFFF) << 5, samples=(v >> 34) & 0x3FFFFFFF, chunks=[])
        more = v & 1
        while more:
            c = struct.unpack_from("<I", d, p)[0]; p += 4
            more = c & 1; sz = (c >> 1) & 0xFFFFFF; t = (c >> 25) & 0x7F
            s["chunks"].append((t, bytes(d[p:p + sz]))); p += sz
            if t == CH_CHANNELS: s["ch"] = d[p - sz]
            if t == CH_FREQ: s["rate"] = struct.unpack_from("<I", d, p - sz)[0]
        subs.append(s)
    if p != j + hdr + shs: raise SoundError("FSB5 sample header size mismatch")
    nt = j + hdr + shs
    for s in subs:
        o = struct.unpack_from("<I", d, nt + 4 * s["idx"])[0]
        e = bytes(d[nt + o:nt + o + 512]).index(b"\0"); s["name"] = bytes(d[nt + o:nt + o + e]).decode("utf-8", "replace")
    data0 = nt + nts; mv = memoryview(d)
    for i, s in enumerate(subs):
        end = subs[i + 1]["off"] if i + 1 < ns else ds
        s["size"] = end - s["off"]; s["data"] = mv[data0 + s["off"]:data0 + end]
    return dict(off=j, ver=ver, n=ns, ds=ds, mode=mode, subs=subs, size=hdr + shs + nts + ds)


def vorbis_packets(data):
    """FSB Vorbis stream: [u16 size][packet] ... then zero padding. Returns (packets, bytes consumed)."""
    p = 0; out = []
    while p + 2 <= len(data):
        n = struct.unpack_from("<H", data, p)[0]
        if n == 0: break
        out.append(data[p + 2:p + 2 + n]); p += 2 + n
    return out, p


def packet_samples(packets, small=256, big=2048):
    """Decoded samples per packet for the two block sizes Bannerlord's setups use (bit 1 of the first byte: long block)."""
    out = []; prev = None
    for pk in packets:
        bs = big if (pk[0] >> 1) & 1 else small
        out.append(0 if prev is None else prev // 4 + bs // 4)
        prev = bs
    return out


CONTAINER_LISTS = {b"PROJ", b"IBSS", b"GBSS", b"RBSS", b"PRTS", b"MBSS", b"BEFX", b"PEFX", b"SEFX", b"SCFX", b"VCAS",
                   b"EVTS", b"TLNS", b"PMLS", b"PRMS", b"MODS", b"INSS", b"WAVS", b"CTRS", b"SNPS", b"TRSS", b"MAPS",
                   b"CRVS", b"PLTS", b"TRNS"}


def bank_objects(d):
    """Leaf-level LIST objects (id = first 16 bytes of their first leaf) and WAV chunks, as (type, start, end, id)."""
    out = []
    for cid, off, body, sz, path in riff_chunks(d):
        if cid == b"SND ": break
        if cid == b"LIST":
            typ = bytes(d[body:body + 4]); first = body + 4
            if bytes(d[first:first + 4]) not in (b"LIST", b"RIFF", b"LCNT") and typ not in CONTAINER_LISTS:
                if struct.unpack_from("<I", d, first + 4)[0] >= 16: out.append((typ, body + 4, body + sz, bytes(d[first + 8:first + 24])))
        elif cid == b"WAV " and sz >= 16:
            out.append((b"WAV ", off, body + sz, bytes(d[body:body + 16])))
    return out


def event_samples(d, guid_to_path):
    """event path -> sorted sample indices, following instruments, playlists and nested events from each event's GUID
    (the little-endian bytes of the GUID are the keys of guid_to_path)."""
    ids = {}
    for typ, s, e, g in bank_objects(d): ids.setdefault(g, (typ, s, e))
    keys = set(ids); edges = {}
    raw = bytes(d)
    for g, (typ, s, e) in ids.items():
        b = raw[s:e]
        for i in range(len(b) - 15):
            x = b[i:i + 16]
            if x in keys and x != g: edges.setdefault(g, set()).add(x)
    res = {}
    for g, p in guid_to_path.items():
        if g not in ids: continue
        seen = {g}; st = [g]; w = set()
        while st:
            x = st.pop()
            for y in edges.get(x, ()):
                if y in seen: continue
                seen.add(y)
                typ, s, e = ids[y]
                if typ == b"WAV ": w.add(struct.unpack_from("<I", raw, s + 8 + 22)[0])
                else: st.append(y)
        res[p] = sorted(w)
    return res


def best_corr(x, y, max_lag=2048, max_len=480000):
    """Largest normalised cross-correlation of two mono signals over lags up to max_lag (the first max_len samples)."""
    n = min(len(x), len(y), max_len)
    if n < 64: return 0.0
    x = x[:n] - x[:n].mean(); y = y[:n] - y[:n].mean()
    den = float(np.sqrt((x * x).sum() * (y * y).sum()))
    if den <= 0: return 0.0
    nfft = 1 << (2 * n - 1).bit_length()
    c = np.fft.irfft(np.fft.rfft(x, nfft) * np.conj(np.fft.rfft(y, nfft)), nfft)
    lag = min(max_lag, n - 1)
    return float(max(c[:lag + 1].max(), c[-lag:].max() if lag else 0.0) / den)


def decode_sample(vgm, bank, idx, wav):
    r = subprocess.run([vgm, "-i", "-s", str(idx + 1), "-o", wav, bank], capture_output=True)
    return r.returncode == 0 and os.path.exists(wav)


def mixer_objects(sample_root):
    """GUID -> description of the sample project's mixer objects (buses, returns, VCAs, ports, snapshots, global parameters)."""
    meta = os.path.join(sample_root, "Metadata"); out = {}
    for sub in ("Group", "Return", "VCA", "Port", "Snapshot"):
        for f in glob.glob(os.path.join(meta, sub, "*.xml")):
            for o in read_objs(f):
                if o.cls in ("MixerGroup", "MixerReturn", "MixerVCA", "MixerPort", "Snapshot"): out[o.id] = "%s %s" % (o.cls, o.prop("name"))
    for o in read_objs(os.path.join(meta, "Master.xml")): out[o.id] = "MixerMaster"
    for f in glob.glob(os.path.join(meta, "ParameterPreset", "*.xml")):
        for o in read_objs(f):
            if o.cls == "GameParameter" and o.prop("isGlobal") == "true": out[o.id] = "global parameter"
    return out


def check_bank(bank, events=None, sources=None, decode=0, native=True):
    """Offline checks of a built bank. events: [{"path", "guid", "files": n}] (the manifest); sources: path -> [WAV paths] for the
    decode comparison. Returns a dict with "ok", "problems" and the facts. Nothing is played."""
    with open(bank, "rb") as f: d = f.read()
    res = {"bank": bank, "bytes": len(d), "problems": [], "notes": []}
    prob = res["problems"].append
    lay = bank_layout(d)
    res["format"] = "0x%x/0x%x" % (lay["format_version"], lay["compat_version"])
    if (lay["format_version"], lay["compat_version"]) != FORMAT: prob("bank format %s: the game's FMOD 2.02 reads 0x8e/0x8c" % res["format"])
    nat_master = os.path.join(native_bank_dir(), "MasterBank.bank")
    nat = None
    if native and os.path.isfile(nat_master):
        with open(nat_master, "rb") as f: nat = f.read()
        nl = bank_layout(nat); res["native_format"] = "0x%x/0x%x" % (nl["format_version"], nl["compat_version"])
        if res["native_format"] != res["format"]: prob("bank format %s, Native's is %s" % (res["format"], res["native_format"]))
    # RIFF structure
    last = None
    for cid, off, body, sz, path in riff_chunks(d):
        if body + sz > len(d): prob("chunk %s at %#x runs past the end of the file" % (cid.decode("latin1"), off))
        last = body + sz
    if struct.unpack_from("<I", d, 4)[0] != len(d) - 8: prob("RIFF size does not match the file")
    if "fsb_off" not in lay:
        prob("no sample data (no SNDH/SND chunk)"); res["ok"] = False; return res
    fsb = parse_fsb(d, lay["fsb_off"])
    if fsb["size"] != lay["fsb_size"]: prob("SNDH says the FSB is %d bytes, it is %d" % (lay["fsb_size"], fsb["size"]))
    crc, seconds = {}, 0.0
    for s in fsb["subs"]:
        if s["off"] % 32: prob("sample %d is not 32-byte aligned" % s["idx"])
        pk, used = vorbis_packets(s["data"])
        if bytes(s["data"][used:]).strip(b"\0"): prob("sample %d: bytes after its last Vorbis packet" % s["idx"])
        total = sum(packet_samples(pk))
        if total < s["samples"]: prob("sample %d: its packets decode %d samples, the header says %d" % (s["idx"], total, s["samples"]))
        for t, b in s["chunks"]:
            if t == CH_VORBIS:
                c = struct.unpack_from("<I", b)[0]; crc["0x%08x" % c] = crc.get("0x%08x" % c, 0) + 1
                if c != SETUP_CRC.get(s["ch"]): res["notes"].append("sample %d has a Vorbis setup (%#010x) that Native's banks do not use" % (s["idx"], c))
        seconds += s["samples"] / float(s["rate"] or 48000)
    res.update(samples=fsb["n"], mono=sum(s["ch"] == 1 for s in fsb["subs"]), stereo=sum(s["ch"] == 2 for s in fsb["subs"]),
               seconds=round(seconds, 3), setups=crc, codec=fsb["mode"])
    # events: GUID present, and the samples it reaches
    if events:
        g2p = {uuid.UUID(e["guid"]).bytes_le: e["path"] for e in events}
        reach = event_samples(d, g2p); rows = []
        for e in events:
            got = reach.get(e["path"], []); present = uuid.UUID(e["guid"]).bytes_le in d
            ok = present and len(got) >= 1 and (e.get("files") is None or len(got) == e["files"] or len(got) < e["files"] and e.get("distinct") == len(got))
            rows.append({"path": e["path"], "guid_in_bank": present, "samples": [fsb["subs"][i]["name"] or i for i in got], "expected": e.get("files"), "ok": ok})
            if not present: prob("%s: its GUID is not in the bank" % e["path"])
            elif not ok: prob("%s reaches %d samples, it has %s files" % (e["path"], len(got), e.get("files")))
        res["events"] = rows
    # the mixer objects the bank routes into must exist in Native's MasterBank (nothing depends on the sample's own master bank)
    sp = sample_project()
    if nat is not None and sp:
        mix = mixer_objects(sp)
        used = {i: n for i, n in mix.items() if uuid.UUID(i).bytes_le in d}
        gone = {i: n for i, n in used.items() if uuid.UUID(i).bytes_le not in nat}
        res["mixer"] = {"referenced": len(used), "missing_in_native_masterbank": sorted(gone.values())}
        if gone: prob("%d mixer objects are not in Native's MasterBank: %s" % (len(gone), ", ".join(sorted(gone.values())[:5])))
    # decode a few samples and compare them with their source files
    if decode:
        vgm = find_vgmstream()
        if not vgm: res["decode"] = {"skipped": "vgmstream-cli not found (BL_VGMSTREAM or the PATH)"}
        elif not events or not sources: res["decode"] = {"skipped": "no events to compare with"}
        else:
            by_idx = {}
            for e in events:
                for i in event_samples(d, {uuid.UUID(e["guid"]).bytes_le: e["path"]}).get(e["path"], []): by_idx.setdefault(i, e["path"])
            picks = sorted(by_idx)[:decode]; tmp = bank + ".dec.wav"; rows = []
            for i in picks:
                if not decode_sample(vgm, bank, i, tmp): rows.append({"sample": i, "error": "vgmstream could not decode it"}); prob("sample %d does not decode" % i); continue
                x, sr = read_wav(tmp); x = x.mean(1); best = 0.0
                for src in sources.get(by_idx[i], []):
                    y, ysr = read_wav(src); y = y.mean(1)
                    if ysr != sr: y = np.interp(np.arange(int(len(y) * sr / ysr)) * ysr / sr, np.arange(len(y)), y)
                    best = max(best, best_corr(x, y))
                rows.append({"sample": i, "event": by_idx[i], "decoded_frames": len(x), "header_frames": fsb["subs"][i]["samples"], "correlation": round(best, 3)})
                if best < 0.9: prob("sample %d (%s) correlates %.3f with its source files" % (i, by_idx[i], best))
            if os.path.exists(tmp): os.remove(tmp)
            res["decode"] = {"tried": len(rows), "matched": sum(1 for r in rows if r.get("correlation", 0) >= 0.9), "samples": rows}
    elif decode == 0: res["decode"] = {"skipped": "not asked for"}
    res["ok"] = not res["problems"]
    return res


# ------------------------------------------------------------------------------------------------ build
def requirements(need_studio=True):
    """Everything a build needs, as (problems, found paths)."""
    problems, found = [], {}
    if not config.BANNERLORD_DIR or not os.path.isdir(config.NATIVE): problems.append("the game install was not found (BANNERLORD_DIR)")
    sp = sample_project(); found["sample_project"] = sp
    if not sp: problems.append("the Modding Kit's FMOD sample project is missing: expected <game>\\modding_resources\\FMOD Sample Project\\FMODModdingProjectExample (it comes with the game)")
    st = find_studio(); found["fmod_studio"] = st
    if need_studio and not st:
        problems.append("FMOD Studio %s was not found: install exactly that version from fmod.com (Download, FMOD Studio, older versions), or point FMOD_STUDIO_CL at its fmodstudiocl.exe" % STUDIO_VERSION)
    return problems, found


def build(project, module, stage="work", dry=False, decode=4):
    """Builds the bank for the project's events and stages the module's sound files. Returns a result dict."""
    spec = load_spec(project)
    if not spec["events"]: raise SoundError("the project has no sound events", "add one with `bl sound.add <event path> <wav files>`")
    problems, found = requirements(need_studio=not dry)
    if problems: raise SoundError(problems[0], "; ".join(problems[1:]) or None)
    native = native_events()
    if not native: raise SoundError("Native's sound_event_data.gen.xml was not found under %s" % config.NATIVE_DATA)
    events = resolve_events(project, spec, native)
    work = work_dir(module); stage_dir = os.path.join(config.SDK_STAGE_DIR, stage)
    bank_rel = "Sounds/PC/%s.bank" % module
    plan = [bank_rel, "ModuleData/soundfiles.xml", "ModuleData/" + event_data_name(module), "ModuleData/project.mbproj"]
    base, base_from = existing_mbproj(project, stage_dir)
    if dry:
        return {"would_build": module + ".bank", "events": [{"path": e["path"], "files": len(e["files"]), "replaces_native": e["replaces"], "mixing_from": e["mix_ref"],
                                                           "attributes_from": e["attrs_ref"], "loop": e["loop"]} for e in events],
                "would_stage": [os.path.join(stage_dir, *p.split("/")) for p in plan], "fmod_studio": found["fmod_studio"], "sample_project": found["sample_project"],
                "project_mbproj_extends": base_from}
    sample = Sample(found["sample_project"])
    build_dir = os.path.join(work, "build")
    if os.path.isdir(build_dir): shutil.rmtree(build_dir)
    os.makedirs(work, exist_ok=True)
    manifest, notes, dangling = generate(module, events, native, sample, work, build_dir)
    print("project %s: %d events, %d audio files, %d dangling references dropped" % (os.path.join(work, "project"), len(manifest), sum(len(m["files"]) for m in manifest), dangling))
    log = os.path.join(work, "build.log")
    code, out = run_studio(found["fmod_studio"], os.path.join(work, "project", module + ".fspro"), module, log)
    bank = os.path.join(build_dir, "PC", module + ".bank")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    for l in lines[-12:]: print(l)
    if not os.path.isfile(bank):
        raise SoundError("FMOD Studio built no bank (exit code %s)" % code, "the output is in " + log + ": " + " | ".join(l for l in lines if re.search(r"error|fail", l, re.I))[:600])
    warn = [l for l in lines if re.search(r"warning", l, re.I)]
    # the stage: the bank and the ModuleData files, written together
    write_text(os.path.join(stage_dir, "ModuleData", "soundfiles.xml"), soundfiles_xml(module))
    write_text(os.path.join(stage_dir, "ModuleData", event_data_name(module)), event_data_xml([m["row"] for m in manifest]))
    write_text(os.path.join(stage_dir, "ModuleData", "project.mbproj"), mbproj_text(module, base))
    os.makedirs(os.path.join(stage_dir, "Sounds", "PC"), exist_ok=True)
    shutil.copy2(bank, os.path.join(stage_dir, "Sounds", "PC", module + ".bank"))
    sources = {e["path"]: e["files"] for e in events}
    for m in manifest:
        m["distinct"] = len({hashlib.sha1(open(f, "rb").read()).hexdigest() for f in sources[m["path"]]})
        m["files_count"] = len(m["files"])
    check = check_bank(bank, [dict(guid=m["guid"], path=m["path"], files=m["files_count"], distinct=m["distinct"]) for m in manifest], sources, decode)
    with open(os.path.join(work, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"module": module, "bank": module + ".bank", "events": [{k: v for k, v in m.items() if k not in ("files_count",)} for m in manifest]}, f, indent=1)
    check["bank"] = os.path.join(stage_dir, "Sounds", "PC", module + ".bank")
    notes += check.get("notes", [])
    if base_from and os.path.normcase(base_from) == os.path.normcase(os.path.join(project, "ModuleData", "project.mbproj")):
        notes.append("the project has its own ModuleData/project.mbproj: `bl deploy` copies that file over the installed one and drops the sound entries; add the two sound lines of the staged file to it")
    return {"bank": os.path.join(stage_dir, "Sounds", "PC", module + ".bank"), "bytes": os.path.getsize(bank), "stage": stage_dir,
            "staged": [os.path.join(stage_dir, *p.split("/")) for p in plan], "events": [{k: m[k] for k in ("path", "guid", "replaces_native", "mixing_from", "template", "template_depth", "loop", "length")}
                                                                       | {"files": m["files_count"]} for m in manifest],
            "check": {k: v for k, v in check.items() if k not in ("events",)}, "ok": check["ok"], "notes": notes, "studio_warnings": warn[:10],
            "work": work}


def verify_stage(stage_dir):
    """Advisories and errors for the sound files of a stage (install --verify, stage.verify): everything the engine reads must
    exist and agree. Returns a list of strings; "WARN " ones are advisories."""
    errs = []
    sd = os.path.join(stage_dir, "Sounds", "PC")
    banks = sorted(glob.glob(os.path.join(sd, "*.bank")))
    md = os.path.join(stage_dir, "ModuleData")
    if not banks and not glob.glob(os.path.join(md, "*sound_event_data.xml")): return errs
    sf_ = os.path.join(md, "soundfiles.xml")
    listed = []
    if os.path.isfile(sf_):
        try: listed = [f.get("name") for f in ET.parse(sf_).getroot().iter("file")]
        except ET.ParseError as e: errs.append("soundfiles.xml: %s" % e)
    for b in banks:
        n = os.path.basename(b)
        try:
            v = bank_format(b)
            if v != FORMAT: errs.append("%s: bank format 0x%x/0x%x, the game reads 0x8e/0x8c" % (n, v[0], v[1]))
        except (SoundError, struct.error) as e: errs.append("%s: %s" % (n, e))
        if n not in listed: errs.append("WARN %s is not listed in ModuleData/soundfiles.xml: the engine never loads it" % n)
    for n in listed:
        if not os.path.isfile(os.path.join(sd, n)) and not os.path.isfile(os.path.join(config.MODULE, "Sounds", "PC", n)):
            errs.append("soundfiles.xml lists %s, which is neither staged nor in the module" % n)
    seen = {}
    for x in sorted(glob.glob(os.path.join(md, "*sound_event_data.xml"))):
        try: rows = list(ET.parse(x).getroot().iter("event"))
        except ET.ParseError as e: errs.append("%s: %s" % (os.path.basename(x), e)); continue
        for r in rows:
            p, g = r.get("path"), (r.get("guid") or "").lower()
            if not p or not p.startswith("event:/"): errs.append("%s: an event without an event:/ path" % os.path.basename(x)); continue
            try: uuid.UUID(g)
            except ValueError: errs.append("%s: %s has no valid guid" % (os.path.basename(x), p)); continue
            if not r.get("max_distance"): errs.append("%s: %s has no max_distance (the engine requires it)" % (os.path.basename(x), p))
            if g in seen and seen[g] != p: errs.append("%s: guid %s is used by %s and %s" % (os.path.basename(x), g, seen[g], p))
            seen[g] = p
    nat = os.path.join(config.NATIVE_DATA, "sound_event_data.gen.xml")
    if seen and os.path.isfile(nat):
        with open(nat, encoding="utf-8-sig") as f: ng = set(re.findall(r'guid="(\{[^}]+\})"', f.read().lower()))
        clash = sorted(p for g, p in seen.items() if g in ng)
        if clash: errs.append("these events reuse a Native GUID, so a module bank cannot own them: %s" % ", ".join(clash[:5]))
    return errs


def info(bank, events_xml=None, decode=0, sources=None):
    """What a built bank holds. events_xml: a sound_event_data.xml whose rows name the bank's events (default: the one beside
    the bank in a stage or a module)."""
    if not os.path.isfile(bank): raise SoundError("no bank " + bank)
    if events_xml is None:
        cands = glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(bank))), "ModuleData", "*sound_event_data.xml"))
        events_xml = cands[0] if len(cands) == 1 else None
    evs = []
    if events_xml:
        with open(events_xml, encoding="utf-8-sig") as f: root = ET.fromstring(f.read())
        evs = [{"path": e.get("path"), "guid": e.get("guid")} for e in root.iter("event")]
        d = open(bank, "rb").read()
        evs = [e for e in evs if uuid.UUID(e["guid"]).bytes_le in d] or evs
    for e in evs:                                     # what the project says each event should hold
        if sources and e["path"] in sources:
            e["files"] = len(sources[e["path"]]); e["distinct"] = len({hashlib.sha1(open(f, "rb").read()).hexdigest() for f in sources[e["path"]]})
    res = check_bank(bank, evs, sources, decode)
    res["events_file"] = events_xml
    return res


def search_native(text, limit=60):
    """Native events whose path contains every word of text: path, 2D/3D, length, parameters and the sample event whose mixing
    a clone would take."""
    native = native_events()
    if not native: raise SoundError("Native's sound events were not found under %s" % config.NATIVE_DATA, "set BANNERLORD_DIR")
    words = text.lower().split(); sp = sample_project(); sample = Sample(sp) if sp else None
    rows = []
    for p, a in native.items():
        if all(w in p.lower() for w in words):
            r = {"path": p, "space": "2D" if a.get("max_distance") == DISTANCE_2D else "3D within %s m" % a.get("max_distance"),
                 "length": "loop" if a.get("length") == "-1" else a.get("length"), "parameters": a.get("parameter_indices") or None}
            if sample and len(rows) < limit: r["mixing_template"] = sample.template_for(p)[0]
            rows.append(r)
    return {"matches": len(rows), "events": rows[:limit]}
