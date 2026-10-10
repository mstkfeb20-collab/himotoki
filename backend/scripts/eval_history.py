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
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from codegraph.workspace import Workspace  # noqa: E402

SKIP = re.compile(r"format|copyright|import statements|sonar|license|typo|version|upgrade|bump|deprecat|"
                  r"schema location|logback|log format|loglevel", re.I)
CODE = re.compile(r"/src/main/.*\.(java|xml|jsp|html|properties|sql|groovy|ftl)$")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def sample(repo, n, max_files):
    out = []
    for line in git(repo, "log", "--no-merges", "--format=%H\t%P\t%s").splitlines():
        sha, parents, subj = line.split("\t", 2)
        if " " in parents or not parents or SKIP.search(subj):
            continue
        files = [f for f in git(repo, "diff-tree", "--no-commit-id", "--name-status", "-r", sha).splitlines()]
        changed = [f.split("\t")[-1] for f in files if f[:1] in "MD" and CODE.search(f)]
        if 2 <= len(changed) <= max_files:
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
    a = ap.parse_args()
    ws = Workspace(a.data)
    rows = []
    for c in sample(a.repo, a.n, a.max_files):
        t0 = time.time()
        name = f"eval@{c['parent'][:8]}"
        st = ws.index(a.repo, a.docs, ref=c["parent"], name=name)
        e = ws.engine(name)
        msg = re.sub(r"\(?#\d+\)?", "", c["message"]).strip()
        rid = e.start_run(msg, background=False)
        run = e.state(rid)
        cand = run["candidates"].values()
        any_f = {x["file"] for x in cand if x.get("file")}
        imp_f = {x["file"] for x in cand if x.get("file") and x["verdict"] == "impacted"}
        hit_any = [f for f in c["files"] if f in any_f]
        hit_imp = [f for f in c["files"] if f in imp_f]
        r = {"sha": c["sha"][:8], "message": msg, "changed": len(c["files"]), "found_any": len(hit_any),
             "found_impacted": len(hit_imp), "candidates": len(run["candidates"]),
             "candidate_files": len(any_f), "seconds_index": st["seconds"], "seconds_total": round(time.time() - t0, 1),
             "missed": [f for f in c["files"] if f not in any_f]}
        rows.append(r)
        print(f"{r['sha']} {r['found_any']}/{r['changed']} any, {r['found_impacted']} impacted, "
              f"{r['candidates']} cands, idx {r['seconds_index']}s  {msg[:60]}", flush=True)
        ws.engines.pop(name, None)
    tot = sum(r["changed"] for r in rows)
    summary = {
        "commits": len(rows), "changed_files": tot,
        "recall_any": round(sum(r["found_any"] for r in rows) / max(1, tot), 3),
        "recall_impacted": round(sum(r["found_impacted"] for r in rows) / max(1, tot), 3),
        "commits_with_any_hit": sum(1 for r in rows if r["found_any"]),
        "median_candidates": sorted(r["candidates"] for r in rows)[len(rows) // 2] if rows else 0,
        "median_seconds_index": sorted(r["seconds_index"] for r in rows)[len(rows) // 2] if rows else 0,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"summary": summary, "rows": rows}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
