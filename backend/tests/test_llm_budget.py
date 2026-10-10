"""AI-judgement budget: reuse on re-investigation, score threshold, per-run cap,
configuration from file / environment / run parameters, unlimited setting."""
import json
import os
import re
import time

import pytest

from codegraph.analysis import llm as L
from codegraph.analysis.engine import Engine
from codegraph.pipeline.run import Pipeline
from codegraph.settings import LLMBudget, load_llm_budget, parse_max_items
from codegraph.store import LocalStore

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "minirepo")
REQ = "郵便番号の入力チェックを7桁から8桁に変更する。"


class FakeClaude(L.ClaudeLLM):
    """ClaudeLLM with the network call replaced: records what would be sent."""
    def __init__(self, vocabulary=None, identifiers=None):
        L.RuleBasedLLM.__init__(self, vocabulary, identifiers)
        self.sent_ids = []

    def _ask(self, system, user, max_tokens=4000, meter=None):
        if meter is not None:
            meter["calls"] = meter.get("calls", 0) + 1
            meter["input_tokens"] = meter.get("input_tokens", 0) + len(user) // 4
            meter["output_tokens"] = meter.get("output_tokens", 0) + 10
        if "judge whether" in system:
            ids = re.findall(r'"id": "([^"]+)"', user)
            self.sent_ids += ids
            return json.dumps({"results": [{"id": i, "verdict": "impacted", "confidence": 0.8, "reason": "AI",
                                            "change_type": "modify"} for i in ids]})
        if "decompose" in system:
            draft = json.loads(user.split("Rule-based draft: ", 1)[1])
            return json.dumps({"intents": draft})
        return "answer"


@pytest.fixture()
def eng(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("CODEGRAPH_LLM_") or k == "CODEGRAPH_SETTINGS":
            monkeypatch.delenv(k)
    store = LocalStore(str(tmp_path / "graph.json"))
    Pipeline(str(tmp_path)).index(FIX, store)
    e = Engine(store, str(tmp_path), FIX, settings_dir=str(tmp_path))
    e.llm = FakeClaude(e.llm.vocab, e.llm.identifiers)
    return e, tmp_path


def run(e, **params):
    rid = e.start_run(REQ, params=params, background=False)
    return e.state(rid)


def test_unlimited_sends_everything_above_threshold(eng):
    e, _ = eng
    st = run(e, llm_max_items="unlimited", llm_min_score=0)
    u = st["llm_usage"]
    assert st["params"]["llm"]["max_items"] == "unlimited"
    assert u["ai_items"] == len(st["candidates"]) and u["rule_cap"] == 0 and u["rule_low_score"] == 0
    assert u["calls"] >= 2 and u["input_tokens"] > 0


def test_min_score_routes_weak_candidates_to_rules(eng):
    e, _ = eng
    st = run(e, llm_max_items="unlimited", llm_min_score=0.5)
    low = [c for c in st["candidates"].values() if c["judged_by"] == "rule:low_score"]
    assert low and all(c["score"] < 0.5 for c in low)
    assert all(c["score"] >= 0.5 for c in st["candidates"].values() if c["judged_by"] == "ai")
    assert st["llm_usage"]["rule_low_score"] == len(low)


def test_cap_keeps_the_highest_scores_for_the_ai(eng):
    e, _ = eng
    st = run(e, llm_max_items=3, llm_min_score=0)
    ai = [c for c in st["candidates"].values() if c["judged_by"] == "ai"]
    capped = [c for c in st["candidates"].values() if c["judged_by"] == "rule:cap"]
    assert len(ai) <= 3 and capped
    # within the first judging pass, AI-judged candidates outrank capped ones
    assert st["llm_usage"]["ai_items"] == len(ai) == 3
    assert run(e, llm_max_items=0)["llm_usage"]["ai_items"] == 0


def test_reinvestigation_reuses_parent_verdicts(eng):
    e, _ = eng
    parent = run(e)
    sent_before = len(e.llm.sent_ids)
    child = e.reinvestigate(parent["id"], [], "テストも確認して")
    for _ in range(200):
        if e.state(child)["status"] != "running":
            break
        time.sleep(0.05)
    cst = e.state(child)
    u = cst["llm_usage"]
    assert u["reused"] >= parent["llm_usage"]["ai_items"] - 1
    resent = set(e.llm.sent_ids[sent_before:]) & {k for k, c in parent["candidates"].items() if c["judged_by"] == "ai"}
    assert not resent, "parent AI verdicts must not be sent again"
    # switching reuse off asks again
    child2 = e.start_run(REQ, params={"llm_reuse_verdicts": False}, parent_id=parent["id"], background=False)
    assert e.state(child2)["llm_usage"]["reused"] == 0


def test_settings_file_env_and_run_params(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("CODEGRAPH_LLM_") or k == "CODEGRAPH_SETTINGS":
            monkeypatch.delenv(k)
    assert load_llm_budget(str(tmp_path)) == LLMBudget()
    (tmp_path / "settings.yaml").write_text("llm:\n  min_score: 0.45\n  max_items: unlimited\n")
    b = load_llm_budget(str(tmp_path))
    assert b.min_score == 0.45 and b.max_items is None
    monkeypatch.setenv("CODEGRAPH_LLM_MAX_ITEMS", "120")
    assert load_llm_budget(str(tmp_path)).max_items == 120
    monkeypatch.setenv("CODEGRAPH_LLM_MAX_ITEMS", "")          # empty = not set
    assert load_llm_budget(str(tmp_path)).max_items is None
    run_b = b.with_run_params({"min_score": 0.2, "llm_max_items": 50})
    assert run_b.max_items == 50 and run_b.min_score == 0.45   # plain min_score is the propagation cut-off
    for v, want in (("unlimited", None), ("無制限", None), (-1, None), (0, 0), ("80", 80), (None, None)):
        assert parse_max_items(v) == want
    with pytest.raises(ValueError):
        parse_max_items("lots")
    with pytest.raises(ValueError):
        LLMBudget().with_overrides({"min_score": 2})


def test_api_accepts_and_validates_budget(tmp_path, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("CODEGRAPH_DATA_DIR", str(tmp_path))
    import codegraph.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    assert c.post("/api/index", json={"repo": FIX}).status_code == 200
    for _ in range(200):
        if c.get("/api/index").json().get("status") == "done":
            break
        time.sleep(0.05)
    assert c.post("/api/runs", json={"requirement": "x", "llm_max_items": "lots"}).status_code == 400
    r = c.post("/api/runs", json={"requirement": REQ, "llm_max_items": "unlimited", "llm_min_score": 0.4})
    assert r.status_code == 200
    st = c.get(f"/api/runs/{r.json()['id']}").json()
    for _ in range(200):
        if st["status"] != "running":
            break
        time.sleep(0.05)
        st = c.get(f"/api/runs/{r.json()['id']}").json()
    assert st["params"]["llm"] == {"min_score": 0.4, "max_items": "unlimited", "reuse_verdicts": True,
                                   "rescore_delta": 0.1, "batch_size": 15}
    assert c.get("/api/settings/llm").json()["effective"]["max_items"] == 300
