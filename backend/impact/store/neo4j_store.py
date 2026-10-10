"""Neo4j-backed graph store (production).

Every node is stored with the label `:Node` plus its kind as a second label, so
both generic traversal and kind-specific Cypher queries stay simple. A small
read-through cache keeps the engine's many neighbour lookups cheap.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

from ..model import Edge, Node
from .base import GraphStore

_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _label(kind: str) -> str:
    if not _LABEL_RE.match(kind):
        raise ValueError(f"invalid kind label: {kind}")
    return kind


class Neo4jStore(GraphStore):
    def __init__(self, uri: str, user: str | None = None, password: str | None = None,
                 database: str | None = None):
        from neo4j import GraphDatabase  # imported lazily so local mode needs no driver

        auth = (user, password) if user else None
        self.driver = GraphDatabase.driver(uri, auth=auth)
        self.database = database
        self._cache: dict[str, Node] = {}
        with self._session() as s:
            s.run("CREATE CONSTRAINT node_id IF NOT EXISTS FOR (n:Node) REQUIRE n.id IS UNIQUE")
            s.run("CREATE FULLTEXT INDEX node_text IF NOT EXISTS FOR (n:Node) ON EACH [n.name, n.text]")

    def _session(self):
        return self.driver.session(database=self.database) if self.database else self.driver.session()

    def close(self) -> None:
        self.driver.close()

    @staticmethod
    def _row(n: Node) -> dict:
        return {"id": n.id, "kind": n.kind, "name": n.name, "file": n.file, "line": n.line,
                "end_line": n.end_line, "text": n.text,
                "props": json.dumps(n.props, ensure_ascii=False)}

    @staticmethod
    def _node(rec) -> Node:
        return Node(id=rec["id"], kind=rec["kind"], name=rec["name"], file=rec.get("file"),
                    line=rec.get("line"), end_line=rec.get("end_line"), text=rec.get("text") or "",
                    props=json.loads(rec.get("props") or "{}"))

    def clear(self) -> None:
        self._cache.clear()
        with self._session() as s:
            s.run("MATCH (n:Node) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS")

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        by_kind: dict[str, list[dict]] = {}
        for n in nodes:
            by_kind.setdefault(_label(n.kind), []).append(self._row(n))
            self._cache.pop(n.id, None)
        with self._session() as s:
            for kind, rows in by_kind.items():
                for i in range(0, len(rows), 2000):
                    s.run(
                        f"UNWIND $rows AS r MERGE (n:Node {{id: r.id}}) "
                        f"SET n:{kind}, n.kind = r.kind, n.name = r.name, n.file = r.file, "
                        f"n.line = r.line, n.end_line = r.end_line, n.text = r.text, n.props = r.props",
                        rows=rows[i:i + 2000])

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        rows = [{"src": e.src, "dst": e.dst, "rel": e.rel,
                 "props": json.dumps(e.props, ensure_ascii=False)} for e in edges if e.src != e.dst]
        with self._session() as s:
            for i in range(0, len(rows), 5000):
                s.run(
                    "UNWIND $rows AS r MATCH (a:Node {id: r.src}), (b:Node {id: r.dst}) "
                    "MERGE (a)-[e:REL {rel: r.rel}]->(b) SET e.props = r.props",
                    rows=rows[i:i + 5000])

    def get(self, node_id: str) -> Node | None:
        if node_id in self._cache:
            return self._cache[node_id]
        with self._session() as s:
            rec = s.run("MATCH (n:Node {id: $id}) RETURN n", id=node_id).single()
        if not rec:
            return None
        node = self._node(dict(rec["n"]))
        self._cache[node_id] = node
        return node

    def nodes(self, kinds=None) -> list[Node]:
        with self._session() as s:
            if kinds is None:
                res = s.run("MATCH (n:Node) RETURN n")
            else:
                res = s.run("MATCH (n:Node) WHERE n.kind IN $kinds RETURN n", kinds=list(kinds))
            out = [self._node(dict(r["n"])) for r in res]
        for n in out:
            self._cache[n.id] = n
        return out

    def _edges(self, query: str, node_id: str, rels) -> list[Edge]:
        with self._session() as s:
            res = s.run(query, id=node_id, rels=list(rels) if rels else None)
            return [Edge(r["src"], r["dst"], r["rel"], json.loads(r["props"] or "{}")) for r in res]

    def out_edges(self, node_id: str, rels=None) -> list[Edge]:
        return self._edges(
            "MATCH (a:Node {id: $id})-[e:REL]->(b) WHERE $rels IS NULL OR e.rel IN $rels "
            "RETURN a.id AS src, b.id AS dst, e.rel AS rel, e.props AS props", node_id, rels)

    def in_edges(self, node_id: str, rels=None) -> list[Edge]:
        return self._edges(
            "MATCH (a)-[e:REL]->(b:Node {id: $id}) WHERE $rels IS NULL OR e.rel IN $rels "
            "RETURN a.id AS src, b.id AS dst, e.rel AS rel, e.props AS props", node_id, rels)

    def edge_count(self) -> int:
        with self._session() as s:
            return s.run("MATCH ()-[e:REL]->() RETURN count(e) AS c").single()["c"]
