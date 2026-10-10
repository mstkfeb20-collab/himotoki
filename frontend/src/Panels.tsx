import { useEffect, useState } from "react";
import { api } from "./api";
import type { IndexReport, RunEvent, RunState } from "./types";

export function ChecksPanel({ run, onSelect, onChanged }: {
  run: RunState; onSelect: (ids: string[], focus?: boolean) => void; onChanged: () => void;
}) {
  const [err, setErr] = useState<string | null>(null);
  const pass = run.checks.filter((c) => c.ok).length;
  const all = run.checks.length > 0 && pass === run.checks.length;
  const acceptHigh = async (threshold: number) => {
    const items = Object.values(run.candidates)
      .filter((c) => !c.decision && c.verdict === "impacted" && (c.confidence ?? 0) >= threshold)
      .map((c) => ({ node_id: c.node_id, status: "confirmed", reason: `AI判定（確度${threshold}以上）を一括承認` }));
    if (items.length) { await api.bulk(run.id, items); onChanged(); }
  };
  const excludeLow = async () => {
    const items = Object.values(run.candidates)
      .filter((c) => !c.decision && c.verdict === "not_impacted")
      .map((c) => ({ node_id: c.node_id, status: "excluded", reason: "AI判定「影響低」を一括除外" }));
    if (items.length) { await api.bulk(run.id, items); onChanged(); }
  };
  return (
    <div className="stack">
      <div className="row between"><span className="k">全数判定の条件</span><span className="mono small">{pass} / {run.checks.length}</span></div>
      {run.checks.map((c) => (
        <div key={c.key} className="check">
          <span className={c.ok ? "mark ok" : "mark"}>{c.ok ? "✓" : "○"}</span>
          <div className="grow">
            <div>{c.title}</div>
            <div className="muted small">{c.detail}</div>
            {!c.ok && c.open.length > 0 && c.key !== "intents" && (
              <button className="link small" onClick={() => onSelect(c.open.filter((id) => run.candidates[id]).slice(0, 40), true)}>
                未判定 {c.open.length} 件をグラフで選択</button>
            )}
          </div>
        </div>
      ))}
      <div className="actions">
        <button className="btn small" onClick={() => acceptHigh(0.8)}>確度0.8以上のAI判定を承認</button>
        <button className="btn small" onClick={excludeLow}>「影響低」をまとめて除外</button>
      </div>
      {run.approved ? (
        <p className="ok-box">全数として確定済み（承認: {run.approved.actor}・{new Date(run.approved.at * 1000).toLocaleString("ja-JP")}・{run.approved.estimate_days}人日）</p>
      ) : (
        <button className={all ? "btn primary wide" : "btn wide"} disabled={!all}
          onClick={async () => { try { await api.approve(run.id); onChanged(); } catch (e: any) { setErr(e.message); } }}>
          {all ? "全数として確定する" : `全数確定まで残り ${run.checks.length - pass} 条件`}
        </button>
      )}
      {err && <p className="warn small">{err}</p>}
      <p className="muted small">全数の定義: 定義した探索範囲（依存グラフ・横断検索・CRUD・独立再探索）に未判定が残っていないこと。100%の網羅を証明するものではありません。</p>
    </div>
  );
}

