"""Tools for a coding agent that ranks for itself.

The evaluations showed the split: an agent chooses better than the graph's ranking
(the top of the list was mostly wrong), the graph reaches further than the agent
(relations that are not visible as the same words: a column read by a batch, the
interface file written from it, the job that sends it, the design-document rows).
So the graph gives facts and checks, not a ranking:

* `start_points`: what in the system the requirement names, and why (no propagation);
  the agent follows relations itself (`neighbors`, `get_element`).
* `audit_scope`: the agent's list of files to change is checked against the reach of
  the requirement. Returned are files that are strongly tied to the chosen ones
  (same column / table, interface file, job, screen, message, design-document row,
  interface and implementation) and reached from the requirement, but not in the
  list — each with the chain of relations, so the agent can decide quickly.
"""
from __future__ import annotations

import os
from collections import deque

from ..model import Kind, Rel

# relations whose two ends usually change together
STRONG = [Rel.READS, Rel.WRITES, Rel.MAPS_TO, Rel.DEFINES, Rel.POPULATES, Rel.DOC_CRUD, Rel.DESCRIBES,
          Rel.IMPLEMENTED_BY, Rel.EXTENDS, Rel.RENDERS, Rel.BINDS, Rel.HANDLES, Rel.USES_MESSAGE, Rel.RUNS,
          Rel.PRECEDES, Rel.EXECUTES, Rel.INCLUDES, Rel.CONFIGURES]
# shared things a chain may pass through (a method is an end point, not a corridor)
CORRIDOR = {Kind.COLUMN, Kind.TABLE, Kind.DATA_FILE, Kind.MESSAGE, Kind.DOC_ENTITY, Kind.DOC, Kind.JOB,
            Kind.JOBNET, Kind.SCRIPT, Kind.TEMPLATE, Kind.ENDPOINT, Kind.SQL_STATEMENT, Kind.SQL_FILE,
            Kind.DB_OBJECT, Kind.XML_ELEMENT, Kind.CONFIG, Kind.BEAN}
MAX_DEPTH = 4
HUB = {Kind.COLUMN: 400, Kind.DATA_FILE: 200, Kind.DOC_ENTITY: 200}   # wider for the things that matter most
HUB_DEFAULT = 80
TEST_DIRS = ("/test/", "src/test", "__tests__", "/e2e/")


def _is_test(path: str | None) -> bool:
    return bool(path) and (any(d in path for d in TEST_DIRS) or path.endswith(("Test.java", ".test.ts", ".spec.ts")))


def category(path: str) -> str:
    low = path.lower()
    if low.startswith("design:") or low.endswith((".xlsx", ".xlsm", ".docx", ".md")):
        return "設計書"
    if low.endswith((".sql", ".pls", ".pks", ".pkb", ".prc", ".fnc", ".trg", ".ddl")):
        return "DB"
    if low.endswith((".jsp", ".html", ".tsx", ".vue", ".js")) or "/webapp/" in low:
        return "画面"
    if low.endswith((".sh", ".ksh", ".bat", ".jp1", ".ajs")) or "cron" in low or "/jp1/" in low or "/job" in low:
        return "ジョブ・シェル"
    if low.endswith((".properties", ".yml", ".yaml")):
        return "設定・メッセージ"
    if low.endswith((".layout", ".csv", ".fmt", ".ctl")) or "layout" in low:
        return "連携・帳票定義"
    if low.endswith(".xml"):
        return "XML（マッパー・定義）"
    return "プログラム"


def start_points(engine, requirement: str, limit: int = 15) -> dict:
    """Elements the requirement names, with why — no propagation, no ranking beyond the match."""
    p = engine.preview_requirement(requirement, limit=limit)
    out = []
    for s in p["seeds"]:
        n = engine.store.get(s["node_id"])
        out.append({"id": s["node_id"], "name": s["name"], "kind": s["kind"],
                    "file": n.file if n else None, "line": n.line if n else None,
                    "why": (s.get("reason") or "")[:120]})
    return {"intents": [{"summary": i["summary"][:120], "keywords": (i.get("keywords") or [])[:8],
                         "new_terms": (i.get("new_terms") or [])[:6]} for i in p["intents"]],
            "start_points": out, "found": p["seed_count"],
            "next": "neighbors(id, direction) で1段ずつたどる。get_element(id) で中身。最後に audit_scope で答え合わせ"}


