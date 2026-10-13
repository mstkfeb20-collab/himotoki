"""Did a design document get read? A per-document check, without AI.

The extractor is run again on one document and the result is laid next to the
document itself, so a person can see — sheet by sheet, section by section — what
became searchable elements and what did not:

* tables found: the header row, which labels were understood (and as what), which
  were not (candidates for `rules/office.yaml`), how many rows were read;
* content left out: non-empty cells outside every table, sheets with no table,
  rows that look like a header the vocabulary does not know;
* what a parser cannot read: shapes and text boxes (screen layouts, flows),
  pictures, charts, connectors, cell comments, merged cells; in Word, text boxes,
  pictures and tables with no recognised header; sections cut at the text limit;
* links: with a graph, which code / table / screen each read row is linked to,
  and the rows that named something the graph does not have.
"""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections import defaultdict

from .extractors import docs
from .model import Kind, Rel
from .security import office_zip_ok

try:                                                      # untrusted XML: no entities, no DTD tricks
    from defusedxml import ElementTree as ET
except ImportError:                                       # pragma: no cover
    import xml.etree.ElementTree as ET

MAX_ROWS_SHOWN = 40
# what a recognised header label is read as (rules/office.yaml), in the words of the report
SEM_JA = {"table": "テーブル", "column": "項目（物理名）", "logical": "論理名", "type": "型", "misc": "補足",
          "screen": "画面", "function": "機能", "job": "ジョブ", "program": "プログラム", "file": "ファイル",
          "message": "メッセージ", "url": "URL"}
SECTION_LIMIT = 4000                                      # docs.py keeps this much text per section
LINK_RELS = [Rel.DESCRIBES, Rel.DOC_CRUD]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _texts(el) -> str:
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _label_as(office, label) -> str | None:
    """What a header label means to the extractor (every vocabulary, `misc` included)."""
    nv = docs._norm(label)
    return next((sem for sem, names in office.headers.items() if nv in names), None)


def _cell_row_col(cell: str) -> tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)$", cell.split("!")[-1])
    if not m:
        return 0, 0
    col = 0
    for ch in m.group(1):
        col = col * 26 + ord(ch) - 64
    return int(m.group(2)), col


# --------------------------------------------------------------------------- package parts
def _rels(z: zipfile.ZipFile, part: str) -> dict[str, str]:
    base, name = posixpath.split(part)
    rp = posixpath.join(base, "_rels", name + ".rels")
    if rp not in z.namelist():
        return {}
    out = {}
    for r in ET.fromstring(z.read(rp)):
        tgt = r.get("Target") or ""
        out[r.get("Id")] = tgt.lstrip("/") if tgt.startswith("/") else posixpath.normpath(posixpath.join(base, tgt))
    return out


def _drawing_stats(z: zipfile.ZipFile, part: str) -> dict:
    """Shapes, text boxes, pictures, charts and connectors on one drawing part."""
    st = {"shapes": 0, "shape_texts": [], "pictures": 0, "charts": 0, "connectors": 0}
    try:
        root = ET.fromstring(z.read(part))
    except (KeyError, ET.ParseError):
        return st
    for el in root.iter():
        name = _local(el.tag)
        if name == "sp":
            st["shapes"] += 1
            t = _texts(el).strip()
            if t:
                st["shape_texts"].append(t[:200])
        elif name == "pic":
            st["pictures"] += 1
        elif name == "graphicFrame":
            st["charts"] += 1
        elif name == "cxnSp":
            st["connectors"] += 1
    return st


def _xlsx_parts(data: bytes) -> dict[str, dict]:
    """Per sheet name: drawing statistics and the number of cell comments."""
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        except KeyError:
            return out
        wrels = _rels(z, "xl/workbook.xml")
        for s in wb.iter():
            if _local(s.tag) != "sheet":
                continue
            rid = next((v for k, v in s.attrib.items() if _local(k) == "id"), None)
            part = wrels.get(rid)
            if not part:
                continue
            info = {"shapes": 0, "shape_texts": [], "pictures": 0, "charts": 0, "connectors": 0, "comments": 0}
            for tgt in _rels(z, part).values():
                if "/drawings/" in tgt and tgt.endswith(".xml") and "vmlDrawing" not in tgt:
                    d = _drawing_stats(z, tgt)
                    for k in ("shapes", "pictures", "charts", "connectors"):
                        info[k] += d[k]
                    info["shape_texts"] += d["shape_texts"]
                elif "comments" in tgt and tgt.endswith(".xml"):
                    try:
                        info["comments"] += sum(1 for c in ET.fromstring(z.read(tgt)).iter()
                                                if _local(c.tag) == "comment")
                    except (KeyError, ET.ParseError):
                        pass
            out[s.get("name")] = info
    return out


