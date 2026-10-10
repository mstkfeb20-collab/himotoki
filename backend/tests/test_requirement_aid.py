"""Requirement writing aid: preview (rule-based, no AI, no trace) and the practice example."""
import os

from codegraph.analysis.engine import Engine
from codegraph.pipeline.run import Pipeline
from codegraph.store import LocalStore

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "minirepo")


def _engine(tmp_path):
    store = LocalStore(str(tmp_path / "graph.json"))
    Pipeline(str(tmp_path)).index(FIX, store)
    return Engine(store, str(tmp_path), FIX, settings_dir=str(tmp_path))


def test_preview_finds_starting_points_without_running(tmp_path):
    e = _engine(tmp_path)
    r = e.preview_requirement("郵便番号の入力チェックを7桁から8桁に変更する。")
    assert r["ai_used"] is False
    assert any(s["node_id"].endswith("Customer.zip") for s in r["seeds"])
    assert r["strong_seeds"] >= 1 and not r["warnings"]
    assert r["intents"][0]["change_type"] == "modify"
    # a preview is not a run: nothing saved, no events
    assert e.runs.list() == [] and e.runs.events("preview") == []


def test_preview_warns_about_vague_short_and_template_text(tmp_path):
    e = _engine(tmp_path)
    vague = e.preview_requirement("画面をいい感じに改善する")
    assert any("あいまい" in t for t in vague["tips"])
    assert vague["warnings"]                                   # nothing matched by name
    assert any("短すぎ" in w for w in e.preview_requirement("直す")["warnings"])
    tpl = e.preview_requirement("【対象】〇〇画面の△△（項目名）\n【変更】上限を 80文字 から 200文字 に変更する")
    assert any("〇〇" in w for w in tpl["warnings"])
    many = e.preview_requirement("郵便番号を8桁にする。氏名を必須にする。住所を削除する。電話番号を追加する。メールを追加する。")
    assert any("分ける" in w for w in many["warnings"])
    assert e.preview_requirement("")["seeds"] == []


def test_preview_hints_old_value(tmp_path):
    e = _engine(tmp_path)
    r = e.preview_requirement("郵便番号の入力チェックを8桁に変更する")
    assert any("変更前の値" in t for t in r["tips"])


def test_example_requirement_is_built_from_the_repository(tmp_path):
    e = _engine(tmp_path)
    ex = e.example_requirement()
    if ex is not None:                       # needs a field with a length limit in the repository
        assert ex["based_on"] in {n.id for n in e.store.nodes()}
        assert "文字から" in ex["requirement"]
