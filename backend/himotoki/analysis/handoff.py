"""Hand-off to development and verification against the actual change.

1. `build_handoff`: the reviewed scope of a run, per file, as the work order a
   developer or a coding agent (Claude Code over MCP) implements: what to change
   and why, the human decisions and comments, transaction / integration risks,
   design documents to update, tests to run, callers to re-check, what was
   excluded (do not touch) and what is still undecided.
2. `verify_change`: after implementation, the changed lines between two refs are
   mapped to graph elements and compared with the prediction: hits, changes the
   analysis missed (split into "was a candidate but judged otherwise" and "not
   found at all" — a review problem vs a graph gap), and predicted elements that
   were not touched. With the actual person-days, the estimator is recalibrated.
"""
from __future__ import annotations

import time

from ..model import EDITABLE, Kind, Rel

TEST_KINDS = {Kind.TEST_CLASS, Kind.TEST_METHOD}
DOC_KINDS = {Kind.DOC, Kind.DOC_ENTITY}
CHANGE_JA = {"add": "追加", "modify": "変更", "delete": "削除", "test": "テスト", "doc": "設計書"}
AGENT_RULES = [
    "この作業指示の「修正対象」の範囲で実装する。範囲外のファイルを直す必要が出たら、理由を書いて人に確認する（勝手に広げない）。",
    "「除外」に挙がった要素は変更しない。除外の理由を読んでから判断する。",
    "「未判定」の要素は、実装中に影響の有無を確かめ、結果を報告する。",
    "「トランザクション・連携の注意」がある要素は、ロールバック時の扱い・相手システム・再実行を確認してから変更する。",
    "「関連する設計書」は、コードと合わせて更新が必要か確認する。",
    "「実行するテスト」を実行し、無いものは追加する。",
    "完了したら、実際の差分を himotoki の verify_change で予測と突き合わせる。",
]


def _decision(c):
    return (c.get("decision") or {}).get("status")


def _item(engine, st, c, confirmed_ids, excluded_ids):
    store = engine.store
    nid = c["node_id"]
    node = store.get(nid)
    ins = store.in_edges(nid) if node else []
    tests, docs, callers, defined_in = [], [], [], []
    for e in ins:
        o = store.get(e.src)
        if o is None:
            continue
        if e.rel == Rel.DEFINES and o.file:
            defined_in.append({"name": o.name, "file": o.file.replace("design:", "docs/", 1), "line": e.props.get("line")})
        elif o.kind in TEST_KINDS or e.rel == Rel.TESTS:
            tests.append({"name": o.name, "file": o.file, "line": o.line})
        elif o.kind in DOC_KINDS and e.rel in (Rel.DESCRIBES, Rel.DOC_CRUD):
            docs.append({"name": o.name, "file": (o.file or "").replace("design:", "docs/", 1), "line": o.line})
        elif e.rel in (Rel.CALLS, Rel.REFERENCES) and o.id not in confirmed_ids and o.id not in excluded_ids \
                and o.kind not in TEST_KINDS:
            callers.append({"node_id": o.id, "name": o.name, "file": o.file, "line": e.props.get("line") or o.line})
    reason = (c.get("decision") or {}).get("reason") or c.get("reason") or c.get("seed_reason") or ""
    return {
        "node_id": nid, "name": c["name"], "kind": c["kind"], "file": c.get("file"),
        "line": c.get("line"), "end_line": node.end_line if node else None,
        "change_type": c.get("change_type") or "modify", "reason": reason,
        "decided_by": (c.get("decision") or {}).get("actor"), "assignee": c.get("assignee"),
        "comments": [{"author": x["author"], "text": x["text"]} for x in c.get("comments", [])],
        "tx_risks": [{k: r.get(k) for k in ("code", "level", "message")} for r in c.get("tx_risks", [])],
        "tx": c.get("tx"), "data_flow": bool(c.get("data_flow")),
        "tests": tests[:8], "docs": docs[:8], "callers_to_check": callers[:5], "defined_in": defined_in[:4],
        "confidence": round(c.get("confidence") or c["score"], 2),
    }


