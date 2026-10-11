import os

from .base import GraphStore
from .local import LocalStore


def open_store(data_dir: str, graph: str | None = None) -> GraphStore:
    """Pick the store from the environment: HIMOTOKI_STORE=neo4j uses NEO4J_URI etc."""
    if os.environ.get("HIMOTOKI_STORE", "local").lower() == "neo4j":
        from .neo4j_store import Neo4jStore
        return Neo4jStore(
            os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            os.environ.get("NEO4J_USER"), os.environ.get("NEO4J_PASSWORD"),
            os.environ.get("NEO4J_DATABASE"), graph=graph,
        )
    return LocalStore(os.path.join(data_dir, "graph.json"))


__all__ = ["GraphStore", "LocalStore", "open_store"]
