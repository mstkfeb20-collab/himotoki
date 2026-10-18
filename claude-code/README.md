# Claude Code で使うスキル

himotoki のビュワーは git に触れません。issue の登録と開発は、開発する PC の Claude Code が行います。そのためのスキルを2つ置いています。

| スキル | 使い方 | すること |
|---|---|---|
| `himotoki-issue` | `/himotoki-issue <run ID>` | 確定した影響範囲から issue の文面を取り（MCP `get_issue_drafts`）、確認を取ってから `gh issue create` で登録する。要求ごとに1件か、サブシステムごとに分割 |
| `himotoki-dev` | `/himotoki-dev <issue 番号 または run ID>` | issue 本文の `himotoki-run` から作業指示を取り（`get_handoff`）、ブランチを切って範囲内で実装・テスト・コミットし、`verify_change` で予測と突き合わせて報告する |

## 入れ方

1. himotoki の MCP サーバを Claude Code に登録する（ビュワーの「開発へ渡す」→「Claude Code で実装」にあるコマンド）。
2. このフォルダの `skills/` の中身を、次のどちらかにコピーする。
   - 自分だけで使う: `~/.claude/skills/`
   - チームで共有する: 開発するリポジトリの `.claude/skills/`（コミットすると全員が使える）
3. `gh auth login` を済ませておく（issue の登録・閲覧に使う）。

## 流れ

```
ビュワー: 候補を確定 → 承認 → 「開発へ渡す」→「issue 案」で文面を確認（手で登録してもよい）
Claude Code: /himotoki-issue <run ID>   → issue を登録
Claude Code: /himotoki-dev <issue 番号>  → 実装・テスト・照合・issue へ報告
ビュワー: 「実績と照合」タブに照合結果（予測漏れは学習に使われる）
```
