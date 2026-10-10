"""Pattern-rule extractor for text formats (templates, scripts, Groovy, JS...).

Each rule names file globs, the node kind for the file, and regex patterns that
emit references to keys. `set: name` stores a capture group in the file
context; `{ctx.name}` reuses it later (e.g. the form object of a JSP).
"""
from __future__ import annotations

import fnmatch
import re
from functools import lru_cache

from ..model import Kind
from ..pipeline.facts import FileFacts
from ..text import words

NAME = "text"
VERSION = "2"


@lru_cache(maxsize=512)
def _glob_rx(pattern: str):
    # "**/" matches zero or more directories
    rx = fnmatch.translate(pattern).replace(".*.*/", "(?:.*/)?")
    return re.compile(rx)


def matches(rule, path: str) -> bool:
    p = path if "/" in path else "./" + path
    return any(_glob_rx(g).match(path) or _glob_rx(g).match(p) for g in rule.get("files", []))


def rules_for(rules, path: str):
    return [r for r in rules.text if matches(r, path)]


@lru_cache(maxsize=1024)
def _rx(pattern):
    return re.compile(pattern, re.M)


def _view_keys(path: str, roots):
    keys = []
    stem = path.rsplit(".", 1)[0]
    for root in roots or []:
        i = path.find(root)
        if i >= 0:
            rel = path[i + len(root):]
            keys += [f"view:{rel.rsplit('.', 1)[0]}", f"view:{rel}"]
    i = path.find("webapp/")
    if i >= 0:
        rel = path[i + len("webapp"):]
        keys += [f"view:{rel}", f"view:{rel.lstrip('/')}"]
    keys.append(f"view:{stem.rsplit('/', 1)[-1]}") if not roots else None
    return keys


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    src = data.decode("utf-8", "replace")
    applicable = rules_for(rules, path)
    if not applicable:
        return f
    fid = f"file:{path}"
    first = applicable[0]
    nd = first.get("node", {})
    base = path.rsplit("/", 1)[-1]
    fmt = {"path": path, "basename": base, "stem": base.rsplit(".", 1)[0]}
    keys = [f"file:{path}"] + [k.format(**fmt) for k in nd.get("keys", [])]
    for r in applicable:
        keys += _view_keys(path, r.get("view_roots")) if r.get("view_roots") is not None else []
    visible = re.sub(r"<[^>]+>", " ", src) if nd.get("kind") == Kind.TEMPLATE else src
    visible = re.sub(r"\s+", " ", visible)[:3000]
    f.node(fid, nd.get("kind", Kind.CONFIG), path.rsplit("/", 1)[-1] if nd.get("kind") != Kind.TEMPLATE else
           _template_name(path, applicable), 1, src.count("\n") + 1, words(path) + " " + visible, keys=keys,
           rules=[r["name"] for r in applicable])
    line_starts = [0] + [m.end() for m in re.finditer("\n", src)]

    def line_of(pos):
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    events = []
    for r in applicable:
        for pi, pat in enumerate(r.get("patterns", [])):
            for m in _rx(pat["regex"]).finditer(src):
                events.append((m.start(), pi, r, pat, m))
    events.sort(key=lambda e: (e[0], e[1]))
    ctx: dict[str, str] = {}
    seen = set()
    for pos, _, r, pat, m in events:
        if pat.get("set"):
            ctx[pat["set"]] = m.group(1)
            continue
        key = pat.get("key")
        if not key:
            continue
        try:
            groups = {str(i): (m.group(i) or "") for i in range(1, (m.re.groups or 0) + 1)}
            out = re.sub(r"\{ctx\.(\w+)\}", lambda mm: ctx.get(mm.group(1), "\x00"), key)
            if "\x00" in out:
                continue
            out = re.sub(r"\{(\d+)\}", lambda mm: groups.get(mm.group(1), ""), out)
        except IndexError:
            continue
        line = line_of(pos)
        if pat.get("sql"):
            from .sql import crud_refs
            crud_refs(f, fid, out, line, via=r["name"], conf=float(pat.get("conf", 0.85)))
            continue
        k = (out, pat.get("rel"), line)
        if k in seen:
            continue
        seen.add(k)
        f.ref(fid, out, pat.get("rel", "REFERENCES"), float(pat.get("conf", 0.9)), line, via=r["name"],
              optional=bool(pat.get("optional", True)))
    return f


def _template_name(path, applicable):
    for r in applicable:
        for root in r.get("view_roots") or []:
            i = path.find(root)
            if i >= 0:
                return path[i + len(root):].rsplit(".", 1)[0]
    return path.rsplit("/", 1)[-1]
