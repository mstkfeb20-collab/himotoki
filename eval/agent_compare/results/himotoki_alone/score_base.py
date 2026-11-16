import json, collections, statistics
def files(r, verdicts=None):
    out = []
    for c in r["candidates"]:
        f = c.get("file")
        if not f or f.startswith("design:"): continue
        if verdicts and c.get("verdict") not in verdicts: continue
        if f not in out: out.append(f)
    return out
agent = {}
for b, tf in (("l", "tasks_sor_l.json"), ("g", "tasks_sor_g.json")):
    for r in json.load(open(f"/home/claude/compare/rows-{tf}.json")):
        agent.setdefault((b, r["task"], r["cond"]), []).append(r["listed"])
tot = collections.Counter(); det = []
for b, tf in (("l", "tasks_sor_l.json"), ("g", "tasks_sor_g.json")):
    tasks = json.load(open(f"/home/claude/himotoki/eval/agent_compare/{tf}"))["tasks"]
    for t in tasks:
        T = set(t["truth"]); r1 = json.load(open(f"{b}-{t['id']}-1.json")); r2 = json.load(open(f"{b}-{t['id']}-2.json"))
        tot[b, "truth"] += len(T)
        for name, fs in (("impacted", files(r1, {"impacted"})), ("impacted+uncertain", files(r1, {"impacted", "uncertain"})),
                         ("all", files(r1))):
            tot[b, name, "listed"] += len(fs); tot[b, name, "hit"] += len(T & set(fs))
        for cond in "AC":
            n = round(statistics.mean(agent[(b, t["id"], cond)]))
            fs = files(r1)[:n]
            tot[b, f"top=same as {cond}", "listed"] += len(fs); tot[b, f"top=same as {cond}", "hit"] += len(T & set(fs))
        a1, a2 = files(r1, {"impacted", "uncertain"}), files(r2, {"impacted", "uncertain"})
        det.append(len(set(a1) & set(a2)) / max(1, len(set(a1) | set(a2))))
for b in "lg":
    print("==", {"l": "大規模", "g": "保険"}[b], "正解", tot[b, "truth"])
    for name in ("impacted", "impacted+uncertain", "all", "top=same as A", "top=same as C"):
        l, h = tot[b, name, "listed"], tot[b, name, "hit"]
        print(f"  {name:20} 挙げた {l:5} 当たり {h:4} 再現率 {h/tot[b,'truth']:.0%} 適合率 {h/max(1,l):.0%}")
print("2回の一致度(影響あり+不明のファイル)", f"{statistics.mean(det):.1%}", "最小", f"{min(det):.0%}")