def _docx_parts(data: bytes) -> dict:
    st = {"pictures": 0, "textboxes": [], "charts": 0}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        try:
            root = ET.fromstring(z.read("word/document.xml"))
        except KeyError:
            return st
        for el in root.iter():
            name = _local(el.tag)
            if name == "pic":
                st["pictures"] += 1
            elif name == "chart":
                st["charts"] += 1
            elif name == "txbxContent":
                t = " ".join(_texts(p) for p in el.iter() if _local(p.tag) == "p").strip()
                if t:
                    st["textboxes"].append(t[:200])
    return st


# --------------------------------------------------------------------------- links
def _links(facts, store) -> dict:
    """For every row / section read: what in the graph it is linked to."""
    refs = defaultdict(list)
    for r in facts.refs:
        refs[r["src"]].append(r["key"])
    out = {}
    for n in facts.nodes:
        if n["kind"] not in (Kind.DOC_ENTITY, Kind.DOC):
            continue
        targets = []
        if store is not None:
            for e in store.out_edges(n["id"], LINK_RELS):
                t = store.get(e.dst)
                if t is not None:
                    targets.append({"id": t.id, "name": t.name, "kind": t.kind, "file": t.file,
                                    "conf": round(e.conf, 2), "rel": e.rel})
        named = [k for k in refs.get(n["id"], []) if not k.startswith("mention:")]
        out[n["id"]] = {"targets": targets, "named": named, "mentions": sum(1 for k in refs.get(n["id"], [])
                                                                             if k.startswith("mention:"))}
    return out


# --------------------------------------------------------------------------- per format
def _check_xlsx(path, data, rules, facts, links) -> dict:
    import openpyxl
    office = docs._Office(rules)
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)      # not read-only: merged cells
    parts = _xlsx_parts(data)
    ents_by_sheet = defaultdict(list)
    for n in facts.nodes:
        if n["kind"] == Kind.DOC_ENTITY:
            ents_by_sheet[n["props"].get("sheet")].append(n)
    sheets = []
    for ws in wb.worksheets:
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        ents = ents_by_sheet.get(ws.title, [])
        covered_rows, covered_cols = set(), set()
        tables = defaultdict(list)
        crud = []
        for n in ents:
            if n["props"].get("crud"):
                crud.append(n)
                row, col = _cell_row_col(n["props"]["cell"])
                if row == 1 and col > 1:
                    covered_cols.add(col)
                else:
                    covered_rows.add(row)
            else:
                tables[n["props"].get("header_row")].append(n)
                covered_rows.add(n["line"])
        if crud:
            # the header line of a matrix carries the table names
            covered_rows.update(r + 1 for r, row in enumerate(grid[:40])
                                if sum(1 for v in row if v not in (None, "") and
                                       docs._TABLE_LIKE.match(str(v).strip())) >= 3)
        table_out = []
        for hr, rows in sorted(tables.items(), key=lambda kv: kv[0] or 0):
            covered_rows.add(hr)
            header = grid[hr - 1] if hr and hr <= len(grid) else []
            hm = office.header_map(header)
            columns = [{"cell": f"{docs.col_letter(i)}{hr}", "label": str(v).strip(),
                        "as": hm.get(i) or _label_as(office, v), "used": i in hm,
                        "as_ja": SEM_JA.get(hm.get(i) or _label_as(office, v) or "")}
                       for i, v in enumerate(header) if v not in (None, "")]
            sample = []
            for n in rows[:MAX_ROWS_SHOWN]:
                lk = links.get(n["id"], {})
                sample.append({"id": n["id"], "cell": n["props"]["cell"], "name": n["name"].split(": ", 1)[-1],
                               "fields": n["props"].get("fields", {}), "targets": lk.get("targets", []),
                               "named": lk.get("named", [])})
            table_out.append({"header_row": hr, "columns": columns,
                              "unrecognised": [c["label"] for c in columns if not c["as"]],
                              "rows": len(rows), "linked": sum(1 for n in rows if links.get(n["id"], {}).get("targets")),
                              "sample": sample})
        nonempty, uncovered = 0, []
        for r, row in enumerate(grid, 1):
            cells = [(f"{docs.col_letter(c - 1)}{r}", str(v).strip()) for c, v in enumerate(row, 1)
                     if v not in (None, "") and str(v).strip()]
            nonempty += len(cells)
            if r in covered_rows:
                continue
            rest = [(a, t[:80]) for a, t in cells if _cell_row_col(a)[1] not in covered_cols]
            if rest:
                uncovered.append({"row": r, "cells": rest[:12]})
        uncovered_cells = sum(len(u["cells"]) for u in uncovered)
        # rows of several short labels that the vocabulary did not take as a header
        candidates = []
        if not table_out:
            for r, row in enumerate(grid[:60], 1):
                labels = [str(v).strip() for v in row if v not in (None, "") and 0 < len(str(v).strip()) <= 20
                          and not re.fullmatch(r"[\d.,/:\- ]+", str(v).strip())]
                if len(labels) >= 3:
                    candidates.append({"row": r, "labels": labels[:12]})
                if len(candidates) >= 3:
                    break
        p = parts.get(ws.title, {})
        merged = len(getattr(ws.merged_cells, "ranges", []))
        sheets.append({
            "name": ws.title, "rows": len(grid), "cells": nonempty, "merged": merged,
            "tables": table_out, "crud_rows": len(crud),
            "crud_tables": sorted({t for n in crud for t in (n["props"].get("crud") or {})}),
            "read_cells": nonempty - uncovered_cells, "uncovered": uncovered[:MAX_ROWS_SHOWN],
            "uncovered_rows": len(uncovered), "uncovered_cells": uncovered_cells,
            "header_candidates": candidates,
            "shapes": p.get("shapes", 0), "shape_texts": p.get("shape_texts", [])[:30], "pictures": p.get("pictures", 0),
            "charts": p.get("charts", 0), "connectors": p.get("connectors", 0), "comments": p.get("comments", 0),
        })
    return {"sheets": sheets}


