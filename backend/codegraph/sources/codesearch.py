"""Code search back-ends used by the keyword sweep (blind-spot check).

  local   `git grep` in the working copy (default; falls back to a Python scan)
  zoekt   a Zoekt server's JSON API          CODEGRAPH_CODESEARCH=zoekt:http://zoekt:6070
  gitlab  GitLab's blob search API           CODEGRAPH_CODESEARCH=gitlab:https://gitlab.example.com/api/v4:123
          (token in GITLAB_TOKEN)

Running the search on the source-control server side means the sweep covers
every branch and repository the server indexes, not just one checkout.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

TEXT_EXT = (".java", ".html", ".sql", ".properties", ".yml", ".yaml", ".xml", ".md", ".txt", ".js", ".ts", ".jsp",
            ".jspf", ".tag", ".ftl", ".groovy", ".kt", ".sh", ".ksh", ".bat", ".cmd", ".jmx", ".gradle", ".css",
            ".json", ".pls", ".pkb", ".pks", ".prc", ".fnc", ".trg", ".ddl", ".csv", ".jp1", ".ajs")


class LocalSearch:
    name = "local"

    def __init__(self, root: str):
        self.root = root

    def search(self, terms: list[str], limit: int = 5000) -> list[dict]:
        if not terms:
            return []
        args = ["git", "-C", self.root, "grep", "-n", "-I", "-i", "-w", "--no-color"]
        for t in terms:
            args += ["-e", t]
        try:
            out = subprocess.run(args, capture_output=True, text=True, errors="replace").stdout
            if out or os.path.isdir(os.path.join(self.root, ".git")):
                return self._parse(out, terms, limit)
        except FileNotFoundError:
            pass
        return self._scan(terms, limit)

    @staticmethod
    def _parse(out: str, terms, limit):
        rx = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9_])", re.I)
        hits = []
        for line in out.splitlines():
            path, _, rest = line.partition(":")
            ln, _, text = rest.partition(":")
            if not path.endswith(TEXT_EXT) or not ln.isdigit():
                continue
            m = rx.search(text)
            hits.append({"file": path, "line": int(ln), "term": m.group(1) if m else terms[0], "text": text.strip()[:160]})
            if len(hits) >= limit:
                break
        return hits

    def _scan(self, terms, limit):
        rx = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(re.escape(t) for t in terms) + r")(?![A-Za-z0-9_])", re.I)
        hits = []
        for d, dirs, fs in os.walk(self.root):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in ("node_modules", "target", "build")]
            for f in fs:
                if not f.endswith(TEXT_EXT):
                    continue
                p = os.path.join(d, f)
                if os.path.islink(p):
                    continue
                rel = os.path.relpath(p, self.root)
                try:
                    for i, line in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                        m = rx.search(line)
                        if m:
                            hits.append({"file": rel, "line": i, "term": m.group(1), "text": line.strip()[:160]})
                            if len(hits) >= limit:
                                return hits
                except OSError:
                    continue
        return hits


class ZoektSearch:
    name = "zoekt"

    def __init__(self, url: str, repo: str | None = None):
        self.url, self.repo = url.rstrip("/"), repo

    def search(self, terms, limit=5000):
        q = "(" + " or ".join(f"\\b{re.escape(t)}\\b" for t in terms) + ")" + (f" r:{self.repo}" if self.repo else "")
        body = json.dumps({"Q": q, "Opts": {"MaxDocDisplayCount": limit, "NumContextLines": 0}}).encode()
        req = urllib.request.Request(f"{self.url}/api/search", data=body, headers={"content-type": "application/json"})
        res = json.load(urllib.request.urlopen(req, timeout=30))
        hits = []
        for fm in (res.get("Result", {}).get("Files") or []):
            for lm in fm.get("LineMatches") or []:
                import base64
                text = base64.b64decode(lm.get("Line", "")).decode("utf-8", "replace")
                term = next((t for t in terms if re.search(rf"\b{re.escape(t)}\b", text, re.I)), terms[0])
                hits.append({"file": fm["FileName"], "line": lm["LineNumber"], "term": term, "text": text.strip()[:160]})
        return hits[:limit]


class GitLabSearch:
    name = "gitlab"

    def __init__(self, api: str, project: str, token: str | None):
        self.api, self.project, self.token = api.rstrip("/"), project, token

    def search(self, terms, limit=5000):
        hits = []
        for t in terms:
            url = f"{self.api}/projects/{urllib.parse.quote(self.project, safe='')}/search?scope=blobs&per_page=100&search={urllib.parse.quote(t)}"
            req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self.token} if self.token else {})
            for b in json.load(urllib.request.urlopen(req, timeout=30)):
                for i, line in enumerate((b.get("data") or "").splitlines()):
                    if re.search(rf"\b{re.escape(t)}\b", line, re.I):
                        hits.append({"file": b["path"], "line": (b.get("startline") or 1) + i, "term": t,
                                     "text": line.strip()[:160]})
        return hits[:limit]


def make_search(root: str | None):
    spec = os.environ.get("CODEGRAPH_CODESEARCH", "")
    if spec.startswith("zoekt:"):
        url, _, repo = spec[len("zoekt:"):].rpartition("@") if "@" in spec else (spec[len("zoekt:"):], "", None)
        return ZoektSearch(url, repo or None)
    if spec.startswith("gitlab:"):
        api, _, project = spec[len("gitlab:"):].rpartition(":")
        return GitLabSearch(api, project, os.environ.get("GITLAB_TOKEN"))
    return LocalSearch(root) if root else None
