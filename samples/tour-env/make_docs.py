"""Generate the sample Office design documents used in the verification.

python samples/tour-env/make_docs.py  ->  docs/設計書_予約.xlsx, docs/基本設計書_予約.docx
"""
import os

import docx
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(HERE, "ops", "docs"), exist_ok=True)

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "テーブル定義_RESERVE"
ws.append(["テーブル定義書"])
ws.append(["テーブル名", "RESERVE", "", "論理名", "予約"])
ws.append([])
ws.append(["No", "項目名(物理)", "論理名", "データ型", "桁数", "必須", "備考"])
rows = [
    ("RESERVE_NO", "予約番号", "CHAR", 8, "○", "採番"),
    ("TOUR_CODE", "ツアーコード", "CHAR", 10, "○", ""),
    ("RESERVED_DAY", "予約日", "DATE", "", "○", ""),
    ("ADULT_COUNT", "大人人数", "NUMBER", 2, "○", ""),
    ("CHILD_COUNT", "子供人数", "NUMBER", 2, "○", ""),
    ("CUSTOMER_CODE", "顧客コード", "CHAR", 8, "○", ""),
    ("TRANSFER", "振込状況", "CHAR", 1, "○", "0:未 1:済 9:取消"),
    ("SUM_PRICE", "合計金額", "NUMBER", 7, "○", ""),
    ("REMARKS", "備考", "VARCHAR", 80, "", "利用者の自由記入。80文字以内"),
]
for i, r in enumerate(rows, 1):
    ws.append([i, *r])

ws2 = wb.create_sheet("画面項目定義_予約入力")
ws2.append(["画面項目定義書", "", "画面ID", "SC-RV-01", "画面名", "ツアー予約入力"])
ws2.append([])
ws2.append(["No", "画面ID", "項目名(論理)", "項目名(物理)", "入力チェック", "メッセージキー"])
ws2.append([1, "SC-RV-01", "大人人数", "adultCount", "0〜5", ""])
ws2.append([2, "SC-RV-01", "子供人数", "childCount", "0〜5", ""])
ws2.append([3, "SC-RV-01", "備考", "remarks", "80文字以内", "label.tr.searchtour.lessThanEightyCharsMessage"])

ws3 = wb.create_sheet("CRUD表")
ws3.append(["CRUD表（機能×テーブル）"])
ws3.append(["機能ID", "機能名", "RESERVE", "CUSTOMER", "TOURINFO", "TOURCON"])
ws3.append(["F-RV-01", "ツアー予約", "C", "R", "R", "R"])
ws3.append(["F-RV-02", "予約変更", "U", "R", "R", ""])
ws3.append(["F-RV-03", "予約取消", "D", "", "R", ""])
ws3.append(["F-RV-04", "予約照会", "R", "R", "R", ""])
ws3.append(["B-RV-01", "夜間予約連携", "R", "R", "", ""])
ws3.append(["B-RV-02", "取消予約削除", "D", "", "", ""])
wb.save(os.path.join(HERE, "ops", "docs", "設計書_予約.xlsx"))

d = docx.Document()
d.add_heading("基本設計書 ツアー予約", 0)
d.add_heading("1. 予約入力", 1)
d.add_paragraph("利用者はツアー詳細画面から人数と備考を入力して予約する。画面は reservetour/reserveForm、"
                "入力内容は ReserveTourForm で受け取り、ReserveService で RESERVE テーブルへ登録する。")
d.add_paragraph("備考（REMARKS）は80文字以内とし、超過時は入力エラーとする。")
d.add_heading("2. 夜間処理", 1)
d.add_paragraph("JP1 ジョブネット /TOUR/DAILY で、当日予約を reserve_export.csv に出力し（J010_EXPORT）、"
                "後続の J020_REMIND が案内メールを送る。")
t = d.add_table(rows=1, cols=3)
t.rows[0].cells[0].text, t.rows[0].cells[1].text, t.rows[0].cells[2].text = "ジョブ名", "プログラム名", "ファイル名"
for job, prog, f in (("J010_EXPORT", "export_reserve.sh", "reserve_export.csv"),
                     ("J020_REMIND", "send_reminder.sh", "reserve_export.csv")):
    c = t.add_row().cells
    c[0].text, c[1].text, c[2].text = job, prog, f
d.save(os.path.join(HERE, "ops", "docs", "基本設計書_予約.docx"))
print("written")