def _norm(path: str) -> str:
    return path.strip().removeprefix("./").lstrip("/")


def audit_scope(engine, requirement: str, files: list[str], limit: int = 15) -> dict:
    """Files reached from the requirement and strongly tied to the chosen files, but not chosen."""
    store = engine.store
    chosen = {_norm(f) for f in files if f and f.strip()}
    by_file = engine._file_nodes()
    known = {f for f in chosen if f in by_file or store.get(f"file:{f}")}
    unknown = sorted(chosen - known)
    # what the requirement reaches (the graph's analysis, rule-based unless the server has a key)
    rid = engine.start_run(requirement, background=False)
    st = engine.state(rid)
    reach = st["candidates"]

    # breadth-first from every element of the chosen files, over strong relations, through shared things
    start = [n.id for f in known for n in by_file.get(f, [])] + [f"file:{f}" for f in known]
    parent: dict[str, tuple[str, str]] = {}
    depth = {s: 0 for s in start}
    q = deque(start)
    degree_cache: dict[str, int] = {}
    while q:
        cur = q.popleft()
        d = depth[cur]
        if d >= MAX_DEPTH:
            continue
        node = store.get(cur)
        if d > 0 and (node is None or node.kind not in CORRIDOR):
            continue                       # reached an end point (a method, a class…): stop there
        edges = [(e, e.dst) for e in store.out_edges(cur, STRONG)] + [(e, e.src) for e in store.in_edges(cur, STRONG)]
        if d > 0:
            cap = HUB.get(node.kind, HUB_DEFAULT)
            degree_cache[cur] = len(edges)
            if len(edges) > cap:
                continue                   # a hub (a widely used table) says little about this change
        for e, other in edges:
            if other in depth:
                continue
            depth[other] = d + 1
            parent[other] = (cur, e.rel)
            q.append(other)

    def chain(nid: str) -> str:
        names, cur = [], nid
        while cur in parent:
            prev, rel = parent[cur]
            n = store.get(cur)
            names.append(f"{n.name if n else cur}")
            cur = prev
        n = store.get(cur)
        names.append(f"{n.name if n else cur}（選択済み）")
        names.reverse()
        if len(names) > 5:
            names = names[:2] + ["…"] + names[-2:]
        return " → ".join(names)

    missing: dict[str, dict] = {}
    for nid, c in reach.items():
        f = c.get("file")
        if not f or f in chosen or _is_test(f) or nid not in depth or depth[nid] == 0:
            continue
        score = c.get("confidence") or c["score"]
        cur = missing.get(f)
        if cur is None or score > cur["score"]:
            missing[f] = {"file": f, "category": category(f), "score": round(score, 2),
                          "element": c["name"], "kind": c["kind"], "via": chain(nid)}
    ranked = sorted(missing.values(), key=lambda m: -m["score"])
    # named by the requirement itself (an identifier or the wording), yet not chosen
    named = []
    for c in sorted(reach.values(), key=lambda c: -c["score"]):
        f = c.get("file")
        if (c["origin"] in ("seed", "pattern") and f and f not in chosen and f not in missing and not _is_test(f)
                and c["score"] >= 0.6):
            named.append({"file": f, "category": category(f), "element": c["name"],
                          "why": (c.get("seed_reason") or "")[:100]})
        if len(named) >= 5:
            break
    by_cat: dict[str, int] = {}
    for m in ranked:
        by_cat[m["category"]] = by_cat.get(m["category"], 0) + 1
    return {"run_id": rid, "checked_files": len(known), "unknown_files": unknown[:20],
            "not_chosen_but_tied": ranked[:limit], "more": max(0, len(ranked) - limit), "by_category": by_cat,
            "named_by_requirement_not_chosen": named,
            "note": "選んだファイルと強くつながっていて、要求からも届くのに選ばれていないもの。必ずしも変更が要るとは限らないので、"
                    "経路（via）を見て判断してください"}
