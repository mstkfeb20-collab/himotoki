"""Design documents: Excel / Word (Office Open XML), Markdown and plain text.

Excel and Word tables are read generically: wherever a row contains enough
known header labels (rules/office.yaml) the rows below become DocEntity nodes
that reference tables, columns, screens, functions, jobs, programs, files and
messages by key. CRUD matrices (functions x tables marked C/R/U/D) are detected
in either orientation. Every entity keeps its "Sheet!A12" cell address so the
viewer can point back to the exact spot in the original document. Free text is
split by headings and emits `mention:` references for identifier-like tokens,
which the linker resolves against the names in the code graph.
"""
from __future__ import annotations

import io
import re
import unicodedata

from ..model import Kind, Rel
from ..pipeline.facts import FileFacts
from ..security import office_zip_ok
from ..text import words

NAME = "docs"
VERSION = "2"

_IDENT = re.compile(r"(?<![\w/.])([A-Za-z_][A-Za-z0-9_]{3,}(?:\.[A-Za-z_][A-Za-z0-9_]*)?|/[\w\-]+(?:/[\w\-{}.]+)+)(?![\w])")
_TABLE_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{1,40}$")


def _norm(s) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip().lower().replace(" ", "").replace("　", "")


def col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def mentions(f: FileFacts, nid: str, text: str, line=None, limit=60):
    seen = set()
    for m in _IDENT.finditer(text):
        tok = m.group(1)
        if tok.lower() in seen or tok.isdigit():
            continue
        seen.add(tok.lower())
        f.ref(nid, f"mention:{tok}", Rel.DESCRIBES, 0.9, line, via="mention", optional=True)
        if len(seen) >= limit:
            break


class _Office:
    def __init__(self, rules):
        cfg = rules.office or {}
        self.headers = {sem: {_norm(h) for h in hs} for sem, hs in (cfg.get("headers") or {}).items()}
        self.marks = {_norm(m) for m in cfg.get("crud_marks", ["C", "R", "U", "D", "CRUD"])}
        self.min_hits = int(cfg.get("min_header_hits", 2))

    def header_map(self, row):
        out = {}
        for i, v in enumerate(row):
            if v is None or str(v).strip() == "":
                continue
            nv = _norm(v)
            for sem, names in self.headers.items():
                if nv in names and sem not in out.values():
                    out[i] = sem
                    break
        return out if len(out) >= self.min_hits else {}

    def is_mark(self, v):
        return v is not None and _norm(v).upper().lower() in self.marks


def _context_table(grid, office: _Office):
    """Table name written in the sheet header area, e.g. 'テーブル名 | RESERVE'."""
    names = office.headers.get("table", set())
    for row in grid[:12]:
        for i, v in enumerate(row):
            if v is not None and _norm(v) in names:
                for w in row[i + 1:i + 4]:
                    if w is not None and _TABLE_LIKE.match(str(w).strip()):
                        return str(w).strip()
    return None


def process_grid(f: FileFacts, grid: list[list], sheet: str, office: _Office, parent: str):
    ctx_table = _context_table(grid, office)
    r = 0
    n = len(grid)
    while r < n:
        hm = office.header_map(grid[r])
        if hm:
            # a key/value line such as "テーブル名 | RESERVE | 論理名 | 予約" just above the real
            # header also matches; prefer the row with the most header labels nearby
            better = [office.header_map(grid[x]) for x in range(r + 1, min(n, r + 4))]
            if any(len(b) > len(hm) for b in better):
                r += 1
                continue
            r = _table_rows(f, grid, r, hm, sheet, ctx_table, parent)
            continue
        r += 1
    _crud_matrix(f, grid, sheet, office, parent)


