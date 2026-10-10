"""Intermediate format produced by every extractor, one per source file.

Extractors never look at other files. Whatever they cannot resolve locally they
emit as a *reference to a key* (`table:reserve`, `class:org.x.Foo`,
`sql:org.x.FooRepository.findById`, `view:reservetour/reserveForm` ...). Nodes
declare the keys they provide. The linker then joins references to providers
across all files and languages. Because facts depend only on the file's content
and the rule packs, they are cached by content hash and only changed files are
re-extracted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ref:
    src: str                 # node id inside this file
    key: str                 # "<namespace>:<value>"
    rel: str
    conf: float = 1.0
    line: int | None = None
    via: str = ""            # what produced it: rule name, "parser", "literal", ...
    optional: bool = False   # unresolved optional refs are not reported as gaps
    props: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        d = {"src": self.src, "key": self.key, "rel": self.rel, "conf": self.conf}
        if self.line is not None:
            d["line"] = self.line
        if self.via:
            d["via"] = self.via
        if self.optional:
            d["optional"] = True
        if self.props:
            d["props"] = self.props
        return d


@dataclass
class FileFacts:
    path: str
    extractor: str
    nodes: list[dict] = field(default_factory=list)   # Node dicts plus optional "keys": [...]
    edges: list[dict] = field(default_factory=list)   # resolved within the file
    refs: list[dict] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)   # language-specific data for global resolvers
    errors: list[str] = field(default_factory=list)

    def node(self, id: str, kind: str, name: str, line=None, end_line=None, text="", keys=None, **props):
        d = {"id": id, "kind": kind, "name": name, "file": self.path, "line": line, "end_line": end_line,
             "text": text, "props": props}
        if keys:
            d["keys"] = list(dict.fromkeys(keys))
        self.nodes.append(d)
        return id

    def canonical(self, id: str, kind: str, name: str, text="", keys=None, **props):
        """A node that several files may declare (a table, a message key...). It has
        no single file; declarations are merged by id."""
        d = {"id": id, "kind": kind, "name": name, "file": None, "line": None, "end_line": None,
             "text": text, "props": props, "canonical": True}
        if keys:
            d["keys"] = list(dict.fromkeys(keys))
        self.nodes.append(d)
        return id

    def edge(self, src, dst, rel, conf=1.0, src_kind="parser", **props):
        props.update({"conf": conf, "src": src_kind})
        self.edges.append({"src": src, "dst": dst, "rel": rel, "props": props})

    def ref(self, src, key, rel, conf=1.0, line=None, via="", optional=False, **props):
        self.refs.append(Ref(src, key, rel, conf, line, via, optional, props).to_dict())

    def to_dict(self):
        return {"path": self.path, "extractor": self.extractor, "nodes": self.nodes, "edges": self.edges,
                "refs": self.refs, "payload": self.payload, "errors": self.errors}

    @staticmethod
    def from_dict(d) -> "FileFacts":
        return FileFacts(d["path"], d["extractor"], d.get("nodes", []), d.get("edges", []), d.get("refs", []),
                         d.get("payload", {}), d.get("errors", []))
