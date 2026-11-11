"""Which column each parameter marker of a SQL statement stands for.

    markers("UPDATE M_CUST C SET CUST_NM = ?, UPD_DT = SYSDATE WHERE C.CUST_CD = ?")
    -> [{"name": None, "table": "M_CUST", "column": "CUST_NM", "usage": "write"},
        {"name": None, "table": "M_CUST", "column": "CUST_CD", "usage": "cond"}]

Markers: JDBC `?`, named `:name`, MyBatis `#{name}` / `#{name,jdbcType=..}`, iBATIS
`#name#`; in the order they appear. `usage` is `write` for INSERT values, UPDATE SET and
MERGE sources, `cond` for WHERE / ON / HAVING comparisons. A marker whose column cannot be
told (a function argument with no comparison, a procedure call) gets column None.
"""
from __future__ import annotations

import re

_MARK = re.compile(r"\?|(?<![:\w]):([A-Za-z_]\w*)|#\{\s*([\w.\[\]]+)[^}]*\}|#([\w.\[\]]+)(?::\w+)?#")
_KW = {"select", "from", "where", "and", "or", "not", "in", "is", "null", "like", "between", "on", "join", "inner",
       "left", "right", "outer", "full", "cross", "as", "set", "values", "into", "update", "insert", "delete", "merge",
       "using", "when", "matched", "then", "order", "group", "by", "having", "case", "else", "end", "exists", "distinct",
       "union", "all", "with", "nvl", "nvl2", "coalesce", "trim", "upper", "lower", "to_date", "to_char", "to_number",
       "substr", "substrb", "decode", "sysdate", "systimestamp", "rtrim", "ltrim", "lpad", "rpad", "trunc", "escape",
       "current_timestamp", "current_date", "cast", "rownum", "sum", "max", "min", "count", "avg", "replace", "concat",
       "dual", "asc", "desc", "limit", "offset", "for", "nowait", "returning", "date", "timestamp", "number", "varchar2"}
_IDENT = r"[A-Za-z_][\w$#]*"


def _norm(sql: str) -> tuple[str, list]:
    """Comments and string literals blanked (same length), markers listed with their spans."""
    s = re.sub(r"--[^\n]*", lambda m: " " * len(m.group(0)), sql)
    s = re.sub(r"/\*.*?\*/", lambda m: " " * len(m.group(0)), s, flags=re.S)
    s = re.sub(r"'(?:[^']|'')*'", lambda m: "'" + "x" * (len(m.group(0)) - 2) + "'", s)
    marks = []
    for m in _MARK.finditer(s):
        if m.group(0).startswith(":") and re.match(r"\d", m.group(1) or "0"):
            continue
        marks.append((m.start(), m.end(), m.group(1) or m.group(2) or m.group(3)))
    return s, marks


def _split_top(s: str) -> list[tuple[int, int]]:
    """Spans of comma-separated parts at parenthesis depth 0."""
    out, depth, start = [], 0, 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append((start, i))
            start = i + 1
    out.append((start, len(s)))
    return out


