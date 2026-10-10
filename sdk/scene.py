"""Reader and writer for scene.xscene (and any other XML of the game) that changes nothing it is not asked to change.

A scene folder (Modules/<module>/SceneObj/<scene>/) holds scene.xscene (entities, levels, environment, the terrain's layer
list), atmosphere.xml, terrain.bin (sdk/terrain.py), navmesh.bin and flora.bin. scene.xscene is plain XML written by the
editor with tabs and CRLF. A general XML library would re-format it, so this module keeps every byte of the source next to
the parsed tree: an element that was not edited is written back exactly as it was read, an edited or new element is written
in the editor's own style. load() followed by save() reproduces the file byte for byte (check()).

    doc = scene.load(path)
    for e in doc.entities(): print(e.prefab, e.position)
    doc.add_entity(prefab="rock_cliff_b", position=(600.0, 600.0, 45.0), rotation=(0.0, 0.0, 1.57))
    doc.save(other_path)
"""
import os, re, sys

_NEWLINES = {}   # id(root element) -> the document's line end, so a new element is laid out like its neighbours
_NAME = re.compile(r"[^\s/>=]+")
_ATTR = re.compile(r"""\s+([^\s/>=]+)\s*=\s*("[^"]*"|'[^']*')""")


def _unescape(s):
    if "&" not in s: return s
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def _escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


class Raw:
    """Text between tags, a comment, a declaration, CDATA: kept as read."""
    __slots__ = ("text",)

    def __init__(self, text): self.text = text

    def write(self, out): out.append(self.text)


class Element:
    __slots__ = ("name", "attrs", "children", "parent", "_open", "_close", "_dirty")

    def __init__(self, name, attrs=None, children=None):
        self.name = name
        self.attrs = [[k, str(v)] for k, v in (attrs.items() if isinstance(attrs, dict) else attrs or [])]
        self.children = []
        self.parent = None
        self._open = None      # the start tag exactly as read
        self._close = None     # the end tag exactly as read, None for <a/>
        self._dirty = True     # a new element is written in the editor's style
        for c in children or []: self.append(c)

    # ---- attributes
    def get(self, key, default=None):
        for k, v in self.attrs:
            if k == key: return v
        return default

    def set(self, key, value):
        value = str(value)
        for a in self.attrs:
            if a[0] == key:
                if a[1] != value: a[1] = value; self._dirty = True
                return
        self.attrs.append([key, value]); self._dirty = True

    def pop(self, key):
        for i, a in enumerate(self.attrs):
            if a[0] == key: del self.attrs[i]; self._dirty = True; return a[1]
        return None

    # ---- tree
    def elements(self, name=None):
        return [c for c in self.children if isinstance(c, Element) and (name is None or c.name == name)]

    def find(self, name):
        for c in self.children:
            if isinstance(c, Element) and c.name == name: return c
        return None

    def iter(self, name=None):
        if name is None or self.name == name: yield self
        for c in self.children:
            if isinstance(c, Element): yield from c.iter(name)

    def depth(self):
        d = 0; p = self.parent
        while p is not None: d += 1; p = p.parent
        return d

    def _newline(self):
        e = self
        while e.parent is not None: e = e.parent
        return _NEWLINES.get(id(e), "\r\n")

    def append(self, child):
        """Adds a child element as the last one, on its own line, indented one tab deeper than this element."""
        assert isinstance(child, Element)
        if child.parent is not None: child.parent.remove(child)
        nl = self._newline(); d = self.depth()
        tail = nl + "\t" * d
        if self.children and isinstance(self.children[-1], Raw) and self.children[-1].text.strip() == "":
            self.children[-1].text = nl + "\t" * (d + 1)      # the old "before the end tag" gap becomes the gap before the child
        else:
            self.children.append(Raw(nl + "\t" * (d + 1)))
        child.parent = self
        self.children.append(child); self.children.append(Raw(tail))
        child._reindent(d + 1, nl)
        if self._close is None: self._dirty = True              # <a/> becomes <a>...</a>
        return child

    def remove(self, child):
        """Removes a child element together with the whitespace in front of it."""
        i = next(k for k, c in enumerate(self.children) if c is child)
        del self.children[i]
        if i > 0 and isinstance(self.children[i - 1], Raw) and self.children[i - 1].text.strip() == "":
            if i < len(self.children) and isinstance(self.children[i], Raw) and self.children[i].text.strip() == "":
                del self.children[i - 1]
        child.parent = None
        if not self.elements() and all(isinstance(c, Raw) and c.text.strip() == "" for c in self.children):
            self.children = []; self._dirty = True              # back to <a/>

    def _reindent(self, d, nl):
        """Lays a new (dirty) subtree out in the editor's style: one element a line, tabs."""
        kids = self.elements()
        if not kids:
            if any(isinstance(c, Raw) and c.text.strip() for c in self.children): return   # real text content: keep
            self.children = []; return
        new = []
        for k in kids:
            new.append(Raw(nl + "\t" * (d + 1))); new.append(k); k._reindent(d + 1, nl)
        new.append(Raw(nl + "\t" * d))
        self.children = new

    # ---- output
    def write(self, out):
        empty = not self.children
        if self._dirty or self._open is None:
            out.append("<" + self.name + "".join(' %s="%s"' % (k, _escape(v)) for k, v in self.attrs) + ("/>" if empty else ">"))
        else:
            out.append(self._open)
        if empty and (self._dirty or self._close is None): return
        for c in self.children: c.write(out)
        out.append(self._close if (self._close is not None and not self._dirty) else "</" + self.name + ">")

    def __repr__(self): return "<%s %s>" % (self.name, " ".join('%s="%s"' % (k, v) for k, v in self.attrs))


