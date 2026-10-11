"""Getting started: a workspace folder with a fixed layout, an environment check, and
a demo that runs end to end on the bundled sample system.

    <workspace>/
      repos/      git clone each repository (one folder per repository / server)
      docs/       design documents, JP1 definitions (ajsprint output), operation scripts
      db/         database dictionary exports (optional)
      rules/      rule packs for in-house frameworks (optional, *.yaml)
      settings.yaml
      (graphs/, facts.db, workspace.json ... are created by himotoki)

Commands run inside the workspace (or any folder below it) find it on their own.
"""
from __future__ import annotations

import importlib
import os
import re
import shutil
import socket
import subprocess
import sys

MARKER = ".himotoki-workspace"
USER_DIRS = ("repos", "docs", "db", "rules")
DEMO = os.path.join(os.path.dirname(__file__), "demo", "sor-env")
DEMO_REQUIREMENT = "受注の備考（NOTE）の上限を200文字から500文字に拡張する"

SETTINGS = """\
# himotoki の設定（必要なところだけ書き換えてください。消しても既定値で動きます）
# AI判定は環境変数 ANTHROPIC_API_KEY を設定したときだけ使われます。

llm:
  min_score: 0.3        # これより低い候補はAIに送らずルールで判定する
  max_items: 300        # 1回の解析でAIに送る件数の上限（unlimited で上限なし、0 でAIを使わない）

pricing:
  jpy_per_usd: 158      # ビュワーの費用表示（円）に使う為替レート
"""

README = """\
himotoki ワークスペース
========================

このフォルダに解析したいものを置き、このフォルダ（または下のフォルダ）で himotoki コマンドを実行します。

  repos/   ソースコード。リポジトリごとに git clone してください（例: repos/order-web）
           複数のサーバで1つの業務を構成している場合は、全部ここに並べます（1つのシステムとして解析します）
  docs/    設計書（Excel・Word・Markdown）、JP1のジョブ定義（ajsprint の出力）、crontab、運用シェル
  db/      本番DBの辞書のエクスポート（任意）。db/export_oracle.sql で書き出した CSV を
           db/<DB名>/ に置くと、himotoki index のときに取り込みます
  rules/   社内フレームワーク用のルール（任意。*.yaml を置くと自動で読み込みます）
  settings.yaml   AI判定の上限や費用表示の設定

使い方:
  himotoki doctor     準備ができているかを確認
  himotoki index      repos/ と docs/ を取り込む（中身を更新したらもう一度実行）
  himotoki serve --open   ビュワーを開く（止めるときは Ctrl+C）

graphs/ facts.db workspace.json などは himotoki が作るファイルです。消すと取り込み直しになります。
"""