export function EstimatePanel({ run, onSelect }: { run: RunState; onSelect: (ids: string[], f?: boolean) => void }) {
  const e = run.estimate;
  if (!e || e.days === undefined) return <p className="muted small">算出中…</p>;
  return (
    <div className="stack">
      <div className="big"><span className="mono">{e.days}</span><span className="muted"> 人日</span>
        <span className="mono small muted">（{e.low} 〜 {e.high}）</span></div>
      <p className="muted small">確定分 {e.confirmed_hours}h ＋ 未判定の期待値 {e.pending_expected_hours}h、レビュー等の係数 ×{e.overhead}、
        実績補正 ×{e.calibration}{e.calibration_samples ? `（過去${e.calibration_samples}件）` : "（実績未登録）"}</p>
      <table className="tbl">
        <thead><tr><th>対象</th><th>変更</th><th className="r">h</th></tr></thead>
        <tbody>
          {(e.lines || []).map((l) => (
            <tr key={l.node_id} title={l.notes.join(" / ")}>
              <td><button className="link" onClick={() => onSelect([l.node_id], true)}>{l.name}</button>
                <div className="muted tiny">{l.notes.join(" · ")}</div></td>
              <td>{l.change_type}</td><td className="r mono">{l.hours}</td>
            </tr>
          ))}
          {(e.pending || []).slice(0, 15).map((l) => (
            <tr key={l.node_id} className="pending-row">
              <td><button className="link" onClick={() => onSelect([l.node_id], true)}>{l.name}</button>
                <div className="muted tiny">未判定・確率 {l.probability}</div></td>
              <td>{l.change_type}?</td><td className="r mono">{l.hours}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const EV_LABEL: Record<string, (d: any) => string> = {
  "run.started": (d) => `解析開始（判定: ${d.llm}）`,
  phase: (d) => d.label,
  intent: (d) => `変更意図 ${d.id}: ${d.summary}（${d.change_type}）`,
  pattern: (d) => d.pattern ? `${d.intent} に変更パターン ${d.pattern} を適用` : "",
  candidate: (d) => `${d.origin === "graph" ? "追跡" : d.origin}: ${d.name}`,
  verdict: () => "",
  "grep.done": (d) => `横断検索: 語 ${d.terms.length} 個・${d.hits} 行ヒット・新規 ${d.new_candidates} 件`,
  "shadow.done": (d) => `独立再探索: ${d.found} 件中 ${d.only} 件が本探索に無し`,
  "crud.done": (d) => `CRUD確認: ${Object.keys(d.tables).join(", ") || "対象なし"}`,
  checks: (d) => `全数条件 ${d.checks.filter((c: any) => c.ok).length}/${d.checks.length}・規模 ${d.estimate.days}人日`,
  decision: (d) => `${d.actor}: ${d.node_id.split(/[.#/]/).pop()} を${d.decision ? (d.decision.status === "confirmed" ? "確定" : "除外") : "未判定に戻す"}`,
  "expand.done": (d) => `追加ノードから ${d.new.length} 件を追跡`,
  comment: () => "コメント更新",
  approved: (d) => `全数として承認（${d.actor}）`,
  "run.done": (d) => `解析完了: 候補 ${d.candidates} 件`,
  error: (d) => `エラー: ${d.message}`,
};

export function LogPanel({ events, onSelect }: { events: RunEvent[]; onSelect: (ids: string[], f?: boolean) => void }) {
  const rows = events.filter((e) => e.type !== "verdict").slice(-300).reverse();
  return (
    <ol className="log mono">
      {rows.map((e) => {
        const label = (EV_LABEL[e.type] || ((d: any) => e.type))(e.data);
        if (!label) return null;
        const nid = e.data?.node_id;
        return (
          <li key={e.seq} className={`ev ev-${e.type.replace(".", "-")} o-${e.data?.origin || ""}`}>
            <span className="dim">{new Date(e.ts * 1000).toLocaleTimeString("ja-JP")}</span>
            {nid ? <button className="link" onClick={() => onSelect([nid], true)}>{label}</button> : <span>{label}</span>}
          </li>
        );
      })}
    </ol>
  );
}

export function DiffPanel({ run, onOpenRun }: { run: RunState; onOpenRun: (id: string) => void }) {
  const [diff, setDiff] = useState<any>(null);
  useEffect(() => {
    if (run.parent_id && run.status === "done") api.diff(run.parent_id, run.id).then(setDiff).catch(() => setDiff(null));
    else setDiff(null);
  }, [run.id, run.parent_id, run.status]);
  if (!run.parent_id) return <p className="muted small">この解析は最初のrunです。再調査を実行すると、前のrunとの差分がここに出ます。</p>;
  if (!diff) return <p className="muted small">比較中…</p>;
  return (
    <div className="stack">
      <button className="link small" onClick={() => onOpenRun(run.parent_id!)}>前のrunを開く</button>
      <span className="k">追加された候補 {diff.added.length}</span>
      <ul className="list">{diff.added.slice(0, 30).map((x: any) => <li key={x.node_id}>{x.name} <span className="dim small">{x.verdict}</span></li>)}</ul>
      <span className="k">判定が変わった {diff.changed.length}</span>
      <ul className="list">{diff.changed.map((x: any) => <li key={x.node_id}>{x.name} <span className="dim small">{x.from} → {x.to}</span></li>)}</ul>
      <span className="k">前回のみ {diff.removed.length}</span>
    </div>
  );
}

/** What the graph itself knows it is missing. Shown next to the run so that a
 * reviewer judging "is this complete?" sees the blind spots of the index too. */
export function IndexPanel({ graph }: { graph?: string | null }) {
  const [r, setR] = useState<IndexReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setR(null); api.indexReport(graph).then(setR).catch((e) => setErr(e.message)); }, [graph]);
  if (err) return <p className="warn small">{err}</p>;
  if (!r) return <p className="muted small">読み込み中…</p>;
  const unresolved = Object.entries(r.unresolved || {}).sort((a, b) => b[1] - a[1]);
  const drift = r.drift || {};
  const rt = r.runtime;
  return (
    <div className="stack">
      <div className="row between"><span className="k">取込</span>
        <span className="mono small">{r.files} ファイル・ノード {r.nodes}・関係 {r.edges}</span></div>
      <p className="muted small">再抽出 {r.parsed}・キャッシュ {r.cached}・{r.seconds}秒{r.commit ? `・${r.commit.slice(0, 8)}` : ""}
        {r.rules?.length ? `・ルール ${r.rules.join(", ")}` : ""}</p>

      <span className="k">解決できなかった参照 {unresolved.length ? "" : "（なし）"}</span>
      {unresolved.length > 0 && (
        <table className="report-table"><tbody>
          {unresolved.map(([ns, n]) => (
            <tr key={ns}><td>{ns}</td><td className="num">{n}</td>
              <td className="sample">{(r.unresolved_samples?.[ns] || []).slice(0, 2).map((x) => `${x.key}（${x.file.split("/").pop()}）`).join(" / ")}</td></tr>
          ))}
        </tbody></table>
      )}
      {r.external && Object.keys(r.external).length > 0 && (
        <p className="muted small">ライブラリへの参照（グラフ外・漏れではない）: {Object.entries(r.external).map(([k, v]) => `${k} ${v}`).join("・")}</p>
      )}

      <span className="k">DB辞書とDDLのずれ</span>
      {drift.counts ? (
        <>
          <p className="small">DBにのみ {drift.counts.only_db ?? 0}・DDLにのみ {drift.counts.only_ddl ?? 0}・一致 {drift.counts.both ?? 0}
            {drift.counts.tables_not_in_catalog ? `・辞書に無いテーブル ${drift.counts.tables_not_in_catalog}` : ""}</p>
          {(drift.columns_only_in_db || []).length > 0 &&
            <p className="sample">DBにのみ: {(drift.columns_only_in_db || []).slice(0, 12).map((c) => c.replace("col:", "")).join(", ")}</p>}
        </>
      ) : <p className="muted small">{drift.note || "DB辞書が未取込です（impact catalog で取り込めます）"}</p>}

      <span className="k">実行時SQLとの照合</span>
      {rt ? (
        <>
          <p className="small">実行時SQL {rt.statements} 件中、コードと対応 {rt.matched}・対応なし {rt.unmatched}</p>
          {(rt.unmatched_samples || []).slice(0, 8).map((x) => (
            <p key={x.id} className="sample">{x.calls ? `${x.calls}回 ` : ""}{x.sql}</p>
          ))}
          {rt.unmatched > 0 && <p className="muted small">対応なしのSQLは、動的SQL・他システム・運用ツールなど、静的解析の死角を示します。</p>}
        </>
      ) : <p className="muted small">実行時SQLが未取込です（impact runtime で取り込めます）</p>}

      {r.errors > 0 && <><span className="k">抽出エラー {r.errors}</span>
        {(r.error_samples || []).slice(0, 5).map((e, i) => <p key={i} className="sample">{e.file}: {e.error}</p>)}</>}
    </div>
  );
}
