"""Value flow: a screen input followed through the code to the column it is written to or
searched by, by static analysis only."""
import os

import pytest

from himotoki.pipeline.run import Pipeline
from himotoki.pipeline.sqlparams import markers
from himotoki.store.local import LocalStore

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "sorreaders")
P = "java:jp.co.acme."


@pytest.fixture(scope="module")
def flows(tmp_path_factory):
    d = tmp_path_factory.mktemp("g")
    st = LocalStore(str(d / "graph.json"))
    Pipeline(str(d)).index(FIX, st)
    out = {}
    for n in st.nodes():
        for e in st.out_edges(n.id, ["FLOWS_TO"]):
            out[(e.src.replace(P, ""), e.dst)] = e.props
    return out


def test_markers_name_their_columns():
    m = markers("UPDATE M_CUST C SET CUST_NM = ?, UPD_DT = SYSDATE WHERE C.CUST_CD = ?")
    assert [(x["table"], x["column"], x["usage"]) for x in m] == [("M_CUST", "CUST_NM", "write"), ("M_CUST", "CUST_CD", "cond")]
    m = markers("INSERT INTO T (A, B, C) VALUES (?, TRIM(?), NVL(?, '0'))")
    assert [x["column"] for x in m] == ["A", "B", "C"]
    m = markers("SELECT * FROM T_H H, M_C C WHERE H.YMD BETWEEN ? AND ? AND C.NM LIKE ? || '%'")
    assert [(x["table"], x["column"]) for x in m] == [("T_H", "YMD"), ("T_H", "YMD"), ("M_C", "NM")]
    assert [x["name"] for x in markers("UPDATE T SET Q = #qty# WHERE K = #{item.k}")] == ["qty", "item.k"]


def test_form_to_dto_to_jdbc(flows):
    e = flows[("web.ShohinForm.shohinCd", "col:m_shohin.shohin_cd")]
    assert "write" in e["usage"]
    assert any("trim" in h for h in e["path"]) and any("?1" in h for h in e["path"])     # i++ counted
    assert ("web.ShohinForm.shohinNm", "col:m_shohin.shohin_nm") in flows                 # via a local and toUpperCase


def test_session_carry_and_ibatis(flows):
    # put in the session on the entry screen, inserted by the confirm screen with iBATIS #prop#
    assert ("web.ShohinForm.shohinCd", "col:t_shohin_rireki.shohin_cd") in flows
    # a map with constant keys -> #tanka# / #cd#
    assert ("web.TankaForm.tanka", "col:m_shohin.tanka") in flows                          # copyProperties(dest, orig)
    assert "cond" in flows[("dao.ShohinDto.shohinCd", "col:m_shohin.shohin_cd")]["usage"]


def test_dynamic_sql_mybatis_and_request_parameters(flows):
    assert flows[("web.TankaForm.keyword", "col:m_shohin.shohin_nm")]["usage"] == "cond"   # StringBuffer + list
    assert flows[("reqparam:keyword", "col:m_shohin.shohin_nm")]["usage"] == "cond"        # mapper #{name}


def test_a_field_that_is_never_saved_has_no_flow(flows):
    assert not any(k[0] == "web.TankaForm.memo" for k in flows)


def test_dyna_form_properties(flows):
    assert flows[("formprop:KensakuForm.shohinNm", "col:m_shohin.shohin_nm")]["usage"] == "cond"


def test_shared_helpers_do_not_mix_callers_and_constant_keys(flows):
    # both actions pass their value through CodeUtil.zeroPad: each reaches only its own column
    assert ("web.ShohinForm.shohinCd", "col:m_shohin.shohin_cd") in flows
    assert "cond" in flows[("web.TankaForm.keyword", "col:m_shohin.shohin_cd")]["usage"]   # p.put(CodeUtil.KEY_CD, ..) -> #cd#
    assert ("web.TankaForm.keyword", "col:t_shohin_rireki.shohin_cd") not in flows
    assert ("web.ShohinForm.shohinCd", "col:m_shohin.tanka") not in flows


def test_stored_procedure_parameters(flows):
    assert flows[("reqparam:haishiCd", "col:m_shohin.shohin_cd")]["usage"] == "cond"
    assert "write" in flows[("reqparam:riyu", "col:m_shohin.shohin_nm")]["usage"]   # through a local variable
