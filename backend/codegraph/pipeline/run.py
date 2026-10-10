"""Indexing pipeline: list -> classify -> extract (cached, parallel) -> link -> store.

Only files whose content hash changed are parsed again; everything else comes
from the fact cache. Linking always runs over the whole fact set (it is cheap
compared with parsing) so cross-file edges stay consistent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Callable

from ..security import max_file_bytes, safe_join

from ..extractors import docs, java, jobs, props, sql, text, xml
from ..rules import load_rules
from .cache import FactCache
from .facts import FileFacts
from .linker import Linker

EXTRACTORS = {m.NAME: m for m in (java, xml, sql, text, props, jobs, docs)}
SQL_EXT = (".sql", ".pls", ".pks", ".pkb", ".prc", ".fnc", ".trg", ".ddl", ".plsql")
DOC_EXT = (".md", ".markdown", ".xlsx", ".xlsm", ".docx")
LEGACY_OFFICE = (".xls", ".doc")
JOB_HINT = re.compile(r"(jp1|ajs|cron|jobnet|job_def|jobdef)", re.I)
SKIP_DIRS = {".git", "node_modules", "target", "build", ".gradle", ".idea", "dist", ".mvn", "bin", "out"}


def classify(path: str, rules, head: str | None = None) -> str | None:
    low = path.lower()
    if low.endswith(".java"):
        return "java"
    if low.endswith((".xml", ".xhtml", ".wsdl", ".xsd")) and not low.endswith(("pom.xml",)):
        return "xml"
    if low.endswith(SQL_EXT):
        return "sql"
    if low.endswith((".properties", ".yml", ".yaml")):
        return "props"
    if text.rules_for(rules, path):
        return "text"
    if low.endswith(DOC_EXT):
        return "docs"
    if JOB_HINT.search(path) or low.endswith((".jp1", ".ajs", ".def", ".tab")) or "crontab" in low:
        if head is not None and (jobs.is_jp1(path, head) or jobs.is_crontab(path, head)):
            return "jobs"
        if head is None:
            return "jobs?"
    if low.endswith(".txt"):
        return "docs"
    return None


def list_repo(root: str) -> dict[str, str]:
    """path -> content hash (git blob id when available)."""
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-s", "-z"], capture_output=True, check=True).stdout
        res = {}
        for rec in out.decode("utf-8", "replace").split("\0"):
            if not rec:
                continue
            meta, _, path = rec.partition("\t")
            parts = meta.split()
            # 120000 = symlink, 160000 = submodule: never follow them out of the checkout
            if len(parts) >= 2 and parts[0] not in ("120000", "160000"):
                res[path] = parts[1]
        if res:
            # include modified-but-uncommitted content by hashing dirty files
            dirty = subprocess.run(["git", "-C", root, "diff", "--name-only", "-z"], capture_output=True).stdout
            for p in dirty.decode("utf-8", "replace").split("\0"):
                if p and p in res and os.path.exists(os.path.join(root, p)):
                    res[p] = "w" + _sha1(os.path.join(root, p))
            return res
    except Exception:
        pass
    res = {}
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in fs:
            full = os.path.join(d, f)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            res[os.path.relpath(full, root)] = "w" + _sha1(full)
    return res


def _sha1(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _convert_legacy(full: str) -> bytes | None:
    """.xls/.doc -> .xlsx/.docx via LibreOffice when it is installed."""
    import shutil
    import tempfile
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    target = "xlsx" if full.lower().endswith(".xls") else "docx"
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([exe, "--headless", "--convert-to", target, "--outdir", td, full], capture_output=True,
                       timeout=120)
        out = os.path.join(td, os.path.splitext(os.path.basename(full))[0] + "." + target)
        return open(out, "rb").read() if os.path.exists(out) else None


def _work(args):
    root, rel, display, ext_name, rules_dir = args
    rules = load_rules(rules_dir)
    full = safe_join(root, rel)
    try:
        if not full or os.path.islink(full) or not os.path.isfile(full):
            f = FileFacts(display, ext_name)
            f.errors.append("skipped: not a regular file inside the repository")
            return f.to_dict()
        limit = max_file_bytes() * (4 if ext_name == "docs" else 1)
        size = os.path.getsize(full)
        if size > limit:
            f = FileFacts(display, ext_name)
            f.errors.append(f"skipped: {size // 1048576} MB is over the {limit // 1048576} MB limit (CODEGRAPH_MAX_FILE_MB)")
            return f.to_dict()
        data = open(full, "rb").read()
        if ext_name == "docs" and rel.lower().endswith(LEGACY_OFFICE):
            conv = _convert_legacy(full)
            if conv is None:
                f = FileFacts(display, "docs")
                f.errors.append("legacy Office format needs LibreOffice (soffice) to convert")
                return f.to_dict()
            display_ext = ".xlsx" if rel.lower().endswith(".xls") else ".docx"
            f = EXTRACTORS["docs"].extract(display + display_ext, conv, rules)
            f.path = display
            return f.to_dict()
        return EXTRACTORS[ext_name].extract(display, data, rules).to_dict()
    except RecursionError:
        f = FileFacts(display, ext_name)
        f.errors.append("skipped: nesting too deep")
        return f.to_dict()
    except Exception as e:      # one bad file never stops the index; it is reported instead
        f = FileFacts(display, ext_name)
        f.errors.append(f"{type(e).__name__}: {str(e)[:300]}")
        return f.to_dict()


def _code_digest() -> str:
    """Extractor code is part of the cache key, so editing an extractor (or a helper it
    shares, such as the SQL analyser) re-extracts the files it affects."""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    h = hashlib.sha1()
    for sub in ("extractors", "pipeline/facts.py", "text.py"):
        p = os.path.join(base, sub)
        files = [p] if p.endswith(".py") else sorted(os.path.join(p, f) for f in os.listdir(p) if f.endswith(".py"))
        for f in files:
            with open(f, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()[:12]


CODE_DIGEST = _code_digest()


class Pipeline:
    def __init__(self, data_dir: str, rules_dir: str | None = None, workers: int | None = None):
        self.data_dir = data_dir
        self.rules_dir = rules_dir
        self.rules = load_rules(rules_dir)
        self.cache = FactCache(os.path.join(data_dir, "facts.db"))
        self.workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2)))

    def plan(self, root: str, prefix: str = "") -> list[tuple]:
        files = list_repo(root)
        out = []
        for rel, h in files.items():
            kind = classify(rel, self.rules)
            if kind == "jobs?":
                try:
                    with open(os.path.join(root, rel), "rb") as fh:
                        head = fh.read(4000).decode("utf-8", "replace")
                except OSError:
                    continue
                kind = "jobs" if (jobs.is_jp1(rel, head) or jobs.is_crontab(rel, head)) else None
            if kind is None and rel.lower().endswith(LEGACY_OFFICE):
                kind = "docs"
            if kind:
                out.append((root, rel, prefix + rel, kind, h))
        return out

    def extract_all(self, plan, progress=None):
        emit = progress or (lambda *_: None)
        facts, misses = [], []
        for root, rel, display, kind, h in plan:
            ver = f"{EXTRACTORS[kind].VERSION}:{CODE_DIGEST}:{self.rules.digest}"
            hit = self.cache.get(display, h, kind, ver)
            if hit is not None:
                facts.append(FileFacts.from_dict(hit))
            else:
                misses.append((root, rel, display, kind, h, ver))
        emit("extract.plan", {"files": len(plan), "cached": len(facts), "to_parse": len(misses)})
        t0 = time.time()
        results = []
        if misses:
            args = [(m[0], m[1], m[2], m[3], self.rules_dir) for m in misses]
            if self.workers > 1 and len(misses) > 40:
                with ProcessPoolExecutor(max_workers=self.workers) as ex:
                    for i, r in enumerate(ex.map(_work, args, chunksize=16)):
                        results.append(r)
                        if i % 200 == 0:
                            emit("extract.progress", {"done": i, "total": len(misses)})
            else:
                results = [_work(a) for a in args]
            self.cache.put_many([(m[2], m[4], m[3], m[5], r) for m, r in zip(misses, results)])
        facts += [FileFacts.from_dict(r) for r in results]
        emit("extract.done", {"parsed": len(misses), "seconds": round(time.time() - t0, 2)})
        return facts, len(misses)

    def index(self, repo: str | dict, store, docs_dir: str | None = None, extra_facts: list | None = None,
              progress: Callable | None = None, stats_dir: str | None = None) -> dict:
        """`repo` is one repository, or {name: path} for a system made of several
        repositories: their files are indexed into one graph under `<name>/`, so
        calls, files, tables and queues that cross servers link like any other."""
        emit = progress or (lambda *_: None)
        t0 = time.time()
        repos = check_repo_names(repo) if isinstance(repo, dict) else None
        if repos:
            plan = [p for name, path in repos.items() for p in self.plan(path, prefix=f"{name}/")]
        else:
            plan = self.plan(repo)
        if docs_dir and os.path.isdir(docs_dir):
            plan += self.plan(docs_dir, prefix="design:")
        facts, parsed = self.extract_all(plan, progress)
        t1 = time.time()
        from ..sources.catalog import load_sources
        facts += (extra_facts or []) + load_sources(self.data_dir)
        facts.sort(key=lambda f: f.path)        # deterministic linking regardless of cache hits
        linker = Linker(self.rules)
        nodes, edges, report = linker.link(facts)
        t2 = time.time()
        emit("link.done", {"nodes": len(nodes), "edges": len(edges), "seconds": round(t2 - t1, 2)})
        store.clear()
        store.upsert_nodes(nodes)
        store.upsert_edges(edges)
        store.flush()
        errors = [(f.path, e) for f in facts for e in f.errors]
        by_ext = {}
        for p in plan:
            by_ext[p[3]] = by_ext.get(p[3], 0) + 1
        stats = store.stats()
        stats.update({
            "repo": repo, "docs_dir": docs_dir, "files": len(plan), "by_extractor": by_ext, "parsed": parsed,
            "cached": len(plan) - parsed, "seconds": round(time.time() - t0, 2),
            "seconds_extract": round(t1 - t0, 2), "seconds_link": round(t2 - t1, 2),
            "unresolved": report["unresolved"], "unresolved_samples": report["unresolved_samples"],
            "external": report["external"], "runtime": report["runtime"], "drift": report["drift"], "transactions": report.get("transactions"),
            "resolved_by": report["resolved_by"], "errors": len(errors), "error_samples": errors[:20],
            "rules": self.rules.names(), "rules_digest": self.rules.digest,
            "commit": None if repos else _git_head(repo),
        })
        if repos:
            stats.update({"repo": None, "repos": repos, "commits": {n: _git_head(p) for n, p in repos.items()},
                          "files_by_repo": {n: sum(1 for x in plan if x[2].startswith(n + "/")) for n in repos}})
        json.dump(stats, open(os.path.join(stats_dir or self.data_dir, "index_stats.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        emit("index.done", {k: stats[k] for k in ("nodes", "edges", "files", "parsed", "seconds")})
        return stats


REPO_NAME = re.compile(r"[A-Za-z0-9][\w.\-]{0,99}")


def check_repo_names(repos: dict) -> dict:
    """Repository names become path prefixes inside the graph: plain names only."""
    if not repos:
        raise ValueError("no repositories given")
    out = {}
    for name, path in repos.items():
        if not REPO_NAME.fullmatch(str(name)) or name in ("design",):
            raise ValueError(f"invalid repository name: {str(name)[:60]!r}")
        out[str(name)] = path
    return out


def _git_head(repo):
    try:
        return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:
        return None
