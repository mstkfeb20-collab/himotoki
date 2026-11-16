import json, random, collections, os, tempfile, sys
from himotoki.pipeline.run import Pipeline
from himotoki.store.local import LocalStore
random.seed(23)
WANT = {("DESCRIBES", "office-table"), ("MAPS_TO", "resultmap"), ("MAPS_TO", "layout"), ("REFERENCES", "xml-generic"),
        ("RUNS", "shell"), ("INCLUDES", "shell"), ("POPULATES", "dml"), ("WRITES", "dml"), ("TESTS", "naming"),
        ("DESCRIBES", "mention"), ("CALLS", "parser"), ("REFERENCES", "parser")}
out = []; sizes = {}
for b, root in (("g", "/home/claude/work/sor-bench-g-cr"), ("l", "/home/claude/work/judge-l")):
    d = tempfile.mkdtemp(); st = LocalStore(os.path.join(d, "g.json")); Pipeline(d).index(root, st)
    nodes = {n.id: n for n in st.nodes()}
    groups = collections.defaultdict(list)
    for n in nodes.values():
        for e in st.out_edges(n.id):
            if e.dst in nodes:
                groups[(e.rel, e.props.get("via") or e.props.get("src") or "-")].append(e)
    for k, es in groups.items():
        sizes[f"{b} {k[0]} {k[1]}"] = len(es)
    for k in sorted(WANT):
        es = groups.get(k, [])
        for e in random.sample(es, min(8, len(es))):
            s, t = nodes[e.src], nodes[e.dst]
            desc = lambda n: {"id": n.id, "kind": n.kind, "name": n.name, "file": n.file, "line": n.line}
            out.append({"bench": b, "root": root, "rel": e.rel, "via": k[1], "group_size": len(es), "from": desc(s), "to": desc(t),
                        "edge_line": e.props.get("line"), "props": {x: v for x, v in e.props.items() if x in ("usage", "length", "ops")}})
for i, x in enumerate(out):
    x["n"] = 1000 + i
json.dump(out, open("sample2.json", "w"), ensure_ascii=False, indent=1)
json.dump(sizes, open("sizes2.json", "w"), ensure_ascii=False, indent=1)
part = [{k: v for k, v in x.items() if k not in ("via", "group_size")} for x in out]
json.dump(part, open("part5.json", "w"), ensure_ascii=False, indent=1)
print(len(out))
