# himotoki — 要求からソースコードの影響範囲・修正規模を出す仕組み

> 名前は「紐解く」から。要求を起点に、コード・DB・ジョブ・設計書・サーバ間のつながりを一本ずつたどります（旧名: codegraph、Impact Analyzer）。

要求仕様書の該当箇所やチャットの内容を投入すると、ソースコード・SQL・画面・メッセージ・DB・ジョブ・運用スクリプト・設計書（Excel/Word）のどこに影響するかを依存グラフで洗い出します。その過程はビュワーでリアルタイムに表示されます。

**このツールはソースコードを書き換えません。** 出力は「どこを直すか（影響範囲）」と「どれくらいかかるか（規模）」までです。

対象として想定しているのは、Java と Oracle / PostgreSQL を使い、ジョブスケジューラ（JP1 / cron）・運用シェル・Office 設計書を抱えた業務システム（SoR）のリポジトリです。TypeScript（Next.js・React・Supabase・Prisma）のリポジトリも扱えます。特定のフレームワークに合わせたコードを書くのではなく、**フレームワークの知識を YAML のルールパックとして外出し**しています。そのため、社内フレームワークにもルールの追加だけで対応できます。

**はじめて使う方は [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)** を見てください。インストールからデモ、自分のシステムのデータの置き方までを順に説明しています。最短は次の 3 行です（Python 3.11 以上と Git が必要。Node.js は不要）。

```bash
git clone https://github.com/mstkfeb20-collab/himotoki.git && cd himotoki
python3 -m venv .venv && source .venv/bin/activate      # Windows: py -m venv .venv && .venv\Scripts\activate
pip install ./backend && himotoki demo                  # サンプルを取り込んでビュワーを開く
```

![モジュール単位にまとめたグラフと取込状況](docs/screenshots/v2-aggregated.png)

## このツールがやること・やらないこと

修正プロセス全体のうち、このツールが受け持つのは「影響範囲と規模を確定させる」ところまでです。コードは書き換えませんが、確定した範囲を開発（人・Claude Code）へ渡し、実装後の差分を予測と突き合わせるところまでをつないでいます。

| # | 工程 | 担当 | 成果物 |
|---|---|---|---|
| 1 | 要求の受け取り（仕様書・チャット）／変更差分（MR・PR） | 人・ソース管理サーバ | 要求文／差分 |
| 2 | 影響候補の洗い出し | **このツール（解析エンジン）** | 影響候補の一覧（根拠・確度つき） |
| 3 | 候補の確認・判断の訂正・再調査 | **このツール（ビュワー）を使って人またはエージェント（MCP経由）** | 確定・除外の判断（理由つき） |
| 4 | 全数判定の条件を満たしたうえでの承認 | **このツール（ビュワー）を使って人** | 確定した影響範囲・規模・レポート |
| 5 | コードの修正 | 開発者、または Claude Code など。**このツールは作業指示を渡す**（ビュワーの「開発へ渡す」、MCP の `get_handoff`、CLI `handoff`） | 変更差分 |
| 6 | テスト・レビュー | 対象外。ただし MR/PR には回帰確認の範囲をコメントで返す | テスト結果 |
| 7 | 実績の振り返り（予測と実際の差分・工数） | **このツール**（「実績と照合」、MCP の `verify_change`、CLI `verify`）。予測漏れを「判断の見直し」と「グラフの欠け」に分け、実績工数で規模を補正 | 照合結果・見積もり係数の補正 |

3 の「訂正」は、AIが出した影響候補の**判断を直す**こと（確定・除外・変更種別や難易度の補正・漏れの追加）を指します。ソースコードの修正ではありません。

## 仕組み

設計の詳細（要件・設計原則・部品の境界と契約・設計判断・制約・拡張手順）は [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) にあります。

```
 リポジトリ(git)  設計書(xlsx/docx/md)  ジョブ定義(JP1/cron)  DB辞書(Oracle/PG)  実行時SQL(pg_stat_statements/CSV)
       │               │                    │                   │                   │
       ▼               ▼                    ▼                   ▼                   ▼
 ┌─ 抽出（ファイル単位・並列・キャッシュ） ───────────────┐   ┌─ 取込（sources/） ─────────┐
 │ java / ts / prisma / xml / text / sql / props / jobs / docs / layout │   │ catalog.py / runtime.py      │
 │   ＋ ルールパック（YAML）がフレームワークの意味を与える   │   └──────────────┬──────────┘
 │ → FileFacts: ノード・ファイル内の関係・「キーへの参照」   │                  │
 └──────────────┬─────────────────────────────┘                  │
                ▼                                                               ▼
 ┌─ リンク（全体） ───────────────────────────────────────────────┐
 │ キー一致 → 名前空間ごとの解決（型解決・URL・ビュー・プロパティパス・テーブル/列…） │
 │ すべての関係に 根拠(src) と 確度(conf)。解決できない参照・DBとDDLのずれ・        │
 │ コードに対応の無い実行時SQL を「取込状況」として報告                             │
 └──────────────┬────────────────────────────────────────────────┘
                ▼
 コード知識グラフ（ローカル JSON / Neo4j）── ブランチ・タグごとに別グラフ（抽出結果は共有）
                ▼
 解析エンジン: 要求の構造化 → 起点 → 伝播（ルールで重み付け）→ 判定 → 横断検索 → 独立再探索
              → CRUD確認（実行時SQL込み）→ 規模   ／ 差分モード: 変更行を含む要素 → 回帰確認範囲
                ▼
 ビュワー（REST + WebSocket）・CLI・MCP サーバ（エージェント向け）・Webhook（MR/PR にコメント）
```

### 1. 抽出: 汎用の抽出器と、意味を与えるルールパック

抽出器は言語・形式ごとに 10 個あり、Java 側はフレームワークを知りません（TypeScript は Next.js・Supabase・Prisma の慣習を抽出器が直接読みます）。

| 抽出器 | 対象 | 取り出すもの |
|---|---|---|
| `java` | *.java（tree-sitter） | クラス・メソッド・フィールド、呼び出し・型・フィールドアクセスの記述子、アノテーション、文字列リテラル（同じクラスの定数経由も含む） |
| `ts` | *.ts・*.tsx（tree-sitter）、tsconfig.json・package.json | モジュール・関数（React コンポーネントを含む）・クラス・インターフェース・型・定数。呼び出し・JSX のコンポーネント（RENDERS）・型の参照・`implements`、Supabase の `.from("表")` と Prisma の `prisma.モデル.操作()` の読み書き、Next.js App Router の画面・API、`it`/`test` ごとのテスト、画面の文言・コメント（日本語で引けるように） |
| `prisma` | schema.prisma | モデル → テーブル（`@@map`）、項目 → 列（`@map`）、`///` の説明 |
| `xml` | *.xml | ルールパックの `xml:` ルールに従ってノード・参照を作る（パス・属性テンプレート・条件）。Shift_JIS などの宣言も読む。Struts の validation.xml（入力チェック・桁）も読む。ルールの無い XML（帳票定義・ベンダーツールの定義など）は、列名・クラス名・SQL・システムIDに当たる値と桁の属性を拾う |
| `text` | JSP・テンプレート・シェル・Groovy など | ルールパックの `text:` ルールの正規表現で参照を作る。JSP は Struts の `<html:*>` タグ（項目・maxlength）も読む。シェルは同じファイルの変数（`OUT_FILE=${OUT_DIR}/X_${YMD}.dat`）を展開してから読み、`send_file` などのラッパー関数・転送コマンドの送受信も拾う |
| `layout` | 連携ファイルのレイアウト定義（固定長の項目一覧・CSV 項目定義・COBOL コピー句） | 項目ごとに開始位置・桁を持つ「レイアウト項目」を作り、同じ物理名の列と、レイアウトが定める連携ファイルにつなぐ（対象ファイルは `rules/data-layouts.yaml`） |
| `sql` | *.sql、および他の抽出器が見つけた SQL 文字列 | sqlglot で CRUD・列を解析（失敗時は正規表現）。CREATE TABLE・コメント・PL/SQL・PL/pgSQL のオブジェクト |
| `props` | *.properties・YAML | メッセージキー（全ロケール）・設定キー。設定値に書かれた連携ファイル名・クラス名 |
| `jobs` | JP1/AJS の定義（ajsprint 形式）・crontab | ジョブネット・ジョブ・先行関係・起動するスクリプト |
| `docs` | Excel・Word・Markdown（表も）・設計フォルダの CSV（.xls/.doc は LibreOffice で変換） | 表の見出し行を自動で探し、行ごとに「設計書の項目」を作る。CRUD表（機能×テーブル）も検出。読めたもの・読めなかったもの（表の外の文字、図形・テキストボックス・画像、意味の分からない見出し）は `himotoki docs-check` とビュワーの「設計書の取込確認」で原本と見比べられる。「桁数」「対応カラム」の列は桁と列の対応として読む |

