"""Ingestion recall: how many of the real relations between assets the graph holds.

    python score.py <repo> <relations.jsonl> [--out result.json]

relations.jsonl comes from whoever built the repository (not from himotoki); every line
names two assets (`from`, `to`: file + element) and a relation type. An asset maps to
the graph nodes that stand for it (an element-less file: every node of that file; a
class: the class and its members). A relation counts as

* direct: an edge (any type, either direction) joins the two assets (a method includes
          the SQL statements it runs);
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
                if e.rel in ("CONTAINS", "EXECUTES"):
                    # the SQL a method runs is part of that method, as a reader would see it
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
            ns, _, v = el.partition(":") if re.match(r"^(datafile|action|formbean|jsp|validation|layout|report|doc|config|msg|func|cron|param):", el) else ("", "", el)
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
                    prop = re.sub(r"\[\d*\]", "", prop)
                    hits |= {f"formprop:{form}.{prop}"} & N.keys()
                    for e in self.st.out_edges(f"formbean:{form}") if f"formbean:{form}" in N else []:
                        if e.rel == "CONFIGURES":
                            hits |= self.field_path(e.dst, prop.split("."))
            elif ns == "param":
                hits |= {f"reqparam:{v}"} & N.keys()
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

    def length_known(self, A, B, n: str) -> bool:
        """The graph records the stated length n at the stating element, on its link to
        the column, or on a screen / check bound to it."""
        want = re.match(r"\d+", n or "")
        if not want:
            return False
        want = want.group(0)

        def ok(v):
            m = re.match(r"\s*(\d+)", str(v or ""))
            return bool(m) and m.group(1) == want
        for a in A:
            nd = self.nodes[a]
            if any(ok(nd.props.get(k)) for k in ("length", "maxlength")):
                return True
            for e in self.st.out_edges(a) + self.st.in_edges(a):
                if any(ok(e.props.get(k)) for k in ("length", "maxlength")):
                    return True
        return False

    def field_path(self, cls_id: str, parts: list) -> set:
        """java:Form + [header, shipDate] -> the field shipDate of the type of Form.header."""
        cur = cls_id
        for i, p in enumerate(parts):
            fid = f"{cur}.{p}"
            if fid not in self.nodes:
                return set()
            if i == len(parts) - 1:
                return {fid}
            t = re.sub(r".*<\s*([\w.]+)\s*>.*", r"\1", str(self.nodes[fid].props.get("type") or ""))
            t = t.split(".")[-1].replace("[]", "")
            nxt = [n for n in self.nodes if n.startswith("java:") and n.endswith("." + t) and self.nodes[n].kind == "Class"]
            if len(nxt) != 1:
                return set()
            cur = nxt[0]
        return set()

    def flows(self, A, B):
        """FLOWS_TO edges from A to B -> set of usages."""
        out = set()
        for a in A:
            for e in self.st.out_edges(a):
                if e.rel == "FLOWS_TO" and e.dst in B:
                    out |= set(str(e.props.get("usage") or "").split(","))
        return out

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
    truth_pairs, flow_fields, nots = set(), set(), []
    truth_rows = [r for r in rows if r["type"] == "form_field_maps_column"]
    for r in rows:
        A, B = g.resolve(r["from"]), g.resolve(r["to"])
        c = per[r["type"]]
        c["total"] += 1
        if r["type"] == "length_fact":
            m = re.search(r"len=(\d+)", r.get("note") or "")
            if A and g.length_known(A, B, m.group(1) if m else ""):
                c["length"] += 1
        if not A or not B:
            c["unmapped"] += 1
            misses[r["type"]].append({"why": "unmapped " + ("from" if not A else "to"), **r})
            continue
        if r["type"] == "form_field_maps_column":
            # only the value-flow analysis counts here (no name matching, no other edges)
            fol = r.get("followable", True)
            c["followable" if fol else "unfollowable"] += 1
            us = g.flows(A, B)
            want = "cond" if re.search(r"検索|条件|WHERE", r.get("note") or "") else "write"
            if us:
                c["direct"] += 1
                c["near"] += 1
                c["found_followable" if fol else "found_unfollowable"] += 1
                if want in us:
                    c["usage_ok"] += 1
                else:
                    misses[r["type"]].append({"why": "usage " + ",".join(sorted(us)), **r})
                truth_pairs.add((frozenset(A), frozenset(B)))
            else:
                misses[r["type"]].append({"why": "missing" + ("" if fol else " (unfollowable)"), **r})
            flow_fields.update(A)
            continue
        if r["type"] == "field_not_saved":
            flow_fields.update(A)
            nots.append(A)
            continue
        if g.direct(A, B):
            c["direct"] += 1
            c["near"] += 1
        elif g.near(A, B):
            c["near"] += 1
            misses[r["type"]].append({"why": "near only", **r})
        else:
            misses[r["type"]].append({"why": "missing", **r})
    # precision of value flow: predicted FLOWS_TO from the screen fields of the truth
    truth_set = set()
    for r in truth_rows:
        for x in g.resolve(r["from"]):
            for y in g.resolve(r["to"]):
                truth_set.add((x, y))
    pred = set()
    for x in flow_fields:
        for e in st.out_edges(x):
            if e.rel == "FLOWS_TO":
                pred.add((x, e.dst))
    tp = len(pred & truth_set)
    flow_prec = {"predicted": len(pred), "correct": tp, "precision": round(tp / max(1, len(pred)), 3),
                 "not_saved_fields": len(nots),
                 "not_saved_with_flow": sum(1 for A in nots if any(e.rel == "FLOWS_TO" for x in A for e in st.out_edges(x)))}
    per.pop("field_not_saved", None)
    tot = collections.Counter()
    table = []
    for t, c in sorted(per.items()):
        tot.update(c)
        if t == "length_fact":
            c["length_recall_pct"] = round(100 * c["length"] / c["total"])
        table.append({"type": t, **c, "direct_recall": round(c["direct"] / c["total"], 3),
                      "near_recall": round(c["near"] / c["total"], 3)})
    summary = {"total": tot["total"], "direct": tot["direct"], "near": tot["near"], "unmapped": tot["unmapped"],
               "direct_recall": round(tot["direct"] / max(1, tot["total"]), 3),
               "near_recall": round(tot["near"] / max(1, tot["total"]), 3),
               "nodes": len(g.nodes)}
    out = {"summary": summary, "by_type": table, "misses": misses, "value_flow_precision": flow_prec}
    if a.out:
        json.dump(out, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    print("value flow precision:", json.dumps(flow_prec, ensure_ascii=False))
    for t in table:
        print(f"{t['type']:28} {t['total']:4}  direct {t['direct_recall']:.2f}  near {t['near_recall']:.2f}  unmapped {t.get('unmapped', 0)}"
              + (f"  length-known {t.get('length', 0) / t['total']:.2f}" if t['type'] == 'length_fact' else "")
              + (f"  followable {t.get('found_followable', 0)}/{t.get('followable', 0)}  unfollowable {t.get('found_unfollowable', 0)}/{t.get('unfollowable', 0)}  usage-ok {t.get('usage_ok', 0)}"
                 if t['type'] == 'form_field_maps_column' else ""))


if __name__ == "__main__":
    main()
