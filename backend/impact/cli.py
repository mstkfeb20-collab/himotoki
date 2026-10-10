"""Command line entry point.

  python -m impact.cli index  --repo PATH [--docs PATH] [--data DIR]
  python -m impact.cli analyze --repo PATH "要求文" [--docs PATH] [--data DIR] [--json]
  python -m impact.cli serve  [--port 8765]
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="impact")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("index", "analyze"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--docs")
        p.add_argument("--data", default=os.environ.get("IMPACT_DATA_DIR", "./data"))
        if name == "analyze":
            p.add_argument("requirement")
            p.add_argument("--depth", type=int, default=3)
            p.add_argument("--json", action="store_true", help="print the whole run state as JSON")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("impact.api:app", host=a.host, port=a.port)
        return

    from .analysis.engine import Engine
    from .indexer import index_repository
    from .store import open_store

    os.makedirs(a.data, exist_ok=True)
    store = open_store(a.data)
    stats = index_repository(os.path.abspath(a.repo), store, a.docs and os.path.abspath(a.docs))
    json.dump({"repo": os.path.abspath(a.repo), "docs_dir": a.docs and os.path.abspath(a.docs), "stats": stats},
              open(os.path.join(a.data, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False)
    if a.cmd == "index":
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return
    eng = Engine(store, a.data, os.path.abspath(a.repo), a.docs and os.path.abspath(a.docs))
    rid = eng.start_run(a.requirement, params={"depth": a.depth}, background=False)
    st = eng.state(rid)
    if st["status"] != "done":
        print("error:", st.get("error"), file=sys.stderr)
        sys.exit(1)
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return
    print(f"run {rid}  判定: {eng.llm.name}  候補 {len(st['candidates'])} 件  規模(暫定) {st['estimate']['days']} 人日")
    for it in st["intents"]:
        print(f"  {it['id']} {it['summary']} [{it['change_type']}{', ' + it['pattern'] if it.get('pattern') else ''}]")
    print("影響ありと判定（確度順・上位30）:")
    top = sorted((c for c in st["candidates"].values() if c["verdict"] == "impacted"),
                 key=lambda c: -(c["confidence"] or 0))[:30]
    for c in top:
        print(f"  {c['confidence']:.2f}  {c['kind']:<11} {c['name']:<50} {c['reason'] or ''}")
    print(f"ビュワーで確認: python -m impact.cli serve  →  http://localhost:8765/?run={rid}")


if __name__ == "__main__":
    main()