**値の流れ（画面項目 → 列）**：Java のメソッドごとに「値がどこから来てどこへ行くか」を構文木から要約し（代入・getter/setter・`BeanUtils.copyProperties`・Map の定数キー・セッション属性・`request.getParameter`）、取り込みの最後に呼び出しをまたいでつなぎます。行き先は SQL のパラメータで、JDBC の `ps.setXxx(n, v)`（番号・`i++`・値を集めたリスト）、JdbcTemplate の引数、`:name`、MyBatis の `#{prop}`、iBATIS の `#prop#` が、SQL を解析して「何番目の ? がどの列か（登録・更新／検索条件）」に対応付けられます。結果は項目 → 列の `FLOWS_TO` として、通った箇所の一覧つきでグラフに入ります。名前の似かたからの推定はしません（追えない受け渡し＝実行時に作る Map のキーやリフレクションは、辺を作りません）。

リンクでは、**画面ID・ジョブID・帳票ID**（`HZK-G010`・`KOBB090`・`R_HAN030` など、区切りの書き方が違っても同じものとして扱う）を、その ID を名前に持つファイル・ジョブ・クラスに結びます。連携ファイルは、置き場所の変数と日付部分を除いた名前（`${OUT}/URIAGE_${YMD}.dat` と `URIAGE_yyyymmdd.dat` は同じ `uriage.dat`）で、書く側と読む側をつなぎます。列の詳細には **桁の流れ**（DDL・設計書・画面の maxlength・入力チェック・連携レイアウト・帳票の桁）を並べ、DDL と違う桁に印を付けます。

抽出結果は `FileFacts`（ノード、ファイル内の関係、`table:reserve` や `view:reservetour/reserveForm` のような**キーへの参照**）という共通形式で、ファイルごとに独立しています。そのため並列に処理でき、キャッシュも効きます。

フレームワークの知識は `backend/himotoki/rules/*.yaml` にあります。

| パック | 内容 |
|---|---|
| `java-common` | エンドポイントのアノテーション（Spring MVC・JAX-RS）、DI、テスト、文字列リテラルを照合する名前空間 |
| `jpa` | エンティティ・列・関連、@Query/@Select などの SQL、Spring Data の派生クエリ（`findByZipStartingWith` → `Customer.zip`） |
| `mybatis` | Mapper XML の statement・`<sql>`・`<include>`・resultMap、インターフェースとの結び付け |
| `spring-xml` | Bean 定義・プロパティ参照、Spring Batch の job/step/tasklet/chunk |
| `struts1` | action・form-bean・forward |
| `web-templates` | JSP / Thymeleaf / FreeMarker の項目バインド・メッセージ・リンク |
| `jee-remoting` | EJB（コンテナ管理／Bean管理トランザクション、MDB）・ejb-jar.xml・weblogic-ejb-jar.xml・WebLogic の JDBC データソース・JNDI、JAX-WS（SOAP）のサービスとクライアント・WSDL、Spring `@Transactional`、Java 文字列に直接書いた SQL（JDBC）とストアド呼び出し |
| `scripts-jobs` | シェルから起動される Java メインクラス・バッチジョブ・SQL（sqlplus/psql）・他スクリプト、読み書きするデータファイル（cp・sftp/ftp の put/get・scp・SQL*Loader の制御ファイルとデータ）、埋め込みSQL |
| `office` | 設計書の見出しの語彙（テーブル名・項目名(物理)・画面ID・ジョブ名…）と CRUD 記号 |
| `propagation` | 影響が伝わる関係・向き・重み（エンジンはこの表だけを見る） |

`HIMOTOKI_RULES` で指定したディレクトリのパックも読み込みます。`samples/rules/ofbiz.yaml` は、Apache OFBiz（エンティティエンジン・サービスエンジン・リクエストコントローラ・スクリーン・ラベル・Groovy）を**コードを1行も書かずに**ルールだけで取り込む例です。

#### 社内フレームワークへの対応例

SQL を名前付きで XML に置き、Java から `run("findOrder")` のように呼ぶフレームワークの場合、次のパックを置くだけで Java → SQL → テーブル・列 の経路がつながります（`backend/tests/test_mechanism.py` で検証）。

```yaml
name: inhouse
java:
  literal_namespaces: [q]          # Java の文字列リテラル（定数経由を含む）を q: キーとして照合
xml:
  - name: query
    when: {root: queries}          # ルート要素が <queries> のファイルだけ
    select: /queries/query
    node:
      kind: SqlStatement
      id: "q:{@name}"
      name: "{@name}"
      keys: ["q:{@name}"]
      sql: body                    # 本文を SQL として解析し、テーブル・列の READS/WRITES を作る
```

XML ルールのテンプレートには `{@attr}`（自要素の属性）・`{/@attr}`（ルート）・`{../@attr}`（親）・`{ancestor::tag@attr}`・`{>child/path}`（子要素の本文）・`{..>child}`（親の子要素）・`{text}`・`{file}` を使えます。`|` で代替（最初に値があるもの）、`:snake` `:lower` `:basename` などのフィルタも指定できます。ほかに `where`（属性条件）・`vars`（ファイルパスから正規表現で取り出す変数）・`links`（キー同士の関係）・`sql_skip`（SQL に含めない子要素）があります。テキストルールは正規表現と `{1}` などのグループ参照、`sql: true`（捕まえた文字列を SQL として解析）で書きます。

Java のアノテーションは `java.annotation_rules` で意味を与えます（`on: class|method|field`、`requires`・`requires_class`・`class_kind`、`keys`・`props`・`refs`）。テンプレートには `{method}` `{class}` `{fqn}` `{field}` `{type}`、要素の `{@Ann.attr}`、クラスの `{class@Ann.attr}`、代替 `|`・既定値 `'REQUIRED'`・フィルタ `:last` などが使えます。たとえば SOAP は、サーバ側の `@WebService` 実装に `soapop:{名前空間}#{オペレーション}` というキーを持たせ、クライアント側のインタフェースのメソッドから同じキーを参照するだけで、リポジトリをまたいでつながります。

### 2. リンク: 根拠と確度つきの関係

参照のキーは、まず完全一致で、次に名前空間ごとの解決で関係になります。名前空間ごとの解決には次のものがあります。

