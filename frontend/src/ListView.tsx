import { KindChip, subsystem } from "./kinds";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import type { Filters } from "./Graph";
import { elementSize, isFiltered, sizeOf, visibleCandidates } from "./sizing";
import type { Candidate, RunState } from "./types";
import { Hint } from "./ui";

/** What kind of thing a candidate is, in words a reviewer uses. */
export const KIND_JA: Record<string, string> = {
  Endpoint: "画面・API", Feature: "機能", JobNet: "ジョブネット", Job: "ジョブ", CronEntry: "cron", BatchJob: "バッチ",
  Class: "クラス", Method: "処理（メソッド）", Field: "項目", Template: "画面テンプレート", Script: "シェル",
  Bean: "Bean定義", SqlMapper: "SQLマッパー", SqlStatement: "SQL", MessageKey: "メッセージ", ConfigFile: "設定・ファイル",
  XmlElement: "XML設定", Table: "テーブル", Column: "列", SqlFile: "SQLファイル", DbObject: "DBオブジェクト",
  DataFile: "連携ファイル", RuntimeSql: "実行時SQL", TestClass: "テスト", TestMethod: "テスト", DocSection: "設計書",
  DocEntity: "設計書の項目", Service: "サービス",
};
const LAYER_JA: Record<string, string> = { feature: "機能", code: "コード", data: "データ", test: "テスト", doc: "設計書" };

type Group = "todo-impacted" | "todo-uncertain" | "todo-low" | "done";
const GROUPS: { key: Group; title: string; hint: string }[] = [
  { key: "todo-impacted", title: "影響ありの可能性が高い", hint: "判定が「影響あり」。中身を見て確定してください" },
  { key: "todo-uncertain", title: "要確認", hint: "根拠が弱い、または判定が割れたもの。開いて判断してください" },
  { key: "todo-low", title: "影響は低そう", hint: "つながりが弱いもの。問題なければまとめて除外できます" },
  { key: "done", title: "判断済み", hint: "" },
];

function groupOf(c: Candidate): Group {
  if (c.decision) return "done";
  if (c.verdict === "impacted") return "todo-impacted";
  if (c.verdict === "not_impacted") return "todo-low";
  return "todo-uncertain";
}

function byLabel(c: Candidate): string {
  if (c.origin === "human") return "人が追加";
  switch (c.judged_by) {
    case "ai": return "AI";
    case "reused": return "AI（前回）";
    case "rule:fallback": return "ルール（AI失敗）";
    case null: case undefined: return "";
    default: return "ルール";
  }
}

/** At most two badges, most important first (transaction risk > learned > how it was judged); the rest is in the inspector. */
function rowBadges(c: Candidate) {
  const out: JSX.Element[] = [];
  if ((c.tx_risks?.length || 0) > 0 || c.tx) out.push(<TxBadge key="tx" c={c} />);
  if ((c.learned?.length || 0) > 0) out.push(
    <span key="ln" className="lnb" title={c.learned!.map((n) => n.message).join("\n")}>
      学習{c.learned!.some((n) => n.type === "missed_before" || n.type === "learned_link") ? "・実績" : ""}</span>);
  // plain rule-based judgement is the default and is said once in the inspector; only exceptions get a badge
  const by = byLabel(c);
  if (by && by !== "ルール") out.push(<span key="by" className="by">{by}</span>);
  return out.slice(0, 2);
}

function where(c: Candidate): string {
  if (!c.file) return "";
  const f = c.file.replace(/^design:/, "設計書/");
  const parts = f.split("/");
  const short = parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : f;
  return c.line ? `${short}:${c.line}` : short;
}

type Sort = "priority" | "score" | "kind" | "file";

