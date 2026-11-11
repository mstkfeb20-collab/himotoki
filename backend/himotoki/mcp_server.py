"""MCP server: lets coding agents (Claude Code and others) explore the code graph.

Run on the machine that holds the repositories (e.g. next to the source-control
server):  python -m himotoki.cli mcp   — stdio transport.
Agents get structured answers (who calls this, which tables, which jobs, what a
requirement or a change set reaches) instead of reading whole files, and the
code itself never has to leave the network.

For an agent working on a requirement: `impact_files` first (ranked files with the
path that leads to each, about 2k tokens), then `get_element` / `neighbors` on what it
names. The default graph is opened in the background at start-up.

Hand-off to development: `list_runs` → `get_handoff` (the reviewed scope as a
work order) → implement → `verify_change` (the actual diff against the
prediction).

Claude Code:  claude mcp add --transport stdio --env HIMOTOKI_DATA_DIR=<workspace> himotoki -- himotoki mcp
"""
from __future__ import annotations

import os
import re
import threading

try:                                    # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .workspace import Workspace

mcp = _Server("himotoki")
_ws: Workspace | None = None
_ws_lock = threading.Lock()


def ws() -> Workspace:
    global _ws
    with _ws_lock:
        if _ws is None:
            from .setup_cmds import find_workspace
            _ws = Workspace(os.environ.get("HIMOTOKI_DATA_DIR") or find_workspace() or "./data")
    return _ws


def _preload():
    """Open the default graph while the agent is still reading the task, so the first
    tool call does not pay for loading it (a large graph takes seconds to tens of seconds)."""
    try:
        ws().engine(None)
    except Exception:           # a missing or broken workspace is reported by the first tool call
        pass


def _run_engine(run_id: str):
    e = ws().find_run(run_id)
    if e is None:
        raise ValueError(f"run {run_id} not found (list_runs shows the analyses)")
    return e


def _engine(graph: str | None):
    e = ws().engine(graph)
    if e is None:
        raise ValueError("no graph indexed yet")
    return e


def _brief(n) -> dict:
    return {"id": n.id, "kind": n.kind, "name": n.name, "file": n.file, "line": n.line}


@mcp.tool()
def list_graphs() -> dict:
    """Indexed graphs (repository@branch) and the default one."""
    return {"default": ws().default(), "graphs": ws().meta["graphs"]}


@mcp.tool()
def search_elements(query: str, limit: int = 15, graph: str | None = None) -> list[dict]:
    """Find code elements (classes, methods, SQL, tables, screens, jobs, design-doc rows) by name or meaning.
    Works with Japanese business terms as well as identifiers."""
    return [dict(_brief(n), score=round(s, 3)) for n, s in _engine(graph).search.query(query, k=limit, min_score=0.05)]


@mcp.tool()
def get_element(node_id: str, lines: int = 40, graph: str | None = None) -> dict:
    """One element: where it is, its source (up to `lines` lines), what it calls and what calls it,
    tables it reads / writes, design documents describing it and tests covering it. Compact:
    relations are names and ids (follow them with get_element / neighbors)."""
    d = _engine(graph).node_detail(node_id)
    if not d:
        return {"error": f"not found: {node_id}"}
    n = d["node"]
    rel = lambda xs, k=25: [{"rel": x["rel"], "id": x["id"], "name": x["name"], "kind": x["kind"]} for x in xs[:k]]
    nb = d.get("neighbors", [])
    out = {
        "id": n["id"], "kind": n["kind"], "name": n["name"], "file": n.get("file"), "line": n.get("line"),
        "end_line": n.get("end_line"),
        "code": "\n".join(f"{l['n']}: {l['text']}" for l in d.get("code", [])[:max(0, lines)]),
        "used_by": rel([x for x in nb if x["dir"] == "in" and x["rel"] not in ("TESTS", "DESCRIBES", "CONTAINS")]),
        "uses": rel([x for x in nb if x["dir"] == "out" and x["rel"] not in ("CONTAINS",)]),
        "contains": [x["name"] for x in nb if x["dir"] == "out" and x["rel"] == "CONTAINS"][:30],
        "docs": [{"id": x["id"], "name": x["name"], "text": (x.get("text") or "")[:300]} for x in d.get("docs", [])[:4]],
        "tests": [x["name"] for x in d.get("tests", [])[:10]],
    }
    tx = (n.get("props") or {}).get("tx_risks")
    if tx:
        out["tx_risks"] = [r.get("message") for r in tx[:5]]
    return out