def _check_docx(path, data, rules, facts, links) -> dict:
    import docx as pydocx
    d = pydocx.Document(io.BytesIO(data))
    office = docs._Office(rules)
    sections = []
    for n in facts.nodes:
        if n["kind"] == Kind.DOC:
            body = n["text"][len(n["props"].get("title") or ""):].strip()
            lk = links.get(n["id"], {})
            sections.append({"id": n["id"], "title": n["props"].get("title") or n["name"], "chars": len(body),
                             "cut": len(body) >= SECTION_LIMIT - 2, "targets": lk.get("targets", []),
                             "mentions": lk.get("mentions", 0)})
    ents = defaultdict(list)
    for n in facts.nodes:
        if n["kind"] == Kind.DOC_ENTITY:
            ents[n["props"].get("sheet")].append(n)
    tables = []
    for ti, t in enumerate(d.tables, 1):
        grid = [[c.text.strip() for c in row.cells] for row in t.rows]
        rows = ents.get(f"表{ti}", [])
        hr = rows[0]["props"].get("header_row") if rows and not rows[0]["props"].get("crud") else None
        header = grid[hr - 1] if hr else (grid[0] if grid else [])
        hm = office.header_map(header) if hr else {}
        tables.append({
            "name": f"表{ti}", "rows": len(grid), "read_rows": len(rows),
            "linked": sum(1 for n in rows if links.get(n["id"], {}).get("targets")),
            "header": [{"label": v, "as": hm.get(i) or _label_as(office, v),
                        "as_ja": SEM_JA.get(hm.get(i) or _label_as(office, v) or "")} for i, v in enumerate(header) if v],
            "recognised": bool(rows),
            "sample": [{"id": n["id"], "cell": n["props"]["cell"], "name": n["name"].split(": ", 1)[-1],
                        "fields": n["props"].get("fields", {}), "targets": links.get(n["id"], {}).get("targets", []),
                        "named": links.get(n["id"], {}).get("named", [])} for n in rows[:MAX_ROWS_SHOWN]]})
    parts = _docx_parts(data)
    headings = sum(1 for p in d.paragraphs if p.style is not None and
                   (p.style.name or "").lower().startswith(("heading", "見出し", "title")))
    return {"sections": sections, "tables": tables, "pictures": parts["pictures"], "charts": parts["charts"],
            "textboxes": parts["textboxes"][:30], "headings": headings,
            "paragraphs": sum(1 for p in d.paragraphs if p.text.strip())}


