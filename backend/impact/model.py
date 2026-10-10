"""Core graph model shared by the indexer, the stores and the analysis engine."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


class Kind:
    """Node kinds. `layer` groups them into the four rings the viewer shows."""
    REQUIREMENT = "Requirement"
    INTENT = "Intent"
    ENDPOINT = "Endpoint"        # feature layer: an HTTP entry point
    FEATURE = "Feature"          # feature layer: a business area (package)
    CLASS = "Class"
    METHOD = "Method"
    FIELD = "Field"
    TEMPLATE = "Template"
    TABLE = "Table"
    COLUMN = "Column"
    SQL_FILE = "SqlFile"
    MESSAGE = "MessageKey"
    CONFIG = "ConfigFile"
    TEST_CLASS = "TestClass"
    TEST_METHOD = "TestMethod"
    DOC = "DocSection"


LAYER = {
    Kind.REQUIREMENT: "requirement",
    Kind.INTENT: "requirement",
    Kind.FEATURE: "feature",
    Kind.ENDPOINT: "feature",
    Kind.CLASS: "code",
    Kind.METHOD: "code",
    Kind.FIELD: "code",
    Kind.TEMPLATE: "code",
    Kind.MESSAGE: "code",
    Kind.CONFIG: "code",
    Kind.TABLE: "data",
    Kind.COLUMN: "data",
    Kind.SQL_FILE: "data",
    Kind.TEST_CLASS: "test",
    Kind.TEST_METHOD: "test",
    Kind.DOC: "doc",
}

# Kinds that represent something a developer actually edits (used for estimates).
EDITABLE = {
    Kind.METHOD, Kind.FIELD, Kind.CLASS, Kind.TEMPLATE, Kind.COLUMN, Kind.TABLE,
    Kind.SQL_FILE, Kind.MESSAGE, Kind.CONFIG, Kind.TEST_CLASS, Kind.TEST_METHOD,
}


class Rel:
    CONTAINS = "CONTAINS"          # class -> method/field, table -> column, feature -> class
    CALLS = "CALLS"                # method -> method
    REFERENCES = "REFERENCES"      # method/class -> class (type use), method -> field
    EXTENDS = "EXTENDS"
    MAPS_TO = "MAPS_TO"            # entity class -> table, field -> column
    DEFINES = "DEFINES"            # sql file -> table/column
    POPULATES = "POPULATES"        # data sql file -> table
    RENDERS = "RENDERS"            # handler method -> template
    BINDS = "BINDS"                # template -> field
    USES_MESSAGE = "USES_MESSAGE"  # template -> message key
    INCLUDES = "INCLUDES"          # template -> template (fragment)
    HANDLES = "HANDLES"            # endpoint -> handler method
    TESTS = "TESTS"                # test class/method -> class/method/endpoint
    DESCRIBES = "DESCRIBES"        # doc section -> any node


@dataclass
class Node:
    id: str
    kind: str
    name: str
    file: str | None = None          # repo-relative path
    line: int | None = None
    end_line: int | None = None
    text: str = ""                   # searchable text (identifiers split, comments, labels)
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def layer(self) -> str:
        return LAYER.get(self.kind, "code")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["layer"] = self.layer
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Node":
        return Node(
            id=d["id"], kind=d["kind"], name=d["name"], file=d.get("file"),
            line=d.get("line"), end_line=d.get("end_line"), text=d.get("text", ""),
            props=dict(d.get("props") or {}),
        )


@dataclass
class Edge:
    src: str
    dst: str
    rel: str
    props: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.rel)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