@mcp.tool()
def neighbors(node_id: str, direction: str = "both", relations: list[str] | None = None,
              graph: str | None = None) -> list[dict]:
    """Relations of an element. direction: in (who depends on it) | out (what it depends on) | both.
    Each edge carries how it was found (src) and a confidence (conf)."""
    e = _engine(graph)
    out = []
    if direction in ("in", "both"):
        for ed in e.store.in_edges(node_id, relations):
            n = e.store.get(ed.src)
            out.append({"dir": "in", "rel": ed.rel, **(_brief(n) if n else {"id": ed.src}),
                        "conf": ed.props.get("conf"), "src": ed.props.get("src")})
    if direction in ("out", "both"):
        for ed in e.store.out_edges(node_id, relations):
            n = e.store.get(ed.dst)
            out.append({"dir": "out", "rel": ed.rel, **(_brief(n) if n else {"id": ed.dst}),
                        "conf": ed.props.get("conf"), "src": ed.props.get("src")})
    return out[:300]


def _chain(e, st, nid, limit=4) -> str:
    """Requirement -> ... -> element, as names, with the reason at the start."""
    c = st["candidates"][nid]
    ids = c.get("path") or [nid]
    names = [st["candidates"][i]["name"] for i in ids if i in st["candidates"]]
    if len(names) > limit:
        names = names[:1] + ["…"] + names[-(limit - 2):]
    root = st["candidates"].get(ids[0], c)
    why = re.sub(r"（[^）]*）", "", root.get("seed_reason") or "")[:80]     # the long notes stay in the viewer
    return (f"{why} ⇒ " if why else "") + " → ".join(names)


@mcp.tool()
def start_points(requirement: str, limit: int = 15, graph: str | None = None) -> dict:
    """Where to start for a requirement: the elements it names (by identifier, by the wording on
    screens / in comments / in design documents, by an old value), each with why. Nothing is
    propagated or ranked beyond the match — follow relations yourself with neighbors / get_element,
    choose what has to change, then check your list with audit_scope."""
    from .analysis.audit import start_points as sp
    return sp(_engine(graph), requirement, limit)


@mcp.tool()
def audit_scope(requirement: str, files: list[str], graph: str | None = None) -> dict:
    """Check a list of files you intend to change (repository-relative paths). Returns files that
    are strongly tied to yours — the same column or table, an interface file, a job, a screen, a
    message, a design-document row, an interface and its implementation — and are reached from
    the requirement, but are not in your list, each with the chain of relations (via). Use it
    before you finish; open what it names and decide."""
    from .analysis.audit import audit_scope as au
    return au(_engine(graph), requirement, files)