def build_handoff(engine, run_id: str, include_pending: bool = True) -> dict:
    st = engine.state(run_id)
    if st is None:
        raise KeyError(run_id)
    cands = st["candidates"]
    confirmed = [c for c in cands.values() if _decision(c) == "confirmed"]
    excluded = [c for c in cands.values() if _decision(c) == "excluded"]
    pending = [c for c in cands.values() if not c.get("decision") and c.get("verdict") in ("impacted", "uncertain")]
    cids, xids = {c["node_id"] for c in confirmed}, {c["node_id"] for c in excluded}
    items = [_item(engine, st, c, cids, xids) for c in confirmed]
    files: dict[str, list] = {}
    other = []
    for it in sorted(items, key=lambda i: (i["file"] or "~", i["line"] or 0)):
        if it["file"] and it["kind"] in EDITABLE:
            files.setdefault(it["file"].replace("design:", "docs/", 1), []).append(it)
        else:
            other.append(it)
    tests = {}
    for it in items:
        for t in it["tests"]:
            tests[(t["name"], t["file"])] = t
    warnings = []
    if not st.get("approved"):
        warnings.append("この影響範囲は未承認です（全数判定の条件が未達、または承認操作がされていない）。")
    if pending and include_pending:
        warnings.append(f"未判定の候補が {len(pending)} 件あります。実装中に影響の有無を確認してください。")
    if not confirmed:
        warnings.append("確定した修正対象がありません。ビュワーで候補を確定してから引き渡してください。")
    e = st.get("estimate") or {}
    return {
        "run_id": run_id, "title": st.get("title"), "requirement": st["requirement"],
        "intents": [{"id": i["id"], "summary": i["summary"], "change_type": i.get("change_type")}
                    for i in st.get("intents", [])],
        "approved": st.get("approved"), "warnings": warnings,
        "estimate_days": e.get("days"), "estimate_range": [e.get("low"), e.get("high")],
        "files": [{"file": f, "items": its} for f, its in files.items()],
        "other_targets": other,
        "pending": [{"node_id": c["node_id"], "name": c["name"], "kind": c["kind"], "file": c.get("file"),
                     "line": c.get("line"), "verdict": c.get("verdict"), "reason": c.get("reason") or c.get("seed_reason"),
                     "tx_risks": [r.get("code") for r in c.get("tx_risks", [])]}
                    for c in sorted(pending, key=lambda c: -(c.get("confidence") or c["score"]))[:60]]
        if include_pending else [],
        "excluded": [{"node_id": c["node_id"], "name": c["name"], "file": c.get("file"),
                      "reason": (c.get("decision") or {}).get("reason") or ""} for c in excluded],
        "tests_to_run": list(tests.values()),
        "regression_scope": engine.regression_scope(st),
        "rules_for_agent": AGENT_RULES,
        "verification": st.get("verification"),
    }


def _loc(x):
    if not x.get("file"):
        return ""
    return f"`{x['file']}" + (f":{x['line']}" if x.get("line") else "") + "`"