def _table_rows(f, grid, hr, hm, sheet, ctx_table, parent):
    blank = 0
    r = hr + 1
    while r < len(grid):
        row = grid[r]
        vals = {sem: str(row[i]).strip() for i, sem in hm.items()
                if i < len(row) and row[i] not in (None, "") and sem != "misc"}
        if not vals:
            blank += 1
            if blank >= 2:
                break
            r += 1
            continue
        blank = 0
        cell = f"{sheet}!{col_letter(min(hm))}{r + 1}"
        nid = f"doc:{f.path}#{cell}"
        table = vals.get("table") or ctx_table
        label = vals.get("column") or vals.get("screen") or vals.get("function") or vals.get("job") or \
            vals.get("program") or vals.get("table") or vals.get("logical") or cell
        name = f"{table}.{label}" if table and vals.get("column") else label
        text = " ".join(str(v) for v in row if v not in (None, ""))[:600]
        f.node(nid, Kind.DOC_ENTITY, f"{sheet}: {name}", r + 1, r + 1, text, keys=[nid], cell=cell, sheet=sheet,
               fields=vals, doc=f.path)
        f.edge(parent, nid, Rel.CONTAINS, 1.0, "doc")
        ref = lambda key, conf=0.9: f.ref(nid, key, Rel.DESCRIBES, conf, r + 1, via="office-table", optional=True)
        if vals.get("column"):
            col = vals["column"].lower()
            if table:
                ref(f"column:{table.lower()}.{col}", 0.95)
            else:
                f.ref(nid, f"colany:{col}", Rel.DESCRIBES, 0.7, r + 1, via="office-table", optional=True)
        if table and not vals.get("column"):
            ref(f"table:{table.lower()}", 0.95)
        for sem, ns in (("screen", "screen"), ("function", "function"), ("job", "job"), ("message", "msg"),
                        ("url", "url"), ("file", "datafile")):
            if vals.get(sem):
                ref(f"{ns}:{vals[sem]}")
        if vals.get("program"):
            ref(f"mention:{vals['program']}", 0.85)
        mentions(f, nid, text, r + 1, limit=10)
        r += 1
    return r


_SQL_TYPES = {"char", "varchar", "varchar2", "nvarchar", "nchar", "number", "numeric", "decimal", "int", "integer",
              "bigint", "smallint", "date", "timestamp", "time", "text", "clob", "blob", "boolean", "float", "double",
              "real", "raw", "long", "serial"}


def _crud_matrix(f, grid, sheet, office, parent):
    def scan(rows):
        for hr, row in enumerate(rows[:40]):
            cols = [i for i, v in enumerate(row) if v is not None and _TABLE_LIKE.match(str(v).strip())
                    and _norm(v) not in office.marks and _norm(v) not in _SQL_TYPES
                    and _norm(v) not in {h for hs in office.headers.values() for h in hs}]
            if len(cols) < 3:
                continue
            body = rows[hr + 1:]
            marked = sum(1 for br in body for i in cols if i < len(br) and office.is_mark(br[i]))
            body_rows = sum(1 for br in body if any(i < len(br) and br[i] not in (None, "") for i in cols))
            if marked >= 3 and marked >= 0.3 * max(1, body_rows):
                return hr, cols
        return None

    for orient, rows in (("rows", grid), ("cols", [list(x) for x in zip(*grid)] if grid else [])):
        found = scan(rows)
        if not found:
            continue
        hr, cols = found
        tables = {i: str(rows[hr][i]).strip() for i in cols}
        for ri in range(hr + 1, len(rows)):
            row = rows[ri]
            marks = {tables[i]: _norm(row[i]).upper() for i in cols if i < len(row) and office.is_mark(row[i])}
            if not marks:
                continue
            label_cells = [str(v).strip() for v in row[:min(cols)] if v not in (None, "")]
            label = " ".join(label_cells) or f"row {ri + 1}"
            cell = f"{sheet}!{col_letter(0)}{ri + 1}" if orient == "rows" else f"{sheet}!{col_letter(ri)}1"
            nid = f"doc:{f.path}#{cell}#crud"
            keys = [nid] + [f"function:{c}" for c in label_cells[:2] if re.match(r"^[\w\-]{2,}$", c)]
            f.node(nid, Kind.DOC_ENTITY, f"{sheet}: {label} (CRUD)", ri + 1, ri + 1,
                   label + " " + " ".join(f"{t}:{m}" for t, m in marks.items()), keys=keys, cell=cell, sheet=sheet,
                   crud=marks, doc=f.path)
            f.edge(parent, nid, Rel.CONTAINS, 1.0, "doc")
            for t, m in marks.items():
                f.ref(nid, f"table:{t.lower()}", Rel.DOC_CRUD, 0.95, ri + 1, via="crud-matrix", ops=m)
        return


