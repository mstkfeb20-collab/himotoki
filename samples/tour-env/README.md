# 検証用の周辺資産（TERASOLUNA ツアー予約サンプル向け）

[terasoluna-tourreservation-mybatis3](https://github.com/terasolunaorg/terasoluna-tourreservation-mybatis3) には
ジョブ定義・運用スクリプト・Office設計書・本番DBだけにある改修が含まれないため、SoRで実際に起きる状況を
検証するために作成したものです（公式資料ではありません）。

| パス | 内容 |
|---|---|
| `ops/jobs/AJS_TOUR_DAILY.txt` | JP1/AJS3 のユニット定義（ajsprint 出力形式）。夜間ジョブネットと先行関係 |
| `ops/jobs/crontab` | cron の定義 |
| `ops/batch/*.sh`, `ops/batch/sql/*.sql` | ジョブが起動するシェルとSQL。連携ファイルを書き出し、後続ジョブが読む |
| `ops/docs/設計書_予約.xlsx` | テーブル定義・画面項目定義・CRUD表（`make_docs.py` で生成） |
| `ops/docs/基本設計書_予約.docx` | 見出しと本文、項目表（`make_docs.py` で生成） |
| `db/db_only_objects.sql` | DBに直接当てた列・ビュー・ストアド・トリガー（リポジトリのDDLとのずれを作る） |
| `db/workload.sql` | pg_stat_statements に実行時の記録を残すためのSQL（コードに無いSQLを含む） |
| `db/oracle_catalog/*.csv` | Oracle 辞書エクスポート形式の取り込み確認用データ |

`ops/` は解析時に `--docs samples/tour-env/ops` として渡します（設計書・ジョブ定義・運用スクリプトを同じグラフに入れるため）。
