"""Core tests on a small fixture repository (no network, no LLM, no Neo4j)."""
import os
import time

import pytest
from fastapi.testclient import TestClient

from codegraph.analysis.engine import Engine
from codegraph.model import Rel
from codegraph.pipeline.run import Pipeline
from codegraph.store import LocalStore

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "minirepo")
P = "java:com.example.shop."
TPL = "file:src/main/resources/templates/customers/customerForm.html"


@pytest.fixture()
def indexed(tmp_path):
    store = LocalStore(str(tmp_path / "graph.json"))
    stats = Pipeline(str(tmp_path)).index(FIX, store)
    return store, stats, tmp_path


def rels(store, src, rel):
    return {e.dst for e in store.out_edges(src, [rel])}


def test_index_links_layers(indexed):
    store, stats, _ = indexed
    assert stats["nodes"] > 20
    zip_field = P + "Customer.zip"
    assert "col:customers.zip" in rels(store, zip_field, Rel.MAPS_TO)
    assert P + "Customer.name" in {e.src for e in store.in_edges("col:customers.full_name", [Rel.MAPS_TO])}
    assert zip_field in rels(store, TPL, Rel.BINDS)
    assert "msg:zip.invalid" in rels(store, zip_field, Rel.USES_MESSAGE)
    # Spring Data derived query references the field
    assert zip_field in rels(store, P + "CustomerRepository#findByZipStartingWith(String)", Rel.REFERENCES)
    # constant view name resolves to the template
    assert TPL in rels(store, P + "CustomerController#initForm()", Rel.RENDERS)
    # endpoint -> handler, and a test exercising the endpoint by URL
    assert P + "CustomerController#initForm()" in rels(store, "ep:GET /customers/new", Rel.HANDLES)
    assert "ep:GET /customers/new" in rels(store, P + "CustomerControllerTests#initForm()", Rel.TESTS)
    # positional insert is recorded
    pop = [e for e in store.in_edges("table:customers", [Rel.POPULATES])]
    assert pop and pop[0].props["positional"] is True


def test_run_finds_impact_and_gates_approval(indexed):
    store, _, tmp = indexed
    eng = Engine(store, str(tmp), FIX)
    rid = eng.start_run("郵便番号の入力チェックを7桁から8桁に変更する。", background=False)
    st = eng.state(rid)
    assert st["status"] == "done", st.get("error")
    cands = st["candidates"]
    for must in (P + "Customer.zip", "msg:zip.invalid", TPL):
        assert must in cands, must
    assert cands[P + "Customer.zip"]["verdict"] == "impacted"
    with pytest.raises(ValueError):
        eng.approve(rid)
    items = [{"node_id": n, "status": "confirmed" if n in (P + "Customer.zip", "msg:zip.invalid") else "excluded",
              "reason": "test"} for n in cands]
    eng.bulk_decide(rid, items)
    st = eng.state(rid)
    assert all(c["ok"] for c in st["checks"]), st["checks"]
    assert eng.approve(rid)["estimate_days"] > 0
    # changing a decision re-opens the gate
    eng.decide(rid, P + "Customer.zip", "open")
    assert eng.state(rid)["approved"] is None


def test_add_attribute_pattern_and_reinvestigation(indexed):
    store, _, tmp = indexed
    eng = Engine(store, str(tmp), FIX)
    rid = eng.start_run("顧客にメールアドレスを登録できるようにする。", background=False)
    st = eng.state(rid)
    assert st["intents"][0]["pattern"] == "add-attribute"
    for must in (P + "Customer", "table:customers", "file:src/main/resources/db/schema.sql",
                 "file:src/main/resources/db/data.sql", TPL):
        assert must in st["candidates"], must
    eng.decide(rid, P + "Customer", "confirmed", "add email")
    child = eng.reinvestigate(rid, [P + "CustomerController#save(Customer)"], "テストも確認して")
    for _ in range(100):
        if eng.state(child)["status"] != "running":
            break
        time.sleep(0.05)
    cst = eng.state(child)
    assert cst["status"] == "done"
    assert cst["candidates"][P + "Customer"]["decision"]["status"] == "confirmed"
    assert cst["intents"][-1]["instruction"] is True
    d = eng.diff(rid, child)
    assert set(d) >= {"added", "removed", "changed"}


def test_api_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEGRAPH_DATA_DIR", str(tmp_path))
    import importlib
    import codegraph.api as api
    importlib.reload(api)
    client = TestClient(api.app)
    assert client.post("/api/index", json={"repo": FIX}).status_code == 200
    for _ in range(100):
        if client.get("/api/index").json()["status"] == "done":
            break
        time.sleep(0.05)
    rid = client.post("/api/runs", json={"requirement": "郵便番号を7桁から8桁にする"}).json()["id"]
    for _ in range(200):
        if client.get(f"/api/runs/{rid}").json()["status"] != "running":
            break
        time.sleep(0.05)
    st = client.get(f"/api/runs/{rid}").json()
    assert st["status"] == "done"
    nid = P + "Customer.zip"
    assert client.post(f"/api/runs/{rid}/decisions", json={"node_id": nid, "status": "confirmed"}).status_code == 200
    detail = client.get(f"/api/nodes/{nid}", params={"run_id": rid}).json()
    assert detail["code"] and detail["node"]["kind"] == "Field"
    with client.websocket_connect(f"/ws/runs/{rid}") as ws:
        first = ws.receive_json()
        assert first["type"] == "run.started"
    assert "影響分析レポート" in client.get(f"/api/runs/{rid}/report").text