- Java の型解決（入れ子・明示 import・同一パッケージ・ワイルドカード・一意な単純名）
- 受け手の型からのメソッド解決、インターフェース／抽象メソッドから実装への `IMPLEMENTED_BY`
- URL テンプレート、ビュー名の揺れ、`output.reserve.remarks` のようなプロパティパス
- テーブル・列（別名・候補テーブル）、ジョブ名、Bean 名、データファイル（ファイル名で照合するので、送信側の `/data/out/orders.csv` と受信側の `/recv/orders.csv` が同じ連携ファイルになる）
- ejb-jar.xml の `<method-name>*</method-name>`（その EJB の全メソッド）

すべての関係には次の 2 つが付きます。

- `src`: 根拠の種類。構文（parser）／ルール名（rule:mybatis）／DB辞書（catalog）／実行時（runtime）／設計書（doc）／推定（heuristic）
- `conf`: 確度

エンジンは「伝播ルールの重み × 確度」で影響の強さを計算します。

リンクの結果、グラフが**自分で分かっている欠け**を報告します（CLI `report`、API `/api/index/report`、ビュワーの「取込状況」タブ）。

- 解決できなかった参照（名前空間ごとの件数と例）。リポジトリに無いパッケージへの参照はライブラリとして別に数えます。
- DB辞書とリポジトリの DDL のずれ（DB にだけある列、DDL にだけある列、辞書に無いテーブル）
- 実行時SQLのうち、コード中のどの SQL とも対応しないもの（動的SQL・他システム・運用ツールといった静的解析の死角）

### 2.1 サーバ間連携とトランザクション

SoR では 1 つの業務が複数のサーバにまたがります。次の連携を、リポジトリをまたいで関係にします。

| 連携 | つなぎ方 |
|---|---|
| SOAP | クライアントのインタフェース（`@WebService`）→ サーバの実装。名前空間＋オペレーション名で照合。WSDL の operation も同じキーで結ぶ |
| REST | クライアントのパスのリテラル → `@Path` / `@RequestMapping` のエンドポイント |
| JMS | `@Resource(lookup=…)` のキュー → `@MessageDriven(mappedName=…)` |
| JNDI | `@Resource` のデータソース → WebLogic の JDBC モジュール（ドライバ・URL・2相コミットの設定）。weblogic-ejb-jar.xml の jndi-name |
| DB リンク | `INSERT INTO T@LINK` → テーブル T（`dblink` 属性付き）と `CREATE DATABASE LINK` の定義 |
| ストアド | JDBC の `{call PKG.PROC(?)}`・PL/SQL 内の呼び出し → パッケージ内の手続き単位のノード |
| ファイル連携 | SQL*Plus の SPOOL、cp、sftp/ftp の put/get、scp、SQL*Loader、JP1 のファイル監視（`flwf`）、Java のファイル読み書き → 同じ名前のデータファイル |

インデックスの最後に**トランザクション解析**を行います（`pipeline/transactions.py`）。境界（EJB のコンテナ管理トランザクション。ejb-jar.xml の指定 → メソッドのアノテーション → クラスのアノテーション → EJB 既定の REQUIRED の順。Bean 管理の `UserTransaction`、Spring `@Transactional`、自分で COMMIT するストアド）から、呼び出し・実装・SQL 実行・ストアド呼び出しをたどって範囲を求めます。REQUIRES_NEW / NOT_SUPPORTED の呼び出し先とサーバ外への呼び出しで止めます。範囲の中で次を注意点として記録します。

| コード | 内容 |
|---|---|
| `REMOTE_IN_TX` | トランザクション内の SOAP / REST 呼び出し（ロールバックしても相手の更新は戻らない） |
| `INDEPENDENT_TX` | REQUIRES_NEW などで別トランザクションとして先に確定する呼び出し |
| `PROC_COMMIT_IN_TX` | コンテナ管理トランザクションから呼ぶストアドが内部で COMMIT / ROLLBACK する |
| `AUTONOMOUS` | PRAGMA AUTONOMOUS_TRANSACTION |
| `DBLINK_WRITE` | DB リンク越しの更新（分散トランザクション） |
| `COMMIT_IN_LOOP` | ループ内の分割コミット（ストアド・Bean 管理トランザクション） |
| `LOCK` | SELECT … FOR UPDATE などの行ロック |
| `ASYNC_MESSAGE` | トランザクション内の JMS 送信 |
| `FILE_OUTPUT_IN_TX` | トランザクション内のファイル出力 |

結果は要素の属性（境界の種類・範囲の要素数・更新テーブル・リモート呼び出し、注意点）になり、取込状況（`transactions`）、ビュワーのリスト行のバッジと詳細パネル、レポートの「トランザクション・連携上の注意」に出ます。

**システム単位のグラフ**: 複数のリポジトリを 1 つのグラフにまとめます。ファイルは `<リポジトリ名>/<パス>` として入り、上の連携がサーバをまたいでつながります。

```bash
himotoki index --system sor --repo order-web=/src/order-web --repo /src/inventory-svc --repo /src/shipping-svc
himotoki diff --graph sor --repo order-web --base main --head feature/x   # 変更したリポジトリを指定
```

自作のサンプル `backend/himotoki/demo/sor-env`（受注・在庫・出荷の 3 サーバ）で、受注の NOTE 列の拡張が、出力 SQL → CSV → sftp 送信 → ファイル監視ジョブ → 取り込み → 出荷側の SHIPMENT_REQ.NOTE まで届くことを確認しています。

### 3. 増分・スナップショット・規模

- **抽出キャッシュ**: `facts.db`（SQLite）に (パス, git の blob SHA, 抽出器, 抽出器のコード, ルールのダイジェスト) をキーとして保存します。変更の無いファイルは再抽出しません。ルールや抽出器を直すと、影響する分だけ再抽出されます。
- **ワークスペース**: ブランチ・タグごとに git worktree を作り、別のグラフとして持てます（`graphs/<repo@ref>/`）。抽出キャッシュはグラフ間で共有します。
- **大規模向けの探索**: エンジンは BFS の 1 段ごとに近傍をまとめて取得します（`neighbors_many` / `get_many`）。Neo4j ストアでは 1 段 1 クエリ（UNWIND）になります。

### 4. 解析エンジン

| 工程 | 内容 |
|---|---|
| 要求の構造化 | Claude（APIキーがあるとき）またはルールベースで変更意図に分ける（追加／変更／削除、値の変更、新しい語） |
| 起点 | 識別子の一致、用語集、変更前の値（「80文字」→ 80 を持つ要素）、類似度 |
| 伝播 | `propagation.yaml` の関係・向き・重みで閉包を計算。設計書・テスト・SQLファイルなどは終端（SQLファイルはそれを起動するジョブと、書き出すデータファイルへは伝わる）。データ連携（ファイルの書き出し → 読み手 → ファイル監視ジョブ → 後続ジョブ）は `flow` 指定の関係だけで深さの上限を超えてたどる（`flow_depth` 既定 8 段） |
| 判定 | 確度・理由・変更種別 |
| 横断検索 | 起点の語でリポジトリ全体を検索（既定は git grep。`HIMOTOKI_CODESEARCH` で Zoekt・GitLab のコード検索に切り替え、ソース管理サーバ側の全ブランチ・全リポジトリを対象にできる） |
| 独立再探索 | 依存グラフを使わない別戦略で探し、本探索と突き合わせる |
| CRUD確認 | 影響テーブルを読み書きする全箇所（コード・SQL・ジョブ・設計書の CRUD 表・実行時SQL） |
| 規模 | 係数表 ＋ 複雑度・行数・呼び出し元数・テスト有無・ロケール数 |