def handoff_markdown(h: dict) -> str:
    out = [f"# 作業指示: {h['title'] or h['requirement'][:40]}", "",
           f"- 要求: {h['requirement']}", f"- run: `{h['run_id']}`（himotoki）",
           f"- 承認: {h['approved']['actor'] + ' が承認済み' if h.get('approved') else '未承認'}",
           f"- 規模の見込み: {h.get('estimate_days')} 人日（{h['estimate_range'][0]}〜{h['estimate_range'][1]}）", ""]
    if h["warnings"]:
        out += ["> " + w for w in h["warnings"]] + [""]
    if h["intents"]:
        out += ["## 変更意図", ""] + [f"- {i['id']} {i['summary']}（{CHANGE_JA.get(i['change_type'], i['change_type'])}）"
                                       for i in h["intents"]] + [""]
    out += ["## 修正対象（ファイルごと）", ""]
    if not h["files"]:
        out += ["（確定した修正対象はありません）", ""]
    for f in h["files"]:
        out += [f"### `{f['file']}`", ""]
        for it in f["items"]:
            rng = f"{it['line']}–{it['end_line']}行" if it.get("line") and it.get("end_line") else \
                (f"{it['line']}行" if it.get("line") else "")
            out.append(f"- **{it['name']}**（{it['kind']}・{CHANGE_JA.get(it['change_type'], it['change_type'])}"
                       f"{'・' + rng if rng else ''}）: {it['reason']}")
            for cm in it["comments"]:
                out.append(f"  - コメント（{cm['author']}）: {cm['text']}")
            for r in it["tx_risks"]:
                out.append(f"  - ⚠ {r['message']}")
            if it["docs"]:
                out.append("  - 関連する設計書: " + "、".join(f"{d['name']}" for d in it["docs"][:4]))
            if it["callers_to_check"]:
                out.append("  - 呼び出し元も確認: " + "、".join(f"{x['name']} {_loc(x)}" for x in it["callers_to_check"]))
        out.append("")
    if h["other_targets"]:
        out += ["## DB・連携ファイル・ジョブなど", ""]
        for it in h["other_targets"]:
            where = _loc(it) or "、".join(_loc(d) for d in it.get("defined_in", [])) or ""
            out.append(f"- **{it['name']}**（{it['kind']}・{CHANGE_JA.get(it['change_type'], it['change_type'])}）"
                       f"{' 定義: ' + where if where else ''}: {it['reason']}")
            for r in it["tx_risks"]:
                out.append(f"  - ⚠ {r['message']}")
        out.append("")
    if h["pending"]:
        out += ["## 未判定（実装中に影響の有無を確認）", ""]
        out += [f"- {p['name']}（{p['kind']}）{_loc(p)}: {p.get('reason') or ''}" for p in h["pending"][:30]] + [""]
    if h["excluded"]:
        out += ["## 除外（変更しない）", ""]
        out += [f"- {x['name']} {_loc(x)}: {x['reason'] or '理由なし'}" for x in h["excluded"]] + [""]
    if h["tests_to_run"]:
        out += ["## 実行するテスト", ""] + [f"- {t['name']} {_loc(t)}" for t in h["tests_to_run"]] + [""]
    rs = h.get("regression_scope") or {}
    labels = {"entry": "入口（API・画面遷移）", "screen": "画面", "batch": "バッチ・ジョブ", "data": "データ"}
    rows = [(labels[k], v) for k, v in rs.items() if k in labels and v]
    if rows:
        out += ["## 回帰確認の範囲", ""] + [f"- {lab}: " + "、".join(x["name"] for x in v[:12]) for lab, v in rows] + [""]
    out += ["## 進め方（エージェント・開発者向け）", ""] + [f"{i}. {r}" for i, r in enumerate(h["rules_for_agent"], 1)]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- verification
def _family(store, nid: str) -> set[str]:
    """The element, what contains it and what it contains (a changed method inside a
    predicted class, or a predicted field inside a changed class, count as a hit)."""
    fam = {nid}
    for e in store.in_edges(nid, [Rel.CONTAINS]):
        fam.add(e.src)
    for e in store.out_edges(nid, [Rel.CONTAINS]):
        fam.add(e.dst)
    return fam


