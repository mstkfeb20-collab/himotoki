"""Runtime evidence: SQL that actually ran in production / test environments.

Sources: PostgreSQL pg_stat_statements (live through psql) or any CSV with the
SQL text and execution count (Oracle V$SQL / AWR exports, APM exports).
Each distinct statement becomes a RuntimeSql node with table/column references.
The linker matches it to the code-side SqlStatement with the same operation and
table set (OBSERVED_AS). Statements that match nothing are exactly the dynamic
SQL / unknown callers that static analysis cannot see, and the impact engine
treats them as blind-spot candidates.
"""
from __future__ import annotations

import csv
import hashlib
import re

from ..extractors.sql import analyze, crud_refs
from ..model import Kind
from ..pipeline.facts import FileFacts
from ..text import words
from .catalog import _psql

PG_STATEMENTS = """select query, calls from pg_stat_statements s join pg_database d on d.oid = s.dbid
  where d.datname = current_database() and query !~* '^\\s*(begin|commit|rollback|set|show|select\\s+pg_|create|drop|alter|vacuum|analyze)'
  and query !~* 'pg_stat_statements|information_schema|pg_catalog'"""


def normalize(sql: str) -> str:
    s = re.sub(r"'(?:[^']|'')*'", "?", sql)
    s = re.sub(r"\$\d+|\b\d+(\.\d+)?\b", "?", s)
    s = re.sub(r":\w+", "?", s)
    return re.sub(r"\s+", " ", s).strip()


def build(name: str, rows, dialect: str | None = None, min_calls: int = 1) -> FileFacts:
    """rows: iterable of (sql_text, calls, extra-dict)"""
    f = FileFacts(f"runtime:{name}", "runtime")
    f.node(f"runtime:{name}", Kind.CONFIG, f"実行時SQL {name}", None, None, f"runtime sql {name}",
           keys=[f"runtime:{name}"])
    seen = {}
    for sql, calls, extra in rows:
        if not sql or not re.match(r"\s*(select|insert|update|delete|merge|with)\b", sql, re.I):
            continue
        if re.search(r"\b(pg_\w+|information_schema|all_\w+|dba_\w+|v\$\w+|user_\w+)\b", sql, re.I):
            continue   # dictionary / monitoring queries are not application SQL
        n = normalize(sql)
        h = hashlib.sha1(n.lower().encode()).hexdigest()[:12]
        calls = int(float(calls or 0))
        if h in seen:
            seen[h] += calls
            continue
        seen[h] = calls
        a = analyze(n, dialect)
        if not a["tables"]:
            continue
        nid = f"runtime:{name}:{h}"
        f.node(nid, Kind.RUNTIME_SQL, f"{a['op'] or 'SQL'} {', '.join(sorted(a['tables']))[:60]}", None, None,
               n[:1500] + " " + " ".join(words(t) for t in a["tables"]), keys=[nid], sql=n[:4000], op=a["op"],
               calls=calls, source=name, tables=sorted(a["tables"]), **(extra or {}))
        crud_refs(f, nid, n, via="runtime", dialect=dialect, conf=0.95)
    for nd in f.nodes:
        h = nd["id"].rsplit(":", 1)[-1]
        if nd["kind"] == Kind.RUNTIME_SQL and h in seen:
            nd["props"]["calls"] = seen[h]
    return f


def from_pg_stat_statements(dsn: str, name: str = "pg_stat_statements") -> FileFacts:
    rows = [(r[0], r[1], {}) for r in _psql(dsn, PG_STATEMENTS) if len(r) >= 2]
    return build(name, rows, "postgres")


def from_csv(path: str, name: str | None = None, sql_col: str | None = None, calls_col: str | None = None,
             dialect: str | None = None) -> FileFacts:
    import io
    from ..text import read_source
    with io.StringIO(read_source(path), newline="") as fh:        # UTF-8 or Shift_JIS exports
        rd = csv.DictReader(fh)
        cols = rd.fieldnames or []
        sc = sql_col or next((c for c in cols if c.lower() in ("sql_text", "sql_fulltext", "query", "sql", "statement")), cols[0])
        cc = calls_col or next((c for c in cols if c.lower() in ("executions", "calls", "count", "execs")), None)
        rows = [(r.get(sc), r.get(cc) if cc else 1, {k.lower(): v for k, v in r.items()
                                                       if k not in (sc, cc) and k.lower() in ("module", "action", "parsing_schema_name", "service")})
                for r in rd]
    return build(name or path.rsplit("/", 1)[-1].rsplit(".", 1)[0], rows, dialect)
