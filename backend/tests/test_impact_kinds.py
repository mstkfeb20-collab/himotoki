"""Kinds of impact: an element is edited (change), only checked by testing (verify), or
needs agreement outside the code (coordinate). The judge decides the kind with evidence,
a reviewer's decision carries it, and the estimate, the hand-off, the issues and the
verification treat the kinds differently."""
import json
import os
import re

import pytest

import himotoki.analysis.llm as L
from himotoki.analysis.engine import Engine
from himotoki.analysis.handoff import build_handoff, handoff_markdown, verify_change
from himotoki.analysis.impact import normalise, rule_impact
from himotoki.analysis.issues import issue_drafts
from himotoki.model import Kind, Rel
from himotoki.pipeline.run import Pipeline
from himotoki.store.local import LocalStore

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "minirepo")
REQ = "郵便番号の入力チェックを7桁から8桁に変更する。"


def test_rule_guess_from_how_the_element_was_reached():
    assert rule_impact({"kind": Kind.METHOD, "origin": "seed"}, "impacted")[0] == "change"
    caller = {"kind": Kind.METHOD, "origin": "propagation", "via_rel": Rel.CALLS, "via_dir": "in"}
    assert rule_impact(caller, "impacted")[0] == "verify"
    writer = {"kind": Kind.SQL_STATEMENT, "origin": "propagation", "via_rel": Rel.WRITES, "via_dir": "in"}
    assert rule_impact(writer, "uncertain")[0] == "change"
    reader = {"kind": Kind.SQL_STATEMENT, "origin": "propagation", "via_rel": Rel.READS, "via_dir": "in"}
    assert rule_impact(reader, "impacted")[0] == "verify"
    assert rule_impact(reader, "impacted", "delete")[0] == "change"       # a reader of what is removed changes
    assert rule_impact({"kind": Kind.DOC, "origin": "propagation"}, "impacted")[0] == "change"
    assert rule_impact(caller, "not_impacted")[0] == "none"
    flow = dict(caller, data_flow=True, via_rel=Rel.RUNS)
    assert "連携先" in rule_impact(flow, "uncertain")[1]["check"]


def test_verdict_and_kind_stay_consistent():
    assert normalise("impacted", "none") == ("not_impacted", "none")
    assert normalise("not_impacted", "verify") == ("not_impacted", "none")
    assert normalise("impacted", "unknown") == ("uncertain", "unknown")
    assert normalise("impacted", "bogus") == ("impacted", None)


class KindsClaude(L.ClaudeLLM):
    """The judge with the network call replaced: the controller changes, everything else is checked."""
    model = "claude-sonnet-5-5"

    def __init__(self, vocabulary=None, identifiers=None):
        L.RuleBasedLLM.__init__(self, vocabulary, identifiers)
        self.items = []

    def _ask(self, system, user, max_tokens=4000, meter=None, purpose=""):
        if "kind of impact" in system:
            items = json.loads(user.split("Items:\n", 1)[1])
            self.items += items
            res = []
            for it in items:
                if "Controller" in it["name"]:
                    res.append({"id": it["id"], "impact": "change", "verdict": "impacted", "confidence": 0.9,
                                "reason": "入力チェックの桁を直す", "evidence": {"where": "@Size(max=7) を 8 に"}})
                else:
                    res.append({"id": it["id"], "impact": "verify", "verdict": "impacted", "confidence": 0.7,
                                "reason": "呼び出し先が変わる", "evidence": {"check": "8桁で保存・表示できること"}})
            return json.dumps({"results": res})
        if "decompose" in system:
            return json.dumps({"intents": json.loads(user.split("Rule-based draft: ", 1)[1])})
        return "answer"


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("HIMOTOKI_LLM_") or k == "HIMOTOKI_SETTINGS":
            monkeypatch.delenv(k)
    store = LocalStore(str(tmp_path / "graph.json"))
    Pipeline(str(tmp_path)).index(FIX, store)
    e = Engine(store, str(tmp_path), FIX, settings_dir=str(tmp_path))
    e.llm = KindsClaude(e.llm.vocab, e.llm.identifiers)
    rid = e.start_run(REQ, params={"llm_max_items": "unlimited", "llm_min_score": 0}, background=False)
    return e, rid


def test_the_judge_gets_the_link_and_returns_kinds_with_evidence(eng):
    e, rid = eng
    sent = e.llm.items
    assert sent and all("found_by" in it for it in sent)
    assert any(it.get("link") for it in sent)                      # how the element hangs on its parent
    cands = e.state(rid)["candidates"].values()
    ctrl = [c for c in cands if "Controller" in c["name"] and c["judged_by"] == "ai"]
    assert ctrl and all(c["impact"] == "change" and c["evidence"]["where"] for c in ctrl)
    others = [c for c in cands if c["judged_by"] == "ai" and "Controller" not in c["name"]]
    assert others and all(c["impact"] == "verify" and c["evidence"]["check"] for c in others)


def test_decisions_estimate_handoff_issues_and_verification_follow_the_kind(eng):
    e, rid = eng
    st = e.state(rid)
    ctrl = next(c for c in st["candidates"].values() if c["kind"] == Kind.METHOD and "Controller" in c["name"])
    other = next(c for c in st["candidates"].values()
                 if c["kind"] == Kind.METHOD and "Controller" not in c["name"] and c["impact"] == "verify")
    e.decide(rid, ctrl["node_id"], "confirmed", "桁を直す")                 # the judge's kind is kept
    e.decide(rid, other["node_id"], "confirmed", "動作確認だけ")
    c2 = e.state(rid)["candidates"]
    assert c2[ctrl["node_id"]]["decision"]["impact"] == "change"
    assert c2[other["node_id"]]["decision"]["impact"] == "verify"
    by = e.state(rid)["estimate"]["by_impact"]
    assert by["change"]["confirmed_days"] > 0 and by["verify"]["confirmed_days"] > 0

    h = build_handoff(e, rid)
    edited = {i["node_id"] for f in h["files"] for i in f["items"]}
    assert ctrl["node_id"] in edited and other["node_id"] not in edited
    assert [v["node_id"] for v in h["verify"]] == [other["node_id"]]
    assert h["verify"][0]["evidence"]["check"]
    md = handoff_markdown(h)
    assert "試験で確かめるもの" in md and "8桁で保存・表示できること" in md

    body = issue_drafts(e, rid)["issues"][0]["body"]
    assert "試験で確かめるもの（変更しない）" in body and other["name"] in body

    # the reviewer changes only the kind of a confirmed element
    e.decide(rid, other["node_id"], "keep", impact="coordinate")
    assert e.state(rid)["candidates"][other["node_id"]]["decision"]["impact"] == "coordinate"
    e.decide(rid, other["node_id"], "keep", impact="verify")

    # verification: the edited element changed (hit); the checked one also changed -> wrong kind
    n1, n2 = e.store.get(ctrl["node_id"]), e.store.get(other["node_id"])
    hunks = {n1.file: [(n1.line, n1.line)], n2.file: [(n2.line, n2.line)]}
    v = verify_change(e, rid, hunks)
    assert any(m["why"] == "judged_verify" for m in v["missed"])
    assert all(u["node_id"] != other["node_id"] for u in v["untouched"])   # a test point is not a wrong prediction
