"""Changes to the game's own data (settlements, items, troops, kingdoms, cultures ...) from a module, as XSLT patches.

The game merges every module's XML of one kind (all "Settlements", all "Items" ...) into one document. When a module's XML
file has an .xslt of the same name next to it, the game runs that transform over the document merged so far before adding
the module's own XML. That is the supported way to change an object another module defines without copying it.

    set_attrs(project, mod, kind, element, object_id, {attribute: value})
        kind     the XmlName id the game groups the files by and the root element of the file: Settlements, Items,
                 NPCCharacters, Kingdoms, Clans, Cultures ...
        element  the object's element: Settlement, Item, NPCCharacter, Kingdom ...
    -> writes ModuleData/<mod>_<kind>.xml (an empty root, the file the XSLT belongs to), ModuleData/<mod>_<kind>.xslt (the
       patch) and ModuleData/<mod>_patches.json (what was asked, so the patch is rebuilt from it every time), and adds the
       XmlNode to SubModule.xml. A value set again replaces the old one; value None removes the change.
The transform copies everything and, for each changed object, rewrites the named attributes (added when missing).
"""
import json, os, re
from xml.sax.saxutils import quoteattr, escape

NL = "\n"
GAME_TYPES = ("Campaign", "CampaignStoryMode", "CustomGame", "EditorGame")


def _paths(project, mod, kind):
    base = os.path.join(project, "ModuleData", "%s_%s" % (mod.lower(), kind.lower()))
    return base + ".xml", base + ".xslt", os.path.join(project, "ModuleData", "%s_patches.json" % mod.lower())


def xslt(changes):
    """The transform for {(element, id): {attribute: value}}."""
    out = ['<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">',
           '\t<xsl:output omit-xml-declaration="yes"/>',
           '\t<xsl:template match="@*|node()"><xsl:copy><xsl:apply-templates select="@*|node()"/></xsl:copy></xsl:template>']
    for (element, oid), attrs in sorted(changes.items()):
        if not attrs: continue
        keep = " and ".join("name()!=%s" % quoteattr(k) for k in sorted(attrs))
        out.append("\t<xsl:template match=%s>" % quoteattr("%s[@id='%s']" % (element, oid.replace("'", ""))))
        out.append("\t\t<xsl:copy>")
        out.append("\t\t\t<xsl:apply-templates select=%s/>" % quoteattr("@*[%s]" % keep))
        for k, v in sorted(attrs.items()): out.append("\t\t\t<xsl:attribute name=%s>%s</xsl:attribute>" % (quoteattr(k), escape(str(v))))
        out.append("\t\t\t<xsl:apply-templates select=\"node()\"/>")
        out.append("\t\t</xsl:copy>")
        out.append("\t</xsl:template>")
    out.append("</xsl:stylesheet>")
    return NL.join(out) + NL


def _submodule_node(project, mod, kind):
    """Adds <XmlNode><XmlName id=kind path=<mod>_<kind>/> ... to SubModule.xml when it is not there yet."""
    p = os.path.join(project, "SubModule.xml"); text = open(p, encoding="utf-8").read()
    path = "%s_%s" % (mod.lower(), kind.lower())
    if 'path="%s"' % path in text: return False
    node = ('\t\t<XmlNode>\n\t\t\t<XmlName id="%s" path="%s"/>\n\t\t\t<IncludedGameTypes>\n' % (kind, path)
            + "".join('\t\t\t\t<GameType value="%s"/>\n' % g for g in GAME_TYPES) + "\t\t\t</IncludedGameTypes>\n\t\t</XmlNode>\n")
    if "</Xmls>" in text: text = text.replace("\t</Xmls>", node + "\t</Xmls>", 1) if "\t</Xmls>" in text else text.replace("</Xmls>", node + "</Xmls>", 1)
    else: text = text.replace("</Module>", "\t<Xmls>\n" + node + "\t</Xmls>\n</Module>", 1)
    with open(p, "w", encoding="utf-8", newline=NL) as f: f.write(text)
    return True


def set_attrs(project, mod, kind, element, object_id, attrs):
    """Records the change and rewrites the module's patch files for that kind. Returns {"xml", "xslt", "changes"}."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", kind) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", element): raise ValueError("kind and element are XML names")
    xml_p, xslt_p, spec_p = _paths(project, mod, kind)
    spec = json.load(open(spec_p, encoding="utf-8")) if os.path.isfile(spec_p) else {}
    entry = spec.setdefault(kind, {}).setdefault("%s|%s" % (element, object_id), {})
    for k, v in attrs.items():
        if v is None: entry.pop(k, None)
        else: entry[k] = v
    os.makedirs(os.path.dirname(xml_p), exist_ok=True)
    with open(spec_p, "w", encoding="utf-8", newline=NL) as f: json.dump(spec, f, indent=2, sort_keys=True)
    changes = {tuple(key.split("|", 1)): v for key, v in spec[kind].items()}
    with open(xslt_p, "w", encoding="utf-8", newline=NL) as f: f.write(xslt(changes))
    if not os.path.isfile(xml_p):
        with open(xml_p, "w", encoding="utf-8-sig", newline=NL) as f: f.write('<?xml version="1.0" encoding="utf-8"?>\n<%s>\n</%s>\n' % (kind, kind))
    added = _submodule_node(project, mod, kind)
    return {"xml": xml_p, "xslt": xslt_p, "changes": {"%s %s" % k: v for k, v in changes.items()}, "submodule_node_added": added}
