import json,os,sys,statistics,collections
sys.path.insert(0,'/home/claude/himotoki/eval/agent_compare')
from score import files_of, usage
tasks=json.load(open(sys.argv[1]))["tasks"]; dirs=sys.argv[2:]
rows=[]
for d in dirs:
    for t in tasks:
        for c in "AC":
            p=f"{d}/{t['id']}/result-{c}.json"
            if not os.path.exists(p): continue
            r=json.load(open(p))
            if r.get("is_error") or (r.get("structured_output") or {}).get("files") is None: continue
            got=files_of(r, f"{d}/{t['id']}/{c}/"); truth=set(t["truth"]); hit=[f for f in got if f in truth]
            rows.append(dict(run=d,task=t["id"],cond=c,truth=len(truth),listed=len(got),hit=len(hit),missed=sorted(truth-set(got)),
                             extra=[f for f in got if f not in truth],**usage(r)))
by=collections.defaultdict(list)
for x in rows: by[x["cond"]].append(x)
print("| 条件 | 回数 | 再現率（合計） | 適合率（合計） | 3回の再現率 | 費用/件 | 時間/件 |")
for c in "AC":
    rs=by[c]
    per=[]
    for d in dirs:
        q=[x for x in rs if x["run"]==d]; per.append(sum(x["hit"] for x in q)/max(1,sum(x["truth"] for x in q)))
    h=sum(x["hit"] for x in rs); tr=sum(x["truth"] for x in rs); l=sum(x["listed"] for x in rs)
    print(f"| {c} | {len(rs)} | {h/tr:.1%} ({h}/{tr}) | {h/max(1,l):.1%} | {' / '.join(f'{p:.0%}' for p in per)} | ${statistics.mean(x['cost'] for x in rs):.2f} | {statistics.mean(x['seconds'] for x in rs):.0f}s |")
print()
print("| 課題 | 正解 | A 再現率(3回) | C 再現率(3回) | A 適合率 | C 適合率 |")
for t in tasks:
    def s(c,k):
        q=[x for x in rows if x["task"]==t["id"] and x["cond"]==c]
        if k=="r": return " ".join(f"{x['hit']}" for x in q)
        h=sum(x['hit'] for x in q); l=sum(x['listed'] for x in q); return f"{h/max(1,l):.0%}"
    print(f"| {t['id']} | {len(t['truth'])} | {s('A','r')} | {s('C','r')} | {s('A','p')} | {s('C','p')} |")
print()
for c in "AC":
    m=collections.Counter(f"{x['task']} {f}" for x in by[c] for f in x["missed"])
    print(c,"見落とし（回数）:"); [print("  ",n,k) for k,n in m.most_common(25)]
json.dump(rows,open(os.path.dirname(dirs[0])+"/rows-"+os.path.basename(sys.argv[1])+".json","w"),ensure_ascii=False,indent=0)
