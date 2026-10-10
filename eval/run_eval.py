"""Evaluate the analyzer against a hand-made ground truth.

Phase 1 (always): raw tool output -> candidate recall, AI-verdict precision/recall.
Phase 2 (--decisions file): apply reviewer decisions, check completeness
conditions, approve, and score the confirmed set.

usage: python eval/run_eval.py --repo PATH [--docs PATH] [--decisions eval/decisions.json] [--dump DIR]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from codegraph.analysis.engine import Engine  # noqa: E402
from codegraph.pipeline.run import Pipeline  # noqa: E402
from codegraph.store import LocalStore  # noqa: E402


def covered(item, ids):
    return any(i in ids for i in item["any_of"])


def score(case, ids_flagged, ids_all):
    must, should = case["must"], case["should"]
    truth_ids = {i for it in must + should for i in it["any_of"]}
    must_hit = [it["label"] for it in must if covered(it, ids_flagged)]
    must_miss = [it["label"] for it in must if not covered(it, ids_flagged)]
    tp = [i for i in ids_flagged if i in truth_ids]
    precision = len(tp) / len(ids_flagged) if ids_flagged else 0.0
    return {
        "must_recall": round(len(must_hit) / len(must), 3),
        "must_missed": must_miss,
        "should_recall": round(sum(covered(it, ids_flagged) for it in should) / len(should), 3) if should else None,
        "precision": round(precision, 3),
        "flagged": len(ids_flagged),
        "candidates": len(ids_all),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--docs")
    ap.add_argument("--truth", default=os.path.join(os.path.dirname(__file__), "ground_truth.json"))
    ap.add_argument("--decisions")
    ap.add_argument("--dump")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "results.json"))
    args = ap.parse_args()

    data_dir = tempfile.mkdtemp(prefix="codegraph-eval-")
    store = LocalStore(os.path.join(data_dir, "graph.json"))
    t0 = time.time()
    stats = Pipeline(data_dir).index(args.repo, store, args.docs)
    eng = Engine(store, data_dir, args.repo, args.docs)
    truth = json.load(open(args.truth, encoding="utf-8"))
    decisions = json.load(open(args.decisions, encoding="utf-8")) if args.decisions else {}
    results = {"index": {k: stats[k] for k in ("nodes", "edges", "seconds")}, "llm": eng.llm.name, "cases": []}

    for case in truth["cases"]:
        t1 = time.time()
        rid = eng.start_run(case["requirement"], case["title"], background=False)
        st = eng.state(rid)
        elapsed = round(time.time() - t1, 2)
        cands = st["candidates"]
        all_ids = set(cands)
        impacted = {i for i, c in cands.items() if c["verdict"] == "impacted"}
        impacted_or_unc = {i for i, c in cands.items() if c["verdict"] in ("impacted", "uncertain")}
        row = {
            "id": case["id"], "title": case["title"], "run_id": rid, "seconds": elapsed,
            "intents": [{k: i[k] for k in ("summary", "change_type", "keywords", "new_terms", "pattern")} for i in st["intents"]],
            "candidate_set": score(case, all_ids, all_ids),
            "ai_impacted": score(case, impacted, all_ids),
            "ai_impacted_or_uncertain": score(case, impacted_or_unc, all_ids),
            "origins": {o: sum(1 for c in cands.values() if c["origin"] == o) for o in
                        sorted({c["origin"] for c in cands.values()})},
        }
        if args.dump:
            os.makedirs(args.dump, exist_ok=True)
            with open(os.path.join(args.dump, f"{case['id']}_candidates.tsv"), "w", encoding="utf-8") as f:
                for c in sorted(cands.values(), key=lambda c: -c["score"]):
                    f.write("\t".join([c["node_id"], c["kind"], c["origin"], str(c["score"]), str(c["verdict"]),
                                       (c["reason"] or "").replace("\t", " ")]) + "\n")

        # phase 2: reviewer decisions
        rules = decisions.get(case["id"])
        if rules:
            items = []
            for nid, c in cands.items():
                for rule in rules:
                    if fnmatch.fnmatchcase(nid, rule["match"]):
                        items.append({"node_id": nid, "status": rule["status"], "reason": rule["reason"]})
                        break
            eng.bulk_decide(rid, items, actor="agent:claude")
            st = eng.state(rid)
            undecided = [nid for nid, c in st["candidates"].items() if not c.get("decision")]
            # anything a rule did not cover stays open; report it
            row["review"] = {"decided": len(items), "undecided": undecided[:50], "undecided_count": len(undecided)}
            for add in [r for r in rules if r.get("add")]:
                eng.add_candidate(rid, add["add"], add["reason"], actor="agent:claude", expand=False)
            st = eng.state(rid)
            row["checks"] = [{"key": c["key"], "ok": c["ok"], "detail": c["detail"]} for c in st["checks"]]
            try:
                eng.approve(rid, actor="agent:claude")
                row["approved"] = True
            except ValueError as e:
                row["approved"] = False
                row["approve_error"] = str(e)
            confirmed = {i for i, c in st["candidates"].items() if c.get("decision") and c["decision"]["status"] == "confirmed"}
            row["final"] = score(case, confirmed, set(st["candidates"]))
            row["estimate_days"] = st["estimate"]["days"]
            row["estimate_range"] = [st["estimate"]["low"], st["estimate"]["high"]]
            row["report"] = eng.report(rid)
        results["cases"].append(row)

    results["total_seconds"] = round(time.time() - t0, 2)
    json.dump(results, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    for r in results["cases"]:
        print(f"== {r['id']} {r['title']}  ({r['seconds']}s)")
        print("  intents:", [(i["summary"][:20], i["change_type"], i["pattern"]) for i in r["intents"]])
        for k in ("candidate_set", "ai_impacted", "ai_impacted_or_uncertain", "final"):
            if k in r:
                v = r[k]
                print(f"  {k:26} must_recall={v['must_recall']:.2f} should_recall={v['should_recall']} "
                      f"precision={v['precision']:.2f} n={v['flagged']}/{v['candidates']} missed={v['must_missed']}")
        if "checks" in r:
            print("  checks:", [(c["key"], c["ok"]) for c in r["checks"]], "approved:", r["approved"],
                  "undecided:", r["review"]["undecided_count"], "estimate:", r["estimate_days"], r["estimate_range"])


if __name__ == "__main__":
    main()