**差分モード**: 2 つの ref の差分（MR と同じく merge-base からの差分）の変更行を含む要素を起点にして、回帰確認の範囲（実行すべきテスト・画面・ジョブ・設計書）を出します。

## ソース管理サーバとの連携

- **Webhook**（`POST /api/hooks/git`）: GitLab・GitHub の push と MR/PR を受けます（`HIMOTOKI_WEBHOOK_SECRET` で GitLab トークン／GitHub の HMAC 署名を検証）。
  - push: そのブランチのグラフを更新します（変更ファイルだけ再抽出）。
  - MR/PR: ターゲットブランチのグラフで差分モードを実行し、`GITLAB_TOKEN` / `GITHUB_TOKEN` があれば回帰確認の範囲を MR/PR にコメントします。
- **コード検索**: `HIMOTOKI_CODESEARCH=zoekt:http://zoekt:6070` または `gitlab:https://gitlab.example.com/api/v4:<project id>`
- **MCP サーバ**（`himotoki mcp`、stdio）: Claude Code などのエージェントが次のツールでグラフを使えます。
  - 最初に使う: `impact_files`（要求から、変更が要りそうなファイルを順位付きで返す。各ファイルの主な要素と、要求からそこに至る経路付き。2〜3 千トークン程度）
  - `list_graphs`・`search_elements`・`get_element`（要素の場所・ソース・使う／使われる・設計書・テストを簡潔に）・`neighbors`
  - `analyze_requirement`（要素単位の上位 20 件）・`analyze_change`・`index_gaps`
  - 開発への引き渡し: `list_runs`（解析の一覧）・`get_handoff`（確定した影響範囲の作業指示）・`verify_change`（実装後の差分と予測の照合）
  - 学習: `learned_knowledge`（過去の判断・実績から学んだこと）

  Claude Code への登録: `claude mcp add --transport stdio --env HIMOTOKI_DATA_DIR=<ワークスペース> himotoki -- himotoki mcp`

  リポジトリの近くで動かせば、コードをネットワーク外に出さずに構造だけを渡せます。MCP サーバは起動と同時に既定のグラフを裏で開き、類似検索の索引はグラフの横に保存して再利用します（Fineract 規模で、初回の呼び出しまでの待ちが約 35 秒 → 約 7 秒、2 回目以降の `impact_files` は約 1 秒）。

SVN は対象外です（git 前提。必要なら git-svn で移行・ミラーしてください）。

## 起動

### ローカル（開発）

利用者向けの手順は [GETTING_STARTED](docs/GETTING_STARTED.md) にあります。`himotoki init <フォルダ>` で作るワークスペース（`repos/` `docs/` `db/` `rules/` `settings.yaml`）の中では、`himotoki index` を引数なしで実行すると `repos/`（複数ならシステム単位）・`docs/`・`db/` の Oracle 辞書 CSV をまとめて取り込み、`rules/` のルールも読み込みます。`himotoki doctor` は環境とワークスペースを確認して、次にすることを表示します。

以下は個別のコマンドです。

```bash
cd backend
pip install -e .            # `himotoki` コマンドが使えるようになる（python -m himotoki.cli でも同じ）
export HIMOTOKI_DATA_DIR=./data   # ワークスペースの中では不要

# インデックス（設計書・ジョブ定義・運用シェルのフォルダは --docs）
himotoki index --repo /path/to/repo --docs /path/to/ops-and-docs [--ref release/1.2]
# 複数サーバのリポジトリを1つのシステムとして（名前=パス、または パスのフォルダ名が名前）
himotoki index --system sor --repo /src/order-web --repo /src/inventory-svc --repo /src/shipping-svc

# DB辞書と実行時SQL（任意。取り込んだら index をやり直すとリンクされる）
himotoki catalog postgres --dsn "postgresql://user@host/db" --name proddb
himotoki catalog oracle --csv-dir ./dict_export --name ora      # ALL_TAB_COLUMNS 等の CSV
himotoki catalog oracle --dsn host:1521/svc --user u --password p --owner APP --name ora
himotoki runtime --pg "postgresql://user@host/db" --name prod-rt  # pg_stat_statements
himotoki runtime --csv sql_stats.csv --name ora-rt               # V$SQL などのエクスポート

himotoki report                       # 取込状況（未解決・ずれ・実行時SQLの対応）
himotoki docs-check [FILE…]           # 設計書の取込確認（読めた表・読めなかった図形や文字・結びつき → HTML）
himotoki analyze "要求文"            # 要求から
himotoki diff --base main --head feature/x   # 差分から（回帰確認の範囲）
himotoki knowledge                     # 過去の判断・実績から学んだこと（analyze --no-learning で使わずに解析）
himotoki runs                          # 解析の一覧（run ID・承認・確定件数）
himotoki handoff <run ID> --out 作業指示.md   # 確定した影響範囲を開発向けの作業指示に
himotoki verify <run ID> --base main --head feature/x [--repo order-web] [--actual-days 3]  # 実装後に予測と照合
himotoki serve --port 8765            # ビュワー http://localhost:8765
himotoki mcp                          # MCP サーバ（stdio）
```

ビュワーはビルド済みのものを Python パッケージ（`backend/himotoki/web`）に同梱しているので、利用者は Node.js を入れる必要がありません。ビュワーを改造するときは `cd frontend && npm install && npm run dev` で開発サーバを起動し、`npm run build` で同梱分も更新します（更新し忘れはテストで検出します）。ソースの文字コードは UTF-8 と Shift_JIS（Windows-31J）を自動で判別します。

`serve` は既定で 127.0.0.1 だけで待ち受けます。ほかのマシンから使う場合は `HIMOTOKI_API_TOKEN` と `HIMOTOKI_ALLOWED_HOSTS` が必要です（無いと起動しません）。

### Docker（Neo4j 構成）

```bash
cp .env.example .env          # HIMOTOKI_API_TOKEN・NEO4J_PASSWORD（必須）、ANTHROPIC_API_KEY・REPOS_DIR など
docker compose up --build     # http://localhost:8765/?token=<HIMOTOKI_API_TOKEN> で一度開く
```

## セキュリティ

このサーバはソースコードと設計書を返すため、次の対策を入れています。

- 既定では同一オリジン・ローカルのホスト名からのアクセスだけに応答する
- トークン認証（ビュワーは HttpOnly クッキー）
- 読めるファイルはリポジトリと設計書フォルダの中に限る
- API で指定できるパスは許可ルート配下に限る
- Webhook には署名を必須にする
- 細工されたリポジトリへの防御: シンボリックリンク、巨大ファイル、XML の実体参照、zip 爆弾
- エラー応答から内部情報・資格情報を除く

想定する脅威・設定・残っているリスク・エラー処理の方針は [docs/SECURITY.md](docs/SECURITY.md) にあります。

## ビュワーでできること（上の表の工程 2〜4）

すべて影響範囲を確定させるための操作で、コードを編集する機能はありません。

