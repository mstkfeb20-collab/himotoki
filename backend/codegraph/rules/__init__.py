"""Rule packs: declarative knowledge about frameworks and file formats.

The extractors and the linker are generic. Everything framework-specific —
which annotations mark an HTTP endpoint, how an XML mapper binds SQL to a Java
interface, which string patterns in a template refer to a message key, how a
job definition points at a script — lives in YAML packs under this directory
(and any directory listed in CODEGRAPH_RULES). Supporting a new framework means
adding a pack, not code.
"""
from __future__ import annotations

import glob
import hashlib
import os
from functools import lru_cache

import yaml

BUILTIN = os.path.dirname(__file__)


def _merge(dst: dict, src: dict) -> None:
    for k, v in src.items():
        if isinstance(v, dict):
            _merge(dst.setdefault(k, {}), v)
        elif isinstance(v, list):
            cur = dst.setdefault(k, [])
            for item in v:
                if item not in cur:
                    cur.append(item)
        else:
            dst[k] = v


class RuleSet:
    def __init__(self, packs: list[dict], digest: str):
        self.packs = packs
        self.digest = digest
        merged: dict = {}
        for p in packs:
            _merge(merged, {k: v for k, v in p.items() if k not in ("name", "description", "xml", "text",
                                                                     "propagation")})
        self.java = merged.get("java", {})
        self.office = merged.get("office", {})
        self.sql = merged.get("sql", {})
        self.xml = [dict(r, pack=p["name"]) for p in packs for r in p.get("xml", [])]
        self.text = [dict(r, pack=p["name"]) for p in packs for r in p.get("text", [])]
        self.propagation = [r for p in packs for r in p.get("propagation", [])]

    def names(self):
        return [p["name"] for p in self.packs]

    def ann(self, role: str):
        return self.java.get("annotations", {}).get(role)


@lru_cache(maxsize=4)
def load_rules(extra: str | None = None) -> RuleSet:
    dirs = [BUILTIN] + [d for d in (extra or os.environ.get("CODEGRAPH_RULES", "")).split(os.pathsep) if d]
    files = []
    for d in dirs:
        files += sorted(glob.glob(os.path.join(d, "*.yaml")))
    packs, h = [], hashlib.sha1()
    for f in files:
        raw = open(f, "rb").read()
        h.update(raw)
        data = yaml.safe_load(raw) or {}
        data.setdefault("name", os.path.splitext(os.path.basename(f))[0])
        packs.append(data)
    return RuleSet(packs, h.hexdigest()[:12])
