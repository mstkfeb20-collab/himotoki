"""Java extractor (tree-sitter), one file at a time.

Emits class / field / method nodes, endpoints and SQL declared by annotations
(as configured in the rule packs), and a payload describing every method body
in a serialisable form — call sites with receiver chains, type uses, field
accesses, local variable types, literals — so that cross-file resolution can
run later from the cache without re-parsing.
"""
from __future__ import annotations

import re

import tree_sitter_java
from tree_sitter import Language, Parser

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import decode_source, system_ids, words
from .javaflow import method_flow

JAVA = Language(tree_sitter_java.language())
BRANCH_TYPES = {"if_statement", "for_statement", "enhanced_for_statement", "while_statement", "do_statement",
                "catch_clause", "switch_label", "ternary_expression"}
NAME = "java"
VERSION = "5"
_parser = None


def _p():
    global _parser
    if _parser is None:
        _parser = Parser(JAVA)
    return _parser


def _t(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def base_type(t: str) -> str:
    t = re.sub(r"<.*>", "", t or "").replace("[]", "").replace("...", "").strip()
    return t


def simple(t: str) -> str:
    return base_type(t).split(".")[-1]


def type_args(t: str) -> list[str]:
    m = re.search(r"<(.*)>", t or "")
    if not m:
        return []
    return [base_type(a) for a in re.split(r",\s*(?![^<]*>)", m.group(1)) if a.strip()]


def _strip_comment(c: str) -> str:
    c = re.sub(r"^/\*\*?|\*/$", "", c.strip())
    c = re.sub(r"^\s*\*\s?", "", c, flags=re.M)
    c = re.sub(r"^//\s?", "", c, flags=re.M)
    return re.sub(r"\s+", " ", c).strip()


def _leading_comment(node) -> str:
    prev, parts = node.prev_named_sibling, []
    while prev is not None and prev.type in ("block_comment", "line_comment"):
        parts.insert(0, _strip_comment(_t(prev)))
        prev = prev.prev_named_sibling
    return " ".join(parts)[:800]


def _str(node):
    if node is not None and node.type == "string_literal":
        return "".join(_t(c) for c in node.named_children if c.type in ("string_fragment", "escape_sequence"))
    return None


LOOP_TYPES = {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
SQLISH = re.compile(r"^\s*(select|insert|update|delete|merge|with|call|begin|\{\s*(\?\s*=\s*)?call)\b", re.I)


def _const_str(node):
    """Compile-time string of a constant initializer: a literal, a text block, or
    literals joined with + (multi-line SQL is usually written this way)."""
    if node is None:
        return None
    if node.type == "string_literal":
        return _str(node)
    if node.type == "text_block":
        return _t(node).strip('"').strip()
    if node.type == "parenthesized_expression" and node.named_children:
        return _const_str(node.named_children[0])
    if node.type == "binary_expression":
        op = node.child_by_field_name("operator")
        if op is not None and _t(op) == "+":
            a, b = _const_str(node.child_by_field_name("left")), _const_str(node.child_by_field_name("right"))
            if a is not None and b is not None:
                return a + b
    return None


def _value(node) -> str:
    """Annotation value: string literal, array of literals, or raw text."""
    if node is None:
        return ""
    sv = _str(node)
    if sv is not None:
        return sv
    if node.type in ("element_value_array_initializer", "array_initializer"):
        vals = [_value(c) for c in node.named_children]
        return "|".join(v for v in vals if v)
    if node.type == "binary_expression":   # "a" + "b"
        return "".join(_value(c) for c in node.named_children)
    return _t(node)


def _annotations(mods) -> list[dict]:
    out = []
    if mods is None:
        return out
    for c in mods.named_children:
        if c.type not in ("annotation", "marker_annotation"):
            continue
        name = _t(c.child_by_field_name("name")).split(".")[-1]
        args = {}
        al = c.child_by_field_name("arguments")
        if al is not None:
            for a in al.named_children:
                if a.type == "element_value_pair":
                    args[_t(a.child_by_field_name("key"))] = _value(a.child_by_field_name("value"))
                else:
                    args["value"] = _value(a)
        out.append({"name": name, "args": args})
    return out


# ----------------------------------------------------------- annotation rules
def _ann_arg(anns, spec: str):
    """'WebMethod.operationName' -> that argument; 'Stateless' -> '' when present (marker)."""
    name, _, attr = spec.partition(".")
    for a in anns:
        if a["name"] == name:
            if not attr:
                return a["args"].get("value") or name
            return a["args"].get(attr)
    return None


def _ann_tpl(tpl: str, el_anns, cls_anns, vars: dict):
    """Template for annotation rules: {method} {class} {fqn} {package} {field} {type},
    {@Ann.attr} on the element, {class@Ann.attr} on the class, alternatives with |,
    'literal' defaults, filters :last :lower :upper."""
    missing = False

    def one(m):
        nonlocal missing
        v, flts = None, []
        for alt in m.group(1).split("|"):          # each alternative carries its own filters
            alt, *flts = alt.strip().split(":")
            if alt.startswith("'") and alt.endswith("'"):
                v = alt[1:-1]
            elif alt.startswith("class@"):
                v = _ann_arg(cls_anns, alt[6:])
            elif alt.startswith("@"):
                v = _ann_arg(el_anns, alt[1:])
            else:
                v = vars.get(alt)
            if v:
                break
        if not v:
            missing = True
            return ""
        for flt in flts:
            v = v.split(".")[-1] if flt == "last" else v.lower() if flt == "lower" else v.upper() if flt == "upper" else v
        return v
    out = re.sub(r"\{([^{}]+)\}", one, tpl)
    return None if missing else out


def _ann_rules(ctx, on: str, el_anns, cls, vars: dict):
    """Apply java.annotation_rules for one element (class / method / field).
    Returns (keys, props, refs) — refs as (key, rel, conf, via)."""
    keys, props, refs = [], {}, []
    el_names = {a["name"] for a in el_anns}
    cls_names = {a["name"] for a in cls["anns"]}
    for r in ctx.rules.java.get("annotation_rules", []):
        # YAML 1.1 reads a bare `on:` key as boolean true, so both spellings are accepted
        if r.get("on", r.get(True, "method")) != on:
            continue
        if r.get("requires") and not el_names & set(r["requires"]):
            continue
        if r.get("requires_class") and not cls_names & set(r["requires_class"]):
            continue
        if r.get("class_kind") and cls["kind"] != r["class_kind"]:
            continue
        if r.get("unless") and el_names & set(r["unless"]):
            continue
        for k in r.get("keys", []):
            v = _ann_tpl(k, el_anns, cls["anns"], vars)
            if v:
                keys.append(v)
        for k, t in (r.get("props") or {}).items():
            v = _ann_tpl(str(t), el_anns, cls["anns"], vars)
            if v:
                props[k] = v
        for rf in r.get("refs", []):
            v = _ann_tpl(rf["key"], el_anns, cls["anns"], vars)
            if v:
                refs.append((v, rf.get("rel", Rel.REFERENCES), float(rf.get("conf", 0.9)), r.get("name", "annotation-rule")))
    return keys, props, refs


def _emit_refs(f, src, refs, line):
    for key, rel, conf, via in refs:
        f.ref(src, key, rel, conf, line, via=via, optional=True)


def _complexity(body) -> int:
    if body is None:
        return 1
    n, stack = 1, [body]
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


def _desc(node, depth=0):
    """Serialisable description of a receiver expression."""
    if node is None or depth > 6:
        return None
    t = node.type
    if t == "this":
        return {"k": "this"}
    if t == "super":
        return {"k": "super"}
    if t == "identifier":
        return {"k": "id", "n": _t(node)}
    if t == "field_access":
        return {"k": "fa", "o": _desc(node.child_by_field_name("object"), depth + 1),
                "n": _t(node.child_by_field_name("field"))}
    if t == "method_invocation":
        args = node.child_by_field_name("arguments")
        return {"k": "call", "o": _desc(node.child_by_field_name("object"), depth + 1),
                "n": _t(node.child_by_field_name("name")), "a": len(args.named_children) if args else 0}
    if t == "object_creation_expression":
        return {"k": "new", "t": _t(node.child_by_field_name("type"))}
    if t == "cast_expression":
        return {"k": "new", "t": _t(node.child_by_field_name("type"))}
    if t == "parenthesized_expression" and node.named_children:
        return _desc(node.named_children[0], depth + 1)
    if t == "scoped_identifier":
        return {"k": "id", "n": _t(node).split(".")[-1]}
    return None


def _method_body(body) -> dict:
    out = {"calls": [], "types": [], "news": [], "this_fields": [], "idents": [], "literals": [], "numbers": [],
           "locals": {}, "comments": []}
    if body is None:
        return out
    stack = [(body, False)]
    while stack:
        cur, in_loop = stack.pop()
        t = cur.type
        line = cur.start_point[0] + 1
        if t == "local_variable_declaration":
            vt = _t(cur.child_by_field_name("type"))
            for d in cur.named_children:
                if d.type == "variable_declarator":
                    nm = _t(d.child_by_field_name("name"))
                    v = d.child_by_field_name("value")
                    if vt == "var" and v is not None and v.type == "object_creation_expression":
                        vt = _t(v.child_by_field_name("type"))
                    out["locals"][nm] = vt
        elif t in ("enhanced_for_statement",):
            out["locals"][_t(cur.child_by_field_name("name"))] = _t(cur.child_by_field_name("type"))
        elif t == "catch_formal_parameter":
            nm = cur.child_by_field_name("name")
            ct = next((c for c in cur.named_children if c.type == "catch_type"), None)
            if nm is not None and ct is not None:
                out["locals"][_t(nm)] = _t(ct)
        elif t == "lambda_expression":
            pass
        elif t == "method_invocation":
            args = cur.child_by_field_name("arguments")
            call = {"r": _desc(cur.child_by_field_name("object")), "n": _t(cur.child_by_field_name("name")),
                    "a": len(args.named_children) if args else 0, "l": line}
            if in_loop:
                call["loop"] = True
            out["calls"].append(call)
        elif t == "string_literal":
            sv = _str(cur)
            if sv and len(sv) < 2000:
                out["literals"].append([sv, line])
        elif t in ("decimal_integer_literal", "decimal_floating_point_literal"):
            out["numbers"].append(_t(cur))
        elif t in ("line_comment", "block_comment"):
            out["comments"].append(_strip_comment(_t(cur))[:200])
        elif t == "type_identifier":
            out["types"].append([_t(cur), line])
        elif t == "object_creation_expression":
            out["news"].append([_t(cur.child_by_field_name("type")), line])
        elif t == "field_access":
            obj = cur.child_by_field_name("object")
            if obj is not None and obj.type == "this":
                out["this_fields"].append([_t(cur.child_by_field_name("field")), line])
        elif t == "identifier":
            p = cur.parent
            if p is not None and p.type not in ("method_invocation", "field_access", "method_declaration",
                                                "formal_parameter", "variable_declarator"):
                out["idents"].append([_t(cur), line])
        loop = in_loop or t in LOOP_TYPES
        stack.extend((c, loop) for c in cur.named_children)
    out["numbers"] = sorted(set(out["numbers"]))[:40]
    # dedupe identifiers to keep payloads small
    seen, ids = set(), []
    for n, l in out["idents"]:
        if n not in seen:
            seen.add(n); ids.append([n, l])
    out["idents"] = ids
    return out


def _lower_camel(n: str) -> str:
    return n[:1].lower() + n[1:]


class _Ctx:
    def __init__(self, facts, rules, src, is_test):
        self.f, self.rules, self.src, self.is_test = facts, rules, src, is_test
        self.classes = []
        self.imports: list[str] = []
        self.package = ""

    def qualify(self, t: str) -> str:
        """Best-effort fully qualified name from the file's imports (else same package)."""
        if "." in t:
            return t
        for i in self.imports:
            if i.endswith("." + t):
                return i
        return f"{self.package}.{t}" if self.package else t


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    data = decode_source(data).encode("utf-8")      # Shift_JIS sources are parsed as UTF-8
    tree = _p().parse(data)
    root = tree.root_node
    is_test = "/src/test/" in "/" + path or "/test/java/" in path or path.startswith("test/")
    ctx = _Ctx(f, rules, data, is_test)
    for c in root.named_children:
        if c.type == "package_declaration":
            ctx.package = _t(c).replace("package", "").replace(";", "").strip()
        elif c.type == "import_declaration":
            ctx.imports.append(_t(c).replace("import", "").replace("static", "").replace(";", "").strip())
    for c in root.named_children:
        _type_decl(ctx, c, None)
    f.payload = {"package": ctx.package, "imports": ctx.imports, "classes": ctx.classes, "is_test": is_test}
    return f


KINDS = {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum",
         "record_declaration": "record", "annotation_type_declaration": "annotation"}


def _type_decl(ctx: _Ctx, node, outer):
    if node.type not in KINDS:
        return
    f, rules = ctx.f, ctx.rules
    name = _t(node.child_by_field_name("name"))
    fqn = f"{outer['fqn']}.{name}" if outer else (f"{ctx.package}.{name}" if ctx.package else name)
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    anns = _annotations(mods)
    ann_names = {a["name"] for a in anns}
    extends, implements = [], []
    sc = node.child_by_field_name("superclass")
    if sc is not None:
        extends.append(_t(sc).replace("extends", "").strip())
    si = node.child_by_field_name("interfaces")
    if si is not None and si.named_children:
        implements += [_t(t) for t in si.named_children[0].named_children]
    for c in node.named_children:
        if c.type == "extends_interfaces" and c.named_children:
            extends += [_t(t) for t in c.named_children[0].named_children]
    is_test = ctx.is_test
    cls = {"fqn": fqn, "name": name, "kind": KINDS[node.type], "line": node.start_point[0] + 1,
           "end_line": node.end_point[0] + 1, "anns": anns, "extends": extends, "implements": implements,
           "fields": [], "methods": [], "is_test": is_test, "outer": outer["fqn"] if outer else None}
    cid = f"java:{fqn}"
    keys = [f"class:{fqn}"]
    bean_names = set(rules.ann("bean") or [])
    if ann_names & bean_names:
        val = next((a["args"].get("value") for a in anns if a["name"] in bean_names and a["args"].get("value")), None)
        keys += [f"bean:{_lower_camel(name)}"] + ([f"bean:{val}"] if val else [])
    kind = Kind.TEST_CLASS if is_test else Kind.CLASS
    comment = _leading_comment(node)
    rk, rprops, rrefs = _ann_rules(ctx, "class", anns, cls, {"class": name, "fqn": fqn, "package": ctx.package})
    f.node(cid, kind, name, cls["line"], cls["end_line"], " ".join([words(name), comment] + [words(a) for a in ann_names]),
           keys=keys + rk, fqn=fqn, annotations=sorted(ann_names), type=cls["kind"], package=ctx.package, **rprops)
    _emit_refs(f, cid, rrefs, cls["line"])
    if outer:
        f.edge(f"java:{outer['fqn']}", cid, Rel.CONTAINS)
    _messages(f, cid, anns, cls["line"])

    # class-level endpoint prefix
    ep = rules.ann("endpoint") or {}
    prefix = ""
    for a in anns:
        if a["name"] in (ep.get("class_prefix") or []):
            for attr in ep.get("path_attrs", ["value"]):
                if a["args"].get(attr):
                    prefix = a["args"][attr].split("|")[0]
                    break

    body = node.child_by_field_name("body")
    if body is not None:
        # fields first, so methods can see the class's string constants wherever declared
        for m in body.named_children:
            if m.type in ("field_declaration", "constant_declaration"):
                _field(ctx, cls, m)
        for m in body.named_children:
            if m.type in ("field_declaration", "constant_declaration"):
                continue
            elif m.type in ("method_declaration", "constructor_declaration"):
                _method(ctx, cls, m, prefix)
            elif m.type == "enum_body_declarations":
                for mm in m.named_children:
                    if mm.type in ("method_declaration", "constructor_declaration"):
                        _method(ctx, cls, mm, prefix)
                    elif mm.type == "field_declaration":
                        _field(ctx, cls, mm)
            elif m.type in KINDS:
                _type_decl(ctx, m, cls)
    ctx.classes.append(cls)


def _messages(f, nid, anns, line):
    for a in anns:
        for v in a["args"].values():
            for key in re.findall(r"\{([\w.\-]+)\}", v or ""):
                f.ref(nid, f"msg:{key}", Rel.USES_MESSAGE, 0.95, line, via="annotation", optional=True)


def _ann_length(anns) -> str | None:
    """@Size(max=10) / @Length(max=10) / @Digits(integer=7, fraction=2) / @Column(length=10)."""
    for a in anns:
        g = a.get("args") or {}
        if a["name"] in ("Size", "Length", "Max") and re.fullmatch(r"\d+", str(g.get("max", g.get("value", ""))).strip()):
            return str(g.get("max", g.get("value"))).strip()
        if a["name"] == "Digits" and re.fullmatch(r"\d+", str(g.get("integer", "")).strip()):
            fr = str(g.get("fraction", "")).strip()
            return str(g["integer"]).strip() + ("," + fr if re.fullmatch(r"[1-9]\d*", fr) else "")
        if a["name"] == "Column" and re.fullmatch(r"\d+", str(g.get("length", "")).strip()):
            return str(g["length"]).strip()
    return None


def _field(ctx, cls, node):
    f = ctx.f
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    anns = _annotations(mods)
    ftype = _t(node.child_by_field_name("type"))
    mtxt = _t(mods) if mods is not None else ""
    is_const = "static" in mtxt and "final" in mtxt
    comment = _leading_comment(node)
    for d in node.named_children:
        if d.type != "variable_declarator":
            continue
        fname = _t(d.child_by_field_name("name"))
        const = _const_str(d.child_by_field_name("value")) if is_const else None
        fd = {"name": fname, "type": ftype, "anns": anns, "line": node.start_point[0] + 1,
              "end_line": node.end_point[0] + 1, "constant": const, "static": "static" in mtxt}
        cls["fields"].append(fd)
        fid = f"java:{cls['fqn']}.{fname}"
        props = {"type": ftype, "annotations": [a["name"] for a in anns],
                 "validations": [f"@{a['name']}({a['args']})" for a in anns if a["args"]]}
        if const is not None:
            props["constant"] = const
        ln = _ann_length(anns)
        if ln:
            props["length"] = ln
        rk, rprops, rrefs = _ann_rules(ctx, "field", anns, cls, {"class": cls["name"], "fqn": cls["fqn"],
                                                                 "field": fname, "type": simple(ftype),
                                                                 "package": ctx.package})
        f.node(fid, Kind.FIELD, f"{cls['name']}.{fname}", fd["line"], fd["end_line"],
               " ".join([words(fname), words(simple(ftype)), comment, (const or "")[:300]]),
               keys=[f"field:{cls['fqn']}.{fname}"] + rk, **props, **rprops)
        _emit_refs(f, fid, rrefs, fd["line"])
        f.edge(f"java:{cls['fqn']}", fid, Rel.CONTAINS)
        _messages(f, fid, anns, fd["line"])


def _class_scope(node):
    """Constants and field names of the class around a method (declared anywhere in it)."""
    body = node.parent
    consts, names = {}, set()
    for c in (body.named_children if body is not None else []):
        if c.type not in ("field_declaration", "constant_declaration"):
            continue
        mods = next((x for x in c.named_children if x.type == "modifiers"), None)
        mt = _t(mods) if mods is not None else ""
        for d in c.named_children:
            if d.type == "variable_declarator":
                n = _t(d.child_by_field_name("name"))
                names.add(n)
                if ("static" in mt and "final" in mt) or c.type == "constant_declaration":
                    v = _const_str(d.child_by_field_name("value"))
                    if v is not None:
                        consts[n] = v
    return consts, names


def _method(ctx, cls, node, prefix):
    f, rules = ctx.f, ctx.rules
    mods = next((c for c in node.named_children if c.type == "modifiers"), None)
    anns = _annotations(mods)
    params = []
    fp = node.child_by_field_name("parameters")
    if fp is not None:
        for p in fp.named_children:
            if p.type in ("formal_parameter", "spread_parameter"):
                pt = p.child_by_field_name("type") or next((c for c in p.named_children if "type" in c.type), None)
                pn = p.child_by_field_name("name")
                if pn is None:
                    vd = next((c for c in p.named_children if c.type == "variable_declarator"), None)
                    pn = vd.child_by_field_name("name") if vd is not None else None
                params.append([_t(pt), _t(pn)])
    is_ctor = node.type == "constructor_declaration"
    name = _t(node.child_by_field_name("name"))
    body = node.child_by_field_name("body")
    sig = f"{name}({','.join(simple(t) for t, _ in params)})"
    md = {"name": name, "sig": sig, "params": params, "ret": "" if is_ctor else _t(node.child_by_field_name("type")),
          "anns": anns, "line": node.start_point[0] + 1, "end_line": node.end_point[0] + 1, "is_ctor": is_ctor,
          "abstract": body is None}
    md.update(_method_body(body))
    consts_, fnames_ = _class_scope(node)
    fl = method_flow(body, params, consts_, ctx.imports, fnames_)
    if fl:
        md["flow"] = fl
    cls["methods"].append(md)
    mid = f"java:{cls['fqn']}#{sig}"
    is_test_m = ctx.is_test
    ann_names = {a["name"] for a in anns}
    kind = Kind.TEST_METHOD if is_test_m else Kind.METHOD
    comment = _leading_comment(node)
    lits = [l for l, _ in md["literals"]]
    text = " ".join([words(name), comment, " ".join(words(a) for a in ann_names), " ".join(words(n) for n in md["locals"]),
                     " ".join(md["comments"])[:600], " ".join(l for l in lits if len(l) < 80)[:600], " ".join(md["numbers"])])
    rk, rprops, rrefs = _ann_rules(ctx, "method", anns, cls, {"class": cls["name"], "fqn": cls["fqn"], "method": name,
                                                              "package": ctx.package})
    txc = _tx_calls(rules, cls, md)
    if txc:
        rprops["tx_calls"] = txc
    f.node(mid, kind, f"{cls['name']}.{name}()" if not is_ctor else f"{cls['name']}()", md["line"], md["end_line"],
           text, keys=[f"method:{cls['fqn']}#{name}"] + rk, complexity=_complexity(body), **rprops,
           loc=md["end_line"] - md["line"] + 1, signature=sig, annotations=sorted(ann_names), ctor=is_ctor,
           literals=lits[:40], numbers=md["numbers"],
           is_test=bool(ann_names & set(rules.ann("test") or [])))
    f.edge(f"java:{cls['fqn']}", mid, Rel.CONTAINS)
    _messages(f, mid, anns, md["line"])
    _emit_refs(f, mid, rrefs, md["line"])

    # endpoints
    ep = rules.ann("endpoint") or {}
    for a in anns:
        verb = (ep.get("method") or {}).get(a["name"])
        if not verb:
            continue
        if verb == "ANY" and a["args"].get(ep.get("verb_attr", "method")):
            verb = a["args"][ep.get("verb_attr", "method")].split(".")[-1].split("|")[0]
        paths = []
        for attr in ep.get("path_attrs", ["value"]):
            if a["args"].get(attr):
                paths = a["args"][attr].split("|")
                break
        for p in paths or [""]:
            full = "/" + "/".join(s.strip("/") for s in (prefix, p) if s and s.strip("/"))
            eid = f"ep:{verb} {full}"
            f.node(eid, Kind.ENDPOINT, f"{verb} {full}", md["line"], md["end_line"],
                   " ".join([verb.lower(), words(full), words(name)]), keys=[f"url:{verb} {full}", f"url:{full}"],
                   verb=verb, path=full, package=ctx.package)
            f.edge(eid, mid, Rel.HANDLES, 1.0, "rule:java-annotations")

    # SQL in annotations
    sqla = rules.ann("sql") or {}
    for a in anns:
        if a["name"] in (sqla.get("names") or []) and a["args"].get(sqla.get("attr", "value")):
            sql = a["args"][sqla.get("attr", "value")].replace("|", " ")
            if re.search(r"\b(select|insert|update|delete|merge)\b", sql, re.I):
                sid = f"sql:{cls['fqn']}.{name}"
                f.node(sid, Kind.SQL_STATEMENT, f"@{a['name']} {name}", md["line"], md["end_line"], sql[:2000],
                       keys=[sid], sql=sql[:4000])
                f.edge(mid, sid, Rel.EXECUTES, 1.0, "rule:java-annotations")
                from .sql import crud_refs
                crud_refs(f, sid, sql, md["line"], via="annotation")

    # query methods derived from their names (findByZipStartingWith -> Entity.zip)
    dq = rules.java.get("derived_query")
    if dq and cls["kind"] == "interface" and not is_ctor:
        m = re.match(dq["pattern"], name)
        ent = None
        for t in cls["extends"]:
            g = re.match(r"([\w.]+)\s*<\s*([\w.]+)", t)
            if g and simple(g.group(1)) in dq.get("base_types", []):
                ent = g.group(2)
        if m and ent:
            crit = re.split(r"OrderBy", m.group(1))[0]
            ops = sorted((str(o) for o in dq.get("operators", []) if o), key=len, reverse=True)
            for part in re.split(dq.get("split", "And|Or"), crit):
                for op in ops:
                    if part.endswith(op) and len(part) > len(op):
                        part = part[: -len(op)]
                        break
                if part:
                    f.ref(mid, f"propath:{ctx.qualify(ent)}.{_lower_camel(part)}", Rel.REFERENCES, 0.9,
                          md["line"], via="derived-query", optional=True)

    # interface methods bound to externally defined statements (e.g. XML mappers)
    if cls["kind"] == "interface" and not is_ctor:
        for tpl in rules.java.get("interface_binding", []):
            f.ref(mid, tpl.format(fqn=cls["fqn"], method=name), Rel.EXECUTES, 0.95, md["line"],
                  via="interface-binding", optional=True)

    # string literals probed against configured namespaces; a constant of the same
    # class used in the body (return FORM;) counts as the literal it holds
    nss = set(rules.java.get("literal_namespaces", []))
    consts = {fd["name"]: fd["constant"] for fd in cls["fields"] if fd.get("constant")}
    probes = list(md["literals"]) + [[consts[n], l] for n, l in md["idents"] if n in consts]
    if rules.java.get("sql_literals"):
        _sql_literals(ctx, cls, mid, probes)
    # a file name used by a method that writes files is an output, otherwise an input
    writes_files = any(c["n"] in _FILE_WRITE_CALLS for c in md["calls"]) or \
        any((t or "").split("<")[0] in _FILE_WRITE_TYPES for t, _ in md["news"])
    for lit, line in probes:
        if "view" in nss and re.fullmatch(r"[\w\-]+(/[\w\-]+)+", lit) and not lit.startswith("redirect"):
            f.ref(mid, f"view:{lit}", Rel.TESTS if is_test_m else Rel.RENDERS, 0.9, line, via="literal", optional=True)
        if "url" in nss and lit.startswith("/") and re.fullmatch(r"/[\w/{}.\-?=&]*", lit):
            f.ref(mid, f"url:{lit.split('?')[0]}", Rel.TESTS if is_test_m else Rel.REFERENCES, 0.85, line,
                  via="literal", optional=True)
        if "msg" in nss and re.fullmatch(r"[a-zA-Z][\w\-]*(\.[\w\-]+)+", lit):
            f.ref(mid, f"msg:{lit}", Rel.USES_MESSAGE, 0.8, line, via="literal", optional=True)
        if "batchjob" in nss and re.fullmatch(r"[a-zA-Z][\w\-]{2,}", lit):
            f.ref(mid, f"batchjob:{lit}", Rel.RUNS, 0.7, line, via="literal", optional=True)
        if "config" in nss and re.fullmatch(r"[a-z][\w\-]*(\.[\w\-]+){1,}", lit):
            f.ref(mid, f"config:{lit}", Rel.REFERENCES, 0.7, line, via="literal", optional=True)
        if re.fullmatch(r"[\w\-./]+\.(dat|csv|tsv|fix|txt)", lit) and "/" in lit or \
                re.fullmatch(r"[\w\-.]+\.(dat|csv|tsv|fix)", lit):
            f.ref(mid, f"datafile:{lit}", Rel.WRITES if writes_files else Rel.READS, 0.6, line,
                  via="literal", optional=True)
        if len(lit) <= 40 and re.fullmatch(r"[\w\-./]+", lit):
            # a screen / report / job ID in a literal ("R_HZK030", "HZKG010.jsp")
            for sid in system_ids(lit.rsplit("/", 1)[-1].rsplit(".", 1)[0])[:2]:
                f.ref(mid, f"sysid:{sid}", Rel.REFERENCES, 0.6, line, via="literal", optional=True)
        for ns in nss - {"view", "url", "msg", "batchjob", "config"}:
            if re.fullmatch(r"[A-Za-z][\w\-.]{2,}", lit):
                f.ref(mid, f"{ns}:{lit}", Rel.REFERENCES, 0.75, line, via="literal", optional=True)


def _has_path(a, ep):
    return any(a["args"].get(x) for x in ep.get("path_attrs", ["value"]))


def _tx_calls(rules, cls, md) -> list[dict]:
    """begin / commit / rollback on transaction APIs (UserTransaction, Connection, ...),
    with whether the call sits inside a loop (sequential commits)."""
    api = rules.java.get("transaction_apis") or {}
    types = set(api.get("types", []))
    if not types:
        return []
    ops = {n: op for op in ("begin", "commit", "rollback") for n in api.get(op, [])}
    known = {fd["name"]: simple(fd["type"]) for fd in cls["fields"]}
    known.update({n: simple(t) for t, n in md["params"]})
    known.update({n: simple(t) for n, t in md["locals"].items()})
    out = []
    for c in md["calls"]:
        r = c.get("r") or {}
        if c["n"] in ops and r.get("k") == "id" and known.get(r.get("n")) in types:
            out.append({"op": ops[c["n"]], "line": c["l"], "loop": bool(c.get("loop")), "api": known[r["n"]]})
    return sorted(out, key=lambda x: x["line"])


_FILE_WRITE_CALLS = {"write", "writeString", "newBufferedWriter", "newOutputStream", "println", "transferTo"}
_FILE_WRITE_TYPES = {"FileWriter", "FileOutputStream", "PrintWriter", "BufferedWriter", "OutputStreamWriter",
                     "PrintStream", "RandomAccessFile"}


def _sql_literals(ctx, cls, mid, probes):
    """SQL written as Java strings (JDBC): one SqlStatement per distinct text in the
    class, executed by the methods that use it, with its tables / columns / routine calls."""
    import hashlib
    from .sql import crud_refs
    seen = ctx.__dict__.setdefault("sql_nodes", set())
    for lit, line in probes:
        if not SQLISH.match(lit) or len(lit) < 12 or " " not in lit.strip():
            continue
        h = hashlib.sha1(lit.encode()).hexdigest()[:10]
        sid = f"sql:{cls['fqn']}#{h}"
        if sid not in seen:
            seen.add(sid)
            head = re.sub(r"\s+", " ", lit.strip())
            ctx.f.node(sid, Kind.SQL_STATEMENT, f"SQL {head[:48]}", line, line, head[:600], keys=[sid], sql=head[:4000])
            ctx.f.edge(f"java:{cls['fqn']}", sid, Rel.CONTAINS)
            crud_refs(ctx.f, sid, lit, line, via="jdbc", conf=0.9)
        ctx.f.edge(mid, sid, Rel.EXECUTES, 0.95, "parser", line=line)
