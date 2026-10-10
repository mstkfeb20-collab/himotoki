"""Neo4j-backed graph store (production).

Nodes carry the label `:Node`, their kind as a second label, a graph name `g`
(one graph per repository/branch, several graphs share a database) and a unique
`uid = "<g>|<id>"`. Every relation is `:REL {rel, props}` so that generic
traversal stays one pattern. `neighbors_many` answers a whole BFS frontier in a
single UNWIND query, which is what keeps impact analysis fast on large graphs.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from ..model import Edge, Node
from .base import GraphStore

_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _label(kind: str) -> str:
    return kind if _LABEL_RE.match(kind) else "Other"


class Neo4jStore(GraphStore):
    def __init__(self, uri: str, user: str | None = None, password: str | None = None,
                 database: str | None = None, graph: str | None = None):
        from neo4j import GraphDatabase  # imported lazily so local mode needs no driver

        self.driver = GraphDatabase.driver(uri, auth=(user, password) if user else None)
        self.database = database
        self.g = graph or "default"
        self._cache: dict[str, Node] = {}
        with self._session() as s:
            s.run("CREATE CONSTRAINT node_uid IF NOT EXISTS FOR (n:Node) REQUIRE n.uid IS UNIQUE")
            s.run("CREATE INDEX node_g_id IF NOT EXISTS FOR (n:Node) ON (n.g, n.id)")
            s.run("CREATE INDEX node_g_kind IF NOT EXISTS FOR (n:Node) ON (n.g, n.kind)")
            s.run("CREATE INDEX node_g_file IF NOT EXISTS FOR (n:Node) ON (n.g, n.file)")
            s.run("CREATE FULLTEXT INDEX node_text IF NOT EXISTS FOR (n:Node) ON EACH [n.name, n.text]")

    def _session(self):
        return self.driver.session(database=self.database) if self.database else self.driver.session()

    def close(self) -> None:
        self.driver.close()

    def _uid(self, i: str) -> str:
        return f"{self.g}|{i}"

    def _row(self, n: Node) -> dict:
        return {"uid": self._uid(n.id), "id": n.id, "kind": n.kind, "name": n.name, "file": n.file, "line": n.line,
                "end_line": n.end_line, "text": n.text[:4000], "props": json.dumps(n.props, ensure_ascii=False)}

    @staticmethod
    def _node(rec) -> Node:
        return Node(id=rec["id"], kind=rec["kind"], name=rec["name"], file=rec.get("file"), line=rec.get("line"),
                    end_line=rec.get("end_line"), text=rec.get("text") or "", props=json.loads(rec.get("props") or "{}"))

    def clear(self) -> None:
        self._cache.clear()
        with self._session() as s:
            s.run("MATCH (n:Node {g: $g}) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS", g=self.g)

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        by_kind: dict[str, list[dict]] = {}
        for n in nodes:
            by_kind.setdefault(_label(n.kind), []).append(self._row(n))
            self._cache.pop(n.id, None)
        with self._session() as s:
            for kind, rows in by_kind.items():
                for i in range(0, len(rows), 2000):
                    s.run(f"UNWIND $rows AS r MERGE (n:Node {{uid: r.uid}}) SET n:{kind}, n.g = $g, n.id = r.id, "
                          f"n.kind = r.kind, n.name = r.name, n.file = r.file, n.line = r.line, "
                          f"n.end_line = r.end_line, n.text = r.text, n.props = r.props",
                          rows=rows[i:i + 2000], g=self.g)

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        rows = [{"a": self._uid(e.src), "b": self._uid(e.dst), "rel": e.rel,
                 "props": json.dumps(e.props, ensure_ascii=False)} for e in edges if e.src != e.dst]
        with self._session() as s:
            for i in range(0, len(rows), 5000):
                s.run("UNWIND $rows AS r MATCH (a:Node {uid: r.a}), (b:Node {uid: r.b}) "
                      "MERGE (a)-[e:REL {rel: r.rel}]->(b) SET e.props = r.props", rows=rows[i:i + 5000])

    def get(self, node_id: str) -> Node | None:
        if node_id in self._cache:
            return self._cache[node_id]
        with self._session() as s:
            rec = s.run("MATCH (n:Node {uid: $u}) RETURN n", u=self._uid(node_id)).single()
        if not rec:
            return None
        node = self._node(dict(rec["n"]))
        self._cache[node_id] = node
        return node

    def get_many(self, ids) -> dict:
        missing = [i for i in ids if i not in self._cache]
        if missing:
            with self._session() as s:
                for r in s.run("UNWIND $u AS u MATCH (n:Node {uid: u}) RETURN n", u=[self._uid(i) for i in missing]):
                    n = self._node(dict(r["n"]))
                    self._cache[n.id] = n
        return {i: self._cache[i] for i in ids if i in self._cache}

    def nodes(self, kinds=None) -> list[Node]:
        with self._session() as s:
            if kinds is None:
                res = s.run("MATCH (n:Node {g: $g}) RETURN n", g=self.g)
            else:
                res = s.run("MATCH (n:Node {g: $g}) WHERE n.kind IN $kinds RETURN n", g=self.g, kinds=list(kinds))
            out = [self._node(dict(r["n"])) for r in res]
        for n in out:
            self._cache[n.id] = n
        return out

    def _edges(self, q: str, node_id: str, rels) -> list[Edge]:
        with self._session() as s:
            res = s.run(q, u=self._uid(node_id), rels=list(rels) if rels else None)
            return [Edge(r["src"], r["dst"], r["rel"], json.loads(r["props"] or "{}")) for r in res]

    def out_edges(self, node_id: str, rels=None) -> list[Edge]:
        return self._edges("MATCH (a:Node {uid: $u})-[e:REL]->(b) WHERE $rels IS NULL OR e.rel IN $rels "
                           "RETURN a.id AS src, b.id AS dst, e.rel AS rel, e.props AS props", node_id, rels)

    def in_edges(self, node_id: str, rels=None) -> list[Edge]:
        return self._edges("MATCH (a)-[e:REL]->(b:Node {uid: $u}) WHERE $rels IS NULL OR e.rel IN $rels "
                           "RETURN a.id AS src, b.id AS dst, e.rel AS rel, e.props AS props", node_id, rels)

    def neighbors_many(self, ids, direction: str, rels=None) -> dict[str, list[Edge]]:
        ids = list(ids)
        out = {i: [] for i in ids}
        if not ids:
            return out
        pat = "(n:Node {uid: u})<-[e:REL]-(o)" if direction == "in" else "(n:Node {uid: u})-[e:REL]->(o)"
        q = (f"UNWIND $us AS u MATCH {pat} WHERE $rels IS NULL OR e.rel IN $rels "
             "RETURN n.id AS id, o.id AS other, e.rel AS rel, e.props AS props")
        with self._session() as s:
            for r in s.run(q, us=[self._uid(i) for i in ids], rels=list(rels) if rels else None):
                src, dst = (r["other"], r["id"]) if direction == "in" else (r["id"], r["other"])
                out[r["id"]].append(Edge(src, dst, r["rel"], json.loads(r["props"] or "{}")))
        return out

    def edge_count(self) -> int:
        with self._session() as s:
            return s.run("MATCH (a:Node {g: $g})-[e:REL]->() RETURN count(e) AS c", g=self.g).single()["c"]