@mcp.tool()
def impact_files(requirement: str, top: int = 10, graph: str | None = None) -> dict:
    """START HERE for "what has to change for this requirement / ticket?". Ranks the files most
    likely to change, each with its strongest elements and the path that leads there from the
    requirement (identifier / wording matches, calls, data access, screens, design documents).
    Deliberately short: read the files it names, confirm or widen with get_element / neighbors,
    and treat the ranking as candidates, not a verdict. Also returns tables, jobs and design
    documents reached outside the source files, and test files to look at."""
    e = _engine(graph)
    rid = e.start_run(requirement, background=False)
    st = e.state(rid)
    cands = sorted(st["candidates"].values(), key=lambda c: -(c.get("confidence") or c["score"]))
    files, data, tests = {}, [], {}
    for c in cands:
        f = c.get("file")
        sc = round(c.get("confidence") or c["score"], 2)
        if c["kind"] in ("TestClass", "TestMethod"):
            if f and f not in tests and len(tests) < 6:
                tests[f] = sc
            continue
        if not f or f.startswith("design:"):
            if len(data) < 8 and c["kind"] not in ("Column",):
                data.append({"name": c["name"], "kind": c["kind"], "score": sc, "file": f,
                             "why": _chain(e, st, c["node_id"], 3)})
            continue
        if f not in files:
            if len(files) >= top:
                continue
            files[f] = {"file": f, "score": sc, "verdict": c.get("verdict"), "impact": c.get("impact"),
                        "why": _chain(e, st, c["node_id"]), "elements": []}
        elif c.get("impact") == "change" and files[f].get("impact") != "change":
            files[f]["impact"] = "change"            # a file is edited when any element in it is
        if len(files[f]["elements"]) < 3:
            files[f]["elements"].append({"id": c["node_id"], "name": c["name"], "kind": c["kind"], "line": c.get("line")})
    all_files = {c.get("file") for c in cands if c.get("file") and not c["file"].startswith("design:")}
    return {"run_id": rid,
            "intents": [{"summary": i["summary"][:120], "keywords": (i.get("keywords") or [])[:8]} for i in st["intents"]],
            "files": list(files.values()), "data_and_docs": data,
            "tests": [{"file": f, "score": s} for f, s in tests.items()],
            "more": {"files": len(all_files), "candidates": len(cands),
                     "hint": "get_element(id) で中身とつながり、analyze_requirement で要素単位の一覧"}}


@mcp.tool()
def analyze_requirement(requirement: str, graph: str | None = None, top: int = 20,
                        llm_max_items: str | None = None) -> dict:
    """Run an impact analysis for a requirement text. Returns intents, the most likely
    impacted elements with reasons, the completeness checks and an effort estimate.
    llm_max_items caps AI-judged candidates for this run (a number or "unlimited");
    omit it to use the server's settings."""
    e = _engine(graph)
    rid = e.start_run(requirement, params={"llm_max_items": llm_max_items} if llm_max_items else {},
                      background=False)
    st = e.state(rid)
    cands = sorted(st["candidates"].values(), key=lambda c: -(c.get("confidence") or c["score"]))
    return {"run_id": rid, "intents": st["intents"],
            "candidates": [{**{k: c.get(k) for k in ("node_id", "kind", "name", "file", "line", "verdict", "impact",
                                                     "confidence", "origin") if c.get(k) is not None},
                            **({"evidence": c["evidence"]} if c.get("evidence") else {}),
                            "reason": (c.get("reason") or "")[:200],
                            **({"tx_risks": [r.get("code") for r in c["tx_risks"][:4]]} if c.get("tx_risks") else {})}
                           for c in cands[:top]],
            "total_candidates": len(cands),
            "checks": [{k: c[k] for k in ("key", "title", "ok", "detail")} for c in st["checks"]],
            "estimate_days": st["estimate"].get("days"), "estimate_by_impact": st["estimate"].get("by_impact"),
            "impact_kinds": "change=中身を直す / verify=直さないが試験で確かめる / coordinate=コードの外で調整 / none / unknown",
            "llm_usage": st.get("llm_usage"),
            "llm_budget": st["params"].get("llm")}


@mcp.tool()
def analyze_change(base: str, head: str, graph: str | None = None, repo: str | None = None) -> dict:
    """Regression scope of a change set between two git refs: entry points, screens, jobs,
    tables, tests to run and design documents to review. For a system graph of several
    repositories, `repo` names the repository that changed."""
    e = _engine(graph)
    name = graph or ws().default()
    nodes, added = e.diff_nodes(ws().change_hunks(name, base, head, repo))
    rid = e.start_run(f"変更差分 {base}..{head}", None, {"mode": "diff", "diff_nodes": nodes, "added_files": added},
                      background=False)
    st = e.state(rid)
    return {"run_id": rid, "changed_elements": len(nodes), "added_files": added, "scope": e.regression_scope(st),
            "markdown": e.scope_markdown(st, f"{base}..{head}")}


