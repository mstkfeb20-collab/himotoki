"""TypeScript / TSX extractor (tree-sitter), one file at a time.

A module becomes a container node; its functions (declarations and functions
assigned to constants, React components included), classes and their members,
interfaces, type aliases, enums and exported constants become elements. Inside
every function body it records:

* calls and JSX components, resolved to the same file, to imports, or to
  `this.` members;
* uses of imported or local types and values (REFERENCES);
* data access — Supabase `.from("table")` chains and Prisma `prisma.model.op()`
  — as READS / WRITES of tables; `.rpc("fn")` as a call of a database function;
* URL literals ("/api/...") as references to endpoints;
* comments, string literals and JSX text, so Japanese wording on screens and in
  comments is searchable from a Japanese requirement.

Next.js App Router files (`app/**/page.tsx`, `route.ts`) become endpoints.
Test files (`*.test.ts`, `*.spec.ts`, `__tests__/`) become test classes with one
test method per `it` / `test`, linked to what they call by TESTS.

Imports are emitted as keys the linker resolves across files: relative paths are
resolved here (`tsexp:<module>#<name>`); aliases and workspace packages
(`@/lib/x`, `@scope/pkg/sub`) are left to the linker (`tsimp:<spec>#<name>`),
which reads `tsconfig.json` and `package.json` (also extracted here).
"""
from __future__ import annotations

import json
import posixpath
import re

import tree_sitter_typescript as tst
from tree_sitter import Language, Parser

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import decode_source, words

NAME = "ts"
VERSION = "3"
TS_EXT = (".ts", ".tsx", ".mts", ".cts")
_parsers: dict = {}

TEST_RX = re.compile(r"(\.(test|spec)\.[cm]?tsx?$|(^|/)__tests__/)")
APP_ROUTE = re.compile(r"(?:^|/)app/(.*?)(?:^|/)?(page|route|layout)\.(tsx?|jsx?)$")
WRITE_OPS = {"insert", "update", "upsert", "delete", "create", "createMany", "updateMany", "deleteMany",
             "createManyAndReturn", "updateManyAndReturn"}
PRISMA_OPS = {"findMany", "findFirst", "findUnique", "findFirstOrThrow", "findUniqueOrThrow", "count", "aggregate",
              "groupBy", "create", "createMany", "update", "updateMany", "upsert", "delete", "deleteMany",
              "createManyAndReturn", "updateManyAndReturn"}
HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
TEST_CALLS = {"it", "test"}
FUNC_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}
JP = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")


def is_ts(path: str) -> bool:
    low = path.lower()
    return low.endswith(TS_EXT) and not low.endswith(".d.ts")


