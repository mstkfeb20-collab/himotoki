"""MCP server: lets coding agents (Claude Code and others) explore the code graph.

Run on the machine that holds the repositories (e.g. next to the source-control
server):  python -m codegraph.cli mcp   — stdio transport.
Agents get structured answers (who calls this, which tables, which jobs, what a
requirement or a change set reaches) instead of reading whole files, and the
code itself never has to leave the network.
"""
from __future__ import annotations

import os

try:                                    # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                     # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .workspace import Workspace

mcp = _Server("codegraph")
_ws: Workspace | None = None


def ws() -> Workspace:
    global _ws
    if _ws is None:
        _ws = Workspace(os.environ.get("CODEGRAPH_DATA_DIR", "./data"))
    return _ws


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
                                                  "reason", "origin")} for c in cands[:top]],
            "total_candidates": len(cands),
            "checks": [{k: c[k] for k in ("key", "title", "ok", "detail")} for c in st["checks"]],
            "estimate_days": st["estimate"].get("days"), "llm_usage": st.get("llm_usage"),
            "llm_budget": st["params"].get("llm")}


@mcp.tool()
def analyze_change(base: str, head: str, graph: str | None = None) -> dict:
    """Regression scope of a change set between two git refs: entry points, screens, jobs,
    tables, tests to run and design documents to review."""
    from .sources.git import GitRepo
    e = _engine(graph)
    name = graph or ws().default()
    g = ws().meta["graphs"][name]
    gr = GitRepo(g["repo"], ws().root)
    nodes, added = e.diff_nodes(gr.hunks(gr.resolve(base), gr.resolve(head)))
    rid = e.start_run(f"変更差分 {base}..{head}", None, {"mode": "diff", "diff_nodes": nodes, "added_files": added},
                      background=False)
    st = e.state(rid)
    return {"run_id": rid, "changed_elements": len(nodes), "added_files": added, "scope": e.regression_scope(st),
            "markdown": e.scope_markdown(st, f"{base}..{head}")}


@mcp.tool()
def index_gaps(graph: str | None = None) -> dict:
    """Where the graph is known to be incomplete: unresolved references by namespace,
    DDL vs database drift, runtime SQL that no code explains."""
    st = ws().stats(graph)
    return {k: st.get(k) for k in ("unresolved", "unresolved_samples", "external", "drift", "runtime", "errors")}


def main():
    mcp.run("stdio")


if __name__ == "__main__":
    main()
