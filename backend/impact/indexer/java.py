"""Java indexer built on tree-sitter.

Two passes: the first collects declarations (classes, fields, methods, constants,
annotations) from every file; the second resolves calls, type references, field
accesses, Spring endpoints, JPA mappings and Spring Data derived queries against
the declarations found in the repository.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import tree_sitter_java
from tree_sitter import Language, Parser

from ..model import Edge, Kind, Node, Rel
from .text import singular, snake, split_ident, words

JAVA = Language(tree_sitter_java.language())

MAPPING_ANN = {"GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
               "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": "ANY"}
RELATION_ANN = {"OneToMany", "ManyToOne", "ManyToMany", "OneToOne"}
BRANCH_TYPES = {"if_statement", "for_statement", "enhanced_for_statement", "while_statement",
                "do_statement", "catch_clause", "switch_label", "ternary_expression"}
QUERY_SUFFIXES = ("StartingWith", "EndingWith", "Containing", "IgnoreCase", "GreaterThan",
                  "LessThan", "Between", "Like", "In", "IsNull", "NotNull", "OrderBy", "Not")


def _t(node) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _base_type(type_text: str) -> str:
    """'Page<Owner>' -> 'Page', 'org.x.Owner[]' -> 'Owner'"""
    t = re.sub(r"<.*>", "", type_text).replace("[]", "").strip()
    return t.split(".")[-1]


def _type_args(type_text: str) -> list[str]:
    m = re.search(r"<(.*)>", type_text)
    if not m:
        return []
    return [_base_type(a) for a in re.split(r",\s*(?![^<]*>)", m.group(1)) if a.strip()]


@dataclass
class Ann:
    name: str
    args: dict[str, str]          # 'value' for the unnamed argument; values keep quotes stripped
    raw: str

    def get(self, key: str = "value") -> str | None:
        return self.args.get(key)


@dataclass
class FieldInfo:
    name: str
    type: str
    anns: list[Ann]
    line: int
    end_line: int
    constant: str | None = None   # string literal value of static final constants
    comment: str = ""


@dataclass
class MethodInfo:
    name: str
    params: list[tuple[str, str]]
    ret: str
    anns: list[Ann]
    line: int
    end_line: int
    body: object = None
    complexity: int = 1
    loc: int = 0
    comment: str = ""
    is_ctor: bool = False

    @property
    def sig(self) -> str:
        return f"{self.name}({','.join(_base_type(t) for t, _ in self.params)})"


@dataclass
class ClassInfo:
    fqn: str
    name: str
    package: str
    file: str
    line: int
    end_line: int
    kind: str                      # class | interface | enum | record
    anns: list[Ann]
    extends: list[str]             # raw type texts
    implements: list[str]
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    methods: list[MethodInfo] = field(default_factory=list)
    is_test: bool = False
    comment: str = ""

    @property
    def id(self) -> str:
        return f"java:{self.fqn}"

    def ann(self, name: str) -> Ann | None:
        return next((a for a in self.anns if a.name == name), None)


def _strip_comment(c: str) -> str:
    c = re.sub(r"^/\*\*?|\*/$", "", c.strip())
    c = re.sub(r"^\s*\*\s?", "", c, flags=re.M)
    c = re.sub(r"^//\s?", "", c, flags=re.M)
    return re.sub(r"\s+", " ", c).strip()


def _leading_comment(node) -> str:
    prev = node.prev_named_sibling
    parts = []
    while prev is not None and prev.type in ("block_comment", "line_comment"):
        parts.insert(0, _strip_comment(_t(prev)))
        prev = prev.prev_named_sibling
    return " ".join(parts)


def _string_value(node) -> str | None:
    if node is None:
        return None
    if node.type == "string_literal":
        return "".join(_t(c) for c in node.named_children if c.type in ("string_fragment", "escape_sequence"))
    return None


def _annotations(modifiers) -> list[Ann]:
    out = []
    if modifiers is None:
        return out
    for c in modifiers.named_children:
        if c.type not in ("annotation", "marker_annotation"):
            continue
        name = _t(c.child_by_field_name("name")).split(".")[-1]
        args: dict[str, str] = {}
        al = c.child_by_field_name("arguments")
        if al is not None:
            for a in al.named_children:
                if a.type == "element_value_pair":
                    k = _t(a.child_by_field_name("key"))
                    v = a.child_by_field_name("value")
                    args[k] = _string_value(v) if _string_value(v) is not None else _t(v)
                else:
                    sv = _string_value(a)
                    args["value"] = sv if sv is not None else _t(a)
        out.append(Ann(name, args, _t(c)))
    return out


def _complexity(body) -> int:
    if body is None:
        return 1
    n = 1
    stack = [body]
    while stack:
        cur = stack.pop()
        if cur.type in BRANCH_TYPES:
            n += 1
        elif cur.type == "binary_expression":
            op = cur.child_by_field_name("operator")
            if op is not None and _t(op) in ("&&", "||"):
                n += 1
        stack.extend(cur.named_children)
    return n


class JavaIndexer:
    def __init__(self, repo: str):
        self.repo = repo
        self.parser = Parser(JAVA)
        self.classes: dict[str, ClassInfo] = {}       # fqn -> info
        self.by_name: dict[str, list[ClassInfo]] = {}  # simple name -> infos
        self.sources: dict[str, bytes] = {}
        self.nodes: list[Node] = []
        self.node_by_id: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.refs_by_src: dict[str, set] = {}
        self.endpoints: list[Node] = []
        self.template_refs: list[tuple[str, str, int]] = []   # (method id, template name, line)
        self.message_refs: list[tuple[str, str, int]] = []    # (node id, message key, line)

    def add_node(self, n: Node) -> None:
        self.nodes.append(n)
        self.node_by_id[n.id] = n

    def add_edge(self, e: Edge) -> None:
        self.edges.append(e)
        if e.rel == Rel.REFERENCES:
            self.refs_by_src.setdefault(e.src, set()).add(e.dst)

    # ---------------------------------------------------------------- pass 1
    def collect(self, files: list[str]) -> None:
        for rel in files:
            src = open(os.path.join(self.repo, rel), "rb").read()
            self.sources[rel] = src
            tree = self.parser.parse(src)
            root = tree.root_node
            pkg = ""
            for c in root.named_children:
                if c.type == "package_declaration":
                    pkg = _t(c).replace("package", "").replace(";", "").strip()
            is_test = "/src/test/" in "/" + rel or rel.startswith("src/test/")
            for c in root.named_children:
                self._collect_type(c, pkg, rel, is_test, outer=None)

    def _collect_type(self, node, pkg, rel, is_test, outer):
        kinds = {"class_declaration": "class", "interface_declaration": "interface",
                 "enum_declaration": "enum", "record_declaration": "record"}
        if node.type not in kinds:
            return
        name = _t(node.child_by_field_name("name"))
        fqn = f"{outer.fqn}.{name}" if outer else (f"{pkg}.{name}" if pkg else name)
        mods = next((c for c in node.named_children if c.type == "modifiers"), None)
        extends, implements = [], []
        sc = node.child_by_field_name("superclass")
        if sc is not None:
            extends.append(_t(sc).replace("extends", "").strip())
        si = node.child_by_field_name("interfaces")
        if si is not None:
            implements += [_t(t) for t in si.named_children[0].named_children] if si.named_children else []
        for c in node.named_children:
            if c.type == "extends_interfaces":
                tl = c.named_children[0] if c.named_children else None
                if tl is not None:
                    extends += [_t(t) for t in tl.named_children]
        info = ClassInfo(fqn=fqn, name=name, package=pkg, file=rel,
                         line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                         kind=kinds[node.type], anns=_annotations(mods), extends=extends,
                         implements=implements, is_test=is_test, comment=_leading_comment(node))
        body = node.child_by_field_name("body")
        if body is not None:
            for m in body.named_children:
                if m.type == "field_declaration" or m.type == "constant_declaration":
                    self._collect_field(info, m)
                elif m.type in ("method_declaration", "constructor_declaration"):
                    self._collect_method(info, m)
                elif m.type in kinds:
                    self._collect_type(m, pkg, rel, is_test, outer=info)
        self.classes[fqn] = info
        self.by_name.setdefault(name, []).append(info)

    def _collect_field(self, info: ClassInfo, node):
        mods = next((c for c in node.named_children if c.type == "modifiers"), None)
        anns = _annotations(mods)
        ftype = _t(node.child_by_field_name("type"))
        is_const = mods is not None and "static" in _t(mods) and "final" in _t(mods)
        for d in node.named_children:
            if d.type != "variable_declarator":
                continue
            fname = _t(d.child_by_field_name("name"))
            const = _string_value(d.child_by_field_name("value")) if is_const else None
            info.fields[fname] = FieldInfo(fname, ftype, anns, node.start_point[0] + 1,
                                           node.end_point[0] + 1, const, _leading_comment(node))

    def _collect_method(self, info: ClassInfo, node):
        mods = next((c for c in node.named_children if c.type == "modifiers"), None)
        params = []
        fp = node.child_by_field_name("parameters")
        if fp is not None:
            for p in fp.named_children:
                if p.type in ("formal_parameter", "spread_parameter"):
                    pt = p.child_by_field_name("type")
                    pn = p.child_by_field_name("name")
                    if pt is None:  # spread_parameter
                        pt = next((c for c in p.named_children if "type" in c.type), None)
                        pn = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                        pn = pn.child_by_field_name("name") if pn is not None else None
                    params.append((_t(pt), _t(pn)))
        body = node.child_by_field_name("body")
        is_ctor = node.type == "constructor_declaration"
        m = MethodInfo(name=_t(node.child_by_field_name("name")), params=params,
                       ret="" if is_ctor else _t(node.child_by_field_name("type")),
                       anns=_annotations(mods), line=node.start_point[0] + 1,
                       end_line=node.end_point[0] + 1, body=body, complexity=_complexity(body),
                       loc=node.end_point[0] - node.start_point[0] + 1,
                       comment=_leading_comment(node), is_ctor=is_ctor)
        info.methods.append(m)

    # ---------------------------------------------------------------- helpers
    def resolve_class(self, type_text: str, ctx: ClassInfo | None = None) -> ClassInfo | None:
        base = _base_type(type_text)
        cands = self.by_name.get(base)
        if not cands:
            return None
        if len(cands) == 1 or ctx is None:
            return cands[0]
        same_pkg = [c for c in cands if c.package == ctx.package]
        return (same_pkg or cands)[0]

    def ancestors(self, info: ClassInfo) -> list[ClassInfo]:
        out, seen, queue = [], {info.fqn}, list(info.extends) + list(info.implements)
        while queue:
            t = queue.pop(0)
            c = self.resolve_class(t, info)
            if c and c.fqn not in seen:
                seen.add(c.fqn); out.append(c)
                queue += c.extends + c.implements
        return out

    def all_fields(self, info: ClassInfo) -> dict[str, tuple[ClassInfo, FieldInfo]]:
        out = {}
        for c in reversed([info] + self.ancestors(info)):
            for n, f in c.fields.items():
                out[n] = (c, f)
        return out

    def find_methods(self, info: ClassInfo, name: str, arity: int | None) -> list[tuple[ClassInfo, MethodInfo]]:
        res = []
        for c in [info] + self.ancestors(info):
            for m in c.methods:
                if m.name == name and not m.is_ctor and (arity is None or len(m.params) == arity):
                    res.append((c, m))
            if res:
                return res
        if arity is not None:
            return self.find_methods(info, name, None)
        return res

    @staticmethod
    def mid(c: ClassInfo, m: MethodInfo) -> str:
        return f"{c.id}#{m.sig}"

    @staticmethod
    def fid(c: ClassInfo, f: FieldInfo | str) -> str:
        return f"{c.id}.{f if isinstance(f, str) else f.name}"

    def entity_table(self, info: ClassInfo) -> str | None:
        if not info.ann("Entity"):
            return None
        t = info.ann("Table")
        return (t.get("name") or t.get("value")) if t and (t.get("name") or t.get("value")) else snake(info.name)

    # ---------------------------------------------------------------- pass 2
    def build(self) -> None:
        for info in self.classes.values():
            self._emit_class(info)
        for info in self.classes.values():
            self._emit_jpa(info)
            for m in info.methods:
                self._analyse_method(info, m)
            self._emit_repository(info)
        self._emit_test_links()
        self._link_test_literals()

    def _emit_class(self, info: ClassInfo):
        kind = Kind.TEST_CLASS if info.is_test else Kind.CLASS
        anns = [a.name for a in info.anns]
        text = " ".join([words(info.name), info.comment, " ".join(words(a) for a in anns)])
        self.add_node(Node(info.id, kind, info.name, info.file, info.line, info.end_line, text,
                               {"fqn": info.fqn, "annotations": anns, "type": info.kind,
                                "package": info.package}))
        for t in info.extends + info.implements:
            sup = self.resolve_class(t, info)
            if sup:
                self.add_edge(Edge(info.id, sup.id, Rel.EXTENDS))
            for arg in _type_args(t):
                ac = self.resolve_class(arg, info)
                if ac:
                    self.add_edge(Edge(info.id, ac.id, Rel.REFERENCES, {"via": "type-arg"}))
        for f in info.fields.values():
            fnode_id = self.fid(info, f)
            fanns = [a.name for a in f.anns]
            text = " ".join([words(f.name), words(_base_type(f.type)), f.comment, f.constant or ""])
            props = {"type": f.type, "annotations": fanns,
                     "validations": [a.raw for a in f.anns if a.name not in ("Column", "Id")]}
            if f.constant is not None:
                props["constant"] = f.constant
            self.add_node(Node(fnode_id, Kind.FIELD, f"{info.name}.{f.name}",
                                   info.file, f.line, f.end_line, text, props))
            self.add_edge(Edge(info.id, fnode_id, Rel.CONTAINS))
            ft = self.resolve_class(f.type, info)
            if ft:
                self.add_edge(Edge(fnode_id, ft.id, Rel.REFERENCES))
            for arg in _type_args(f.type):
                ac = self.resolve_class(arg, info)
                if ac:
                    self.add_edge(Edge(fnode_id, ac.id, Rel.REFERENCES))
            for a in f.anns:
                for v in a.args.values():
                    for key in re.findall(r"\{([\w.]+)\}", v or ""):
                        self.message_refs.append((fnode_id, key, f.line))
        for m in info.methods:
            kind = Kind.TEST_METHOD if info.is_test else Kind.METHOD
            mid = self.mid(info, m)
            text = " ".join([words(m.name), m.comment, " ".join(words(a.name) for a in m.anns)])
            self.add_node(Node(mid, kind, f"{info.name}.{m.name}()" if not m.is_ctor else f"{info.name}()",
                                   info.file, m.line, m.end_line, text,
                                   {"complexity": m.complexity, "loc": m.loc, "signature": m.sig,
                                    "annotations": [a.name for a in m.anns], "ctor": m.is_ctor}))
            self.add_edge(Edge(info.id, mid, Rel.CONTAINS))

    def _emit_jpa(self, info: ClassInfo):
        table = self.entity_table(info)
        if not table:
            return
        tid = f"table:{table}"
        self.add_edge(Edge(info.id, tid, Rel.MAPS_TO))
        for fname, (owner, f) in self.all_fields(info).items():
            names = {a.name for a in f.anns}
            if "Transient" in names or (f.constant is not None):
                continue
            jc = next((a for a in f.anns if a.name == "JoinColumn"), None)
            if names & RELATION_ANN:
                if jc and jc.get("name"):
                    if "OneToMany" in names:
                        args = _type_args(f.type)
                        target = self.resolve_class(args[0], info) if args else None
                        ttab = self.entity_table(target) if target else None
                        if ttab:
                            self.add_edge(Edge(self.fid(owner, f), f"col:{ttab}.{jc.get('name')}",
                                                   Rel.MAPS_TO, {"via": info.name}))
                    else:
                        self.add_edge(Edge(self.fid(owner, f), f"col:{table}.{jc.get('name')}",
                                               Rel.MAPS_TO, {"via": info.name}))
                jt = next((a for a in f.anns if a.name == "JoinTable"), None)
                if jt and jt.get("name"):
                    self.add_edge(Edge(self.fid(owner, f), f"table:{jt.get('name')}", Rel.MAPS_TO))
                continue
            col = next((a for a in f.anns if a.name == "Column"), None)
            cname = (col.get("name") if col and col.get("name") else None) or (
                "id" if fname == "id" else snake(fname))
            self.add_edge(Edge(self.fid(owner, f), f"col:{table}.{cname}", Rel.MAPS_TO,
                                   {"via": info.name}))

    def _emit_repository(self, info: ClassInfo):
        """Spring Data: `findByLastNameStartingWith` references Owner.lastName."""
        entity = None
        for t in info.extends:
            if _base_type(t).endswith("Repository") and _type_args(t):
                entity = self.resolve_class(_type_args(t)[0], info)
        if not entity:
            return
        fields = self.all_fields(entity)
        for m in info.methods:
            mm = re.match(r"(?:find|read|get|query|count|exists|delete)\w*?By(\w+)", m.name)
            mid = self.mid(info, m)
            self.add_edge(Edge(mid, entity.id, Rel.REFERENCES, {"via": "repository"}))
            if not mm:
                continue
            expr = re.split(r"OrderBy", mm.group(1))[0]
            for part in re.split(r"And|Or", expr):
                for suf in QUERY_SUFFIXES:
                    if part.endswith(suf) and part != suf:
                        part = part[: -len(suf)]
                fname = part[:1].lower() + part[1:]
                if fname in fields:
                    owner, f = fields[fname]
                    self.add_edge(Edge(mid, self.fid(owner, f), Rel.REFERENCES, {"via": "derived-query"}))
            q = next((a for a in m.anns if a.name == "Query"), None)
            if q and q.get("value"):
                for tok in re.findall(r"\b(?:from|join)\s+(\w+)", q.get("value"), re.I):
                    c = self.resolve_class(tok, info)
                    if c:
                        self.add_edge(Edge(mid, c.id, Rel.REFERENCES, {"via": "query"}))

    def _endpoint(self, info: ClassInfo, m: MethodInfo):
        base = ""
        cm = info.ann("RequestMapping")
        if cm:
            base = cm.get("value") or cm.get("path") or ""
        for a in m.anns:
            if a.name in MAPPING_ANN:
                verb = MAPPING_ANN[a.name]
                if verb == "ANY" and a.get("method"):
                    verb = a.get("method").split(".")[-1]
                paths = a.get("value") or a.get("path") or ""
                plist = re.findall(r'"([^"]*)"', paths) or [paths]
                for p in plist:
                    path = (base.rstrip("/") + "/" + p.lstrip("/")) if p else (base or "/")
                    eid = f"ep:{verb} {path}"
                    node = Node(eid, Kind.ENDPOINT, f"{verb} {path}", info.file, m.line, m.end_line,
                                " ".join([verb.lower(), words(path), words(m.name)]),
                                {"verb": verb, "path": path, "package": info.package})
                    self.add_node(node)
                    self.endpoints.append(node)
                    self.add_edge(Edge(eid, self.mid(info, m), Rel.HANDLES))

    def _analyse_method(self, info: ClassInfo, m: MethodInfo):
        mid = self.mid(info, m)
        self._endpoint(info, m)
        for a in m.anns:
            for v in a.args.values():
                for key in re.findall(r"\{([\w.]+)\}", v or ""):
                    self.message_refs.append((mid, key, m.line))
        if m.body is None:
            return
        fields = self.all_fields(info)
        local_types: dict[str, str] = {n: t for t, n in m.params}
        literals: list[tuple[str, int]] = []
        numbers: list[str] = []
        texts: list[str] = []
        stack = [m.body]
        invocations = []
        while stack:
            cur = stack.pop()
            t = cur.type
            if t == "local_variable_declaration":
                vt = _t(cur.child_by_field_name("type"))
                for d in cur.named_children:
                    if d.type == "variable_declarator":
                        name = _t(d.child_by_field_name("name"))
                        if vt == "var":
                            val = d.child_by_field_name("value")
                            if val is not None and val.type == "object_creation_expression":
                                vt = _t(val.child_by_field_name("type"))
                        local_types[name] = vt
                        texts.append(words(name))
            elif t == "enhanced_for_statement":
                vt = _t(cur.child_by_field_name("type"))
                local_types[_t(cur.child_by_field_name("name"))] = vt
            elif t == "method_invocation":
                invocations.append(cur)
            elif t == "string_literal":
                sv = _string_value(cur)
                if sv:
                    literals.append((sv, cur.start_point[0] + 1))
            elif t in ("decimal_integer_literal", "decimal_floating_point_literal"):
                numbers.append(_t(cur))
            elif t in ("line_comment", "block_comment"):
                texts.append(_strip_comment(_t(cur)))
            elif t == "type_identifier":
                c = self.resolve_class(_t(cur), info)
                if c and c.fqn != info.fqn:
                    self.add_edge(Edge(mid, c.id, Rel.REFERENCES, {"line": cur.start_point[0] + 1}))
            elif t == "object_creation_expression":
                c = self.resolve_class(_t(cur.child_by_field_name("type")), info)
                if c:
                    for cc, cm in [(c, x) for x in c.methods if x.is_ctor] or []:
                        self.add_edge(Edge(mid, self.mid(cc, cm), Rel.CALLS))
            elif t == "field_access":
                obj = cur.child_by_field_name("object")
                fname = _t(cur.child_by_field_name("field"))
                if obj is not None and obj.type == "this" and fname in fields:
                    owner, f = fields[fname]
                    self.add_edge(Edge(mid, self.fid(owner, f), Rel.REFERENCES,
                                           {"line": cur.start_point[0] + 1}))
            elif t == "identifier":
                name = _t(cur)
                parent = cur.parent
                if name in fields and name not in local_types and parent is not None and \
                        parent.type not in ("method_invocation", "field_access"):
                    owner, f = fields[name]
                    self.add_edge(Edge(mid, self.fid(owner, f), Rel.REFERENCES,
                                           {"line": cur.start_point[0] + 1}))
                    if f.constant is not None:
                        literals.append((f.constant, cur.start_point[0] + 1))
                elif name.isupper() or "_" in name:
                    # static constant of another class: Foo.BAR handled via field_access below
                    pass
            stack.extend(cur.named_children)

        for inv in invocations:
            self._resolve_call(info, m, mid, inv, local_types, fields)

        # string literals: templates, i18n keys, field names of referenced types, URLs
        referenced = self.refs_by_src.get(mid, set())
        ref_classes = [c for c in self.classes.values() if c.id in referenced] + [info]
        for lit, line in literals:
            if re.fullmatch(r"[\w\-/]+", lit) and "/" in lit and not lit.startswith("/"):
                self.template_refs.append((mid, lit, line))
            elif re.fullmatch(r"[a-z]\w*", lit):
                for c in ref_classes:
                    flds = self.all_fields(c)
                    if lit in flds:
                        owner, f = flds[lit]
                        self.add_edge(Edge(mid, self.fid(owner, f), Rel.REFERENCES,
                                               {"via": "literal", "line": line}))
                        break
        node = self.node_by_id[mid]
        node.text += " " + " ".join(texts) + " " + " ".join(l for l, _ in literals if len(l) < 80)
        node.props["literals"] = [l for l, _ in literals][:40]
        node.props["numbers"] = sorted(set(numbers))[:40]
        node.text += " " + " ".join(sorted(set(numbers)))

    def _receiver_type(self, info, obj, local_types, fields, depth=0) -> str | None:
        if obj is None or obj.type == "this":
            return info.name
        if obj.type == "identifier":
            name = _t(obj)
            if name in local_types:
                return local_types[name]
            if name in fields:
                return fields[name][1].type
            if name in self.by_name:
                return name
            return None
        if obj.type == "field_access":
            inner = obj.child_by_field_name("object")
            fname = _t(obj.child_by_field_name("field"))
            if inner is not None and inner.type == "this" and fname in fields:
                return fields[fname][1].type
            it = self._receiver_type(info, inner, local_types, fields, depth + 1)
            c = self.resolve_class(it, info) if it else None
            if c:
                f = self.all_fields(c).get(fname)
                return f[1].type if f else None
            return None
        if obj.type == "method_invocation" and depth < 4:
            rt = self._receiver_type(info, obj.child_by_field_name("object"), local_types, fields, depth + 1)
            c = self.resolve_class(rt, info) if rt else None
            if c:
                name = _t(obj.child_by_field_name("name"))
                args = obj.child_by_field_name("arguments")
                ms = self.find_methods(c, name, len(args.named_children) if args is not None else 0)
                if ms:
                    ret = ms[0][1].ret
                    # unwrap Optional<T>, List<T>, Collection<T>, Page<T>
                    if not self.resolve_class(ret, info) and _type_args(ret):
                        return _type_args(ret)[0]
                    return ret
            if rt and _type_args(rt):
                return _type_args(rt)[0]
            return None
        if obj.type == "object_creation_expression":
            return _t(obj.child_by_field_name("type"))
        if obj.type == "parenthesized_expression" and obj.named_children:
            return self._receiver_type(info, obj.named_children[0], local_types, fields, depth + 1)
        return None

    def _resolve_call(self, info, m, mid, inv, local_types, fields):
        name = _t(inv.child_by_field_name("name"))
        args = inv.child_by_field_name("arguments")
        arity = len(args.named_children) if args is not None else 0
        obj = inv.child_by_field_name("object")
        rtype = self._receiver_type(info, obj, local_types, fields)
        target = self.resolve_class(rtype, info) if rtype else None
        line = inv.start_point[0] + 1
        if target is None:
            return
        found = self.find_methods(target, name, arity)
        if found:
            for c, tm in found[:3]:
                self.add_edge(Edge(mid, self.mid(c, tm), Rel.CALLS, {"line": line}))
        else:
            # inherited from a library type (e.g. JpaRepository.save): keep a class-level link
            self.add_edge(Edge(mid, target.id, Rel.REFERENCES, {"line": line, "call": name}))
        # accessor -> field link (getTelephone -> telephone) even when the accessor is generated
        am = re.match(r"(get|set|is|has)([A-Z]\w*)", name)
        if am:
            fname = am.group(2)[:1].lower() + am.group(2)[1:]
            flds = self.all_fields(target)
            if fname in flds:
                owner, f = flds[fname]
                self.add_edge(Edge(mid, self.fid(owner, f), Rel.REFERENCES,
                                       {"via": "accessor", "line": line}))

    def _emit_test_links(self):
        for info in self.classes.values():
            if not info.is_test:
                continue
            base = re.sub(r"(Integration)?Tests?$", "", info.name)
            target = next((c for c in self.by_name.get(base, []) if not c.is_test), None)
            if target:
                self.add_edge(Edge(info.id, target.id, Rel.TESTS, {"via": "naming"}))
            for m in info.methods:
                mid = self.mid(info, m)
                if any(a.name in ("Test", "ParameterizedTest", "RepeatedTest") for a in m.anns):
                    node = self.node_by_id[mid]
                    node.props["is_test"] = True

    def _link_test_literals(self):
        """`param("telephone", ...)` in a test refers to the telephone field of the
        entities that the test class works with (anywhere in the class)."""
        for info in self.classes.values():
            if not info.is_test:
                continue
            classes = set()
            for m in info.methods:
                classes |= self.refs_by_src.get(self.mid(info, m), set())
            for e in self.edges:
                if e.src == info.id and e.rel == Rel.TESTS:
                    classes.add(e.dst)
                    classes |= {d for d in self.refs_by_src.get(e.dst, set())}
                    tgt = next((c for c in self.classes.values() if c.id == e.dst), None)
                    if tgt:
                        for tm in tgt.methods:
                            classes |= self.refs_by_src.get(self.mid(tgt, tm), set())
            infos = [c for c in self.classes.values() if c.id in classes and not c.is_test]
            for m in info.methods:
                mid = self.mid(info, m)
                for lit in self.node_by_id[mid].props.get("literals", []):
                    if not re.fullmatch(r"[a-z]\w*", lit):
                        continue
                    for c in infos:
                        flds = self.all_fields(c)
                        if lit in flds and flds[lit][1].constant is None:
                            owner, f = flds[lit]
                            self.add_edge(Edge(mid, self.fid(owner, f), Rel.REFERENCES, {"via": "test-literal"}))
                            break

    def link_endpoint_literals(self):
        """Tests that call `get("/owners/{id}/edit")` exercise that endpoint."""
        patterns = []
        for ep in self.endpoints:
            rx = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(ep.props["path"])) + "/?$"
            patterns.append((re.compile(rx), ep))
        for n in self.nodes:
            if n.kind not in (Kind.TEST_METHOD, Kind.METHOD):
                continue
            for lit in n.props.get("literals", []):
                if not lit.startswith("/"):
                    continue
                for rx, ep in patterns:
                    if rx.match(lit):
                        rel = Rel.TESTS if n.kind == Kind.TEST_METHOD else Rel.REFERENCES
                        self.add_edge(Edge(n.id, ep.id, rel, {"via": "url"}))


def index_java(repo: str, files: list[str]) -> JavaIndexer:
    ix = JavaIndexer(repo)
    ix.collect(files)
    ix.build()
    ix.link_endpoint_literals()
    return ix