| やりたいこと | 操作 |
|---|---|
| 候補を順番に確認する | 「リスト」表示（解析が終わると自動でこちら）。確認する順（影響ありの可能性が高い → 要確認 → 影響は低そう）に並び、各行で確定・除外できる。チェックやグループ単位でまとめて判断、キー操作（↑↓・c・x・u）、行を選ぶと要求からの経路を表示。「グラフ」表示といつでも切り替えられる |
| 要素ごと・絞り込み範囲の規模を見る | リストの各行に要素の工数（確定分は工数、未判定は「工数 × 確からしさ」の見込み）。層・未判定だけ・確度・検索で絞り込むと「この一覧の規模」を全体と並べて表示（グラフ表示でも同じ）。「規模」タブに層ごとの内訳（行を押すとその層だけに絞り込み） |
| はじめて使う | 初回はリストの上と詳細パネルに 1 行のヒントが出る（「わかった」で次から出ない）。右上の「?」で画面全体の案内（最後の「例文で練習する」で、このリポジトリの実際の項目から作った練習用の要求で解析を始められる）。「確度」「全数判定」「規模」などの点線の付いた言葉は、マウスを乗せると説明が出る |
| 次に何をするか迷わない | ヘッダーに作業の段階（候補を判断 → 全数判定 → 承認 → 開発へ渡す）と、いまの段階でやることのボタンが 1 つ出る（「次の未判定へ」「全数判定を確認」「承認する」「開発へ渡す」） |
| 判定の出どころを見分ける | 状態の表示に判定方法を付ける（「影響あり（AI）」「要確認（ルール）」など）。色は意味ごとに 1 つ: 青＝判定が影響あり、黄＝要確認・注意、水色＝確定、赤＝重大な注意、紫＝学習 |
| リポジトリを取り込む・更新する | 「新しい解析」→「取り込み・更新…」（初回は自動で開く）。単一リポジトリ、または 1 行に 1 つで複数サーバのシステム |
| 要求を書く | 「新しい解析」の入力欄の下に、よくある変更のテンプレート（値・桁数の変更、項目の追加・削除、処理・条件の変更、バッチ・連携の変更）と、入力中の確認を表示する。確認では、どう読み取ったか（追加／変更／削除・手がかりの語）、起点になりそうな要素、注意点（対象が見つからない・あいまいな言葉・変更前の値がない・複数の変更が混在・テンプレートの〇〇が残っている）を出す。ルールベースのみでAIは使わず、解析の記録も残らない |
| 解析の進行をリアルタイムに見る | 解析中はノードが内側から増え、ヘッダーに工程と進捗、ログタブに手順が流れる |
| 大量の候補を見通す | 候補が 150 件を超えると、確度の低い候補をモジュール・領域ごとに 1 つのノードにまとめる（クリックで展開。確度の高いもの・判断済み・選択中は常に表示） |
| 本当に影響があるか確かめる | 行やノードを選ぶと、右の「詳細」に中身が出る（右列いっぱい。「⇤」で横幅を広げられる）。コードは**ファイル全体**を該当箇所までスクロールして表示（該当行をハイライト、ファイル内の要素へ移動・検索）。**列・テーブル**は「スキーマ（DDL に書かれていれば）› テーブル（論理名）› 列（型）」の見出しと、定義している DDL の行、その列を読み書きしている処理（メソッド名と C/R/U/D）、列を指定せずにテーブルを読み書きしている処理、設計書の行、テーブルの全列。**ジョブ・シェル・バッチ**は「ジョブの流れ」に、ジョブネットの中の順序、先行・後続ジョブ、実行の流れ（ジョブ → シェル → Java／SQL。`run_java` のようなラッパー関数経由も）、読み書きするテーブル・ファイル、書いたファイルを次に読むジョブ。プログラムは「触るテーブル」。これらはサーバ上のファイルとグラフから読むだけで、AIは使わない |
| 周辺コードを辿る | 詳細の「つながり」タブに、呼び出し元・呼び出し先・読んでいる処理・先行／後続・設計書などを関係ごとにまとめて表示。名前を押すとその要素の詳細へ移動し（←→で戻る・進む、解析結果は変わらない）、候補にしたいものだけ「＋候補」で追加する。図でたどるときは表示の「つながり」: 選んだ要素を中心に、左に呼び出し元・使っている側、右に呼び出し先・使っているものを並べ、ダブルクリックでその先を1段ずつ展開（関係の種類で絞り込み可） |
| AIの判断を直す | 影響ありで確定／除外（理由必須）／未判定に戻す、変更種別・難易度の補正、複数選択でまとめて判断 |
| 漏れを指摘する・再調査させる・質問する | 指示バーの「漏れを追加」「再調査」「質問」 |
| グラフの欠けを知る | 「取込状況」タブ: 解決できなかった参照、DB辞書とDDLのずれ、コードに対応の無い実行時SQL |
| 差分から始める | 「新しい解析」→「変更差分から」で比較元・変更後を指定 |
| 全数として確定する | 全数判定タブの条件をすべて満たすと承認できる。判断を変えると承認は自動で外れる |
| 規模を見る・報告する | 規模タブ、レポート（Markdown） |
| 使うほど精度を上げる | 確定・除外の判断と、実装との照合結果を、システムごとに学習する。次の解析から、よく除外される経路・要素は確度を下げ（前回の除外理由を表示）、よく確定される経路は上げ、実際には変更されたのに除外・未確定だった要素と、依存関係からは見つからず実際に一緒に変更された要素（学習した関係）は必ず「要確認」以上で出す。リストの「学習」バッジ、詳細の「過去の判断・実績」、右下の「学習」タブで中身を確認でき、項目ごとに無効にできる。「新しい解析」で解析ごとに切ることもできる |
| 開発へ渡す | 右上の「開発へ渡す」: 「issue 案」タブに、要求ごと（またはサブシステムごとに分割）の issue の文面（背景・変更内容・変更対象のチェックリスト・DB の列とテーブル・ジョブの前後関係・確認する設計書とテスト・受け入れ条件。本文に `himotoki-run` の印）。ビュワーは git に触れず、コピーするか、手元の Claude Code のスキル `/himotoki-issue` で登録し `/himotoki-dev` で着手する（[claude-code/](claude-code/README.md)）。「作業指示」タブには確定した要素をファイルごとにまとめた作業指示（理由・コメント・トランザクションの注意・関連する設計書・呼び出し元・テスト・除外・未判定）をコピー／ダウンロード。Claude Code に MCP で登録するコマンドと依頼文もここから。実装後は「実績と照合」で作業前・作業後のブランチを指定し、予測が当たった割合・予測漏れ（候補にあったが判断が違った／候補に無かった）・変更されなかった予測を確認、実績の人日で規模を補正 |

## 「これで全数」の判断

100%の網羅は証明できないため、**定義した探索範囲に未判定が残っていないこと**を全数の定義にしています。

1. **変更意図ごとの起点**: 変更意図すべてに、確定した修正箇所がある
2. **依存の閉包に未判定がない**: 依存グラフで辿った候補がすべて確定か除外
3. **キーワード・SQL文字列の横断検索**: 起点の語でヒットした要素がすべて判定済み
4. **影響テーブルのCRUD全箇所**: 影響テーブルを定義・投入・マッピング・検索する箇所がすべて判定済み
5. **独立した再探索との突き合わせ**: 別戦略で見つけた要素がすべて判定済み
6. **実行時SQLとの突き合わせ**（実行時SQLを取り込んだ場合）: 影響テーブルに実際に触れた SQL がすべて判定済み。コードに対応が無い SQL は、静的解析では見えない利用者がいることを示す

あわせて「取込状況」のずれ・未解決参照も確認できます。

## AIの使用量と設定

AI（Claude）を使うのは「要求の構造化」と「候補の判定」と「ビュワーでの質問」だけです。インデックス作成や、ビュワーでの閲覧・確定・除外には使いません。判定は候補数に比例してトークンを使うため、次の3つで量を抑えます。