def _close(s: str, i: int) -> int:
    """Index of the parenthesis closing the one at i."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(s)


def _aliases(s: str) -> dict[str, str]:
    al = {}
    for m in re.finditer(rf"\b(?:from|join|update|into|using)\s+({_IDENT}(?:\.{_IDENT})?)(?:\s+(?:as\s+)?({_IDENT}))?", s, re.I):
        t = m.group(1).split(".")[-1].upper()
        if t.lower() in _KW or t == "(":
            continue
        al[t] = t
        a = m.group(2)
        if a and a.lower() not in _KW:
            al[a.upper()] = t
    # comma-separated FROM lists: FROM A X, B Y
    for m in re.finditer(r"\bfrom\s+(.*?)(?=\bwhere\b|\bgroup\b|\border\b|\bunion\b|\)|$)", s, re.I | re.S):
        for part in m.group(1).split(","):
            w = part.split()
            if w and re.fullmatch(_IDENT + r"(\." + _IDENT + ")?", w[0]) and w[0].lower() not in _KW:
                t = w[0].split(".")[-1].upper()
                al.setdefault(t, t)
                if len(w) > 1 and re.fullmatch(_IDENT, w[-1]) and w[-1].lower() not in _KW:
                    al.setdefault(w[-1].upper(), t)
    return al


def _tables(s: str) -> list[str]:
    seen = []
    for v in _aliases(s).values():
        if v not in seen:
            seen.append(v)
    return seen


def _col(ref: str, al: dict, tables: list[str]) -> tuple[str | None, str]:
    if "." in ref:
        a, c = ref.rsplit(".", 1)
        return al.get(a.upper(), a.upper()), c.upper()
    return (tables[0] if len(tables) == 1 else None), ref.upper()


def _cond_column(s: str, pos: int) -> str | None:
    """The column compared with the marker at pos: `COL = ?`, `COL LIKE ?`, `COL BETWEEN ? AND ?`,
    `COL IN (?, ?)`, `? = COL`, functions around either side."""
    before = s[:pos]
    # BETWEEN x AND ?
    m = re.search(rf"({_IDENT}(?:\.{_IDENT})?)\s+(?:not\s+)?between\s+[^()]*?(?:\([^()]*\))?[^()]*?\band\s*[\w(]*\s*$", before, re.I)
    if m and m.group(1).lower() not in _KW:
        return m.group(1)
    seg = re.split(r"\b(?:where|and|or|on|having|when)\b|[(,]\s*(?=(?:not\s+)?" + _IDENT + r"\s*(?:=|<|>|!|like|in|between|is)\b)",
                   before, flags=re.I)[-1]
    m = re.search(rf"({_IDENT}(?:\.{_IDENT})?)\s*\)?\s*(?:=|<>|!=|<=|>=|<|>|\blike\b|\bnot\s+like\b|\bin\b|\bnot\s+in\b|\bbetween\b)",
                  seg, re.I)
    cands = [x for x in re.findall(rf"({_IDENT}(?:\.{_IDENT})?)\s*\)*\s*(?:=|<>|!=|<=|>=|<|>|\blike\b|\bnot\s+like\b|\bin\b|\bnot\s+in\b|\bbetween\b)",
                                   seg, re.I) if x.split(".")[-1].lower() not in _KW]
    if cands:
        return cands[-1]
    if m and m.group(1).split(".")[-1].lower() not in _KW:
        return m.group(1)
    after = s[pos:pos + 120]
    m = re.match(rf"\s*\)*\s*(?:=|<>|!=|<=|>=|<|>)\s*(?:\w+\()?({_IDENT}(?:\.{_IDENT})?)", after)
    if m and m.group(1).split(".")[-1].lower() not in _KW:
        return m.group(1)
    return None


def markers(sql: str) -> list[dict]:
    if not sql:
        return []
    s, marks = _norm(sql)
    if not marks:
        return []
    al = _aliases(s)
    tables = _tables(s)
    target = {}          # marker start -> (table, column, usage)

    # INSERT INTO T (cols) VALUES (...) / SELECT ...
    for m in re.finditer(rf"\binsert\s+into\s+({_IDENT}(?:\.{_IDENT})?)\s*(?:{_IDENT}\s*)?\(", s, re.I):
        t = m.group(1).split(".")[-1].upper()
        o = m.end() - 1
        c = _close(s, o)
        cols = [s[o + 1 + a:o + 1 + b].strip().split(".")[-1].upper() for a, b in _split_top(s[o + 1:c])]
        rest = s[c + 1:]
        vm = re.match(r"\s*values\s*\(", rest, re.I)
        sm = re.match(r"\s*(?:select)\b", rest, re.I)
        if vm:
            vo = c + 1 + vm.end() - 1
            vc = _close(s, vo)
            base = vo + 1
            parts = _split_top(s[base:vc])
        elif sm:
            base = c + 1 + sm.end()
            stop = re.search(r"\bfrom\b", s[base:], re.I)
            parts = _split_top(s[base:base + (stop.start() if stop else len(s) - base)])
        else:
            continue
        for i, (a, b) in enumerate(parts):
            if i >= len(cols):
                break
            for ms, me, nm in marks:
                if base + a <= ms < base + b:
                    target[ms] = (t, cols[i], "write")

    # UPDATE T SET a = expr, b = expr / MERGE ... UPDATE SET
    for m in re.finditer(r"\bset\b", s, re.I):
        start = m.end()
        stop = re.search(r"\bwhere\b|\bwhen\b|\bfrom\b|;|$", s[start:], re.I)
        end = start + (stop.start() if stop else len(s) - start)
        um = list(re.finditer(rf"\b(?:update|merge\s+into)\s+({_IDENT}(?:\.{_IDENT})?)", s[:start], re.I))
        t = um[-1].group(1).split(".")[-1].upper() if um else (tables[0] if tables else None)
        for a, b in _split_top(s[start:end]):
            part = s[start + a:start + b]
            pm = re.match(rf"\s*\(?\s*({_IDENT}(?:\.{_IDENT})?)\s*=", part)
            if not pm:
                continue
            col = pm.group(1).split(".")[-1].upper()
            for ms, me, nm in marks:
                if start + a <= ms < start + b:
                    target.setdefault(ms, (t, col, "write"))

    # MERGE INTO T USING (SELECT ? AS COL ...) -> the column of the same name
    mm = re.search(rf"\bmerge\s+into\s+({_IDENT}(?:\.{_IDENT})?)", s, re.I)
    if mm:
        t = mm.group(1).split(".")[-1].upper()
        for ms, me, nm in marks:
            if ms in target:
                continue
            am = re.match(rf"\s*\)?\s*(?:as\s+)?({_IDENT})\b", s[me:me + 60], re.I)
            if am and am.group(1).lower() not in _KW and re.search(r"\busing\b", s[:ms], re.I) \
                    and not re.search(r"\bon\b", s[re.search(r"\busing\b", s[:ms], re.I).end():ms], re.I):
                target[ms] = (t, am.group(1).upper(), "write")

    # conditions
    for ms, me, nm in marks:
        if ms in target:
            continue
        ref = _cond_column(s, ms)
        if ref:
            t, c = _col(ref, al, tables)
            target[ms] = (t, c, "cond")

    out = []
    for ms, me, nm in marks:
        t, c, u = target.get(ms, (None, None, None))
        out.append({"name": nm, "table": t, "column": c, "usage": u})
    return out
