"""設計書からのルール抽出の結果を、ground_truth.json と照合して採点する。

python eval/spec_extraction/score.py <抽出結果.json> [--detail]

抽出結果の形式（prompt.md の出力形式）:
{
  "rules":     [{"statement": "...", "kind": "...", "sources": ["..."], ...}],
  "questions": [{"statement": "...", "sources": ["..."]}]
}

照合は語のゆるい一致（keywords の各グループのいずれかの語を含むか）なので、
最終判断は --detail の対応表を人が見て確認する。
"""
import argparse
import json
import os
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))


def norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").replace(" ", "")


def hits(statement: str, keywords) -> bool:
    text = norm(statement)
    return all(any(norm(k) in text for k in group) for group in keywords)


def match(truth_items, extracted):
    """正解ごとに、一致した抽出結果の番号を返す。"""
    found = {}
    for t in truth_items:
        idx = [i for i, e in enumerate(extracted) if hits(e.get("statement", ""), t["keywords"])]
        found[t["id"]] = idx
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("result")
    ap.add_argument("--truth", default=os.path.join(HERE, "ground_truth.json"))
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()

    truth = json.load(open(a.truth, encoding="utf-8"))
    res = json.load(open(a.result, encoding="utf-8"))
    rules = res.get("rules", [])
    questions = res.get("questions", [])

    r_found = match(truth["rules"], rules)
    q_found = match(truth["questions"], questions)
    n_found = match(truth["procedure_examples"]["items"], rules)

    r_ok = [k for k, v in r_found.items() if v]
    q_ok = [k for k, v in q_found.items() if v]
    noise = [k for k, v in n_found.items() if v]
    matched_idx = {i for v in r_found.values() for i in v}
    extra = [i for i in range(len(rules)) if i not in matched_idx]

    print(f"ルール再現率: {len(r_ok)}/{len(truth['rules'])}")
    by_pat = {}
    for t in truth["rules"]:
        p = t["pattern"]
        by_pat.setdefault(p, [0, 0])
        by_pat[p][1] += 1
        if r_found[t["id"]]:
            by_pat[p][0] += 1
    for p in sorted(by_pat):
        ok, n = by_pat[p]
        print(f"  {p} {truth['patterns'][p].split('：')[0]}: {ok}/{n}")
    print(f"質問の検出: {len(q_ok)}/{len(truth['questions'])}")
    print(f"手順の転記（ルールではないもの）: {len(noise)} 件")
    print(f"正解に対応しない抽出: {len(extra)} 件（妥当な追加ルールの可能性もあるので目視で確認）")

    if a.detail:
        print("\n--- 正解ごとの対応 ---")
        for t in truth["rules"]:
            idx = r_found[t["id"]]
            mark = "○" if idx else "×"
            print(f"{mark} {t['id']} {t['statement']}")
            for i in idx:
                print(f"     ← [{i}] {rules[i].get('statement')}")
        for t in truth["questions"]:
            mark = "○" if q_found[t["id"]] else "×"
            print(f"{mark} {t['id']}（{t['type']}）{t['statement']}")
        if noise:
            print("\n--- 手順の転記 ---")
            for k in noise:
                for i in n_found[k]:
                    print(f"  {k} ← [{i}] {rules[i].get('statement')}")
        if extra:
            print("\n--- 正解に対応しない抽出 ---")
            for i in extra:
                print(f"  [{i}] {rules[i].get('statement')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
