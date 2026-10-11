"""Generic XML extractor driven by rule packs.

A rule selects elements with a small path language
    /mapper/select|/mapper/insert     child steps from the root
    //resultMap/result                 any resultMap's result children
    //job//step                        step anywhere under a job
and turns each match into a node, references and key-to-key links. Values come
from templates such as "{@id}", "{/@namespace}", "{../@type}",
"{ancestor::job@id}", "{@id|@name|line}" (first that exists), "{file}",
"{ancestor-node}" (the nearest enclosing element that produced a node).
"""
from __future__ import annotations

import re
import xml.parsers.expat

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import words
from .sql import crud_refs

NAME = "xml"
VERSION = "2"


class El:
    __slots__ = ("tag", "attrs", "children", "parent", "line", "end_line", "text_parts", "node_id")

    def __init__(self, tag, attrs, parent, line):
        self.tag = tag.split(":")[-1]
        self.attrs = {k.split(":")[-1]: v for k, v in attrs.items()}
        self.children, self.parent, self.line, self.end_line = [], parent, line, line
        self.text_parts, self.node_id = [], None

    def itertext(self):
        for p in self.text_parts:
            yield p

    def iter(self):
        yield self
        for c in self.children:
            yield from c.iter()


def _as_utf8(data: bytes) -> bytes:
    """expat reads UTF-8/16 and Latin-1 only: Shift_JIS / EUC-JP files (common in
    Japanese systems) are decoded first and their declaration rewritten."""
    m = re.match(rb'<\?xml[^>]*encoding=["\']([\w.\-]+)["\']', data[:200])
    enc = m.group(1).decode("ascii").lower() if m else "utf-8"
    if enc.replace("_", "-") in ("utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "utf-16"):
        return data
    try:
        text = data.decode(enc)
    except (LookupError, UnicodeDecodeError):
        text = data.decode("cp932", "replace")
    return re.sub(r'(<\?xml[^>]*encoding=["\'])[\w.\-]+', r"\1UTF-8", text, count=1).encode("utf-8")


def parse_xml(data: bytes) -> El | None:
    data = _as_utf8(data)
    p = xml.parsers.expat.ParserCreate()
    stack: list[El] = []
    root = [None]

    def start(tag, attrs):
        parent = stack[-1] if stack else None
        e = El(tag, attrs, parent, p.CurrentLineNumber)
        if parent is not None:
            parent.children.append(e)
        else:
            root[0] = e
        stack.append(e)

    def end(tag):
        e = stack.pop()
        e.end_line = p.CurrentLineNumber

    def chars(text):
        for e in stack:            # body text includes text of nested dynamic tags
            e.text_parts.append(text)

    p.StartElementHandler, p.EndElementHandler, p.CharacterDataHandler = start, end, chars
    p.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def no_entities(*_):
        # entity declarations are the vehicle for "billion laughs" and external
        # entity (XXE) payloads; configuration files in a repository never need them
        raise ValueError("XML entity declarations are not processed")
    p.EntityDeclHandler = no_entities
    p.ExternalEntityRefHandler = lambda *a: 1     # never fetch, never fail
    try:
        p.Parse(data, True)
    except xml.parsers.expat.ExpatError:
        return root[0]
    return root[0]


def _steps(path: str):
    toks = re.findall(r"//|/|[^/]+", path)
    steps, axis = [], "child"
    for t in toks:
        if t == "//":
            axis = "desc"
        elif t == "/":
            axis = "child"
        else:
            steps.append((axis, t))
            axis = "child"
    return steps


def _match(el: El, steps, absolute: bool) -> bool:
    i, cur = len(steps) - 1, el
    if cur.tag != steps[i][1] and steps[i][1] != "*":
        return False
    while i > 0:
        axis = steps[i][0]
        i -= 1
        want = steps[i][1]
        if axis == "child":
            cur = cur.parent
            if cur is None or (cur.tag != want and want != "*"):
                return False
        else:
            cur = cur.parent
            while cur is not None and cur.tag != want:
                cur = cur.parent
            if cur is None:
                return False
    if absolute and steps[0][0] == "child":
        return cur.parent is None
    return True


def select(root: El, expr: str):
    out = []
    for alt in expr.split("|"):
        alt = alt.strip()
        steps = _steps(alt)
        absolute = alt.startswith("/") and not alt.startswith("//")
        for e in root.iter():
            if _match(e, steps, absolute):
                out.append(e)
    return out


def _value(el: El, root: El, path: str, expr: str):
    for alt in expr.split("|"):
        alt = alt.strip()
        v = None
        if alt.startswith("@"):
            v = el.attrs.get(alt[1:])
        elif alt.startswith("/@"):
            v = root.attrs.get(alt[2:])
        elif alt.startswith("../@"):
            v = el.parent.attrs.get(alt[4:]) if el.parent is not None else None
        elif alt.startswith("ancestor::"):
            tag, _, attr = alt[len("ancestor::"):].partition("@")
            a = el.parent
            while a is not None and a.tag != tag:
                a = a.parent
            v = a.attrs.get(attr) if a is not None else None
        elif alt == "ancestor-node":
            a = el.parent
            while a is not None and a.node_id is None:
                a = a.parent
            v = a.node_id if a is not None else None
        elif alt == "file" or alt == "path":
            v = path
        elif alt == "line":
            v = str(el.line)
        elif alt == "tag":
            v = el.tag
        elif alt == "text":
            v = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
        elif alt == "basename":
            v = path.rsplit("/", 1)[-1]
        elif alt.startswith(">") or alt.startswith("..>"):
            # text of a child element: {>ejb-name}, {>method/method-name}; {..>x} starts at the parent
            base = el.parent if alt.startswith("..>") else el
            cur = base
            for seg in alt.split(">", 1)[1].split("/"):
                cur = next((c for c in (cur.children if cur is not None else []) if c.tag == seg), None)
            if cur is not None:
                v = re.sub(r"\s+", " ", "".join(cur.itertext())).strip()
        if v:
            return v
    return None


def _filter(v: str, flt: str) -> str:
    from ..text import snake
    for f in flt.split(":"):
        if f == "snake":
            v = snake(v)
        elif f == "lower":
            v = v.lower()
        elif f == "upper":
            v = v.upper()
        elif f == "basename":
            v = v.rsplit("/", 1)[-1]
        elif f == "nocomponent":
            v = re.sub(r"^component://[^/]+/", "", v)
    return v


def render(tpl: str, el: El, root: El, path: str, pvars: dict | None = None):
    if tpl is None:
        return None
    missing = False

    def rep(m):
        nonlocal missing
        expr, flts = m.group(1), []
        while True:
            head, sep, tail = expr.rpartition(":")
            if sep and tail in ("snake", "lower", "upper", "basename", "nocomponent") and not head.endswith(":"):
                flts.insert(0, tail)
                expr = head
            else:
                break
        flt = ":".join(flts)
        if pvars and expr in pvars:
            v = pvars[expr]
        else:
            v = _value(el, root, path, expr)
        if v is None:
            missing = True
            return ""
        return _filter(v, flt) if flt else v
    out = re.sub(r"\{([^{}]+)\}", rep, tpl)
    return None if missing else out


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    root = parse_xml(data)
    if root is None:
        return f
    applicable = []
    for r in rules.xml:
        w = r.get("when") or {}
        if w.get("root") and root.tag != w["root"]:
            continue
        if w.get("attr") and w["attr"] not in root.attrs:
            continue
        if w.get("path") and not re.search(w["path"], path):
            continue
        applicable.append(r)
    if not applicable:
        return f
    fid = f"file:{path}"
    f.node(fid, Kind.CONFIG, path.rsplit("/", 1)[-1], 1, root.end_line, words(path) + " " + root.tag,
           keys=[f"file:{path}"], xml_root=root.tag, packs=sorted({r["pack"] for r in applicable}))
    links = []
    for r in applicable:
        src_kind = f"rule:{r['pack']}"
        pvars = {}
        for vn, rx in (r.get("vars") or {}).items():
            m = re.search(rx, path)
            if m:
                pvars[vn] = m.group(1)
        where = r.get("where") or {}
        for el in select(root, r["select"]):
            if any(el.attrs.get(k.lstrip("@")) != v for k, v in where.items()):
                continue
            nid = None
            nd = r.get("node")
            if nd:
                nid = render(nd.get("id"), el, root, path, pvars)
                if nid:
                    name = render(nd.get("name", "{tag}"), el, root, path, pvars) or el.tag
                    keys = [k for k in (render(k, el, root, path, pvars) for k in nd.get("keys", [])) if k]
                    raw = "".join(el.itertext())
                    # sql_skip: child elements whose text is not part of the statement
                    # (e.g. a key-generation query nested in an insert)
                    for d in el.iter():
                        if d is not el and d.tag in (nd.get("sql_skip") or ()):
                            raw = raw.replace("".join(d.itertext()), " ", 1)
                    body = re.sub(r"\s+", " ", raw).strip()
                    text = " ".join([el.tag, words(name), " ".join(el.attrs.values()), body[:300]])
                    props = {"tag": el.tag, "rule": r["name"], "pack": r["pack"], "attrs": dict(list(el.attrs.items())[:12])}
                    if nd.get("layer"):
                        props["layer"] = nd["layer"]
                    for k, v in (nd.get("props") or {}).items():     # extra node attributes from the rule
                        rv = render(str(v), el, root, path, pvars)
                        if rv:
                            props[k] = rv
                    if nd.get("sql") == "body":
                        props["sql"] = body[:4000]
                    f.node(nid, nd.get("kind", Kind.XML_ELEMENT), name, el.line, el.end_line, text, keys=keys, **props)
                    el.node_id = nid
                    parent = render(nd.get("parent"), el, root, path, pvars) if nd.get("parent") else fid
                    f.edge(parent or fid, nid, Rel.CONTAINS, 1.0, src_kind)
                    if nd.get("sql") == "body" and body:
                        crud_refs(f, nid, body, el.line, via=r["name"])
            for rf in r.get("refs", []):
                key = render(rf["key"], el, root, path, pvars)
                src = render(rf.get("from"), el, root, path, pvars) if rf.get("from") else (nid or fid)
                if key and src:
                    extra = {k: render(str(v), el, root, path, pvars) for k, v in (rf.get("props") or {}).items()}
                    f.ref(src, key, rf.get("rel", Rel.REFERENCES), float(rf.get("conf", 0.9)), el.line, via=r["name"],
                          optional=bool(rf.get("optional", False)), **{k: v for k, v in extra.items() if v})
            for lk in r.get("links", []):
                a, b = render(lk["from"], el, root, path, pvars), render(lk["to"], el, root, path, pvars)
                if a and b:
                    links.append({"from": a, "to": b, "rel": lk.get("rel", Rel.REFERENCES),
                                  "conf": float(lk.get("conf", 0.8)), "line": el.line, "via": r["name"],
                                  "src_kind": src_kind})
    if links:
        f.payload["links"] = links
    return f