# ------------------------------------------------------------------ workspace
def find_workspace(start: str | None = None) -> str | None:
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isfile(os.path.join(d, MARKER)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def init_workspace(path: str, quiet: bool = False) -> str:
    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    for sub in USER_DIRS:
        os.makedirs(os.path.join(path, sub), exist_ok=True)
    _write_once(os.path.join(path, MARKER), "himotoki workspace\n")
    _write_once(os.path.join(path, "settings.yaml"), SETTINGS)
    _write_once(os.path.join(path, "README.txt"), README)
    from .sources.catalog import oracle_export_script
    _write_once(os.path.join(path, "db", "export_oracle.sql"), oracle_export_script())
    if not quiet:
        print(f"ワークスペースを作りました: {path}")
        print("  1. repos/ にソースを git clone する（複数サーバならリポジトリごとに）")
        print("  2. docs/ に設計書・ジョブ定義・運用シェルを置く（任意）")
        print(f"  3. cd {path}")
        print("     himotoki doctor  →  himotoki index  →  himotoki serve --open")
    return path


def _write_once(p: str, text: str):
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def workspace_repos(ws: str) -> dict[str, str]:
    base = os.path.join(ws, "repos")
    if not os.path.isdir(base):
        return {}
    return {n: os.path.join(base, n) for n in sorted(os.listdir(base))
            if not n.startswith(".") and os.path.isdir(os.path.join(base, n))}


def _has_files(d: str) -> bool:
    for _, _, fs in os.walk(d):
        if any(not f.startswith(".") for f in fs):
            return True
    return False


def graph_name_for(ws: str) -> str:
    name = re.sub(r"[^\w.@\-]+", "_", os.path.basename(ws.rstrip("/\\"))).strip("._") or "system"
    return name[:100]


def index_workspace(ws_obj, ws_dir: str, progress=None) -> dict:
    """`himotoki index` with no arguments: repos/ (one repository, or several as one
    system) together with docs/."""
    repos = workspace_repos(ws_dir)
    if not repos:
        raise SystemExit(f"{os.path.join(ws_dir, 'repos')} にリポジトリがありません。"
                         "git clone で置いてから、もう一度実行してください。")
    docs = os.path.join(ws_dir, "docs")
    docs = docs if os.path.isdir(docs) and _has_files(docs) else None
    import_db_exports(ws_dir, progress)
    if len(repos) == 1:
        name, path = next(iter(repos.items()))
        return ws_obj.index(path, docs, None, re.sub(r"[^\w.@\-]+", "_", name), progress=progress)
    return ws_obj.index_system(graph_name_for(ws_dir), repos, docs, progress=progress)


def import_db_exports(ws_dir: str, progress=None) -> list[str]:
    """Oracle dictionary exports in db/ (ALL_TAB_COLUMNS.csv ..., directly or one folder per
    database) are imported before indexing, so DB-only columns and stored code are linked."""
    from .sources.catalog import import_oracle, save_source
    base = os.path.join(ws_dir, "db")
    found = []
    if os.path.isfile(os.path.join(base, "ALL_TAB_COLUMNS.csv")):
        found.append(("db", base))
    if os.path.isdir(base):
        for n in sorted(os.listdir(base)):
            d = os.path.join(base, n)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "ALL_TAB_COLUMNS.csv")):
                found.append((re.sub(r"[^\w.@\-]+", "_", n), d))
    for name, d in found:
        save_source(ws_dir, import_oracle(name, d))
        if progress:
            progress("catalog.imported", {"name": name})
    return [n for n, _ in found]


def workspace_rules(ws_dir: str | None) -> str | None:
    d = os.path.join(ws_dir, "rules") if ws_dir else None
    if d and os.path.isdir(d) and any(f.endswith(".yaml") for f in os.listdir(d)):
        return d
    return None


