"""Indexers for non-Java artefacts: Thymeleaf templates, SQL, message bundles,
configuration files and design documents (Markdown / text)."""
from __future__ import annotations

import os
import re
from collections import defaultdict

from ..model import Edge, Kind, Node, Rel
from .text import singular, split_ident, words

# ----------------------------------------------------------------- templates
_EXPR = re.compile(r"\$\{\s*([A-Za-z_]\w*)((?:\.[A-Za-z_]\w*)+)")
_SEL = re.compile(r"\*\{\s*([A-Za-z_]\w*)")
_MSG = re.compile(r"#\{\s*([\w.\-]+)")
_OBJ = re.compile(r'th:object\s*=\s*"\$\{\s*(\w+)\s*\}"')
_EACH = re.compile(r'th:each\s*=\s*"\s*(\w+)\s*(?:,\s*\w+\s*)?:\s*\$\{\s*(\w+)((?:\.\w+)*)\s*\}"')
_FRAG = re.compile(r"~\{\s*([\w/\-]+)\s*::")
_QUOTED = re.compile(r"'([A-Za-z_]\w*)'")


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def index_templates(repo: str, files: list[str], java) -> tuple[list[Node], list[Edge], dict[str, str]]:
    """Returns nodes, edges and a map template-name -> node id.

    `java` is the JavaIndexer, used to resolve `${owner.telephone}` to the Owner
    class's `telephone` field by matching variable names to entity classes."""
    nodes, edges, names = [], [], {}
    entity_by_var = {}
    for name, infos in java.by_name.items():
        for c in infos:
            if not c.is_test:
                entity_by_var.setdefault(name[:1].lower() + name[1:], c)

    def resolve_var(var: str, aliases: dict) -> object | None:
        if var in aliases:
            return aliases[var]
        return entity_by_var.get(var) or entity_by_var.get(singular(var))

    for rel in files:
        src = open(os.path.join(repo, rel), encoding="utf-8", errors="replace").read()
        tname = re.sub(r"^.*?templates/", "", rel)[:-5] if "templates/" in rel else rel
        tid = f"tpl:{tname}"
        names[tname] = tid
        visible = re.sub(r"<[^>]+>", " ", src)
        visible = re.sub(r"\s+", " ", visible)
        nodes.append(Node(tid, Kind.TEMPLATE, tname, rel, 1, src.count("\n") + 1,
                          words(tname) + " " + visible[:2000], {"engine": "thymeleaf"}))
        aliases: dict[str, object] = {}
        obj = None
        m = _OBJ.search(src)
        if m:
            obj = resolve_var(m.group(1), aliases)
        for m in _EACH.finditer(src):
            var, root, path = m.group(1), m.group(2), m.group(3)
            holder = resolve_var(root, aliases)
            target = None
            if holder and path:
                f = java.all_fields(holder).get(path.strip(".").split(".")[-1])
                if f:
                    args = re.findall(r"<\s*(\w+)", f[1].type)
                    target = java.resolve_class(args[0], holder) if args else None
            aliases[var] = target or resolve_var(var, aliases)

        def bind(cls, fname, pos, via):
            if cls is None:
                return
            f = java.all_fields(cls).get(fname)
            if f:
                owner, fi = f
                edges.append(Edge(tid, java.fid(owner, fi), Rel.BINDS, {"line": _line_of(src, pos), "via": via}))
                return True
            return False

        for m in _EXPR.finditer(src):
            var, path = m.group(1), m.group(2).strip(".").split(".")
            cls = resolve_var(var, aliases)
            for i, prop in enumerate(path):
                if cls is None:
                    break
                bind(cls, prop, m.start(), "expr")
                f = java.all_fields(cls).get(prop)
                if not f:
                    break
                args = re.findall(r"<\s*(\w+)", f[1].type)
                cls = java.resolve_class(args[0] if args else f[1].type, cls)
        for m in _SEL.finditer(src):
            bind(obj, m.group(1), m.start(), "selection")
        for m in _QUOTED.finditer(src):
            bind(obj, m.group(1), m.start(), "literal")
        for m in _MSG.finditer(src):
            edges.append(Edge(tid, f"msg:{m.group(1)}", Rel.USES_MESSAGE, {"line": _line_of(src, m.start())}))
        for m in _FRAG.finditer(src):
            edges.append(Edge(tid, f"tpl:{m.group(1)}", Rel.INCLUDES, {"line": _line_of(src, m.start())}))
        if obj is not None:
            edges.append(Edge(tid, obj.id, Rel.REFERENCES, {"via": "th:object"}))
    return nodes, edges, names


# ----------------------------------------------------------------------- SQL
_CREATE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"]?(\w+)[`\"]?\s*\(", re.I)
_INSERT = re.compile(r"INSERT\s+(?:IGNORE\s+)?INTO\s+[`\"]?(\w+)[`\"]?\s*(\([^)]*\))?\s*(VALUES|SELECT)", re.I)
_CONSTRAINT = {"primary", "foreign", "unique", "constraint", "key", "index", "check"}


