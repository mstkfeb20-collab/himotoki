"""Repository indexing: walks a repository, runs the per-language indexers,
links them together and writes the result into a GraphStore."""
from __future__ import annotations

import os
import re
import subprocess
import time
from collections import defaultdict
from typing import Callable

from ..model import Edge, Kind, Node, Rel
from ..store.base import GraphStore
from .java import index_java
from .resources import index_config, index_docs, index_messages, index_sql, index_templates
from .text import words

SKIP_DIRS = {".git", "node_modules", "target", "build", ".gradle", ".idea", "dist", ".mvn"}
TEXT_EXT = {".java", ".html", ".sql", ".properties", ".yml", ".yaml", ".xml", ".md", ".txt",
            ".js", ".ts", ".css", ".scss", ".json", ".kt", ".jsp"}


def list_files(root: str) -> list[str]:
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True, check=True).stdout
        files = [f for f in out.splitlines() if f]
        if files:
            return files
    except Exception:
        pass
    files = []
    for d, dirs, fs in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in fs:
            files.append(os.path.relpath(os.path.join(d, f), root))
    return files


def git_history(repo: str, limit: int = 400) -> dict[str, list[dict]]:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", f"-n{limit}", "--name-only", "--date=short",
             "--format=%x1e%h%x1f%ad%x1f%an%x1f%s"], capture_output=True, text=True, check=True).stdout
    except Exception:
        return {}
    hist: dict[str, list[dict]] = defaultdict(list)
    for block in out.split("\x1e"):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if not lines or "\x1f" not in lines[0]:
            continue
        h, date, author, subject = lines[0].split("\x1f", 3)
        for f in lines[1:]:
            hist[f].append({"hash": h, "date": date, "author": author, "subject": subject})
    return hist


def index_repository(repo: str, store: GraphStore, docs_dir: str | None = None,
                     progress: Callable[[str, dict], None] | None = None) -> dict:
    t0 = time.time()
    emit = progress or (lambda *_: None)
    files = list_files(repo)
    java_files = [f for f in files if f.endswith(".java")]
    tpl_files = [f for f in files if f.endswith(".html") and "templates/" in f]
    sql_files = [f for f in files if f.endswith(".sql")]
    msg_files = [f for f in files if f.endswith(".properties") and "messages" in f]
    cfg_files = [f for f in files if re.search(r"application[\w\-]*\.(properties|ya?ml)$", f)]
    repo_docs = [f for f in files if f.lower().endswith(".md")]
    emit("index.files", {"java": len(java_files), "templates": len(tpl_files), "sql": len(sql_files),
                         "messages": len(msg_files), "docs": len(repo_docs)})

    java = index_java(repo, java_files)
    emit("index.java", {"classes": len(java.classes), "endpoints": len(java.endpoints)})
    nodes: list[Node] = list(java.nodes)
    edges: list[Edge] = list(java.edges)

    tnodes, tedges, tnames = index_templates(repo, tpl_files, java)
    nodes += tnodes; edges += tedges
    for mid, name, line in java.template_refs:
        if name in tnames:
            src_node = java.node_by_id.get(mid)
            rel = Rel.TESTS if src_node is not None and src_node.kind == Kind.TEST_METHOD else Rel.RENDERS
            edges.append(Edge(mid, tnames[name], rel, {"line": line}))
    emit("index.templates", {"templates": len(tnodes)})

    snodes, sedges = index_sql(repo, sql_files)
    nodes += snodes; edges += sedges
    mnodes, medges = index_messages(repo, msg_files)
    nodes += mnodes; edges += medges
    for nid, key, line in java.message_refs:
        edges.append(Edge(nid, f"msg:{key}", Rel.USES_MESSAGE, {"line": line}))
    nodes += index_config(repo, cfg_files)
    emit("index.resources", {"sql": len(snodes), "messages": len(mnodes)})

    # feature layer: one feature per business package, containing classes and endpoints
    feats: dict[str, Node] = {}
    for info in java.classes.values():
        if info.is_test or not info.package:
            continue
        seg = info.package.split(".")[-1]
        fid = f"feat:{seg}"
        if fid not in feats:
            feats[fid] = Node(fid, Kind.FEATURE, seg, None, None, None, words(seg), {"package": info.package})
        feats[fid].text += " " + words(info.name)
        edges.append(Edge(fid, info.id, Rel.CONTAINS))
    for ep in java.endpoints:
        seg = ep.props["package"].split(".")[-1]
        if f"feat:{seg}" in feats:
            edges.append(Edge(f"feat:{seg}", ep.id, Rel.CONTAINS))
    nodes += feats.values()

    # templates are rendered by endpoints' handlers; i18n labels of a template become
    # searchable text for the fields it binds, which bridges Japanese requirements
    by_id = {}
    for n in nodes:
        if n.id in by_id:
            by_id[n.id].props.update(n.props)
        else:
            by_id[n.id] = n
    msg_by_key = {n.name: n for n in by_id.values() if n.kind == Kind.MESSAGE}
    for n in by_id.values():
        if n.kind == Kind.FIELD:
            fname = n.name.split(".")[-1]
            m = msg_by_key.get(fname)
            if m:
                n.text += " " + " ".join(m.props["values"].values())
                edges.append(Edge(n.id, m.id, Rel.REFERENCES, {"via": "label"}))
        elif n.kind == Kind.COLUMN:
            m = msg_by_key.get(n.name.split(".")[-1].replace("_", ""))  # first_name -> firstName? keep simple
            if m:
                n.text += " " + " ".join(m.props["values"].values())

    # design documents (repo markdown + optional external design docs)
    vocab: dict[str, str] = {}
    for n in by_id.values():
        if n.kind in (Kind.CLASS, Kind.TABLE):
            vocab[n.name] = n.id
        elif n.kind == Kind.TEMPLATE:
            vocab[n.name] = n.id
            vocab[n.name.split("/")[-1]] = n.id
        elif n.kind == Kind.ENDPOINT:
            vocab[n.props["path"]] = n.id
        elif n.kind == Kind.FIELD:
            vocab[n.name] = n.id
        elif n.kind == Kind.COLUMN:
            vocab[n.name] = n.id
    dnodes, dedges = index_docs(repo, repo_docs, "", vocab)
    if docs_dir and os.path.isdir(docs_dir):
        ext = [os.path.relpath(os.path.join(d, f), docs_dir) for d, _, fs in os.walk(docs_dir)
               for f in fs if f.lower().endswith((".md", ".txt", ".markdown"))]
        dn2, de2 = index_docs(docs_dir, ext, "design:", vocab)
        dnodes += dn2; dedges += de2
    for n in dnodes:
        by_id[n.id] = n
    edges += dedges
    emit("index.docs", {"sections": len(dnodes)})

    # git history per file
    hist = git_history(repo)
    for n in by_id.values():
        if n.file and n.file in hist:
            n.props["history"] = hist[n.file][:6]
            n.props["churn"] = len(hist[n.file])

    valid = [e for e in edges if e.src in by_id and e.dst in by_id]
    dropped = len(edges) - len(valid)
    store.clear()
    store.upsert_nodes(by_id.values())
    store.upsert_edges(valid)
    store.flush()
    stats = store.stats()
    stats.update({"dropped_edges": dropped, "seconds": round(time.time() - t0, 2), "repo": repo,
                  "files": len(files)})
    emit("index.done", stats)
    return stats