def _check_text(path, facts, links) -> dict:
    sections = []
    for n in facts.nodes:
        if n["kind"] == Kind.DOC:
            lk = links.get(n["id"], {})
            body_len = n["props"].get("chars") or len(n["text"])
            sections.append({"id": n["id"], "title": n["props"].get("title") or n["name"], "chars": body_len,
                             "cut": len(n["text"]) >= SECTION_LIMIT, "targets": lk.get("targets", []),
                             "mentions": lk.get("mentions", 0)})
    return {"sections": sections, "tables": []}


# --------------------------------------------------------------------------- findings
def _findings(rep: dict) -> list[dict]:
    """Plain statements of what was not read, most important first."""
    out = []
    graph = (rep.get("summary") or {}).get("graph")
    for s in rep.get("sheets", []):
        drawn = s["shapes"] + s["pictures"] + s["charts"]
        if not s["tables"] and not s["crud_rows"] and s["cells"]:
            msg = f"シート「{s['name']}」は表として読めていません（文字のあるセル {s['cells']}）"
            if s["header_candidates"]:
                msg += "。見出しらしい行: " + " / ".join("・".join(c["labels"][:6]) for c in s["header_candidates"][:2])
            out.append({"level": "warn", "where": s["name"], "message": msg})
        elif s["uncovered_cells"] and s["cells"] and s["uncovered_cells"] / s["cells"] > 0.3:
            out.append({"level": "info", "where": s["name"],
                        "message": f"シート「{s['name']}」の文字のあるセルのうち {s['uncovered_cells']} 個"
                                   f"（{round(100 * s['uncovered_cells'] / s['cells'])}%）は表の外で、検索の対象になっていません"})
        if drawn:
            out.append({"level": "warn" if s["shape_texts"] else "info", "where": s["name"],
                        "message": f"シート「{s['name']}」に図形 {s['shapes']}・画像 {s['pictures']}・グラフ {s['charts']}"
                                   f"・線 {s['connectors']} があり、中身は読んでいません"
                                   + (f"（図形の文字 {sum(len(t) for t in s['shape_texts'])} 文字）" if s["shape_texts"] else "")})
        for t in s["tables"]:
            if t["unrecognised"]:
                out.append({"level": "info", "where": s["name"],
                            "message": f"シート「{s['name']}」{t['header_row']} 行目の表で、意味の分からない見出し: "
                                       + "・".join(t["unrecognised"][:8])
                                       + "（rules/office.yaml の headers に足すと、その列も読めます）"})
            if graph and t["rows"] and t["linked"] < t["rows"]:
                out.append({"level": "info", "where": s["name"],
                            "message": f"シート「{s['name']}」{t['header_row']} 行目の表: {t['rows']} 行のうち "
                                       f"{t['rows'] - t['linked']} 行がコードやテーブルに結びついていません"})
        if s["comments"]:
            out.append({"level": "info", "where": s["name"],
                        "message": f"シート「{s['name']}」のセルのコメント {s['comments']} 件は読んでいません"})
    for t in rep.get("tables", []):
        if not t["recognised"] and t["rows"] > 1:
            out.append({"level": "warn", "where": t["name"],
                        "message": f"{t['name']}（{t['rows']} 行）は見出しを認識できず、表として読めていません。1 行目: "
                                   + "・".join(h["label"] for h in t["header"][:8])})
        elif graph and t.get("read_rows") and t["linked"] < t["read_rows"]:
            out.append({"level": "info", "where": t["name"],
                        "message": f"{t['name']}: {t['read_rows']} 行のうち {t['read_rows'] - t['linked']} 行が結びついていません"})
    if rep.get("textboxes"):
        out.append({"level": "warn", "where": "テキストボックス",
                    "message": f"テキストボックス {len(rep['textboxes'])} 個の文字は読んでいません"})
    if rep.get("pictures") or rep.get("charts"):
        out.append({"level": "info", "where": "画像",
                    "message": f"画像 {rep.get('pictures', 0)}・グラフ {rep.get('charts', 0)} の中身は読んでいません"})
    cut = [s["title"] for s in rep.get("sections", []) if s["cut"]]
    if cut:
        out.append({"level": "info", "where": "節",
                    "message": f"{len(cut)} 節は {SECTION_LIMIT} 文字を超え、検索には先頭だけを使っています: " + "・".join(cut[:5])})
    if rep.get("errors"):
        out.insert(0, {"level": "error", "where": "読み込み", "message": "読み込みエラー: " + " / ".join(rep["errors"])})
    order = {"error": 0, "warn": 1, "info": 2}
    return sorted(out, key=lambda f: order[f["level"]])


