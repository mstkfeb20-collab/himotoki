"""手順で書かれた設計書から「守りたいもの」を抽出できるかを試すための題材を生成する。

python samples/spec-extraction/make_docs.py  ->  samples/spec-extraction/docs/*.docx, *.xlsx

ドメインは tour-env と同じツアー予約（RESERVE.TRANSFER は 0:未 1:済 9:取消）。
設計書はすべて手順・表・擬似コードなどの「作り方」で書き、守りたいルールはその中に埋め込む。
正解は eval/spec_extraction/ground_truth.json にある（パターン P1〜P6）。
"""
import os

import docx
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "docs")
os.makedirs(OUT, exist_ok=True)


def save_doc(d, name):
    d.save(os.path.join(OUT, name))


def table(d, header, rows):
    t = d.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    for i, h in enumerate(header):
        t.rows[0].cells[i].text = h
    for r in rows:
        cells = t.add_row().cells
        for i, v in enumerate(r):
            cells[i].text = str(v)


# ---------------------------------------------------------------------------
# P1 手順型：番号付きの処理手順の中に、事前条件・状態遷移・不変条件が埋もれている
# ---------------------------------------------------------------------------
d = docx.Document()
d.add_heading("処理設計書 F-RV-03 予約取消", 0)
d.add_heading("1. 概要", 1)
d.add_paragraph("利用者が予約照会画面から予約を取り消す。ReserveService#cancel で処理する。")
d.add_heading("2. 処理手順", 1)
for step in [
    "① 画面から受け取った予約番号で RESERVE を取得する。取得できない場合、または TRANSFER が「9:取消」の場合は"
    "メッセージ E-RV-031「この予約は取り消せません」を表示して終了する。",
    "② ツアーの出発日を TOURINFO から取得し、システム日付が出発日の前日を過ぎている場合は"
    "メッセージ E-RV-032「取消期限を過ぎています」を表示して終了する。",
    "③ TRANSFER が「1:済」の場合は、取消料を計算し（別紙「取消料率表」参照）、返金処理（4章）を呼び出す。",
    "④ RESERVE.TRANSFER を「9:取消」に更新する。",
    "⑤ TOURINFO の残席数に、予約の大人人数と子供人数の合計を加算する。",
    "⑥ 取消完了メールを送信し、予約照会画面に戻る。",
]:
    d.add_paragraph(step)
d.add_heading("3. 補足", 1)
d.add_paragraph("④と⑤は同一トランザクションで実行する。")
d.add_heading("4. 返金処理", 1)
d.add_paragraph("振込額から取消料を差し引いた額を、振込元口座へ返金する。返金額は経理システムへ連携する。")
save_doc(d, "P1_処理設計書_予約取消.docx")

# ---------------------------------------------------------------------------
# P2 判定表型：表のセルの並びに、単調性・境界・計算式が埋もれている
# ---------------------------------------------------------------------------
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "取消料率表"
ws.append(["取消料率表（F-RV-03 別紙）"])
ws.append([])
ws.append(["No", "取消日（出発日基準）", "振込状況", "取消料率", "返金額", "備考"])
for row in [
    (1, "21日前まで", "1:済", "0%", "振込額の全額", ""),
    (2, "20日前〜8日前", "1:済", "20%", "振込額 −（合計金額 × 20%）", ""),
    (3, "7日前〜2日前", "1:済", "30%", "振込額 −（合計金額 × 30%）", ""),
    (4, "前日", "1:済", "40%", "振込額 −（合計金額 × 40%）", ""),
    (5, "当日以降", "−", "−", "−", "取消不可"),
    (6, "前日まで（期間を問わない）", "0:未", "請求しない", "なし", "未振込の取消には取消料を請求しない"),
]:
    ws.append(list(row))
ws.append([])
ws.append(["※ 返金額の計算結果が0円未満になる場合は0円とする。"])
ws.append(["※ 1円未満の端数は切り捨てる。"])
wb.save(os.path.join(OUT, "P2_取消料率表.xlsx"))

# ---------------------------------------------------------------------------
# P3 擬似コード型：入力チェックの分岐に、事前条件と不変条件が埋もれている
# ---------------------------------------------------------------------------
d = docx.Document()
d.add_heading("処理設計書 F-RV-01 ツアー予約（入力チェック）", 0)
d.add_heading("1. 入力チェック処理", 1)
d.add_paragraph("ReserveService#reserve の冒頭で、以下の順にチェックする。")
for line in [
    "IF 大人人数 + 子供人数 = 0 THEN",
    "    エラー E-RV-011「人数を入力してください」",
    "END IF",
    "IF 大人人数 = 0 AND 子供人数 > 0 THEN",
    "    エラー E-RV-012「お子様のみのご予約はお受けできません」",
    "END IF",
    "IF 大人人数 + 子供人数 > TOURINFO.残席数 THEN",
    "    エラー E-RV-013「残席が不足しています」",
    "END IF",
    "IF 出発日 − システム日付 < 7 THEN",
    "    エラー E-RV-014「ご予約は出発日の7日前までです」",
    "END IF",
]:
    p = d.add_paragraph(line)
    p.style = "No Spacing"
