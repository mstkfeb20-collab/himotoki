"""Ingestion recall: how many of the real relations between assets the graph holds.

    python score.py <repo> <relations.jsonl> [--out result.json]

relations.jsonl comes from whoever built the repository (not from himotoki); every line
names two assets (`from`, `to`: file + element) and a relation type. An asset maps to
the graph nodes that stand for it (an element-less file: every node of that file; a
class: the class and its members). A relation counts as

* direct: an edge (any type, either direction) joins the two assets;
* near:   the two are connected within 3 steps (either direction), not counting the
          files' own CONTAINS fan-out as a step, through nodes with fewer than 60 edges.

Unmapped assets (nothing in the graph stands for them) are reported separately: that
is a reading gap of its own.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import tempfile
from collections import deque

_bi = sys.argv.index("--backend") if "--backend" in sys.argv else -1
sys.path.insert(0, sys.argv[_bi + 1] if _bi > 0 else os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
from himotoki.pipeline.run import Pipeline  # noqa: E402
from himotoki.store.local import LocalStore  # noqa: E402


def datafile_keys(v: str) -> set:
    """Data-file node ids under the current and the earlier naming of himotoki."""
    old = re.sub(r"\$\{?\w+\}?", "", v).rsplit("/", 1)[-1].lower()
    new = re.sub(r"\$\{?\w+\}?|%\w+%|%[\d$\-]*[sd]|\{\d+\}", "", v.strip()).rsplit("/", 1)[-1].lower()
    new = re.sub(r"(yyyy|yy)(mm)?(dd)?|\d{6,14}|\*", "", new)
    new = re.sub(r"[_\-.]{2,}", lambda m: m.group(0)[-1], new)
    new = re.sub(r"[_\-]+\.", ".", new).strip("_-")
    return {f"datafile:{old}", f"datafile:{new}"}


class Graph:
    def __init__(self, store):
        self.st = store
        self.nodes = {n.id: n for n in store.nodes()}
        self.by_file = collections.defaultdict(set)
        for n in self.nodes.values():
            if n.file:
                self.by_file[n.file].add(n.id)
        self.adj = collections.defaultdict(set)
        self.contains = collections.defaultdict(set)
        for n in self.nodes.values():
            for e in store.out_edges(n.id):
                if e.dst not in self.nodes:
                    continue
                self.adj[e.src].add(e.dst)
                self.adj[e.dst].add(e.src)
                if e.rel == "CONTAINS":
                    self.contains[e.src].add(e.dst)

    def members(self, ids):
        out, q = set(ids), deque(ids)
        while q:
            x = q.popleft()
            for c in self.contains.get(x, ()):
                if c not in out:
                    out.add(c)
                    q.append(c)
        return out

    def resolve(self, a: dict) -> set:
        f, el = a.get("file"), a.get("element")
        N = self.nodes
        hits = set()
        if el:
            ns, _, v = el.partition(":") if re.match(r"^(datafile|action|formbean|jsp|validation|layout|report|doc|config|msg|func|cron):", el) else ("", "", el)
            if ns == "datafile":
                hits |= datafile_keys(v) & N.keys()
            elif ns == "action":
                p = v.split("?")[0]
                hits |= {i for i in (f"ep:ANY {p}.do", f"ep:ANY {p}", f"ep:ANY {re.sub(r'.do$', '', p)}.do") if i in N}
            elif ns == "formbean":
                form, _, prop = v.partition(".")
                if not prop:
                    hits |= {f"formbean:{form}"} & N.keys()
                else:
                    hits |= {f"formprop:{form}.{prop}"} & N.keys()
                    for e in self.st.out_edges(f"formbean:{form}") if f"formbean:{form}" in N else []:
                        if e.rel == "CONFIGURES":
                            hits |= {f"{e.dst}.{prop}"} & N.keys()
            elif ns == "validation":
                hits |= {f"valid:{v}"} & N.keys()
            elif ns == "layout" and f:
                hits |= {f"layout:{f}#{v}"} & N.keys()
            elif ns == "config":
                hits |= {f"config:{v}"} & N.keys()
            elif ns == "msg":
                hits |= {f"msg:{v}"} & N.keys()
            elif ns == "cron" and f:
                hits |= {f"cron:{f}:{v}"} & N.keys()
            elif ns == "report" and f:
                hits |= {i for i in self.by_file.get(f, ()) if i.startswith("xml:") and N[i].name.endswith(" " + v)}
            elif ns in ("jsp", "doc", "func"):
                pass                                            # file level below
            elif el.startswith("/"):                            # JP1 unit path
                hits |= {f"job:{el}"} & N.keys()
            elif re.fullmatch(r"[A-Z][A-Z0-9_$#]*\.[A-Z0-9_$#]+", el):
                hits |= {f"col:{el.lower()}"} & N.keys()
            elif re.fullmatch(r"[A-Z][A-Z0-9_$#]*", el):
                hits |= {f"table:{el.lower()}"} & N.keys()
            elif "#" in el:
                cls, _, m = el.partition("#")
                hits |= {i for i in N if i.startswith(f"java:{cls}#{m}(")}
            elif re.fullmatch(r"(?:[a-z_]\w*\.)+[A-Z]\w*", el):
                hits |= {f"java:{el}"} & N.keys()
            elif re.fullmatch(r"(?:[a-z_]\w*\.)+[A-Z]\w*\.\w+", el):
                hits |= {f"java:{el}"} & N.keys()
        if not hits and f:
            hits = set(self.by_file.get(f, ())) | ({f"file:{f}"} & N.keys())
        return self.members(hits)

    def direct(self, A, B):
        return any(b in self.adj.get(a, ()) for a in A for b in B) or bool(A & B)

    def near(self, A, B, hops=3, hub=60):
        if A & B:
            return True
        seen, frontier = set(A), set(A)
        for _ in range(hops):
            nxt = set()
            for x in frontier:
                if x not in A and len(self.adj.get(x, ())) >= hub:
                    continue
                for y in self.adj.get(x, ()):
                    if y in B:
                        return True
                    if y not in seen:
                        nxt.add(y)
            nxt = (nxt | self.members(nxt)) - seen
            if nxt & B:
                return True
            seen |= nxt
            frontier = nxt
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("truth")
    ap.add_argument("--out")
    ap.add_argument("--backend", help="backend directory of the himotoki version to measure")
    a = ap.parse_args()
    d = tempfile.mkdtemp()
    st = LocalStore(os.path.join(d, "graph.json"))
    Pipeline(d).index(a.repo, st)
    g = Graph(st)
    rows = [json.loads(l) for l in open(a.truth, encoding="utf-8") if l.strip()]
    per = collections.defaultdict(lambda: collections.Counter())
    misses = collections.defaultdict(list)
    for r in rows:
        A, B = g.resolve(r["from"]), g.resolve(r["to"])
        c = per[r["type"]]
        c["total"] += 1
        if not A or not B:
            c["unmapped"] += 1
            misses[r["type"]].append({"why": "unmapped " + ("from" if not A else "to"), **r})
            continue
        if g.direct(A, B):
            c["direct"] += 1
            c["near"] += 1
        elif g.near(A, B):
            c["near"] += 1
            misses[r["type"]].append({"why": "near only", **r})
        else:
            misses[r["type"]].append({"why": "missing", **r})
    tot = collections.Counter()
    table = []
    for t, c in sorted(per.items()):
        tot.update(c)
        table.append({"type": t, **c, "direct_recall": round(c["direct"] / c["total"], 3),
                      "near_recall": round(c["near"] / c["total"], 3)})
    summary = {"total": tot["total"], "direct": tot["direct"], "near": tot["near"], "unmapped": tot["unmapped"],
               "direct_recall": round(tot["direct"] / max(1, tot["total"]), 3),
               "near_recall": round(tot["near"] / max(1, tot["total"]), 3),
               "nodes": len(g.nodes)}
    out = {"summary": summary, "by_type": table, "misses": misses}
    if a.out:
        json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    for t in table:
        print(f"{t['type']:28} {t['total']:4}  direct {t['direct_recall']:.2f}  near {t['near_recall']:.2f}  unmapped {t.get('unmapped', 0)}")


if __name__ == "__main__":
    main()
