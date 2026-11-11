"""Score the runs against the files each commit actually changed.

  python score.py --tasks tasks.json --out ~/compare
"""
import argparse
import json
import os
import statistics


def files_of(res: dict, root: str) -> list[str]:
    so = res.get("structured_output")
    if so is None:                                   # older versions: JSON text in `result`
        try:
            so = json.loads(res.get("result") or "{}")
        except ValueError:
            so = {}
    out = []
    for f in (so or {}).get("files", []):
        p = (f.get("path") or "").strip()
        if p.startswith(root):
            p = p[len(root):]
        p = p.removeprefix("./").lstrip("/")
        if p and p not in out:
            out.append(p)
    return out


def usage(res: dict) -> dict:
    u = res.get("usage") or {}
    return {"input": u.get("input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0),
            "cost": res.get("total_cost_usd") or 0.0, "turns": res.get("num_turns") or 0,
            "tools": res.get("tool_calls") or {},
            "seconds": round((res.get("duration_ms") or 0) / 1000)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    tasks = json.load(open(a.tasks, encoding="utf-8"))["tasks"]
    out = os.path.expanduser(a.out)
    rows = []
    conds = sorted({f[7:-5] for t in tasks for f in (os.listdir(os.path.join(out, t["id"]))
                                                     if os.path.isdir(os.path.join(out, t["id"])) else [])
                    if f.startswith("result-") and f.endswith(".json") and ".prev" not in f})
    for t in tasks:
        for c in conds:
            p = os.path.join(out, t["id"], f"result-{c}.json")
            if not os.path.exists(p) or not os.path.getsize(p):
                continue
            res = json.load(open(p, encoding="utf-8"))
            got = files_of(res, os.path.join(out, t["id"], c) + "/")
            truth = set(t["truth"])
            hit = [f for f in got if f in truth]
            rows.append({"task": t["id"], "cond": c, "truth": len(truth), "listed": len(got), "hit": len(hit),
                         "recall": len(hit) / len(truth) if truth else 0.0,
                         "precision": len(hit) / len(got) if got else 0.0,
                         "missed": sorted(truth - set(got)), "error": res.get("is_error") or res.get("subtype") not in (None, "success"),
                         **usage(res)})
    def tools_short(t):
        if not t:
            return "—"
        mcp = sum(v for k, v in t.items() if k.startswith("mcp__"))
        sub = sum(v for k, v in t.items() if k in ("Task", "Agent"))
        search = sum(v for k, v in t.items() if k in ("Grep", "Glob") or k == "Bash")
        read = t.get("Read", 0)
        return f"himotoki {mcp}・検索 {search}・読込 {read}" + (f"・サブエージェント {sub}" if sub else "")

    print("| task | 条件 | 正解 | 挙げた | 当たり | 再現率 | 適合率 | 入力 | キャッシュ読 | 出力 | 費用$ | ターン | 秒 | 道具 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['task']} | {r['cond']} | {r['truth']} | {r['listed']} | {r['hit']} | {r['recall']:.0%} | {r['precision']:.0%} "
              f"| {r['input'] + r['cache_write']:,} | {r['cache_read']:,} | {r['output']:,} | {r['cost']:.2f} | {r['turns']} | {r['seconds']} "
              f"| {tools_short(r['tools'])} |"
              + (" ⚠" if r["error"] else ""))
    print()
    labels = {"A": "Claude Code 単体", "B": "himotoki（impact_files）", "C": "himotoki（起点＋答え合わせ）"}
    for c, label in ((c, labels.get(c, c)) for c in conds):
        rs = [r for r in rows if r["cond"] == c]
        if not rs:
            continue
        tot = sum(r["truth"] for r in rs)
        print(f"{label}: 再現率 {sum(r['hit'] for r in rs) / max(1, tot):.0%}（{sum(r['hit'] for r in rs)}/{tot}）"
              f"・適合率 {sum(r['hit'] for r in rs) / max(1, sum(r['listed'] for r in rs)):.0%}"
              f"・費用 中央値 ${statistics.median(r['cost'] for r in rs):.2f}／合計 ${sum(r['cost'] for r in rs):.2f}"
              f"・トークン合計（入力+キャッシュ）中央値 {statistics.median(r['input'] + r['cache_write'] + r['cache_read'] for r in rs):,.0f}"
              f"・ターン 中央値 {statistics.median(r['turns'] for r in rs)}"
              f"・時間 中央値 {statistics.median(r['seconds'] for r in rs)} 秒")
    json.dump(rows, open(os.path.join(out, "score.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