d.add_heading("2. 登録処理", 1)
d.add_paragraph("チェックを通過したら RESERVE に TRANSFER=「0:未」で登録し、TOURINFO の残席数から"
                "大人人数と子供人数の合計を減算する。合計金額は 料金 × 人数 で計算して SUM_PRICE に設定する。")
save_doc(d, "P3_処理設計書_予約入力チェック.docx")

# ---------------------------------------------------------------------------
# P4 運用手順書型：オペレーターへの指示の中に、順序制約・冪等性が埋もれている
# ---------------------------------------------------------------------------
d = docx.Document()
d.add_heading("運用手順書 夜間ジョブ /TOUR/DAILY 異常時の対応", 0)
d.add_heading("1. J010_EXPORT が異常終了した場合", 1)
for step in [
    "(1) 出力先の reserve_export.csv を削除せず、reserve_export.csv.<日時>.bak として退避する。",
    "(2) 原因を取り除いたうえで J010_EXPORT を再実行する。",
    "(3) J010_EXPORT が正常終了したことを確認してから、J020_REMIND を実行する。",
]:
    d.add_paragraph(step)
d.add_heading("2. J020_REMIND が異常終了した場合", 1)
for step in [
    "(1) J020_REMIND をそのまま再実行してよい。送信済みの予約には REMIND_SENT に送信日時が入っており、"
    "再実行時は REMIND_SENT が空の予約にだけ送信される。",
    "(2) J020_REMIND を単独で実行するのは、当日の J010_EXPORT が正常終了している場合に限る。"
    "J010_EXPORT が未実行・異常終了のまま J020_REMIND を実行してはならない。",
]:
    d.add_paragraph(step)
d.add_heading("3. 注意", 1)
d.add_paragraph("お客様から「案内メールが2通届いた」との問い合わせがあった場合は、障害として報告すること。")
save_doc(d, "P4_運用手順書_夜間ジョブ再実行.docx")

# ---------------------------------------------------------------------------
# P5 改訂履歴型：本文は古いまま、改訂履歴にだけ現在の値がある（矛盾の検出）
# ---------------------------------------------------------------------------
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "画面項目定義_予約入力"
ws.append(["画面項目定義書", "", "画面ID", "SC-RV-01", "画面名", "ツアー予約入力"])
ws.append([])
ws.append(["No", "項目名(論理)", "項目名(物理)", "入力チェック", "メッセージキー"])
ws.append([1, "大人人数", "adultCount", "0〜5", ""])
ws.append([2, "子供人数", "childCount", "0〜5", ""])
ws.append([3, "備考", "remarks", "80文字以内", "label.tr.searchtour.lessThanEightyCharsMessage"])
ws2 = wb.create_sheet("改訂履歴")
ws2.append(["版", "日付", "改訂内容", "改訂者"])
ws2.append(["1.0", "2022/04/01", "初版", "山田"])
ws2.append(["1.1", "2023/10/02", "支払期限を出発日の7日前から10日前に変更（経理締めの前倒し対応）", "佐藤"])
ws2.append(["1.2", "2024/04/15", "団体予約に対応するため、大人人数の上限を5名から9名に変更", "佐藤"])
wb.save(os.path.join(OUT, "P5_画面項目定義_予約入力.xlsx"))

# ---------------------------------------------------------------------------
# P6 曖昧・例外型：副作用として書かれた原子性と、条件の書かれていない例外
# ---------------------------------------------------------------------------
d = docx.Document()
d.add_heading("処理設計書 F-RV-02 予約変更", 0)
d.add_heading("1. 処理概要", 1)
d.add_paragraph("利用者は予約照会画面から、人数と備考を変更できる。")
d.add_heading("2. 処理内容", 1)
for para in [
    "人数を変更した場合は、変更前との差分を TOURINFO の残席数に反映する。増員により残席が足りなくなる場合は、"
    "RESERVE と TOURINFO を変更前の状態に戻し、エラー E-RV-021 を表示する。",
    "人数変更後は、合計金額（SUM_PRICE）を 料金 × 変更後の人数 で再計算する。",
    "原則として、振込済（TRANSFER=「1:済」）の予約は変更を受け付けない。ただし、営業担当が認めた場合はこの限りでない。",
    "備考は80文字以内で変更できる。",
]:
    d.add_paragraph(para)
save_doc(d, "P6_処理設計書_予約変更.docx")

print("written:", sorted(os.listdir(OUT)))
