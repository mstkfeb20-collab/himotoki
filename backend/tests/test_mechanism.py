"""Tests of the framework-agnostic mechanism: rule packs, cross-language linking,
jobs / scripts / data files, Office documents, DB catalog and runtime SQL, the
fact cache, git diff impact, webhooks. Everything runs offline on a repository
built in a temporary directory."""
import os
import subprocess
import textwrap

import pytest

from impact.analysis.engine import Engine
from impact.extractors.sql import analyze
from impact.model import Rel
from impact.pipeline.run import Pipeline
from impact.sources.catalog import CatalogBuilder, save_source
from impact.sources.git import GitRepo, parse_webhook
from impact.sources.runtime import build as runtime_build
from impact.store import LocalStore

RULES = """
name: inhouse
description: an in-house framework that keeps SQL in XML files keyed by name
java:
  literal_namespaces: [q]
xml:
  - name: query
    when: {root: queries}
    select: /queries/query
    node:
      kind: SqlStatement
      id: "q:{@name}"
      name: "{@name}"
      keys: ["q:{@name}"]
      sql: body
"""

FILES = {
    "src/main/java/app/OrderDao.java": """
        package app;
        public class OrderDao {
            private static final String FIND = "findOrder";
            public Object find(String id) { return run(FIND, id); }
            public void purge() { run("purgeOrders"); }
            private Object run(String q, Object... a) { return null; }
        }
    """,
    "src/main/resources/queries/order-queries.xml": """
        <queries>
          <query name="findOrder">SELECT order_id, note FROM orders WHERE order_id = ?</query>
          <query name="purgeOrders">DELETE FROM orders WHERE status = '9'</query>
        </queries>
    """,
    "db/ddl/orders.sql": """
        CREATE TABLE orders (
          order_id CHAR(8) NOT NULL,
          note VARCHAR(80),
          status CHAR(1)
        );
    """,
    "ops/jobs/AJS_NIGHT.txt": """
        unit=/NIGHT,,admin,;
        {
        ty=n;
        cm="nightly";
        unit=J010,,admin,;
        {
        ty=j;
        sc=/opt/app/bin/export_orders.sh;
        }
        unit=J020,,admin,;
        {
        ty=j;
        sc=/opt/app/bin/load_orders.sh;
        }
        ar=(f=J010,t=J020);
        }
    """,
    "ops/bin/export_orders.sh": """
        #!/bin/sh
        psql -c "SELECT order_id, note FROM orders" > /data/orders.csv
    """,
    "ops/bin/load_orders.sh": """
        #!/bin/sh
        cat /data/orders.csv | java app.LoadMain
    """,
    "ops/crontab": """
        0 2 * * * /opt/app/bin/export_orders.sh
    """,
}


def _git(repo, *a):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com", *a],
                   check=True, capture_output=True)


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "repo"
    for p, body in FILES.items():
        fp = repo / p
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(textwrap.dedent(body).lstrip())
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "テーブル定義"
    ws.append(["テーブル名", "ORDERS", "", "論理名", "受注"])     # key/value line above the real header
    ws.append([])
    ws.append(["No", "項目名(物理)", "論理名", "データ型", "桁数", "必須"])
    ws.append([1, "ORDER_ID", "受注番号", "CHAR", 8, "○"])
    ws.append([2, "NOTE", "備考", "VARCHAR", 80, ""])
    crud = wb.create_sheet("CRUD")
    crud.append(["機能ID", "機能名", "ORDERS", "CUSTOMERS", "ITEMS"])
    crud.append(["F01", "受注登録", "C", "R", "R"])
    crud.append(["F02", "受注削除", "D", "", "R"])
    (repo / "ops" / "docs").mkdir(parents=True)
    wb.save(repo / "ops" / "docs" / "設計書.xlsx")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-qm", "init")
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "inhouse.yaml").write_text(RULES)
    data = tmp_path / "data"
    data.mkdir()
    return repo, rules, data


def _index(repo, rules, data, **kw):
    store = LocalStore(str(data / "graph.json"))
    stats = Pipeline(str(data), rules_dir=str(rules), workers=0).index(str(repo), store, **kw)
    return store, stats


def outs(store, src, rel=None):
    return {e.dst for e in store.out_edges(src, [rel] if rel else None)}


def test_rule_pack_links_java_to_xml_sql_to_tables(env):
    repo, rules, data = env
    store, stats = _index(repo, rules, data)
    # a framework unknown to the code base is supported by the YAML pack alone
    assert store.get("q:findOrder").kind == "SqlStatement"
    find = "java:app.OrderDao#find(String)"
    assert "q:findOrder" in outs(store, find)                    # via a class constant
    assert "q:purgeOrders" in outs(store, "java:app.OrderDao#purge()")
    assert "col:orders.note" in outs(store, "q:findOrder", Rel.READS)
    assert "table:orders" in outs(store, "q:purgeOrders", Rel.WRITES)
    assert "col:orders.note" in outs(store, "file:db/ddl/orders.sql", Rel.DEFINES)
    # every edge carries provenance and confidence
    e = next(iter(store.out_edges("q:findOrder", [Rel.READS])))
    assert e.props.get("src") and 0 < e.conf <= 1
    assert stats["unresolved"].get("q", 0) == 0


