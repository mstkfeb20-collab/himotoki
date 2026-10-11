# はじめての himotoki

このページの手順どおりに進めれば、プログラミングに詳しくなくても、自分の担当システムで影響調査を始められます。

```
① インストール（15分） → ② デモで試す（5分） → ③ 自分のデータを置く → ④ 取り込む → ⑤ ビュワーで解析する
```

---

## 1. 用意するもの

### パソコン

| 項目 | 目安 |
|---|---|
| OS | Windows 10/11、macOS、Linux |
| メモリ | 8 GB 以上（数千ファイルを超える大きなシステムは 16 GB 以上） |
| ディスク | 解析するリポジトリの容量 ＋ 1 GB 程度 |
| ブラウザ | Chrome または Edge |

まず個人の PC で試し、チームで使うようになったらサーバに置く（[README の Docker 構成](../README.md#dockerneo4j-構成)）、という順で進めるのがおすすめです。

### インストールするソフト

| ソフト | 必須か | 用途 |
|---|---|---|
| Python 3.11 以上 | 必須 | himotoki 本体 |
| Git | ほぼ必須 | リポジトリの取得、差分からの解析、変更履歴 |
| LibreOffice | 任意 | 古い形式の設計書（.xls / .doc）を読むときだけ。.xlsx / .docx はそのまま読めます |
| Node.js | 不要 | ビュワーはパッケージに同梱しています（ビュワーを改造する開発者だけ必要） |
| Claude の API キー | 任意 | AI による判定。無くても全機能が動きます（[5章](#5-ai判定を使う任意)） |

### 集めるデータ

最初はソースコードだけで構いません。あとから足すほど、影響候補の漏れが減ります。

| データ | 必須か | 入っていると見つかるもの |
|---|---|---|
| ソースコード（Git リポジトリ） | 必須 | Java・SQL・画面・設定・リポジトリ内のシェルやジョブ定義 |
| 設計書（Excel・Word・Markdown） | 推奨 | テーブル定義書・画面項目定義書・CRUD 表の該当行 |
| JP1 のジョブ定義・crontab | 推奨 | 夜間バッチ・連携ジョブ・ファイル監視 |
| サーバ上にしかない運用シェル・SQL | 推奨 | リポジトリに入っていない運用スクリプト |
| 本番 DB の辞書（CSV） | 任意 | DB にだけ追加された列・ビュー・ストアド |
| 社内フレームワークのルール | 任意 | 社内独自の書き方による呼び出し関係 |

---

## 2. インストール

### Windows

1. **Python** を <https://www.python.org/downloads/> から入れます。インストーラの最初の画面で「**Add python.exe to PATH**」にチェックを入れてください。
2. **Git** を <https://git-scm.com/download/win> から入れます（設定は既定のままで構いません）。
3. コマンドプロンプトを開き、次を順に実行します。

```bat
cd %USERPROFILE%
git clone https://github.com/mstkfeb20-collab/himotoki.git
cd himotoki
py -m venv .venv
.venv\Scripts\activate
pip install .\backend
himotoki doctor
```

> 2 回目からは、コマンドプロンプトを開いたら `cd %USERPROFILE%\himotoki` → `.venv\Scripts\activate` を実行してから使います。
> PowerShell で `activate` がエラーになる場合は、コマンドプロンプトを使うか、`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を一度実行してください。

### macOS / Linux

```bash
# Python と Git が無ければ: macOS は brew install python git、Ubuntu は sudo apt install python3-venv git
cd ~
git clone https://github.com/mstkfeb20-collab/himotoki.git
cd himotoki
python3 -m venv .venv
source .venv/bin/activate
pip install ./backend
himotoki doctor
```

### 社内ネットワークで pip が通らないとき

- **プロキシ経由**: `pip install --proxy http://プロキシ:ポート ./backend`
- **インターネットに出られない環境**: インターネットに出られる PC（同じ OS・同じ Python のバージョン）で部品をまとめてダウンロードし、持ち込みます。

```bash
# インターネットに出られる PC
pip download -d wheels ./backend
# 閉じた環境の PC（himotoki のフォルダと wheels フォルダを持ち込んで）
pip install --no-index --find-links wheels ./backend
```

### 確認

`himotoki doctor` で準備の状態が表示されます。`NG` があれば、その行の説明どおりに直してください。`注意` は任意の項目なので、そのままでも使えます。

```
[OK  ] Python               3.12.6
[OK  ] 必要なライブラリ     そろっています
[OK  ] git                  git version 2.47.0
[OK  ] ビュワー             同梱のものを使います
[注意] LibreOffice（任意）  無し。.xlsx / .docx は読めます。古い .xls / .doc を読むときだけ必要です
[注意] AI判定（任意）       未設定。ルールで判定します（候補は出ます。絞り込みはレビューで）
...
```

---

## 3. デモで試す

```bash
himotoki demo
```

サンプルのシステム（受注・在庫・出荷の 3 サーバで 1 つの業務）がフォルダ `himotoki-demo` に置かれます。続いて取り込み、「受注の備考（NOTE）の上限を200文字から500文字に拡張する」という要求を解析し、ブラウザでビュワーを開きます。止めるときは Ctrl+C です。

ビュワーでは次の順に見てください。

1. **リスト**: 影響候補が「影響ありの可能性が高い」「要確認」「影響は低そう」に分かれています。行を選ぶと、その要素に至った経路が出ます。
2. **右の詳細**: コード・設計書・テスト。`OrderServiceBean.placeOrder()` を選ぶと「トランザクション・連携」の欄があり、トランザクション内の SOAP / REST 呼び出しなど、改修時の注意点が出ます。
3. **確定・除外**: 行の「確定」「除外…」で判断を記録します（キーボードの `c` / `x` でも操作できます）。
4. **レポート**: 右上の「レポート」で、確定した影響箇所と規模を Markdown で出力します。

初回は画面の案内（チュートリアル）が出ます。右上の「使い方」からいつでも見直せます。

---

## 4. 自分のシステムで使う

### 4.1 ワークスペースを作る

ワークスペースは、解析に使うものを決まった場所に置くためのフォルダです。担当システムごとに 1 つ作ります。

```bash
himotoki init C:\work\juchu      # Windows の例（macOS / Linux は ~/work/juchu など）
cd C:\work\juchu
```

中には次のフォルダができます。

```
juchu/
├─ repos/          ソースコード（リポジトリごとに git clone）
├─ docs/           設計書・ジョブ定義・運用シェル
├─ db/             DB 辞書の CSV（export_oracle.sql 付き）
├─ rules/          社内フレームワーク用のルール
├─ settings.yaml   AI 判定の上限・費用表示の設定
└─ README.txt      このフォルダの説明
```

これ以降のコマンドは、このフォルダ（または中のフォルダ）で実行します。場所の指定は要りません。

### 4.2 データを置く

#### ソースコード → `repos/`

```bash
cd repos
git clone https://git.example.co.jp/juchu/order-web.git
git clone https://git.example.co.jp/juchu/inventory-svc.git      # 別サーバのリポジトリも並べる
cd ..
```

- **複数のサーバで 1 つの業務を構成している場合**は、関係するリポジトリをすべて `repos/` に並べてください。1 つのシステムとしてまとめて解析し、SOAP・REST・JMS・DB リンク・ファイル連携でつながった先まで影響を追います。
- **関係の無いシステム**は、別のワークスペースに分けてください。
- **SVN の場合**は `git svn clone` で Git に変換するのがおすすめです。フォルダをコピーして置くだけでも取り込めますが、差分からの解析と変更履歴は使えません。

#### 設計書 → `docs/`

Excel・Word・Markdown をフォルダ構成のまま置いてください（例: `docs/設計書/基本設計/…`）。

- Excel の表は、見出し行（「テーブル名」「項目名(物理)」「画面ID」など）を自動で探して 1 行ずつ取り込みます。CRUD 表（機能 × テーブル）も見分けます。
- 古い形式（.xls / .doc）は LibreOffice が入っていれば自動で変換します。
- 図だけの資料や、結合セルの多い帳票型の資料からは、あまり取り出せません。

#### JP1 のジョブ定義 → `docs/jp1/`

JP1/AJS3 の `ajsprint` で、ユニット定義を定義形式で出力したテキストを置きます。

```bash
ajsprint -F AJSROOT1 -a -R "/*" > ajs_all.txt     # JP1 のサーバで実行（コマンドは環境に合わせて JP1 管理者に確認）
```

ファイルは `docs/jp1/` に置いてください（パスに `jp1` か `ajs` を含むファイルをジョブ定義として読みます）。

#### crontab → `docs/cron/`

```bash
crontab -l > crontab_batch01.txt     # 各サーバで実行し、docs/cron/ に置く
```

#### サーバ上にしかない運用シェル・SQL → `docs/ops/<サーバ名>/`

ジョブから起動されるシェル・SQL・SQL*Loader の制御ファイルがリポジトリに無い場合は、サーバからコピーして置きます（例: `/app/batch` を `docs/ops/batch01/` へ）。ジョブ → シェル → SQL → テーブル、シェル → 連携ファイル → 後続ジョブ、とつながります。

#### 本番 DB の辞書（Oracle）→ `db/<DB名>/`

DB にだけ追加された列やストアドを見つけるためのものです（任意）。

```bash
sqlplus ユーザ/パスワード@接続先 @db/export_oracle.sql スキーマ名
```

出力された `ALL_TAB_COLUMNS.csv` などを `db/proddb/` のようなフォルダに移します。読み取り専用のユーザで実行してください。PostgreSQL の場合は `himotoki catalog postgres --dsn …` で直接取り込めます（[README](../README.md#ローカル開発)）。

> `export_oracle.sql` は SQL*Plus 12.2 以降向けで、作成環境では実際の Oracle で確認できていません。うまく出力できない場合は、SQL Developer で同じ SELECT を実行し、見出し付きの CSV で保存しても読み込めます。

#### 社内フレームワークのルール → `rules/`

社内独自の書き方（例: SQL を XML に名前付きで置き、Java から名前で呼ぶ）がある場合は、ルール（YAML）を置きます。書き方は [README の「社内フレームワークへの対応例」](../README.md#社内フレームワークへの対応例)にあります。最初は無くて構いません。取り込み後の「取込状況」で未解決の参照が多いときに検討します。

### 4.3 確認して取り込む

```bash
himotoki doctor      # 置いたものが見えているか確認
himotoki index       # repos/・docs/・db/ をまとめて取り込む
```

取り込みが終わったら、どこまで取り込めたかを確認します。

```bash
himotoki report
```

ビュワーの「取込状況」タブでも同じ内容が見られます。見るところは次の 3 つです。

- **未解決の参照**: 取り込めなかったつながり。特定の種類が多ければ、ルールの追加で改善できる可能性があります。
- **エラー**: 読めなかったファイル。
- **トランザクション**: トランザクションの境界と注意点の件数。

### 4.4 解析する

```bash
himotoki serve --open
```

ビュワーの「新しい解析」に要求を貼り付けて「解析を開始」を押します。書き方のコツは画面の補助に出ます。特に、変更前と変更後の値（例:「80文字から200文字に」）を書くと、その値を使っている箇所まで探します。あとは[3章](#3-デモで試す)と同じ流れで、確定・除外してレポートを出します。

### 4.5 ソースや資料を更新したとき

`git pull` で最新にするか資料を置き直してから、もう一度 `himotoki index` を実行します。変わったファイルだけを読み直すので、2 回目からは速く終わります。

---

## 5. AI判定を使う（任意）

API キーが無くても、影響候補の洗い出しから規模の算出まで全部動きます。その場合、絞り込みはルールで行うため「要確認」が多めになり、人のレビューで判断します。

AI 判定を使うと、候補ごとに「影響あり／なし」と理由を AI が付けます。

```bat
setx ANTHROPIC_API_KEY "sk-ant-..."         :: Windows（コマンドプロンプトを開き直すと有効）
```
```bash
export ANTHROPIC_API_KEY="sk-ant-..."        # macOS / Linux
```

- **社外に送られるもの**: 要求文と、判定する候補のコード断片（候補ごとに最大 25 行程度）が Anthropic の API に送られます。リポジトリ全体は送りません。**利用前に、社内の情報セキュリティ規程と顧客との契約で、外部 AI サービスへのソースコード送信が認められているかを確認してください。**
- **費用**: ビュワー右上にトークン量と円換算がリアルタイムに出ます。1 回の解析で AI に送る件数の上限は `settings.yaml` で変えられます（既定 300 件）。

---

## 6. 困ったとき

| 症状 | 対処 |
|---|---|
| `himotoki` が見つからない | 仮想環境を有効にしていません。`.venv\Scripts\activate`（Windows）/ `source .venv/bin/activate`（macOS / Linux）を実行 |
| `pip install` が失敗する | 社内プロキシの可能性があります。[2章](#社内ネットワークで-pip-が通らないとき)を参照 |
| ポートが使用中と出る | 起動済みなら表示された URL を開く。別のポートで起動するには `himotoki serve --open --port 8766` |
| `himotoki index` が「リポジトリがありません」 | `repos/` の中に、リポジトリのフォルダ（`repos/order-web/…`）があるか確認。`repos/` 直下にソースを直接置かないでください |
| 日本語が文字化けする | UTF-8 と Shift_JIS（Windows-31J）は自動で判別します。EUC-JP など、それ以外の文字コードは未対応です |
| 設計書が候補に出ない | 表の見出し行が見つからない可能性があります。「取込状況」のエラーと、`docs/` に置いたかを確認 |
| 候補が多すぎる | ビュワーの確度のスライダーと層のフィルタ（コード・データなど）で絞れます。AI 判定を使うと「影響あり」が絞り込まれます |
| 候補にあるべきものが無い | 「取込状況」の未解決の参照を確認。社内フレームワークの書き方が原因なら `rules/` にルールを追加します |

> Windows での動作は、作成環境では確認できていません（パスの区切りや文字コードの違いには対応済みです）。うまく動かない場合は、WSL（Windows 上の Ubuntu）で macOS / Linux の手順を使う方法もあります。
