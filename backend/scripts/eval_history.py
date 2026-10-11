"""History-based evaluation: does analysing a commit's message find the files it changed?

For each sampled non-merge commit, the repository is indexed at the parent commit
(a git worktree; facts are shared through the cache, so only changed files are
re-extracted), the commit message is analysed as the requirement, and the files
the commit actually changed are compared with the files of the candidates.

  python scripts/eval_history.py --repo /path/to/repo --data /tmp/ws --n 25 [--docs DIR] [--out result.json]

Recall is reported for "any candidate" (what a reviewer gets to look at) and for
candidates judged impacted. Commit messages are short and written after the fact,
so this is a pessimistic proxy for a requirement document.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from himotoki.workspace import Workspace  # noqa: E402

SKIP = re.compile(r"format|copyright|import statements|sonar|license|typo|version|upgrade|bump|deprecat|"
                  r"schema location|logback|log format|loglevel", re.I)
CODE = re.compile(r"/src/main/.*\.(java|xml|jsp|html|properties|sql|groovy|ftl)$")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def sample(repo, n, max_files, since=None, subject=None, skip=None, stride=1):
    out = []
    args = ["log", "--no-merges", "--format=%H\t%P\t%s"] + ([f"--since={since}"] if since else [])
    seen = 0
    for line in git(repo, *args).splitlines():
        sha, parents, subj = line.split("\t", 2)
        if " " in parents or not parents or SKIP.search(subj):
            continue
        if subject and not re.search(subject, subj):
            continue
        if skip and re.search(skip, subj, re.I):
            continue
        files = [f for f in git(repo, "diff-tree", "--no-commit-id", "--name-status", "-r", sha).splitlines()]
        changed = [f.split("\t")[-1] for f in files if f[:1] in "MD" and CODE.search(f)]
        if 2 <= len(changed) <= max_files:
            seen += 1
            if (seen - 1) % stride:
                continue          # spread the sample over the period instead of taking one burst of work
            out.append({"sha": sha, "parent": parents, "message": subj, "files": changed})
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--docs")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--max-files", type=int, default=15)
    ap.add_argument("--out")
    ap.add_argument("--since", help="only commits after this date")
    ap.add_argument("--subject", help="regex the commit subject must match (e.g. '^FINERACT-')")
    ap.add_argument("--skip", help="regex of subjects to leave out (tests-only, docs...)")
    ap.add_argument("--strip", default=r"^[A-Z][A-Z0-9]+-\d+\s*:\s*", help="regex removed from the message (ticket id)")
    ap.add_argument("--stride", type=int, default=1, help="take every n-th qualifying commit")
    ap.add_argument("--cache-graphs", help="keep each commit's graph here and reuse it (re-runs only re-analyse)")
    ap.add_argument("--resume", action="store_true", help="keep the rows already in --out and skip those commits")
    ap.add_argument("--max-commits", type=int, help="stop after this many new commits (run in slices)")
    ap.add_argument("--only", help="comma-separated short shas to evaluate (from an earlier sample)")
    a = ap.parse_args()
    ws = Workspace(a.data)
    rows = []
    if a.resume and a.out and os.path.exists(a.out):
        with open(a.out) as f:
            rows = json.load(f)["rows"]
    done = {r["sha"] for r in rows}
    # one checkout moved from parent to parent (a worktree per commit would need gigabytes)
    wt = os.path.join(os.path.abspath(a.data), "eval-checkout")
    if not os.path.isdir(wt):
        git(a.repo, "worktree", "prune")       # a data folder deleted by hand leaves a stale entry
        git(a.repo, "worktree", "add", "--detach", wt, "HEAD")
    commits = sample(a.repo, a.n, a.max_files, a.since, a.subject, a.skip, a.stride)
    if a.only:
        keep = a.only.split(",")
        commits = [c for c in commits if c["sha"][:8] in keep]
    if a.cache_graphs:
        os.makedirs(a.cache_graphs, exist_ok=True)
    commits = [c for c in commits if c["sha"][:8] not in done]
    if a.max_commits is not None:
        commits = commits[:a.max_commits]
    for c in commits:
        t0 = time.time()
        name = "eval"
        git(wt, "checkout", "-q", "--detach", c["parent"])
        gdir = ws.graph_dir(name)
        cached = os.path.join(a.cache_graphs, c["sha"][:12] + ".json.gz") if a.cache_graphs else None
        for f in ("runs.db", "runs.db-wal", "runs.db-shm", "knowledge.db"):   # every commit starts clean
            if os.path.exists(os.path.join(gdir, f)):
                os.remove(os.path.join(gdir, f))
        if cached and os.path.exists(cached) and name in ws.meta["graphs"]:
            with gzip.open(cached, "rb") as src, open(os.path.join(gdir, "graph.json"), "wb") as dst:
                shutil.copyfileobj(src, dst)
            st = {"seconds": 0.0}
        else:
            st = ws.index(wt, a.docs, name=name)
            if cached:
                with open(os.path.join(gdir, "graph.json"), "rb") as src, gzip.open(cached, "wb", 3) as dst:
                    shutil.copyfileobj(src, dst)
        ws.engines.pop(name, None)       # fresh engine: reloads the graph and an empty run store
        e = ws.engine(name)
        msg = re.sub(r"\(?#\d+\)?", "", c["message"]).strip()
        msg = re.sub(a.strip, "", msg).strip() if a.strip else msg
        rid = e.start_run(msg, background=False)
        run = e.state(rid)
        cand = run["candidates"].values()
        any_f = {x["file"] for x in cand if x.get("file")}
        imp_f = {x["file"] for x in cand if x.get("file") and x["verdict"] == "impacted"}
        # rank of each file = best rank of a candidate in it (the order a reviewer goes through the list)
        order = sorted(cand, key=lambda x: -(x.get("confidence") or x["score"]))
        rank = {}
        for i, x in enumerate(order, 1):
            if x.get("file") and x["file"] not in rank:
                rank[x["file"]] = i
        hit_any = [f for f in c["files"] if f in any_f]
        hit_imp = [f for f in c["files"] if f in imp_f]
        r = {"sha": c["sha"][:8], "message": msg, "changed": len(c["files"]), "found_any": len(hit_any),
             "found_impacted": len(hit_imp), "candidates": len(run["candidates"]),
             "candidate_files": len(any_f), "seconds_index": st["seconds"], "seconds_total": round(time.time() - t0, 1),
             "missed": [f for f in c["files"] if f not in any_f],
             "ranks": {f: rank.get(f) for f in c["files"]},
             **{f"found_top{k}": sum(1 for f in c["files"] if rank.get(f, 10**9) <= k) for k in (20, 50, 100)}}
        rows.append(r)
        print(f"{r['sha']} {r['found_any']}/{r['changed']} any, {r['found_impacted']} impacted, "
              f"{r['candidates']} cands, idx {r['seconds_index']}s  {msg[:60]}", flush=True)
        r["missed_kinds"] = sorted({os.path.splitext(f)[1] for f in r["missed"]})
        if a.out:     # written after every commit, so an interrupted run can --resume
            with open(a.out, "w") as f:
                json.dump({"rows": rows}, f, ensure_ascii=False, indent=1)
    tot = sum(r["changed"] for r in rows)
    summary = {
        "commits": len(rows), "changed_files": tot,
        "recall_any": round(sum(r["found_any"] for r in rows) / max(1, tot), 3),
        "recall_impacted": round(sum(r["found_impacted"] for r in rows) / max(1, tot), 3),
        "commits_with_any_hit": sum(1 for r in rows if r["found_any"]),
        **{f"recall_top{k}": round(sum(r[f"found_top{k}"] for r in rows) / max(1, tot), 3) for k in (20, 50, 100)},
        "commits_with_hit_top20": sum(1 for r in rows if r["found_top20"]),
        "median_candidates": sorted(r["candidates"] for r in rows)[len(rows) // 2] if rows else 0,
        "median_seconds_index": sorted(r["seconds_index"] for r in rows)[len(rows) // 2] if rows else 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
