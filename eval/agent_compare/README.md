# Claude Code 単体 と himotoki あり の比較

同じ影響調査の課題を、まっさらな Claude Code に 2 つの条件で解かせ、漏れ・余計な指摘・トークン・時間を比べます。

| 条件 | 中身 |
|---|---|
| A: Claude Code 単体 | MCP サーバなし。grep とファイルの読み取りで調べる |
| B: himotoki あり | A と同じ道具に、himotoki の MCP サーバ（`analyze_requirement`・`search_elements`・`get_element`・`neighbors` など）を加える |

課題は「この要求を実装するために変更が必要な本番コードのファイルを挙げる」です。調査だけにしているのは、正解（そのコミットで実際に変わったファイル）と機械的に突き合わせられ、コードの出来の良し悪しという別の要素が混ざらないためです。

## 公平にするための仕掛け

- **答えが見えない。** 課題ごとに、親コミット時点のソースを **git の履歴なし** で取り出した新しいリポジトリを使います（`git log` で正解のコミットを見られない）。Web 検索・Web 取得は禁止します（GitHub や JIRA で答えを見られない）。
- **条件の差は 2 つだけ。** MCP の設定（なし／himotoki）と、B の指示文の末尾に足す「himotoki の MCP ツールを使えます」の 1 行です。モデル・指示文・許可する道具・予算は同じです。
- **他の設定が混ざらない。** `--strict-mcp-config` で、ふだん登録している MCP サーバを読み込みません。会話は保存しません（`--no-session-persistence`）。課題ごと・条件ごとに別のフォルダを使うので、Claude Code の自動メモリも混ざりません。
- **課題を選り好みしない。** 履歴評価の 30 件を変更ファイル数の順に並べて 6 つに区切り、各区間から固定の乱数で 1 件ずつ選びます（`make_tasks.py`）。要求として成り立たない件名（「address review comments」など）は、選ぶ前に除きます。
- **順番の偏りを消す。** 課題ごとに A と B の実行順を入れ替えます。

## 用意するもの

- Claude Code（`claude -p` で `--json-schema`・`--strict-mcp-config`・`--max-budget-usd` が使える版）
- Python 3.11 以上、Git
- himotoki（このリポジトリで `pip install ./backend`）
- 対象のリポジトリ
  - Fineract（英語・Java・大規模）: `git clone https://github.com/apache/fineract.git ~/work/fineract`
  - marumie（日本語・TypeScript）: `git clone https://github.com/team-mirai/marumie.git ~/work/marumie`

課題の一覧は 2 つ用意してあります。

- `tasks_fineract.json`: Fineract の 6 件（変更ファイル 2〜14）
- `tasks_marumie.json`: marumie の 6 件（変更ファイル 2〜15）

## 手順

以下は Fineract の例です。marumie なら、ファイル名とリポジトリのパスを読み替えてください。

### 1. 課題ごとのフォルダを作る

```bash
cd eval/agent_compare
python prepare.py --tasks tasks_fineract.json --repo ~/work/fineract --out ~/compare-fineract
```

課題ごとに `A/`・`B/`（ソース）、`ws/`（B 用の himotoki のワークスペース）、`mcp-A.json`・`mcp-B.json`、`prompt.txt` ができます。Fineract は 1 課題あたり 1 分ほどかかります（大半は B 用の取り込み）。

### 2. 試しに 1 件だけ回す

```bash
MODEL=opus BUDGET=5 ./run.sh ~/compare-fineract
```

最初の課題の A と B が終わったら `Ctrl+C` で止め、`~/compare-fineract/<課題>/result-A.json` と `result-B.json` を開きます。次の 3 点を確かめてください。

- `"is_error": false` になっている
- `structured_output.files` にファイルの一覧が入っている
- B の `stderr-B.log` に MCP の接続エラーが出ていない

### 3. 全件を回す

```bash
MODEL=opus BUDGET=10 ./run.sh ~/compare-fineract
```

結果のある課題は飛ばすので、途中で止めても続きから再開できます。

- `MODEL` は両条件で同じものを使います（`opus`・`sonnet` など）。
- `BUDGET` は 1 回あたりの費用の上限（ドル）です。これを超えると、その回は打ち切られます。

### 4. 集計する

```bash
python score.py --tasks tasks_fineract.json --out ~/compare-fineract
```

課題・条件ごとの表と、条件ごとのまとめを出し、`score.json` に保存します。

| 項目 | 意味 |
|---|---|
| 再現率 | 実際に変わったファイルのうち、挙げられた割合（漏れの少なさ） |
| 適合率 | 挙げたファイルのうち、実際に変わった割合（余計な指摘の少なさ） |
| 入力／キャッシュ読／出力 | トークン数。入力は新規の入力とキャッシュ書き込みの合計 |
| 費用 | Claude Code が見積もった費用（請求額とは一致しない場合がある） |
| ターン・秒 | エージェントの往復回数と所要時間 |

## 費用と時間の目安

1 回あたり数十万〜数百万トークン、数分〜十数分を見込んでください。6 課題 × 2 条件で 12 回です。サブスクリプションで回す場合は利用上限に当たることがあるので、日をまたいで分けても構いません（結果は課題ごとに保存されます）。

## 注意

- **himotoki の判定はルールベースのまま。** B の himotoki は API キーなしで動きます（AI判定なし）。AI判定を入れると、その費用は Claude Code の費用に含まれないので、別に足す必要があります。まずはルールベースで比べてください。
- **要求はコミットの件名。** 実際の要求文より情報が少ないので、どちらの条件にも厳しめに出ます。差を見る目的には十分です。
- **1 回ずつだとばらつきが大きい。** 余裕があれば、結果のフォルダを別名で残して 2 回目を回し、平均を取ってください。
- **実装まで比べたい場合。** `prompt.txt` を「実装してコミットする」に変え、`run.sh` の `DENY` から `Edit Write` を外し、`ALLOW` に `Edit Write "Bash(git add *)" "Bash(git commit *)"` を足します。採点は、各フォルダで `git diff --name-only HEAD~1` を見て、変わったファイルを正解と突き合わせます。