# ------------------------------------------------------------------ doctor
def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def doctor(ws_dir: str | None, data_dir: str, port: int = 8765) -> int:
    """Prints what is ready and what to do next. Returns the number of blocking problems."""
    rows: list[tuple[str, str, str]] = []      # (mark, item, detail)
    ok, warn, bad = "OK  ", "注意", "NG  "

    v = sys.version_info
    rows.append((ok if v >= (3, 11) else bad, "Python", f"{v.major}.{v.minor}.{v.micro}"
                 + ("" if v >= (3, 11) else "（3.11 以上が必要です）")))
    missing = []
    for mod in ("fastapi", "uvicorn", "tree_sitter", "tree_sitter_java", "sqlglot", "yaml", "openpyxl", "docx",
                "sklearn"):
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    rows.append((bad if missing else ok, "必要なライブラリ",
                 "そろっています" if not missing else f"不足: {', '.join(missing)}（pip install -e backend をやり直してください）"))
    git = shutil.which("git")
    gv = ""
    if git:
        try:
            gv = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            gv = git
    rows.append((ok if git else warn, "git", gv or "見つかりません（無くても取り込みはできますが、差分解析・履歴・ブランチ指定が使えません）"))

    from .api import viewer_dir
    vd = viewer_dir()
    rows.append((ok if vd else bad, "ビュワー", "同梱のものを使います" if vd and vd.endswith("web") else
                 (vd or "見つかりません（パッケージを入れ直すか、frontend で npm run build）")))
    office = shutil.which("soffice") or shutil.which("libreoffice")
    rows.append((ok if office else warn, "LibreOffice（任意）",
                 office or "無し。.xlsx / .docx は読めます。古い .xls / .doc を読むときだけ必要です"))
    rows.append((ok if os.environ.get("ANTHROPIC_API_KEY") else warn, "AI判定（任意）",
                 "ANTHROPIC_API_KEY が設定されています" if os.environ.get("ANTHROPIC_API_KEY") else
                 "未設定。ルールで判定します（候補は出ます。絞り込みはレビューで）"))

    if ws_dir:
        repos = workspace_repos(ws_dir)
        rows.append((ok, "ワークスペース", ws_dir))
        rows.append((ok if repos else bad, "repos/", f"{len(repos)} 件: {', '.join(repos)}" if repos else
                     "空です。解析したいリポジトリを git clone してください"))
        docs = os.path.join(ws_dir, "docs")
        rows.append((ok if _has_files(docs) else warn, "docs/", "あり" if _has_files(docs) else
                     "空です（設計書・ジョブ定義が無くても動きますが、その分は影響候補に出ません）"))
        if workspace_rules(ws_dir):
            rows.append((ok, "rules/", "社内ルールを読み込みます"))
    else:
        rows.append((warn, "ワークスペース", f"見つかりません（作業領域: {data_dir}）。"
                     "himotoki init <フォルダ> で作ると、置き場所が決まって迷いません"))
    target = data_dir
    while not os.path.exists(target) and os.path.dirname(target) != target:
        target = os.path.dirname(target)          # checked without creating anything
    if os.access(target, os.W_OK):
        rows.append((ok, "書き込み", data_dir))
    else:
        rows.append((bad, "書き込み", f"{data_dir} に書き込めません（権限のあるフォルダで実行してください）"))
    import json
    meta = os.path.join(data_dir, "workspace.json")
    graphs = {}
    if os.path.isfile(meta):
        try:
            graphs = json.load(open(meta, encoding="utf-8")).get("graphs", {})
        except ValueError:
            pass
    rows.append((ok if graphs else warn, "取り込み済み", ", ".join(f"{n}（{g.get('nodes')} 要素）" for n, g in graphs.items())
                 if graphs else "まだありません → himotoki index"))
    rows.append((ok if port_free(port) else warn, f"ポート {port}", "空いています" if port_free(port) else
                 f"使用中です（起動済みならそのまま http://127.0.0.1:{port} を開く。別のポートは --port 8766）"))

    import unicodedata
    width = lambda t: sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in t)
    w = max(width(r[1]) for r in rows)
    for mark, item, detail in rows:
        print(f"[{mark}] {item}{' ' * (w - width(item))}  {detail}")
    problems = sum(1 for r in rows if r[0] == bad)
    print()
    if problems:
        print(f"NG が {problems} 件あります。上の説明に沿って直してから、もう一度 himotoki doctor を実行してください。")
    elif not graphs:
        print("準備はできています。次は himotoki index（お試しなら himotoki demo）。")
    else:
        print("準備はできています。himotoki serve --open でビュワーを開けます。")
    return problems


# ------------------------------------------------------------------ demo
def prepare_demo(path: str) -> str:
    ws = init_workspace(path, quiet=True)
    for name in ("order-web", "inventory-svc", "shipping-svc"):
        dst = os.path.join(ws, "repos", name)
        if not os.path.exists(dst):
            shutil.copytree(os.path.join(DEMO, name), dst)
    readme = os.path.join(ws, "サンプルの説明.md")          # beside the folders, so it is not analysed
    if not os.path.exists(readme):
        shutil.copyfile(os.path.join(DEMO, "README.md"), readme)
    return ws
