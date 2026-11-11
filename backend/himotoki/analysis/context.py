"""What a reviewer needs around one element to decide whether it has to change.

The detail panel showed the element's own lines and a short list of callers. That is
not enough to judge a change: a column has to be seen with its table, its definition and
whoever reads or writes it; a job with the job net it belongs to, the jobs before and
after it, what it runs and the data that flows on from there. This module gathers those
views from the graph (no AI, no ranking):

* `links`: every relation of the element, both directions, grouped and named in the
  reviewer's words (呼び出し元, 読んでいる, 後続ジョブ …);
* `data`: for a column / table / SQL / program: schema (when the DDL names one), table
  and its logical name, columns, the DDL lines, and the programs, SQL and documents that
  read or write it;
* `flow`: for a job / job net / script / batch program: the job net, the jobs before and
  after, the chain of what runs (job -> shell -> program / SQL), the tables and files it
  reads and writes, and who picks those files up next.
"""
from __future__ import annotations

import re

from collections import defaultdict, deque

from ..model import Kind, Rel

# (relation, direction) -> what the other end is, seen from this element
LINK_LABEL = {
    (Rel.CALLS, "out"): "呼び出し先", (Rel.CALLS, "in"): "呼び出し元",
    (Rel.REFERENCES, "out"): "参照先", (Rel.REFERENCES, "in"): "参照元",
    (Rel.CONTAINS, "out"): "中に含むもの", (Rel.CONTAINS, "in"): "親（含まれている先）",
    (Rel.EXTENDS, "out"): "継承元", (Rel.EXTENDS, "in"): "継承しているもの",
    (Rel.IMPLEMENTED_BY, "out"): "実装", (Rel.IMPLEMENTED_BY, "in"): "実装している定義",
    (Rel.INJECTS, "out"): "注入するもの", (Rel.INJECTS, "in"): "注入されている先",
    (Rel.MAPS_TO, "out"): "対応するテーブル・列", (Rel.MAPS_TO, "in"): "対応している項目",
    (Rel.DEFINES, "out"): "定義しているもの", (Rel.DEFINES, "in"): "定義している場所",
    (Rel.POPULATES, "out"): "値を入れる先", (Rel.POPULATES, "in"): "値を入れているもの",
    (Rel.EXECUTES, "out"): "実行するSQL", (Rel.EXECUTES, "in"): "このSQLを実行する処理",
    (Rel.READS, "out"): "読んでいるもの", (Rel.READS, "in"): "読んでいる処理",
    (Rel.WRITES, "out"): "書き込む先", (Rel.WRITES, "in"): "書き込んでいる処理",
    (Rel.RENDERS, "out"): "表示する部品", (Rel.RENDERS, "in"): "この部品を表示する画面",
    (Rel.BINDS, "out"): "結びつく項目", (Rel.BINDS, "in"): "結びついている画面",
    (Rel.USES_MESSAGE, "out"): "使うメッセージ", (Rel.USES_MESSAGE, "in"): "このメッセージを使う処理",
    (Rel.INCLUDES, "out"): "取り込むもの", (Rel.INCLUDES, "in"): "取り込んでいるもの",
    (Rel.HANDLES, "out"): "処理する対象", (Rel.HANDLES, "in"): "処理しているもの",
    (Rel.RUNS, "out"): "実行するもの", (Rel.RUNS, "in"): "実行元",
    (Rel.PRECEDES, "out"): "後続", (Rel.PRECEDES, "in"): "先行",
    (Rel.OBSERVED_AS, "out"): "実行時に観測されたSQL", (Rel.OBSERVED_AS, "in"): "観測されたSQL",
    (Rel.TESTS, "out"): "テスト対象", (Rel.TESTS, "in"): "テスト",
    (Rel.DESCRIBES, "out"): "説明している対象", (Rel.DESCRIBES, "in"): "設計書",
    (Rel.DOC_CRUD, "out"): "CRUD の対象", (Rel.DOC_CRUD, "in"): "CRUD表",
    (Rel.CONFIGURES, "out"): "設定する対象", (Rel.CONFIGURES, "in"): "設定しているもの",
}
# the order groups are shown in: what usually matters first for a change
LINK_ORDER = ["呼び出し元", "呼び出し先", "読んでいる処理", "書き込んでいる処理", "値を入れているもの", "読んでいるもの",
              "書き込む先", "値を入れる先", "先行", "後続", "実行元", "実行するもの", "実行するSQL",
              "このSQLを実行する処理", "対応するテーブル・列", "対応している項目", "結びついている画面", "結びつく項目",
              "この部品を表示する画面", "表示する部品", "使うメッセージ", "このメッセージを使う処理", "親（含まれている先）",
              "中に含むもの", "定義している場所", "定義しているもの", "実装", "実装している定義", "継承元",
              "継承しているもの", "参照元", "参照先", "設計書", "CRUD表", "テスト"]