def check_document(path: str, data: bytes, rules, store=None) -> dict:
    """`path` is the name the document has in the graph (e.g. `design:画面設計書.xlsx`),
    so read rows can be matched with the graph's links."""
    low = path.lower()
    rep = {"path": path, "format": "xlsx" if low.endswith((".xlsx", ".xlsm")) else
           "docx" if low.endswith(".docx") else "text", "errors": []}
    if rep["format"] in ("xlsx", "docx"):
        bad = office_zip_ok(data)
        if bad:
            rep["errors"].append(bad)
            rep["findings"] = _findings(rep)
            return rep
    facts = docs.extract(path, data, rules)
    rep["errors"] += facts.errors
    links = _links(facts, store)
    try:
        if rep["format"] == "xlsx":
            rep.update(_check_xlsx(path, data, rules, facts, links))
        elif rep["format"] == "docx":
            rep.update(_check_docx(path, data, rules, facts, links))
        else:
            rep.update(_check_text(path, facts, links))
    except Exception as e:                              # a damaged document still gets a report
        rep["errors"].append(f"{type(e).__name__}: {str(e)[:200]}")
    rows = [n for n in facts.nodes if n["kind"] == Kind.DOC_ENTITY]
    secs = [n for n in facts.nodes if n["kind"] == Kind.DOC]
    rep["summary"] = {
        "rows_read": len(rows), "sections": len(secs),
        "linked_rows": sum(1 for n in rows if links.get(n["id"], {}).get("targets")),
        "linked_sections": sum(1 for n in secs if links.get(n["id"], {}).get("targets")),
        "graph": store is not None,
        "cells": sum(s["cells"] for s in rep.get("sheets", [])),
        "read_cells": sum(s["read_cells"] for s in rep.get("sheets", [])),
        "unread_drawings": sum(s["shapes"] + s["pictures"] + s["charts"] for s in rep.get("sheets", []))
        + rep.get("pictures", 0) + rep.get("charts", 0) + len(rep.get("textboxes", [])),
    }
    rep["findings"] = _findings(rep)
    return rep


def document_list(store) -> list[dict]:
    """Every design document in the graph with what was read from it."""
    docs_ = {}
    for n in store.nodes([Kind.CONFIG]):
        if n.props.get("document"):
            docs_[n.file] = {"path": n.file, "name": n.name, "rows": 0, "sections": 0, "linked": 0}
    for n in store.nodes([Kind.DOC, Kind.DOC_ENTITY]):
        d = docs_.get(n.file)
        if d is None:
            continue
        d["rows" if n.kind == Kind.DOC_ENTITY else "sections"] += 1
        if store.out_edges(n.id, LINK_RELS):
            d["linked"] += 1
    return sorted(docs_.values(), key=lambda d: d["path"])


