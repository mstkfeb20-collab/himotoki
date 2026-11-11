"""Readers for assets a typical SoR has besides Java and SQL: Struts validation and tag
libraries, record layouts, report / vendor XML, design tables in Markdown, system IDs,
shell wrappers with variables, file names in settings, and the lengths of values."""
import os

import pytest

from himotoki.analysis.context import data_context
from himotoki.extractors.layout import parse
from himotoki.extractors.text import expand_vars
from himotoki.pipeline.run import Pipeline, classify
from himotoki.rules import load_rules
from himotoki.store.local import LocalStore
from himotoki.text import datafile_base, system_ids

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sorreaders")


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    d = tmp_path_factory.mktemp("g")
    st = LocalStore(str(d / "graph.json"))
    Pipeline(str(d)).index(FIX, st)
    return st


def edges(st, src, rel=None):
    return {(e.dst, e.rel) for e in st.out_edges(src) if rel is None or e.rel == rel}


def has(st, src, dst, rel):
    return any(e.dst == dst and e.rel == rel for e in st.out_edges(src))


def test_helpers():
    assert system_ids("HZK-G010 KOBB090 IF_ZAI_005 Kob041Action utf8") == ["HZKG010", "KOBB090", "IFZAI005", "KOB041"]
    for v in ("${OUT}/URIAGE_${YMD}.dat", "/data/URIAGE_20260401.dat", "URIAGE_yyyymmdd.dat", "URIAGE_%s.dat"):
        assert datafile_base(v) == "uriage.dat"
    out = expand_vars("D=/data/out\nF=${D}/X_${YMD}.dat\nsend_file $F")
    assert out.splitlines()[2] == "send_file /data/out/X_${YMD}.dat"


def test_layout_forms():
    hdr = parse("No,項目名,物理名,開始位置,桁数\n1,伝票番号,DENPYO_NO,1,10\n2,得意先,TOKUI_CD,11,8\n")
    assert [(f["name"], f["start"], f["length"], f["logical"]) for f in hdr] == \
        [("DENPYO_NO", 1, 10, "伝票番号"), ("TOKUI_CD", 11, 8, "得意先")]
    plain = parse("DENPYO_NO  1  10\nTOKUI_CD  11 8\n")
    assert [(f["start"], f["length"]) for f in plain] == [(1, 10), (11, 8)]
    cob = parse("01 REC.\n   05 DENPYO-NO PIC X(10).\n   05 KIN PIC S9(9)V99 COMP-3.\n")
    assert [(f["name"], f["length"]) for f in cob] == [("DENPYO-NO", 10), ("KIN", 6)]
    rules = load_rules()
    assert classify("if/layout/URIAGE_layout.txt", rules, "") == "layout"
    assert classify("src/LayoutUtil.java", rules, "") == "java"
    assert classify("docs/IF定義/uriage.csv", rules, "") == "docs"


def test_struts_validation_and_tags_reach_the_form_field(store):
    fld = "java:jp.co.acme.web.ShohinForm.shohinCd"
    assert has(store, "valid:ShohinForm.shohinCd", fld, "BINDS")
    jsp = [e for e in store.in_edges(fld, ["BINDS"]) if e.src == "file:web/WEB-INF/jsp/ABCG010.jsp"]
    assert jsp and jsp[0].props.get("maxlength") == "8"
    assert has(store, "ep:ANY /shohin/regist.do", "file:web/WEB-INF/jsp/ABCG010.jsp", "RENDERS")   # input=


def test_shell_wrappers_variables_and_settings_meet_on_one_data_file(store):
    df = "datafile:shohin.dat"
    assert has(store, "file:batch/sh/ABCB010.sh", df, "READS")           # send_file $OUT_FILE
    assert has(store, "file:batch/sh/ABCB020.sh", df, "WRITES")          # recv_file
    assert has(store, "config:export.shohin.file", df, "WRITES")         # export.shohin.file=/data/send/SHOHIN_%s.dat
    assert has(store, "java:jp.co.acme.batch.ShohinExport#main(String)", "config:export.shohin.file", "REFERENCES")
    assert has(store, "file:if/layout/SHOHIN_layout.txt", df, "DEFINES")  # by the stem of the layout's name


def test_layout_report_and_doc_tables_reach_columns(store):
    col = "col:m_shohin.shohin_cd"
    assert has(store, "layout:if/layout/SHOHIN_layout.txt#SHOHIN_CD", col, "MAPS_TO")
    assert has(store, "xml:report/R_ABC030.xml#4:field", col, "REFERENCES")
    assert has(store, "file:report/R_ABC030.xml", "table:m_shohin", "READS")
    rows = [e for e in store.in_edges(col, ["DESCRIBES"]) if e.props.get("via") == "office-table"]
    assert rows and rows[0].props.get("length") == "10"


def test_ids_link_documents_to_screens_and_jobs(store):
    sec = "doc:docs/画面設計書_ABC-G010.md#0"
    assert has(store, sec, "file:web/WEB-INF/jsp/ABCG010.jsp", "DESCRIBES")   # ABC-G010
    assert has(store, sec, "file:batch/sh/ABCB010.sh", "DESCRIBES")           # ABC-B010
    assert not has(store, sec, "file:docs/画面設計書_ABC-G010.md", "DESCRIBES")


def test_length_facts_flag_differences(store):
    lf = data_context(store, "col:m_shohin.shohin_cd")["column"]["lengths"]
    by = {(f["where"], f["length"], f["differs"]) for f in lf["facts"]}
    assert ("DDL", "10", False) in by and ("入力チェック", "10", False) in by
    assert ("画面", "8", True) in by and ("連携レイアウト", "8", True) in by
    assert lf["mismatch"]
