"""Source-control integration: branch snapshots, change sets and webhooks.

Each branch (or commit) is analysed in its own git worktree, so several
branches can be indexed side by side from one clone; the content-addressed fact
cache is shared, so a new branch only parses the files that differ.
"""
from __future__ import annotations

import os
import re
import subprocess

from ..security import check_ref


GIT_TIMEOUT = int(os.environ.get("HIMOTOKI_GIT_TIMEOUT", "900"))


def _git(repo, *args, check=True) -> str:
    # GIT_TERMINAL_PROMPT=0: a missing credential fails fast instead of hanging a worker
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check,
                          errors="replace", timeout=GIT_TIMEOUT, env=env).stdout


class GitRepo:
    def __init__(self, path: str, cache_dir: str):
        self.path = os.path.abspath(path)
        self.cache_dir = cache_dir

    def fetch(self, remote: str = "origin"):
        try:
            _git(self.path, "fetch", "--prune", "--", remote)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass   # offline / no remote: work with what we have

    def resolve(self, ref: str) -> str:
        check_ref(ref)              # refs reach git as arguments: no options, no "..", no odd characters
        for cand in (ref, f"origin/{ref}"):
            try:
                return _git(self.path, "rev-parse", "--verify", f"{cand}^{{commit}}").strip()
            except subprocess.CalledProcessError:
                continue
        raise ValueError(f"unknown ref: {ref}")

    def worktree(self, ref: str) -> str:
        """A checkout of `ref` under the cache directory (created or moved as needed)."""
        sha = self.resolve(ref)
        safe = re.sub(r"[^\w.\-]+", "_", ref)
        wt = os.path.join(self.cache_dir, "worktrees", safe)
        if os.path.isdir(os.path.join(wt, ".git")) or os.path.isfile(os.path.join(wt, ".git")):
            _git(wt, "checkout", "--detach", "--force", sha)
        else:
            os.makedirs(os.path.dirname(wt), exist_ok=True)
            _git(self.path, "worktree", "add", "--detach", "--force", wt, sha)
        return wt

    def changed_files(self, base: str, head: str) -> list[tuple[str, str]]:
        out = _git(self.path, "diff", "--name-status", "--no-renames", f"{base}", f"{head}")
        res = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                res.append((parts[0][0], parts[1]))
        return res

    def hunks(self, base: str, head: str, merge_base: bool = True) -> dict[str, list[tuple[int, int]]]:
        """Changed line ranges on the *base* side, per file (what the base graph knows).
        By default the change set is head relative to its merge base with base (what a
        merge request shows), so commits that landed on base meanwhile are not counted."""
        rng = [f"{base}...{head}"] if merge_base else [base, head]
        out = _git(self.path, "diff", "-U0", "--no-renames", "--no-color", *rng)
        res: dict[str, list[tuple[int, int]]] = {}
        cur = None
        for line in out.splitlines():
            if line.startswith("--- "):
                p = line[4:]
                cur = p[2:] if p.startswith("a/") else None
            elif line.startswith("+++ ") and cur is None:
                p = line[4:]
                cur = ("+" + p[2:]) if p.startswith("b/") else None     # added file
            elif line.startswith("@@") and cur:
                m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
                if m:
                    a, b = int(m.group(1)), int(m.group(2) or 1)
                    res.setdefault(cur, []).append((a, a + max(b, 1) - 1))
        return res

    def log_with_messages(self, rev_range: str = "HEAD", limit: int = 500, no_merges: bool = True) -> list[dict]:
        args = ["log", f"-n{limit}", "--date=short", "--format=%H%x1f%P%x1f%ad%x1f%s%x1f%b%x1e"]
        if no_merges:
            args.append("--no-merges")
        out = _git(self.path, *args, rev_range)
        res = []
        for rec in out.split("\x1e"):
            fields = rec.strip("\n").split("\x1f")
            if len(fields) < 5 or not fields[0].strip():
                continue
            sha = fields[0].strip()
            files = [f for f in _git(self.path, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).splitlines() if f]
            res.append({"sha": sha, "parents": fields[1].split(), "date": fields[2], "subject": fields[3],
                        "body": fields[4].strip(), "files": files})
        return res


def parse_webhook(headers: dict, payload: dict) -> dict | None:
    """Normalise GitLab / GitHub push and merge/pull-request events."""
    h = {k.lower(): v for k, v in headers.items()}
    gh = h.get("x-github-event")
    if gh == "push":
        return {"kind": "push", "branch": payload.get("ref", "").replace("refs/heads/", ""), "sha": payload.get("after")}
    if gh == "pull_request":
        pr = payload.get("pull_request", {})
        return {"kind": "review", "number": pr.get("number"), "base": pr.get("base", {}).get("ref"),
                "base_sha": pr.get("base", {}).get("sha"), "head": pr.get("head", {}).get("ref"),
                "head_sha": pr.get("head", {}).get("sha"), "repo": payload.get("repository", {}).get("full_name"),
                "title": pr.get("title", ""), "provider": "github"}
    kind = payload.get("object_kind")
    if kind == "push":
        return {"kind": "push", "branch": payload.get("ref", "").replace("refs/heads/", ""), "sha": payload.get("after")}
    if kind == "merge_request":
        a = payload.get("object_attributes", {})
        return {"kind": "review", "number": a.get("iid"), "base": a.get("target_branch"), "head": a.get("source_branch"),
                "head_sha": (a.get("last_commit") or {}).get("id"), "base_sha": None,
                "repo": payload.get("project", {}).get("id"), "title": a.get("title", ""), "provider": "gitlab"}
    return None


def post_review_comment(event: dict, body: str) -> str:
    """Post the impact summary on the merge/pull request when a token is configured."""
    import json
    import urllib.request
    if event.get("provider") == "gitlab" and os.environ.get("GITLAB_TOKEN") and os.environ.get("GITLAB_API"):
        url = f"{os.environ['GITLAB_API'].rstrip('/')}/projects/{event['repo']}/merge_requests/{event['number']}/notes"
        req = urllib.request.Request(url, data=json.dumps({"body": body}).encode(), method="POST",
                                     headers={"PRIVATE-TOKEN": os.environ["GITLAB_TOKEN"], "content-type": "application/json"})
    elif event.get("provider") == "github" and os.environ.get("GITHUB_TOKEN"):
        api = os.environ.get("GITHUB_API", "https://api.github.com").rstrip("/")
        url = f"{api}/repos/{event['repo']}/issues/{event['number']}/comments"
        req = urllib.request.Request(url, data=json.dumps({"body": body}).encode(), method="POST",
                                     headers={"authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                              "accept": "application/vnd.github+json", "content-type": "application/json"})
    else:
        return "skipped (no token configured)"
    with urllib.request.urlopen(req, timeout=30) as r:
        return f"posted ({r.status})"
