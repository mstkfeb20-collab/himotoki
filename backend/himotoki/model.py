"""Core graph model shared by the pipeline, the stores and the analysis engine.

Every edge carries provenance: `props["src"]` says how the relation was found
(compiler, parser, rule:<pack>, catalog, runtime, doc, heuristic) and
`props["conf"]` how much to trust it (0..1). The engine multiplies propagation
weights by `conf`, so a relation guessed from a naming convention spreads impact
less than one read from the database dictionary.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class Kind:
    REQUIREMENT = "Requirement"
    INTENT = "Intent"
    # feature / entry points
    ENDPOINT = "Endpoint"
    FEATURE = "Feature"
    JOBNET = "JobNet"
    JOB = "Job"
    CRON = "CronEntry"
    BATCH_JOB = "BatchJob"
    # code
    CLASS = "Class"
    METHOD = "Method"
    FIELD = "Field"
    TEMPLATE = "Template"
    SCRIPT = "Script"
    BEAN = "Bean"
    SQL_MAPPER = "SqlMapper"
    SQL_STATEMENT = "SqlStatement"
    MESSAGE = "MessageKey"
    CONFIG = "ConfigFile"
    XML_ELEMENT = "XmlElement"
    # data
    TABLE = "Table"
    COLUMN = "Column"
    SQL_FILE = "SqlFile"
    DB_OBJECT = "DbObject"          # view, function, procedure, trigger, sequence, synonym
    DATA_FILE = "DataFile"          # files exchanged between jobs
    LAYOUT_FIELD = "LayoutField"    # a field of a record layout (fixed-length / CSV / copybook)
    RUNTIME_SQL = "RuntimeSql"      # SQL observed at runtime that no code element explains
    # tests
    TEST_CLASS = "TestClass"
    TEST_METHOD = "TestMethod"
    # documents
    DOC = "DocSection"
    DOC_ENTITY = "DocEntity"        # a row / cell range in an Office design document


LAYER = {
    Kind.REQUIREMENT: "requirement", Kind.INTENT: "requirement",
    Kind.FEATURE: "feature", Kind.ENDPOINT: "feature", Kind.JOBNET: "feature", Kind.JOB: "feature",
    Kind.CRON: "feature", Kind.BATCH_JOB: "feature",
    Kind.CLASS: "code", Kind.METHOD: "code", Kind.FIELD: "code", Kind.TEMPLATE: "code",
    Kind.SCRIPT: "code", Kind.BEAN: "code", Kind.SQL_MAPPER: "code", Kind.SQL_STATEMENT: "code",
    Kind.MESSAGE: "code", Kind.CONFIG: "code", Kind.XML_ELEMENT: "code",
    Kind.TABLE: "data", Kind.COLUMN: "data", Kind.SQL_FILE: "data", Kind.DB_OBJECT: "data",
    Kind.DATA_FILE: "data", Kind.RUNTIME_SQL: "data", Kind.LAYOUT_FIELD: "data",
    Kind.TEST_CLASS: "test", Kind.TEST_METHOD: "test",
    Kind.DOC: "doc", Kind.DOC_ENTITY: "doc",
}

EDITABLE = {
    Kind.METHOD, Kind.FIELD, Kind.CLASS, Kind.TEMPLATE, Kind.COLUMN, Kind.TABLE, Kind.SQL_FILE,
    Kind.MESSAGE, Kind.CONFIG, Kind.TEST_CLASS, Kind.TEST_METHOD, Kind.SCRIPT, Kind.SQL_STATEMENT,
    Kind.BEAN, Kind.XML_ELEMENT, Kind.DB_OBJECT, Kind.JOB, Kind.JOBNET, Kind.CRON, Kind.DOC,
    Kind.DOC_ENTITY, Kind.LAYOUT_FIELD,
}


class Rel:
    CONTAINS = "CONTAINS"
    CALLS = "CALLS"
    REFERENCES = "REFERENCES"
    EXTENDS = "EXTENDS"
    IMPLEMENTED_BY = "IMPLEMENTED_BY"   # interface method -> implementation method
    INJECTS = "INJECTS"                 # field/bean -> injected bean/class
    MAPS_TO = "MAPS_TO"
    DEFINES = "DEFINES"
    POPULATES = "POPULATES"
    EXECUTES = "EXECUTES"               # method -> SQL statement
    READS = "READS"                     # SQL/program/job -> table, column, data file
    WRITES = "WRITES"
    RENDERS = "RENDERS"
    BINDS = "BINDS"
    USES_MESSAGE = "USES_MESSAGE"
    INCLUDES = "INCLUDES"
    HANDLES = "HANDLES"
    RUNS = "RUNS"                       # job -> script -> program
    PRECEDES = "PRECEDES"               # job -> following job
    OBSERVED_AS = "OBSERVED_AS"         # runtime SQL -> statement it matches
    TESTS = "TESTS"
    DESCRIBES = "DESCRIBES"
    DOC_CRUD = "DOC_CRUD"               # design-doc function -> table (C/R/U/D)
    CONFIGURES = "CONFIGURES"           # XML/config element -> code element
    FLOWS_TO = "FLOWS_TO"               # a screen / DTO field's value is written to or searched by a column


@dataclass
class Node:
    id: str
    kind: str
    name: str
    file: str | None = None
    line: int | None = None
    end_line: int | None = None
    text: str = ""
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def layer(self) -> str:
        return self.props.get("layer") or LAYER.get(self.kind, "code")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["layer"] = self.layer
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Node":
        return Node(id=d["id"], kind=d["kind"], name=d["name"], file=d.get("file"), line=d.get("line"),
                    end_line=d.get("end_line"), text=d.get("text", ""), props=dict(d.get("props") or {}))


@dataclass
class Edge:
    src: str
    dst: str
    rel: str
    props: dict[str, Any] = field(default_factory=dict)

    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.rel)

    @property
    def conf(self) -> float:
        return float(self.props.get("conf", 1.0))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
