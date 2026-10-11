"""Learning from people and from actual changes.

Every confirm / exclude decision and every verification of an implemented change
is recorded per graph (one system) in `knowledge.db`, beside the graph and
independent of it, so re-indexing never loses it. Later analyses of the same
system use what was learned:

* path patterns — "a Column reached over READS from a SqlStatement" was
  confirmed / excluded how often → the propagation weight of that pattern is
  raised or lowered (only after MIN_PATTERN decisions);
* specific edges — the same parent → element step decided repeatedly;
* elements — excluded again and again, confirmed again and again, changed in
  reality although excluded or left undecided ("missed"), predicted but never
  changed (over-prediction) → score adjusted and a note shown with the reason;
* learned links — changes the graph never reached ("not found" in a
  verification) become links from the run's starting elements to what was
  actually changed, so the next analysis finds them.

Structural edges are never modified: learning is a separate layer, every item
keeps its evidence (runs, people, reasons), counts distinct analyses (a
re-investigation is the same analysis), can be switched off per run
(`learning: false`) and individual items can be ignored.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections import defaultdict

MIN_PATTERN = 5          # decisions on a pattern before its weight moves
MIN_EDGE = 2             # decisions on one parent -> element step
MIN_NODE = 2             # analyses that excluded / confirmed an element

LEARNED_REL = "LEARNED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  root TEXT, run_id TEXT, node_id TEXT, kind TEXT, parent_id TEXT, parent_kind TEXT, rel TEXT,
  outcome TEXT, change_type TEXT, reason TEXT, actor TEXT, at REAL, PRIMARY KEY (root, node_id));
CREATE TABLE IF NOT EXISTS outcomes (
  root TEXT, run_id TEXT, node_id TEXT, outcome TEXT, at REAL, PRIMARY KEY (root, node_id));
CREATE TABLE IF NOT EXISTS links (
  src TEXT, dst TEXT, root TEXT, run_id TEXT, at REAL, PRIMARY KEY (src, dst, root));
CREATE TABLE IF NOT EXISTS ignored (
  kind TEXT, key TEXT, actor TEXT, at REAL, PRIMARY KEY (kind, key));
"""


def _rate_mult(c: int, x: int, lo: float, hi: float) -> float:
    """Smoothed confirmation rate mapped to a weight factor; 1.0 at an even split."""
    r = (c + 1) / (c + x + 2)
    return round(max(lo, min(hi, 0.5 + r)), 3)


def pattern_key(rel, parent_kind, kind) -> str:
    return f"{rel}|{parent_kind}|{kind}"


class Learned:
    """Read-only snapshot used by one analysis."""

    def __init__(self):
        self.patterns: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.nodes: dict[str, dict] = {}
        self.links: dict[str, list] = defaultdict(list)

    def pattern(self, rel, parent_kind, kind) -> float:
        p = self.patterns.get(pattern_key(rel, parent_kind, kind))
        return p["mult"] if p and p["active"] else 1.0

    def edge(self, parent, node, rel) -> float:
        e = self.edges.get((parent, node, rel))
        return e["mult"] if e and e["active"] else 1.0

    def node(self, node_id) -> tuple[float, list[dict]]:
        n = self.nodes.get(node_id)
        if not n or not n["active"]:
            return 1.0, []
        return n["mult"], n["notes"]

    @property
    def empty(self) -> bool:
        return not (self.patterns or self.edges or self.nodes or self.links)


