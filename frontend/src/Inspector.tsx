import { TX_LABEL } from "./ListView";
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { statusOf } from "./Graph";
import type { Candidate, NodeDetail, RunState } from "./types";

const STATUS_LABEL: Record<string, string> = {
  confirmed: "確定", human: "人が追加", excluded: "除外", impacted: "影響あり（AI）", uncertain: "要確認（AI）",
  low: "影響低（AI）", pending: "未判定",
};
const ORIGIN_LABEL: Record<string, string> = {
  seed: "要求から直接", pattern: "変更パターン", graph: "依存の追跡", grep: "横断検索", shadow: "独立再探索",
  crud: "CRUD確認", human: "人が追加", focus: "再調査の起点", learned: "過去の実績から",
};
const LAYER_LABEL: Record<string, string> = { feature: "機能", code: "コード", data: "データ", test: "テスト", doc: "設計書" };

type Tab = "why" | "code" | "doc" | "test" | "hist" | "comment";

export default function Inspector({ run, ids, onSelect, onChanged, busy }: {
  run: RunState;
  ids: string[];
  onSelect: (ids: string[], focus?: boolean) => void;
  onChanged: () => void;
  busy: boolean;
}) {
  if (ids.length === 0) return <Empty run={run} onSelect={onSelect} />;
  if (ids.length > 1) return <Multi run={run} ids={ids} onChanged={onChanged} onSelect={onSelect} />;
  const c = run.candidates[ids[0]];
  if (!c) return <div className="pane inspector"><p className="muted">ノードが見つかりません</p></div>;
  return <Single key={c.node_id} run={run} c={c} onSelect={onSelect} onChanged={onChanged} busy={busy} />;
}

function Empty({ run, onSelect }: { run: RunState; onSelect: (ids: string[], focus?: boolean) => void }) {
  const open = Object.values(run.candidates).filter((c) => !c.decision)
    .sort((a, b) => (b.confidence ?? b.score) - (a.confidence ?? a.score)).slice(0, 8);
  return (
    <div className="pane inspector">
      <span className="k">要求</span>
      <p className="req">{run.requirement}</p>
      {run.instruction && <p className="muted small">再調査の指示: {run.instruction}</p>}
      <span className="k">変更意図</span>
      <ul className="intents">
        {run.intents.map((i) => (
          <li key={i.id}><span className="mono dim">{i.id}</span> {i.summary}
            <span className="tags">
              {(i as any).instruction ? <span className="tag violet">再調査の指示</span> : <span className="tag">{i.change_type}</span>}
              {i.pattern && <span className="tag accent">{i.pattern}</span>}
              {i.new_terms.map((t) => <span key={t} className="tag violet">新: {t}</span>)}
            </span>
          </li>
        ))}
      </ul>
      {open.length > 0 && <>
        <span className="k">確度の高い未判定</span>
        <ul className="list">
          {open.map((c) => (
            <li key={c.node_id}><button className="link" onClick={() => onSelect([c.node_id], true)}>{c.name}</button>
              <span className="mono dim small">{(c.confidence ?? c.score).toFixed(2)}</span></li>
          ))}
        </ul>
      </>}
      <p className="muted small">グラフのノードをクリックで選択、Shift+クリックやShift+ドラッグで複数選択できます。</p>
    </div>
  );
}

function Multi({ run, ids, onChanged, onSelect }: { run: RunState; ids: string[]; onChanged: () => void; onSelect: (ids: string[], f?: boolean) => void }) {
  const [reason, setReason] = useState("");
  const cs = ids.map((i) => run.candidates[i]).filter(Boolean);
  const act = async (status: string) => {
    await api.bulk(run.id, cs.map((c) => ({ node_id: c.node_id, status, reason })));
    onChanged();
  };
  return (
    <div className="pane inspector">
      <span className="k">{cs.length} 件を選択中</span>
      <ul className="list scroll">
        {cs.map((c) => <li key={c.node_id}><button className="link" onClick={() => onSelect([c.node_id], true)}>{c.name}</button>
          <span className={`badge s-${statusOf(c)}`}>{STATUS_LABEL[statusOf(c)]}</span></li>)}
      </ul>
      <label className="k" htmlFor="bulk-reason">理由（除外時は必須）</label>
      <input id="bulk-reason" className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例: 表示専用で今回の変更対象外" />
      <div className="actions">
        <button className="btn primary" onClick={() => act("confirmed")}>まとめて確定</button>
        <button className="btn" disabled={!reason.trim()} onClick={() => act("excluded")}>まとめて除外</button>
        <button className="btn" onClick={() => act("open")}>未判定に戻す</button>
      </div>
    </div>
  );
}