@mcp.tool()
def list_runs(graph: str | None = None, limit: int = 20) -> list[dict]:
    """Impact analyses done so far (newest first), with whether they are approved and how
    many elements were confirmed. Pick the run_id to hand off with get_handoff."""
    out = []
    names = [graph] if graph else list(ws().meta["graphs"])
    for n in names:
        e = ws().engine(n)
        if not e:
            continue
        for r in e.runs.list():
            st = e.state(r["id"]) or {}
            cands = st.get("candidates", {}).values()
            out.append({"run_id": r["id"], "graph": n, "title": r.get("title"), "requirement": (r.get("requirement") or "")[:200],
                        "created_at": r.get("created_at"), "status": r.get("status"),
                        "approved": bool(st.get("approved")),
                        "confirmed": sum(1 for c in cands if (c.get("decision") or {}).get("status") == "confirmed"),
                        "verified": bool(st.get("verification"))})
    return sorted(out, key=lambda r: -(r.get("created_at") or 0))[:limit]


@mcp.tool()
def get_handoff(run_id: str, include_pending: bool = True) -> dict:
    """The reviewed impact scope of an analysis as a work order for implementation:
    files and elements to change (with the reviewers' reasons and comments), transaction /
    integration risks, design documents to update, callers to re-check, tests to run,
    elements excluded by reviewers (do not change them), undecided candidates, and the
    rules to follow. `markdown` is the same content as a document. Stay within this scope;
    ask a human before changing anything outside it."""
    from .analysis.handoff import build_handoff, handoff_markdown
    h = build_handoff(_run_engine(run_id), run_id, include_pending)
    return dict(h, markdown=handoff_markdown(h))


@mcp.tool()
def get_issue_drafts(run_id: str, split: str = "one") -> dict:
    """Issue drafts (title, labels, Markdown body) for the reviewed scope of an analysis.
    split="one": one issue for the requirement; split="subsystem": a parent issue plus one per
    top-level folder of the files to change and one for design documents. Each body starts with
    a `himotoki-run: <id>` marker; use get_handoff with that id for the full work order. Create
    the issues with your own git tooling (e.g. `gh issue create`); himotoki does not touch git."""
    from .analysis.issues import issue_drafts
    return issue_drafts(_run_engine(run_id), run_id, "subsystem" if split == "subsystem" else "one")


@mcp.tool()
def verify_change(run_id: str, base: str, head: str, repo: str | None = None,
                  actual_days: float | None = None) -> dict:
    """After implementing: compare the actual change between two git refs with the
    analysis. Returns hits, missed changes (with whether the analysis had found them and a
    reviewer judged otherwise, or never reached them) and predicted elements left unchanged.
    `repo` names the repository in a system graph; `actual_days` (person-days spent)
    recalibrates the effort estimate. The result is stored on the run and shown in the viewer."""
    from .analysis.handoff import verification_markdown, verify_change as _verify
    e = _run_engine(run_id)
    name = next(n for n in ws().meta["graphs"] if ws().engine(n) is e)
    v = _verify(e, run_id, ws().change_hunks(name, base, head, repo), base, head, actual_days)
    return dict(v, markdown=verification_markdown(v))


@mcp.tool()
def learned_knowledge(graph: str | None = None) -> dict:
    """What earlier analyses of this system learned from reviewers' decisions and from
    verifications of implemented changes: path patterns that are usually confirmed or
    excluded, elements with a history (excluded repeatedly, missed in reality, over-predicted),
    and links between elements the dependency graph does not know but past changes showed."""
    e = _engine(graph)
    return e.knowledge.summary(e.store)


@mcp.tool()
def index_gaps(graph: str | None = None) -> dict:
    """Where the graph is known to be incomplete: unresolved references by namespace,
    DDL vs database drift, runtime SQL that no code explains."""
    st = ws().stats(graph)
    return {k: st.get(k) for k in ("unresolved", "unresolved_samples", "external", "drift", "runtime", "transactions", "errors")}


def main():
    threading.Thread(target=_preload, daemon=True).start()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