GROUP_CAP = 60
DATA_KINDS = {Kind.TABLE, Kind.COLUMN}
FLOW_KINDS = {Kind.JOB, Kind.JOBNET, Kind.CRON, Kind.SCRIPT, Kind.BATCH_JOB}
TOUCH = [Rel.READS, Rel.WRITES, Rel.POPULATES]
DOCS_IN = [Rel.DESCRIBES, Rel.DOC_CRUD]


def _brief(store, nid: str, **extra) -> dict | None:
    n = store.get(nid)
    if n is None:
        return None
    d = {"id": n.id, "name": n.name, "kind": n.kind, "layer": n.layer, "file": n.file, "line": n.line}
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def links(store, node_id: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for d, edges in (("out", store.out_edges(node_id)), ("in", store.in_edges(node_id))):
        for e in edges:
            other = e.dst if d == "out" else e.src
            label = LINK_LABEL.get((e.rel, d), f"{e.rel.lower()}（{'先' if d == 'out' else '元'}）")
            g = groups.setdefault(label, {"label": label, "rel": e.rel, "dir": d, "items": [], "total": 0})
            g["total"] += 1
            if len(g["items"]) < GROUP_CAP:
                b = _brief(store, other, at=e.props.get("line"), ops=e.props.get("ops"), src=e.props.get("src"),
                           conf=round(e.conf, 2) if e.conf < 0.95 else None)
                if b:
                    if b["kind"] == Kind.SQL_STATEMENT:
                        b["owner"] = _owner(store, other)     # a statement is known by the method it is in
                    g["items"].append(b)
    order = {l: i for i, l in enumerate(LINK_ORDER)}
    out = [g for g in groups.values() if g["items"]]
    for g in out:
        seen, uniq = set(), []
        for it in g["items"]:
            if it["id"] not in seen:
                seen.add(it["id"]); uniq.append(it)
        g["items"] = uniq
    return sorted(out, key=lambda g: (order.get(g["label"], 99), g["label"]))


# ------------------------------------------------------------------ data
def _table_of(store, col_id: str) -> str | None:
    for e in store.in_edges(col_id, [Rel.CONTAINS]):
        n = store.get(e.src)
        if n and n.kind == Kind.TABLE:
            return n.id
    return None


def _schema(n) -> str | None:
    s = n.props.get("schemas") if n else None
    if isinstance(s, dict) and s:
        vals = sorted(set(s.values()))
        return " / ".join(vals)
    return None


def _definitions(store, nid: str) -> list[dict]:
    out = []
    n = store.get(nid)
    types = (n.props.get("types") or {}) if n else {}
    for e in store.in_edges(nid, [Rel.DEFINES]):
        src = store.get(e.src)
        if src and src.file:
            out.append({"id": src.id, "file": src.file, "line": e.props.get("line") or src.line,
                        "type": types.get(src.file)})
    return out


def _owner(store, nid: str) -> dict | None:
    """A SQL statement is shown with the method / program it belongs to."""
    n = store.get(nid)
    if n is None:
        return None
    if n.kind == Kind.SQL_STATEMENT:
        best = None
        for e in store.in_edges(nid, [Rel.EXECUTES, Rel.CONTAINS]):
            p = store.get(e.src)
            if p and p.kind in (Kind.METHOD, Kind.CLASS, Kind.SQL_MAPPER, Kind.SCRIPT, Kind.DB_OBJECT, Kind.SQL_FILE):
                if p.kind == Kind.METHOD:            # the method that runs it says more than its class
                    return {"id": p.id, "name": p.name, "kind": p.kind}
                best = best or {"id": p.id, "name": p.name, "kind": p.kind}
        return best
    return None


def _users(store, targets: list[str]) -> list[dict]:
    """Who reads or writes these tables / columns, one row per element with the operations."""
    rows: dict[str, dict] = {}
    tset = set(targets)
    for t in targets:
        tn = store.get(t)
        for e in store.in_edges(t, TOUCH):
            u = store.get(e.src)
            if u is None or u.id in tset:
                continue
            r = rows.get(u.id)
            if r is None:
                r = rows[u.id] = dict(_brief(store, u.id), ops=set(), via=set(), owner=_owner(store, u.id))
                r["at"] = e.props.get("line") or u.line
            op = e.props.get("ops") or {Rel.READS: "R", Rel.WRITES: "U", Rel.POPULATES: "C"}.get(e.rel, "")
            r["ops"].update(op)
            if tn and tn.kind == Kind.COLUMN:
                r["via"].add(tn.name.split(".")[-1])
    out = []
    for r in rows.values():
        r["ops"] = "".join(c for c in "CRUDT" if c in r["ops"])
        r["via"] = sorted(r["via"])
        out.append(r)
    layer_order = {"code": 0, "data": 1, "feature": 2, "test": 3, "doc": 4}
    return sorted(out, key=lambda r: (layer_order.get(r["layer"], 5), r.get("file") or "", r.get("at") or 0))


def _docs(store, targets: list[str]) -> list[dict]:
    out, seen = [], set()
    for t in targets:
        for e in store.in_edges(t, DOCS_IN):
            if e.src in seen:
                continue
            seen.add(e.src)
            b = _brief(store, e.src, at=e.props.get("line"), ops=e.props.get("ops"))
            if b:
                out.append(b)
    return out


def _table_view(store, tid: str, focus_cols: list[str] | None = None) -> dict:
    t = store.get(tid)
    cols = []
    for e in store.out_edges(tid, [Rel.CONTAINS]):
        c = store.get(e.dst)
        if c and c.kind == Kind.COLUMN:
            types = c.props.get("types") or {}
            at = min((e2.props.get("line") or 10**6 for e2 in store.in_edges(c.id, [Rel.DEFINES])), default=10**6)
            cols.append({"id": c.id, "name": c.name.split(".")[-1], "type": next(iter(types.values()), None),
                         "comment": c.props.get("comment"), "focus": bool(focus_cols and c.id in focus_cols), "_at": at})
    cols.sort(key=lambda x: (x.pop("_at"), x["name"]))       # in the order of the DDL
    return {"id": tid, "name": t.name if t else tid.split(":", 1)[-1], "logical": t.props.get("comment") if t else None,
            "schema": _schema(t), "definitions": _definitions(store, tid), "columns": cols}


def data_context(store, node_id: str, follow_calls: int = 0) -> dict | None:
    n = store.get(node_id)
    if n is None:
        return None
    if n.kind == Kind.COLUMN:
        tid = _table_of(store, node_id)
        table = _table_view(store, tid, [node_id]) if tid else None
        col = {"id": n.id, "name": n.name.split(".")[-1], "logical": n.props.get("comment"),
               "types": n.props.get("types") or {}, "definitions": _definitions(store, node_id)}
        col["lengths"] = length_facts(store, node_id)
        users = _users(store, [node_id])
        # whoever touches the whole table (SELECT *, INSERT without a column list) may be affected too
        table_only = [u for u in _users(store, [tid]) if u["id"] not in {x["id"] for x in users}] if tid else []
        return {"type": "column", "schema": (table or {}).get("schema"), "table": table, "column": col,
                "users": users, "table_users": table_only[:80], "docs": _docs(store, [node_id] + ([tid] if tid else []))}
    if n.kind == Kind.TABLE:
        table = _table_view(store, node_id)
        cols = [c["id"] for c in table["columns"]]
        return {"type": "table", "schema": table["schema"], "table": table, "users": _users(store, [node_id] + cols),
                "docs": _docs(store, [node_id])}
    # programs and SQL: which tables and columns they touch (through the SQL they contain)
    touched: dict[str, dict] = {}
    frontier, seen = [node_id], {node_id}
    for depth in range(3 + 2 * follow_calls):
        nxt = []
        for x in frontier:
            for e in store.out_edges(x, TOUCH):
                t = store.get(e.dst)
                if t is None or t.kind not in (Kind.TABLE, Kind.COLUMN, Kind.DATA_FILE):
                    continue
                tid = t.id if t.kind != Kind.COLUMN else (_table_of(store, t.id) or t.id)
                tn = store.get(tid)
                row = touched.setdefault(tid, {"id": tid, "name": tn.name if tn else tid, "kind": tn.kind if tn else t.kind,
                                               "logical": tn.props.get("comment") if tn else None,
                                               "schema": _schema(tn), "ops": set(), "columns": set(), "at": []})
                row["ops"].update(e.props.get("ops") or {Rel.READS: "R", Rel.WRITES: "U", Rel.POPULATES: "C"}[e.rel])
                if t.kind == Kind.COLUMN:
                    row["columns"].add(t.name.split(".")[-1])
                if e.props.get("line"):
                    row["at"].append(e.props["line"])
            rels = [Rel.CONTAINS, Rel.EXECUTES] + ([Rel.CALLS] if follow_calls else [])
            for e in store.out_edges(x, rels):
                c = store.get(e.dst)
                if c and c.kind in (Kind.METHOD, Kind.SQL_STATEMENT) and c.id not in seen:
                    seen.add(c.id)
                    nxt.append(c.id)
        frontier = nxt
    if not touched:
        return None
    rows = []
    for r in touched.values():
        r["ops"] = "".join(c for c in "CRUDT" if c in r["ops"])
        r["columns"] = sorted(r["columns"])
        r["at"] = sorted(set(r["at"]))[:8]
        rows.append(r)
    return {"type": "touches", "tables": sorted(rows, key=lambda r: (r["kind"] != Kind.TABLE, r["name"]))}


# ------------------------------------------------------------------ lengths (桁の流れ)
def _first_len(v) -> int | None:
    m = re.match(r"\s*(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def length_facts(store, column_id: str) -> dict:
    """Every place that states the length of a column's value: the DDL, design-document
    rows, interface layouts and report fields linked to the column, and the screen inputs
    and validation rules of the form fields of the same name. Lengths that differ from
    the DDL are flagged, a common source of truncation and overflow bugs."""
    from ..text import snake
    col = store.get(column_id)
    if col is None:
        return {"facts": [], "mismatch": False}
    facts = []
    if col.props.get("length"):
        facts.append({"where": "DDL", "name": col.name, "file": next(iter(col.props.get("types") or {}), None),
                      "length": str(col.props["length"]), "id": col.id})
    for e in store.in_edges(column_id):
        ln = e.props.get("length") or e.props.get("maxlength")
        src = store.get(e.src)
        if src is not None and (ln or src.props.get("length")):
            facts.append({"where": _LEN_WHERE.get(src.kind, src.kind), "name": src.name, "file": src.file,
                          "line": e.props.get("line") or src.line, "length": str(ln or src.props.get("length")),
                          "id": src.id})
    cname = col.name.split(".")[-1].lower()
    seen_fields = 0
    for fd in store.nodes([Kind.FIELD]):
        if snake(fd.name.split(".")[-1]).lower() != cname or seen_fields > 20:
            continue
        seen_fields += 1
        for e in store.in_edges(fd.id, [Rel.BINDS]):
            ln = e.props.get("maxlength")
            src = store.get(e.src)
            if ln and src is not None:
                facts.append({"where": "画面" if src.kind == Kind.TEMPLATE else "入力チェック", "name": src.name,
                              "file": src.file, "line": e.props.get("line"), "length": str(ln), "id": src.id,
                              "field": fd.id})
    base = _first_len(col.props.get("length"))
    for f in facts:
        f["differs"] = bool(base and _first_len(f["length"]) not in (None, base))
    return {"facts": facts, "mismatch": any(f["differs"] for f in facts)}


_LEN_WHERE = {Kind.DOC_ENTITY: "設計書", Kind.LAYOUT_FIELD: "連携レイアウト", Kind.XML_ELEMENT: "帳票・XML定義",
              Kind.TEMPLATE: "画面"}


# ------------------------------------------------------------------ job flow
def _owning_jobs(store, node_id: str) -> list[str]:
    """Jobs that (eventually) run this element: job <- script <- script <- class."""
    jobs, seen, q = [], {node_id}, deque([(node_id, 0)])
    while q:
        cur, d = q.popleft()
        n = store.get(cur)
        if n and n.kind in (Kind.JOB, Kind.CRON) and cur != node_id:
            jobs.append(cur)
            continue
        if d >= 4:
            continue
        for e in store.in_edges(cur, [Rel.RUNS]):
            if e.src not in seen:
                seen.add(e.src); q.append((e.src, d + 1))
    return jobs


def _run_chain(store, start: str, max_depth: int = 4) -> list[dict]:
    """Depth-first chain of what runs, with depth, so it reads like the job's script."""
    out, seen = [], {start}

    def walk(nid: str, depth: int):
        if depth > max_depth:
            return
        edges = sorted(store.out_edges(nid, [Rel.RUNS]), key=lambda e: (e.props.get("line") or 0))
        for e in edges:
            if e.dst in seen:
                continue
            seen.add(e.dst)
            b = _brief(store, e.dst, depth=depth, at=e.props.get("line"),
                       conf=round(e.conf, 2) if e.conf < 0.95 else None)
            if b is None:
                continue
            n = store.get(e.dst)
            # shared environment / function libraries are listed but not expanded
            b["common"] = bool(n.file and n.kind == Kind.SCRIPT and any(seg in ("common", "lib", "env") for seg in n.file.lower().split("/")[:-1])
                               and not store.out_edges(n.id, TOUCH))
            out.append(b)
            if not b["common"]:
                walk(e.dst, depth + 1)

    walk(start, 1)
    return out


def _data_of(store, ids: list[str]) -> list[dict]:
    """Tables and files read / written by the run chain (programs through their SQL)."""
    rows: dict[str, dict] = {}
    for nid in ids:
        # a batch program: what it does, including the DAO methods its main method calls
        ctx = data_context(store, nid, follow_calls=1)
        n = store.get(nid)
        for t in (ctx or {}).get("tables", []) if ctx and ctx.get("type") == "touches" else []:
            r = rows.setdefault(t["id"], dict(t, ops=set(), by=[]))
            r["ops"].update(t["ops"])
            if n and n.name not in r["by"]:
                r["by"].append(n.name)
    out = []
    for r in rows.values():
        r["ops"] = "".join(c for c in "CRUDT" if c in r["ops"])
        out.append(r)
    return sorted(out, key=lambda r: (r["kind"] != Kind.DATA_FILE, r["name"]))


def _downstream(store, files: list[str], exclude: set[str]) -> list[dict]:
    """Who picks a written file up next (another job's script, a transfer, an import)."""
    out = []
    for fid in files:
        readers = []
        for e in store.in_edges(fid, [Rel.READS]):
            if e.src in exclude:
                continue
            b = _brief(store, e.src)
            if b:
                b["jobs"] = [store.get(j).name for j in _owning_jobs(store, e.src) if store.get(j)]
                readers.append(b)
        if readers:
            out.append({"file": _brief(store, fid), "readers": readers})
    return out


def _net_of(store, job_id: str) -> str | None:
    for e in store.in_edges(job_id, [Rel.CONTAINS]):
        n = store.get(e.src)
        if n and n.kind == Kind.JOBNET:
            return n.id
    return None


def _net_jobs(store, net_id: str) -> list[dict]:
    jobs = []
    for e in store.out_edges(net_id, [Rel.CONTAINS]):
        c = store.get(e.dst)
        if c and c.kind in (Kind.JOB, Kind.JOBNET):
            jobs.append(c.id)
    jset = set(jobs)
    indeg = {j: 0 for j in jobs}
    nxt = defaultdict(list)
    for j in jobs:
        for e in store.out_edges(j, [Rel.PRECEDES]):
            if e.dst in jset:
                nxt[j].append(e.dst); indeg[e.dst] += 1
    order, q = [], deque(sorted(j for j in jobs if indeg[j] == 0))
    while q:
        j = q.popleft(); order.append(j)
        for k in sorted(nxt[j]):
            indeg[k] -= 1
            if indeg[k] == 0:
                q.append(k)
    order += [j for j in jobs if j not in order]
    out = []
    for j in order:
        n = store.get(j)
        out.append({"id": j, "name": n.name.split("/")[-1], "full": n.name, "kind": n.kind,
                    "comment": n.props.get("comment") or n.props.get("cm"), "next": [store.get(x).name.split("/")[-1] for x in nxt[j]]})
    return out


def flow_context(store, node_id: str) -> dict | None:
    n = store.get(node_id)
    if n is None:
        return None
    if n.kind == Kind.JOBNET:
        jobs = _net_jobs(store, node_id)
        parent = _net_of(store, node_id)
        return {"type": "jobnet", "jobnet": _brief(store, node_id, schedule=n.props.get("schedule")),
                "parent": _brief(store, parent) if parent else None, "jobs": jobs}
    jobs = [node_id] if n.kind in (Kind.JOB, Kind.CRON) else _owning_jobs(store, node_id)
    if not jobs and n.kind not in FLOW_KINDS:
        return None
    flows = []
    for jid in jobs[:3] or [node_id]:
        j = store.get(jid)
        net = _net_of(store, jid)
        chain = _run_chain(store, jid)
        run_ids = [jid] + [c["id"] for c in chain if not c.get("common")]
        data = _data_of(store, run_ids)
        written = [d["id"] for d in data if d["kind"] == Kind.DATA_FILE and set(d["ops"]) & set("CU")]
        flows.append({
            "job": _brief(store, jid, comment=j.props.get("comment") or j.props.get("cm")),
            "jobnet": _brief(store, net, schedule=store.get(net).props.get("schedule")) if net else None,
            "net_jobs": _net_jobs(store, net) if net else [],
            "before": [_brief(store, e.src) for e in store.in_edges(jid, [Rel.PRECEDES])],
            "after": [_brief(store, e.dst) for e in store.out_edges(jid, [Rel.PRECEDES])],
            "chain": chain,
            "data": data,
            "downstream": _downstream(store, written, set(run_ids)),
        })
    return {"type": "job", "focus": node_id, "flows": flows, "more_jobs": max(0, len(jobs) - 3)}


def node_context(store, node_id: str) -> dict:
    return {"links": links(store, node_id), "data": data_context(store, node_id), "flow": flow_context(store, node_id)}
