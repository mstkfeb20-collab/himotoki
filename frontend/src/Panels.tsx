import { useEffect, useState } from "react";
import { api } from "./api";
import { LAYERS_JA, sizeOf } from "./sizing";
import type { IndexReport, KnowledgeSummary, RunEvent, RunState } from "./types";

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

/** Effort by layer (機能 / コード / データ / テスト / 設計書); clicking a row filters the views to it. */
function LayerBreakdown({ run, onOnlyLayer }: { run: RunState; onOnlyLayer?: (layer: string) => void }) {
  const cs = Object.values(run.candidates);
  const rows = LAYERS_JA.map(([k, l]) => ({ k, l, s: sizeOf(run, cs.filter((c) => c.layer === k)) }))
    .filter((r) => r.s.count);
  const tot = sizeOf(run, cs);
  return (
    <div>
      <span className="k">層ごとの内訳</span>
      <table className="tbl layer-tbl">
        <thead><tr><th>層</th><th className="r">件数</th><th className="r">確定 h</th><th className="r">見込み h</th><th className="r">人日</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.k} className={onOnlyLayer ? "click" : ""} onClick={() => onOnlyLayer?.(r.k)}
              title={onOnlyLayer ? `${r.l}だけを一覧・グラフに表示` : ""}>
              <td>{r.l}</td><td className="r mono">{r.s.count}</td><td className="r mono">{r.s.confirmedH}</td>
              <td className="r mono">{r.s.expectedH}</td><td className="r mono">{r.s.days}</td>
            </tr>
          ))}
          <tr className="sum"><td>合計</td><td className="r mono">{tot.count}</td><td className="r mono">{tot.confirmedH}</td>
            <td className="r mono">{tot.expectedH}</td><td className="r mono">{tot.days}</td></tr>
        </tbody>
      </table>
      <p className="muted tiny">人日 =（確定 h ＋ 見込み h）× レビュー等の係数 × 実績補正 ÷ 1日の時間。見込み h は未判定の要素の工数 × 確からしさ。</p>
    </div>
  );
}

export function EstimatePanel({ run, onSelect, onOnlyLayer }: { run: RunState; onSelect: (ids: string[], f?: boolean) => void;
  onOnlyLayer?: (layer: string) => void }) {
  const e = run.estimate;
  if (!e || e.days === undefined) return <p className="muted small">算出中…</p>;
  return (
    <div className="stack">
      <div className="big"><span className="mono">{e.days}</span><span className="muted"> 人日</span>
        <span className="mono small muted">（{e.low} 〜 {e.high}）</span></div>
      <p className="muted small">確定分 {e.confirmed_hours}h ＋ 未判定の期待値 {e.pending_expected_hours}h、レビュー等の係数 ×{e.overhead}、
        実績補正 ×{e.calibration}{e.calibration_samples ? `（過去${e.calibration_samples}件）` : "（実績未登録）"}</p>
      <LayerBreakdown run={run} onOnlyLayer={onOnlyLayer} />
      <AiUsage run={run} />
      <span className="k">要素ごとの工数（確定分と、見込みの大きい未判定分）</span>
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
      ) : <p className="muted small">{drift.note || "DB辞書が未取込です（himotoki catalog で取り込めます）"}</p>}

      <span className="k">実行時SQLとの照合</span>
      {rt ? (
        <>
          <p className="small">実行時SQL {rt.statements} 件中、コードと対応 {rt.matched}・対応なし {rt.unmatched}</p>
          {(rt.unmatched_samples || []).slice(0, 8).map((x) => (
            <p key={x.id} className="sample">{x.calls ? `${x.calls}回 ` : ""}{x.sql}</p>
          ))}
          {rt.unmatched > 0 && <p className="muted small">対応なしのSQLは、動的SQL・他システム・運用ツールなど、静的解析の死角を示します。</p>}
        </>
      ) : <p className="muted small">実行時SQLが未取込です（himotoki runtime で取り込めます）</p>}

      {r.errors > 0 && <><span className="k">抽出エラー {r.errors}</span>
        {(r.error_samples || []).slice(0, 5).map((e, i) => <p key={i} className="sample">{e.file}: {e.error}</p>)}</>}
    </div>
  );
}


