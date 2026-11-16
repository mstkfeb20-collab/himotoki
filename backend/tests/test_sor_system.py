"""A system of three servers (samples/sor-env): EJB / WebLogic transactions, SOAP and
REST between servers, JDBC SQL, stored procedures, a database link, JMS, and a CSV
handed over by SQL*Plus, sftp, a JP1 file-watch job and an importer — indexed as one
graph and analysed across the repositories."""
import importlib
import os
import time

import pytest

from himotoki.extractors import text
from himotoki.rules import load_rules
from himotoki.workspace import Workspace

SOR = os.path.join(os.path.dirname(__file__), "..", "himotoki", "demo", "sor-env")
REPOS = {n: os.path.join(SOR, n) for n in ("order-web", "inventory-svc", "shipping-svc")}
NOTE = "受注の備考（NOTE）の上限を200文字から500文字に拡張する"


@pytest.fixture(scope="module")
def system(tmp_path_factory):
    ws = Workspace(str(tmp_path_factory.mktemp("ws")))
    stats = ws.index_system("sor", REPOS)
    e = ws.engine("sor")
    nodes = {n.id: n for n in e.store.nodes()}
    edges = [x for n in nodes for x in e.store.out_edges(n)]
    return ws, e, stats, nodes, edges


def _edge(edges, src_part, dst_part, rel=None):
    return [x for x in edges if src_part in x.src and dst_part in x.dst and (rel is None or x.rel == rel)]


def test_system_graph_keeps_repositories_apart(system):
    ws, e, stats, nodes, _ = system
    assert stats["repos"].keys() == REPOS.keys() and all(stats["files_by_repo"].values())
    assert not stats["unresolved"]
    files = {n.file.split("/", 1)[0] for n in nodes.values() if n.file}
    assert files == set(REPOS)
    # source shown in the viewer is read from the right repository
    d = e.node_detail("java:com.example.inventory.InventoryEndpoint")
    assert any("@WebService" in l["text"] for l in d["code"])
    assert e._source_path("../etc/passwd") is None and e._source_path("nosuch/x.java") is None
    with pytest.raises(ValueError):
        ws.change_hunks("sor", "HEAD~1", "HEAD")      # which repository changed must be named


def test_calls_between_servers(system):
    _, _, _, nodes, edges = system
    # SOAP: service endpoint interface on the client -> @WebService implementation on the server
    assert _edge(edges, "InventoryPortType#reserveStock", "InventoryEndpoint#reserveStock", "CALLS")
    assert _edge(edges, "wsdl:http://example.com/inventory#reserveStock", "InventoryEndpoint#reserveStock")
    # REST: client path literal -> JAX-RS endpoint of the other server
    assert _edge(edges, "ShippingClient#requestShipment", "ep:POST /shipments")
    # JMS: @Resource queue on the sender -> message-driven bean on the receiver
    assert _edge(edges, "ShipResultSender.shipResultQueue", "ShipResultListener")
    # JNDI data source of the WebLogic JDBC module
    ds = [x for x in _edge(edges, "OrderDao.ds", "ds:")]
    assert ds
    assert nodes[ds[0].dst].props["global_tx"] == "TwoPhaseCommit"
    # JDBC SQL in Java strings, stored procedure calls, database link
    assert _edge(edges, "sql:com.example.order.dao.OrderDao#", "table:orders", "WRITES")
    assert _edge(edges, "sql:com.example.order.dao.OrderDao#", "pkg_order.confirm_order", "EXECUTES")
    w = _edge(edges, "pkg_stock.sync_to_order", "table:order_stock_hist", "WRITES")
    assert w and w[0].props["dblink"] == "orderdb"
    # file hand-over: SQL*Plus spool, cp, sftp put, JP1 file watch, Java importer, ftp get + SQL*Loader
    assert _edge(edges, "export_orders.sql", "datafile:orders_work.csv", "WRITES")
    assert _edge(edges, "export_orders.sh", "datafile:orders.csv", "WRITES")
    assert _edge(edges, "send_orders.sh", "datafile:orders.csv", "READS")
    watch = _edge(edges, "J200_WATCH", "datafile:orders.csv", "READS")
    assert watch and watch[0].props["via"] == "jp1-file-watch"
    assert _edge(edges, "ImportOrdersMain#main", "datafile:orders.csv", "READS")
    assert _edge(edges, "ShipResultSender#send", "datafile:ship_result.csv", "WRITES")
    assert _edge(edges, "load_ship_result.sh", "datafile:ship_result.csv", "READS")
    assert _edge(edges, "ship_result.ctl", "table:order_ship_result", "WRITES")


