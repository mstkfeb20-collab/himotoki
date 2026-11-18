"""Linker: joins the per-file facts of all languages into one graph.

1. merges node declarations (canonical nodes such as tables or message keys are
   declared by many files and merged by id),
2. runs the Java resolver over cached payloads,
3. resolves every reference key against the keys nodes provide, with
   namespace-specific fallbacks (simple class names, URL templates, view name
   variants, file basenames, columns in any of the statement's tables,
   free-text mentions...), lowering confidence when a fallback was needed,
4. reports what stayed unresolved, per namespace — the list of places where the
   graph is known to be incomplete.
"""
from __future__ import annotations

import os
import posixpath
import re
from collections import Counter, defaultdict

from ..model import Edge, Kind, Node, Rel
from ..text import datafile_base, system_ids, words
from .resolve_java import JavaResolver

CASE_INSENSITIVE = {"table", "column", "colany", "datafile", "proc", "dbobj", "screen", "function", "dblink", "prisma",
                    "datastem", "sysid"}
DEFERRED = ("datastem:",)        # resolved after every other reference (they match nodes made on the way)


def norm_key(key: str) -> str:
    ns, _, v = key.partition(":")
    ns = ns.lower()
    if ns in CASE_INSENSITIVE:
        v = v.lower().strip('"')
    return f"{ns}:{v}"


def _merge_props(a: dict, b: dict):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            a[k].update(v)
        elif isinstance(v, list) and isinstance(a.get(k), list):
            a[k] = list(dict.fromkeys(a[k] + v))
        elif v not in (None, "") and a.get(k) in (None, ""):
            a[k] = v


