"""設計書の取込確認: what was read from a document and what was not, without AI."""
import io
import os
import re
import subprocess
import zipfile

import pytest

from himotoki.doccheck import check_document, document_list, to_html
from himotoki.rules import load_rules

SHAPES = (
    '<xdr:twoCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
    '<xdr:to><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>3</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
    '<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="10" name="btn"/><xdr:cNvSpPr/></xdr:nvSpPr><xdr:spPr/>'
    '<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>予約一覧へ戻る</a:t></a:r></a:p></xdr:txBody></xdr:sp><xdr:clientData/></xdr:twoCellAnchor>'
    '<xdr:twoCellAnchor><xdr:from><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>5</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>'
    '<xdr:to><xdr:col>4</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>6</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>'
    '<xdr:cxnSp><xdr:nvCxnSpPr><xdr:cNvPr id="11" name="arrow"/><xdr:cNvCxnSpPr/></xdr:nvCxnSpPr><xdr:spPr/></xdr:cxnSp>'
    '<xdr:clientData/></xdr:twoCellAnchor>')


def make_xlsx() -> bytes:
    import openpyxl
    from openpyxl.chart import BarChart, Reference
    from openpyxl.comments import Comment
    wb = openpyxl.Workbook()
    lay = wb.active
    lay.title = "画面レイアウト"                     # a grid-paper layout: merged cells, no table
    lay.merge_cells("B2:H2")
    lay["B2"] = "予約入力画面"
    lay["B4"], lay["E4"] = "ツアー名", "（選択）"
    lay["B5"], lay["E5"] = "大人人数", "（入力）"
    lay["B6"], lay["E6"] = "備考", "（80文字まで）"
    lay["B4"].comment = Comment("必須項目", "設計者")
    for r in range(1, 6):
        lay.cell(row=20 + r, column=10, value=r)
    chart = BarChart()
    chart.add_data(Reference(lay, min_col=10, min_row=21, max_row=25))
    lay.add_chart(chart, "J2")                          # gives the sheet a drawing part to put shapes in
    items = wb.create_sheet("項目定義")
    items.append(["画面項目定義"])
    items.append([])
    items.append(["No", "項目名(物理)", "論理名", "データ型", "チェック仕様", "表示順"])
    items.append([1, "TOUR_NAME", "ツアー名", "VARCHAR", "選択必須", 1])
    items.append([2, "ADULT_COUNT", "大人人数", "NUMBER", "1以上", 2])
    items.append([])
    items.append([])
    items.append(["改訂履歴: 2024/04 備考欄を追加"])
    buf = io.BytesIO()
    wb.save(buf)
    # put a button shape and a connector into the layout sheet's drawing
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in src.infolist():
            data = src.read(info.filename)
            if re.match(r"xl/drawings/drawing\d+\.xml$", info.filename):
                # openpyxl writes the default namespace; Excel writes the xdr: prefix. Declare
                # both prefixes on the root and append the shapes as Excel would write them.
                txt = re.sub(r"<(\w+:)?wsDr ", lambda m: f"<{m.group(1) or ''}wsDr "
                             'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
                             'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" ', data.decode("utf-8"), count=1)
                txt = re.sub(r"</(\w+:)?wsDr>\s*$", lambda m: SHAPES + m.group(0), txt)
                data = txt.encode("utf-8")
            z.writestr(info, data)
    return out.getvalue()