# --------------------------------------------------------------------------- HTML
def to_html(reports: list[dict]) -> str:
    """One self-contained page for the CLI (no scripts, no external files)."""
    from html import escape as h

    def targets(ts, graph=True):
        if not graph:
            return '<span class=k>（グラフ未指定）</span>'
        if not ts:
            return '<span class="miss">なし</span>'
        return "、".join(f"{h(t['name'])} <span class=k>{h(t['kind'])}</span>" for t in ts[:4]) + \
            (f" <span class=k>ほか {len(ts) - 4}</span>" if len(ts) > 4 else "")

    parts = []
    # overview first: every document, how much was read, how much needs a look
    rows = []
    for i, r in enumerate(reports):
        s = r.get("summary", {})
        warn = sum(1 for f in r.get("findings", []) if f["level"] in ("warn", "error"))
        rows.append(f"<tr><td><a href='#d{i}'>{h(r['path'])}</a></td><td>{h(r.get('format', ''))}</td>"
                    f"<td>{s.get('rows_read', 0)}</td><td>{s.get('linked_rows', 0) if s.get('graph') else '—'}</td>"
                    f"<td>{s.get('sections', 0)}</td><td class={'miss' if warn else 'ok'}>{warn or 'なし'}</td></tr>")
    parts.append("<table><tr><th>設計書</th><th>形式</th><th>表から読んだ行</th><th>うち結びついた行</th><th>節</th>"
                 "<th>要確認</th></tr>" + "".join(rows) + "</table>")
    for i, r in enumerate(reports):
        s = r.get("summary", {})
        g = s.get("graph")
        parts.append(f"<section id='d{i}'><h2>{h(r['path'])}</h2>")
        if r.get("format") == "xlsx":
            parts.append(f"<p class=sum>読んだ行 <b>{s.get('rows_read', 0)}</b>（うち結びついた行 {s.get('linked_rows', 0)}）・"
                         f"文字のあるセル {s.get('cells', 0)} のうち表として読んだもの {s.get('read_cells', 0)}・"
                         f"読んでいない図形・画像 {s.get('unread_drawings', 0)}</p>")
        else:
            parts.append(f"<p class=sum>節 <b>{s.get('sections', 0)}</b>・表から読んだ行 {s.get('rows_read', 0)}"
                         f"（結びついた行 {s.get('linked_rows', 0)}）</p>")
        if r.get("findings"):
            parts.append("<ul class=find>" + "".join(f"<li class={f['level']}>{h(f['message'])}</li>"
                                                     for f in r["findings"]) + "</ul>")
        else:
            parts.append("<p class=ok>読み残しは見つかりませんでした。</p>")
        for sh in r.get("sheets", []):
            parts.append(f"<h3>シート「{h(sh['name'])}」 <span class=k>{sh['rows']} 行・文字のあるセル {sh['cells']}"
                         f"・結合 {sh['merged']}</span></h3>")
            for t in sh["tables"]:
                cols = "".join(f"<th title='{h(c['cell'])}'>{h(c['label'])}<br><span class={'k' if c['as'] else 'miss'}>"
                               f"{h(SEM_JA.get(c['as'], c['as']) if c['as'] else '読まない')}</span></th>" for c in t["columns"])
                rows = "".join(f"<tr><td class='k nw'>{h(x['cell'].split('!')[-1])}</td><td>{h(x['name'])}</td>"
                               f"<td>{h(' / '.join(f'{SEM_JA.get(k, k)}: {v}' for k, v in x['fields'].items()))}</td>"
                               f"<td>{targets(x['targets'], g)}</td></tr>" for x in t["sample"])
                parts.append(f"<p>{t['header_row']} 行目を見出しとする表: {t['rows']} 行（結びついた行 {t['linked']}）</p>"
                             f"<table class=hdr><tr>{cols}</tr></table>"
                             f"<table><tr><th>セル</th><th>読んだ名前</th><th>読んだ値</th><th>結びつき</th></tr>{rows}</table>")
            if sh["crud_rows"]:
                parts.append(f"<p>CRUD 表: {sh['crud_rows']} 行（テーブル {h('・'.join(sh['crud_tables'][:12]))}）</p>")
            if sh["uncovered"]:
                parts.append("<details><summary>表の外にある文字（検索の対象外）"
                             f" {sh['uncovered_cells']} セル</summary><table>" +
                             "".join(f"<tr><td class=k>{u['row']}</td><td>" +
                                     " ・ ".join(f"<span class=k>{h(a)}</span> {h(t)}" for a, t in u["cells"]) +
                                     "</td></tr>" for u in sh["uncovered"]) + "</table></details>")
            if sh["shape_texts"]:
                parts.append("<details><summary>図形の中の文字（読んでいない）</summary><ul>" +
                             "".join(f"<li>{h(t)}</li>" for t in sh["shape_texts"]) + "</ul></details>")
        secs = [x for x in r.get("sections", []) if x["chars"] or x["targets"]]
        if secs:
            parts.append("<h3>節</h3><table><tr><th>節</th><th>文字数</th><th>識別子</th><th>結びつき</th></tr>" +
                         "".join(f"<tr><td>{h(x['title'])}</td><td>{x['chars']}{'（途中まで）' if x['cut'] else ''}</td>"
                                 f"<td>{x['mentions']}</td><td>{targets(x['targets'], g)}</td></tr>" for x in secs) +
                         "</table>")
        for t in r.get("tables", []):
            head = "".join(f"<th>{h(x['label'])}<br><span class={'k' if x['as'] else 'miss'}>{h(SEM_JA.get(x['as'], x['as']) if x['as'] else '読まない')}</span></th>"
                           for x in t["header"])
            parts.append(f"<h3>{h(t['name'])} <span class=k>{t['rows']} 行・読んだ行 {t['read_rows']}</span></h3>"
                         f"<table class=hdr><tr>{head}</tr></table>")
        for tb in r.get("textboxes", []):
            parts.append(f"<p class=miss>テキストボックス（読んでいない）: {h(tb)}</p>")
        parts.append("</section>")
    css = """:root{--bg:#fff;--fg:#1d2433;--mute:#5d6678;--line:#d9dee7;--warn:#9a5b00;--err:#b42318;--ok:#0f766e;--card:#f6f8fb}
@media (prefers-color-scheme:dark){:root{--bg:#121722;--fg:#e6e9ef;--mute:#9aa4b5;--line:#2a3242;--warn:#f0b35a;--err:#ff8a7a;--ok:#5fd4c4;--card:#1a2130}}
body{background:var(--bg);color:var(--fg);font:14px/1.6 system-ui,'Hiragino Sans','Noto Sans JP',sans-serif;margin:0;padding:24px;max-width:1200px}
h1{font-size:20px}h2{font-size:17px;border-bottom:1px solid var(--line);padding-bottom:4px;margin-top:32px}h3{font-size:15px;margin:20px 0 6px}
table{border-collapse:collapse;margin:6px 0 12px;width:100%}th,td{border:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top;font-size:13px}
th{background:var(--card)}.hdr th{min-width:80px}.k{color:var(--mute);font-size:12px}.miss{color:var(--warn)}
.find li{margin:2px 0}.find .warn{color:var(--warn)}.find .error{color:var(--err)}.ok{color:var(--ok)}.sum{background:var(--card);padding:8px 12px;border-radius:6px}
.sec{border-left:3px solid var(--line);padding-left:10px}details{margin:6px 0}summary{cursor:pointer;color:var(--mute)}.nw{white-space:nowrap}a{color:inherit}"""
    return (f"<!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>設計書の取込確認</title><style>{css}</style><h1>設計書の取込確認</h1>"
            f"<p class=k>himotoki の抽出をもう一度行い、原本のどこが読めて、どこが読めなかったかを並べたものです（AI は使っていません）。"
            f"「結びつき」はグラフ（取り込み済みのコード・テーブル）とのつながりです。</p>{''.join(parts)}</html>")