export default function ListView({ run, filters, selected, onSelect, onChanged, onShowInGraph }: {
  run: RunState;
  filters: Filters;
  selected: string[];
  onSelect: (ids: string[], focus?: boolean) => void;
  onChanged: () => void;
  onShowInGraph: (id: string) => void;
}) {
  const [sort, setSort] = useState<Sort>("priority");
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [excluding, setExcluding] = useState<string | null>(null);   // node id, or "bulk"
  const [reason, setReason] = useState("");
  const [showDone, setShowDone] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const all = Object.values(run.candidates);
  const rows = useMemo(() => {
    let cs = visibleCandidates(run, filters);
    const conf = (c: Candidate) => c.confidence ?? c.score;
    if (sort === "score") cs = cs.sort((a, b) => conf(b) - conf(a));
    else if (sort === "kind") cs = cs.sort((a, b) => (KIND_JA[a.kind] || a.kind).localeCompare(KIND_JA[b.kind] || b.kind, "ja") || conf(b) - conf(a));
    else if (sort === "file") cs = cs.sort((a, b) => (a.file || "~").localeCompare(b.file || "~") || (a.line ?? 0) - (b.line ?? 0));
    else cs = cs.sort((a, b) => conf(b) - conf(a));
    return cs;
  }, [run, filters, sort]);
  const size = useMemo(() => sizeOf(run, rows), [run, rows]);
  const whole = run.estimate?.days;

  const grouped = useMemo(() => {
    const g: Record<Group, Candidate[]> = { "todo-impacted": [], "todo-uncertain": [], "todo-low": [], done: [] };
    for (const c of rows) g[groupOf(c)].push(c);
    return g;
  }, [rows]);

  const total = all.length;
  const decided = all.filter((c) => c.decision).length;
  const visibleIds = useMemo(() => (sort === "priority"
    ? [...grouped["todo-impacted"], ...grouped["todo-uncertain"], ...grouped["todo-low"], ...(showDone ? grouped.done : [])]
    : rows.filter((c) => showDone || !c.decision)).map((c) => c.node_id), [grouped, rows, sort, showDone]);

  const decide = async (ids: string[], status: "confirmed" | "excluded" | "open", why = "") => {
    setErr(null);
    try {
      if (ids.length === 1) await api.decide(run.id, ids[0], status, why);
      else await api.bulk(run.id, ids.map((node_id) => ({ node_id, status, reason: why })));
      setChecked(new Set()); setExcluding(null); setReason("");
      onChanged();
      // move on to the next open item, the way a reviewer works through a list
      if (ids.length === 1 && ids[0] === selected[0] && status !== "open") {
        const i = visibleIds.indexOf(ids[0]);
        const next = visibleIds.slice(i + 1).find((id) => !run.candidates[id]?.decision);
        if (next) onSelect([next]);
      }
    } catch (e: any) { setErr(e.message); }
  };

  // keyboard: ↑↓ / j k to move, c to confirm, x to exclude, u to undo
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
      const cur = selected[0];
      const i = cur ? visibleIds.indexOf(cur) : -1;
      if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); const n = visibleIds[Math.min(visibleIds.length - 1, i + 1)]; if (n) onSelect([n]); }
      else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); const n = visibleIds[Math.max(0, i - 1)]; if (n) onSelect([n]); }
      else if (e.key === "c" && cur) decide([cur], "confirmed");
      else if (e.key === "x" && cur) { setExcluding(cur); setReason(""); }
      else if (e.key === "u" && cur && run.candidates[cur]?.decision) decide([cur], "open");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // keep the selected row in view
  useEffect(() => {
    const id = selected[0];
    if (!id || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-id="${CSS.escape(id)}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  const toggle = (id: string) => setChecked((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const checkGroup = (cs: Candidate[], on: boolean) => setChecked((s) => {
    const n = new Set(s); cs.forEach((c) => (on ? n.add(c.node_id) : n.delete(c.node_id))); return n;
  });

  const renderRow = (c: Candidate) => {
    const sel = selected.includes(c.node_id);
    const conf = c.confidence ?? c.score;
    const st = c.decision ? c.decision.status : groupOf(c);
    const chain = c.path.slice(0, -1).map((id) => run.candidates[id]).filter(Boolean) as Candidate[];
    const intent = run.intents.find((i) => i.id === c.intent_ids[0]);
    return (
      <div key={c.node_id} data-id={c.node_id} role="row" aria-selected={sel}
        className={`lrow ${sel ? "sel" : ""} st-${st}`} onClick={() => onSelect([c.node_id])}>
        <div className="lrow-main">
          <input type="checkbox" aria-label={`${c.name} を選択`} checked={checked.has(c.node_id)}
            onClick={(e) => e.stopPropagation()} onChange={() => toggle(c.node_id)} />
          <span className={`pill p-${st}`}>{c.decision ? (c.decision.status === "confirmed" ? "確定" : "除外")
            : c.verdict === "impacted" ? "影響あり" : c.verdict === "not_impacted" ? "影響低" : "要確認"}</span>
          <div className="lrow-name">
            <div className="nm"><KindChip kind={c.kind} file={c.file} /> <span className="nmt">{c.name}</span></div>
            <div className="meta">
              {subsystem(c.file) && <span className="sub">{subsystem(c.file)}</span>}
              <span>{KIND_JA[c.kind] || c.kind}</span>
              {where(c) && <span className="mono loc">{where(c)}</span>}
              {rowBadges(c)}
            </div>
          </div>
          <div className="lrow-reason">{c.decision?.reason ? `「${c.decision.reason}」` : (c.reason || c.seed_reason || "")}</div>
          <Effort run={run} c={c} />
          <div className="lrow-conf" title={`確からしさ ${conf.toFixed(2)}`}>
            <div className="cbar"><div style={{ width: `${Math.round(conf * 100)}%` }} /></div>
            <span className="mono">{conf.toFixed(2)}</span>
          </div>
          <div className="lrow-act" onClick={(e) => e.stopPropagation()}>
            {c.decision ? (
              <button className="btn small ghost" onClick={() => decide([c.node_id], "open")} title="未判定に戻す (u)">戻す</button>
            ) : (
              <>
                <button className="btn small ok" onClick={() => decide([c.node_id], "confirmed")} title="影響ありで確定 (c)">確定</button>
                <button className="btn small" onClick={() => { setExcluding(c.node_id); setReason(""); }} title="除外 (x)">除外…</button>
              </>
            )}
          </div>
        </div>
        {excluding === c.node_id && (
          <form className="lrow-ex" onClick={(e) => e.stopPropagation()}
            onSubmit={(e) => { e.preventDefault(); if (reason.trim()) decide([c.node_id], "excluded", reason.trim()); }}>
            <input autoFocus className="input" value={reason} onChange={(e) => setReason(e.target.value)}
              placeholder="除外の理由（記録されます）例: 表示のみで今回の項目は扱わない" aria-label="除外の理由" />
            <button className="btn small" disabled={!reason.trim()}>除外する</button>
            <button type="button" className="btn small ghost" onClick={() => setExcluding(null)}>やめる</button>
          </form>
        )}
        {sel && (
          <div className="lrow-path" onClick={(e) => e.stopPropagation()}>
            <span className="k">見つかった経路</span>
            <ol>
              <li className="req">{intent ? intent.summary : run.requirement.slice(0, 60)}</li>
              {chain.map((p) => (
                <li key={p.node_id}><button className="link" onClick={() => onSelect([p.node_id])}>{p.name}</button>
                  <span className="dim small"> {KIND_JA[p.kind] || p.kind}</span></li>
              ))}
              <li className="me">{c.name}</li>
            </ol>
            <button className="btn small ghost" onClick={() => onShowInGraph(c.node_id)}>グラフで見る</button>
          </div>
        )}
      </div>
    );
  };

  const nChecked = checked.size;
  return (
    <div className="listview">
      <div className="lv-head">
        <div className="lv-progress">
          <span className="mono">{isFiltered(filters) ? size.decided : decided}</span>
          <span className="muted"> / {isFiltered(filters) ? `${size.count} 件（全体 ${total} 件）` : `${total} 件`} 判断済み</span>
          <div className="bar"><div className="fill" style={{ width: `${isFiltered(filters)
            ? (size.count ? (size.decided / size.count) * 100 : 0) : (total ? (decided / total) * 100 : 0)}%` }} /></div>
        </div>
        <div className="lv-size" title={`確定分 ${size.confirmedH}h ＋ 未判定の見込み ${size.expectedH}h（最大 ${size.maxH}h）に、全体と同じ係数（レビュー等・実績補正）を掛けたもの`}>
          <span className="k">{isFiltered(filters) ? "この一覧の規模" : "規模"}</span>
          <span className="mono">{size.days}</span><span className="muted small"> 人日</span>
          <span className="muted small">（{size.low}〜{size.high}）</span>
          {isFiltered(filters) && whole !== undefined && <span className="muted small"> ／ 全体 {whole}人日</span>}
        </div>
        <label className="lv-sort"><span className="k">並び順</span>
          <select className="input" value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="priority">確認する順（おすすめ）</option>
            <option value="score">確からしさの高い順</option>
            <option value="kind">種類ごと</option>
            <option value="file">ファイルの場所順</option>
          </select></label>
        <label className="lv-done"><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> 判断済みも表示</label>
      </div>

      {nChecked > 0 && (
        <div className="lv-bulk">
          <span>{nChecked} 件を選択中</span>
          {excluding === "bulk" ? (
            <form onSubmit={(e) => { e.preventDefault(); if (reason.trim()) decide([...checked], "excluded", reason.trim()); }}>
              <input autoFocus className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="除外の理由（全件に記録）" aria-label="まとめて除外する理由" />
              <button className="btn small" disabled={!reason.trim()}>まとめて除外</button>
              <button type="button" className="btn small ghost" onClick={() => setExcluding(null)}>やめる</button>
            </form>
          ) : (
            <>
              <button className="btn small ok" onClick={() => decide([...checked], "confirmed", "一覧でまとめて確定")}>まとめて確定</button>
              <button className="btn small" onClick={() => { setExcluding("bulk"); setReason(""); }}>まとめて除外…</button>
              <button className="btn small ghost" onClick={() => setChecked(new Set())}>選択を解除</button>
            </>
          )}
        </div>
      )}
      {err && <p className="warn small">{err}</p>}

      <Hint id="list">上から順に行を選び、右の根拠とコードを見て「確定」か「除外…」を押します（キーボードの <b>c</b>・<b>x</b>、移動は <b>↑↓</b>）。判断すると次の行に進みます。</Hint>
      <div className="lv-rows" ref={listRef} role="grid" aria-label="影響候補の一覧">
        {sort === "priority" ? GROUPS.map((g) => {
          const cs = grouped[g.key];
          if (g.key === "done" && !showDone) return cs.length ? (
            <button key="done" className="lv-more link small" onClick={() => setShowDone(true)}>判断済み {cs.length} 件を表示</button>) : null;
          if (!cs.length) return null;
          const allOn = cs.every((c) => checked.has(c.node_id));
          return (
            <section key={g.key} className={`lv-group g-${g.key}`}>
              <header>
                <input type="checkbox" aria-label={`${g.title}をすべて選択`} checked={allOn} onChange={(e) => checkGroup(cs, e.target.checked)} />
                <h3>{g.title} <span className="mono muted">{cs.length}</span></h3>
                {g.hint && <span className="muted small">{g.hint}</span>}
              </header>
              {cs.map(renderRow)}
            </section>
          );
        }) : rows.filter((c) => showDone || !c.decision).map(renderRow)}
        {!rows.length && <p className="muted small lv-empty">条件に合う候補はありません。上の絞り込みを確認してください。</p>}
      </div>
      <p className="lv-keys muted tiny">キー操作: ↑↓（j/k）で移動・c で確定・x で除外・u で戻す。層のチップ・未判定だけ・確度・検索の絞り込みはリストにも効きます（{LAYER_JA.code}・{LAYER_JA.data} など）。</p>
    </div>
  );
}