function Single({ run, c, onSelect, onChanged, busy }: {
  run: RunState; c: Candidate; onSelect: (ids: string[], f?: boolean) => void; onChanged: () => void; busy: boolean;
}) {
  const [tab, setTab] = useState<Tab>("why");
  const [d, setD] = useState<NodeDetail | null>(null);
  const [excluding, setExcluding] = useState(false);
  const [reason, setReason] = useState("");
  const [comment, setComment] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const st = statusOf(c);

  useEffect(() => { api.node(c.node_id, run.id).then(setD).catch(() => setD(null)); }, [c.node_id, run.id]);
  const codeRef = useRef<HTMLPreElement>(null);
  useEffect(() => {
    const pre = codeRef.current;
    const hit = pre?.querySelector(".ln.hit") as HTMLElement | null;
    if (pre && hit) pre.scrollTop = Math.max(0, hit.offsetTop - pre.clientHeight / 3);
  }, [tab, d]);

  const decide = async (status: string, extra: Record<string, unknown> = {}) => {
    try {
      setErr(null);
      await api.decide(run.id, c.node_id, status, status === "excluded" ? reason : (reason || ""), extra);
      setExcluding(false); setReason("");
      onChanged();
    } catch (e: any) { setErr(e.message); }
  };
  const addNeighbor = async (id: string, name: string) => {
    if (run.candidates[id]) { onSelect([id], true); return; }
    await api.add(run.id, { node_id: id, reason: `${c.name} の確認中に追加（${name}）` });
    onChanged();
    onSelect([id], true);
  };
  const conf = c.confidence ?? c.score;
  const hl = new Set(d?.highlight || []);
  const nodeRange = d?.node.line ? [d.node.line, d.node.end_line || d.node.line] : null;

  return (
    <div className="pane inspector">
      <div className="row between">
        <span className="k">{LAYER_LABEL[c.layer] || c.layer} · {c.kind}</span>
        <span className={`badge s-${st}`}>{STATUS_LABEL[st]}</span>
      </div>
      <h2 className="mono title">{c.name}</h2>
      {c.file && <span className="mono dim small">{c.file}{c.line ? `:${c.line}` : ""}</span>}
      <div className="row conf"><div className="bar"><div style={{ width: `${conf * 100}%` }} className={`fill s-${st}`} /></div>
        <span className="mono small">確度 {conf.toFixed(2)}</span></div>
      <p className="reason">{c.reason || c.seed_reason}</p>
      {(c.learned?.length || 0) > 0 && (
        <section className="txbox learnbox" aria-label="過去の判断・実績">
          <div className="k">過去の判断・実績（このシステムで学んだこと）</div>
          <ul>{c.learned!.map((n, i) => (
            <li key={i} className={n.factor > 1 ? "tx-medium" : ""}>{n.message}
              {n.factor !== 1 && <span className="mono dim small">（確度 ×{n.factor}）</span>}</li>))}</ul>
        </section>
      )}
      {(c.tx || (c.tx_risks?.length || 0) > 0) && (
        <section className="txbox" aria-label="トランザクション・連携上の注意">
          <div className="k">トランザクション・連携{c.tx ? ` — 境界 ${c.tx.boundary}（${c.tx.source}、${c.tx.members}要素）` : ""}</div>
          {c.tx && c.tx.tables.length > 0 && <div className="small dim">更新・参照テーブル: <span className="mono">{c.tx.tables.join(", ")}</span></div>}
          <ul>
            {(c.tx_risks || []).map((r, i) => (
              <li key={i} className={`tx-${r.level}`}>
                <span className="txcode">{TX_LABEL[r.code] || r.code}</span> {r.message}
                {r.at !== c.node_id && (run.candidates[r.at]
                  ? <> — <button className="link" onClick={() => onSelect([r.at], true)}>{run.candidates[r.at].name}</button></>
                  : <span className="mono dim small"> — {r.at.replace(/^[a-z]+:/, "").split(/[.#]/).slice(-2).join(".")}</span>)}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="tabs" role="tablist">
        {([["why", "根拠"], ["code", "コード"], ["doc", `設計書${d?.docs.length ? ` ${d.docs.length}` : ""}`],
           ["test", `テスト${d ? ` ${d.tests.length}` : ""}`], ["hist", "履歴"], ["comment", `コメント${c.comments.length ? ` ${c.comments.length}` : ""}`]] as [Tab, string][])
          .map(([k, label]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "tab on" : "tab"} onClick={() => setTab(k)}>{label}</button>
          ))}
      </div>

      <div className="tabbody">
        {tab === "why" && (
          <div className="stack">
            <ol className="path mono">
              <li><span className="dim">00</span> 要求</li>
              {c.intent_ids[0] && <li><span className="dim">01</span> {c.intent_ids[0]} {run.intents.find((i) => i.id === c.intent_ids[0])?.summary.slice(0, 26)}</li>}
              {c.path.map((p, i) => (
                <li key={p}><span className="dim">{String(i + 2).padStart(2, "0")}</span>{" "}
                  <button className="link" onClick={() => onSelect([p], true)}>{run.candidates[p]?.name || p}</button></li>
              ))}
            </ol>
            <dl className="meta">
              <dt>見つけ方</dt><dd>{ORIGIN_LABEL[c.origin] || c.origin}</dd>
              <dt>AI判定</dt><dd>{c.verdict ?? "—"}（{run.llm}）</dd>
              {c.decision && <><dt>人の判断</dt><dd>{c.decision.status === "confirmed" ? "確定" : "除外"} — {c.decision.actor}{c.decision.reason ? `「${c.decision.reason}」` : ""}{c.decision.inherited ? "（前回runから引継ぎ）" : ""}</dd></>}
            </dl>
          </div>
        )}
        {tab === "code" && (
          <div className="stack">
            {d && d.code.length > 0 ? (
              <pre className="code" ref={codeRef}>{d.code.map((l) => {
                const inNode = nodeRange && l.n >= nodeRange[0] && l.n <= nodeRange[1];
                return <div key={l.n} className={hl.has(l.n) ? "ln hit" : inNode ? "ln in" : "ln"}><span className="no">{l.n}</span>{l.text}</div>;
              })}</pre>
            ) : <p className="muted small">コード断片はありません（{c.kind}）。</p>}
            {d && (d.callers?.length || 0) > 0 && <NeighborList title="参照元・呼び出し元" items={d.callers!} run={run} onPick={addNeighbor} />}
            {d && (d.callees?.length || 0) > 0 && <NeighborList title="参照先・呼び出し先" items={d.callees!} run={run} onPick={addNeighbor} />}
          </div>
        )}
        {tab === "doc" && (
          <div className="stack">
            {d?.docs.length ? d.docs.map((doc) => (
              <div key={doc.id} className="doc"><span className="mono dim small">{doc.file}:{doc.line}</span>
                <pre className="doctext">{doc.text}</pre></div>
            )) : <p className="muted small">この要素に触れている設計書は見つかっていません。設計書フォルダを指定して再インデックスすると照合できます。</p>}
          </div>
        )}
        {tab === "test" && (
          <div className="stack">
            {d?.tests.length ? <NeighborList title="この要素を検証しているテスト" items={d.tests} run={run} onPick={addNeighbor} />
              : <p className="warn small">この要素を検証するテストが見つかりません。修正時はテスト追加が必要です。</p>}
          </div>
        )}
        {tab === "hist" && (
          <ul className="list">
            {d?.history.length ? d.history.map((h) => (
              <li key={h.hash}><span className="mono dim small">{h.date} {h.hash}</span> {h.subject}</li>
            )) : <li className="muted small">変更履歴はありません。</li>}
          </ul>
        )}
        {tab === "comment" && (
          <div className="stack">
            {c.comments.map((m, i) => <div key={i} className="comment"><b>{m.author}</b> {m.text}</div>)}
            <label className="k" htmlFor="assignee">確認担当</label>
            <input id="assignee" className="input" defaultValue={c.assignee || ""} placeholder="例: 請求チーム 佐藤"
              onBlur={(e) => { if ((e.target.value || null) !== (c.assignee || null)) api.comment(run.id, c.node_id, "", e.target.value).then(onChanged); }} />
            <label className="k" htmlFor="cm">コメント</label>
            <textarea id="cm" className="input" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
            <button className="btn" disabled={!comment.trim()} onClick={async () => { await api.comment(run.id, c.node_id, comment); setComment(""); onChanged(); }}>コメントを追加</button>
          </div>
        )}
      </div>

      <div className="grid2">
        <label className="field"><span className="k">変更種別</span>
          <select className="input" value={c.change_type || "modify"} onChange={(e) => decide(c.decision ? c.decision.status : "keep", { change_type: e.target.value })}>
            {["add", "modify", "delete", "test", "doc"].map((v) => <option key={v} value={v}>{v}</option>)}
          </select></label>
        <label className="field"><span className="k">難易度補正</span>
          <select className="input" value={c.difficulty || "mid"} onChange={(e) => decide(c.decision ? c.decision.status : "keep", { difficulty: e.target.value })}>
            <option value="mid">標準（推定）</option><option value="low">低</option><option value="high">高</option>
          </select></label>
      </div>

      {excluding ? (
        <div className="stack">
          <label className="k" htmlFor="ex-reason">除外理由（記録されます）</label>
          <input id="ex-reason" autoFocus className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="例: 一覧表示のみで今回の項目は出さない" />
          <div className="actions">
            <button className="btn" disabled={!reason.trim() || busy} onClick={() => decide("excluded")}>除外する</button>
            <button className="btn ghost" onClick={() => setExcluding(false)}>やめる</button>
          </div>
        </div>
      ) : (
        <div className="actions">
          <button className="btn primary" disabled={busy} onClick={() => decide("confirmed")}>影響ありで確定</button>
          <button className="btn" disabled={busy} onClick={() => setExcluding(true)}>除外…</button>
          {c.decision && <button className="btn ghost" disabled={busy} onClick={() => decide("open")}>未判定に戻す</button>}
        </div>
      )}
      {err && <p className="warn small">{err}</p>}
    </div>
  );
}

/** where an edge came from: the parser, a rule pack, the DB dictionary, production
 * SQL, a design document, or a name-based guess */
const SRC_JA: Record<string, string> = {
  parser: "構文", catalog: "DB辞書", runtime: "実行時", doc: "設計書", heuristic: "推定",
};

function NeighborList({ title, items, run, onPick }: {
  title: string; items: NodeDetail["neighbors"]; run: RunState; onPick: (id: string, name: string) => void;
}) {
  const uniq = Array.from(new Map(items.map((i) => [i.id, i])).values()).slice(0, 30);
  return (
    <div>
      <span className="k">{title}</span>
      <ul className="list">
        {uniq.map((n) => {
          const inRun = !!run.candidates[n.id];
          return (
            <li key={n.id}>
              <button className="link" onClick={() => onPick(n.id, n.name)} title={inRun ? "グラフで表示" : "影響ありとして追加"}>
                {n.name}</button>
              <span className="mono dim small">{n.rel.toLowerCase()}{n.line ? ` L${n.line}` : ""}</span>
              {n.src && <span className={`prov ${n.src}`} title={`根拠: ${SRC_JA[n.src] || n.src}${n.via ? `（${n.via}）` : ""}・確度 ${(n.conf ?? 1).toFixed(2)}`}>
                {SRC_JA[n.src] || (n.src.startsWith("rule:") ? `規則 ${n.src.slice(5)}` : n.src)}{(n.conf ?? 1) < 0.95 ? ` ${(n.conf ?? 1).toFixed(2)}` : ""}</span>}
              {!inRun && <span className="tag violet">＋追加</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
