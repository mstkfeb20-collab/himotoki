"""Database dictionary import (the database's own view of tables, columns,
views, stored code, triggers and their dependencies).

PostgreSQL is read live through `psql` (no driver needed). Oracle is read either
live through python-oracledb (thin mode) or from CSV files exported with
`scripts/export_oracle_catalog.sql`; both go through the same row handlers.
The result is stored as one FileFacts document ("catalog:<name>") that the
pipeline links together with the code, so a stored procedure that writes a
table becomes part of the same impact graph as the Java that reads it.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
from collections import defaultdict

from ..extractors.sql import crud_refs
from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import words

PG_QUERIES = {
    "columns": """select c.table_schema, c.table_name, c.column_name, c.data_type, c.is_nullable,
        coalesce(c.character_maximum_length::text, c.numeric_precision::text, ''),
        coalesce(col_description((quote_ident(c.table_schema)||'.'||quote_ident(c.table_name))::regclass, c.ordinal_position), ''),
        t.table_type
        from information_schema.columns c join information_schema.tables t
          on t.table_schema = c.table_schema and t.table_name = c.table_name
        where c.table_schema not in ('pg_catalog','information_schema')
          and not exists (select 1 from pg_depend d join pg_class k on k.oid = d.objid
                          join pg_namespace kn on kn.oid = k.relnamespace
                          where d.deptype = 'e' and k.relname = c.table_name and kn.nspname = c.table_schema)
        order by 1,2,c.ordinal_position""",
    "table_comments": """select n.nspname, c.relname, coalesce(obj_description(c.oid, 'pg_class'), '')
        from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where c.relkind in ('r','v','m','p') and n.nspname not in ('pg_catalog','information_schema')""",
    "views": """select v.schemaname, v.viewname, v.definition from pg_views v
        where v.schemaname not in ('pg_catalog','information_schema')
          and not exists (select 1 from pg_depend d join pg_class k on k.oid = d.objid
                          where d.deptype = 'e' and k.relname = v.viewname)""",
    "routines": """select n.nspname, p.proname, case p.prokind when 'p' then 'procedure' else 'function' end,
        replace(replace(p.prosrc, E'\\t', ' '), E'\\n', ' ')
        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname not in ('pg_catalog','information_schema') and p.prolang <> 12
          and not exists (select 1 from pg_depend d where d.objid = p.oid and d.deptype = 'e')""",
    "triggers": """select n.nspname, c.relname, t.tgname, p.proname
        from pg_trigger t join pg_class c on c.oid = t.tgrelid join pg_namespace n on n.oid = c.relnamespace
        join pg_proc p on p.oid = t.tgfoid where not t.tgisinternal""",
    "sequences": """select sequence_schema, sequence_name from information_schema.sequences""",
}


def _psql(dsn: str, sql: str) -> list[list[str]]:
    out = subprocess.run(["psql", dsn, "-X", "-A", "-t", "-F", "\x1f", "-R", "\x1e", "-c", sql],
                         capture_output=True, text=True, check=True).stdout
    return [r.split("\x1f") for r in out.split("\x1e") if r.strip()]


class CatalogBuilder:
    def __init__(self, name: str, dialect: str):
        self.f = FileFacts(f"catalog:{name}", "catalog")
        self.dialect = dialect
        self.name = name
        self.f.node(f"catalog:{name}", Kind.CONFIG, f"DB辞書 {name}", None, None, f"database catalog {name}",
                    keys=[f"catalog:{name}"], dialect=dialect)
        self.counts = defaultdict(int)

    def column(self, schema, table, col, dtype, nullable, length, comment, ttype="BASE TABLE"):
        t, c = table.lower(), col.lower()
        is_view = "VIEW" in (ttype or "").upper()
        if is_view:
            return   # a view's columns are described by the view itself
        self.f.canonical(f"table:{t}", Kind.TABLE, t, words(t), keys=[f"table:{t}"], catalog=self.name,
                         schema=schema)
        cid = f"col:{t}.{c}"
        dt = f"{dtype}({length})" if length and length not in ("", "None") else dtype
        self.f.canonical(cid, Kind.COLUMN, f"{t}.{c}", f"{words(t)} {words(c)} {comment or ''}",
                         keys=[f"column:{t}.{c}"], catalog_type=dt, nullable=nullable, catalog=self.name,
                         comment=comment or None)
        self.f.edge(f"table:{t}", cid, Rel.CONTAINS, 1.0, "catalog")
        self.counts["columns"] += 1

    def table_comment(self, schema, table, comment):
        if comment:
            t = table.lower()
            self.f.canonical(f"table:{t}", Kind.TABLE, t, comment, keys=[f"table:{t}"], comment=comment)

    def view(self, schema, name, definition):
        v = name.lower()
        oid = f"dbobj:view:{v}"
        self.f.node(oid, Kind.DB_OBJECT, f"view {v}", None, None, f"view {words(v)}", keys=[oid, f"table:{v}"],
                    object_type="view", catalog=self.name, definition=(definition or "")[:4000])
        if definition:
            crud_refs(self.f, oid, definition, via="catalog-view", dialect=self.dialect, conf=0.98)
        self.counts["views"] += 1

    def routine(self, schema, name, otype, source):
        r = name.lower()
        oid = f"dbobj:{otype.lower()}:{r}"
        self.f.node(oid, Kind.DB_OBJECT, f"{otype.lower()} {r}", None, None, f"{otype.lower()} {words(r)}",
                    keys=[oid, f"proc:{r}"], object_type=otype.lower(), catalog=self.name, source=(source or "")[:6000])
        if source:
            crud_refs(self.f, oid, source, via="catalog-source", dialect=self.dialect, conf=0.9)
        self.counts["routines"] += 1

    def trigger(self, schema, table, trig, func):
        oid = f"dbobj:trigger:{trig.lower()}"
        self.f.node(oid, Kind.DB_OBJECT, f"trigger {trig.lower()}", None, None, f"trigger {words(trig)}",
                    keys=[oid], object_type="trigger", catalog=self.name)
        self.f.ref(oid, f"table:{table.lower()}", Rel.READS, 1.0, via="catalog-trigger", ops="T")
        if func:
            self.f.ref(oid, f"proc:{func.lower()}", Rel.CALLS, 1.0, via="catalog-trigger", optional=True)
        self.counts["triggers"] += 1

    def sequence(self, schema, name):
        oid = f"dbobj:sequence:{name.lower()}"
        self.f.node(oid, Kind.DB_OBJECT, f"sequence {name.lower()}", None, None, words(name), keys=[oid],
                    object_type="sequence", catalog=self.name)

    def dependency(self, name, otype, ref_name, ref_type):
        """Oracle ALL_DEPENDENCIES row: exact compiler-maintained reference."""
        src = f"dbobj:{otype.lower()}:{name.lower()}"
        rt = ref_type.upper()
        if rt in ("TABLE", "VIEW"):
            self.f.ref(src, f"table:{ref_name.lower()}", Rel.READS, 1.0, via="catalog-dependency")
        elif rt in ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY"):
            self.f.ref(src, f"proc:{ref_name.lower()}", Rel.CALLS, 1.0, via="catalog-dependency", optional=True)
        elif rt == "SEQUENCE":
            self.f.ref(src, f"dbobj:sequence:{ref_name.lower()}", Rel.READS, 1.0, via="catalog-dependency",
                       optional=True)
        self.counts["dependencies"] += 1


def import_postgres(dsn: str, name: str = "postgres") -> FileFacts:
    b = CatalogBuilder(name, "postgres")
    for r in _psql(dsn, PG_QUERIES["columns"]):
        b.column(*r[:8])
    for r in _psql(dsn, PG_QUERIES["table_comments"]):
        b.table_comment(*r[:3])
    for r in _psql(dsn, PG_QUERIES["views"]):
        b.view(*r[:3])
    for r in _psql(dsn, PG_QUERIES["routines"]):
        b.routine(r[0], r[1], r[2], r[3] if len(r) > 3 else "")
    for r in _psql(dsn, PG_QUERIES["triggers"]):
        b.trigger(*r[:4])
    for r in _psql(dsn, PG_QUERIES["sequences"]):
        b.sequence(*r[:2])
    b.f.payload["catalog_counts"] = dict(b.counts)
    return b.f


ORACLE_QUERIES = {
    "ALL_TAB_COLUMNS": "select owner, table_name, column_name, data_type, nullable, data_length from all_tab_columns where owner = :owner",
    "ALL_COL_COMMENTS": "select owner, table_name, column_name, comments from all_col_comments where owner = :owner",
    "ALL_TAB_COMMENTS": "select owner, table_name, comments from all_tab_comments where owner = :owner",
    "ALL_VIEWS": "select owner, view_name, text from all_views where owner = :owner",
    "ALL_SOURCE": "select owner, name, type, line, text from all_source where owner = :owner order by name, type, line",
    "ALL_TRIGGERS": "select owner, table_name, trigger_name, trigger_body from all_triggers where owner = :owner",
    "ALL_DEPENDENCIES": "select owner, name, type, referenced_owner, referenced_name, referenced_type from all_dependencies where owner = :owner",
    "ALL_SEQUENCES": "select sequence_owner, sequence_name from all_sequences where sequence_owner = :owner",
}


def _oracle_rows_from_csv(folder: str, view: str):
    p = os.path.join(folder, f"{view}.csv")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8-sig", newline="") as fh:
        rd = csv.reader(fh)
        header = next(rd, None)
        return [row for row in rd]


def _oracle_rows_live(dsn: str, user: str, password: str, owner: str, view: str):
    import oracledb   # thin mode, no Oracle client needed
    with oracledb.connect(user=user, password=password, dsn=dsn) as con:
        cur = con.cursor()
        cur.execute(ORACLE_QUERIES[view], owner=owner.upper())
        return [[("" if v is None else str(v)) for v in row] for row in cur]


def import_oracle(name: str = "oracle", csv_dir: str | None = None, dsn: str | None = None, user: str | None = None,
                  password: str | None = None, owner: str | None = None) -> FileFacts:
    rows = (lambda v: _oracle_rows_from_csv(csv_dir, v)) if csv_dir else \
        (lambda v: _oracle_rows_live(dsn, user, password, owner or user, v))
    b = CatalogBuilder(name, "oracle")
    comments = {(r[1].lower(), r[2].lower()): r[3] for r in rows("ALL_COL_COMMENTS") if len(r) >= 4}
    view_names = {r[1].lower() for r in rows("ALL_VIEWS") if len(r) >= 2}
    for r in rows("ALL_TAB_COLUMNS"):
        if len(r) < 6:
            continue
        ttype = "VIEW" if r[1].lower() in view_names else "BASE TABLE"
        b.column(r[0], r[1], r[2], r[3], "YES" if r[4] in ("Y", "YES") else "NO", r[5],
                 comments.get((r[1].lower(), r[2].lower()), ""), ttype)
    for r in rows("ALL_TAB_COMMENTS"):
        if len(r) >= 3:
            b.table_comment(*r[:3])
    for r in rows("ALL_VIEWS"):
        if len(r) >= 3:
            b.view(*r[:3])
    src = defaultdict(list)
    for r in rows("ALL_SOURCE"):
        if len(r) >= 5 and r[2].upper() not in ("TRIGGER",):
            src[(r[1], r[2])].append(r[4])
    for (nm, typ), lines in src.items():
        b.routine("", nm, typ, "".join(lines))
    for r in rows("ALL_TRIGGERS"):
        if len(r) >= 3:
            b.trigger(r[0], r[1], r[2], None)
            if len(r) >= 4 and r[3]:
                crud_refs(b.f, f"dbobj:trigger:{r[2].lower()}", r[3], via="catalog-source", dialect="oracle", conf=0.9)
    for r in rows("ALL_DEPENDENCIES"):
        if len(r) >= 6 and r[5].upper() not in ("NON-EXISTENT",) and r[3].upper() not in ("SYS", "PUBLIC"):
            b.dependency(r[1], r[2], r[4], r[5])
    for r in rows("ALL_SEQUENCES"):
        if len(r) >= 2:
            b.sequence(*r[:2])
    b.f.payload["catalog_counts"] = dict(b.counts)
    return b.f


def save_source(data_dir: str, facts: FileFacts) -> str:
    d = os.path.join(data_dir, "sources")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, facts.path.replace(":", "_").replace("/", "_") + ".json")
    json.dump(facts.to_dict(), open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return p


def load_sources(data_dir: str) -> list[FileFacts]:
    d = os.path.join(data_dir, "sources")
    if not os.path.isdir(d):
        return []
    return [FileFacts.from_dict(json.load(open(os.path.join(d, f), encoding="utf-8")))
            for f in sorted(os.listdir(d)) if f.endswith(".json")]