/** The element's share of the estimate: confirmed hours, or the expected hours while undecided. */
function Effort({ run, c }: { run: RunState; c: Candidate }) {
  const s0 = elementSize(run, c.node_id);
  const r2 = (x: number) => Math.round(x * 100) / 100;
  const s = { ...s0, hours: r2(s0.hours), expected: r2(s0.expected) };
  if (s.status === "none") {
    return <div className="lrow-eff muted" title={c.decision?.status === "excluded" ? "除外のため工数に含めません"
      : "工数の対象外（設定・ログなど）、または影響低のため含めません"}>—</div>;
  }
  const tip = s.status === "confirmed"
    ? `確定: ${s.hours}h${s.notes.length ? `（${s.notes.join(" / ")}）` : ""}`
    : `未判定: ${s.hours}h × 確からしさ ${s.probability} = 見込み ${s.expected}h${s.notes.length ? `（${s.notes.join(" / ")}）` : ""}`;
  return (
    <div className={`lrow-eff ${s.status}`} title={tip}>
      <span className="mono">{s.status === "confirmed" ? s.hours : s.expected}h</span>
      {s.status === "pending" && <span className="tiny muted">見込み</span>}
    </div>
  );
}

const TX_LABEL: Record<string, string> = {
  REMOTE_IN_TX: "TX内リモート呼出", INDEPENDENT_TX: "別TXで確定", PROC_COMMIT_IN_TX: "ストアド内COMMIT",
  AUTONOMOUS: "自律型TX", DBLINK_WRITE: "DBリンク更新", COMMIT_IN_LOOP: "分割コミット", LOCK: "行ロック",
  ASYNC_MESSAGE: "非同期メッセージ", FILE_OUTPUT_IN_TX: "TX内ファイル出力",
};

/** Transaction / integration risks as one compact badge (worst level first); details in the inspector. */
export function TxBadge({ c }: { c: Candidate }) {
  const rs = c.tx_risks || [];
  if (!rs.length && !c.tx) return null;
  const worst = rs.some((r) => r.level === "high") ? "high" : rs.some((r) => r.level === "medium") ? "medium" : "low";
  const codes = Array.from(new Set(rs.map((r) => TX_LABEL[r.code] || r.code)));
  const title = [c.tx ? `トランザクション境界: ${c.tx.boundary}（${c.tx.source}）` : "", ...rs.map((r) => `・${r.message}`)]
    .filter(Boolean).join("\n");
  return (
    <span className={`txb tx-${rs.length ? worst : "none"}`} title={title}>
      {c.tx ? `TX ${c.tx.boundary}` : "TX"}{codes.length ? ` · ${codes.slice(0, 2).join("・")}${codes.length > 2 ? ` 他${codes.length - 2}` : ""}` : ""}
    </span>
  );
}

export { TX_LABEL };