/** How the candidates of this run were judged and what the AI calls cost. */
export function AiUsage({ run }: { run: RunState }) {
  const u = run.llm_usage;
  const b = run.params?.llm;
  if (!u) return null;
  const fmt = (n: number) => n.toLocaleString("ja-JP");
  return (
    <div className="ai-usage">
      <span className="k">AIの利用</span>
      <table className="report-table"><tbody>
        <tr><td>AIで判定</td><td className="num">{fmt(u.ai_items)}</td>
          <td className="sample">上限 {b ? (b.max_items === "unlimited" ? "なし" : fmt(b.max_items as number)) : "—"}</td></tr>
        <tr><td>前回の判定を再利用</td><td className="num">{fmt(u.reused)}</td><td /></tr>
        <tr><td>ルールで判定（低スコア）</td><td className="num">{fmt(u.rule_low_score)}</td>
          <td className="sample">{b ? `${b.min_score} 未満` : ""}</td></tr>
        <tr><td>ルールで判定（上限超過）</td><td className="num">{fmt(u.rule_cap)}</td>
          <td className="sample">{u.rule_cap ? "要確認として残ります" : ""}</td></tr>
        {u.rule > 0 && <tr><td>ルールで判定（AI未設定）</td><td className="num">{fmt(u.rule)}</td><td /></tr>}
        {u.fallback > 0 && <tr><td>AI失敗でルールに切替</td><td className="num">{fmt(u.fallback)}</td><td /></tr>}
        <tr><td>API呼び出し</td><td className="num">{fmt(u.calls)}</td>
          <td className="sample">入力 {fmt(u.input_tokens)}・出力 {fmt(u.output_tokens)} トークン</td></tr>
        <tr><td>費用</td><td className="num">¥{(u.cost_jpy ?? 0).toLocaleString("ja-JP", { maximumFractionDigits: 1 })}</td>
          <td className="sample">${(u.cost_usd ?? 0).toFixed(4)} × {u.jpy_per_usd ?? "—"}円{u.model ? `・${u.model}` : ""}</td></tr>
        {!!u.unpriced_calls && <tr><td colSpan={3} className="warn small">単価が未設定のモデルの呼び出し {u.unpriced_calls} 回は費用に含まれていません（settings.yaml の pricing）</td></tr>}
      </tbody></table>
    </div>
  );
}


const REL_SHORT: Record<string, string> = { READS: "読む", WRITES: "書く", CALLS: "呼ぶ", REFERENCES: "参照", EXECUTES: "実行",
  DEFINES: "定義", CONTAINS: "含む", BINDS: "画面項目", MAPS_TO: "対応", RUNS: "起動", DESCRIBES: "記載", LEARNED: "実績" };

/** What this system's analyses learned from people's decisions and verified changes; each item can be switched off. */
export function KnowledgePanel({ graph, onSelect }: { graph?: string | null; onSelect: (ids: string[], f?: boolean) => void }) {
  const [k, setK] = useState<KnowledgeSummary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { setK(null); api.knowledge(graph).then(setK).catch((e) => setErr(e.message)); }, [graph]);
  const toggle = async (kind: string, key: string, ignore: boolean) => {
    try { setK(await api.knowledgeIgnore(graph, kind, key, ignore)); } catch (e: any) { setErr(e.message); }
  };
  if (err) return <p className="warn small">{err}</p>;
  if (!k) return <p className="muted small">読み込み中…</p>;
  const sw = (kind: string, key: string, ignored: boolean) => (
    <button className="btn small ghost" onClick={() => toggle(kind, key, !ignored)}>{ignored ? "有効に戻す" : "無効にする"}</button>);
  return (
    <div className="stack knowledge">
      <p className="small">判断 <b>{k.decisions}</b> 件（解析 {k.analyses} 回）・実装との照合 <b>{k.verifications}</b> 回から学んだ補正です。
        次の解析から確度と並び順に反映されます（解析ごとに「新しい解析」で切れます）。</p>
      {k.decisions === 0 && k.verifications === 0 && <p className="muted small">まだ学んだことはありません。候補を確定・除外したり、「開発へ渡す」→「実績と照合」を行うと、ここにたまっていきます。</p>}
      {k.nodes.length > 0 && <>
        <div className="k">要素ごとの履歴</div>
        <ul className="klist">{k.nodes.map((n) => (
          <li key={n.node_id} className={n.ignored ? "off" : ""}>
            <div className="row between"><button className="link" onClick={() => onSelect([n.node_id], true)}>{n.name}</button>
              <span className="row"><span className="mono small">×{n.mult}</span>{sw("node", n.node_id, n.ignored)}</span></div>
            <div className="muted small">{n.notes.map((x) => x.message).join("／")}</div>
          </li>))}</ul>
      </>}
      {k.links.length > 0 && <>
        <div className="k">学習した関係（依存関係には無いが、一緒に変更された）</div>
        <ul className="klist">{k.links.map((l) => (
          <li key={l.key} className={l.ignored ? "off" : ""}><div className="row between">
            <span className="small">{l.src_name} → <b>{l.dst_name}</b> <span className="muted">（{l.count} 回）</span></span>{sw("link", l.key, l.ignored)}</div></li>))}</ul>
      </>}
      {k.patterns.length > 0 && <>
        <div className="k">経路の傾向（{k.thresholds.pattern} 件の判断で反映）</div>
        <ul className="klist">{k.patterns.slice(0, 30).map((p) => (
          <li key={p.key} className={p.ignored ? "off" : ""}><div className="row between">
            <span className="small">{p.parent_kind} →（{REL_SHORT[p.rel] || p.rel}）→ {p.kind}
              <span className="muted">　確定 {p.confirmed}・除外 {p.excluded}</span></span>
            <span className="row"><span className="mono small">{p.active ? `×${p.mult}` : "件数不足"}</span>{sw("pattern", p.key, p.ignored)}</span></div></li>))}</ul>
      </>}
    </div>
  );
}