def parse(text):
    """The document as a list of top-level nodes (Raw and one root Element). text keeps its BOM and line ends."""
    top = []; stack = []; i = 0; n = len(text)

    def add(node):
        if stack:
            stack[-1].children.append(node)
            if isinstance(node, Element): node.parent = stack[-1]
        else: top.append(node)

    while i < n:
        lt = text.find("<", i)
        if lt < 0: add(Raw(text[i:])); break
        if lt > i: add(Raw(text[i:lt]))
        if text.startswith("<!--", lt):
            e = text.index("-->", lt) + 3; add(Raw(text[lt:e])); i = e; continue
        if text.startswith("<![CDATA[", lt):
            e = text.index("]]>", lt) + 3; add(Raw(text[lt:e])); i = e; continue
        if text.startswith("<?", lt):
            e = text.index("?>", lt) + 2; add(Raw(text[lt:e])); i = e; continue
        if text.startswith("<!", lt):
            e = text.index(">", lt) + 1; add(Raw(text[lt:e])); i = e; continue
        if text.startswith("</", lt):
            e = text.index(">", lt) + 1
            el = stack.pop(); name = text[lt + 2:e - 1].strip()
            assert name == el.name, "end tag </%s> closes <%s> at offset %d" % (name, el.name, lt)
            el._close = text[lt:e]; i = e; continue
        m = _NAME.match(text, lt + 1); el = Element(m.group()); el._dirty = False
        p = m.end()
        while True:
            a = _ATTR.match(text, p)
            if not a: break
            el.attrs.append([a.group(1), _unescape(a.group(2)[1:-1])]); p = a.end()
        e = text.index(">", p) + 1
        assert text[p:e].strip() in (">", "/>"), "cannot read the tag at offset %d: %r" % (lt, text[lt:lt + 80])
        el._open = text[lt:e]
        add(el)
        if not text[lt:e].endswith("/>"): stack.append(el)
        i = e
    assert not stack, "unclosed <%s>" % stack[-1].name
    return top


def _fmt(v): return ", ".join("%.3f" % float(x) for x in v)


def _vec(s): return tuple(float(x) for x in s.split(",")) if s else None