def test_transaction_boundaries_and_risks(system):
    _, _, stats, nodes, edges = system
    po = nodes["java:com.example.order.ejb.OrderServiceBean#placeOrder(String,String,int,String)"]
    assert po.props["tx"]["boundary"] == "REQUIRED"
    assert {"orders", "order_items"} <= set(po.props["tx"]["tables"])
    codes = {r["code"] for r in po.props["tx_risks"]}
    assert {"REMOTE_IN_TX", "INDEPENDENT_TX", "PROC_COMMIT_IN_TX", "AUTONOMOUS"} <= codes
    remote = " ".join(r["message"] for r in po.props["tx_risks"] if r["code"] == "REMOTE_IN_TX")
    assert "SOAP" in remote and "REST" in remote
    # ejb-jar.xml overrides the class annotation
    co = nodes["java:com.example.order.ejb.OrderServiceBean#cancelOrder(String)"]
    assert co.props["tx"]["boundary"] == "REQUIRES_NEW" and co.props["tx"]["source"] == "descriptor"
    assert "LOCK" in {r["code"] for r in co.props["tx_risks"]}
    # bean-managed transaction committing every 100 rows
    cd = nodes["java:com.example.order.ejb.OrderBatchBean#closeDay(String)"]
    assert cd.props["tx"]["boundary"] == "BEAN"
    assert "COMMIT_IN_LOOP" in {r["code"] for r in cd.props["tx_risks"]}
    # PL/SQL traits
    assert "AUTONOMOUS" in {r["code"] for r in nodes["dbobj:procedure:pkg_order.log_status"].props["tx_risks"]}
    assert "COMMIT_IN_LOOP" in {r["code"] for r in nodes["dbobj:procedure:pkg_order.purge_audit"].props["tx_risks"]}
    inv = nodes["java:com.example.inventory.InventoryEndpoint#reserveStock(String,int,String)"]
    assert "DBLINK_WRITE" in {r["code"] for r in inv.props["tx_risks"]}
    # the risk is also shown where it happens
    sei = nodes["java:com.example.order.client.InventoryPortType#reserveStock(String,int,String)"]
    assert any(r["code"] == "REMOTE_IN_TX" and r["root"] == po.id for r in sei.props["tx_risks"])
    assert stats["transactions"]["risks"]["REMOTE_IN_TX"] >= 2


def test_change_follows_the_data_to_the_other_server(system):
    _, e, _, _, _ = system
    rid = e.start_run(NOTE, background=False)
    st = e.state(rid)
    c = st["candidates"]
    by_name = {v["name"]: v for v in c.values()}
    for must in ("orders.note", "shipment_req.note", "OrderForm.note", "export_orders.sql", "orders_work.csv",
                 "orders.csv", "send_orders.sh", "/SHIP/RECV/J200_WATCH", "/SHIP/RECV/J210_IMPORT",
                 "ImportOrdersMain.main()"):
        assert must in by_name, must
    # data hand-over is kept for review even where the score has decayed
    assert by_name["send_orders.sh"]["data_flow"] and by_name["send_orders.sh"]["verdict"] != "not_impacted"
    # transaction risks travel with candidates and into the report
    po = by_name["OrderServiceBean.placeOrder()"]
    assert {r["code"] for r in po["tx_risks"]} >= {"REMOTE_IN_TX"} and po["tx"]["boundary"] == "REQUIRED"
    e.decide(rid, po["node_id"], "confirmed", "NOTEを受け渡す")
    rep = e.report(rid)
    assert "トランザクション・連携上の注意" in rep and "REMOTE_IN_TX" in rep


def test_transfer_script_directions():
    rules = load_rules()
    src = b"#!/bin/sh\nsftp -b - u@h <<EOF\nput /data/out/a.csv /recv/b.csv\nget /send/c.csv /data/in/d.csv\nbye\nEOF\n" \
          b"scp /data/out/e.csv u@h:/recv/f.csv\nsqlldr u/p control=/app/x.ctl data=/data/in/g.csv\n"
    f = text.extract("ops/t.sh", src, rules)
    got = {(r["rel"], r["key"]) for r in f.refs}
    for want in (("READS", "datafile:/data/out/a.csv"), ("WRITES", "datafile:/recv/b.csv"),
                 ("READS", "datafile:/send/c.csv"), ("WRITES", "datafile:/data/in/d.csv"),
                 ("READS", "datafile:/data/out/e.csv"), ("WRITES", "datafile:/recv/f.csv"),
                 ("EXECUTES", "file:/app/x.ctl"), ("READS", "datafile:/data/in/g.csv")):
        assert want in got, want


def test_api_indexes_a_system(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HIMOTOKI_DATA_DIR", str(tmp_path))
    for k in ("HIMOTOKI_API_TOKEN", "HIMOTOKI_ALLOWED_ROOTS", "HIMOTOKI_ALLOWED_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    import himotoki.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    assert c.post("/api/index", json={"repos": {"../x": REPOS["order-web"]}, "graph": "s"}).status_code == 400
    assert c.post("/api/index", json={"repos": REPOS}).status_code == 400            # system name required
    assert c.post("/api/index", json={}).status_code == 400
    r = c.post("/api/index", json={"repos": REPOS, "graph": "sor"})
    assert r.status_code == 200
    for _ in range(200):
        if c.get("/api/index", params={"graph": "sor"}).json().get("status") == "done":
            break
        time.sleep(0.05)
    rep = c.get("/api/index/report", params={"graph": "sor"}).json()
    assert rep["transactions"]["boundaries"] >= 5
    assert c.get("/api/graphs").json()["graphs"]["sor"]["repos"].keys() == REPOS.keys()
