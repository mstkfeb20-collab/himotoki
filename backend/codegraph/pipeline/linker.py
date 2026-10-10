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
import re
from collections import Counter, defaultdict

from ..model import Edge, Kind, Node, Rel
from ..text import words
from .resolve_java import JavaResolver

CASE_INSENSITIVE = {"table", "column", "colany", "datafile", "proc", "dbobj", "screen", "function"}


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
        jr = JavaResolver(facts_list, self.rules)
        for e in jr.run():
            self.add_edge(e["src"], e["dst"], e["rel"], e["props"])
        self.java = jr
        for f in facts_list:
            for r in f.refs:
                self._resolve_ref(r)
            for lk in f.payload.get("links", []):
                a = self.resolve_key(lk["from"])
                b = self.resolve_key(lk["to"])
                for sa, ca in a:
                    for sb, cb in b:
                        self.add_edge(sa, sb, lk["rel"], {"conf": round(lk["conf"] * ca * cb, 3),
                                                          "src": lk.get("src_kind", "rule"), "via": lk.get("via"),
                                                          "line": lk.get("line")})
                if not a or not b:
                    self._miss(lk["from"] if not a else lk["to"], lk.get("via", ""), f.path)
        runtime = self._match_runtime()
        drift = self._drift()
        valid = [e for e in self.edges.values() if e.src in self.nodes and e.dst in self.nodes and e.src != e.dst]
        self._groups()
        report = {
            "nodes": len(self.nodes), "edges": len(valid),
            "unresolved": dict(self.unresolved.most_common()),
            "unresolved_samples": {k: v[:8] for k, v in self.unresolved_samples.items()},
            "resolved_by": dict(self.resolved_by.most_common()),
            "external": dict(self.external),
            "runtime": runtime,
            "drift": drift,
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
        targets = self.resolve_key(r["key"], r.get("props") or {})
        if not targets:
            if self._is_external(r["key"]):
                self.external[r["key"].split(":", 1)[0]] += 1
                return
            if not r.get("optional"):
                self._miss(r["key"], r.get("via", ""), self.nodes[r["src"]].file if r["src"] in self.nodes else "")
            return
        for t, c in targets:
            props = {"conf": round(float(r.get("conf", 1.0)) * c, 3), "src": _src_kind(r.get("via", "")),
                     "via": r.get("via"), "line": r.get("line")}
            for k in ("ops", "positional"):
                if (r.get("props") or {}).get(k) is not None:
                    props[k] = r["props"][k]
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
        for n in self.nodes.values():
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
        base = re.sub(r"\$\{?\w+\}?", "", v).rsplit("/", 1)[-1].lower()
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