def is_config(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base == "package.json" or (base.startswith("tsconfig") and base.endswith(".json"))


def module_of(path: str) -> str:
    """`web/src/lib/a.ts` -> `web/src/lib/a`; `x/index.tsx` -> `x/index` (the linker also tries `x`)."""
    return re.sub(r"\.[cm]?[tj]sx?$", "", path)


def _parser(tsx: bool):
    if tsx not in _parsers:
        _parsers[tsx] = Parser(Language(tst.language_tsx() if tsx else tst.language_typescript()))
    return _parsers[tsx]


def _t(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _str(n) -> str | None:
    """Value of a plain string literal (or a template string without substitutions)."""
    if n is None:
        return None
    if n.type == "string":
        return "".join(_t(c) for c in n.named_children if c.type in ("string_fragment", "escape_sequence"))
    if n.type == "template_string" and not any(c.type == "template_substitution" for c in n.named_children):
        return "".join(_t(c) for c in n.named_children if c.type == "string_fragment")
    return None


def _comment(c: str) -> str:
    c = re.sub(r"^/\*\*?|\*/$", "", c.strip())
    c = re.sub(r"^\s*\*\s?", "", c, flags=re.M)
    c = re.sub(r"^//\s?", "", c, flags=re.M)
    return re.sub(r"\s+", " ", c).strip()


def _doc_before(node) -> str:
    """Comments directly above a declaration (also above its `export`)."""
    out = []
    n = node.parent if node.parent is not None and node.parent.type == "export_statement" else node
    p = n.prev_named_sibling
    while p is not None and p.type == "comment" and p.end_point[0] >= n.start_point[0] - 1 - len(out) * 0:
        out.insert(0, _comment(_t(p)))
        n, p = p, p.prev_named_sibling
    return " ".join(out)[:600]


def resolve_relative(importer: str, spec: str) -> str:
    """`./a` from `web/src/x/b.ts` -> `web/src/x/a` (no extension; the linker tries index files)."""
    base = posixpath.dirname(importer)
    return module_of(posixpath.normpath(posixpath.join(base, spec)))


def route_of(path: str) -> tuple[str, str] | None:
    """Next.js App Router: `web/src/app/(public)/bills/[id]/page.tsx` -> ("page", "/bills/{id}")."""
    m = re.search(r"(?:^|/)app/((?:[^/]+/)*)(page|route)\.[cm]?[tj]sx?$", path)
    if not m:
        return None
    segs = []
    for s in m.group(1).strip("/").split("/"):
        if not s or (s.startswith("(") and s.endswith(")")) or s.startswith("@"):
            continue                       # route groups and parallel-route slots are not in the URL
        s = re.sub(r"^\[\[?\.\.\.(\w+)\]\]?$", r"{\1}", s)
        s = re.sub(r"^\[(\w+)\]$", r"{\1}", s)
        segs.append(s)
    return m.group(2), "/" + "/".join(segs)


# --------------------------------------------------------------------------- config files
def _config(path: str, data: bytes) -> FileFacts:
    f = FileFacts(path, NAME)
    raw = decode_source(data)
    # tsconfig allows comments and trailing commas
    txt = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*[\s\S]*?\*/', lambda m: m.group(1) or "", raw)
    txt = re.sub(r",(\s*[}\]])", r"\1", txt)
    try:
        d = json.loads(txt)
    except ValueError as e:
        f.errors.append(f"unreadable JSON: {str(e)[:120]}")
        return f
    folder = posixpath.dirname(path)
    if path.endswith("package.json"):
        if isinstance(d.get("name"), str):
            exports = {}
            ex = d.get("exports")
            if isinstance(ex, str):
                exports["."] = ex
            elif isinstance(ex, dict):
                for k, v in ex.items():
                    if isinstance(v, dict):
                        v = v.get("types") or v.get("import") or v.get("default") or v.get("require")
                    if isinstance(v, str) and k.startswith("."):
                        exports[k] = v
            f.payload["ts_package"] = {"dir": folder, "name": d["name"], "exports": exports,
                                       "main": d.get("types") or d.get("main") or d.get("module")}
    else:
        co = d.get("compilerOptions") or {}
        paths = co.get("paths") or {}
        if paths or co.get("baseUrl"):
            f.payload["ts_config"] = {"dir": folder, "baseUrl": co.get("baseUrl") or ".",
                                      "paths": {k: v for k, v in paths.items() if isinstance(v, list)}}
    return f


# --------------------------------------------------------------------------- source files
class _Ctx:
    def __init__(self, f: FileFacts, path: str, test: bool):
        self.f, self.path, self.test = f, path, test
        self.mod = module_of(path)
        self.stem = posixpath.basename(self.mod)
        if self.stem == "index":
            self.stem = posixpath.basename(posixpath.dirname(self.mod)) or "index"
        self.imports: dict[str, tuple[str, str]] = {}    # local name -> (key prefix, imported name or "*")
        self.local: dict[str, str] = {}                  # top-level symbol -> node id
        self.members: dict[str, dict[str, str]] = {}     # class -> member -> node id
        self.exported: dict[str, list[str]] = {}         # node id -> exported names
        self.bodies: list[tuple] = []                    # (node id, body node, class name)

    def key(self, spec: str, name: str) -> str:
        if spec.startswith("."):
            return f"tsexp:{resolve_relative(self.path, spec)}#{name}"
        return f"tsimp:{spec}#{name}"


def extract(path: str, data: bytes, rules=None) -> FileFacts:
    if is_config(path):
        return _config(path, data)
    f = FileFacts(path, NAME)
    src = decode_source(data)
    tree = _parser(path.lower().endswith("x")).parse(src.encode("utf-8"))
    root = tree.root_node
    test = bool(TEST_RX.search(path))
    c = _Ctx(f, path, test)
    mid = f"ts:{path}"
    lines = src.count("\n") + 1
    head = " ".join(_comment(_t(n)) for n in root.named_children[:3] if n.type == "comment")
    f.node(mid, Kind.TEST_CLASS if test else Kind.CLASS, c.stem, 1, lines, f"{words(c.stem)} {head}"[:600],
           keys=[f"tsmod:{c.mod}"], fqn=c.mod, tskind="module")

    for n in root.named_children:
        if n.type == "import_statement":
            _import(c, n)
    for n in root.named_children:
        _top(c, n, mid)
    for nid, names in c.exported.items():
        for nm in names:
            for d in f.nodes:
                if d["id"] == nid:
                    d.setdefault("keys", []).append(f"tsexp:{c.mod}#{nm}")
    for nid, body, cls in c.bodies:
        _body(c, nid, body, cls)
    if test:
        _tests(c, root, mid)
    route = route_of(path)
    if route:
        kind, url = route
        handlers = []
        if kind == "page":
            dflt = [nid for nid, names in c.exported.items() if "default" in names]
            handlers = [("PAGE", h) for h in dflt[:1]]
        else:
            handlers = [(v, c.local[v]) for v in HTTP_VERBS if v in c.local]
        for verb, h in handlers:
            eid = f"ep:{verb} {url}"
            f.node(eid, Kind.ENDPOINT, f"{verb} {url}", 1, lines, f"{verb.lower()} {words(url)} {c.stem}",
                   keys=[f"url:{verb} {url}", f"url:{url}"], verb="GET" if verb == "PAGE" else verb, path=url,
                   page=verb == "PAGE")
            f.edge(eid, h, Rel.HANDLES, 1.0, "parser")
    return f


def _import(c: _Ctx, n):
    src = _str(n.child_by_field_name("source"))
    if not src:
        return
    clause = next((x for x in n.named_children if x.type == "import_clause"), None)
    if clause is None:
        return
    for x in clause.named_children:
        if x.type == "identifier":
            c.imports[_t(x)] = (src, "default")
        elif x.type == "namespace_import":
            ident = next((y for y in x.named_children if y.type == "identifier"), None)
            if ident is not None:
                c.imports[_t(ident)] = (src, "*")
        elif x.type == "named_imports":
            for s in x.named_children:
                if s.type != "import_specifier":
                    continue
                name = _t(s.child_by_field_name("name"))
                alias = s.child_by_field_name("alias")
                c.imports[_t(alias) if alias is not None else name] = (src, name)


def _top(c: _Ctx, n, mid: str):
    f = c.f
    if n.type == "export_statement":
        src = _str(n.child_by_field_name("source"))
        decl = n.child_by_field_name("declaration")
        is_default = any(ch.type == "default" for ch in n.children)
        if src is not None:                                   # re-export from another module
            clause = next((x for x in n.named_children if x.type == "export_clause"), None)
            names = {}
            if clause is not None:
                for s in clause.named_children:
                    if s.type == "export_specifier":
                        nm = _t(s.child_by_field_name("name"))
                        al = s.child_by_field_name("alias")
                        names[_t(al) if al is not None else nm] = nm
            ns = next((x for x in n.named_children if x.type == "namespace_export"), None)
            target = resolve_relative(c.path, src) if src.startswith(".") else None
            f.payload.setdefault("ts_reexports", []).append(
                {"module": c.mod, "target": target, "spec": None if target else src, "from": c.path,
                 "names": names or None, "as": _t(ns.named_children[0]) if ns is not None and ns.named_children else None})
            return
        if decl is not None:
            ids = _decl(c, decl, mid, exported=True)
            for i, nm in ids:
                c.exported.setdefault(i, []).append(nm)
                if is_default:
                    c.exported[i].append("default")
            return
        if is_default:
            val = n.child_by_field_name("value") or (n.named_children[-1] if n.named_children else None)
            if val is None:
                return
            if val.type == "identifier" and _t(val) in c.local:
                c.exported.setdefault(c.local[_t(val)], []).append("default")
            elif val.type in FUNC_TYPES or val.type in ("call_expression", "class"):
                nid = _func(c, "default", val, mid, n)
                c.exported.setdefault(nid, []).append("default")
            return
        clause = next((x for x in n.named_children if x.type == "export_clause"), None)
        if clause is not None:                              # export { a, b as c }
            for s in clause.named_children:
                nm = _t(s.child_by_field_name("name"))
                al = s.child_by_field_name("alias")
                if nm in c.local:
                    c.exported.setdefault(c.local[nm], []).append(_t(al) if al is not None else nm)
        return
    if n.type in ("function_declaration", "generator_function_declaration", "class_declaration",
                  "abstract_class_declaration", "interface_declaration", "type_alias_declaration",
                  "enum_declaration", "lexical_declaration", "variable_declaration"):
        _decl(c, n, mid, exported=False)
    elif n.type == "expression_statement" and not c.test:
        # top-level statements (module side effects): calls are attributed to the module
        c.bodies.append((mid, n, None))


def _decl(c: _Ctx, n, mid: str, exported: bool) -> list[tuple[str, str]]:
    f = c.f
    t = n.type
    if t in ("function_declaration", "generator_function_declaration"):
        name = _t(n.child_by_field_name("name")) or "default"
        return [(_func(c, name, n, mid, n), name)]
    if t in ("class_declaration", "abstract_class_declaration"):
        return [(_class(c, n, mid), _t(n.child_by_field_name("name")))]
    if t in ("interface_declaration", "type_alias_declaration"):
        name = _t(n.child_by_field_name("name"))
        tid = f"ts:{c.path}#{name}"
        body = n.child_by_field_name("body") or n.child_by_field_name("value")
        props = _props(body)
        f.node(tid, Kind.CLASS, name, n.start_point[0] + 1, n.end_point[0] + 1,
               f"{words(name)} {' '.join(words(p) for p, _ in props)} {_doc_before(n)}"[:1500],
               fqn=f"{c.mod}.{name}", tskind="interface" if t == "interface_declaration" else "type")
        f.edge(mid, tid, Rel.CONTAINS)
        c.local[name] = tid
        c.members[name] = {}
        for p, pn in props:
            pid = f"{tid}.{p}"
            sig = pn.type == "method_signature"
            f.node(pid, Kind.METHOD if sig else Kind.FIELD, f"{name}.{p}()" if sig else f"{name}.{p}",
                   pn.start_point[0] + 1, pn.end_point[0] + 1,
                   f"{words(p)} {words(name)} {_comment_near(pn)}"[:400], tsfield=not sig or None, abstract=sig or None)
            f.edge(tid, pid, Rel.CONTAINS)
            c.members[name][p] = pid
        c.bodies.append((tid, body, None)) if body is not None else None
        return [(tid, name)]
    if t == "enum_declaration":
        name = _t(n.child_by_field_name("name"))
        eid = f"ts:{c.path}#{name}"
        f.node(eid, Kind.CLASS, name, n.start_point[0] + 1, n.end_point[0] + 1,
               f"{words(name)} {_doc_before(n)} {_strings(n)}"[:1000], fqn=f"{c.mod}.{name}", tskind="enum")
        f.edge(mid, eid, Rel.CONTAINS)
        c.local[name] = eid
        body = n.child_by_field_name("body")
        c.members[name] = {}
        for m in (body.named_children if body is not None else []):
            mn = m.child_by_field_name("name") if m.type == "enum_assignment" else m
            if mn is None or mn.type not in ("property_identifier", "identifier", "string"):
                continue
            nm = _str(mn) or _t(mn)
            pid = f"{eid}.{nm}"
            f.node(pid, Kind.FIELD, f"{name}.{nm}", m.start_point[0] + 1, m.end_point[0] + 1,
                   f"{words(nm)} {_strings(m)}"[:300])
            f.edge(eid, pid, Rel.CONTAINS)
            c.members[name][nm] = pid
        return [(eid, name)]
    if t in ("lexical_declaration", "variable_declaration"):
        out = []
        for d in n.named_children:
            if d.type != "variable_declarator":
                continue
            nn = d.child_by_field_name("name")
            if nn is None or nn.type != "identifier":
                continue
            name = _t(nn)
            val = d.child_by_field_name("value")
            if val is not None and _is_func_value(val):
                out.append((_func(c, name, val, mid, n), name))
            else:
                vid = f"ts:{c.path}#{name}"
                f.node(vid, Kind.FIELD, f"{c.stem}.{name}", n.start_point[0] + 1, n.end_point[0] + 1,
                       f"{words(name)} {_doc_before(n)} {_strings(val)}"[:1500], tsconst=True)
                f.edge(mid, vid, Rel.CONTAINS)
                c.local[name] = vid
                if val is not None:
                    c.bodies.append((vid, val, None))
                out.append((vid, name))
        return out
    return []


def _is_func_value(v) -> bool:
    """`() => …`, `function () {…}`, and wrappers such as `memo(() => …)`, `cache(async …)`,
    `forwardRef(…)` or `action.schema(s).action(async …)` around one."""
    while v is not None and v.type in ("as_expression", "satisfies_expression", "parenthesized_expression",
                                        "non_null_expression"):
        v = v.named_children[0] if v.named_children else None
    if v is None:
        return False
    if v.type in FUNC_TYPES:
        return True
    if v.type == "call_expression":
        args = v.child_by_field_name("arguments")
        return any(a.type in FUNC_TYPES for a in (args.named_children if args is not None else []))
    return False


def _func(c: _Ctx, name: str, n, mid: str, decl) -> str:
    f = c.f
    fid = f"ts:{c.path}#{name}"
    params = n.child_by_field_name("parameters")
    text = " ".join([words(name), _doc_before(decl), " ".join(words(_t(p)) for p in
                                                             (params.named_children if params is not None else []))[:200],
                     _strings(n, 3500)])
    component = name[:1].isupper() and _has_jsx(n)
    f.node(fid, Kind.TEST_METHOD if c.test else Kind.METHOD, f"{c.stem}.{name}()",
           decl.start_point[0] + 1, decl.end_point[0] + 1, text[:4000],
           component=component or None, server_action=_directive(n, "use server") or None)
    f.edge(mid, fid, Rel.CONTAINS)
    c.local[name] = fid
    c.bodies.append((fid, n, None))
    return fid


def _class(c: _Ctx, n, mid: str) -> str:
    f = c.f
    name = _t(n.child_by_field_name("name")) or "default"
    cid = f"ts:{c.path}#{name}"
    f.node(cid, Kind.TEST_CLASS if c.test else Kind.CLASS, name, n.start_point[0] + 1, n.end_point[0] + 1,
           f"{words(name)} {_doc_before(n)}"[:800], fqn=f"{c.mod}.{name}", tskind="class")
    f.edge(mid, cid, Rel.CONTAINS)
    c.local[name] = cid
    c.members[name] = {}
    implements = []
    for h in n.named_children:
        if h.type == "class_heritage":
            for clause in h.named_children:
                for x in clause.named_children:
                    nm = _t(x).split("<")[0]
                    if clause.type == "extends_clause":
                        _use(c, cid, nm, Rel.EXTENDS, x)
                    else:
                        implements.append((nm, x))
    body = n.child_by_field_name("body")
    for m in (body.named_children if body is not None else []):
        mn = m.child_by_field_name("name")
        if mn is None:
            continue
        nm = _t(mn)
        if m.type == "method_definition":
            mid2 = f"{cid}.{nm}"
            f.node(mid2, Kind.TEST_METHOD if c.test else Kind.METHOD, f"{name}.{nm}()", m.start_point[0] + 1,
                   m.end_point[0] + 1, f"{words(nm)} {words(name)} {_comment_near(m)} {_strings(m)}"[:2500])
            f.edge(cid, mid2, Rel.CONTAINS)
            c.members[name][nm] = mid2
            c.bodies.append((mid2, m, name))
            for iname, inode in implements:
                _implements(c, mid2, iname, nm, inode)
        elif m.type in ("public_field_definition", "property_signature"):
            pid = f"{cid}.{nm}"
            f.node(pid, Kind.FIELD, f"{name}.{nm}", m.start_point[0] + 1, m.end_point[0] + 1,
                   f"{words(nm)} {words(name)} {_comment_near(m)}"[:400])
            f.edge(cid, pid, Rel.CONTAINS)
            c.members[name][nm] = pid
            val = m.child_by_field_name("value")
            if val is not None:
                c.bodies.append((pid, val, name))
    for iname, inode in implements:
        _implements(c, cid, iname, None, inode)
    return cid


def _implements(c: _Ctx, src: str, iface: str, member: str | None, node):
    """`class A implements IRepo`: the interface (and each of its members) is IMPLEMENTED_BY
    the class (and the same-named member), so a change to the contract reaches every
    implementation and a change to an implementation reaches the contract."""
    target = iface if member is None else f"{iface}.{member}"
    line = node.start_point[0] + 1
    if iface in c.local:
        tid = c.local[iface] if member is None else c.members.get(iface, {}).get(member)
        if tid:
            c.f.edge(tid, src, Rel.IMPLEMENTED_BY, 1.0, "parser", line=line)
        return
    imp = c.imports.get(iface)
    if imp and imp[1] != "*":
        name = imp[1] if member is None else f"{imp[1]}.{member}"
        c.f.ref(src, c.key(imp[0], name), Rel.IMPLEMENTED_BY, 1.0, line, via="implements", optional=True,
                reverse=True)


def _props(body) -> list[tuple[str, object]]:
    out = []
    if body is None:
        return out
    stack = [body]
    while stack:
        x = stack.pop()
        if x.type in ("property_signature", "method_signature"):
            nm = x.child_by_field_name("name")
            if nm is not None:
                out.append((_str(nm) or _t(nm), x))
            continue                              # nested object types belong to the property
        if x.type in ("object_type", "interface_body", "union_type", "intersection_type", "parenthesized_type"):
            stack.extend(reversed(x.named_children))
    return out


def _comment_near(n) -> str:
    p = n.prev_named_sibling
    return _comment(_t(p))[:300] if p is not None and p.type == "comment" else ""


def _directive(n, word: str) -> bool:
    body = n.child_by_field_name("body")
    first = body.named_children[0] if body is not None and body.named_children else None
    return first is not None and first.type == "expression_statement" and word in _t(first)[:20]


def _has_jsx(n) -> bool:
    stack = [n]
    while stack:
        x = stack.pop()
        if x.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
            return True
        stack.extend(x.named_children)
    return False


def _strings(n, limit: int = 1500) -> str:
    """Comments, string literals and JSX text inside a node: the words users and
    reviewers see, often Japanese — what a Japanese requirement can be matched on."""
    if n is None:
        return ""
    out, size = [], 0
    stack = [n]
    while stack and size < limit:
        x = stack.pop()
        t = x.type
        if t == "comment":
            s = _comment(_t(x))
        elif t in ("string_fragment", "jsx_text"):
            s = _t(x).strip()
            if not JP.search(s) and (len(s) < 3 or len(s) > 80 or s.isupper() or
                                     any(ch in tok for tok in s.split() for ch in "-:/[]#.=")):
                s = ""                     # CSS classes, paths, constants: noise for search
        else:
            s = ""
            stack.extend(reversed(x.children))
        if s:
            out.append(s)
            size += len(s)
    return " ".join(out)[:limit]


# --------------------------------------------------------------------------- bodies
def _use(c: _Ctx, src: str, name: str, rel: str, node, conf: float = 1.0):
    """A use of `name` (call, component, type, value) from element `src`."""
    if c.test and rel in (Rel.CALLS, Rel.REFERENCES, Rel.RENDERS):
        rel = Rel.TESTS
    line = node.start_point[0] + 1
    if name in c.local:
        if c.local[name] != src:
            c.f.edge(src, c.local[name], rel, conf, "parser", line=line)
        return
    imp = c.imports.get(name)
    if imp:
        spec, imported = imp
        if imported == "*":
            return                         # namespace object itself: members are resolved at use
        c.f.ref(src, c.key(spec, imported), rel, conf, line, via="import", optional=not spec.startswith("."))


def _body(c: _Ctx, nid: str, body, cls: str | None):
    if body is None:
        return
    seen = set()
    stack = [body]
    while stack:
        x = stack.pop()
        t = x.type
        if t == "call_expression" or t == "new_expression":
            fn = x.child_by_field_name("function") or x.child_by_field_name("constructor")
            _call(c, nid, x, fn, cls, seen)
        elif t in ("jsx_opening_element", "jsx_self_closing_element"):
            nm = x.child_by_field_name("name")
            if nm is not None:
                s = _t(nm)
                if s[:1].isupper() and "." not in s:
                    if ("C", s) not in seen:
                        seen.add(("C", s))
                        _use(c, nid, s, Rel.RENDERS, x)
                elif "." in s:
                    _member(c, nid, s.split(".")[0], s.split(".")[-1], x, Rel.RENDERS, seen)
        elif t == "type_identifier":
            s = _t(x)
            if ("T", s) not in seen and (s in c.local or s in c.imports):
                seen.add(("T", s))
                _use(c, nid, s, Rel.REFERENCES, x)
        elif t == "identifier":
            s = _t(x)
            p = x.parent
            if p is not None and p.type in ("call_expression",) and p.child_by_field_name("function") == x:
                pass                                              # handled as a call
            elif ("V", s) not in seen and (s in c.local or (s in c.imports and c.imports[s][1] != "*")):
                seen.add(("V", s))
                _use(c, nid, s, Rel.REFERENCES, x, 0.9)
        elif t in ("string", "template_string"):
            s = _str(x)
            if s and re.match(r"^/(api|v\d)/[\w\-/{}:.]*$", s):
                c.f.ref(nid, f"url:{s.split('?')[0]}", Rel.TESTS if c.test else Rel.REFERENCES, 0.85,
                        x.start_point[0] + 1, via="literal", optional=True)
        stack.extend(reversed(x.named_children))          # closures belong to the function that holds them


def _call(c: _Ctx, nid: str, call, fn, cls: str | None, seen: set):
    if fn is None:
        return
    line = call.start_point[0] + 1
    if fn.type == "identifier":
        s = _t(fn)
        if ("F", s) not in seen:
            seen.add(("F", s))
            _use(c, nid, s, Rel.CALLS, fn)
        return
    if fn.type != "member_expression":
        return
    prop = _t(fn.child_by_field_name("property"))
    obj = fn.child_by_field_name("object")
    # Supabase: client.from("table")....select / insert / update / delete / upsert
    if prop == "from":
        args = call.child_by_field_name("arguments")
        a0 = args.named_children[0] if args is not None and args.named_children else None
        table = _str(a0)
        if table and re.fullmatch(r"[A-Za-z_][\w.]*", table):
            ops = _chain_ops(call)
            rel = Rel.WRITES if ops & WRITE_OPS else Rel.READS
            c.f.ref(nid, f"table:{table.split('.')[-1]}", rel, 0.95, line, via="supabase",
                    ops="".join(sorted({"insert": "C", "update": "U", "upsert": "U", "delete": "D"}.get(o, "R")
                                       for o in (ops or {"select"}))))
            return
    if prop == "rpc":
        args = call.child_by_field_name("arguments")
        a0 = args.named_children[0] if args is not None and args.named_children else None
        fname = _str(a0)
        if fname:
            c.f.ref(nid, f"proc:{fname}", Rel.CALLS, 0.95, line, via="supabase", optional=True)
            return
    # Prisma: prisma.model.op(...) / tx.model.op(...)
    if prop in PRISMA_OPS and obj is not None and obj.type == "member_expression":
        model = _t(obj.child_by_field_name("property"))
        base = _t(obj.child_by_field_name("object"))
        if model and model[:1].islower() and re.search(r"(prisma|tx|db|client|trx)$", base, re.I):
            rel = Rel.WRITES if prop in WRITE_OPS else Rel.READS
            c.f.ref(nid, f"prisma:{model}", rel, 0.95, line, via="prisma",
                    ops={"create": "C", "createMany": "C", "update": "U", "updateMany": "U", "upsert": "U",
                         "delete": "D", "deleteMany": "D"}.get(prop, "R"))
            return
    if obj is None:
        return
    if obj.type == "this" and cls:
        target = c.members.get(cls, {}).get(prop)
        if target and target != nid:
            c.f.edge(nid, target, Rel.TESTS if c.test else Rel.CALLS, 1.0, "parser", line=line)
        return
    if obj.type == "identifier":
        _member(c, nid, _t(obj), prop, fn, Rel.CALLS, seen)


def _member(c: _Ctx, nid: str, obj: str, prop: str, node, rel: str, seen: set):
    """`ns.fn()` on a namespace import, `Cls.staticFn()` / `obj.fn()` on an imported or local symbol."""
    if ("M", obj, prop) in seen:
        return
    seen.add(("M", obj, prop))
    if c.test and rel in (Rel.CALLS, Rel.RENDERS):
        rel = Rel.TESTS
    line = node.start_point[0] + 1
    imp = c.imports.get(obj)
    if imp and imp[1] == "*":
        c.f.ref(nid, c.key(imp[0], prop), rel, 1.0, line, via="import", optional=not imp[0].startswith("."))
        return
    if obj in c.members and prop in c.members[obj]:
        c.f.edge(nid, c.members[obj][prop], rel, 1.0, "parser", line=line)
        return
    if obj in c.local or obj in c.imports:
        _use(c, nid, obj, Rel.REFERENCES, node, 0.9)
        if imp:
            c.f.ref(nid, c.key(imp[0], f"{imp[1]}.{prop}"), rel, 0.9, line, via="import", optional=True)


def _chain_ops(call) -> set[str]:
    """Method names called on the result of `.from(...)`: `.select().eq()`, `.update({...}).eq()`."""
    ops = set()
    p = call.parent
    while p is not None and p.type in ("member_expression", "call_expression", "await_expression",
                                       "parenthesized_expression"):
        if p.type == "member_expression":
            ops.add(_t(p.child_by_field_name("property")))
        p = p.parent
    return ops & (WRITE_OPS | {"select"})


def _tests(c: _Ctx, root, mid: str):
    """`it("…", () => …)` / `test(…)` blocks as test methods named by their description."""
    stack = [(root, "")]
    n_tests = 0
    while stack and n_tests < 400:
        x, prefix = stack.pop()
        if x.type == "call_expression":
            fn = x.child_by_field_name("function")
            fname = _t(fn).split(".")[0] if fn is not None else ""
            args = x.child_by_field_name("arguments")
            a = args.named_children if args is not None else []
            title = _str(a[0]) if a else None
            body = next((y for y in a if y.type in FUNC_TYPES), None)
            if fname in ("describe",) and title and body is not None:
                stack.append((body, f"{prefix}{title} > "))
                continue
            if fname in TEST_CALLS and title and body is not None:
                n_tests += 1
                tid = f"ts:{c.path}#test{n_tests}"
                c.f.node(tid, Kind.TEST_METHOD, f"{c.stem} {prefix}{title}"[:200], x.start_point[0] + 1,
                         x.end_point[0] + 1, f"{prefix}{title} {_strings(body, 600)}"[:1200])
                c.f.edge(mid, tid, Rel.CONTAINS)
                _body(c, tid, body, None)
                continue
        stack.extend((y, prefix) for y in reversed(x.named_children))
    if n_tests == 0:
        _body(c, mid, root, None)              # a test file without it/test blocks: the file tests what it uses