def verify_change(engine, run_id: str, hunks: dict, base: str = "", head: str = "",
                  actual_days: float | None = None) -> dict:
    st = engine.state(run_id)
    if st is None:
        raise KeyError(run_id)
    changed, added = engine.diff_nodes(hunks)
    cands = st["candidates"]
    predicted = {nid for nid, c in cands.items() if _decision(c) == "confirmed" and c["kind"] in EDITABLE}
    hit, missed = [], []
    touched_pred = set()
    for ch in changed:
        nid = ch["node_id"]
        n = engine.store.get(nid)
        fam = _family(engine.store, nid)
        match = fam & predicted
        row = {"node_id": nid, "name": n.name if n else nid, "kind": n.kind if n else "", "file": n.file if n else None,
               "change": ch["reason"]}
        if match:
            touched_pred |= match
            hit.append(row)
            continue
        c = next((cands[x] for x in fam if x in cands), None)
        if c is None:
            row["why"] = "not_found"          # the graph never reached it: a gap to fix with rules / data
            row["why_ja"] = "候補に無かった（グラフで届いていない）"
        else:
            status = _decision(c) or c.get("verdict") or "undecided"
            row["why"] = f"candidate_{status}"
            row["why_ja"] = {"excluded": "候補にあったが除外していた", "impacted": "候補にあったが未確定",
                             "uncertain": "候補にあったが未確定（要確認）",
                             "not_impacted": "候補にあったが影響低と判定"}.get(status, "候補にあったが未確定")
            row["candidate_reason"] = (c.get("decision") or {}).get("reason") or c.get("reason")
        missed.append(row)
    untouched = [{"node_id": nid, "name": cands[nid]["name"], "kind": cands[nid]["kind"], "file": cands[nid].get("file")}
                 for nid in sorted(predicted - touched_pred)
                 if cands[nid]["kind"] not in DOC_KINDS]           # design documents are not in the code diff
    n_changed = len(hit) + len(missed)
    pred_code = len([p for p in predicted if cands[p]["kind"] not in DOC_KINDS])
    res = {
        "base": base, "head": head, "at": time.time(),
        "changed_elements": n_changed, "added_files": added,
        "hit": hit, "missed": missed, "untouched": untouched,
        "recall": round(len(hit) / n_changed, 3) if n_changed else None,
        "precision": round((pred_code - len(untouched)) / pred_code, 3) if pred_code else None,
        "missed_by_reason": {k: sum(1 for m in missed if m["why"] == k) for k in {m["why"] for m in missed}},
    }
    if actual_days is not None and (st.get("estimate") or {}).get("days"):
        hist = engine.estimator.coef.setdefault("history", [])
        hist.append({"ref": run_id, "predicted_days": st["estimate"]["days"], "actual_days": float(actual_days)})
        res["calibration"] = engine.estimator.calibrate(hist)
        res["predicted_days"], res["actual_days"] = st["estimate"]["days"], float(actual_days)
    with engine._locks[run_id]:
        st["verification"] = res
        engine._save(st)
        engine.emit(st, "verification", {k: res[k] for k in ("recall", "precision", "changed_elements")}, pace=False)
    return res


def verification_markdown(v: dict) -> str:
    out = [f"# 予測と実績の突き合わせ（{v.get('base')}..{v.get('head')}）", "",
           f"- 変更された要素: {v['changed_elements']}（追加ファイル {len(v['added_files'])}）",
           f"- 予測が当たった割合（再現率）: {v['recall']}", f"- 予測のうち実際に変更された割合（適合率）: {v['precision']}", ""]
    if v["missed"]:
        out += ["## 予測漏れ", ""] + [f"- {m['name']}（{m['kind']}）`{m.get('file') or ''}`: {m['why_ja']}"
                                     for m in v["missed"]] + [""]
    if v["untouched"]:
        out += ["## 予測したが変更されなかった", ""] + [f"- {u['name']}（{u['kind']}）" for u in v["untouched"]] + [""]
    if v.get("calibration"):
        out += [f"規模: 見込み {v['predicted_days']} 人日 / 実績 {v['actual_days']} 人日 → 補正係数 "
                f"{v['calibration']['calibration']}（{v['calibration']['samples']} 件）"]
    return "\n".join(out) + "\n"