| 仕組み | 内容 | 設定（既定値） |
|---|---|---|
| 再調査での判定の再利用 | 親の解析でAIが判定した候補は判定し直さない。新しい根拠で点数が一定以上上がった候補だけ再判定する | `reuse_verdicts`（true）、`rescore_delta`（0.1） |
| 低スコアはルールで判定 | 点数が閾値未満の候補はAIに送らない | `min_score`（0.3） |
| 1回あたりの上限 | 点数の高い順に上限件数までAIで判定し、残りはルールで判定する（要確認として残る） | `max_items`（300。`unlimited` または -1 で上限なし、0 でAI判定なし） |

あわせて、横断検索・独立再探索・CRUD確認で見つかった候補も、1件ずつではなくまとめて判定するようにしました（`batch_size`、既定 15 件／回）。

**設定の場所**（下ほど優先）:

1. 設定ファイル `<HIMOTOKI_DATA_DIR>/settings.yaml`（場所は `HIMOTOKI_SETTINGS` で変更可）。例は `samples/settings.example.yaml`
2. 環境変数 `HIMOTOKI_LLM_MIN_SCORE`・`HIMOTOKI_LLM_MAX_ITEMS`・`HIMOTOKI_LLM_REUSE_VERDICTS`・`HIMOTOKI_LLM_RESCORE_DELTA`・`HIMOTOKI_LLM_BATCH_SIZE`
3. 解析ごとの指定:
   - API の `llm_max_items` / `llm_min_score`
   - CLI の `--llm-max-items` / `--llm-min-score`
   - MCP の `llm_max_items`
   - ビュワーの「新しい解析」の入力欄

解析ごとの内訳（AIで判定・再利用・ルール判定・API呼び出し回数・実際の入出力トークン数）は、ビュワーの「規模」タブ、CLI の出力、API の `llm_usage` で確認できます。現在の既定値は `GET /api/settings/llm` で確認できます。

**費用のリアルタイム表示**: ビュワーのヘッダーに、表示中の解析のAI費用（円）とトークン数、ワークスペース全体の今月の費用を表示します。

- 解析中は AI を1回呼ぶたびに更新されます。トークン数は API が返す実際の値です。
- マウスを重ねると、入力・出力の内訳、API 呼び出し回数、モデル、為替レート、今日・今月の合計が出ます。
- 「規模」タブにも費用の内訳を表示します。
- 集計は `GET /api/usage`（今日・今月・日別）で取れます。呼び出しごとの記録は `<HIMOTOKI_DATA_DIR>/usage.db` に残ります。

単価（USD／100万トークン）と為替レートは `settings.yaml` の `pricing` で設定します。

- 既定値は 2026-10-10 時点の公開価格（Sonnet 5.5 は入力 $2・出力 $10）と 1ドル＝158円です。価格改定や契約単価に合わせて更新してください。
- 単価が未設定のモデルを使った呼び出しは費用に含めず、その件数を表示します。

**測定結果**（ツアー予約の「備考 80→200文字」、候補 243 件）: Claude の呼び出し部分を記録用に差し替えて送信内容を数えたもので、トークン数は文字数からの換算です。

| 条件 | AIで判定 | API呼び出し | トークン（入力＋出力） |
|---|---|---|---|
| 変更前（1件ずつの判定あり、再利用なし） | 243 | 102 回 | 約 88,000 |
| 上限なし・閾値 0 | 243 | 19 回 | 約 78,000 |
| 既定（閾値 0.3・上限 300） | 216 | 17 回 | 約 71,000 |
| 閾値 0.4 | 59 | 5 回 | 約 20,000 |
| 閾値 0.5 | 25 | 3 回 | 約 8,500 |
| 上限 100 | 100 | 8 回 | 約 32,000 |
| 再調査（既定、変更前は約 78,000） | 4（212 件は再利用） | 2 回 | 約 3,600 |

閾値や上限を下げるほど、ルールで判定されて「要確認」のまま残る候補が増え、人の確認の手間が増えます。既定値は網羅を優先した値です。

## 規模見積もり

`backend/himotoki/analysis/estimate.py` の係数表（要素種別 × 変更種別の基準時間。ジョブ・スクリプト・SQL文・DBオブジェクト・設計書の項目も含む）を基準にします。これに循環的複雑度・行数・呼び出し元数・難易度補正・ロケール数を掛け、テストが無い要素にはテスト作成時間を加えます。最後にレビュー等の係数（既定 1.3）と、過去実績から求める補正係数（`POST /api/estimator/calibrate`）を掛けて人日にします。

## API

| メソッド | パス | 用途 |
|---|---|---|
| GET | /api/status・/api/graphs | 状態・グラフ（ブランチ・タグごと）一覧 |
| GET | /api/settings/llm | AI判定の既定の閾値・上限、単価・為替レート |
| GET | /api/usage | AIの使用量と費用（今日・今月・日別、円換算） |
| POST | /api/requirements/preview | 要求の書き方の確認（読み取り・起点・注意点。AIは使わない） |
| GET | /api/requirements/example | チュートリアル用の練習の要求（リポジトリの項目から作成） |
| POST | /api/index | インデックス（repo, docs_dir, ref, graph）。システム単位は `repos: {名前: パス}` と `graph` |
| GET | /api/index/report | 取込状況（未解決参照・ずれ・実行時SQLの対応・エラー） |
| GET | /api/docs | 取り込んだ設計書の一覧（読んだ行・節・結びつきの数） |
| GET | /api/docs/check?path= | 設計書 1 本の取込確認（取り込み済みの設計書だけを読み直す） |
| POST | /api/sources/catalog・/api/sources/runtime | DB辞書・実行時SQLの取込 |
| POST | /api/runs | 要求から解析（`llm_max_items`・`llm_min_score` で今回だけAIの上限・閾値を変更） |
| GET | /api/knowledge | このシステムで学習したこと（経路の傾向・要素の履歴・学習した関係） |
| POST | /api/knowledge/ignore | 学習した項目を無効にする／戻す（kind: pattern・edge・node・link） |
| GET | /api/runs/{id}/handoff | 開発向けの作業指示（JSON と Markdown。`pending=false` で未判定を除く） |
| POST | /api/runs/{id}/verify | 実装後の差分と予測の照合（base, head、システム単位では repo、任意で actual_days） |
| POST | /api/runs/diff | 差分から解析（base, head。システム単位のグラフでは repo も） |
| GET | /api/runs/{id}・/api/runs/{id}/scope | 状態・回帰確認の範囲（Markdown 付き） |
| WS | /ws/runs/{id} | イベントストリーム |
| POST | /api/runs/{id}/decisions(/bulk)・candidates・reinvestigate・approve・ask・comments | 人の操作 |
| GET | /api/runs/{a}/diff/{b}・/api/runs/{id}/report | run の差分・レポート |
| GET | /api/nodes/{id}・/api/search | ノード詳細・検索 |
| POST | /api/hooks/git | GitLab / GitHub の Webhook |
| POST | /api/estimator/calibrate | 規模の補正係数 |

## 検証結果

判定はすべてルールベース（APIキー無し）です。

### TERASOLUNA ツアー予約（MyBatis3・JSP・PostgreSQL）＋ 周辺資産