class Linker:
    def __init__(self, rules):
        self.rules = rules
        self.nodes: dict[str, Node] = {}
        self.keys: dict[str, list[str]] = defaultdict(list)
        self.edges: dict[tuple, Edge] = {}
        self.unresolved: Counter = Counter()
        self.unresolved_samples: dict[str, list] = defaultdict(list)
        self.resolved_by: Counter = Counter()
        self.external: Counter = Counter()

    # ------------------------------------------------------------ nodes
    def add_node(self, d: dict):
        nid = d["id"]
        keys = d.get("keys") or []
        if nid in self.nodes:
            n = self.nodes[nid]
            _merge_props(n.props, d.get("props") or {})
            if d.get("text") and d["text"] not in n.text:
                n.text = (n.text + " " + d["text"])[:6000]
            if not n.file and d.get("file") and not d.get("canonical"):
                n.file, n.line, n.end_line = d["file"], d.get("line"), d.get("end_line")
        else:
            self.nodes[nid] = Node(nid, d["kind"], d["name"], d.get("file"), d.get("line"), d.get("end_line"),
                                   d.get("text", ""), dict(d.get("props") or {}))
        for k in keys:
            nk = norm_key(k)
            if nid not in self.keys[nk]:
                self.keys[nk].append(nid)

    def add_edge(self, src, dst, rel, props):
        k = (src, dst, rel)
        cur = self.edges.get(k)
        if cur is None or float(props.get("conf", 1)) > cur.conf:
            self.edges[k] = Edge(src, dst, rel, dict(props))

    # ------------------------------------------------------------ main
    def link(self, facts_list) -> tuple[list[Node], list[Edge], dict]:
        for f in facts_list:
            for d in f.nodes:
                self.add_node(d)
        self._build_indexes()
        for f in facts_list:
            for e in f.edges:
                self.add_edge(e["src"], e["dst"], e["rel"], e.get("props") or {})
        self._ts_init(facts_list)
        jr = JavaResolver(facts_list, self.rules)
        for e in jr.run():
            self.add_edge(e["src"], e["dst"], e["rel"], e["props"])
        self.java = jr
        later = []
        self._amb = []                       # column names that several tables have, settled by context
        for f in facts_list:
            for r in f.refs:
                if r["key"].lower().startswith(DEFERRED):
                    later.append(r)
                    continue
                if self._ambiguous_col(r["key"], r.get("props")):
                    self._amb.append(("ref", r, f.path))
                    continue
                self._resolve_ref(r)
            for lk in f.payload.get("links", []):
                if self._ambiguous_col(lk["to"], None):
                    self._amb.append(("link", lk, f.path))
                    continue
                a = self.resolve_key(lk["from"])
                b = self.resolve_key(lk["to"])
                for sa, ca in a:
                    for sb, cb in b:
                        self.add_edge(sa, sb, lk["rel"], {"conf": round(lk["conf"] * ca * cb, 3),
                                                          "src": lk.get("src_kind", "rule"), "via": lk.get("via"),
                                                          "line": lk.get("line")})
                if not a or not b:
                    self._miss(lk["from"] if not a else lk["to"], lk.get("via", ""), f.path)
        for r in later:
            self._resolve_ref(r)
        self._settle_ambiguous()
        self._value_flow()
        runtime = self._match_runtime()
        drift = self._drift()
        valid = [e for e in self.edges.values() if e.src in self.nodes and e.dst in self.nodes and e.src != e.dst]
        self._groups()
        from .transactions import analyze as analyze_transactions
        transactions = analyze_transactions(self.nodes, valid)
        report = {
            "nodes": len(self.nodes), "edges": len(valid),
            "unresolved": dict(self.unresolved.most_common()),
            "unresolved_samples": {k: v[:8] for k, v in self.unresolved_samples.items()},
            "resolved_by": dict(self.resolved_by.most_common()),
            "external": dict(self.external),
            "runtime": runtime,
            "drift": drift,
            "transactions": transactions,
            "dropped_edges": len(self.edges) - len(valid),
        }
        return list(self.nodes.values()), valid, report

    def _miss(self, key, via, path):
        ns = key.split(":", 1)[0]
        self.unresolved[ns] += 1
        if len(self.unresolved_samples[ns]) < 20:
            self.unresolved_samples[ns].append({"key": key, "via": via, "file": path})

    def _is_external(self, key: str) -> bool:
        """class:/method: refs into packages that do not exist in the repository are
        library dependencies, not gaps in the graph."""
        ns, _, v = key.partition(":")
        if ns not in ("class", "method") or "." not in v:
            return False
        pkg = ".".join(v.split("#")[0].split(".")[:-1])
        # the class lives in a package the repository does not have (an ancestor such as
        # "org.example" is shared by app and library, so the full package is compared)
        return pkg not in self.repo_pkgs

    def _resolve_ref(self, r):
        targets = self.resolve_key(r["key"], dict(r.get("props") or {}, _src=r["src"], _via=r.get("via")))
        if not targets:
            if self._is_external(r["key"]) or self._ts_external(r):
                self.external[r["key"].split(":", 1)[0]] += 1
                return
            if not r.get("optional"):
                self._miss(r["key"], r.get("via", ""), self.nodes[r["src"]].file if r["src"] in self.nodes else "")
            return
        for t, c in targets:
            props = {"conf": round(float(r.get("conf", 1.0)) * c, 3), "src": _src_kind(r.get("via", "")),
                     "via": r.get("via"), "line": r.get("line")}
            for k, v in (r.get("props") or {}).items():       # rule / extractor attributes travel with the edge
                if k not in ("tables", "reverse") and v is not None:
                    props[k] = v
            if (r.get("props") or {}).get("reverse"):         # the edge points at the referring element
                self.add_edge(t, r["src"], r["rel"], props)
            else:
                self.add_edge(r["src"], t, r["rel"], props)

    # ------------------------------------------------------------ indexes
    def _build_indexes(self):
        self.repo_pkgs = set()
        for n in self.nodes.values():
            if n.kind in (Kind.CLASS, Kind.TEST_CLASS):
                parts = n.props.get("fqn", "").split(".")[:-1]
                self.repo_pkgs.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        self.class_by_simple = defaultdict(list)
        self.file_by_base = defaultdict(list)
        self.endpoints = []
        self.cols_by_name = defaultdict(list)
        self.name_index = defaultdict(set)
        self.ids = defaultdict(set)
        for n in self.nodes.values():
            for sid in node_system_ids(n):
                self.ids[sid].add(n.id)
            if n.kind in (Kind.CLASS, Kind.TEST_CLASS):
                self.class_by_simple[n.name].append(n.id)
                self.name_index[n.name.lower()].add(n.id)
                self.name_index[n.props.get("fqn", "").lower()].add(n.id)
            elif n.kind == Kind.FIELD:
                self.name_index[n.name.lower()].add(n.id)              # Class.field
            elif n.kind == Kind.TABLE:
                self.name_index[n.name.lower()].add(n.id)
            elif n.kind == Kind.COLUMN:
                self.name_index[n.name.lower()].add(n.id)              # table.column
                self.cols_by_name[n.name.split(".")[-1].lower()].append(n.id)
            elif n.kind == Kind.ENDPOINT:
                path = n.props.get("path")
                if path:
                    rx = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(path.rstrip("/") or "/")) + "/?$"
                    self.endpoints.append((re.compile(rx), n.id, n.props.get("verb")))
                    self.name_index[path.lower()].add(n.id)
            elif n.kind in (Kind.TEMPLATE,):
                self.name_index[n.name.lower()].add(n.id)
            elif n.kind in (Kind.JOB, Kind.JOBNET, Kind.BATCH_JOB):
                self.name_index[n.name.lower()].add(n.id)
                self.name_index[n.name.rsplit("/", 1)[-1].lower()].add(n.id)
            elif n.kind == Kind.DB_OBJECT:
                self.name_index[n.name.split(" ", 1)[-1].lower()].add(n.id)
            if n.file:
                self.file_by_base[n.file.rsplit("/", 1)[-1].lower()].append(n.id) if n.id == f"file:{n.file}" else None

    # ------------------------------------------------------------ resolution
    def resolve_key(self, key: str, props: dict | None = None) -> list[tuple[str, float]]:
        nk = norm_key(key)
        ns, _, v = nk.partition(":")
        hit = self.keys.get(nk)
        if hit:
            self.resolved_by[f"{ns}:exact"] += 1
            return [(h, 1.0 / len(hit) if len(hit) > 1 else 1.0) for h in hit]
        fn = getattr(self, f"_ns_{ns}", None)
        out = fn(v, props or {}) if fn else []
        if out:
            self.resolved_by[f"{ns}:fallback"] += 1
        return out

    # ------------------------------------------------------------ TypeScript modules
    def _ts_init(self, facts_list):
        """Module table, re-exports (barrel files), tsconfig `paths` and workspace packages."""
        self.ts_modules = {k[6:] for k in self.keys if k.startswith("tsmod:")}
        self.ts_reexp = defaultdict(list)
        self.ts_configs, self.ts_packages = [], {}
        for f in facts_list:
            for e in f.payload.get("ts_reexports", []):
                self.ts_reexp[e["module"]].append(e)
            if f.payload.get("ts_config"):
                self.ts_configs.append(f.payload["ts_config"])
            if f.payload.get("ts_package"):
                p = f.payload["ts_package"]
                self.ts_packages[p["name"]] = p
        self.ts_configs.sort(key=lambda c: -len(c["dir"]))          # nearest tsconfig first
        self._ts_spec_cache = {}

    def _ts_existing(self, mod: str) -> list[str]:
        return [m for m in (mod, f"{mod}/index") if m in self.ts_modules]

    def _ts_spec(self, spec: str, importer: str) -> list[str]:
        """An import specifier that is not relative (`@/lib/x`, `@scope/pkg/sub`) -> module paths."""
        from ..extractors.ts import module_of
        ck = (spec, importer.split("/", 1)[0])
        if ck in self._ts_spec_cache and not any(c["dir"] for c in self.ts_configs):
            return self._ts_spec_cache[ck]
        out = []
        for name in sorted(self.ts_packages, key=len, reverse=True):          # workspace packages
            if spec != name and not spec.startswith(name + "/"):
                continue
            p = self.ts_packages[name]
            sub = "." + spec[len(name):]
            targets = []
            if sub in p["exports"]:
                targets.append(p["exports"][sub])
            for k, t in p["exports"].items():
                if k.endswith("*") and sub.startswith(k[:-1]):
                    targets.append(t.replace("*", sub[len(k) - 1:]))
            if sub == "." and p.get("main"):
                targets.append(p["main"])
            rest = sub[2:] if sub != "." else "index"
            targets += [f"src/{rest}", rest]
            for t in targets:
                out += self._ts_existing(module_of(posixpath.normpath(posixpath.join(p["dir"], t))))
            if out:
                break
        if not out:
            for cfg in self.ts_configs:                                      # tsconfig paths
                if cfg["dir"] and not importer.startswith(cfg["dir"] + "/"):
                    continue
                base = posixpath.normpath(posixpath.join(cfg["dir"], cfg.get("baseUrl") or "."))
                for pat, targets in sorted(cfg["paths"].items(), key=lambda kv: -len(kv[0])):
                    if pat.endswith("*") and spec.startswith(pat[:-1]):
                        rest = spec[len(pat) - 1:]
                    elif pat == spec:
                        rest = ""
                    else:
                        continue
                    for t in targets:
                        out += self._ts_existing(module_of(posixpath.normpath(posixpath.join(base, t.replace("*", rest)))))
                    if out:
                        break
                if out:
                    break
        if not out and spec[:2] in ("@/", "~/", "#/"):
            # no tsconfig seen: the usual alias for the package's src folder
            top = importer.split("/", 1)[0]
            rest = spec[2:]
            out = [m for m in self.ts_modules if m.startswith(top + "/") and
                   (m.endswith("/src/" + rest) or m.endswith("/src/" + rest + "/index"))][:2]
        self._ts_spec_cache[ck] = out
        return out

    def _ts_lookup(self, mod: str, name: str, depth: int = 0) -> list[tuple[str, float]]:
        for m in self._ts_existing(mod) or [mod]:
            hit = self.keys.get(f"tsexp:{m}#{name}")
            if hit:
                return [(h, 1.0) for h in hit]
            if "." in name:                                  # Cls.member
                head, _, member = name.partition(".")
                for h in self.keys.get(f"tsexp:{m}#{head}", []):
                    if f"{h}.{member}" in self.nodes:
                        return [(f"{h}.{member}", 1.0)]
            if depth < 6:
                for e in self.ts_reexp.get(m, []):
                    targets = [e["target"]] if e.get("target") else self._ts_spec(e["spec"], e["from"])
                    for t in targets:
                        if e.get("as"):                      # export * as ns from "./x"
                            if name == e["as"]:
                                mods = self._ts_existing(t)
                                return [(i, 0.9) for i in self.keys.get(f"tsmod:{mods[0]}", [])] if mods else []
                            continue
                        if e.get("names"):
                            if name.split(".")[0] in e["names"]:
                                inner = e["names"][name.split(".")[0]] + name[len(name.split(".")[0]):]
                                r = self._ts_lookup(t, inner, depth + 1)
                                if r:
                                    return r
                            continue
                        r = self._ts_lookup(t, name, depth + 1)
                        if r:
                            return r
        return []

    def _ns_tsexp(self, v, props):
        mod, _, name = v.rpartition("#")
        r = self._ts_lookup(mod, name)
        if r:
            return r
        mods = self._ts_existing(mod)
        if mods:            # the module is there, the name is not (a type-only or computed export)
            return [(i, 0.6) for i in self.keys.get(f"tsmod:{mods[0]}", [])]
        return []

    def _ns_tsimp(self, v, props):
        spec, _, name = v.rpartition("#")
        src = props.get("_src")
        importer = self.nodes[src].file if src in self.nodes and self.nodes[src].file else ""
        for m in self._ts_spec(spec, importer):
            r = self._ns_tsexp(f"{m}#{name}", props)
            if r:
                return [(t, round(c * 0.95, 3)) for t, c in r]
        return []

    def _ts_external(self, r) -> bool:
        """An import of a library (react, next, zod…) is a dependency, not a gap in the graph."""
        if not r["key"].startswith("tsimp:"):
            return False
        spec = r["key"][6:].rpartition("#")[0]
        src = self.nodes.get(r["src"])
        if self._ts_spec(spec, src.file if src and src.file else ""):
            return False
        self.external["tsimp"] += 1
        return True

    def _ns_ejbmethod(self, v, props):
        """ejb-jar.xml <method-name>*</method-name>: every business method of that EJB."""
        bean, _, m = v.partition("#")
        if m != "*":
            return []
        pre = f"ejbmethod:{bean}#"
        return [(i, 1.0) for k, ids in self.keys.items() if k.startswith(pre) for i in ids]

    def _ns_class(self, v, props):
        v = v.replace("$", ".")
        if f"class:{v}" in self.keys:
            return [(i, 1.0) for i in self.keys[f"class:{v}"]]
        simple = v.rsplit(".", 1)[-1]
        c = self.class_by_simple.get(simple, [])
        if len(c) == 1:
            return [(c[0], 0.8)]
        if 1 < len(c) <= 3:
            return [(x, 0.5 / len(c)) for x in c]
        return []

    def _ns_method(self, v, props):
        cls, _, name = v.partition("#")
        r = self.java.resolve(cls, None) if hasattr(self, "java") else None
        if r:
            ms = self.java.methods(r, name, None)
            return [(self.java.mid(o, m), 0.9) for o, m in ms[:3]]
        return []

    def _ns_field(self, v, props):
        return []

    def _ns_propath(self, v, props):
        """Type.a.b -> field b of the type of field a of Type."""
        parts = v.split(".")
        for i in range(len(parts) - 1, 0, -1):
            tname = ".".join(parts[:i])
            fq = self.java.resolve(tname, None)
            if fq:
                cur = fq
                fid = None
                for p in parts[i:]:
                    f = self.java.fields(cur).get(p) if cur in self.java.cls else None
                    if not f:
                        return []
                    fid = f"java:{f[0]}.{p}"
                    cur = self.java._elem(f[1]["type"], self.java.cls[f[0]])
                return [(fid, 0.95)] if fid else []
        return []

    def _ns_prop(self, v, props):
        """`form.remarks` / `output.reserve.remarks`: the variable name is matched to a
        class by name; when that fails, shorter suffixes are tried (reserve.remarks)."""
        parts = v.split(".")
        for start in range(0, len(parts) - 1):
            var, rest = parts[start], ".".join(parts[start + 1:])
            cands = self.class_by_simple.get(var[:1].upper() + var[1:], [])
            if not cands and var.endswith("s"):
                cands = self.class_by_simple.get(var[:1].upper() + var[1:-1], [])
            out = []
            for cid in cands[:3]:
                r = self._ns_propath(f"{cid[5:]}.{rest}", props)
                out += [(t, c * (0.9 if start == 0 else 0.75) / len(cands)) for t, c in r]
            if out:
                return out
        return []

    def _ns_url(self, v, props):
        verb, _, path = v.partition(" ") if " " in v else (None, "", v)
        path = path or v
        path = re.sub(r"\$\{[^}]*\}", "", path).split("?")[0].rstrip("/") or "/"
        out = []
        for rx, nid, ev in self.endpoints:
            if rx.match(path) and (verb is None or ev in (verb, "ANY")):
                out.append(nid)
        return [(o, 0.85 / len(out)) for o in out[:4]] if out else []

    def _ns_view(self, v, props):
        cands = {v.lstrip("/"), "/" + v.lstrip("/"), v.rsplit(".", 1)[0], v.lstrip("/").rsplit(".", 1)[0]}
        for pre in ("WEB-INF/views/", "WEB-INF/jsp/", "WEB-INF/", "templates/"):
            s = v.lstrip("/")
            if s.startswith(pre):
                cands.add(s[len(pre):].rsplit(".", 1)[0])
        for c in cands:
            if f"view:{c}" in self.keys:
                return [(i, 0.9) for i in self.keys[f"view:{c}"]]
        return []

    def _ns_file(self, v, props):
        v = re.sub(r"\$\{?\w+\}?/?", "", v)
        if f"file:{v}" in self.keys:
            return [(i, 1.0) for i in self.keys[f"file:{v}"]]
        base = v.rsplit("/", 1)[-1].lower()
        c = self.file_by_base.get(base, [])
        if len(c) == 1:
            return [(c[0], 0.85)]
        if 1 < len(c) <= 4:
            tail = v.lower()
            best = [x for x in c if x.lower().endswith(tail)]
            if len(best) == 1:
                return [(best[0], 0.9)]
            return [(x, 0.6 / len(c)) for x in c]
        return []

    def _ns_column(self, v, props):
        if f"column:{v}" in self.keys:
            return [(i, 1.0) for i in self.keys[f"column:{v}"]]
        return []

    def _ns_colany(self, v, props):
        tables = [t.lower() for t in props.get("tables") or []]
        if tables:
            out = [i for t in tables for i in self.keys.get(f"column:{t}.{v}", [])]
            return [(o, 1.0 / len(out)) for o in out] if out else []
        out = self.cols_by_name.get(v, [])
        return [(o, 1.0 / len(out)) for o in out[:6]] if out else []

    def _ns_datafile(self, v, props):
        base = datafile_base(v)
        if not base or "." not in base:
            return []
        nid = f"datafile:{base}"
        if nid not in self.nodes:
            self.nodes[nid] = Node(nid, Kind.DATA_FILE, base, None, None, None, words(base), {"paths": [v]})
            self.keys[f"datafile:{base}"].append(nid)
        else:
            self.nodes[nid].props.setdefault("paths", [])
            if v not in self.nodes[nid].props["paths"]:
                self.nodes[nid].props["paths"].append(v)
        return [(nid, 1.0)]

    def _ns_mention(self, v, props):
        hit = self.name_index.get(v.lower())
        if hit and len(hit) <= 3:
            # an identifier written in a document that names exactly one element is strong
            # evidence; ambiguous names share the confidence
            return [(h, 0.95 if len(hit) == 1 else 0.8 / len(hit)) for h in hit]
        for k in (f"table:{v.lower()}", f"view:{v}", f"url:{v}", f"msg:{v}", f"job:{v}"):
            if k in self.keys and len(self.keys[k]) <= 3:
                return [(h, 0.7) for h in self.keys[k]]
        return self._ns_sysid(v, props)

    # ------------------------------------------------------------ same column name, several tables
    def _ambiguous_col(self, key: str, props) -> bool:
        if not key.lower().startswith("colany:") or (props or {}).get("tables"):
            return False
        return len(self.cols_by_name.get(key.split(":", 1)[1].lower(), [])) > 1

    def _group(self, src_id: str, path: str) -> str:
        """Where the context of a reference is shared: the sheet of a document row, else the file."""
        if src_id.startswith("doc:") and "!" in src_id:
            return src_id.split("!", 1)[0]
        return path

    def _settle_ambiguous(self):
        """A column written without its table (CUST_CD in a design sheet, a layout, a report
        field, a resultMap) is linked only to the table its context points to: a table named
        in the file or sheet name, else the table the other references of the same sheet /
        file go to. Without such a context it is not linked at all, rather than linked to
        every table that has a column of that name; those are counted as ambiguous."""
        if not self._amb:
            return
        votes = defaultdict(Counter)
        for e in self.edges.values():
            sn = self.nodes.get(e.src)
            if sn is None or not sn.file:
                continue
            t = e.dst[6:] if e.dst.startswith("table:") else e.dst[4:].rsplit(".", 1)[0] if e.dst.startswith("col:") else None
            if t:
                votes[self._group(e.src, sn.file)][t] += 1
                if self._group(e.src, sn.file) != sn.file:
                    votes[sn.file][t] += 0.25
        # second opinion: the table that has the most of the columns this sheet / file names
        cover = defaultdict(Counter)
        for kind, r, path in self._amb:
            key = r["key"] if kind == "ref" else r["to"]
            sid = r["src"] if kind == "ref" else path
            for c in self.cols_by_name.get(key.split(":", 1)[1].lower(), []):
                cover[self._group(sid, path)][c[4:].rsplit(".", 1)[0]] += 1
        self.ambiguous = Counter()
        for kind, r, path in self._amb:
            key = r["key"] if kind == "ref" else r["to"]
            src_ids = [r["src"]] if kind == "ref" else [x for x, _ in self.resolve_key(r["from"])]
            if not src_ids:
                continue
            col = key.split(":", 1)[1].lower()
            cands = self.cols_by_name.get(col, [])
            tables = {c[4:].rsplit(".", 1)[0]: c for c in cands if c.startswith("col:")}
            ctx = " ".join([path, src_ids[0], str((r.get("props") or {}).get("context", ""))]).lower()
            named = [t for t in tables if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", ctx)]
            pick = named
            if not pick:
                g = self._group(src_ids[0] if kind == "ref" else path, path)
                v = votes.get(g) or votes.get(path) or Counter()
                cv = cover.get(g) or Counter()
                scored = sorted(((v.get(t, 0), cv.get(t, 0), t) for t in tables), reverse=True)
                if scored and (scored[0][0] > 0 or scored[0][1] > 1) and \
                        (len(scored) == 1 or scored[0][:2] > scored[1][:2]):
                    pick = [scored[0][2]]
            if len(pick) != 1:
                self.ambiguous[path] += 1
                continue
            target = tables[pick[0]]
            if kind == "ref":
                rr = dict(r)
                rr["key"] = f"column:{pick[0]}.{col}"
                self._resolve_ref(rr)
            else:
                for sa, ca in self.resolve_key(r["from"]):
                    self.add_edge(sa, target, r["rel"], {"conf": round(r["conf"] * ca, 3), "src": r.get("src_kind", "rule"),
                                                         "via": r.get("via"), "line": r.get("line")})

    def out_edges_of(self, nid: str):
        if not hasattr(self, "_out_idx"):
            self._out_idx = defaultdict(list)
            for e in self.edges.values():
                self._out_idx[e.src].append((e.dst, e.rel))
        return self._out_idx.get(nid, [])

    def _value_flow(self):
        """Screen input -> column through the code (pipeline/flow.py)."""
        from .flow import Flow
        try:
            fl = Flow(self.java, self)
            found = fl.run()
        except RecursionError:
            return
        self.flow_stats = {"locations": len(fl.edges), "pairs": len(found)}
        cache = {}
        for src, colkey, usage, hops in found:
            if colkey not in cache:
                kind, _, ref = colkey.partition(":")
                cache[colkey] = self.resolve_key(f"column:{ref.lower()}" if kind == "COL" else f"colany:{ref.lower()}")
            targets = cache[colkey]
            if not targets or len(targets) > 3:
                continue
            if src.startswith("reqparam:") and src not in self.nodes:
                k = src.split(":", 1)[1]
                self.add_node({"id": src, "kind": Kind.FIELD, "name": f"request.{k}", "file": None, "line": None,
                               "end_line": None, "text": f"request parameter {k}", "props": {"request_param": True},
                               "keys": [src]})
            if src not in self.nodes:
                continue
            for dst, conf in targets:
                k = (src, dst, Rel.FLOWS_TO)
                if k in self.edges:                 # written and also searched: keep both
                    e = self.edges[k]
                    us = set(str(e.props.get("usage", "")).split(",")) | {usage}
                    e.props["usage"] = ",".join(sorted(u for u in us if u))
                    continue
                self.add_edge(src, dst, Rel.FLOWS_TO, {"conf": round(0.85 * conf, 3), "src": "dataflow",
                                                       "usage": usage, "path": hops[:10], "via": "dataflow"})

    def _ns_sysid(self, v, props):
        """A screen / job / program / interface ID (HZK-G010, KOBB090, IF_ZAI_005) names the
        files and jobs that carry it in their name, whatever the spelling of separators."""
        if (props or {}).get("_via") == "mention" and "!" in str((props or {}).get("_src", "")).split("#", 1)[-1]:
            # an ID repeated in the free text of a sheet row is not what the row is about; an ID in
            # the row's own screen / job / program column (a ledger such as 画面一覧) is
            return []
        ids = system_ids(v)
        if len(ids) != 1:
            return []
        hit = self.ids.get(ids[0], ())
        src = self.nodes.get((props or {}).get("_src"))
        if src is not None and src.file:                 # a document naming its own ID
            hit = {h for h in hit if (self.nodes[h].file if h in self.nodes else None) != src.file}
        if not hit or len(hit) > 8:
            return []
        return [(h, round(0.85 / max(1, len(hit)) ** 0.5, 3)) for h in sorted(hit)]

    def _ns_datastem(self, v, props):
        """A layout definition or a document names a data file without its date part or
        extension (URIAGE layout -> URIAGE_yyyymmdd.dat)."""
        stem = datafile_base(v + ".x").rsplit(".", 1)[0]
        stem = re.sub(r"(?i)[_\-]?(layout|lay|fmt|format|def|record|rec|if|定義|レイアウト)$", "", stem)
        stem = re.sub(r"(?i)^(layout|lay|fmt|if|rec|レイアウト)[_\-]", "", stem)
        if len(stem) < 3:
            return []
        out = [n.id for n in self.nodes.values() if n.kind == Kind.DATA_FILE and n.name.rsplit(".", 1)[0] == stem]
        return [(o, 0.8 / len(out)) for o in out[:3]]

    def _form_class(self, form: str):
        for nid in self.keys.get(norm_key(f"formbean:{form}"), []):
            t = self.nodes[nid].props.get("type") if nid in self.nodes else None
            fq = self.java.resolve(t, None) if t else None
            if fq:
                return fq
        return None

    def _ns_formfield(self, v, props):
        """`form.property` of a Struts form bean: the field of its ActionForm class."""
        form, _, prop = v.partition(".")
        prop = re.sub(r"\[\d*\]", "", prop)
        if not prop:
            return []
        fq = self._form_class(form)
        if fq:
            return self._ns_propath(f"{fq}.{prop}", props)
        return []

    def _ns_strutsprop(self, v, props):
        """`/action/path|property` from <html:form action=..>: the form bean of that action."""
        action, _, prop = v.partition("|")
        action = re.sub(r"\.do$", "", action.split("?")[0])
        for key in (f"url:{action}", f"url:{action}.do", f"url:/{action.lstrip('/')}"):
            for nid in self.keys.get(norm_key(key), []):
                form = self.nodes[nid].props.get("form") if nid in self.nodes else None
                if form:
                    hit = self.keys.get(norm_key(f"formfield:{form}.{prop}")) or []
                    if hit:
                        return [(h, 0.9) for h in hit]
                    return self._ns_formfield(f"{form}.{prop}", props)
        return []

    def _ns_screen(self, v, props):
        return self._ns_mention(v, props)

    def _ns_function(self, v, props):
        return self._ns_mention(v, props)

    def _ns_job(self, v, props):
        hit = self.name_index.get(v.lower().rsplit("/", 1)[-1])
        return [(h, 0.8) for h in hit] if hit and len(hit) <= 3 else []

    def _ns_bean(self, v, props):
        c = self.class_by_simple.get(v[:1].upper() + v[1:], [])
        return [(c[0], 0.85)] if len(c) == 1 else []

    def _ns_table(self, v, props):
        return []

    # ------------------------------------------------------------ runtime / drift
    def _match_runtime(self) -> dict:
        rts = [n for n in self.nodes.values() if n.kind == Kind.RUNTIME_SQL]
        if not rts:
            return {}
        out_by_src = defaultdict(list)
        for e in self.edges.values():
            out_by_src[e.src].append(e)

        def tables(nid, depth=0):
            ts = set()
            for e in out_by_src.get(nid, []):
                if e.rel in (Rel.READS, Rel.WRITES) and e.dst.startswith("table:"):
                    ts.add(e.dst[6:])
                elif e.rel == Rel.INCLUDES and depth < 3:
                    ts |= tables(e.dst, depth + 1)
            return ts

        def columns(nid, depth=0):
            cs = set()
            for e in out_by_src.get(nid, []):
                if e.rel in (Rel.READS, Rel.WRITES) and e.dst.startswith("col:"):
                    cs.add(e.dst[4:])
                elif e.rel == Rel.INCLUDES and depth < 3:
                    cs |= columns(e.dst, depth + 1)
            return cs

        def op_of(n):
            tag = (n.props.get("tag") or "").lower()
            if tag in ("select", "insert", "update", "delete"):
                return tag.upper()
            sql = (n.props.get("sql") or "").lstrip().lower()
            m = re.match(r"(select|insert|update|delete|merge|with)", sql)
            if m:
                return "SELECT" if m.group(1) == "with" else m.group(1).upper()
            return (n.props.get("tag") or "").upper()

        stmts = []
        for n in self.nodes.values():
            if n.kind == Kind.SQL_STATEMENT and not n.id.startswith("sqlfrag:"):
                ts = frozenset(tables(n.id))
                if ts:
                    stmts.append((op_of(n), ts, frozenset(columns(n.id)), n.id))
        jac = lambda a, b: len(a & b) / len(a | b) if (a or b) else 1.0
        matched = unmatched = 0
        blind = []
        for r in rts:
            op = (r.props.get("op") or "").upper()
            rt = frozenset(r.props.get("tables") or [])
            rc = frozenset(columns(r.id))
            best, score = [], 0.0
            for o, ts, cs, sid in stmts:
                if o != op:
                    continue
                tj = jac(ts, rt)
                if tj < 0.5:
                    continue
                cj = jac(cs, rc) if (cs and rc) else 0.5
                if rc and cs and cj < 0.5:
                    continue        # same tables but a different column set: not the same statement
                sc = 0.5 * tj + 0.5 * cj
                if sc > score + 1e-9:
                    best, score = [sid], sc
                elif abs(sc - score) < 1e-9:
                    best.append(sid)
            hits, conf = (best, round(score, 2)) if best and score >= 0.6 else ([], 0)
            if hits:
                matched += 1
                r.props["match_score"] = conf
                r.props["matched"] = True
                for h in hits[:5]:
                    self.add_edge(r.id, h, Rel.OBSERVED_AS, {"conf": round(conf / len(hits[:5]), 3), "src": "runtime"})
            else:
                unmatched += 1
                r.props["matched"] = False
                blind.append({"id": r.id, "sql": (r.props.get("sql") or r.text or "")[:240],
                              "calls": r.props.get("calls"), "tables": sorted(rt)})
        # executed in production but found nowhere in the code base: dynamic SQL,
        # other systems, ad-hoc tools - a blind spot of the static graph
        blind.sort(key=lambda b: -(b.get("calls") or 0))
        return {"statements": len(rts), "matched": matched, "unmatched": unmatched, "unmatched_samples": blind[:30]}

    def _drift(self) -> dict:
        in_catalog = {n.id for n in self.nodes.values() if n.kind in (Kind.TABLE, Kind.COLUMN) and n.props.get("catalog")}
        if not in_catalog:
            return {}
        in_ddl = {e.dst for e in self.edges.values() if e.rel == Rel.DEFINES and e.src.startswith("file:")}
        ddl_cols = {i for i in in_ddl if i.startswith("col:")}
        cat_cols = {i for i in in_catalog if i.startswith("col:")}
        if not ddl_cols:
            return {"note": "no DDL files in the repository to compare with"}
        cat_tables = {i.split(":", 1)[1] for i in in_catalog if i.startswith("table:")} | \
                     {i.split(":", 1)[1].rsplit(".", 1)[0] for i in cat_cols}
        only_db = sorted(cat_cols - ddl_cols)
        # a column is "missing in the DB" only for tables the catalog covers; tables the
        # catalog does not mention at all are listed separately (partial exports are common)
        only_ddl = sorted(c for c in ddl_cols - cat_cols if c.split(":", 1)[1].rsplit(".", 1)[0] in cat_tables)
        tables_not_in_db = sorted({c.split(":", 1)[1].rsplit(".", 1)[0] for c in ddl_cols} - cat_tables)
        for i in only_db:
            self.nodes[i].props["drift"] = "DBにのみ存在（リポジトリのDDLに無い）"
        for i in only_ddl:
            if i in self.nodes:
                self.nodes[i].props["drift"] = "DDLにのみ存在（DBに無い）"
        return {"columns_only_in_db": only_db[:200], "columns_only_in_ddl": only_ddl[:200],
                "tables_not_in_catalog": tables_not_in_db[:200],
                "counts": {"only_db": len(only_db), "only_ddl": len(only_ddl), "both": len(cat_cols & ddl_cols),
                           "tables_not_in_catalog": len(tables_not_in_db)}}

    # ------------------------------------------------------------ groups
    def _groups(self):
        pkgs = [n.props.get("package", "") for n in self.nodes.values() if n.kind == Kind.CLASS and n.props.get("package")]
        prefix = os.path.commonprefix([p.split(".") for p in pkgs]) if pkgs else []
        plen = len(prefix)
        for n in self.nodes.values():
            n.props["group"] = group_of(n, plen)


def group_of(n: Node, prefix_len: int = 0) -> str:
    if not n.file:
        return {Kind.TABLE: "DB", Kind.COLUMN: "DB", Kind.MESSAGE: "メッセージ", Kind.DATA_FILE: "連携ファイル",
                Kind.CONFIG: "設定"}.get(n.kind, n.kind)
    p = n.file
    if p.startswith("design:"):
        return "設計書"
    parts = p.split("/")
    module = "/".join(parts[:parts.index("src")]) if "src" in parts else parts[0] if len(parts) > 1 else "(root)"
    pkg = n.props.get("package")
    if not pkg and n.id.startswith("java:"):
        pkg = ""
    if pkg:
        segs = pkg.split(".")
        area = segs[prefix_len] if len(segs) > prefix_len else segs[-1]
        return f"{module}:{area}" if module else area
    parent = parts[-2] if len(parts) > 1 else ""
    return f"{module}:{parent}" if module and parent and parent != module else (module or parent or "(root)")


def _src_kind(via: str) -> str:
    if via in ("mention", "office-table", "crud-matrix"):
        return "doc"
    if via in ("literal", "test-literal", "accessor"):
        return "heuristic"
    if via in ("jp1-sc", "crontab", "trigger", "db-object", "dml", "query", "annotation", "interface-binding"):
        return "parser"
    return f"rule:{via}" if via else "parser"


# ------------------------------------------------------------------ shared helpers
def node_system_ids(n: Node) -> list[str]:
    """IDs a node carries in its own name: the file name of files, the name of jobs and
    classes (a class whose name starts with an ID, e.g. Kob041Action)."""
    if n.file and n.id == f"file:{n.file}":
        return system_ids(n.file.rsplit("/", 1)[-1].rsplit(".", 1)[0])
    if n.kind in (Kind.JOB, Kind.JOBNET, Kind.CLASS, Kind.TEMPLATE, Kind.ENDPOINT, Kind.DATA_FILE, Kind.BATCH_JOB):
        return system_ids(n.name.rsplit("/", 1)[-1])
    return []
