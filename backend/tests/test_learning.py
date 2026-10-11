"""Learning from reviewers' decisions and from verified changes (per system)."""
import importlib
import os
import subprocess

import pytest

from himotoki.analysis.handoff import verify_change
from himotoki.analysis.knowledge import MIN_NODE, MIN_PATTERN
from himotoki.setup_cmds import index_workspace, prepare_demo
from himotoki.workspace import Workspace

NOTE = "受注の備考（NOTE）の上限を200文字から500文字に拡張する"


def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def system(tmp_path):
    ws_dir = prepare_demo(str(tmp_path / "sor"))
    for r in ("order-web", "inventory-svc", "shipping-svc"):
        p = os.path.join(ws_dir, "repos", r)
        _git(p, "init", "-q", "-b", "main")
        _git(p, "add", "-A")
        _git(p, "commit", "-qm", "init")
    ws = Workspace(ws_dir)
    index_workspace(ws, ws_dir)
    return ws, ws_dir, ws.engine()


def _run(e, **params):
    rid = e.start_run(NOTE, params=params, background=False)
    return rid, e.state(rid)["candidates"]


def _by_name(cands, name):
    return next(c for c in cands.values() if c["name"] == name)


def test_repeated_exclusions_lower_an_element_and_show_why(system):
    ws, ws_dir, e = system
    base_rid, base = _run(e)
    getter = _by_name(base, "OrderForm.getNote()")
    for i in range(MIN_NODE):
        rid, c = _run(e)
        e.decide(rid, getter["node_id"], "excluded", "getterは桁に関係しない", actor=f"r{i}")
    # a re-investigation of an analysis is the same analysis: it does not count twice
    child = e.reinvestigate(rid, [], "テストも")
    for _ in range(200):
        if e.state(child)["status"] != "running":
            break
    if getter["node_id"] not in e.state(child)["candidates"]:
        e.add_candidate(child, getter["node_id"], "確認", expand=False)
    assert e.root_of(e.state(child)) == rid
    e.decide(child, getter["node_id"], "excluded", "getterは桁に関係しない")
    k = e.knowledge.summary(e.store)
    n = next(x for x in k["nodes"] if x["node_id"] == getter["node_id"])
    assert n["excluded"] == MIN_NODE and n["mult"] < 1
    _, after = _run(e)
    g2 = _by_name(after, "OrderForm.getNote()")
    assert g2["score"] < getter["score"]
    assert g2["learned"][0]["type"] == "excluded_before" and "getterは桁に関係しない" in g2["learned"][0]["message"]
    # switched off per run, or per item
    _, plain = _run(e, learning=False)
    assert abs(_by_name(plain, "OrderForm.getNote()")["score"] - getter["score"]) < 1e-6
    e.knowledge.ignore("node", getter["node_id"])
    _, ign = _run(e)
    assert "learned" not in _by_name(ign, "OrderForm.getNote()")


def test_path_patterns_move_only_after_enough_decisions(system):
    ws, ws_dir, e = system
    rid0, c0 = _run(e)
    targets = [c for c in c0.values() if c["kind"] == "Column" and c.get("via_rel") == "WRITES"][:MIN_PATTERN]
    assert len(targets) == MIN_PATTERN
    rid, _ = _run(e)
    e.bulk_decide(rid, [{"node_id": t["node_id"], "status": "excluded", "reason": "書き込み先の別列"}
                        for t in targets[:MIN_PATTERN - 1]])
    assert not any(p["active"] for p in e.knowledge.summary()["patterns"])
    e.decide(rid, targets[-1]["node_id"], "excluded", "書き込み先の別列")
    pats = [p for p in e.knowledge.summary()["patterns"] if p["active"]]
    assert pats and pats[0]["mult"] < 1 and pats[0]["kind"] == "Column"
    _, after = _run(e)
    lowered = [c for c in after.values() if any(n["type"] == "path" for n in c.get("learned", []))]
    assert lowered and all(after[c["node_id"]]["score"] <= c0[c["node_id"]]["score"] for c in lowered if c["node_id"] in c0)
    # undoing a decision removes what it taught
    e.decide(rid, targets[-1]["node_id"], "open")
    assert not any(p["active"] for p in e.knowledge.summary()["patterns"])