def extract_xlsx(path, data, rules, f: FileFacts):
    import openpyxl
    bad = office_zip_ok(data)
    if bad:
        f.errors.append(bad)
        return
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    office = _Office(rules)
    fid = f"file:{path}"
    for ws in wb.worksheets:
        grid = [list(row) for row in ws.iter_rows(values_only=True)]
        grid = [r for r in grid]
        sid = f"doc:{path}#{ws.title}"
        text = " ".join(str(v) for row in grid[:30] for v in row if v not in (None, ""))[:2000]
        f.node(sid, Kind.DOC, f"{path.rsplit('/', 1)[-1]} / {ws.title}", 1, len(grid), f"{ws.title} {text}",
               keys=[sid], sheet=ws.title, doc=path, title=ws.title)
        f.edge(fid, sid, Rel.CONTAINS, 1.0, "doc")
        process_grid(f, grid, ws.title, office, sid)


def extract_docx(path, data, rules, f: FileFacts):
    import docx
    bad = office_zip_ok(data)
    if bad:
        f.errors.append(bad)
        return
    d = docx.Document(io.BytesIO(data))
    office = _Office(rules)
    fid = f"file:{path}"
    sec_id, sec_title, buf, idx = None, path.rsplit("/", 1)[-1], [], 0

    def flush():
        nonlocal sec_id
        if buf or sec_id is None:
            sid = f"doc:{path}#s{idx}"
            body = "\n".join(buf)
            f.node(sid, Kind.DOC, sec_title, idx + 1, idx + 1, sec_title + " " + body[:4000], keys=[sid],
                   title=sec_title, doc=path)
            f.edge(fid, sid, Rel.CONTAINS, 1.0, "doc")
            mentions(f, sid, sec_title + " " + body)
            sec_id = sid
    for p in d.paragraphs:
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith(("heading", "見出し", "title")):
            flush()
            idx += 1
            sec_title, buf = p.text.strip() or sec_title, []
        elif p.text.strip():
            buf.append(p.text.strip())
    flush()
    for ti, t in enumerate(d.tables, 1):
        grid = [[c.text.strip() for c in row.cells] for row in t.rows]
        process_grid(f, grid, f"表{ti}", office, sec_id or fid)


_HEADING = re.compile(r"^(#{1,6})\s+(.+)$", re.M)


def extract_text(path, src, f: FileFacts):
    heads = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in _HEADING.finditer(src)] or [(0, 1, path.rsplit("/", 1)[-1])]
    stack = []
    for i, (pos, level, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(src)
        body = src[pos:end]
        stack = stack[:level - 1] + [title]
        line = src.count("\n", 0, pos) + 1
        sid = f"doc:{path}#{i}"
        f.node(sid, Kind.DOC, " / ".join(stack[-2:]), line, line + body.count("\n"), title + " " + body[:4000],
               keys=[sid], title=title, path=stack, doc=path)
        f.edge(f"file:{path}", sid, Rel.CONTAINS, 1.0, "doc")
        mentions(f, sid, body, line)


def extract(path: str, data: bytes, rules) -> FileFacts:
    f = FileFacts(path, NAME)
    f.node(f"file:{path}", Kind.CONFIG, path.rsplit("/", 1)[-1], 1, 1, words(path), keys=[f"file:{path}"],
           document=True)
    low = path.lower()
    try:
        if low.endswith((".xlsx", ".xlsm")):
            extract_xlsx(path, data, rules, f)
        elif low.endswith(".docx"):
            extract_docx(path, data, rules, f)
        else:
            extract_text(path, data.decode("utf-8", "replace"), f)
    except Exception as e:  # a broken document must not stop indexing
        f.errors.append(f"{type(e).__name__}: {e}")
    return f