class Knowledge:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "knowledge.db")
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------ recording
    def record_decision(self, root: str, run_id: str, c: dict, parent_kind: str | None, actor: str):
        status = (c.get("decision") or {}).get("status")
        with self.lock:
            if status not in ("confirmed", "excluded"):
                self.db.execute("DELETE FROM decisions WHERE root=? AND node_id=?", (root, c["node_id"]))
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (root, run_id, c["node_id"], c["kind"], c.get("parent"), parent_kind, c.get("via_rel"), status,
                     c.get("change_type"), (c.get("decision") or {}).get("reason") or "", actor, time.time()))
            self.db.commit()

    def record_verification(self, root: str, run_id: str, v: dict, seeds: list[str], matched_predicted: list[str]):
        now = time.time()
        rows = [(root, run_id, nid, "hit", now) for nid in matched_predicted]
        for m in v["missed"]:
            outcome = "missed_not_found" if m["why"] == "not_found" else "missed_candidate"
            rows.append((root, run_id, m["node_id"], outcome, now))
            if m.get("candidate_id") and m["candidate_id"] != m["node_id"]:
                rows.append((root, run_id, m["candidate_id"], outcome, now))
        rows += [(root, run_id, u["node_id"], "untouched", now) for u in v["untouched"]]
        links = [(s, m["node_id"], root, run_id, now) for m in v["missed"] if m["why"] == "not_found"
                 for s in seeds if s != m["node_id"]]
        with self.lock:
            self.db.execute("DELETE FROM outcomes WHERE root=?", (root,))     # the latest verification counts
            self.db.execute("DELETE FROM links WHERE root=?", (root,))
            self.db.executemany("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?)", rows)
            self.db.executemany("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?)", links)
            self.db.commit()

    def ignore(self, kind: str, key: str, actor: str = "user", on: bool = True):
        with self.lock:
            if on:
                self.db.execute("INSERT OR REPLACE INTO ignored VALUES (?,?,?,?)", (kind, key, actor, time.time()))
            else:
                self.db.execute("DELETE FROM ignored WHERE kind=? AND key=?", (kind, key))
            self.db.commit()

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> Learned:
        L = Learned()
        with self.lock:
            dec = self.db.execute("SELECT root, node_id, kind, parent_id, parent_kind, rel, outcome, reason, actor "
                                  "FROM decisions").fetchall()
            out = self.db.execute("SELECT root, node_id, outcome FROM outcomes").fetchall()
            lk = self.db.execute("SELECT src, dst, root FROM links").fetchall()
            ign = {(k, key) for k, key in self.db.execute("SELECT kind, key FROM ignored")}
        pat = defaultdict(lambda: [0, 0])
        edg = defaultdict(lambda: [0, 0])
        nod = defaultdict(lambda: {"confirmed": set(), "excluded": set(), "reasons": [], "outcomes": defaultdict(set)})
        for root, nid, kind, parent, pkind, rel, outcome, reason, actor in dec:
            i = 0 if outcome == "confirmed" else 1
            if rel and pkind:
                pat[pattern_key(rel, pkind, kind)][i] += 1
            if parent and rel:
                edg[(parent, nid, rel)][i] += 1
            n = nod[nid]
            n[outcome].add(root)
            if outcome == "excluded" and reason:
                n["reasons"].append(f"{reason}（{actor}）")
        for root, nid, outcome in out:
            nod[nid]["outcomes"][outcome].add(root)
        for key, (c, x) in pat.items():
            L.patterns[key] = {"key": key, "confirmed": c, "excluded": x, "mult": _rate_mult(c, x, 0.5, 1.4),
                               "active": c + x >= MIN_PATTERN and ("pattern", key) not in ign,
                               "ignored": ("pattern", key) in ign}
        for key, (c, x) in edg.items():
            k = "|".join(key)
            L.edges[key] = {"key": k, "confirmed": c, "excluded": x, "mult": _rate_mult(c, x, 0.4, 1.5),
                            "active": c + x >= MIN_EDGE and ("edge", k) not in ign}
        for nid, n in nod.items():
            c, x = len(n["confirmed"]), len(n["excluded"])
            missed = len(n["outcomes"]["missed_candidate"])
            untouched = len(n["outcomes"]["untouched"]) - len(n["outcomes"]["hit"])
            notes, mult = [], 1.0
            if missed:
                mult = 1.3
                notes.append({"type": "missed_before", "factor": 1.3,
                              "message": f"過去の解析で除外・未確定だったが、実際には変更された（{missed} 回）"})
            elif x >= MIN_NODE and c == 0:
                mult = 0.7
                notes.append({"type": "excluded_before", "factor": 0.7,
                              "message": f"過去 {x} 回の解析で除外された" + (f"。理由: {n['reasons'][-1]}" if n["reasons"] else "")})
            elif c >= MIN_NODE and x == 0:
                mult = 1.15
                notes.append({"type": "confirmed_before", "factor": 1.15, "message": f"過去 {c} 回の解析で影響ありと確定された"})
            if untouched >= MIN_NODE and not missed:
                mult = round(mult * 0.85, 3)
                notes.append({"type": "overpredicted", "factor": 0.85,
                              "message": f"過去 {untouched} 回、影響ありと予測したが実際には変更されなかった"})
            if x and c and not notes:
                notes.append({"type": "mixed", "factor": 1.0, "message": f"過去の判断が分かれている（確定 {c}・除外 {x}）"})
            if notes:
                L.nodes[nid] = {"node_id": nid, "mult": mult, "notes": notes, "confirmed": c, "excluded": x,
                                "missed": missed, "untouched": max(untouched, 0),
                                "active": ("node", nid) not in ign, "ignored": ("node", nid) in ign}
        cnt = defaultdict(set)
        for src, dst, root in lk:
            cnt[(src, dst)].add(root)
        for (src, dst), roots in cnt.items():
            k = f"{src}|{dst}"
            L.links[src].append({"dst": dst, "count": len(roots), "weight": round(min(0.8, 0.4 + 0.1 * len(roots)), 2),
                                 "key": k, "active": ("link", k) not in ign, "ignored": ("link", k) in ign})
        return L

    def summary(self, store=None) -> dict:
        """For the viewer and MCP: what has been learned, with names when a store is given."""
        L = self.snapshot()
        with self.lock:
            n_dec = self.db.execute("SELECT COUNT(*), COUNT(DISTINCT root) FROM decisions").fetchone()
            n_ver = self.db.execute("SELECT COUNT(DISTINCT root) FROM outcomes").fetchone()[0]
        name = (lambda i: (store.get(i).name if store and store.get(i) else i))
        return {
            "decisions": n_dec[0], "analyses": n_dec[1], "verifications": n_ver,
            "thresholds": {"pattern": MIN_PATTERN, "edge": MIN_EDGE, "node": MIN_NODE},
            "patterns": sorted([dict(p, rel=p["key"].split("|")[0], parent_kind=p["key"].split("|")[1],
                                     kind=p["key"].split("|")[2]) for p in L.patterns.values()],
                               key=lambda p: -(p["confirmed"] + p["excluded"])),
            "nodes": sorted([dict(n, name=name(n["node_id"])) for n in L.nodes.values()],
                            key=lambda n: -abs(n["mult"] - 1)),
            "links": [{"src": s, "src_name": name(s), "dst": l["dst"], "dst_name": name(l["dst"]), "count": l["count"],
                       "weight": l["weight"], "key": l["key"], "ignored": l["ignored"]} for s, ls in L.links.items() for l in ls],
        }