class Entity:
    """A <game_entity> of a scene."""

    def __init__(self, el): self.el = el

    name = property(lambda s: s.el.get("name"), lambda s, v: s.el.set("name", v))
    prefab = property(lambda s: s.el.get("prefab"), lambda s, v: s.el.set("prefab", v))

    def _transform(self, create=False):
        t = self.el.find("transform")
        if t is None and create: t = self.el.append(Element("transform"))
        return t

    @property
    def position(self):
        t = self._transform(); return _vec(t.get("position")) if t is not None else None

    @position.setter
    def position(self, v): self._transform(True).set("position", _fmt(v))

    @property
    def rotation(self):
        t = self._transform(); return _vec(t.get("rotation_euler")) if t is not None else None

    @rotation.setter
    def rotation(self, v): self._transform(True).set("rotation_euler", _fmt(v))

    @property
    def scale(self):
        t = self._transform(); return _vec(t.get("scale")) if t is not None and t.get("scale") else None

    @scale.setter
    def scale(self, v): self._transform(True).set("scale", _fmt(v))

    @property
    def tags(self):
        t = self.el.find("tags"); return [x.get("name") for x in t.elements("tag")] if t is not None else []

    @property
    def scripts(self):
        """{script name: {variable: value}}"""
        out = {}
        s = self.el.find("scripts")
        for sc in (s.elements("script") if s is not None else []):
            v = sc.find("variables")
            out[sc.get("name")] = {x.get("name"): x.get("value") for x in (v.elements("variable") if v is not None else [])}
        return out

    def children(self):
        c = self.el.find("children"); return [Entity(x) for x in c.elements("game_entity")] if c is not None else []

    def __repr__(self): return "Entity(%s%s at %s)" % (self.prefab or "", (" '%s'" % self.name) if self.name else "", self.position)


class Scene:
    def __init__(self, path=None, data=None):
        self.path = path
        if data is None:
            with open(path, "rb") as f: data = f.read()
        self.encoding = "utf-8-sig" if data[:3] == b"\xef\xbb\xbf" else "utf-8"
        self.bom = data[:3] == b"\xef\xbb\xbf"
        text = data[3:].decode("utf-8") if self.bom else data.decode("utf-8")
        self.nodes = parse(text)
        self.root = next(x for x in self.nodes if isinstance(x, Element))
        self._nl = "\r\n" if "\r\n" in text else "\n"
        _NEWLINES[id(self.root)] = self._nl

    def tobytes(self):
        out = []
        for x in self.nodes: x.write(out)
        return (b"\xef\xbb\xbf" if self.bom else b"") + "".join(out).encode("utf-8")

    def save(self, path=None):
        path = path or self.path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f: f.write(self.tobytes())

    # ---- scene content
    def entities_element(self): return self.root.find("entities")

    def entities(self, recursive=False):
        """The top-level entities, or with recursive=True every entity including children of entities."""
        root = self.entities_element()
        if root is None: return []
        if recursive: return [Entity(e) for e in root.iter("game_entity")]
        return [Entity(e) for e in root.elements("game_entity")]

    def add_entity(self, prefab=None, name=None, position=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0), scale=None, tags=(), scripts=None):
        """A new top-level entity, written the way the editor writes one. scripts: {script name: {variable: value}}."""
        attrs = []
        if name is not None: attrs.append(["name", name])
        if prefab is not None: attrs.append(["prefab", prefab])
        else: attrs.append(["old_prefab_name", ""])
        el = Element("game_entity", attrs)
        if tags:
            el.children.append(Element("tags", None, [Element("tag", [["name", t]]) for t in tags]))
        t = [["position", _fmt(position)], ["rotation_euler", _fmt(rotation)]]
        if scale is not None: t.append(["scale", _fmt(scale)])
        el.children.append(Element("transform", t))
        if scripts:
            el.children.append(Element("scripts", None, [
                Element("script", [["name", sn]], [Element("variables", None, [Element("variable", [["name", k], ["value", v]]) for k, v in vs.items()])])
                for sn, vs in scripts.items()]))
        for c in el.children:
            if isinstance(c, Element): c.parent = el
        root = self.entities_element()
        assert root is not None, "the scene has no <entities>"
        root.append(el)
        return Entity(el)

    def remove_entity(self, entity):
        entity.el.parent.remove(entity.el)


def load(path): return Scene(path)


def loads(data): return Scene(data=data)


def check(path):
    """True when load + save reproduces the file byte for byte."""
    with open(path, "rb") as f: data = f.read()
    return loads(data).tobytes() == data


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "check":
        print("identical" if check(a[1]) else "DIFFERS")
    elif len(a) == 2 and a[0] == "list":
        s = load(a[1]); es = s.entities()
        print(len(es), "top-level entities,", len(s.entities(True)), "in all")
        for e in es[:40]: print(" ", e)
    else:
        print("scene.py check <file.xscene> | list <file.xscene>")
