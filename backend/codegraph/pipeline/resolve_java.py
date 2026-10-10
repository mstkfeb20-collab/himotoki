"""Cross-file Java resolution from cached extractor payloads.

Type names are resolved the way javac does it, as far as source allows: nested
classes, explicit imports, same package, wildcard imports, then a unique simple
name. Call receivers are typed from parameters, locals, fields (including
inherited ones) and return types of earlier calls in the chain. Interface and
abstract methods are linked to their implementations (IMPLEMENTED_BY), so a
change to an implementation reaches callers that only know the interface.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..model import Kind, Rel
from ..text import snake
from ..extractors.java import base_type, simple, type_args

_COLLECTION_HINT = {"List", "Set", "Collection", "Optional", "Page", "Iterable", "Stream", "Map", "Slice",
                    "ArrayList", "HashSet", "LinkedList"}


class JavaResolver:
    def __init__(self, facts_list, rules):
        self.rules = rules
        self.cls: dict[str, dict] = {}
        self.file_of: dict[str, dict] = {}
        self.by_simple: dict[str, list[str]] = defaultdict(list)
        self.edges: list[dict] = []
        for f in facts_list:
            if f.extractor != "java":
                continue
            p = f.payload
            for c in p.get("classes", []):
                c["_file"] = f.path
                c["_pkg"] = p.get("package", "")
                c["_imports"] = p.get("imports", [])
                self.cls[c["fqn"]] = c
                self.by_simple[c["name"]].append(c["fqn"])
        self._anc_cache: dict[str, list[str]] = {}
        self._fields_cache: dict[str, dict] = {}
        self._impls: dict[str, list[str]] | None = None

    # ------------------------------------------------------------ types
    def resolve(self, name: str, ctx: dict | None):
        if not name:
            return None
        t = base_type(name)
        if t in self.cls:
            return t
        if "." in t:
            head, _, rest = t.partition(".")
            h = self.resolve(head, ctx)
            if h and f"{h}.{rest}" in self.cls:
                return f"{h}.{rest}"
            return None
        if ctx:
            # nested in current class or its outers
            o = ctx["fqn"]
            while o:
                if f"{o}.{t}" in self.cls:
                    return f"{o}.{t}"
                o = o.rsplit(".", 1)[0] if "." in o else ""
                if o == ctx.get("_pkg"):
                    break
            for imp in ctx.get("_imports", []):
                if imp.endswith("." + t) and imp in self.cls:
                    return imp
            if f"{ctx.get('_pkg')}.{t}" in self.cls:
                return f"{ctx.get('_pkg')}.{t}"
            for imp in ctx.get("_imports", []):
                if imp.endswith(".*") and f"{imp[:-2]}.{t}" in self.cls:
                    return f"{imp[:-2]}.{t}"
            for imp in ctx.get("_imports", []):      # import of an outer class -> nested type
                for fq in self.by_simple.get(t, []):
                    if fq.startswith(imp + "."):
                        return fq
        cands = self.by_simple.get(t, [])
        if len(cands) == 1:
            return cands[0]
        if cands and ctx:
            pkg = ctx.get("_pkg", "")
            best = max(cands, key=lambda c: len(_common(c, pkg)))
            return best
        return None

    def ancestors(self, fqn: str) -> list[str]:
        if fqn in self._anc_cache:
            return self._anc_cache[fqn]
        out, seen, queue = [], {fqn}, [(t, fqn) for t in self.cls[fqn]["extends"] + self.cls[fqn]["implements"]]
        self._anc_cache[fqn] = out
        while queue:
            t, ctxf = queue.pop(0)
            r = self.resolve(t, self.cls.get(ctxf))
            if r and r not in seen and r in self.cls:
                seen.add(r)
                out.append(r)
                queue += [(x, r) for x in self.cls[r]["extends"] + self.cls[r]["implements"]]
        return out

    def fields(self, fqn: str) -> dict:
        if fqn in self._fields_cache:
            return self._fields_cache[fqn]
        out = {}
        for c in reversed([fqn] + self.ancestors(fqn)):
            for fd in self.cls[c]["fields"]:
                out[fd["name"]] = (c, fd)
        self._fields_cache[fqn] = out
        return out

    def methods(self, fqn: str, name: str, arity: int | None):
        for c in [fqn] + self.ancestors(fqn):
            ms = [m for m in self.cls[c]["methods"] if m["name"] == name and not m["is_ctor"]
                  and (arity is None or len(m["params"]) == arity or any("..." in p[0] for p in m["params"]))]
            if ms:
                return [(c, m) for m in ms]
        return self.methods(fqn, name, None) if arity is not None else []

    @staticmethod
    def mid(c, m):
        return f"java:{c}#{m['sig']}"

    def add(self, src, dst, rel, conf, kind="parser", **props):
        props.update({"conf": round(conf, 3), "src": kind})
        self.edges.append({"src": src, "dst": dst, "rel": rel, "props": props})

    # ------------------------------------------------------------ main
    def run(self):
        for fqn, c in self.cls.items():
            cid = f"java:{fqn}"
            for t in c["extends"] + c["implements"]:
                r = self.resolve(t, c)
                if r:
                    self.add(cid, f"java:{r}", Rel.EXTENDS, 1.0)
                for a in type_args(t):
                    ra = self.resolve(a, c)
                    if ra:
                        self.add(cid, f"java:{ra}", Rel.REFERENCES, 0.9, via="type-arg")
            for fd in c["fields"]:
                fid = f"java:{fqn}.{fd['name']}"
                inject = bool({a["name"] for a in fd["anns"]} & set(self.rules.ann("inject") or []))
                for t in [fd["type"]] + type_args(fd["type"]):
                    r = self.resolve(t, c)
                    if r:
                        self.add(fid, f"java:{r}", Rel.INJECTS if inject else Rel.REFERENCES, 0.95)
            for m in c["methods"]:
                self._method(c, m)
            self._jpa(c)
        self._dispatch()
        self._tests()
        return self.edges

    def _receiver(self, c, m, d, depth=0):
        if d is None or d.get("k") == "this":
            return c["fqn"]
        k = d.get("k")
        if k == "super":
            sup = c["extends"][0] if c["extends"] else None
            return self.resolve(sup, c) if sup else None
        if k == "id":
            n = d["n"]
            loc = {p[1]: p[0] for p in m["params"]}
            loc.update(m.get("locals", {}))
            if n in loc:
                return self._elem(loc[n], c)
            f = self.fields(c["fqn"]).get(n)
            if f:
                return self._elem(f[1]["type"], self.cls[f[0]])
            o = self.cls.get(c.get("outer") or "")
            while o:
                f = self.fields(o["fqn"]).get(n)
                if f:
                    return self._elem(f[1]["type"], self.cls[f[0]])
                o = self.cls.get(o.get("outer") or "")
            return self.resolve(n, c)
        if k == "fa":
            if d.get("o", {}) and d["o"].get("k") == "this":
                f = self.fields(c["fqn"]).get(d["n"])
                return self._elem(f[1]["type"], self.cls[f[0]]) if f else None
            ot = self._receiver(c, m, d.get("o"), depth + 1)
            if ot and ot in self.cls:
                f = self.fields(ot).get(d["n"])
                return self._elem(f[1]["type"], self.cls[f[0]]) if f else None
            return self.resolve(f"{d.get('o', {}).get('n', '')}.{d['n']}", c) if d.get("o") else None
        if k == "call" and depth < 5:
            ot = self._receiver(c, m, d.get("o"), depth + 1)
            if ot and ot in self.cls:
                ms = self.methods(ot, d["n"], d.get("a"))
                if ms:
                    owner, tm = ms[0]
                    return self._elem(tm["ret"], self.cls[owner])
            return None
        if k == "new":
            return self.resolve(d["t"], c)
        return None

    def _elem(self, t, ctx):
        r = self.resolve(t, ctx)
        if r:
            return r
        if simple(t) in _COLLECTION_HINT or type_args(t):
            for a in reversed(type_args(t)):
                ra = self.resolve(a, ctx)
                if ra:
                    return ra
        return None

    def _method(self, c, m):
        mid = self.mid(c["fqn"], m)
        fields = self.fields(c["fqn"])
        refd = set()
        loc_names = {p[1] for p in m["params"]} | set(m.get("locals", {}))
        for t, line in m.get("types", []):
            r = self.resolve(t, c)
            if r and r != c["fqn"]:
                refd.add(r)
                self.add(mid, f"java:{r}", Rel.REFERENCES, 0.9, line=line)
        for t, line in m.get("news", []):
            r = self.resolve(t, c)
            if r:
                ctors = [x for x in self.cls[r]["methods"] if x["is_ctor"]]
                for x in ctors[:3]:
                    self.add(mid, self.mid(r, x), Rel.CALLS, 0.9, line=line)
        for n, line in m.get("this_fields", []):
            if n in fields:
                o, fd = fields[n]
                self.add(mid, f"java:{o}.{n}", Rel.REFERENCES, 0.95, line=line)
        for n, line in m.get("idents", []):
            if n in fields and n not in loc_names:
                o, fd = fields[n]
                self.add(mid, f"java:{o}.{n}", Rel.REFERENCES, 0.9, line=line)
                if fd.get("constant"):
                    m.setdefault("literals", []).append([fd["constant"], line])
        for call in m.get("calls", []):
            rt = self._receiver(c, m, call.get("r"))
            if not rt or rt not in self.cls:
                continue
            ms = self.methods(rt, call["n"], call["a"])
            if ms:
                exact = any(len(x[1]["params"]) == call["a"] for x in ms)
                for o, tm in ms[:3]:
                    self.add(mid, self.mid(o, tm), Rel.CALLS, 0.95 if exact else 0.7, line=call["l"])
            else:
                self.add(mid, f"java:{rt}", Rel.REFERENCES, 0.8, line=call["l"], call=call["n"])
            am = re.match(r"(get|set|is|has)([A-Z]\w*)", call["n"])
            if am:
                fn = am.group(2)[:1].lower() + am.group(2)[1:]
                f = self.fields(rt).get(fn)
                if f:
                    self.add(mid, f"java:{f[0]}.{fn}", Rel.REFERENCES, 0.9, line=call["l"], via="accessor")
            refd.add(rt)
        # string literals that name a field of a class this method works with
        for lit, line in m.get("literals", []):
            if not re.fullmatch(r"[a-z]\w*", lit):
                continue
            for r in list(refd) + [c["fqn"]]:
                f = self.fields(r).get(lit)
                if f and not f[1].get("constant"):
                    self.add(mid, f"java:{f[0]}.{lit}", Rel.REFERENCES, 0.7, line=line, via="literal")
                    break
        m["_refd"] = refd

    def table_of(self, fqn):
        ent = self.rules.ann("entity") or {}
        c = self.cls.get(fqn)
        if not c or not {a["name"] for a in c["anns"]} & set(ent.get("names", [])):
            return None
        tab = next((a["args"].get(ent.get("table_attr", "name")) for a in c["anns"]
                    if a["name"] == ent.get("table_annotation")), None) or snake(c["name"])
        return tab.lower()

    def _jpa(self, c):
        tab = self.table_of(c["fqn"])
        if not tab:
            return
        self.add(f"java:{c['fqn']}", f"table:{tab}", Rel.MAPS_TO, 0.95, "rule:jpa")
        colr = self.rules.ann("column") or {}
        joinr = self.rules.ann("join_column") or {}
        rel = set(self.rules.ann("relation") or [])
        trans = set(self.rules.ann("transient") or [])
        for fname, (owner, fd) in self.fields(c["fqn"]).items():
            an = {a["name"] for a in fd["anns"]}
            if an & trans or fd.get("constant") is not None or fd.get("static"):
                continue
            fid = f"java:{owner}.{fname}"
            jc = next((a for a in fd["anns"] if a["name"] in joinr.get("names", [])), None)
            if an & rel:
                if jc and jc["args"].get(joinr.get("attr", "name")):
                    col = jc["args"][joinr.get("attr", "name")].lower()
                    if "OneToMany" in an:
                        args = type_args(fd["type"])
                        tgt = self.resolve(args[0], self.cls[owner]) if args else None
                        if tgt and self.table_of(tgt):
                            self.add(fid, f"col:{self.table_of(tgt)}.{col}", Rel.MAPS_TO, 0.9, "rule:jpa")
                    else:
                        self.add(fid, f"col:{tab}.{col}", Rel.MAPS_TO, 0.95, "rule:jpa")
                continue
            ca = next((a for a in fd["anns"] if a["name"] in colr.get("names", [])), None)
            cname = (ca["args"].get(colr.get("attr", "name")) if ca else None) or ("id" if fname == "id" else snake(fname))
            self.add(fid, f"col:{tab}.{cname.lower()}", Rel.MAPS_TO, 0.95, "rule:jpa")

    def _dispatch(self):
        impls = defaultdict(list)
        for fqn in self.cls:
            for a in self.ancestors(fqn):
                impls[a].append(fqn)
        for fqn, c in self.cls.items():
            if not impls.get(fqn):
                continue
            abstract_like = c["kind"] == "interface"
            for m in c["methods"]:
                if m["is_ctor"] or not (abstract_like or m.get("abstract")):
                    continue
                targets = []
                for sub in impls[fqn]:
                    for sm in self.cls[sub]["methods"]:
                        if sm["name"] == m["name"] and len(sm["params"]) == len(m["params"]) and not sm.get("abstract"):
                            targets.append((sub, sm))
                for sub, sm in targets:
                    self.add(self.mid(fqn, m), self.mid(sub, sm), Rel.IMPLEMENTED_BY, 1.0 if len(targets) == 1 else 0.9)

    def _tests(self):
        sufs = sorted(self.rules.ann("test_class_suffixes") or ["Test", "Tests"], key=len, reverse=True)
        for fqn, c in self.cls.items():
            if not c["is_test"]:
                continue
            base = c["name"]
            for s in sufs:
                if base.endswith(s) and len(base) > len(s):
                    base = base[: -len(s)]
                    break
            base = re.sub(r"Impl$", "", base) if base not in self.by_simple else base
            target = self.resolve(base, c)
            if target and not self.cls[target]["is_test"]:
                self.add(f"java:{fqn}", f"java:{target}", Rel.TESTS, 0.9, via="naming")
            for m in c["methods"]:
                for r in m.get("_refd", ()):
                    if r in self.cls and not self.cls[r]["is_test"]:
                        self.add(self.mid(fqn, m), f"java:{r}", Rel.TESTS, 0.8, via="uses")
                # literals naming fields of entities the test class works with
            refd = set()
            for m in c["methods"]:
                refd |= m.get("_refd", set())
            if target:
                refd.add(target)
                for tm in self.cls[target]["methods"]:
                    refd |= tm.get("_refd", set())
            for m in c["methods"]:
                for lit, line in m.get("literals", []):
                    if not re.fullmatch(r"[a-z]\w*", lit):
                        continue
                    for r in refd:
                        if r in self.cls and not self.cls[r]["is_test"]:
                            f = self.fields(r).get(lit)
                            if f and not f[1].get("constant"):
                                self.add(self.mid(fqn, m), f"java:{f[0]}.{lit}", Rel.REFERENCES, 0.7, line=line,
                                         via="test-literal")
                                break


def _common(a: str, b: str) -> str:
    out = []
    for x, y in zip(a.split("."), b.split(".")):
        if x != y:
            break
        out.append(x)
    return ".".join(out)
