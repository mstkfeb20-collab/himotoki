"""HTTP + WebSocket API.

REST for commands and state, one WebSocket per run for the live event stream
(the socket first replays stored events, then streams new ones). Every call may
name a `graph` (repository/branch); the workspace default is used otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .security import PathNotAllowed, check_input_path, check_ref, constant_time_equals, redact
from .workspace import Workspace

log = logging.getLogger("himotoki.api")
DATA_DIR = os.environ.get("HIMOTOKI_DATA_DIR", os.path.abspath("./data"))
ws_root = Workspace(DATA_DIR)

app = FastAPI(title="himotoki", version="2.0")

# --------------------------------------------------------------- security
# The API exposes source code, so by default it only answers same-origin requests
# for a local host name: no CORS, and the Host header must be a local name (this
# also stops DNS-rebinding pages from talking to a server on 127.0.0.1).
# Deployments set HIMOTOKI_ALLOWED_HOSTS and HIMOTOKI_API_TOKEN (see docs/SECURITY.md).
API_TOKEN = os.environ.get("HIMOTOKI_API_TOKEN") or None
_hosts = [h.strip() for h in os.environ.get(
    "HIMOTOKI_ALLOWED_HOSTS", "localhost,127.0.0.1,::1,[::1],testserver").split(",") if h.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_hosts)
_origins = [o.strip() for o in os.environ.get("HIMOTOKI_CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["GET", "POST"],
                       allow_headers=["content-type", "authorization"], allow_credentials=False)

_PUBLIC = ("/api/hooks/git",)          # authenticated by the webhook secret instead
COOKIE = "himotoki_token"


def _token_ok(headers, cookies, query: str = "") -> bool:
    if not API_TOKEN:
        return True
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and constant_time_equals(auth[7:].strip(), API_TOKEN):
        return True
    return constant_time_equals(cookies.get(COOKIE), API_TOKEN)


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if API_TOKEN and request.method == "GET" and "token" in request.query_params and not path.startswith("/api/"):
        # first visit with ?token=...: keep it in an HttpOnly, SameSite=Strict cookie
        # and drop it from the URL (and from the browser history)
        if not constant_time_equals(request.query_params["token"], API_TOKEN):
            return JSONResponse({"detail": "invalid token"}, status_code=401)
        rest = {k: v for k, v in request.query_params.items() if k != "token"}
        resp = RedirectResponse(path + (("?" + urlencode(rest)) if rest else ""), status_code=303)
        resp.set_cookie(COOKIE, API_TOKEN, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", max_age=12 * 3600)
        return resp
    if path.startswith("/api/") and not path.startswith(_PUBLIC) and \
            not _token_ok(request.headers, request.cookies):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if not path.startswith("/api/"):
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return resp


@app.exception_handler(PathNotAllowed)
async def _path_denied(request: Request, exc: PathNotAllowed):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception):
    # details go to the server log only; the client gets a reference id
    eid = uuid.uuid4().hex[:8]
    log.exception("unhandled error %s on %s %s", eid, request.method, request.url.path)
    return JSONResponse({"detail": "internal error", "error_id": eid}, status_code=500)


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
TEXT = 100_000      # requirement / instruction size limit (characters)
NAME = 200


class IndexReq(BaseModel):
    repo: str | None = Field(None, max_length=4096)
    # a system made of several repositories: {name: path}; indexed as one graph named `graph`
    repos: dict[str, str] | None = Field(None, max_length=50)
    docs_dir: str | None = Field(None, max_length=4096)
    ref: str | None = Field(None, max_length=NAME)
    graph: str | None = Field(None, max_length=NAME)


class RunReq(BaseModel):
    requirement: str = Field(min_length=1, max_length=TEXT)
    title: str | None = Field(None, max_length=NAME)
    depth: int = Field(3, ge=1, le=8)
    min_score: float = Field(0.2, ge=0.0, le=1.0)
    pace: float = Field(0.0, ge=0.0, le=2.0)
    graph: str | None = Field(None, max_length=NAME)
    # AI-judgement budget for this run (omit to use settings.yaml / HIMOTOKI_LLM_*)
    llm_min_score: float | None = Field(None, ge=0.0, le=1.0)
    llm_max_items: int | str | None = None          # number, or "unlimited"
    learning: bool = True                            # use what earlier reviews / verifications taught


class KnowledgeIgnoreReq(BaseModel):
    graph: str | None = Field(None, max_length=NAME)
    kind: str = Field(pattern="^(pattern|edge|node|link)$")
    key: str = Field(min_length=1, max_length=2000)
    ignore: bool = True


class VerifyReq(BaseModel):
    base: str = Field(max_length=NAME)
    head: str = Field(max_length=NAME)
    repo: str | None = Field(None, max_length=NAME)        # repository of a system graph
    actual_days: float | None = Field(None, ge=0, le=100_000)


class DiffReq(BaseModel):
    base: str = Field(max_length=NAME)
    head: str = Field(max_length=NAME)
    graph: str | None = Field(None, max_length=NAME)
    repo: str | None = Field(None, max_length=NAME)        # which repository of a system graph changed
    title: str | None = Field(None, max_length=NAME)
    depth: int = Field(3, ge=1, le=8)


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
    reason: str = Field("", max_length=TEXT)
    actor: str = "user"
    change_type: str | None = None
    difficulty: str | None = None


class BulkReq(BaseModel):
    items: list[dict] = Field(max_length=50_000)
    actor: str = "user"


class AddReq(BaseModel):
    node_id: str | None = None
    query: str | None = None
    reason: str = ""
    actor: str = "user"


class CommentReq(BaseModel):
    node_id: str
    text: str = Field("", max_length=TEXT)
    author: str = "user"
    assignee: str | None = None


class ReinvestigateReq(BaseModel):
    node_ids: list[str] = Field([], max_length=5000)
    instruction: str = Field(max_length=TEXT)
    depth: int | None = Field(None, ge=1, le=8)
    actor: str = "user"
    pace: float = 0.0


class AskReq(BaseModel):
    node_ids: list[str] = Field(max_length=200)
    question: str = Field(max_length=TEXT)


class CalibrateReq(BaseModel):
    history: list[dict]


# ------------------------------------------------------------------ workspace / index
@app.get("/api/status")
def status(graph: str | None = None):
    name = graph or ws_root.default()
    e = ws_root.engine(name) if name else None
    st = ws_root.stats(name) if name else {}
    return {"graph": name, "graphs": ws_root.meta["graphs"], "repo": e.repo if e else None,
            "repos": e.repos if e else None, "workspace": ws_root.root,
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
    if req.repos:
        return _index_system(req)
    if not req.repo:
        raise HTTPException(400, "repo or repos is required")
    check_input_path(req.repo, "repository")
    check_input_path(req.docs_dir, "documents folder")
    try:
        check_ref(req.ref)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not os.path.isdir(req.repo):
        raise HTTPException(400, "repository not found")
    if req.docs_dir and not os.path.isdir(req.docs_dir):
        raise HTTPException(400, "documents folder not found")
    name = req.graph or ws_root.graph_name(req.repo, req.ref)
    try:
        ws_root.graph_dir(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
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
            log.exception("indexing %s failed", name)
            state.update(status="error", error=redact(str(e))[:500])

    threading.Thread(target=work, daemon=True).start()
    return {"status": "started", "graph": name}


def _index_system(req: IndexReq):
    from .pipeline.run import check_repo_names
    if req.ref:
        raise HTTPException(400, "ref is not supported for a system of several repositories")
    if not req.graph:
        raise HTTPException(400, "graph (the system name) is required with repos")
    try:
        check_repo_names(req.repos)
        ws_root.graph_dir(req.graph)
    except ValueError as e:
        raise HTTPException(400, str(e))
    for n, p in req.repos.items():
        check_input_path(p, "repository")
        if not os.path.isdir(p):
            raise HTTPException(400, f"repository not found: {n}")
    check_input_path(req.docs_dir, "documents folder")
    if req.docs_dir and not os.path.isdir(req.docs_dir):
        raise HTTPException(400, "documents folder not found")
    name = req.graph
    if ws_root.index_state.get(name, {}).get("status") == "running":
        raise HTTPException(409, "indexing already running")
    state = ws_root.index_state.setdefault(name, {})
    state.update(status="running", events=[])

    def work():
        try:
            stats = ws_root.index_system(name, req.repos, req.docs_dir,
                                         progress=lambda e, d: state["events"].append({"type": e, "data": d}))
            state.update(status="done", stats={k: stats.get(k) for k in ("nodes", "edges", "files", "seconds")})
        except Exception as e:
            log.exception("indexing %s failed", name)
            state.update(status="error", error=redact(str(e))[:500])

    threading.Thread(target=work, daemon=True).start()
    return {"status": "started", "graph": name, "repos": list(req.repos)}


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
            "seconds_link", "unresolved", "unresolved_samples", "external", "runtime", "drift", "transactions", "errors",
            "error_samples", "rules", "commit")
    return {k: st.get(k) for k in keys}


def _source_req(name: str, dsn: str | None, path: str | None):
    import re as _re
    if not _re.fullmatch(r"[\w.\-]{1,80}", name or "") or name in (".", ".."):
        raise HTTPException(400, "name: letters, digits, . _ - only")
    if dsn and os.environ.get("HIMOTOKI_ALLOW_DB_FROM_API") != "1":
        # connecting to arbitrary hosts from the server is opt-in (use the CLI, or CSV exports)
        raise HTTPException(403, "database connections from the API are disabled (set HIMOTOKI_ALLOW_DB_FROM_API=1)")
    check_input_path(path, "export folder")


@app.post("/api/sources/catalog")
def catalog(req: CatalogReq, graph: str | None = None):
    from .sources.catalog import import_oracle, import_postgres, save_source
    _source_req(req.name, req.dsn, req.csv_dir)
    try:
        if req.kind == "postgres":
            f = import_postgres(req.dsn, req.name)
        elif req.kind == "oracle":
            f = import_oracle(req.name, req.csv_dir, req.dsn, req.user, req.password, req.owner)
        else:
            raise HTTPException(400, "kind must be postgres or oracle")
    except HTTPException:
        raise
    except PathNotAllowed:
        raise
    except Exception as e:
        log.exception("catalog import failed")
        raise HTTPException(400, f"catalog import failed: {redact(str(e))[:300]}")
    save_source(ws_root.root, f)
    return {"saved": f.path, "counts": f.payload.get("catalog_counts"), "note": "re-index to link it with the code"}


@app.post("/api/sources/runtime")
def runtime(req: RuntimeReq):
    from .sources.catalog import save_source
    from .sources.runtime import from_csv, from_pg_stat_statements
    _source_req(req.name, req.dsn if req.kind == "pg_stat_statements" else None, req.path)
    try:
        f = from_pg_stat_statements(req.dsn, req.name) if req.kind == "pg_stat_statements" else \
            from_csv(req.path, req.name, dialect=req.dialect)
    except Exception as e:
        log.exception("runtime import failed")
        raise HTTPException(400, f"runtime import failed: {redact(str(e))[:300]}")
    save_source(ws_root.root, f)
    return {"saved": f.path, "statements": sum(1 for n in f.nodes if n["kind"] == "RuntimeSql"),
            "note": "re-index to link it with the code"}


@app.get("/api/docs")
def docs_list(graph: str | None = None):
    """Design documents in the graph, with how many rows / sections were read and linked."""
    from .doccheck import document_list
    return document_list(eng(graph).store)


@app.get("/api/docs/check")
def docs_check(path: str, graph: str | None = None):
    """Read one indexed design document again and show what was read and what was not."""
    from .doccheck import check_graph_document
    rep = check_graph_document(eng(graph), path)
    if rep is None:
        raise HTTPException(404, "not an indexed design document")
    return rep


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


@app.get("/api/context/{node_id:path}")
def node_context(node_id: str, run_id: str | None = None, graph: str | None = None):
    """Everything around one element a reviewer needs: grouped relations, the table and
    its readers / writers for data, the job net and run chain for jobs."""
    from .analysis.context import node_context as ctx
    e = run_engine(run_id) if run_id else eng(graph)
    if e.store.get(node_id) is None:
        raise HTTPException(404, node_id)
    return ctx(e.store, node_id)


@app.get("/api/file")
def file_view(path: str, run_id: str | None = None, graph: str | None = None):
    """A whole source file, for files that are part of the graph only (no arbitrary reads)."""
    e = run_engine(run_id) if run_id else eng(graph)
    d = e.file_view(path)
    if d is None:
        raise HTTPException(404, "not an indexed file")
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
    params = {"depth": req.depth, "min_score": req.min_score, "pace": req.pace, "learning": req.learning}
    if req.llm_min_score is not None:
        params["llm_min_score"] = req.llm_min_score
    if req.llm_max_items is not None:
        params["llm_max_items"] = req.llm_max_items
    try:
        rid = e.start_run(req.requirement, req.title, params)
    except ValueError as ex:
        raise HTTPException(400, f"invalid AI budget: {ex}")
    return {"id": rid}


class PreviewReq(BaseModel):
    requirement: str = Field("", max_length=TEXT)
    graph: str | None = Field(None, max_length=NAME)


@app.post("/api/requirements/preview")
def preview_requirement(req: PreviewReq):
    """Check a requirement while it is being written: how it is read and what it would
    start from. Rule-based only — no AI tokens are used."""
    return eng(req.graph).preview_requirement(req.requirement)


@app.get("/api/requirements/example")
def example_requirement(graph: str | None = None):
    """A practice requirement built from this repository (for the tutorial)."""
    return eng(graph).example_requirement() or {"requirement": None}


@app.get("/api/usage")
def usage():
    """AI usage of the whole workspace: today, this month, the last 31 days by day,
    priced with the current settings (USD) and converted to yen."""
    import datetime as _dt
    from .settings import load_pricing
    from .usage import UsageLedger
    led = UsageLedger(os.path.join(ws_root.root, "usage.db"))
    pricing = load_pricing(ws_root.root)
    now = _dt.datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1)
    jpy = lambda d: dict(d, cost_jpy=round(d["cost_usd"] * pricing.jpy_per_usd, 2))
    return {"today": jpy(led.summary(day0.timestamp())), "month": jpy(led.summary(month0.timestamp())),
            "by_day": [jpy(d) for d in led.by_day((day0 - _dt.timedelta(days=30)).timestamp())],
            "jpy_per_usd": pricing.jpy_per_usd, "pricing_source": pricing.source}


@app.get("/api/settings/llm")
def llm_settings():
    """The AI-judgement budget new runs get (settings file + environment)."""
    from .settings import load_llm_budget, load_pricing, settings_path
    return {"effective": load_llm_budget(ws_root.root).to_dict(), "settings_file": settings_path(ws_root.root),
            "pricing": load_pricing(ws_root.root).to_dict(),
            "llm": (ws_root.engine(ws_root.default()).llm.name if ws_root.default() and
                    ws_root.engine(ws_root.default()) else None)}


@app.post("/api/runs/diff")
def create_diff_run(req: DiffReq):
    """Change-set analysis: what do the changes between two refs reach?"""
    from .sources.git import GitRepo
    name = req.graph or ws_root.default()
    e = eng(name)
    try:
        hunks = ws_root.change_hunks(name, check_ref(req.base, "base"), check_ref(req.head, "head"), req.repo)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    except Exception:
        log.exception("diff failed")
        raise HTTPException(400, "could not compute the diff between the given refs")
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


@app.get("/api/knowledge")
def knowledge(graph: str | None = None):
    """What this system's analyses have learned from people's decisions and verifications."""
    e = eng(graph)
    return e.knowledge.summary(e.store)


@app.post("/api/knowledge/ignore")
def knowledge_ignore(req: KnowledgeIgnoreReq):
    """Switch one learned item off (or back on)."""
    e = eng(req.graph)
    e.knowledge.ignore(req.kind, req.key, "user", req.ignore)
    return e.knowledge.summary(e.store)


@app.get("/api/runs/{run_id}/issues")
def issues(run_id: str, split: str = "one"):
    """Issue drafts for the reviewed scope (copied by hand or created by the local Claude Code)."""
    from .analysis.issues import issue_drafts
    return issue_drafts(run_engine(run_id), run_id, "subsystem" if split == "subsystem" else "one")


@app.get("/api/runs/{run_id}/handoff")
def handoff(run_id: str, pending: bool = True):
    """Work order for development (JSON + Markdown): the reviewed scope per file."""
    from .analysis.handoff import build_handoff, handoff_markdown
    h = build_handoff(run_engine(run_id), run_id, pending)
    return dict(h, markdown=handoff_markdown(h))


@app.post("/api/runs/{run_id}/verify")
def verify(run_id: str, req: VerifyReq):
    """Compare the actual change (base..head) with the analysis."""
    from .analysis.handoff import verification_markdown, verify_change
    e = run_engine(run_id)
    name = next((n for n in ws_root.meta["graphs"] if ws_root.engine(n) is e), None)
    try:
        hunks = ws_root.change_hunks(name, check_ref(req.base, "base"), check_ref(req.head, "head"), req.repo)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    except Exception:
        log.exception("verify diff failed")
        raise HTTPException(400, "could not compute the diff between the given refs")
    v = verify_change(e, run_id, hunks, req.base, req.head, req.actual_days)
    return dict(v, markdown=verification_markdown(v))


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
    secret = os.environ.get("HIMOTOKI_WEBHOOK_SECRET")
    if not secret:
        # an unauthenticated hook would let anyone trigger fetches and indexing
        raise HTTPException(503, "webhook disabled: set HIMOTOKI_WEBHOOK_SECRET")
    raw = await request.body()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(413, "payload too large")
    gl = request.headers.get("x-gitlab-token")
    gh = request.headers.get("x-hub-signature-256", "")
    ok = (gl is not None and constant_time_equals(gl, secret)) or \
         (bool(gh) and constant_time_equals(gh, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()))
    if not ok:
        raise HTTPException(401, "bad webhook signature")
    try:
        payload = _json.loads(raw or b"{}")
        ev = parse_webhook(dict(request.headers), payload) if isinstance(payload, dict) else None
        if ev:
            for k in ("branch", "base", "head"):
                check_ref(ev.get(k), k)
            for k in ("sha", "base_sha", "head_sha"):
                if ev.get(k) and not __import__("re").fullmatch(r"[0-9a-f]{7,64}", str(ev[k])):
                    ev[k] = None
    except ValueError as e:
        raise HTTPException(400, f"invalid webhook payload: {e}")
    if not ev:
        return {"ignored": True}
    name = ws_root.default()
    g = ws_root.meta["graphs"].get(name or "", {})
    if not g:
        raise HTTPException(400, "no graph registered for this repository")
    repo = g.get("repo")
    if not repo:
        raise HTTPException(400, "webhooks need a single-repository graph as the default")

    entry = {"at": time.time(), "event": {k: ev.get(k) for k in ("kind", "branch", "base", "head", "number")},
             "status": "running"}
    HOOK_LOG.append(entry)
    del HOOK_LOG[:-50]

    def work():
        try:
            _work()
            entry["status"] = "done"
        except Exception as ex:
            log.exception("webhook processing failed")
            entry.update(status="error", error=redact(str(ex))[:300])

    def _work():
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
        e.state(rid)["review_comment"] = entry["comment"] = post_review_comment(ev, body)
        entry["run_id"] = rid

    threading.Thread(target=work, daemon=True).start()
    return {"accepted": ev}


HOOK_LOG: list[dict] = []


@app.get("/api/hooks/log")
def hook_log():
    """Outcome of the last webhook deliveries (processing runs in the background)."""
    return list(reversed(HOOK_LOG))


# ------------------------------------------------------------------ live events
@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    if not _token_ok(ws.headers, ws.cookies):
        await ws.close(code=4401)
        return
    origin = ws.headers.get("origin")
    if origin and _origins == [] and origin.split("://", 1)[-1] != ws.headers.get("host"):
        await ws.close(code=4403)     # cross-site WebSocket (browsers do not apply CORS to it)
        return
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
def viewer_dir() -> str | None:
    """Built viewer: HIMOTOKI_FRONTEND, a fresh `npm run build` in a checkout, or the
    copy shipped inside the package (so no Node.js is needed to use the tool)."""
    here = os.path.dirname(__file__)
    for d in (os.environ.get("HIMOTOKI_FRONTEND"), os.path.join(here, "..", "..", "frontend", "dist"),
              os.path.join(here, "web")):
        if d and os.path.isfile(os.path.join(d, "index.html")):
            return d
    return None


_DIST = viewer_dir()
if _DIST:
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        return FileResponse(os.path.join(_DIST, "index.html"))
