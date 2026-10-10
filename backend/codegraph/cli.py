"""Command line.

  python -m codegraph.cli index   --repo PATH [--docs PATH] [--ref BRANCH] [--graph NAME]
  python -m codegraph.cli catalog postgres --dsn DSN [--name NAME]
  python -m codegraph.cli catalog oracle (--csv-dir DIR | --dsn DSN --user U --password P [--owner O]) [--name NAME]
  python -m codegraph.cli runtime (--pg DSN | --csv FILE) [--name NAME]
  python -m codegraph.cli report  [--graph NAME]                 what the graph knows it is missing
  python -m codegraph.cli analyze "要求文" [--graph NAME] [--json]
  python -m codegraph.cli diff    --base REF --head REF [--graph NAME]   regression scope of a change set
  python -m codegraph.cli serve   [--port 8765]
  python -m codegraph.cli mcp                                    MCP server on stdio for coding agents

The workspace directory is CODEGRAPH_DATA_DIR (default ./data); --data overrides it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="codegraph")
    ap.add_argument("--data", default=os.environ.get("CODEGRAPH_DATA_DIR", "./data"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index"); p.add_argument("--repo", required=True); p.add_argument("--docs")
    p.add_argument("--ref"); p.add_argument("--graph")
    p = sub.add_parser("catalog"); p.add_argument("kind", choices=["postgres", "oracle"]); p.add_argument("--dsn")
    p.add_argument("--csv-dir"); p.add_argument("--user"); p.add_argument("--password"); p.add_argument("--owner")
    p.add_argument("--name", default="db")
    p = sub.add_parser("runtime"); p.add_argument("--pg"); p.add_argument("--csv"); p.add_argument("--name", default="runtime")
    p.add_argument("--dialect")
    p = sub.add_parser("report"); p.add_argument("--graph")
    p = sub.add_parser("analyze"); p.add_argument("requirement"); p.add_argument("--graph"); p.add_argument("--depth", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("diff"); p.add_argument("--base", required=True); p.add_argument("--head", required=True)
    p.add_argument("--graph")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8765); p.add_argument("--host", default="127.0.0.1")
    sub.add_parser("mcp")
    a = ap.parse_args(argv)
    os.environ["CODEGRAPH_DATA_DIR"] = os.path.abspath(a.data)

    if a.cmd == "serve":
        import uvicorn
        local = a.host in ("127.0.0.1", "localhost", "::1")
        if not local and not os.environ.get("CODEGRAPH_API_TOKEN") and os.environ.get("CODEGRAPH_ALLOW_NO_AUTH") != "1":
            # the API serves source code: refuse to expose it on the network without a token
            sys.exit("refusing to listen on a non-local address without CODEGRAPH_API_TOKEN "
                     "(set CODEGRAPH_ALLOW_NO_AUTH=1 only behind an authenticating proxy)")
        if not local and not os.environ.get("CODEGRAPH_ALLOWED_HOSTS"):
            sys.exit("set CODEGRAPH_ALLOWED_HOSTS to the host names clients use (comma separated)")
        uvicorn.run("codegraph.api:app", host=a.host, port=a.port, proxy_headers=False,
                    limit_concurrency=int(os.environ.get("CODEGRAPH_MAX_CONNECTIONS", "200")))
        return
    if a.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return

    from .workspace import Workspace
    ws = Workspace(a.data)
    if a.cmd == "index":
        st = ws.index(a.repo, a.docs and os.path.abspath(a.docs), a.ref, a.graph,
                      progress=lambda e, d: print(f"  {e} {d}", file=sys.stderr))
        print(json.dumps({k: st[k] for k in ("nodes", "edges", "files", "parsed", "cached", "seconds", "unresolved",
                                            "runtime", "drift") if k in st}, ensure_ascii=False, indent=1, default=str))
        return
    if a.cmd == "catalog":
        from .sources.catalog import import_oracle, import_postgres, save_source
        f = import_postgres(a.dsn, a.name) if a.kind == "postgres" else \
            import_oracle(a.name, a.csv_dir, a.dsn, a.user, a.password, a.owner)
        print(save_source(ws.root, f), f.payload.get("catalog_counts"))
        return
    if a.cmd == "runtime":
        from .sources.catalog import save_source
        from .sources.runtime import from_csv, from_pg_stat_statements
        f = from_pg_stat_statements(a.pg, a.name) if a.pg else from_csv(a.csv, a.name, dialect=a.dialect)
        print(save_source(ws.root, f), sum(1 for n in f.nodes if n["kind"] == "RuntimeSql"), "statements")
        return
    if a.cmd == "report":
        st = ws.stats(a.graph)
        print(json.dumps({k: st.get(k) for k in ("nodes", "edges", "by_kind", "unresolved", "unresolved_samples",
                                                 "external", "runtime", "drift", "errors", "error_samples")},
                         ensure_ascii=False, indent=1))
        return
    e = ws.engine(a.graph)
    if e is None:
        sys.exit("index a repository first")
    if a.cmd == "diff":
        from .sources.git import GitRepo
        g = ws.meta["graphs"][a.graph or ws.default()]
        gr = GitRepo(g["repo"], ws.root)
        nodes, added = e.diff_nodes(gr.hunks(gr.resolve(a.base), gr.resolve(a.head)))
        rid = e.start_run(f"変更差分 {a.base}..{a.head}", None, {"mode": "diff", "diff_nodes": nodes,
                                                              "added_files": added}, background=False)
        print(e.scope_markdown(e.state(rid), f"{a.base}..{a.head}"))
        return
    rid = e.start_run(a.requirement, params={"depth": a.depth}, background=False)
    st = e.state(rid)
    if st["status"] != "done":
        sys.exit(f"error: {st.get('error')}")
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    print(f"run {rid}  判定: {e.llm.name}  候補 {len(st['candidates'])} 件  規模(暫定) {st['estimate']['days']} 人日")
    for it in st["intents"]:
        print(f"  {it['id']} {it['summary']} [{it['change_type']}{', ' + it['pattern'] if it.get('pattern') else ''}]")
    top = sorted((c for c in st["candidates"].values() if c["verdict"] == "impacted"),
                 key=lambda c: -(c["confidence"] or 0))[:30]
    for c in top:
        print(f"  {c['confidence']:.2f}  {c['kind']:<12} {c['name'][:56]:<56} {c['reason'] or ''}")
    print(f"ビュワー: python -m codegraph.cli serve → http://localhost:8765/?run={rid}")


if __name__ == "__main__":
    main()
