"""What the detail panel shows around one element: a column with its schema, table, DDL and
readers / writers; a job with its job net, the jobs before and after, the chain of what it
runs (also through a wrapper function such as `run_java`), and who picks its output file
up; the whole file (only indexed files); and issue drafts from a reviewed analysis."""
import subprocess

import pytest

from himotoki.analysis.context import data_context, flow_context, links, node_context
from himotoki.workspace import Workspace

FILES = {
    "db/ddl/T_PAY.sql": """-- 支払予定
CREATE TABLE kobai.T_PAY (
  PAY_NO    VARCHAR2(10) NOT NULL, -- 支払番号
  HOHO_KBN  VARCHAR2(1),           -- 支払方法区分
  AMOUNT    NUMBER(12)             -- 金額
);
""",
    "kobai/src/main/java/jp/co/ex/kobai/PayDao.java": """package jp.co.ex.kobai;
import java.sql.*;
public class PayDao {
    public int countByHoho(Connection con, String kbn) throws SQLException {
        PreparedStatement ps = con.prepareStatement("SELECT COUNT(*) FROM T_PAY WHERE HOHO_KBN = ?");
        ps.setString(1, kbn);
        ResultSet rs = ps.executeQuery();
        rs.next();
        return rs.getInt(1);
    }
    public void markSent(Connection con, String no) throws SQLException {
        PreparedStatement ps = con.prepareStatement("UPDATE T_PAY SET HOHO_KBN = '9' WHERE PAY_NO = ?");
        ps.setString(1, no);
        ps.executeUpdate();
    }
}
""",
    "kobai/src/main/java/jp/co/ex/kobai/batch/FbBatch.java": """package jp.co.ex.kobai.batch;
import jp.co.ex.kobai.PayDao;
public class FbBatch {
    public static void main(String[] args) throws Exception {
        new PayDao().markSent(null, args[0]);
    }
}
""",
    "kobai/shell/KOB090.sh": """#!/bin/sh
# 総合振込データ作成
. /opt/app/common/env.sh
run_java jp.co.ex.kobai.batch.FbBatch $1
cp /work/out/FB_DATA.dat /send/FB_DATA.dat
""",
    "kobai/shell/KOB095.sh": """#!/bin/sh
# 振込データ送信
sftp -b - fbgw <<EOF
put /send/FB_DATA.dat /in
EOF
""",
    "common/env.sh": "export APP_HOME=/opt/app\n",
    "jobs/KOB_PAY.ajs": """unit=/APP/KOB_PAY,,admin,;
{
  ty=n;
  el=KOB090,j,+80+80;
  el=KOB095,j,+240+80;
  ar=(f=KOB090,t=KOB095);
  unit=KOB090,,admin,;
  {
    ty=j;
    cm="総合振込データ作成";
    sc="/opt/app/kobai/shell/KOB090.sh";
  }
  unit=KOB095,,admin,;
  {
    ty=j;
    cm="振込データ送信";
    sc="/opt/app/kobai/shell/KOB095.sh";
  }
}
""",
}


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    repo = tmp_path_factory.mktemp("repo")
    for p, t in FILES.items():
        f = repo / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(t, encoding="utf-8")
    for a in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "i"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    root = tmp_path_factory.mktemp("ws")
    ws = Workspace(str(root))
    ws.index(str(repo), None, name="g")
    e = ws.engine("g")
    e.test_root = str(root)
    return e


def test_column_shows_schema_table_definition_and_users(eng):
    d = data_context(eng.store, "col:t_pay.hoho_kbn")
    assert d["type"] == "column"
    assert d["schema"] == "kobai"                                  # CREATE TABLE kobai.T_PAY
    assert d["table"]["name"] == "t_pay" and d["table"]["logical"].startswith("支払予定")
    assert d["column"]["logical"] == "支払方法区分"
    (df,) = d["column"]["definitions"]
    assert df["file"] == "db/ddl/T_PAY.sql" and df["line"] == 4 and df["type"].startswith("VARCHAR2(1)")
    ops = {u["owner"]["name"] if u.get("owner") else u["name"]: u["ops"] for u in d["users"]}
    assert any("countByHoho" in k and "R" in v for k, v in ops.items())
    assert any("markSent" in k and "U" in v for k, v in ops.items())
    t = data_context(eng.store, "table:t_pay")
    assert [c["name"] for c in t["table"]["columns"]] == ["pay_no", "hoho_kbn", "amount"]


