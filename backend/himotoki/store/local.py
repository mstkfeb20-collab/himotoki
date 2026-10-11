"""In-process graph store persisted as JSON. Used for development, CI and small
repositories; production uses Neo4jStore through the same interface."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Iterable

from ..model import Edge, Node
from .base import GraphStore


class LocalStore(GraphStore):
    def __init__(self, path: str | None = None):
        self.path = path
        self._nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}
        self._out: dict[str, set] = defaultdict(set)
        self._in: dict[str, set] = defaultdict(set)
        if path and os.path.exists(path):
            self._load()

    def _load(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.upsert_nodes(Node.from_dict(d) for d in data.get("nodes", []))
        self.upsert_edges(Edge(**e) for e in data.get("edges", []))

    def flush(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "nodes": [n.to_dict() for n in self._nodes.values()],
                "edges": [e.to_dict() for e in self._edges.values()],
            }, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self._nodes.clear(); self._edges.clear(); self._out.clear(); self._in.clear()

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        for n in nodes:
            old = self._nodes.get(n.id)
            if old:
                merged = dict(old.props); merged.update(n.props); n.props = merged
            self._nodes[n.id] = n

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        for e in edges:
            if e.src == e.dst:
                continue
            k = e.key()
            self._edges[k] = e
            self._out[e.src].add(k)
            self._in[e.dst].add(k)

    def get(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def nodes(self, kinds: Iterable[str] | None = None) -> list[Node]:
        if kinds is None:
            return list(self._nodes.values())
        ks = set(kinds)
        return [n for n in self._nodes.values() if n.kind in ks]

    def _filter(self, keys, rels):
        rs = set(rels) if rels else None
        out = []
        for k in keys:
            e = self._edges[k]
            if rs is None or e.rel in rs:
                out.append(e)
        return out

    def out_edges(self, node_id: str, rels=None) -> list[Edge]:
        return self._filter(self._out.get(node_id, ()), rels)

    def in_edges(self, node_id: str, rels=None) -> list[Edge]:
        return self._filter(self._in.get(node_id, ()), rels)

    def edge_count(self) -> int:
        return len(self._edges)
