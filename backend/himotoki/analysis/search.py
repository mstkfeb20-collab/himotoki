"""Local similarity search over graph nodes (no external service needed).

Character n-grams make it work for Japanese text and for identifiers split into
words. Production deployments can swap this for an embedding model; the engine
only needs `query(text, k, kinds)`.

Fitting the vectoriser is the slowest part of opening a large graph (tens of
seconds for 100k elements), so the fitted index is kept next to the graph and
reused while the graph file is unchanged."""
from __future__ import annotations

import logging
import os
import pickle
from typing import Iterable

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from ..model import Node

log = logging.getLogger("himotoki.search")
TEXT_CAP = 1200          # characters of an element's text that go into the index
CACHE_VERSION = "3"


class NodeSearch:
    def __init__(self, nodes: Iterable[Node], cache: str | None = None, key: str | None = None):
        self.nodes = [n for n in nodes if (n.text or n.name)]
        self.vec, self.matrix = None, None
        key = f"{CACHE_VERSION}:{TEXT_CAP}:{key}" if key else None
        if cache and key and self._load(cache, key):
            return
        docs = [f"{n.name} {n.name} {n.text[:TEXT_CAP]}".lower() for n in self.nodes]
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1, sublinear_tf=True,
                                   dtype=np.float32)
        self.matrix = self.vec.fit_transform(docs) if docs else None
        if cache and key and self.matrix is not None:
            self._save(cache, key)

    def _load(self, path: str, key: str) -> bool:
        try:
            with open(path, "rb") as f:
                d = pickle.load(f)          # written by this process family only, next to the graph
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError):
            return False
        if d.get("key") != key or d.get("ids") != [n.id for n in self.nodes]:
            return False
        self.vec, self.matrix = d["vec"], d["matrix"]
        return True

    def _save(self, path: str, key: str) -> None:
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                pickle.dump({"key": key, "ids": [n.id for n in self.nodes], "vec": self.vec,
                             "matrix": self.matrix}, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, path)
        except OSError as e:                # a read-only workspace only loses the speed-up
            log.warning("search index not cached: %s", e)

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
