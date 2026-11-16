# つながりの正誤判定

各行は「from の要素」と「to の要素」の間に、rel で示す関係がある、という主張です。
root のリポジトリの実際のファイルを開いて、その主張が正しいかを判定してください。

## 関係（rel）の意味
- CALLS: from（メソッド）が to（メソッド・コンストラクタ）を呼び出している
- READS / WRITES: from（SQL文・メソッド・スクリプト・ジョブ・設定など）が to（テーブル・列・連携ファイル）を読む／書く（INSERT・UPDATE・DELETE・MERGE・ファイル出力は書く、SELECT・WHERE・ファイル入力は読む）
- EXECUTES: from（メソッド・ジョブ・スクリプト）が to（SQL文・SQLファイル・プロシージャ）を実行する
- REFERENCES: from が to を名前で参照している（クラス・定数・URL・設定キー・連携ファイル・列名・ID など。どこかで to を指していれば正）
- DESCRIBES: from（設計書の節・行）が to（テーブル・列・画面・プログラム・ジョブなど）について書いている
- DOC_CRUD: from（CRUD表などの設計書の行）が to の読み書きを記述している
- DEFINES: from（DDL・定義ファイル・レイアウト）が to（テーブル・列・連携ファイルなど）を定義している
- BINDS: from（画面・入力チェック・設定）の項目が to（フォームの項目・Bean・フィールド）に結びついている
- MAPS_TO: from（フィールド・レイアウト項目・resultMap など）が to（列）に対応している
- FLOWS_TO: from（画面の入力項目・フォームのフィールド・リクエストパラメータ・DTO のフィールド）の値が、コードを通って to（列）に登録・更新されるか、検索条件として使われる（props.usage: write=登録・更新, cond=検索条件。props.path は経路の手がかり）
- HANDLES: from（URL・アクション定義）が to（クラス・メソッド）で処理される
- RENDERS: from（アクション・処理）が to（画面テンプレート）を表示する／遷移する
- INCLUDES: from（テンプレート・SQL）が to を取り込む（include・import・sql 参照）
- RUNS: from（ジョブ・cron・スクリプト）が to（スクリプト・プログラム・ジョブ）を起動する
- PRECEDES: from（ジョブ）の後に to（ジョブ）が実行される
- EXTENDS: from（クラス）が to を継承・実装する
- IMPLEMENTED_BY: from（インタフェース・抽象メソッド）が to で実装される
- INJECTS: from（クラス・フィールド）に to（クラス・Bean）が注入される
- CONFIGURES: from（設定・XML の要素）が to（クラス・要素）を設定する
- USES_MESSAGE: from（画面・プログラム・入力チェック）が to（メッセージキー）を使う
- POPULATES: from（初期データ・投入SQL）が to（テーブル・列）にデータを入れる
- TESTS: from（テスト）が to をテストしている

## 判定
- correct: 主張どおりの関係がある
- partial: 関係はあるが、種類や向きが違う（例: 読むのに WRITES、呼ぶのではなく参照している）、または対象が近いが厳密には違う（同名の別の列など）
- wrong: そのような関係は無い
- unclear: ファイルを見ても判断できない（ファイルが無い、動的に決まる等）

要素名の中の `#` の後ろはメソッド、`:` で始まるものは種類の接頭辞です。file と line は目安です。