def test_jobs_scripts_and_data_files_chain(env):
    repo, rules, data = env
    store, _ = _index(repo, rules, data)
    j10, j20 = "job:/NIGHT/J010", "job:/NIGHT/J020"
    assert j20 in outs(store, j10, Rel.PRECEDES)
    exp = next(iter(outs(store, j10, Rel.RUNS)))
    assert exp.endswith("export_orders.sh")
    assert "table:orders" in outs(store, exp, Rel.READS) or \
        any("orders" in d for d in outs(store, exp))           # SQL inside psql -c
    written = {d for d in outs(store, exp, Rel.WRITES) if d.startswith("datafile:")}
    load = next(iter(outs(store, j20, Rel.RUNS)))
    assert written and written & outs(store, load, Rel.READS)   # file hand-off between jobs
    cron = [n for n in store.nodes(["CronEntry"])]
    assert cron and exp in outs(store, cron[0].id, Rel.RUNS)


def test_office_header_detection_and_crud_matrix(env):
    repo, rules, data = env
    store, _ = _index(repo, rules, data)
    docs = {n.id: n for n in store.nodes(["DocEntity"])}
    rows = [i for i, n in docs.items() if "col:orders.note" in outs(store, i, Rel.DESCRIBES)]
    assert rows, "the real header row (after the key/value line) must be used"
    crud = [i for i in docs if i.endswith("#crud")]
    assert len(crud) == 2
    assert "table:orders" in {e.dst for i in crud for e in store.out_edges(i, [Rel.DOC_CRUD])}
    # the table-definition sheet is not mistaken for a CRUD matrix
    assert not any("テーブル定義" in i for i in crud)


def test_catalog_drift_and_runtime_matching(env):
    repo, rules, data = env
    cb = CatalogBuilder("db", "postgres")
    for col, t in (("order_id", "character"), ("note", "character varying"), ("status", "character"),
                   ("cancel_reason", "character varying")):
        cb.column("public", "orders", col, t, "YES", None, "")
    save_source(str(data), cb.f)
    rt = runtime_build("rt", [("SELECT order_id, note FROM orders WHERE order_id = $1", 120, {}),
                              ("SELECT count(*) FROM orders o WHERE o.status = $1 GROUP BY o.note", 3, {})])
    save_source(str(data), rt)
    store, stats = _index(repo, rules, data)
    assert stats["drift"]["columns_only_in_db"] == ["col:orders.cancel_reason"]
    assert stats["runtime"]["statements"] == 2
    assert stats["runtime"]["matched"] >= 1 and stats["runtime"]["unmatched"] >= 1
    matched = [e for n in store.nodes(["RuntimeSql"]) for e in store.out_edges(n.id, [Rel.OBSERVED_AS])]
    assert any(e.dst == "q:findOrder" for e in matched)


def test_fact_cache_reextracts_only_changed_files(env):
    repo, rules, data = env
    _, s1 = _index(repo, rules, data)
    _, s2 = _index(repo, rules, data)
    assert s1["parsed"] == s1["files"] and s2["parsed"] == 0
    p = repo / "db/ddl/orders.sql"
    p.write_text(p.read_text().replace("status CHAR(1)", "status CHAR(1),\n  memo VARCHAR(200)"))
    store, s3 = _index(repo, rules, data)
    assert s3["parsed"] == 1
    assert "col:orders.memo" in outs(store, "file:db/ddl/orders.sql", Rel.DEFINES)


def test_diff_mode_regression_scope(env):
    repo, rules, data = env
    store, _ = _index(repo, rules, data)
    _git(str(repo), "checkout", "-qb", "feature")
    p = repo / "src/main/resources/queries/order-queries.xml"
    p.write_text(p.read_text().replace("SELECT order_id, note", "SELECT order_id, note, status"))
    _git(str(repo), "commit", "-qam", "add status")
    gr = GitRepo(str(repo), str(data))
    hunks = gr.hunks(gr.resolve("master") if _has(repo, "master") else gr.resolve("main"), gr.resolve("feature"))
    eng = Engine(store, str(data), str(repo))
    nodes, added = eng.diff_nodes(hunks)
    assert any(n.get("node_id", n.get("id")) == "q:findOrder" for n in nodes)
    rid = eng.start_run("diff", None, {"mode": "diff", "diff_nodes": nodes, "added_files": added}, background=False)
    st = eng.state(rid)
    assert "java:app.OrderDao#find(String)" in st["candidates"]
    assert "影響分析" in eng.scope_markdown(st, "test")


def _has(repo, branch):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", branch], capture_output=True).returncode == 0


def test_requirement_reaches_code_jobs_and_documents(env):
    repo, rules, data = env
    store, _ = _index(repo, rules, data)
    eng = Engine(store, str(data), str(repo))
    rid = eng.start_run("受注の備考(note)を80文字から200文字に変更する", background=False)
    c = eng.state(rid)["candidates"]
    assert "col:orders.note" in c
    assert "q:findOrder" in c and "java:app.OrderDao#find(String)" in c
    assert any(k.startswith("doc:") for k in c)
    assert any("export_orders.sh" in k for k in c)


def test_sql_analysis_edge_cases():
    assert analyze("SELECT a INTO v_a FROM orders WHERE id = 1")["tables"] == {"orders": {"R"}}
    assert analyze("SELECT nextval('s') INTO v;")["tables"] == {}
    r = analyze("INSERT INTO orders (order_id, note) VALUES (?, ?)")
    assert "C" in r["tables"]["orders"]


def test_webhook_payloads():
    gl = parse_webhook({}, {"object_kind": "merge_request", "project": {"id": 1},
                            "object_attributes": {"iid": 3, "target_branch": "main", "source_branch": "f",
                                                  "title": "t", "last_commit": {"id": "abc"}}})
    assert gl["kind"] == "review" and gl["base"] == "main" and gl["head_sha"] == "abc"
    gh = parse_webhook({"X-GitHub-Event": "push"}, {"ref": "refs/heads/dev", "after": "x"})
    assert gh == {"kind": "push", "branch": "dev", "sha": "x"}
    assert parse_webhook({}, {"object_kind": "note"}) is None
