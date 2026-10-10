"""HTTP + WebSocket API.

REST for commands and state, one WebSocket per run for the live event stream
(the socket first replays stored events, then streams new ones). Every call may
name a `graph` (repository/branch); the workspace default is used otherwise.
"""
from __future__ import annotations

import asyncio
import os
import threading

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .workspace import Workspace

DATA_DIR = os.environ.get("IMPACT_DATA_DIR", os.path.abspath("./data"))
ws_root = Workspace(DATA_DIR)

app = FastAPI(title="Impact Analyzer", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def eng(graph: str | None = None):
    e = ws_root.engine(graph)
    if e is None:
        raise HTTPException(400, "index a repository first")
    return e


def run_engine(run_id: str):
    e = ws_root.find_run(run_id)
    if e is None:
        raise HTTPException(404, run_id)
    return e


# ------------------------------------------------------------------ models
class IndexReq(BaseModel):
    repo: str
    docs_dir: str | None = None
    ref: str | None = None
    graph: str | None = None


class RunReq(BaseModel):
    requirement: str
    title: str | None = None
    depth: int = 3
    min_score: float = 0.2
    pace: float = 0.0
    graph: str | None = None


class DiffReq(BaseModel):
    base: str
    head: str
    graph: str | None = None
    title: str | None = None
    depth: int = 3


class CatalogReq(BaseModel):
    kind: str                    # postgres | oracle
    name: str = "db"
    dsn: str | None = None
    csv_dir: str | None = None
    user: str | None = None
    password: str | None = None
    owner: str | None = None


class RuntimeReq(BaseModel):
    kind: str                    # pg_stat_statements | csv
    name: str = "runtime"
    dsn: str | None = None
    path: str | None = None
    dialect: str | None = None


class DecisionReq(BaseModel):
    node_id: str
    status: str
    reason: str = ""
    actor: str = "user"
    change_type: str | None = None
    difficulty: str | None = None


class BulkReq(BaseModel):
    items: list[dict]
    actor: str = "user"


class AddReq(BaseModel):
    node_id: str | None = None
    query: str | None = None
    reason: str = ""
    actor: str = "user"


class CommentReq(BaseModel):
    node_id: str
    text: str = ""
    author: str = "user"
    assignee: str | None = None


class ReinvestigateReq(BaseModel):
    node_ids: list[str] = []
    instruction: str
    depth: int | None = None
    actor: str = "user"
    pace: float = 0.0


class AskReq(BaseModel):
    node_ids: list[str]
    question: str


class CalibrateReq(BaseModel):
    history: list[dict]


# ------------------------------------------------------------------ workspace / index
@app.get("/api/status")
def status(graph: str | None = None):
    name = graph or ws_root.default()
    e = ws_root.engine(name) if name else None
    st = ws_root.stats(name) if name else {}
    return {"graph": name, "graphs": ws_root.meta["graphs"], "repo": e.repo if e else None,
            "docs_dir": e.docs_dir if e else None, "llm": e.llm.name if e else None,
            "store": type(e.store).__name__ if e else None,
            "index": {"status": ws_root.index_state.get(name, {}).get("status", "done" if st else "idle"),
                      "stats": {k: st.get(k) for k in ("nodes", "edges", "files", "seconds", "commit")} if st else None},
            "estimator": {k: e.estimator.coef[k] for k in ("overhead", "calibration")} if e else None}


@app.get("/api/graphs")
def graphs():
    return {"default": ws_root.default(), "graphs": ws_root.meta["graphs"]}


@app.post("/api/index")
def index(req: IndexReq):
    if not os.path.isdir(req.repo):
        raise HTTPException(400, f"repository not found: {req.repo}")
    name = req.graph or ws_root.graph_name(req.repo, req.ref)
    if ws_root.index_state.get(name, {}).get("status") == "running":
        raise HTTPException(409, "indexing already running")
    state = ws_root.index_state.setdefault(name, {})
    state.update(status="running", events=[])

    def work():
        try:
            stats = ws_root.index(req.repo, req.docs_dir, req.ref, name,
                                  progress=lambda e, d: state["events"].append({"type": e, "data": d}))
            state.update(status="done", stats={k: stats.get(k) for k in ("nodes", "edges", "files", "seconds")})
        except Exception as e:
            state.update(status="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return {"status": "started", "graph": name}


@app.get("/api/index")
def index_status(graph: str | None = None):
    name = graph or ws_root.default()
    return ws_root.index_state.get(name, {"status": "done" if ws_root.stats(name) else "idle",
                                          "stats": ws_root.stats(name) or None})


@app.get("/api/index/report")
def index_report(graph: str | None = None):
    """What the graph knows it is missing: unresolved references per namespace,
    DDL vs database drift, runtime SQL coverage, extractor errors."""
    st = ws_root.stats(graph)
    if not st:
        raise HTTPException(404, "no index")
    keys = ("nodes", "edges", "by_kind", "files", "by_extractor", "parsed", "cached", "seconds", "seconds_extract",
            "seconds_link", "unresolved", "unresolved_samples", "external", "runtime", "drift", "errors",
            "error_samples", "rules", "commit")
    return {k: st.get(k) for k in keys}


@app.post("/api/sources/catalog")
def catalog(req: CatalogReq, graph: str | None = None):
    from .sources.catalog import import_oracle, import_postgres, save_source
    try:
        if req.kind == "postgres":
            f = import_postgres(req.dsn, req.name)
        elif req.kind == "oracle":
            f = import_oracle(req.name, req.csv_dir, req.dsn, req.user, req.password, req.owner)
        else:
            raise HTTPException(400, "kind must be postgres or oracle")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"catalog import failed: {e}")
    save_source(ws_root.root, f)
    return {"saved": f.path, "counts": f.payload.get("catalog_counts"), "note": "re-index to link it with the code"}


@app.post("/api/sources/runtime")
def runtime(req: RuntimeReq):
    from .sources.catalog import save_source
    from .sources.runtime import from_csv, from_pg_stat_statements
    try:
        f = from_pg_stat_statements(req.dsn, req.name) if req.kind == "pg_stat_statements" else \
            from_csv(req.path, req.name, dialect=req.dialect)
    except Exception as e:
        raise HTTPException(400, f"runtime import failed: {e}")
    save_source(ws_root.root, f)
    return {"saved": f.path, "statements": sum(1 for n in f.nodes if n["kind"] == "RuntimeSql"),
            "note": "re-index to link it with the code"}


@app.get("/api/search")
def search(q: str, k: int = 15, graph: str | None = None):
    return [{"id": n.id, "name": n.name, "kind": n.kind, "file": n.file, "score": round(s, 3)}
            for n, s in eng(graph).search.query(q, k=k, min_score=0.05)]


@app.get("/api/nodes/{node_id:path}")
def node(node_id: str, run_id: str | None = None, graph: str | None = None):
    e = run_engine(run_id) if run_id else eng(graph)
    d = e.node_detail(node_id, run_id)
    if not d:
        raise HTTPException(404, node_id)
    return d


# ------------------------------------------------------------------ runs
@app.get("/api/runs")
def runs(graph: str | None = None):
    out = []
    names = [graph] if graph else list(ws_root.meta["graphs"])
    for n in names:
        e = ws_root.engine(n)
        if e:
            out += [dict(r, graph=n) for r in e.runs.list()]
    return sorted(out, key=lambda r: -(r.get("created_at") or 0))


@app.post("/api/runs")
def create_run(req: RunReq):
    e = eng(req.graph)
    rid = e.start_run(req.requirement, req.title, {"depth": req.depth, "min_score": req.min_score, "pace": req.pace})
    return {"id": rid}


@app.post("/api/runs/diff")
def create_diff_run(req: DiffReq):
    """Change-set analysis: what do the changes between two refs reach?"""
    from .sources.git import GitRepo
    name = req.graph or ws_root.default()
    e = eng(name)
    g = ws_root.meta["graphs"].get(name, {})
    repo = GitRepo(g.get("repo") or e.repo, ws_root.root)
    try:
        hunks = repo.hunks(repo.resolve(req.base), repo.resolve(req.head))
    except Exception as ex:
        raise HTTPException(400, str(ex))
    nodes, added = e.diff_nodes(hunks)
    rid = e.start_run(f"変更差分 {req.base}..{req.head}", req.title or f"差分 {req.base}..{req.head}",
                      {"mode": "diff", "diff_nodes": nodes, "added_files": added, "depth": req.depth})
    return {"id": rid, "changed_elements": len(nodes), "added_files": added}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return run_engine(run_id).state(run_id)


@app.get("/api/runs/{run_id}/scope")
def scope(run_id: str):
    e = run_engine(run_id)
    st = e.state(run_id)
    return {"groups": e.regression_scope(st), "markdown": e.scope_markdown(st)}


@app.post("/api/runs/{run_id}/decisions")
def decide(run_id: str, req: DecisionReq):
    try:
        return run_engine(run_id).decide(run_id, req.node_id, req.status, req.reason, req.actor, req.change_type,
                                         req.difficulty)
    except KeyError:
        raise HTTPException(404, req.node_id)


@app.post("/api/runs/{run_id}/decisions/bulk")
def bulk(run_id: str, req: BulkReq):
    return run_engine(run_id).bulk_decide(run_id, req.items, req.actor)


@app.post("/api/runs/{run_id}/candidates")
def add(run_id: str, req: AddReq):
    e = run_engine(run_id)
    node_id = req.node_id
    if not node_id and req.query:
        hits = e.search.query(req.query, k=1, min_score=0.05)
        if not hits:
            raise HTTPException(404, f"no element matches: {req.query}")
        node_id = hits[0][0].id
    try:
        return e.add_candidate(run_id, node_id, req.reason, req.actor)
    except KeyError:
        raise HTTPException(404, str(node_id))


@app.post("/api/runs/{run_id}/comments")
def comment(run_id: str, req: CommentReq):
    return run_engine(run_id).comment(run_id, req.node_id, req.text, req.author, req.assignee)


@app.post("/api/runs/{run_id}/reinvestigate")
def reinvestigate(run_id: str, req: ReinvestigateReq):
    return {"id": run_engine(run_id).reinvestigate(run_id, req.node_ids, req.instruction, req.depth, req.actor, req.pace)}


@app.post("/api/runs/{run_id}/approve")
def approve(run_id: str, actor: str = "user"):
    try:
        return run_engine(run_id).approve(run_id, actor)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/runs/{a}/diff/{b}")
def diff(a: str, b: str):
    e = run_engine(a)
    if not e.state(b):
        raise HTTPException(404, b)
    return e.diff(a, b)


@app.post("/api/runs/{run_id}/ask")
def ask(run_id: str, req: AskReq):
    e = run_engine(run_id)
    return {"answer": e.ask(run_id, req.node_ids, req.question), "llm": e.llm.name}


@app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
def report(run_id: str):
    return run_engine(run_id).report(run_id)


@app.get("/api/runs/{run_id}/events")
def events(run_id: str, after: int = -1):
    return run_engine(run_id).runs.events(run_id, after)


@app.post("/api/estimator/calibrate")
def calibrate(req: CalibrateReq, graph: str | None = None):
    return eng(graph).estimator.calibrate(req.history)


# ------------------------------------------------------------------ source control webhooks
@app.post("/api/hooks/git")
async def git_hook(request: Request):
    """GitLab / GitHub webhook. Push: refresh that branch's graph. Merge/pull request:
    analyse the change set against the target branch and comment the impact."""
    import hashlib
    import hmac
    import json as _json
    from .sources.git import GitRepo, parse_webhook, post_review_comment
    raw = await request.body()
    secret = os.environ.get("IMPACT_WEBHOOK_SECRET")
    if secret:
        gl = request.headers.get("x-gitlab-token")
        gh = request.headers.get("x-hub-signature-256", "")
        ok = (gl is not None and hmac.compare_digest(gl, secret)) or \
             (gh and hmac.compare_digest(gh, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()))
        if not ok:
            raise HTTPException(401, "bad webhook signature")
    payload = _json.loads(raw or b"{}")
    ev = parse_webhook(dict(request.headers), payload)
    if not ev:
        return {"ignored": True}
    name = ws_root.default()
    g = ws_root.meta["graphs"].get(name or "", {})
    if not g:
        raise HTTPException(400, "no graph registered for this repository")
    repo = g["repo"]

    def work():
        gr = GitRepo(repo, ws_root.root)
        gr.fetch()
        if ev["kind"] == "push":
            ws_root.index(repo, g.get("docs_dir"), ev["branch"], ws_root.graph_name(repo, ev["branch"]))
            return
        target = ws_root.graph_name(repo, ev["base"])
        ws_root.index(repo, g.get("docs_dir"), ev["base"], target)
        e = ws_root.engine(target)
        base_sha = ev.get("base_sha") or gr.resolve(ev["base"])
        head_sha = ev.get("head_sha") or gr.resolve(ev["head"])
        nodes, added = e.diff_nodes(gr.hunks(base_sha, head_sha))
        rid = e.start_run(f"変更差分 {ev['base']}..{ev['head']}", f"#{ev['number']} {ev.get('title', '')}"[:60],
                          {"mode": "diff", "diff_nodes": nodes, "added_files": added}, background=False)
        body = e.scope_markdown(e.state(rid), f"#{ev['number']}")
        e.state(rid)["review_comment"] = post_review_comment(ev, body)

    threading.Thread(target=work, daemon=True).start()
    return {"accepted": ev}


# ------------------------------------------------------------------ live events
@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    e = ws_root.find_run(run_id)
    if e is None:
        await ws.close()
        return
    loop = asyncio.get_running_loop()
    q = e.bus.subscribe(run_id, loop)
    last = -1
    try:
        for ev in e.runs.events(run_id):
            await ws.send_json(ev)
            last = ev["seq"]
        while True:
            ev = await q.get()
            if ev["seq"] <= last:
                continue
            last = ev["seq"]
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    finally:
        e.bus.unsubscribe(run_id, q)


# ------------------------------------------------------------- frontend
_DIST = os.environ.get("IMPACT_FRONTEND", os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        return FileResponse(os.path.join(_DIST, "index.html"))
