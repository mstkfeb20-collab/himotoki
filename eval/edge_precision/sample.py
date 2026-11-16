import json, random, collections, sys
from himotoki.store.local import LocalStore
random.seed(17)
out = []
for b, p, root in (("g", "/home/claude/compare/g-r1/cr01/ws/graphs/repo/graph.json", "/home/claude/work/sor-bench-g-cr"),
                   ("l", "/home/claude/compare/l-r1/cr01/ws/graphs/repo/graph.json", "/home/claude/work/judge-l")):
    st = LocalStore(p)
    nodes = {n.id: n for n in st.nodes()}
    groups = collections.defaultdict(list)
    for n in nodes.values():
        for e in st.out_edges(n.id):
            if e.rel == "CONTAINS" or e.dst not in nodes:
                continue
            groups[(e.rel, e.props.get("via") or e.props.get("src") or "-")].append(e)
    per_rel = collections.Counter()
    for (rel, via), es in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        k = min(8, len(es), 20 - per_rel[rel])
        if k <= 0:
            continue
        per_rel[rel] += k
        for e in random.sample(es, k):
            s, d = nodes[e.src], nodes[e.dst]
            desc = lambda n: {"id": n.id, "kind": n.kind, "name": n.name, "file": n.file, "line": n.line}
            out.append({"bench": b, "root": root, "rel": rel, "via": via, "group_size": len(es),
                        "from": desc(s), "to": desc(d), "edge_line": e.props.get("line"),
                        "props": {k2: v for k2, v in e.props.items() if k2 in ("usage", "path", "length", "maxlength", "transfer", "ops")}})
for i, x in enumerate(out):
    x["n"] = i + 1
json.dump(out, open("/home/claude/work/judge/sample.json", "w"), ensure_ascii=False, indent=1)
c = collections.Counter((x["bench"], x["rel"]) for x in out)
print(len(out)); print(sorted(c.items()))
