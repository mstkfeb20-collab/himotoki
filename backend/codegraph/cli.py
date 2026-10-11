"""Command line.

  codegraph demo                       try it: sample system of 3 servers, indexed and opened in the viewer
  codegraph init  FOLDER               make a workspace (repos/ docs/ db/ rules/ settings.yaml)
  codegraph doctor                     check the environment and the workspace, and say what to do next
  codegraph index                      inside a workspace: index repos/ (+ docs/)

  python -m codegraph.cli index   --repo PATH [--docs PATH] [--ref BRANCH] [--graph NAME]
  python -m codegraph.cli catalog postgres --dsn DSN [--name NAME]
  python -m codegraph.cli catalog oracle (--csv-dir DIR | --dsn DSN --user U --password P [--owner O]) [--name NAME]
  python -m codegraph.cli runtime (--pg DSN | --csv FILE) [--name NAME]
  python -m codegraph.cli report  [--graph NAME]                 what the graph knows it is missing
  python -m codegraph.cli analyze "要求文" [--graph NAME] [--json]
  python -m codegraph.cli diff    --base REF --head REF [--graph NAME]   regression scope of a change set
  python -m codegraph.cli serve   [--port 8765]
  python -m codegraph.cli mcp                                    MCP server on stdio for coding agents

The workspace directory is --data, else CODEGRAPH_DATA_DIR, else the workspace made by
`codegraph init` that contains the current folder, else ./data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="codegraph")
    from .setup_cmds import find_workspace
    ws_dir = find_workspace()
    ap.add_argument("--data", default=os.environ.get("CODEGRAPH_DATA_DIR") or ws_dir or "./data",
                    help="workspace folder (default: the codegraph workspace you are in, or ./data)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo", help="サンプル（3サーバの受注システム）で試す")
    p.add_argument("folder", nargs="?", default="codegraph-demo"); p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-open", action="store_true", help="ブラウザを開かない"); p.add_argument("--no-serve", action="store_true")
    p = sub.add_parser("init", help="ワークスペース（置き場所の決まったフォルダ）を作る"); p.add_argument("folder")
    p = sub.add_parser("doctor", help="環境とワークスペースを確認し、次にすることを表示"); p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("index"); p.add_argument("--repo", action="append",
                                                help="repository; repeat with --system for several (name=path or path)")
    p.add_argument("--docs"); p.add_argument("--ref"); p.add_argument("--graph")
    p.add_argument("--system", help="index the --repo repositories together as one graph with this name")
    p = sub.add_parser("catalog"); p.add_argument("kind", choices=["postgres", "oracle"]); p.add_argument("--dsn")
    p.add_argument("--csv-dir"); p.add_argument("--user"); p.add_argument("--password"); p.add_argument("--owner")
    p.add_argument("--name", default="db")
    p = sub.add_parser("runtime"); p.add_argument("--pg"); p.add_argument("--csv"); p.add_argument("--name", default="runtime")
    p.add_argument("--dialect")
    p = sub.add_parser("report"); p.add_argument("--graph")
    p = sub.add_parser("analyze"); p.add_argument("requirement"); p.add_argument("--graph"); p.add_argument("--depth", type=int, default=3)
    p.add_argument("--llm-max-items", help="AI-judged candidates for this run: a number, or 'unlimited'")
    p.add_argument("--llm-min-score", type=float, help="candidates below this score are judged by rules")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("diff"); p.add_argument("--base", required=True); p.add_argument("--head", required=True)
    p.add_argument("--graph"); p.add_argument("--repo", help="repository of a system graph that changed")
    p = sub.add_parser("serve"); p.add_argument("--port", type=int, default=8765); p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--open", action="store_true", help="ブラウザでビュワーを開く")
    sub.add_parser("mcp")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        from .setup_cmds import init_workspace
        init_workspace(a.folder)
        return
    if a.cmd == "demo":
        return _demo(a)
    if a.data == ws_dir and ws_dir:
        # inside a workspace: its rules/ folder holds the site's rule packs
        from .setup_cmds import workspace_rules
        r = workspace_rules(ws_dir)
        if r and not os.environ.get("CODEGRAPH_RULES"):
            os.environ["CODEGRAPH_RULES"] = r
    os.environ["CODEGRAPH_DATA_DIR"] = os.path.abspath(a.data)
    if a.cmd == "doctor":
        from .setup_cmds import doctor
        sys.exit(1 if doctor(ws_dir if a.data == ws_dir else None, os.path.abspath(a.data), a.port) else 0)

    if a.cmd == "serve":
        return _serve(a.host, a.port, a.open)
    if a.cmd == "mcp":

        from .mcp_server import main as mcp_main
        mcp_main()
        return

    from .workspace import Workspace
    ws = Workspace(a.data)
    if a.cmd == "index":
        progress = lambda e, d: print(f"  {e} {d}", file=sys.stderr)
        if not a.repo:
            if not (ws_dir and a.data == ws_dir):
                sys.exit("--repo でリポジトリを指定してください（codegraph init で作ったワークスペースの中なら、"
                         "引数なしで repos/ と docs/ を取り込みます）")
            from .setup_cmds import index_workspace
            st = index_workspace(ws, ws_dir, progress=progress)
            print(json.dumps({k: st[k] for k in ("nodes", "edges", "files", "seconds", "unresolved", "transactions",
                                                "repos", "errors") if k in st}, ensure_ascii=False, indent=1,
                             default=str))
            print("取り込みました。ビュワー: codegraph serve --open", file=sys.stderr)
            return
        if a.system or len(a.repo) > 1:
            if a.ref:
                sys.exit("--ref cannot be combined with several repositories")
            repos = {}
            for r in a.repo:
                n, _, pth = r.partition("=") if "=" in r else (os.path.basename(os.path.abspath(r)), "", r)
                if n in repos:
                    sys.exit(f"two repositories named {n}: use name=path")
                repos[n] = pth
            st = ws.index_system(a.system or a.graph or "system", repos, a.docs and os.path.abspath(a.docs),
                                 progress=progress)
        else:
            st = ws.index(a.repo[0], a.docs and os.path.abspath(a.docs), a.ref, a.graph, progress=progress)
        print(json.dumps({k: st[k] for k in ("nodes", "edges", "files", "parsed", "cached", "seconds", "unresolved",
                                            "runtime", "drift", "transactions", "repos") if k in st}, ensure_ascii=False, indent=1, default=str))
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
                                                 "external", "runtime", "drift", "transactions", "errors", "error_samples")},
                         ensure_ascii=False, indent=1))
        return
    e = ws.engine(a.graph)
    if e is None:
        sys.exit("index a repository first")
    if a.cmd == "diff":
        try:
            hunks = ws.change_hunks(a.graph or ws.default(), a.base, a.head, a.repo)
        except ValueError as ex:
            sys.exit(str(ex))
        nodes, added = e.diff_nodes(hunks)
        rid = e.start_run(f"変更差分 {a.base}..{a.head}", None, {"mode": "diff", "diff_nodes": nodes,
                                                              "added_files": added}, background=False)
        print(e.scope_markdown(e.state(rid), f"{a.base}..{a.head}"))
        return
    params = {"depth": a.depth}
    if a.llm_max_items is not None:
        params["llm_max_items"] = a.llm_max_items
    if a.llm_min_score is not None:
        params["llm_min_score"] = a.llm_min_score
    rid = e.start_run(a.requirement, params=params, background=False)
    st = e.state(rid)
    if st["status"] != "done":
        sys.exit(f"error: {st.get('error')}")
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
        return
    print(f"run {rid}  判定: {e.llm.name}  候補 {len(st['candidates'])} 件  規模(暫定) {st['estimate']['days']} 人日")
    u = st.get("llm_usage") or {}
    print(f"  AI判定 {u.get('ai_items', 0)} 件・再利用 {u.get('reused', 0)}・ルール判定（低スコア {u.get('rule_low_score', 0)}"
          f"・上限超過 {u.get('rule_cap', 0)}・AI未使用 {u.get('rule', 0)}）  API {u.get('calls', 0)} 回 "
          f"入力 {u.get('input_tokens', 0)} / 出力 {u.get('output_tokens', 0)} トークン  上限設定 {st['params']['llm']['max_items']}")
    for it in st["intents"]:
        print(f"  {it['id']} {it['summary']} [{it['change_type']}{', ' + it['pattern'] if it.get('pattern') else ''}]")
    top = sorted((c for c in st["candidates"].values() if c["verdict"] == "impacted"),
                 key=lambda c: -(c["confidence"] or 0))[:30]
    for c in top:
        print(f"  {c['confidence']:.2f}  {c['kind']:<12} {c['name'][:56]:<56} {c['reason'] or ''}")
    print(f"ビュワー: codegraph serve --open → http://localhost:8765/?run={rid}")


if __name__ == "__main__":
    main()


def _serve(host: str, port: int, open_browser: bool = False, path: str = ""):
    import uvicorn
    from .setup_cmds import port_free
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not os.environ.get("CODEGRAPH_API_TOKEN") and os.environ.get("CODEGRAPH_ALLOW_NO_AUTH") != "1":
        # the API serves source code: refuse to expose it on the network without a token
        sys.exit("refusing to listen on a non-local address without CODEGRAPH_API_TOKEN "
                 "(set CODEGRAPH_ALLOW_NO_AUTH=1 only behind an authenticating proxy)")
    if not local and not os.environ.get("CODEGRAPH_ALLOWED_HOSTS"):
        sys.exit("set CODEGRAPH_ALLOWED_HOSTS to the host names clients use (comma separated)")
    if not port_free(port, "127.0.0.1" if local else host):
        sys.exit(f"ポート {port} は使用中です。起動済みなら http://127.0.0.1:{port} を開いてください。"
                 f"別のポートで起動するには --port {port + 1}")
    url = f"http://{'127.0.0.1' if local else host}:{port}/{path}"
    print(f"ビュワー: {url}   （止めるときは Ctrl+C）", file=sys.stderr)
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("codegraph.api:app", host=host, port=port, proxy_headers=False, log_level="warning",
                limit_concurrency=int(os.environ.get("CODEGRAPH_MAX_CONNECTIONS", "200")))


def _demo(a):
    from .setup_cmds import DEMO_REQUIREMENT, index_workspace, prepare_demo
    from .workspace import Workspace
    ws_dir = prepare_demo(a.folder)
    os.environ["CODEGRAPH_DATA_DIR"] = ws_dir
    print(f"サンプルを置きました: {ws_dir}", file=sys.stderr)
    print("  repos/ に受注・在庫・出荷の3リポジトリ（3サーバで1つの業務）", file=sys.stderr)
    ws = Workspace(ws_dir)
    st = index_workspace(ws, ws_dir)
    print(f"取り込みました: {st['files']} ファイル → {st['nodes']} 要素・{st['edges']} 関係"
          f"（トランザクションの注意点 {sum((st.get('transactions') or {}).get('risks', {}).values())} 件）", file=sys.stderr)
    e = ws.engine()
    rid = e.start_run(DEMO_REQUIREMENT, background=False)
    n = len(e.state(rid)["candidates"])
    print(f"要求「{DEMO_REQUIREMENT}」を解析しました: 影響候補 {n} 件", file=sys.stderr)
    print("", file=sys.stderr)
    print("次に自分のシステムで試すとき:", file=sys.stderr)
    print("  codegraph init my-system  →  my-system/repos にソースを git clone  →  cd my-system  →  "
          "codegraph index  →  codegraph serve --open", file=sys.stderr)
    if a.no_serve:
        return
    _serve("127.0.0.1", a.port, not a.no_open, f"?run={rid}")
