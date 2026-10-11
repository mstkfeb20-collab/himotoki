"""Hand-off to development: the reviewed scope as a work order (API, MCP, CLI) and the
check of an actual change against the prediction."""
import importlib
import os
import subprocess

import pytest

from himotoki.analysis.handoff import build_handoff, handoff_markdown, verify_change
from himotoki.setup_cmds import index_workspace, prepare_demo
from himotoki.workspace import Workspace

NOTE = "受注の備考（NOTE）の上限を200文字から500文字に拡張する"


def _git(repo, *a):
    return subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def reviewed(tmp_path):
    ws_dir = prepare_demo(str(tmp_path / "sor"))
    for r in ("order-web", "inventory-svc", "shipping-svc"):
        p = os.path.join(ws_dir, "repos", r)
        _git(p, "init", "-q", "-b", "main")
        _git(p, "add", "-A")
        _git(p, "commit", "-qm", "init")
    ws = Workspace(ws_dir)
    index_workspace(ws, ws_dir)
    e = ws.engine()
    rid = e.start_run(NOTE, background=False)
    by = {c["name"]: c["node_id"] for c in e.state(rid)["candidates"].values()}
    for n in ("OrderForm.note", "orders.note", "orders.sql", "export_orders.sql", "ImportOrdersMain.main()",
              "OrderServiceBean.placeOrder()"):
        e.decide(rid, by[n], "confirmed", "NOTE桁拡張")
    e.decide(rid, by["OrderForm.getNote()"], "excluded", "getterは型変更なし")
    e.comment(rid, by["OrderForm.note"], "@Size(max=500) に変更", "masato")
    return ws, ws_dir, e, rid, by


def test_work_order_groups_reviewed_scope_by_file(reviewed):
    ws, ws_dir, e, rid, by = reviewed
    h = build_handoff(e, rid)
    files = {f["file"]: f["items"] for f in h["files"]}
    assert "order-web/src/main/java/com/example/order/web/OrderForm.java" in files
    assert "shipping-svc/src/main/java/com/example/shipping/ImportOrdersMain.java" in files
    note = next(i for i in files["order-web/src/main/java/com/example/order/web/OrderForm.java"] if i["name"] == "OrderForm.note")
    assert note["comments"][0]["text"] == "@Size(max=500) に変更" and note["reason"] == "NOTE桁拡張"
    po = next(i for f in h["files"] for i in f["items"] if i["name"] == "OrderServiceBean.placeOrder()")
    assert {r["code"] for r in po["tx_risks"]} >= {"REMOTE_IN_TX", "INDEPENDENT_TX"}
    col = next(i for i in h["other_targets"] if i["name"] == "orders.note")
    assert any(d["file"].endswith("orders.sql") for d in col["defined_in"])
    assert [x["name"] for x in h["excluded"]] == ["OrderForm.getNote()"]
    assert h["pending"] and any("未承認" in w for w in h["warnings"])
    md = handoff_markdown(h)
    for part in ("## 修正対象（ファイルごと）", "## 除外（変更しない）", "## 未判定", "⚠", "## 進め方", "@Size(max=500)"):
        assert part in md, part
    assert "## 未判定" not in handoff_markdown(build_handoff(e, rid, include_pending=False))


def test_verification_separates_hits_review_misses_and_graph_gaps(reviewed):
    ws, ws_dir, e, rid, by = reviewed
    repo = os.path.join(ws_dir, "repos", "order-web")
    form = os.path.join(repo, "src/main/java/com/example/order/web/OrderForm.java")
    src = open(form, encoding="utf-8").read()
    assert "200" in src
    open(form, "w", encoding="utf-8").write(src.replace("200", "500"))
    dao = os.path.join(repo, "src/main/java/com/example/order/dao/OrderDao.java")
    open(dao, "w", encoding="utf-8").write(open(dao, encoding="utf-8").read().replace("FOR UPDATE", "FOR UPDATE NOWAIT"))
    _git(repo, "commit", "-qam", "note 500")
    with pytest.raises(ValueError):
        ws.change_hunks("sor", "HEAD~1", "HEAD")                 # system graph: the repository must be named
    v = verify_change(e, rid, ws.change_hunks("sor", "HEAD~1", "HEAD", "order-web"), "HEAD~1", "HEAD", actual_days=2.5)
    assert [x["name"] for x in v["hit"]] == ["OrderForm.note"]
    assert v["missed"] and all(m["why"].startswith("candidate_") or m["why"] == "not_found" for m in v["missed"])
    assert {u["name"] for u in v["untouched"]} >= {"ImportOrdersMain.main()", "export_orders.sql"}
    assert 0 < v["recall"] < 1 and v["calibration"]["samples"] == 1
    assert e.state(rid)["verification"]["head"] == "HEAD"
    assert build_handoff(e, rid)["verification"]["recall"] == v["recall"]


def test_mcp_and_api_expose_the_hand_off(reviewed, monkeypatch):
    ws, ws_dir, e, rid, by = reviewed
    monkeypatch.setenv("HIMOTOKI_DATA_DIR", ws_dir)
    import himotoki.mcp_server as m
    importlib.reload(m)
    runs = m.list_runs()
    assert runs[0]["run_id"] == rid and runs[0]["confirmed"] == 6 and not runs[0]["approved"]
    h = m.get_handoff(rid)
    assert "## 修正対象" in h["markdown"] and h["files"]
    with pytest.raises(ValueError):
        m.get_handoff("nosuchrun")
    from fastapi.testclient import TestClient
    import himotoki.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    r = c.get(f"/api/runs/{rid}/handoff")
    assert r.status_code == 200 and r.json()["run_id"] == rid
    assert c.post(f"/api/runs/{rid}/verify", json={"base": "--evil", "head": "HEAD"}).status_code == 400
    assert c.post(f"/api/runs/{rid}/verify", json={"base": "HEAD", "head": "HEAD"}).status_code == 400   # repo required
    ok = c.post(f"/api/runs/{rid}/verify", json={"base": "HEAD", "head": "HEAD", "repo": "order-web"})
    assert ok.status_code == 200 and ok.json()["changed_elements"] == 0
