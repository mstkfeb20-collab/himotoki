"""Issue drafts from a reviewed analysis.

The viewer does not talk to git: it shows the drafts and copies them. Creating the
issues and starting development happen in the developer's own Claude Code (skills under
`claude-code/skills/`), which reads the same drafts over MCP (`get_issue_drafts`). Every
body carries a `himotoki-run` marker, so an agent that is given only the issue can fetch
the full work order (`get_handoff`) and verify the change against it (`verify_change`).

Split:
* `one`: one issue per requirement (the default; small and medium changes);
* `subsystem`: one parent issue plus one issue per top-level folder (server / subsystem)
  of the files to change, and one for design documents, for changes that several teams
  or partner companies implement separately.
"""
from __future__ import annotations

from collections import OrderedDict

from ..model import Kind
from .context import data_context, flow_context
from .handoff import CHANGE_JA, build_handoff

DOC_PREFIX = "docs/"
JOB_KINDS = {Kind.JOB, Kind.JOBNET, Kind.CRON, Kind.SCRIPT, Kind.BATCH_JOB, Kind.DATA_FILE}


def _loc(file, line=None):
    return f"`{file}" + (f":{line}" if line else "") + "`" if file else ""


def _subsystem(path: str) -> str:
    if path.startswith(DOC_PREFIX) or path.startswith("design:"):
        return "設計書"
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "（ルート）"


def _home(engine, it: dict) -> str:
    """Where an element without a file of its own belongs: a column / table goes with the
    folder of its DDL, anything else with the folder of the file it was found in."""
    if it.get("file"):
        return _subsystem(it["file"])
    d = data_context(engine.store, it["node_id"]) or {}
    defs = (d.get("column") or {}).get("definitions") or (d.get("table") or {}).get("definitions") or []
    return _subsystem(defs[0]["file"]) if defs else "DB"


def _db_lines(engine, items: list[dict]) -> list[str]:
    """Confirmed tables / columns with their table, type and DDL."""
    out = []
    for it in items:
        if it["kind"] not in (Kind.COLUMN, Kind.TABLE):
            continue
        d = data_context(engine.store, it["node_id"]) or {}
        t = d.get("table") or {}
        schema = f"{d['schema']}." if d.get("schema") else ""
        if it["kind"] == Kind.COLUMN:
            col = d.get("column") or {}
            typ = next(iter((col.get("types") or {}).values()), "")
            ddl = ", ".join(_loc(x["file"], x.get("line")) for x in col.get("definitions", []))
            out.append(f"- 列 `{schema}{t.get('name', '?')}.{col.get('name', it['name'])}`"
                       f"{'（' + col['logical'] + '）' if col.get('logical') else ''} {typ} — {it['reason']}"
                       + (f"／定義 {ddl}" if ddl else ""))
        else:
            ddl = ", ".join(_loc(x["file"], x.get("line")) for x in t.get("definitions", []))
            out.append(f"- テーブル `{schema}{t.get('name', it['name'])}`{'（' + t['logical'] + '）' if t.get('logical') else ''}"
                       f" — {it['reason']}" + (f"／定義 {ddl}" if ddl else ""))
    return out


def _job_lines(engine, items: list[dict]) -> list[str]:
    out = []
    for it in items:
        if it["kind"] not in JOB_KINDS:
            continue
        f = flow_context(engine.store, it["node_id"]) or {}
        line = f"- {it['name']}（{it['kind']}）{_loc(it.get('file'), it.get('line'))} — {it['reason']}"
        flows = f.get("flows") or []
        if flows:
            x = flows[0]
            before = "、".join(b["name"].split("/")[-1] for b in x["before"]) or "なし"
            after = "、".join(b["name"].split("/")[-1] for b in x["after"]) or "なし"
            runs = " → ".join(c["name"] for c in x["chain"] if not c.get("common"))
            line += f"\n  - ジョブネット {x['jobnet']['name'] if x.get('jobnet') else '—'}／先行 {before}／後続 {after}"
            if runs:
                line += f"\n  - 実行: {runs}"
        out.append(line)
    return out


