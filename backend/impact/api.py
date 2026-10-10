"""HTTP + WebSocket API.

REST for commands and state, one WebSocket per run for the live event stream
(the socket first replays stored events, then streams new ones)."""
from __future__ import annotations

import asyncio
import json
import os
import threading

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analysis.engine import Engine
from .indexer import index_repository
from .store import open_store

DATA_DIR = os.environ.get("IMPACT_DATA_DIR", os.path.abspath("./data"))
os.makedirs(DATA_DIR, exist_ok=True)
META = os.path.join(DATA_DIR, "meta.json")


def _meta() -> dict:
    return json.load(open(META, encoding="utf-8")) if os.path.exists(META) else {}


meta = _meta()
store = open_store(DATA_DIR)
engine = Engine(store, DATA_DIR, meta.get("repo") or os.environ.get("IMPACT_REPO"),
                meta.get("docs_dir") or os.environ.get("IMPACT_DOCS"))
index_state = {"status": "idle", "events": [], "stats": meta.get("stats")}

app = FastAPI(title="Impact Analyzer", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ models
class IndexReq(BaseModel):
    repo: str
    docs_dir: str | None = None


class RunReq(BaseModel):
    requirement: str
    title: str | None = None
    depth: int = 3
    min_score: float = 0.2
    pace: float = 0.0           # seconds between events, for live demos of fast offline runs


class DecisionReq(BaseModel):
    node_id: str
    status: str                 # confirmed | excluded | open
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


# ------------------------------------------------------------------ index
@app.get("/api/status")
def status():
    return {"repo": engine.repo, "docs_dir": engine.docs_dir, "llm": engine.llm.name,
            "store": type(store).__name__, "index": {k: index_state[k] for k in ("status", "stats")},
            "estimator": {k: engine.estimator.coef[k] for k in ("overhead", "calibration")}}


@app.post("/api/index")
def index(req: IndexReq):
    if not os.path.isdir(req.repo):
        raise HTTPException(400, f"repository not found: {req.repo}")
    if index_state["status"] == "running":
        raise HTTPException(409, "indexing already running")

    def work():
        index_state.update(status="running", events=[])
        try:
            stats = index_repository(req.repo, store, req.docs_dir,
                                     progress=lambda e, d: index_state["events"].append({"type": e, "data": d}))
            engine.repo, engine.docs_dir = req.repo, req.docs_dir
            engine._by_file = None
            engine.reload()
            json.dump({"repo": req.repo, "docs_dir": req.docs_dir, "stats": stats},
                      open(META, "w", encoding="utf-8"), ensure_ascii=False)
            index_state.update(status="done", stats=stats)
        except Exception as e:
            index_state.update(status="error", error=str(e))

    threading.Thread(target=work, daemon=True).start()
    return {"status": "started"}


@app.get("/api/index")
def index_status():
    return index_state


@app.get("/api/search")
def search(q: str, k: int = 15):
    return [{"id": n.id, "name": n.name, "kind": n.kind, "file": n.file, "score": round(s, 3)}
            for n, s in engine.search.query(q, k=k, min_score=0.05)]


@app.get("/api/nodes/{node_id:path}")
def node(node_id: str, run_id: str | None = None):
    d = engine.node_detail(node_id, run_id)
    if not d:
        raise HTTPException(404, node_id)
    return d


# ------------------------------------------------------------------ runs
def _run_or_404(run_id):
    st = engine.state(run_id)
    if not st:
        raise HTTPException(404, run_id)
    return st


@app.get("/api/runs")
def runs():
    return engine.runs.list()


@app.post("/api/runs")
def create_run(req: RunReq):
    if not engine.repo:
        raise HTTPException(400, "index a repository first")
    rid = engine.start_run(req.requirement, req.title,
                           {"depth": req.depth, "min_score": req.min_score, "pace": req.pace})
    return {"id": rid}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return _run_or_404(run_id)


@app.post("/api/runs/{run_id}/decisions")
def decide(run_id: str, req: DecisionReq):
    _run_or_404(run_id)
    try:
        return engine.decide(run_id, req.node_id, req.status, req.reason, req.actor, req.change_type, req.difficulty)
    except KeyError:
        raise HTTPException(404, req.node_id)


@app.post("/api/runs/{run_id}/decisions/bulk")
def bulk(run_id: str, req: BulkReq):
    _run_or_404(run_id)
    return engine.bulk_decide(run_id, req.items, req.actor)


@app.post("/api/runs/{run_id}/candidates")
def add(run_id: str, req: AddReq):
    _run_or_404(run_id)
    node_id = req.node_id
    if not node_id and req.query:
        hits = engine.search.query(req.query, k=1, min_score=0.05)
        if not hits:
            raise HTTPException(404, f"no element matches: {req.query}")
        node_id = hits[0][0].id
    try:
        return engine.add_candidate(run_id, node_id, req.reason, req.actor)
    except KeyError:
        raise HTTPException(404, str(node_id))


@app.post("/api/runs/{run_id}/comments")
def comment(run_id: str, req: CommentReq):
    _run_or_404(run_id)
    return engine.comment(run_id, req.node_id, req.text, req.author, req.assignee)


@app.post("/api/runs/{run_id}/reinvestigate")
def reinvestigate(run_id: str, req: ReinvestigateReq):
    _run_or_404(run_id)
    return {"id": engine.reinvestigate(run_id, req.node_ids, req.instruction, req.depth, req.actor, req.pace)}


@app.post("/api/runs/{run_id}/approve")
def approve(run_id: str, actor: str = "user"):
    _run_or_404(run_id)
    try:
        return engine.approve(run_id, actor)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/runs/{a}/diff/{b}")
def diff(a: str, b: str):
    _run_or_404(a); _run_or_404(b)
    return engine.diff(a, b)


@app.post("/api/runs/{run_id}/ask")
def ask(run_id: str, req: AskReq):
    _run_or_404(run_id)
    return {"answer": engine.ask(run_id, req.node_ids, req.question), "llm": engine.llm.name}


@app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
def report(run_id: str):
    _run_or_404(run_id)
    return engine.report(run_id)


@app.get("/api/runs/{run_id}/events")
def events(run_id: str, after: int = -1):
    return engine.runs.events(run_id, after)


@app.post("/api/estimator/calibrate")
def calibrate(req: CalibrateReq):
    return engine.estimator.calibrate(req.history)


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws.accept()
    loop = asyncio.get_running_loop()
    q = engine.bus.subscribe(run_id, loop)
    last = -1
    try:
        for ev in engine.runs.events(run_id):
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
        engine.bus.unsubscribe(run_id, q)


# ------------------------------------------------------------- frontend
_DIST = os.environ.get("IMPACT_FRONTEND", os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"))
if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        return FileResponse(os.path.join(_DIST, "index.html"))