def make_docx() -> bytes:
    import docx
    d = docx.Document()
    d.add_heading("1. 予約入力", level=1)
    d.add_paragraph("予約入力画面で ReserveService を呼び出す。")
    t = d.add_table(rows=3, cols=3)
    for i, row in enumerate([["区分", "内容", "担当"], ["入力", "ツアー選択", "画面"], ["確認", "金額計算", "サーバ"]]):
        for j, v in enumerate(row):
            t.cell(i, j).text = v
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_grid_paper_excel_reports_what_was_not_read():
    rep = check_document("design:画面設計書.xlsx", make_xlsx(), load_rules())
    assert not rep["errors"]
    lay = next(s for s in rep["sheets"] if s["name"] == "画面レイアウト")
    assert lay["tables"] == [] and lay["merged"] == 1
    assert lay["shapes"] == 1 and lay["connectors"] == 1 and lay["charts"] == 1 and lay["comments"] == 1
    assert "予約一覧へ戻る" in lay["shape_texts"]
    assert lay["uncovered_cells"] >= 7                       # the whole layout is outside any table
    items = next(s for s in rep["sheets"] if s["name"] == "項目定義")
    (t,) = items["tables"]
    assert t["header_row"] == 3 and t["rows"] == 2
    assert t["unrecognised"] == ["チェック仕様", "表示順"]      # "No" is read since the YAML fix
    assert {c["label"]: c["as"] for c in t["columns"]}["項目名(物理)"] == "column"
    assert any("改訂履歴" in txt for u in items["uncovered"] for _, txt in u["cells"])
    msgs = " ".join(f["message"] for f in rep["findings"])
    assert "画面レイアウト" in msgs and "表として読めていません" in msgs
    assert "チェック仕様" in msgs and "図形" in msgs
    assert rep["findings"][0]["level"] == "warn"           # the unread sheet comes first
    assert "結びついていません" not in msgs                  # no graph given: links are not judged
    html = to_html([rep])
    assert "予約一覧へ戻る" in html and "チェック仕様" in html and "<script" not in html


def test_word_table_without_known_header_is_reported():
    rep = check_document("design:基本設計.docx", make_docx(), load_rules())
    (t,) = rep["tables"]
    assert not t["recognised"] and t["rows"] == 3
    assert any("表1" in f["message"] and "見出しを認識できず" in f["message"] for f in rep["findings"])
    assert any(s["title"].startswith("1. 予約入力") for s in rep["sections"])


def test_indexed_documents_link_to_the_graph(tmp_path):
    from himotoki.doccheck import check_graph_document
    from himotoki.workspace import Workspace
    repo, docs = tmp_path / "repo", tmp_path / "docs"
    (repo / "src/main/java/x").mkdir(parents=True)
    (repo / "src/main/java/x/ReserveService.java").write_text("package x; public class ReserveService {}")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    docs.mkdir()
    (docs / "基本設計.docx").write_bytes(make_docx())
    ws = Workspace(str(tmp_path / "ws"))
    ws.index(str(repo), str(docs), name="g")
    e = ws.engine("g")
    listed = {d["path"]: d for d in document_list(e.store)}
    assert "design:基本設計.docx" in listed
    rep = check_graph_document(e, "design:基本設計.docx")
    sec = next(s for s in rep["sections"] if s["title"].startswith("1. 予約入力"))
    assert any(t["name"] == "ReserveService" for t in sec["targets"])
    # only indexed documents can be read through the graph (not arbitrary repository files)
    assert check_graph_document(e, "src/main/java/x/ReserveService.java") is None
    assert check_graph_document(e, "design:../../etc/passwd") is None


def test_api_and_cli(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from himotoki.workspace import Workspace
    repo, docs = tmp_path / "repo", tmp_path / "docs"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    docs.mkdir()
    (docs / "画面設計書.xlsx").write_bytes(make_xlsx())
    data = tmp_path / "ws"
    Workspace(str(data)).index(str(repo), str(docs), name="g")
    monkeypatch.setenv("HIMOTOKI_DATA_DIR", str(data))
    import importlib
    import himotoki.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    d = c.get("/api/docs", params={"graph": "g"}).json()
    assert [x["path"] for x in d] == ["design:画面設計書.xlsx"]
    r = c.get("/api/docs/check", params={"graph": "g", "path": "design:画面設計書.xlsx"}).json()
    assert r["summary"]["graph"] and any(s["name"] == "画面レイアウト" for s in r["sheets"])
    assert c.get("/api/docs/check", params={"graph": "g", "path": "main.py"}).status_code == 404
    out = tmp_path / "check.html"
    res = subprocess.run(["python", "-m", "himotoki.cli", "--data", str(data), "docs-check", "--graph", "g",
                          "--html", str(out)], capture_output=True, text=True,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert res.returncode == 0, res.stderr
    assert "画面設計書.xlsx" in res.stdout and "表として読めていません" in res.stdout
    assert "予約一覧へ戻る" in out.read_text(encoding="utf-8")
