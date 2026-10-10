# テーブル定義書 TB-02 owners（オーナー）

対応エンティティ: Owner（Person を継承）。DBMSごとの定義は schema.sql（h2 / mysql / postgres）。
初期データは data.sql で投入する。

| 列名 | 型 | NULL | 説明 |
|---|---|---|---|
| owners.id | INTEGER | × | 主キー（自動採番） |
| owners.first_name | VARCHAR(30) | ○ | 名 |
| owners.last_name | VARCHAR(30) | ○ | 姓（索引あり） |
| owners.address | VARCHAR(255) | ○ | 住所 |
| owners.city | VARCHAR(80) | ○ | 市区町村 |
| owners.telephone | VARCHAR(20) | ○ | 電話番号 |

## 注意

h2 と mysql の data.sql は列名を指定しない INSERT（列順依存）のため、列を追加する場合は初期データも修正が必要。