# --------------------------------------------------------------------------- graph / files
DOC_EXT = (".xlsx", ".xlsm", ".docx", ".md", ".markdown", ".txt")


def display_path(engine, path: str) -> str:
    """The name a file has in the graph: `design:<path>` in the documents folder,
    `<repo>/<path>` in a system graph, the plain repository path otherwise."""
    import os
    ap = os.path.realpath(path)
    if engine is not None:
        if engine.docs_dir:
            root = os.path.realpath(engine.docs_dir)
            if ap.startswith(root + os.sep):
                return "design:" + os.path.relpath(ap, root).replace(os.sep, "/")
        roots = engine.repos or ({"": engine.repo} if engine.repo else {})
        for name, r in roots.items():
            root = os.path.realpath(r)
            if ap.startswith(root + os.sep):
                rel = os.path.relpath(ap, root).replace(os.sep, "/")
                return f"{name}/{rel}" if name else rel
    return os.path.basename(path)


def check_graph_document(engine, rel: str, rules=None) -> dict | None:
    """A document the graph has, read from where it was indexed (repository or documents folder)."""
    import os
    from .rules import load_rules
    from .security import max_file_bytes
    n = engine.store.get(f"file:{rel}")
    if n is None or not n.props.get("document") or not rel.lower().endswith(DOC_EXT):
        return None
    full = engine._source_path(rel)
    if not full or not os.path.isfile(full) or os.path.getsize(full) > max_file_bytes() * 4:
        return None
    with open(full, "rb") as fh:
        data = fh.read()
    return check_document(rel, data, rules or load_rules(), engine.store)
