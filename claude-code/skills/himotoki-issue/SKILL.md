---
name: himotoki-issue
description: himotoki で影響調査した結果（run）から GitHub の issue を作る。「/himotoki-issue <run ID>」や「himotoki の run から issue を切って」と言われたときに使う。
---

# himotoki の調査結果から issue を作る

himotoki のビュワーで確定した影響範囲（run）を、GitHub の issue にします。issue の文面は himotoki が作り（MCP の `get_issue_drafts`）、登録はこの端末の `gh` で行います。himotoki 自身は git に触れません。

## 前提

- Claude Code に himotoki の MCP サーバが登録されていること（ツール `get_issue_drafts`・`list_runs` が見える）。無ければ、ビュワーの「開発へ渡す」→「Claude Code で実装」にある `claude mcp add …` を案内して止まる。
- `gh` がログイン済みで、issue を作るリポジトリの中にいること（`gh auth status`、`gh repo view --json nameWithOwner`）。どちらかが満たせなければ、何が足りないかを伝えて止まる。

## 手順

1. **run を決める。** 引数に run ID があればそれを使う。無ければ `list_runs` で一覧を出し、どれかを聞く。
2. **分け方を決める。** 既定は「要求ごとに1件」（`split="one"`）。ユーザーが分割を望んだとき、または変更対象が複数のサブシステム（最上位フォルダ）にまたがり、担当が分かれそうなときは「サブシステムごと」（`split="subsystem"`）を提案して選んでもらう。
3. **文面を取得する。** `get_issue_drafts(run_id, split)` を呼ぶ。
4. **確認を取る。** 作る issue のタイトルと件数、`warnings`（未承認・未判定が残っている など）をそのまま見せ、「この内容で登録してよいか」を必ず聞く。未承認の run は、承認前であることを強調する。ユーザーが文面の修正を求めたら、その部分だけ直す（変更対象・除外・受け入れ条件を勝手に増減しない）。
5. **ラベル。** `gh label list --json name` で既存のラベルを確かめる。無いラベル（`himotoki`・`影響調査済み` など）は、作ってよいか聞き、了承があれば `gh label create` で作る。断られたら、そのラベルは付けずに登録する。
6. **登録する。** 本文は一時ファイルに書き、`gh issue create --title … --body-file <file> [--label …]` で作る（本文をコマンドラインに直接書かない）。
   - `split="subsystem"` のときは、親 issue（`key="parent"`）を先に作り、番号を控える。子 issue の本文の先頭の引用行（「親 issue「…」の一部」）を `親 issue: #<番号>` に置き換えてから作る。最後に親 issue の本文のチェックリストを子 issue の番号つきに書き換え、`gh issue edit <親> --body-file` で更新する。
7. **報告する。** 作った issue の番号と URL を一覧で返す。次の一手として「`/himotoki-dev <issue 番号>` で開発に着手できる」と一言添える。

## 守ること

- 本文の先頭にある `<!-- himotoki-run: … -->` は消さない（開発に着手するときに作業指示を引くための印）。
- 登録の前の確認（手順 4）は省略しない。issue はリポジトリの全員に見える。
- 失敗したとき（権限・ラベル・ネットワーク）は、作れた issue と作れなかった issue を分けて報告し、作れたものを重ねて作り直さない。
