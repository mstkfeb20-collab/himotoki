"""SQL: DDL/DML files, PL/SQL & PL/pgSQL objects, and SQL embedded elsewhere.

`crud_refs` is shared by every extractor that finds SQL text (XML mappers,
annotations, scripts): it turns a statement into table / column references with
C/R/U/D operations. sqlglot is used when it can parse the statement; otherwise a
tolerant regex fallback still recovers the tables (dynamic SQL fragments in
mappers often do not parse on their own).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..text import words

NAME = "sql"
VERSION = "2"

KEYWORDS = set("""select from where and or not in is null as on join left right inner outer full cross group by
order having union all distinct insert into values update set delete merge using when then else end case
exists between like limit offset fetch first rows only for nowait with returning asc desc count sum min max avg
coalesce nvl to_char to_date sysdate current_date current_timestamp now nextval currval dual true false
default primary key foreign references constraint unique index create table view sequence drop alter add
column varchar varchar2 char number integer int bigint smallint numeric decimal date timestamp text boolean
clob blob raw over partition row_number rownum lower upper trim substr substring length replace cast""".split())

_TABLE_RX = re.compile(r"\b(from|join|into|update|delete\s+from|merge\s+into|truncate\s+table)\s+([\w.\"$#]+)", re.I)


def clean_sql(sql: str) -> str:
    s = re.sub(r"#\{[^}]*\}", "?", sql)          # MyBatis parameters
    s = re.sub(r"\$\{[^}]*\}", "x", s)
    s = re.sub(r"#\w+#", "?", s)                 # iBATIS 2
    s = re.sub(r":\w+", "?", s) if re.search(r"(?<!:):[a-zA-Z_]\w*", s) and "::" not in s else s
    return s


def _norm_table(name: str) -> str:
    return name.strip('"`[]').split(".")[-1].lower()


_SELECT_INTO = re.compile(r"(\bSELECT\b(?:(?!\bFROM\b)[\s\S])*?)\bINTO\s+[:\w$.,\s]+?(?=\s+FROM\b|\s*;|\s*$)", re.I)


def analyze(sql: str, dialect: str | None = None) -> dict:
    """-> {"op": str, "tables": {name: set(ops)}, "columns": [(table|None, col, op)], "positional": bool}"""
    s = clean_sql(sql)
    # SELECT ... INTO var FROM ... (PL/SQL, PL/pgSQL, embedded SQL) assigns host variables, not tables
    s = _SELECT_INTO.sub(r"\1 ", s)
    res = {"op": "", "tables": {}, "columns": [], "positional": False, "parsed": False}
    try:
        trees = [t for t in sqlglot.parse(s, read=dialect, error_level=ErrorLevel.IGNORE) if t is not None]
    except Exception:
        trees = []
    for tree in trees:
        if not isinstance(tree, (exp.Select, exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Union, exp.With,
                                 exp.Subquery)) and not tree.find(exp.Select):
            continue
        res["parsed"] = True
        ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        alias = {}
        for t in tree.find_all(exp.Table):
            nm = _norm_table(t.name)
            if not nm or nm in ctes or nm == "dual":
                continue
            alias[t.alias_or_name.lower()] = nm
            alias[nm] = nm
            res["tables"].setdefault(nm, set()).add("R")
        target, op = None, "R"
        if isinstance(tree, exp.Insert):
            op = "C"
            tgt = tree.this
            if isinstance(tgt, exp.Schema):
                cols = [i.name.lower() for i in tgt.expressions if isinstance(i, exp.Identifier)]
                tgt = tgt.this
                res["columns"] += [(None, c, "C") for c in cols]
            else:
                res["positional"] = True
            if isinstance(tgt, exp.Table):
                target = _norm_table(tgt.name)
        elif isinstance(tree, (exp.Update, exp.Delete)):
            op = "U" if isinstance(tree, exp.Update) else "D"
            if isinstance(tree.this, exp.Table):
                target = _norm_table(tree.this.name)
        res["op"] = {"R": "SELECT", "C": "INSERT", "U": "UPDATE", "D": "DELETE"}[op]
        if target:
            res["tables"].setdefault(target, set()).add(op)
            if op == "C" and res["columns"]:
                res["columns"] = [(target, c, o) for _, c, o in res["columns"]]
        for c in tree.find_all(exp.Column):
            tq = c.table.lower() if c.table else None
            cop = "R"
            if op == "U" and isinstance(c.parent, exp.EQ) and c.parent.this is c and \
                    isinstance(c.parent.parent, exp.Update):
                cop = "U"
            res["columns"].append((alias.get(tq, tq) if tq else (target if op in ("U",) and cop == "U" else None),
                                   c.name.lower(), cop))
    if not res["tables"]:
        # tolerant fallback
        for kw, name in _TABLE_RX.findall(s):
            nm = _norm_table(name)
            if nm and nm not in KEYWORDS and not nm.startswith("("):
                k = kw.lower()
                o = "C" if k == "into" and re.search(r"\binsert\b", s, re.I) else \
                    "U" if k == "update" else "D" if k.startswith("delete") else "R"
                res["tables"].setdefault(nm, set()).add(o)
        if res["tables"]:
            ident = {w.lower() for w in re.findall(r"\b([A-Za-z_][\w$#]*)\b", s)} - KEYWORDS - set(res["tables"])
            res["columns"] = [(None, w, "R") for w in sorted(ident)]
            m = re.match(r"\s*(select|insert|update|delete|merge)", s, re.I)
            res["op"] = m.group(1).upper() if m else ""
    return res


def crud_refs(f: FileFacts, src: str, sql: str, line=None, via="sql", dialect=None, conf=0.95):
    a = analyze(sql, dialect)
    tables = sorted(a["tables"])
    for t, ops in a["tables"].items():
        rel = Rel.WRITES if ops & {"C", "U", "D"} else Rel.READS
        f.ref(src, f"table:{t}", rel, conf if a["parsed"] else conf * 0.8, line, via=via, ops="".join(sorted(ops)))
    seen = set()
    for t, c, op in a["columns"]:
        k = (t, c, op)
        if k in seen or c == "*":
            continue
        seen.add(k)
        rel = Rel.WRITES if op in ("C", "U") else Rel.READS
        if t:
            f.ref(src, f"column:{t}.{c}", rel, conf, line, via=via, optional=True, ops=op)
        elif tables:
            f.ref(src, f"colany:{c}", rel, conf * (0.95 if a["parsed"] else 0.7), line, via=via, optional=True,
                  ops=op, tables=tables)
    return a


def _dialect(path: str, default=None):
    p = path.lower()
    if "oracle" in p or "ora" in p.split("/"):
        return "oracle"
    if "postgres" in p or "pgsql" in p or "/pg/" in p:
        return "postgres"
    if "mysql" in p:
        return "mysql"
    return default


_OBJ_RX = re.compile(r"create\s+(?:or\s+replace\s+)?(?:editionable\s+)?(function|procedure|trigger|package\s+body|package|view|materialized\s+view|sequence|synonym|type)\s+(?:if\s+not\s+exists\s+)?([\w.\"$#]+)", re.I)
_CREATE_TABLE = re.compile(r"create\s+(?:global\s+temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"$#]+)\s*\(", re.I)
_COMMENT_ON = re.compile(r"comment\s+on\s+(table|column)\s+([\w.\"$#]+)\s+is\s+'((?:[^']|'')*)'", re.I)
_ALTER_ADD = re.compile(r"alter\s+table\s+([\w.\"$#]+)\s+add\s+(?:column\s+)?\(?\s*([\w\"$#]+)\s+([\w()]+)", re.I)
_CONSTRAINT = {"primary", "foreign", "unique", "constraint", "key", "index", "check"}


def _split_statements(src: str) -> list[tuple[int, str]]:
    """Split on ';' and '/' lines outside quotes and PL/SQL bodies (good enough for DDL scripts)."""
    out, cur, start, depth_plsql, q = [], [], 0, 0, False
    i, n, line = 0, len(src), 1
    stmt_line = 1
    lines = src.split("\n")
    buf = []
    for ln_no, ln in enumerate(lines, 1):
        stripped = ln.strip()
        if not buf and not stripped:
            continue
        if not buf:
            stmt_line = ln_no
        buf.append(ln)
        joined = "\n".join(buf)
        is_plsql = re.match(r"\s*create\s+(or\s+replace\s+)?(editionable\s+)?(function|procedure|trigger|package)", joined, re.I)
        if is_plsql:
            if stripped == "/" or re.search(r"\$\$\s*(language\s+\w+)?\s*;?\s*$", stripped, re.I) and joined.count("$$") >= 2:
                out.append((stmt_line, joined.rstrip("/\n ")))
                buf = []
        elif stripped.endswith(";") and joined.count("'") % 2 == 0:
            out.append((stmt_line, joined))
            buf = []
    if buf and "".join(buf).strip():
        out.append((stmt_line, "\n".join(buf)))
    return out


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    src = data.decode("utf-8", "replace")
    dialect = _dialect(path, (rules.sql or {}).get("default_dialect"))
    fid = f"file:{path}"
    f.node(fid, Kind.SQL_FILE, path.split("/")[-1] + (f" ({dialect})" if dialect else ""), 1, src.count("\n") + 1,
           words(path), keys=[f"file:{path}"], dialect=dialect)
    for line, stmt in _split_statements(src):
        body = re.sub(r"--[^\n]*", "", stmt)
        lead = " ".join(re.findall(r"--\s*([^\n]*)", stmt))[:200]       # "-- 出発地テーブル"
        m = _CREATE_TABLE.search(body)
        if m:
            _create_table(f, fid, m, body, stmt, line, lead)
            continue
        m = _COMMENT_ON.search(body)
        if m:
            what, name, text = m.group(1).lower(), m.group(2), m.group(3).replace("''", "'")
            parts = [p.strip('"').lower() for p in name.split(".")]
            if what == "table":
                f.canonical(f"table:{parts[-1]}", Kind.TABLE, parts[-1], text, keys=[f"table:{parts[-1]}"],
                            comment=text)
            elif len(parts) >= 2:
                t, c = parts[-2], parts[-1]
                f.canonical(f"col:{t}.{c}", Kind.COLUMN, f"{t}.{c}", text, keys=[f"column:{t}.{c}"], comment=text)
            continue
        m = _ALTER_ADD.search(body)
        if m:
            t, c = _norm_table(m.group(1)), m.group(2).strip('"').lower()
            cid = f"col:{t}.{c}"
            f.canonical(cid, Kind.COLUMN, f"{t}.{c}", f"{words(t)} {words(c)}", keys=[f"column:{t}.{c}"])
            f.edge(fid, cid, Rel.DEFINES, 1.0, "parser", line=line)
            f.edge(f"table:{t}", cid, Rel.CONTAINS)
            continue
        m = _OBJ_RX.search(body)
        if m:
            otype, name = re.sub(r"\s+", " ", m.group(1).lower()), _norm_table(m.group(2))
            oid = f"dbobj:{otype}:{name}"
            keys = [oid] + ([f"table:{name}"] if "view" in otype else []) + ([f"proc:{name}"] if otype in (
                "function", "procedure", "package", "package body") else [])
            f.node(oid, Kind.DB_OBJECT, f"{otype} {name}", line, line + stmt.count("\n"), f"{otype} {words(name)} {lead}",
                   keys=keys, object_type=otype)
            f.edge(fid, oid, Rel.DEFINES, 1.0, "parser", line=line)
            if otype == "trigger":
                on = re.search(r"\bon\s+([\w.\"$#]+)", body, re.I)
                if on:
                    f.ref(oid, f"table:{_norm_table(on.group(1))}", Rel.READS, 0.95, line, via="trigger", ops="T")
            inner = body[m.end():]
            if otype not in ("sequence", "synonym", "type"):
                crud_refs(f, oid, inner, line, via="db-object", dialect=dialect, conf=0.9)
            continue
        if re.match(r"\s*(insert|update|delete|merge)\b", body, re.I):
            a = analyze(body, dialect)
            for t, ops in a["tables"].items():
                if ops & {"C", "U", "D"}:
                    f.ref(fid, f"table:{t}", Rel.POPULATES, 1.0, line, via="dml", positional=a["positional"],
                          ops="".join(sorted(ops)))
            continue
        if re.match(r"\s*(select|with)\b", body, re.I):
            crud_refs(f, fid, body, line, via="query", dialect=dialect)
    return f


def _create_table(f, fid, m, body, stmt, line, lead):
    table = _norm_table(m.group(1))
    start = m.end()
    depth, i = 1, start
    while i < len(body) and depth:
        depth += {"(": 1, ")": -1}.get(body[i], 0)
        i += 1
    inner = body[start:i - 1]
    tid = f"table:{table}"
    f.canonical(tid, Kind.TABLE, table, f"{words(table)} {lead}", keys=[tid], comment=lead or None)
    f.edge(fid, tid, Rel.DEFINES, 1.0, "parser", line=line)
    raw_lines = stmt.split("\n")
    parts, depth, cur = [], 0, []
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    for p in parts:
        tok = p.strip().split()
        if not tok:
            continue
        cname = tok[0].strip('"`').lower()
        if cname in _CONSTRAINT:
            continue
        ctype = " ".join(tok[1:3])
        cline = line + next((k for k, rl in enumerate(raw_lines) if re.search(r"^\s*\"?" + re.escape(tok[0].strip('"')) + r"\b", rl, re.I)), 0)
        comment = ""
        rl = raw_lines[cline - line] if 0 <= cline - line < len(raw_lines) else ""
        cm = re.search(r"--\s*(.+)$", rl)
        if cm:
            comment = cm.group(1).strip()
        cid = f"col:{table}.{cname}"
        f.canonical(cid, Kind.COLUMN, f"{table}.{cname}", f"{words(table)} {words(cname)} {comment}",
                    keys=[f"column:{table}.{cname}"], types={f.path: ctype}, comment=comment or None)
        f.edge(tid, cid, Rel.CONTAINS)
        f.edge(fid, cid, Rel.DEFINES, 1.0, "parser", line=cline)
