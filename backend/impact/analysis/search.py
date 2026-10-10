"""Local similarity search over graph nodes (no external service needed).

Character n-grams make it work for Japanese text and for identifiers split into
words. Production deployments can swap this for an embedding model; the engine
only needs `query(text, k, kinds)`."""
from __future__ import annotations

from typing import Iterable

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from ..model import Node


class NodeSearch:
    def __init__(self, nodes: Iterable[Node]):
        self.nodes = [n for n in nodes if (n.text or n.name)]
        docs = [f"{n.name} {n.name} {n.text}".lower() for n in self.nodes]
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1, sublinear_tf=True)
        self.matrix = self.vec.fit_transform(docs) if docs else None

    def scores(self, text: str) -> dict[str, float]:
        if self.matrix is None or not text.strip():
            return {}
        sims = linear_kernel(self.vec.transform([text.lower()]), self.matrix).ravel()
        return {n.id: float(s) for n, s in zip(self.nodes, sims) if s > 0}

    def query(self, text: str, k: int = 20, kinds: set[str] | None = None, min_score: float = 0.05):
        if self.matrix is None or not text.strip():
            return []
        q = self.vec.transform([text.lower()])
        sims = linear_kernel(q, self.matrix).ravel()
        order = sims.argsort()[::-1]
        out = []
        for i in order:
            s = float(sims[i])
            if s < min_score:
                break
            n = self.nodes[i]
            if kinds and n.kind not in kinds:
                continue
            out.append((n, s))
            if len(out) >= k:
                break
        return out
