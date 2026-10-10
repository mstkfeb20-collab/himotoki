"""Graph store interface. The engine only talks to this, so Neo4j and the local
store are interchangeable."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from ..model import Edge, Node


class GraphStore(ABC):
    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def upsert_nodes(self, nodes: Iterable[Node]) -> None: ...

    @abstractmethod
    def upsert_edges(self, edges: Iterable[Edge]) -> None: ...

    @abstractmethod
    def get(self, node_id: str) -> Node | None: ...

    @abstractmethod
    def nodes(self, kinds: Iterable[str] | None = None) -> list[Node]: ...

    @abstractmethod
    def out_edges(self, node_id: str, rels: Iterable[str] | None = None) -> list[Edge]: ...

    @abstractmethod
    def in_edges(self, node_id: str, rels: Iterable[str] | None = None) -> list[Edge]: ...

    @abstractmethod
    def edge_count(self) -> int: ...

    def flush(self) -> None:
        """Persist pending changes (no-op for stores that write through)."""

    def stats(self) -> dict:
        by_kind: dict[str, int] = {}
        for n in self.nodes():
            by_kind[n.kind] = by_kind.get(n.kind, 0) + 1
        return {"nodes": sum(by_kind.values()), "edges": self.edge_count(), "by_kind": by_kind}