[terasoluna-tourreservation-mybatis3](https://github.com/terasolunaorg/terasoluna-tourreservation-mybatis3) には業務システムで典型的な周辺資産がありません。そこで、次のものを検証用に作成して加えました（`samples/tour-env`、公式資料ではありません）。

- JP1 ジョブ定義・crontab・運用シェル（psql、連携ファイル、ヒアドキュメントの DELETE）
- Excel のテーブル定義・画面項目定義・CRUD 表、Word の基本設計書
- DB にだけ当てた列・ビュー・関数・トリガー
- 実行時SQL

インデックス結果は次のとおりです。

- 201 ファイル → 1,743 ノード・5,214 関係（0.6 秒）、解決できない参照は 0 件
- DB辞書（PostgreSQL 16）との比較で、DB にだけある列 `customer.customer_rank` を検出
- 実行時SQL 8 件のうち 5 件がコード中の SQL と対応。残り 3 件（コードに無い集計 SQL、DB にだけあるビューの参照、件数取得）を死角として報告

**要求「予約の備考欄(remarks)の文字数上限を80文字から200文字に変更する」**（正解 18 項目は `eval/tour_remarks_check.py`）:

| | 結果 |
|---|---|
| 候補に入った正解 | 18 / 18 |
| うち AI 判定「影響あり」 | 15 / 18（完了画面 JSP は要確認、ジョブ J010 と初期データ投入関数は影響低） |
| 候補数 | 248（影響あり 29・要確認 191・影響低 28） |

経路の例:

- フォーム `@Size` → メッセージキー → 定数クラス
- Entity → Mapper XML の各 statement → 列 → DDL・Excel のテーブル定義行・画面項目定義行・Word の節
- PDF 出力用 DTO → PDF ビュー
- 列 → 夜間ジョブの SQL（export_reserve.sql）→ それを起動するシェル → JP1 ジョブ

**履歴による評価**（`backend/scripts/eval_history.py`）: 書式変更・バージョン更新などを除いた直近 25 コミット（変更ファイル計 92）を対象にしました。コミットメッセージを要求として親コミットの時点で解析し、実際に変更されたファイルが候補に入ったか、候補一覧（確度順）の上位何件に入ったかを見ています。

| 指標 | 値 |
|---|---|
| 候補に入った変更ファイルの割合 | 72.8% |
| 上位 20 / 50 / 100 件に入った割合 | 38.0% / 54.3% / 60.9% |
| AI 判定「影響あり」に入った割合 | 22.8% |
| 1 件以上当たったコミット | 23 / 25（上位 20 件で 13） |
| 候補数（中央値） | 109 |

コミットメッセージは「fix #123」のように短く、要求文の代わりとしては不利な条件です。網羅は候補一覧とレビューで担保し、「影響あり」の絞り込みはルールベースでは弱い、という傾向がここでも出ています。

**差分モード・Webhook・MCP**:

- フォームの `@Size(max = 80)` を 200 にしたブランチの GitLab MR イベントを送り、署名検証 → ベースブランチのグラフ作成 → 回帰確認の範囲（画面 2・テスト 10・設計書の節）の生成までを確認しました。
- MCP は stdio の実クライアントから呼び出して確認しました（当初の 7 ツールすべてと、開発への引き渡しの list_runs・get_handoff・verify_change）。

### Apache Fineract（大規模な実システムの履歴）

コアバンキングの OSS（約 8.9 万ノード）で、2025 年 6 月以降の `FINERACT-` チケットのコミットから 30 件（変更ファイル計 156）を選び、件名を要求として解析しました（チケット番号は除去、判定はルールベース、英語のまま）。結果は `eval/fineract_history_results.json` にあります。

| 指標 | 語の特異度による重み付け前 | 後 |
|---|---|---|
| 上位 20 件に入った割合 | 11.5% | **25.0%** |
| 上位 50 / 100 件に入った割合 | 25.6% / 35.3% | **39.7% / 47.4%** |
| 上位 20 件で 1 件以上当たったコミット | 11 / 30 | **18 / 30** |
| 見つかったファイルの順位（中央値） | 109 位 | **31 位** |
| 候補数（中央値） | 3,262 | **774** |
| 候補のどこかに入った割合 | 75.0% | 65.4% |
| ルール判定「影響あり」に入った割合 | 37.2% | 38.5% |

重み付け前は、loan・note・text のようにシステム中で使われる語から数十の起点が同じ強さで生まれ、横断検索とテストへの伝播で候補が数千件に膨らんでいました。候補のどこかに入る割合は下がりましたが、落ちたファイルはいずれも重み付け前に 900 位より後ろにあったもので、レビューで読まれる範囲の外です。

### チームみらい（TypeScript・日本語の要求）

[marumie](https://github.com/team-mirai/marumie)（政治資金の公開、Next.js・Prisma）と [mirai-gikai](https://github.com/team-mirai/mirai-gikai)（議案の解説、Next.js・Supabase）で、日本語のコミット件名をそのまま要求として解析しました（各 30 件、`feat:` などの接頭辞は除去、判定はルールベース）。対象はテスト・e2e を除く *.ts・*.tsx・*.prisma・*.sql の変更です。結果は `eval/teammirai_history_results.json` にあります。

| 指標 | marumie<br>（語句照合なし） | marumie | mirai-gikai |
|---|---|---|---|
| 候補のどこかに入った割合 | 16.9% | **54.2%** | 43.3% |
| 上位 20 / 50 / 100 件に入った割合 | 7.9% / 11.9% / 15.8% | **24.9% / 33.3% / 39.5%** | 18.1% / 28.3% / 32.3% |
| 上位 20 件で 1 件以上当たったコミット | 10 / 30 | **20 / 30** | 15 / 30 |
| ルール判定「影響あり」に入った割合 | 6.8% | 26.6% | 21.3% |
| 候補数（中央値） | 108 | 197 | 201 |

日本語の要求と英語の識別子の間には、ルールベースでは橋がありません。そこで、要求から語句（漢字・カタカナ・英数の連なり、「」で囲んだ語）を切り出し、画面の文言・コメント・メッセージ・設計書にそのまま出てくる要素を、語の特異度で重み付けして起点にしています（左列はこの照合を入れる前）。設計書の節が合えば、その節が説明しているコードも起点になります。件名の語がコードにも画面にも現れない要求（「依存関係違反を修正」など）は、この方法では見つかりません。

1 コミットの取り込みは 1〜3 秒（約 1,300 ファイル）でした。

### Claude Code 単体との比較（手順のみ）

同じ影響調査の課題を、まっさらな Claude Code に「単体」と「himotoki の MCP あり」で解かせ、漏れ・余計な指摘・トークン・時間を比べる手順と道具を `eval/agent_compare/` に置いています（課題は Fineract・marumie の各 6 件。答えが見えないよう、git の履歴を除いたソースと Web の禁止で実行）。

### Apache OFBiz（規模・未知のフレームワーク）

`samples/rules/ofbiz.yaml` だけで取り込み、Java・Groovy・XML・FTL を含む 3,515 ファイルを処理しました（2 CPU）。

| 条件 | 結果・時間 |
|---|---|
| 初回 | 57,722 ノード・203,503 関係、19.4 秒（抽出 10.0 秒・リンク 4.8 秒） |
| 変更なし | 11.2 秒（再抽出 0、残りはキャッシュ読込・リンク・グラフ書き出し） |
| 1 ファイル変更 | 11.9 秒（再抽出 1） |
| 解決できない参照 | screen 28・method 7・service 2・file 1 |

### Spring PetClinic

v1 からの回帰確認です。正解リストは `eval/ground_truth.json`、手順は `python eval/run_eval.py --repo <petclinic> --docs samples/petclinic-design --decisions eval/decisions.json`。

| 要求 | 候補数 | 候補の必須網羅率 | AI判定「影響あり」の必須網羅率 / 適合率 | レビュー後の必須網羅率 / 適合率 | 規模 |
|---|---|---|---|---|---|
| R1 オーナーにメールアドレス追加 | 135 | 100% | 100% / 61% | 100% / 85% | 3.0人日 |
| R2 電話番号の桁数チェック変更 | 108 | 100% | 80% / 19%（検証テストが要確認止まり） | 100% / 53% | 1.4人日 |
| R3 獣医一覧のページサイズ変更 | 88 | 100% | 100% / 31% | 100% / 67% | 0.5人日 |

### テスト

`backend/tests`（67 件、すべてオフライン）:

- 小さな固定リポジトリでの一連の操作
- 一時リポジトリでの仕組みの検証: 社内フレームワークのルールパック、JP1→シェル→データファイル→後続ジョブ、Excel の見出し検出と CRUD 表、DB辞書のずれと実行時SQLの対応、キャッシュの増分、差分モード、Webhook の解釈、SQL の端のケース
- サーバ間連携とトランザクション（`test_sor_system.py`）: 3 リポジトリのシステム単位のインデックス、SOAP・REST・JMS・JNDI・DB リンク・ファイル連携のつながり、トランザクション境界と注意点、NOTE 拡張が出荷サーバまで届くこと、API
- 学習（`test_learning.py`）: 繰り返し除外された要素の確度が下がり理由が出ること、経路の傾向は一定件数の判断までは動かないこと、判断を戻すと学習も戻ること、再調査は同じ解析として数えること、照合で見つかった関係が次の解析で候補に出ること、取り込み直しても残ること、解析ごと・項目ごとの無効化、API と MCP
- 開発への引き渡し（`test_handoff.py`）: 作業指示の内容（ファイルごと・コメント・トランザクションの注意・定義場所・除外・未判定）、実装後の照合（当たり・判断の見直し・グラフの欠け・変更されなかった予測・規模の補正）、MCP と API
- 利用開始（`test_getting_started.py`）: 同梱ビュワーがソースと一致していること、ワークスペースの作成と検出、引数なしの取り込み、デモ、環境確認、Shift_JIS のソース
- 設計書の取込確認（`test_doccheck.py`）: 方眼紙型の Excel（結合セル・図形・線・グラフ・コメント・表の外の文字）と見出しを認識できない Word の表を報告すること、意味の分からない見出しの指摘、グラフとの結びつき、取り込み済みの設計書以外は API から読めないこと、CLI の HTML
- TypeScript（`test_typescript.py`）: tsconfig の `paths`・ワークスペースパッケージの `exports`・`export *` の再エクスポートを通じた解決、JSX のコンポーネント、Supabase・Prisma の読み書き、`implements` の実装関係、Next.js の画面・API、テスト、日本語の要求が画面の文言から画面とその部品に届くこと
- 語の特異度（`test_specificity.py`）: システム中で一般的な語ほど重みが下がり、境界がシステムの規模に合わせて動くこと、連結した語（working capital loan → WorkingCapitalLoan）を識別子として見つけること
- AI判定の予算（`test_llm_budget.py`）: 再調査での再利用、閾値、上限、上限なし、設定ファイル・環境変数・解析ごとの指定の優先順位
- 攻撃が失敗することの確認（`test_security.py`）: ファイル読み取りの迂回、CORS・Host 偽装・クロスサイト WebSocket、認証、パス制限、Webhook 偽装、シンボリックリンク、XXE、zip 爆弾

Oracle 辞書の CSV 取込（`samples/tour-env/db/oracle_catalog`）は、パッケージ本体の CRUD と DB にだけある列 `reserve.cancel_reason` の検出までを確認しました。

## 既知の制約

- **実機未検証のもの**: Neo4j ストアと Docker 構成、Oracle への直接接続、Zoekt・GitLab のコード検索、MR/PR へのコメント投稿、Claude 判定。作成環境から Docker Hub・Maven Central・外部サービスに接続できなかったためです。ローカルストアと同じインターフェースのテストは通っていますが、初回は実機での確認が必要です。
- **評価の独立性**: 周辺資産（ジョブ・設計書・DB にだけある改修）と正解リストは同じ作り手（Claude）が用意したもので、独立した評価ではありません。社内の実案件で、有識者が作った正解と突き合わせる評価が次の段階です。
- **ルールベース判定の絞り込みは弱い**: 候補の網羅は高い一方、「影響あり」の適合率・履歴評価の再現率は低めです。Claude 判定かレビューが前提です。
- **言語の範囲**: Java の型解決は javac 相当の簡易版（ジェネリクスの推論・ラムダの型は追わない）。TypeScript は型推論をしないため、`import` した名前・`this.`・同じファイルの名前を通る呼び出しだけをつなぎます（引数で受け取ったオブジェクトのメソッド呼び出しは追いません）。COBOL・C#・PL/SQL の手続き内部の制御フローは対象外です。PL/SQL はパッケージ内の手続き単位で CRUD・呼び出し・COMMIT/自律型トランザクション/行ロック/ループ内コミットを取り出しますが、分岐ごとの実行経路は追いません。
- **トランザクション解析の範囲**: 静的な呼び出し関係による近似です。実行時の XA 構成（どのデータソースが同じグローバルトランザクションに入るか）、タイムアウト、例外によるロールバック条件（`rollbackFor` など）、EJB の `@Remote` 越しの伝播は判定しません。REST の接続先 URL を設定ファイルから組み立てる場合は、パスのリテラルでつなぎます。
- **設計書**: 表の見出し行を語彙で探す方式です。結合セルが多い帳票型や図だけの設計書からは取り出せません。`.xls` / `.doc` には LibreOffice が必要です。
- **静的解析の限界**: リフレクション・動的SQL・外部システム連携は依存グラフでは追えません。横断検索・独立再探索・実行時SQLとの突き合わせで補う形です。
- **規模の係数は初期値**です。実績の登録による補正が前提です。
- **学習は同じシステムでの解析が重なってから効きます**。経路の傾向は 5 件、要素の履歴は 2 回の解析で反映を始めます。少ない件数で強く効かせない作りのため、使い始めは変化がほとんどありません。工数の補正は全体の倍率のみで、要素の種類ごとの補正はまだありません。
- **予測と実績の照合は要素の単位**です。差分の行を含む最小の要素で数えるため、同じ要素の中の別の箇所を直した場合も「当たり」になります。設計書の更新は差分に出ないため、照合の対象外です。

## ディレクトリ

```
backend/himotoki/extractors/         抽出器（java / ts / prisma / xml / text / sql / props / jobs / docs）
backend/himotoki/rules/              ルールパック（YAML）: フレームワークの知識と伝播の重み
backend/himotoki/pipeline/           抽出の実行・キャッシュ・リンク（名前空間ごとの解決・Java の型解決）
backend/himotoki/sources/            DB辞書・実行時SQL・git（worktree・差分・Webhook）・コード検索
backend/himotoki/store/              ローカルストア / Neo4jストア（グラフ名前空間・一括取得）
backend/himotoki/analysis/           解析エンジン・LLM／ルールベース判定・規模見積もり
backend/himotoki/workspace.py        複数グラフ（ブランチ・タグ）の管理
backend/himotoki/api.py・cli.py・mcp_server.py   REST + WebSocket / コマンドライン / MCP
backend/scripts/eval_history.py    コミット履歴による評価
backend/tests/                     テスト
frontend/src/                      ビュワー
samples/rules/                     追加ルールパックの例（OFBiz）
samples/tour-env/                  検証用の周辺資産（ジョブ・シェル・Office 設計書・DB 改修・実行時SQL）
samples/petclinic-design/          検証用のサンプル設計書（v1）
eval/                              正解リスト・評価スクリプト・結果
docs/screenshots/                  画面の記録
```
