"""Message bundles and configuration files (.properties, .yml/.yaml)."""
from __future__ import annotations

import re

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import words

NAME = "props"
VERSION = "2"


def is_bundle(path: str) -> bool:
    b = path.rsplit("/", 1)[-1].lower()
    return b.endswith(".properties") and any(k in b for k in ("message", "label", "resource", "validation", "_ja",
                                                             "_en", "uilabel", "errors"))


def _decode(v: str) -> str:
    if "\\u" in v:
        try:
            return v.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except Exception:
            return v
    return v


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    src = data.decode("utf-8", "replace")
    fid = f"file:{path}"
    bundle = is_bundle(path)
    loc = re.search(r"_([a-z]{2}(?:_[A-Z]{2})?)\.properties$", path)
    locale = loc.group(1) if loc else "default"
    f.node(fid, Kind.CONFIG, path.rsplit("/", 1)[-1], 1, src.count("\n") + 1,
           words(path) + ("" if bundle else " " + re.sub(r"[^\w\s.]", " ", src)[:3000]),
           keys=[f"file:{path}"], locale=locale if bundle else None, bundle=bundle)
    if path.endswith((".yml", ".yaml")):
        stack = []
        for i, line in enumerate(src.splitlines(), 1):
            m = re.match(r"^(\s*)([\w.\-]+)\s*:\s*(.*)$", line)
            if not m or line.lstrip().startswith("#"):
                continue
            indent = len(m.group(1))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(2)))
            if m.group(3).strip():
                key = ".".join(s for _, s in stack)
                f.ref(fid, f"configdef:{key}", "DEFINES", 1.0, i, optional=True)
                f.payload.setdefault("config_keys", []).append([key, i])
        return f
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith(("#", "!")) or ("=" not in s and ":" not in s):
            continue
        k, _, v = re.split(r"(\s*[=:]\s*)", s, maxsplit=1) if re.search(r"[=:]", s) else (s, "", "")
        k, v = k.strip(), _decode(v.strip())
        if bundle:
            mid = f"msg:{k}"
            f.canonical(mid, Kind.MESSAGE, k, f"{words(k)} {v}", keys=[f"msg:{k}"],
                        values={locale: v}, files={path: i})
            f.edge(fid, mid, Rel.CONTAINS, line=i)
        else:
            f.payload.setdefault("config_keys", []).append([k, i])
    if not bundle:
        for k, i in f.payload.get("config_keys", []):
            cid = f"config:{k}"
            f.canonical(cid, Kind.CONFIG, k, words(k), keys=[cid], files={path: i})
            f.edge(fid, cid, Rel.CONTAINS, line=i)
    return f