def _body(h: dict, engine, files: list[dict], others: list[dict], scope: str, parent_note: str | None) -> str:
    approved = h.get("approved")
    out = [f"<!-- himotoki-run: {h['run_id']} -->", ""]
    if parent_note:
        out += [f"> {parent_note}", ""]
    out += ["## 背景・要求", "", h["requirement"], ""]
    if h["intents"]:
        out += ["## 変更内容", ""] + [f"- {i['summary']}（{CHANGE_JA.get(i['change_type'], i['change_type'])}）" for i in h["intents"]] + [""]
    out += [f"## 変更対象{('（' + scope + '）') if scope else ''}", ""]
    if not files:
        out += ["（この範囲で確定したファイルはありません）", ""]
    for f in files:
        out.append(f"- [ ] {_loc(f['file'])}")
        for it in f["items"]:
            ln = f"{it['line']}–{it['end_line']}行" if it.get("line") and it.get("end_line") and it["end_line"] != it["line"] \
                else (f"{it['line']}行" if it.get("line") else "")
            out.append(f"  - {it['name']}（{CHANGE_JA.get(it['change_type'], it['change_type'])}{'・' + ln if ln else ''}）: {it['reason']}")
            for cm in it.get("comments", []):
                out.append(f"    - コメント（{cm['author']}）: {cm['text']}")
            for r in it.get("tx_risks", []):
                out.append(f"    - ⚠ 連携・トランザクション: {r['message']}")
    out.append("")
    db = _db_lines(engine, others + [i for f in files for i in f["items"]])
    if db:
        out += ["## DB（テーブル・列）", ""] + db + [""]
    jobs = _job_lines(engine, others + [i for f in files for i in f["items"]])
    if jobs:
        out += ["## ジョブ・連携", ""] + jobs + [""]
    docs = sorted({d["file"] for f in files for it in f["items"] for d in it.get("docs", []) if d.get("file")})
    if docs:
        out += ["## 合わせて確認する設計書", ""] + [f"- {_loc(d)}" for d in docs] + [""]
    tests = sorted({(t["name"], t.get("file")) for f in files for it in f["items"] for t in it.get("tests", [])})
    callers = OrderedDict()
    for f in files:
        for it in f["items"]:
            for c in it.get("callers_to_check", []):
                callers[c["node_id"]] = c
    if files:
        out += ["## 確認・テスト", ""]
        out += [f"- [ ] テスト {n} {_loc(fl)}" for n, fl in tests] or ["- [ ] 変更箇所を検証するテストを追加する（既存のテストが見つかっていません）"]
    if callers:
        out += [f"- [ ] 呼び出し元の動作確認: {c['name']} {_loc(c.get('file'), c.get('line'))}" for c in list(callers.values())[:10]]
    if h.get("excluded") and scope in ("", None):
        out += ["", "### 変更しないもの（レビューで除外）", ""] + \
               [f"- {x['name']} {_loc(x.get('file'))}: {x['reason']}" for x in h["excluded"][:20]]
    if h.get("pending") and scope in ("", None):
        out += ["", f"### 未判定（実装中に影響の有無を確認） {len(h['pending'])} 件", ""] + \
               [f"- {p['name']} {_loc(p.get('file'), p.get('line'))}" for p in h["pending"][:15]]
        if len(h["pending"]) > 15:
            out.append(f"- ほか {len(h['pending']) - 15} 件（作業指示を参照）")
    out += ["", "## 受け入れ条件", "",
            "- [ ] 変更対象のファイルがすべて修正済み、または修正不要の理由が記録されている",
            "- [ ] 範囲外のファイルを変更した場合は、その理由がこの issue に書かれている",
            "- [ ] 確認・テストの項目がすべて済んでいる",
            "- [ ] 設計書の該当箇所が更新されている",
            f"- [ ] himotoki の `verify_change`（run `{h['run_id']}`）で予測と突き合わせ、漏れと余分を確認した", "",
            "---",
            f"himotoki run `{h['run_id']}`・{'承認済み（' + approved['actor'] + '）' if approved else '未承認'}"
            f"・規模の見込み {h.get('estimate_days')} 人日（{h['estimate_range'][0]}〜{h['estimate_range'][1]}）"]
    if h.get("warnings"):
        out += [""] + [f"> {w}" for w in h["warnings"]]
    return "\n".join(out)


def issue_drafts(engine, run_id: str, split: str = "one") -> dict:
    h = build_handoff(engine, run_id, include_pending=True)
    title = (h.get("title") or h["requirement"]).strip().replace("\n", " ")
    title = title if len(title) <= 60 else title[:59] + "…"
    labels = ["himotoki", "影響調査済み" if h.get("approved") else "影響調査中"]
    if split != "subsystem":
        return {"run_id": run_id, "split": "one", "warnings": h["warnings"],
                "issues": [{"key": "main", "title": title, "labels": labels,
                            "body": _body(h, engine, h["files"], h["other_targets"], "", None)}]}
    groups: dict[str, list] = OrderedDict()
    for f in h["files"]:
        groups.setdefault(_subsystem(f["file"]), []).append(f)
    others_by: dict[str, list] = OrderedDict()
    for it in h["other_targets"]:
        others_by.setdefault(_home(engine, it), []).append(it)
    names = list(dict.fromkeys(list(groups) + list(others_by)))
    names.sort(key=lambda n: (n == "設計書", n))
    parent_lines = [f"- [ ] {n}（{len(groups.get(n, [])) + len(others_by.get(n, []))} 件）" for n in names]
    parent_body = _body(h, engine, [], [], "", None).replace(
        "## 変更対象\n\n（この範囲で確定したファイルはありません）",
        "## 変更対象（サブシステムごとの issue に分割）\n\n" + "\n".join(parent_lines), 1)
    issues = [{"key": "parent", "title": title, "labels": labels + ["親issue"], "body": parent_body}]
    for n in names:
        issues.append({"key": n, "title": f"{title} — {n}", "labels": labels + [n],
                       "body": _body(h, engine, groups.get(n, []), others_by.get(n, []), n,
                                     f"親 issue「{title}」の一部（{n}）です。全体の要求・除外・未判定は親 issue を参照してください。")})
    return {"run_id": run_id, "split": "subsystem", "warnings": h["warnings"], "issues": issues}
