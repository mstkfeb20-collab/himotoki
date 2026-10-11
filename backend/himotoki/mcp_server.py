"""MCP server: lets coding agents (Claude Code and others) explore the code graph.

Run on the machine that holds the repositories (e.g. next to the source-control
server):  python -m himotoki.cli mcp   — stdio transport.
Agents get structured answers (who calls this, which tables, which jobs, what a
requirement or a change set reaches) instead of reading whole files, and the
code itself never has to leave the network.

Hand-off to development: `list_runs` → `get_handoff` (the reviewed scope as a
work order) → implement → `verify_change` (the actual diff against the
prediction).

Claude Code:  claude mcp add --transport stdio --env HIMOTOKI_DATA_DIR=<workspace> himotoki -- himotoki mcp
"""
from __future__ import annotations

import os

try:                                    # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .workspace import Workspace

mcp = _Server("himotoki")
_ws: Workspace | None = None


def ws() -> Workspace:
    global _ws
    if _ws is None:
        from .setup_cmds import find_workspace
        _ws = Workspace(os.environ.get("HIMOTOKI_DATA_DIR") or find_workspace() or "./data")
    return _ws


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
def get_element(node_id: str, graph: str | None = None) -> dict:
    """Details of one element: source excerpt, callers/callees, design documents, tests, change history."""
    d = _engine(graph).node_detail(node_id)
    if not d:
        return {"error": f"not found: {node_id}"}
    d["code"] = "\n".join(f"{l['n']}: {l['text']}" for l in d.get("code", [])[:80])
    d["neighbors"] = d["neighbors"][:80]
    return d


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


@mcp.tool()
def analyze_requirement(requirement: str, graph: str | None = None, top: int = 40,
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
            "candidates": [{k: c.get(k) for k in ("node_id", "kind", "name", "file", "line", "verdict", "confidence",
                                                  "reason", "origin", "tx_risks") if c.get(k) is not None}
                           for c in cands[:top]],
            "total_candidates": len(cands),
            "checks": [{k: c[k] for k in ("key", "title", "ok", "detail")} for c in st["checks"]],
            "estimate_days": st["estimate"].get("days"), "llm_usage": st.get("llm_usage"),
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
def index_gaps(graph: str | None = None) -> dict:
    """Where the graph is known to be incomplete: unresolved references by namespace,
    DDL vs database drift, runtime SQL that no code explains."""
    st = ws().stats(graph)
    return {k: st.get(k) for k in ("unresolved", "unresolved_samples", "external", "drift", "runtime", "transactions", "errors")}


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