def test_program_shows_the_tables_it_touches(eng):
    d = data_context(eng.store, "java:jp.co.ex.kobai.PayDao")
    assert d["type"] == "touches"
    (t,) = d["tables"]
    assert t["name"] == "t_pay" and "R" in t["ops"] and "U" in t["ops"] and "hoho_kbn" in t["columns"]


def test_job_flow_follows_wrapper_functions_and_the_file_handed_over(eng):
    f = flow_context(eng.store, "job:/APP/KOB_PAY/KOB090")
    (fl,) = f["flows"]
    assert fl["jobnet"]["name"] == "/APP/KOB_PAY"
    assert [j["name"] for j in fl["net_jobs"]] == ["KOB090", "KOB095"]
    assert [b["name"].split("/")[-1] for b in fl["after"]] == ["KOB095"]
    chain = {c["name"]: c for c in fl["chain"]}
    assert "KOB090.sh" in chain and "FbBatch" in chain             # `run_java <class>` is followed
    # through the program's SQL, also in what its main method calls
    assert any(d["name"] == "t_pay" and "U" in d["ops"] for d in fl["data"])
    # the batch program is shown inside the flow of the job that runs it
    g = flow_context(eng.store, "java:jp.co.ex.kobai.batch.FbBatch")
    assert g and g["flows"][0]["job"]["id"] == "job:/APP/KOB_PAY/KOB090"


def test_links_are_grouped_in_the_reviewers_words(eng):
    labels = {g["label"]: g for g in links(eng.store, "col:t_pay.hoho_kbn")}
    assert "読んでいる処理" in labels and "定義している場所" in labels and "親（含まれている先）" in labels
    # a SQL statement is named by the method it belongs to
    r = labels["読んでいる処理"]["items"][0]
    assert r["owner"] and "countByHoho" in r["owner"]["name"]
    assert node_context(eng.store, "table:t_pay")["data"]["type"] == "table"


def test_file_view_serves_indexed_files_only(eng):
    v = eng.file_view("kobai/shell/KOB090.sh")
    assert v["lines"][3]["text"].startswith("run_java") and v["total"] == 5
    assert eng.file_view("../../etc/passwd") is None
    assert eng.file_view("not/indexed.txt") is None
    j = eng.file_view("kobai/src/main/java/jp/co/ex/kobai/PayDao.java")
    assert any(o["name"].startswith("PayDao.countByHoho") for o in j["outline"])


def test_issue_drafts_carry_the_run_and_split_by_subsystem(eng):
    from himotoki.analysis.issues import issue_drafts
    rid = eng.start_run("支払方法区分にでんさいを追加する", background=False)
    st = eng.state(rid)
    for nid in ("col:t_pay.hoho_kbn", "java:jp.co.ex.kobai.PayDao#markSent(Connection,String)", "job:/APP/KOB_PAY/KOB090"):
        if nid not in st["candidates"]:
            eng.add_candidate(rid, nid, "テストで追加") if hasattr(eng, "add_candidate") else None
        eng.decide(rid, nid, "confirmed", "確認済み", "tester")
    one = issue_drafts(eng, rid, "one")
    (i,) = one["issues"]
    assert i["body"].startswith(f"<!-- himotoki-run: {rid} -->")
    assert "## DB（テーブル・列）" in i["body"] and "kobai.t_pay.hoho_kbn" in i["body"] and "db/ddl/T_PAY.sql:4" in i["body"]
    assert "## ジョブ・連携" in i["body"] and "後続 KOB095" in i["body"]
    assert "verify_change" in i["body"]
    sub = issue_drafts(eng, rid, "subsystem")
    keys = [x["key"] for x in sub["issues"]]
    assert keys[0] == "parent" and "kobai" in keys and "db" in keys      # the column goes with its DDL folder
    assert all(x["body"].startswith(f"<!-- himotoki-run: {rid} -->") for x in sub["issues"])


def test_api(eng, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HIMOTOKI_DATA_DIR", eng.test_root)
    import himotoki.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    r = c.get("/api/context/col:t_pay.hoho_kbn", params={"graph": "g"}).json()
    assert r["data"]["table"]["name"] == "t_pay" and r["links"]
    assert c.get("/api/context/nosuch", params={"graph": "g"}).status_code == 404
    assert c.get("/api/file", params={"graph": "g", "path": "kobai/shell/KOB090.sh"}).json()["total"] == 5
    for bad in ("../../etc/passwd", "/etc/passwd", "kobai/../../etc/passwd"):
        assert c.get("/api/file", params={"graph": "g", "path": bad}).status_code == 404
