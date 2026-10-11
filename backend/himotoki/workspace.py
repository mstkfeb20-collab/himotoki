"""Workspace: several graphs (one per repository / branch) sharing one fact cache.

    <HIMOTOKI_DATA_DIR>/
      facts.db                 content-addressed extraction cache (shared)
      sources/                 DB catalogs and runtime SQL imports (shared)
      worktrees/<ref>/         git worktrees for branch snapshots
      graphs/<name>/           graph store, runs, index statistics per graph
      workspace.json           registered graphs and the default
"""
from __future__ import annotations

import json
import logging
import time
import os
import re
import threading

from .analysis.engine import Engine
from .pipeline.run import Pipeline
from .security import atomic_write_json
from .store import open_store


log = logging.getLogger("himotoki.workspace")

class Workspace:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(os.path.join(self.root, "graphs"), exist_ok=True)
        self.meta_path = os.path.join(self.root, "workspace.json")
        self.meta = self._load_meta()
        self.engines: dict[str, Engine] = {}
        self.lock = threading.RLock()
        self.pipeline = Pipeline(self.root)
        self.index_state: dict[str, dict] = {}
        self._graph_locks: dict[str, threading.Lock] = {}

    def _load_meta(self) -> dict:
        empty = {"graphs": {}, "default": None}
        if not os.path.exists(self.meta_path):
            return empty
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict) or not isinstance(meta.get("graphs"), dict):
                raise ValueError("unexpected structure")
            return meta
        except (OSError, ValueError) as e:
            # keep the broken file for inspection and start with an empty registry;
            # graphs on disk stay and are re-registered by indexing again
            bad = f"{self.meta_path}.corrupt-{int(time.time())}"
            os.replace(self.meta_path, bad)
            log.error("workspace.json unreadable (%s); moved to %s", e, bad)
            return empty

    def save(self):
        atomic_write_json(self.meta_path, self.meta, indent=1)

    @staticmethod
    def graph_name(repo: str, ref: str | None) -> str:
        base = os.path.basename(os.path.abspath(repo).rstrip("/")) or "repo"
        return re.sub(r"[^\w.@\-]+", "_", f"{base}@{ref}" if ref else base)

    def graph_dir(self, name: str) -> str:
        if not re.fullmatch(r"[\w.@\-]{1,200}", name or "") or name in (".", ".."):
            raise ValueError(f"invalid graph name: {name[:60]!r}")
        d = os.path.join(self.root, "graphs", name)
        os.makedirs(d, exist_ok=True)
        return d

    def default(self) -> str | None:
        return self.meta.get("default")

    def engine(self, name: str | None = None) -> Engine | None:
        name = name or self.default()
        if not name:
            return None
        with self.lock:
            if name not in self.meta["graphs"]:
                return None          # unknown names never touch the file system
            if name not in self.engines:
                g = self.meta["graphs"].get(name, {})
                d = self.graph_dir(name)
                store = open_store(d, graph=name)
                self.engines[name] = Engine(store, d, g.get("snapshot") or g.get("repo"), g.get("docs_dir"),
                                            settings_dir=self.root, repos=g.get("repos"))
            return self.engines[name]

    def index(self, repo: str, docs_dir: str | None = None, ref: str | None = None, name: str | None = None,
              progress=None) -> dict:
        from .sources.git import GitRepo
        repo = os.path.abspath(repo)
        name = name or self.graph_name(repo, ref)
        d = self.graph_dir(name)
        with self.lock:
            glock = self._graph_locks.setdefault(name, threading.Lock())
        with glock:      # a webhook and a manual request never write the same graph at once
            snapshot = GitRepo(repo, self.root).worktree(ref) if ref else repo
            store = open_store(d, graph=name)
            stats = self.pipeline.index(snapshot, store, docs_dir, progress=progress, stats_dir=d)
        return self._register(name, repo, ref, snapshot, docs_dir, store, stats)

    def index_system(self, name: str, repos: dict[str, str], docs_dir: str | None = None, progress=None) -> dict:
        """Several repositories (the servers of one business system) as one graph."""
        from .pipeline.run import check_repo_names
        repos = {n: os.path.abspath(p) for n, p in check_repo_names(repos).items()}
        d = self.graph_dir(name)
        with self.lock:
            glock = self._graph_locks.setdefault(name, threading.Lock())
        with glock:
            store = open_store(d, graph=name)
            stats = self.pipeline.index(repos, store, docs_dir, progress=progress, stats_dir=d)
        return self._register(name, None, None, None, docs_dir, store, stats, repos=repos)

    def _register(self, name, repo, ref, snapshot, docs_dir, store, stats, repos=None) -> dict:
        with self.lock:
            self.meta["graphs"][name] = {"repo": repo, "ref": ref, "snapshot": snapshot, "docs_dir": docs_dir,
                                         "commit": stats.get("commit"), "nodes": stats["nodes"], "edges": stats["edges"]}
            if repos:
                self.meta["graphs"][name].update(repos=repos, commits=stats.get("commits"))
            if not self.meta.get("default"):
                self.meta["default"] = name
            self.save()
            if name in self.engines:
                e = self.engines[name]
                e.store = store
                e.repo, e.docs_dir, e.repos = snapshot, docs_dir, repos
                e.reload()
            else:
                self.engines.pop(name, None)
        return stats

    def change_hunks(self, name: str, base: str, head: str, repo_name: str | None = None) -> dict:
        """Changed line ranges between two refs, in graph paths. In a system graph the
        change belongs to one of its repositories (`repo_name`; optional when it has one)."""
        from .sources.git import GitRepo
        g = self.meta["graphs"][name]
        repos = g.get("repos")
        if not repos:
            gr = GitRepo(g["repo"], self.root)
            return gr.hunks(gr.resolve(base), gr.resolve(head))
        if repo_name is None and len(repos) == 1:
            repo_name = next(iter(repos))
        if repo_name not in repos:
            raise ValueError(f"name the repository of this system graph: {', '.join(repos)}")
        gr = GitRepo(repos[repo_name], self.root)
        out = {}
        for f, rs in gr.hunks(gr.resolve(base), gr.resolve(head)).items():
            out[("+" + repo_name + "/" + f[1:]) if f.startswith("+") else f"{repo_name}/{f}"] = rs
        return out

    def stats(self, name: str | None = None) -> dict:
        name = name or self.default()
        p = os.path.join(self.root, "graphs", name or "", "index_stats.json")
        if not (name and os.path.exists(p)):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            log.warning("index_stats.json for %s is unreadable", name)
            return {}

    def find_run(self, run_id: str):
        for name in list(self.meta["graphs"]):
            e = self.engine(name)
            if e and e.state(run_id):
                return e
        return None