def _split_top(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def index_sql(repo: str, files: list[str]) -> tuple[list[Node], list[Edge]]:
    nodes: dict[str, Node] = {}
    edges = []
    for rel in files:
        src = open(os.path.join(repo, rel), encoding="utf-8", errors="replace").read()
        fid = f"file:{rel}"
        dialect = rel.split("/")[-2] if "/" in rel else ""
        nodes[fid] = Node(fid, Kind.SQL_FILE, rel.split("/")[-1] + (f" ({dialect})" if dialect else ""),
                          rel, 1, src.count("\n") + 1, words(rel), {"dialect": dialect})
        for m in _CREATE.finditer(src):
            table = m.group(1).lower()
            start = m.end()
            depth, i = 1, start
            while i < len(src) and depth:
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                i += 1
            body = src[start:i - 1]
            line = _line_of(src, m.start())
            tid = f"table:{table}"
            nodes.setdefault(tid, Node(tid, Kind.TABLE, table, None, None, None, words(table), {"files": []}))
            nodes[tid].props["files"].append(rel)
            edges.append(Edge(fid, tid, Rel.DEFINES, {"line": line}))
            offset = start
            for col_def in _split_top(body):
                tok = col_def.strip().split()
                if not tok:
                    offset += len(col_def) + 1
                    continue
                cname = tok[0].strip("`\"").lower()
                if cname in _CONSTRAINT:
                    offset += len(col_def) + 1
                    continue
                cid = f"col:{table}.{cname}"
                cline = _line_of(src, offset + len(col_def) - len(col_def.lstrip()))
                ctype = " ".join(tok[1:3])
                nodes.setdefault(cid, Node(cid, Kind.COLUMN, f"{table}.{cname}", None, None, None,
                                           f"{words(table)} {words(cname)}", {"types": {}}))
                nodes[cid].props["types"][dialect or rel] = ctype
                edges.append(Edge(tid, cid, Rel.CONTAINS))
                edges.append(Edge(fid, cid, Rel.DEFINES, {"line": cline}))
                offset += len(col_def) + 1
        for m in _INSERT.finditer(src):
            table = m.group(1).lower()
            positional = m.group(2) is None
            edges.append(Edge(fid, f"table:{table}", Rel.POPULATES,
                              {"line": _line_of(src, m.start()), "positional": positional}))
    # dedupe populate edges per (file, table) keeping first line and positional flag
    seen, out = {}, []
    for e in edges:
        k = e.key()
        if k in seen:
            continue
        seen[k] = e; out.append(e)
    return list(nodes.values()), out


# ------------------------------------------------------------ message bundles
def index_messages(repo: str, files: list[str]) -> tuple[list[Node], list[Edge]]:
    keys: dict[str, dict] = defaultdict(lambda: {"values": {}, "lines": {}})
    nodes, edges = [], []
    for rel in files:
        loc = re.search(r"_([a-z]{2}(?:_[A-Z]{2})?)\.properties$", rel)
        locale = loc.group(1) if loc else "default"
        fid = f"file:{rel}"
        src = open(os.path.join(repo, rel), encoding="utf-8", errors="replace").read()
        nodes.append(Node(fid, Kind.CONFIG, rel.split("/")[-1], rel, 1, src.count("\n") + 1,
                          words(rel), {"locale": locale, "bundle": True}))
        for i, line in enumerate(src.splitlines(), 1):
            if not line.strip() or line.lstrip().startswith(("#", "!")) or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            try:
                v = v.encode("latin-1").decode("unicode_escape") if "\\u" in v else v
            except UnicodeDecodeError:
                pass
            keys[k]["values"][locale] = v.strip()
            keys[k]["lines"][rel] = i
            edges.append(Edge(fid, f"msg:{k}", Rel.CONTAINS, {"line": i}))
    for k, d in keys.items():
        text = words(k) + " " + " ".join(d["values"].values())
        nodes.append(Node(f"msg:{k}", Kind.MESSAGE, k, None, None, None, text,
                          {"values": d["values"], "files": d["lines"]}))
    return nodes, edges


def index_config(repo: str, files: list[str]) -> list[Node]:
    out = []
    for rel in files:
        src = open(os.path.join(repo, rel), encoding="utf-8", errors="replace").read()
        out.append(Node(f"file:{rel}", Kind.CONFIG, rel.split("/")[-1], rel, 1, src.count("\n") + 1,
                        words(rel) + " " + re.sub(r"[^\w\s]", " ", src)[:3000], {"bundle": False}))
    return out


# ------------------------------------------------------------- design docs
_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.M)


def index_docs(root: str, files: list[str], prefix: str, vocab: dict[str, str]) -> tuple[list[Node], list[Edge]]:
    """Split Markdown/text documents by heading and link each section to the code
    elements it mentions. `vocab` maps a mention string (class name, table name,
    template name, endpoint path, Class.field) to a node id."""
    nodes, edges = [], []
    mention_rx = [(re.compile(r"(?<![\w/])" + re.escape(k) + r"(?![\w])"), nid) for k, nid in vocab.items() if len(k) >= 4]
    for rel in files:
        src = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        heads = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in _HEADING.finditer(src)]
        if not heads:
            heads = [(0, 1, os.path.basename(rel))]
        stack: list[str] = []
        for i, (pos, level, title) in enumerate(heads):
            end = heads[i + 1][0] if i + 1 < len(heads) else len(src)
            body = src[pos:end]
            stack = stack[: level - 1] + [title]
            sid = f"doc:{prefix}{rel}#{i}"
            line = _line_of(src, pos)
            nodes.append(Node(sid, Kind.DOC, " / ".join(stack[-2:]), f"{prefix}{rel}", line,
                              line + body.count("\n"), title + " " + body[:4000],
                              {"title": title, "path": stack, "doc": rel, "source": prefix.rstrip(":") or "repo"}))
            hits = set()
            for rx, nid in mention_rx:
                if nid not in hits and rx.search(body):
                    hits.add(nid)
                    edges.append(Edge(sid, nid, Rel.DESCRIBES))
    return nodes, edges