def test_verification_teaches_links_and_misses(system):
    ws, ws_dir, e = system
    rid, c = _run(e)
    form = _by_name(c, "OrderForm.note")
    e.decide(rid, form["node_id"], "confirmed", "桁拡張")
    lock_sql = next(x for x in c.values() if x["kind"] == "SqlStatement" and "SELECT STATUS" in x["name"])
    e.decide(rid, lock_sql["node_id"], "excluded", "ロックは無関係")
    repo = os.path.join(ws_dir, "repos", "order-web")
    f = os.path.join(repo, "src/main/java/com/example/order/web/OrderForm.java")
    open(f, "w", encoding="utf-8").write(open(f, encoding="utf-8").read().replace("200", "500"))
    dao = os.path.join(repo, "src/main/java/com/example/order/dao/OrderDao.java")
    open(dao, "w", encoding="utf-8").write(open(dao, encoding="utf-8").read().replace("FOR UPDATE", "FOR UPDATE NOWAIT"))
    audit = os.path.join(repo, "src/main/java/com/example/order/ejb/AuditLogBean.java")
    src = open(audit, encoding="utf-8").read()
    open(audit, "w", encoding="utf-8").write(src.replace("{", "{ // note length\n", 1))
    _git(repo, "commit", "-qam", "change")
    v = verify_change(e, rid, ws.change_hunks("sor", "HEAD~1", "HEAD", "order-web"), "HEAD~1", "HEAD")
    whys = {m["name"]: m["why"] for m in v["missed"]}
    assert any(w == "not_found" for w in whys.values()), whys
    k = e.knowledge.summary(e.store)
    assert k["verifications"] == 1 and k["links"]
    _, nxt = _run(e)
    learned = [x for x in nxt.values() if any(n["type"] == "learned_link" for n in x.get("learned", []))]
    assert learned and all(x["via_rel"] == "LEARNED" and x["verdict"] != "not_impacted" for x in learned)
    missed_before = [x for x in nxt.values() if any(n["type"] == "missed_before" for n in x.get("learned", []))]
    assert missed_before and all(x["verdict"] != "not_impacted" for x in missed_before)
    # learning survives re-indexing (it lives beside the graph, not in it)
    index_workspace(ws, ws_dir)
    assert ws.engine().knowledge.summary()["links"]


def test_api_and_mcp_show_and_switch_off_learning(system, monkeypatch):
    ws, ws_dir, e = system
    rid, c = _run(e)
    getter = _by_name(c, "OrderForm.getNote()")
    for _ in range(MIN_NODE):
        r, _ = _run(e)
        e.decide(r, getter["node_id"], "excluded", "無関係")
    monkeypatch.setenv("HIMOTOKI_DATA_DIR", ws_dir)
    import himotoki.mcp_server as m
    importlib.reload(m)
    assert m.learned_knowledge()["nodes"][0]["name"] == "OrderForm.getNote()"
    from fastapi.testclient import TestClient
    import himotoki.api as api
    importlib.reload(api)
    cl = TestClient(api.app)
    k = cl.get("/api/knowledge").json()
    assert k["analyses"] == MIN_NODE and k["nodes"]
    assert cl.post("/api/knowledge/ignore", json={"kind": "bogus", "key": "x"}).status_code == 422
    r = cl.post("/api/knowledge/ignore", json={"kind": "node", "key": getter["node_id"]}).json()
    assert r["nodes"][0]["ignored"]
    rr = cl.post("/api/runs", json={"requirement": NOTE, "learning": False})
    assert rr.status_code == 200
