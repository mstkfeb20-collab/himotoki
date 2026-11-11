import { TX_LABEL } from "./ListView";
import { judgedSuffix } from "./ui";
import { useEffect, useRef, useState, Fragment } from "react";
import { api } from "./api";
import { statusOf } from "./Graph";
import type { Brief, Candidate, DataContext, DataUser, FileView, FlowContext, JobFlow, LinkGroup, NodeContext, NodeDetail, RunState } from "./types";
import { KindChip, KIND_SHORT, subsystem } from "./kinds";
import { AFFECTING, effectiveImpact, EVIDENCE_JA, IMPACT_HELP, IMPACT_JA, ImpactTag, type Impact } from "./impact";

const STATUS_LABEL: Record<string, string> = {
  confirmed: "確定", human: "人が追加", excluded: "除外", impacted: "影響あり", uncertain: "要確認",
  low: "影響低", pending: "未判定",
};
/** The verdict with how it was reached: rule-based judgement is never shown as AI. */
const statusText = (c: Candidate) => {
  const st = statusOf(c);
  return STATUS_LABEL[st] + (["impacted", "uncertain", "low"].includes(st) ? judgedSuffix(c) : "");
};
const ORIGIN_LABEL: Record<string, string> = {
  seed: "要求から直接", pattern: "変更パターン", graph: "依存の追跡", grep: "横断検索", shadow: "独立再探索",
  crud: "CRUD確認", human: "人が追加", focus: "再調査の起点", learned: "過去の実績から",
};
const LAYER_LABEL: Record<string, string> = { feature: "機能", code: "コード", data: "データ", test: "テスト", doc: "設計書" };


export default function Inspector({ run, ids, onSelect, onChanged, busy, wide = false, onWide = () => {}, onShowLinks }: {
  run: RunState;
  ids: string[];
  onSelect: (ids: string[], focus?: boolean) => void;
  onChanged: () => void;
  busy: boolean;
  wide?: boolean;
  onWide?: () => void;
  onShowLinks?: (id: string) => void;
}) {
  if (ids.length === 0) return <Empty run={run} onSelect={onSelect} />;
  if (ids.length > 1) return <Multi run={run} ids={ids} onChanged={onChanged} onSelect={onSelect} />;
  // an element outside the candidates (picked in the connection view) is shown the same way
  return <Detail key={ids[0]} run={run} rootId={ids[0]} onSelect={onSelect} onChanged={onChanged} busy={busy} wide={wide} onWide={onWide} onShowLinks={onShowLinks} />;
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
          <span className={`badge s-${statusOf(c)}`}>{statusText(c)}</span></li>)}
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

type Tab = "why" | "code" | "links" | "data" | "flow" | "doc" | "test" | "hist" | "comment";

const DATA_KINDS = new Set(["Table", "Column"]);
const FLOW_KINDS = new Set(["Job", "JobNet", "CronEntry", "Script", "BatchJob"]);

/** The detail of one element. It can move to related elements (back / forward) without
 *  changing the analysis; adding one to the candidates is a separate, explicit button. */
function Detail({ run, rootId, onSelect, onChanged, busy, wide, onWide, onShowLinks }: {
  run: RunState; rootId: string; onSelect: (ids: string[], f?: boolean) => void; onChanged: () => void; busy: boolean;
  wide: boolean; onWide: () => void; onShowLinks?: (id: string) => void;
}) {
  const [stack, setStack] = useState<string[]>([rootId]);
  const [pos, setPos] = useState(0);
  const cur = stack[pos];
  const go = (id: string) => { if (id === cur) return; setStack([...stack.slice(0, pos + 1), id]); setPos(pos + 1); };
  const c = run.candidates[cur];
  return (
    <div className="pane inspector">
      {(stack.length > 1) && (
        <nav className="dnav" aria-label="たどった履歴">
          <button className="icon sm" disabled={pos === 0} onClick={() => setPos(pos - 1)} aria-label="戻る" title="戻る">←</button>
          <button className="icon sm" disabled={pos >= stack.length - 1} onClick={() => setPos(pos + 1)} aria-label="進む" title="進む">→</button>
          <span className="dcrumb small">
            {stack.slice(0, pos + 1).map((id, i) => (
              <span key={id + i}>{i > 0 && <span className="dim"> › </span>}
                {i === pos ? <b>{short(run, id)}</b> : <button className="link" onClick={() => setPos(i)}>{short(run, id)}</button>}</span>
            ))}
          </span>
          {cur !== rootId && <button className="btn small ghost" onClick={() => setPos(0)}>選択中の候補に戻る</button>}
        </nav>
      )}
      <NodePane key={cur} run={run} id={cur} c={c} go={go} onSelect={onSelect} onChanged={onChanged} busy={busy}
        wide={wide} onWide={onWide} isRoot={cur === rootId} onShowLinks={onShowLinks} />
    </div>
  );
}

const short = (run: RunState, id: string) => {
  const n = run.candidates[id]?.name || id.replace(/^[a-z]+:/, "");
  const t = n.split(/[/#]/).pop() || n;
  return t.length > 28 ? t.slice(0, 27) + "…" : t;
};

function NodePane({ run, id, c, go, onSelect, onChanged, busy, wide, onWide, isRoot, onShowLinks }: {
  run: RunState; id: string; c?: Candidate; go: (id: string) => void; onSelect: (ids: string[], f?: boolean) => void;
  onChanged: () => void; busy: boolean; wide: boolean; onWide: () => void; isRoot: boolean; onShowLinks?: (id: string) => void;
}) {
  const [d, setD] = useState<NodeDetail | null>(null);
  const [ctx, setCtx] = useState<NodeContext | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [jump, setJump] = useState<{ file: string; line?: number | null } | null>(null);
  useEffect(() => {
    api.node(id, run.id).then(setD).catch((e) => setErr(e.message));
    api.context(id, run.id).then(setCtx).catch(() => setCtx({ links: [], data: null, flow: null }));
  }, [id, run.id]);
  const kind = c?.kind || d?.node.kind || "";
  const file = c?.file ?? d?.node.file ?? null;
  const line = c?.line ?? d?.node.line ?? null;
  const name = c?.name || d?.node.name || id;
  const hasData = !!ctx?.data;
  const hasFlow = !!ctx?.flow;
  const nLinks = ctx ? ctx.links.reduce((a, g) => a + g.total, 0) : 0;
  const defaultTab: Tab = DATA_KINDS.has(kind) && hasData ? "data" : FLOW_KINDS.has(kind) && hasFlow ? "flow" : file ? "code" : "links";
  const [tab, setTab] = useState<Tab | null>(null);
  const shown: Tab = tab ?? (ctx && d ? defaultTab : "why");
  const openAt = (f: string, l?: number | null) => { setJump({ file: f, line: l }); setTab("code"); };
  const st = c ? statusOf(c) : null;
  const conf = c ? (c.confidence ?? c.score) : null;
  const data = ctx?.data;
  const tabs: [Tab, string][] = [
    ...(c ? [["why", "根拠"]] as [Tab, string][] : []),
    ...(file ? [["code", "コード"]] as [Tab, string][] : []),
    ...(hasData ? [["data", DATA_KINDS.has(kind) ? "データ" : "触るテーブル"]] as [Tab, string][] : []),
    ...(hasFlow ? [["flow", "ジョブの流れ"]] as [Tab, string][] : []),
    ["links", `つながり${ctx ? ` ${nLinks}` : ""}`],
    ["doc", `設計書${d?.docs.length ? ` ${d.docs.length}` : ""}`],
    ["test", `テスト${d ? ` ${d.tests.length}` : ""}`],
    ["hist", "履歴"],
    ...(c ? [["comment", `コメント${c.comments.length ? ` ${c.comments.length}` : ""}`]] as [Tab, string][] : []),
  ];

  return (
    <>
      <div className="dhead">
        <div className="row between">
          <span className="row gap6">
            <KindChip kind={kind} file={file} />
            <span className="k">{KIND_SHORT[kind] || kind}{subsystem(file) ? ` · ${subsystem(file)}` : ""}</span>
          </span>
          <span className="row gap6">
            {c ? <span className={`badge s-${st}`}>{statusText(c)}</span> : <span className="badge out">候補外</span>}
            {onShowLinks && <button className="icon sm" onClick={() => onShowLinks(id)} aria-label="つながりを図で見る" title="ここからのつながりを図でたどる">⇄</button>}
            <button className="icon sm" onClick={onWide} aria-label={wide ? "詳細を狭める" : "詳細を広げる"}
              title={wide ? "詳細を狭める" : "詳細を広げてコードを読む"}>{wide ? "⇥" : "⇤"}</button>
          </span>
        </div>
        {data && data.type !== "touches" ? <DataTitle data={data} /> : <h2 className="mono title">{name}</h2>}
        {file && <button className="link mono dim small fileref" onClick={() => openAt(file, line)}>{file}{line ? `:${line}` : ""}</button>}
        {c && conf !== null && <div className="row conf"><div className="bar"><div style={{ width: `${conf * 100}%` }} className={`fill s-${st}`} /></div>
          <span className="mono small">確度 {conf.toFixed(2)}</span></div>}
        {c && <p className="reason">{c.reason || c.seed_reason}</p>}
        {c && <ImpactBox c={c} />}
        {!c && <p className="muted small">この要素は今回の候補に入っていません。つながりを確かめるために開いています（解析結果は変わりません）。</p>}
      </div>
      {c && (c.learned?.length || 0) > 0 && (
        <section className="txbox learnbox" aria-label="過去の判断・実績">
          <div className="k">過去の判断・実績（このシステムで学んだこと）</div>
          <ul>{c.learned!.map((n, i) => (
            <li key={i} className={n.factor > 1 ? "tx-medium" : ""}>{n.message}
              {n.factor !== 1 && <span className="mono dim small">（確度 ×{n.factor}）</span>}</li>))}</ul>
        </section>
      )}
      {c && (c.tx || (c.tx_risks?.length || 0) > 0) && (
        <section className="txbox" aria-label="トランザクション・連携上の注意">
          <div className="k">トランザクション・連携{c.tx ? ` — 境界 ${c.tx.boundary}（${c.tx.source}、${c.tx.members}要素）` : ""}</div>
          {c.tx && c.tx.tables.length > 0 && <div className="small dim">更新・参照テーブル: <span className="mono">{c.tx.tables.join(", ")}</span></div>}
          <ul>
            {(c.tx_risks || []).map((r, i) => (
              <li key={i} className={`tx-${r.level}`}>
                <span className="txcode">{TX_LABEL[r.code] || r.code}</span> {r.message}
                {r.at !== c.node_id && <> — <button className="link" onClick={() => go(r.at)}>{run.candidates[r.at]?.name || r.at.replace(/^[a-z]+:/, "").split(/[.#]/).slice(-2).join(".")}</button></>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <div className="tabs" role="tablist">
        {tabs.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={shown === k} className={shown === k ? "tab on" : "tab"} onClick={() => setTab(k)}>{label}</button>
        ))}
      </div>

      <div className={`tabbody dbody t-${shown}`}>
        {err && <p className="warn small">{err}</p>}
        {shown === "why" && c && (
          <div className="stack">
            <ol className="path mono">
              <li><span className="dim">00</span> 要求</li>
              {c.intent_ids[0] && <li><span className="dim">01</span> {c.intent_ids[0]} {run.intents.find((i) => i.id === c.intent_ids[0])?.summary.slice(0, 40)}</li>}
              {c.path.map((p, i) => (
                <li key={p}><span className="dim">{String(i + 2).padStart(2, "0")}</span>{" "}
                  <button className="link" onClick={() => go(p)}>{run.candidates[p]?.name || p}</button></li>
              ))}
            </ol>
            <dl className="meta">
              <dt>見つけ方</dt><dd>{ORIGIN_LABEL[c.origin] || c.origin}</dd>
              <dt>判定</dt><dd>{c.verdict ?? "—"}（{run.llm}）</dd>
              {c.decision && <><dt>人の判断</dt><dd>{c.decision.status === "confirmed" ? "確定" : "除外"} — {c.decision.actor}{c.decision.reason ? `「${c.decision.reason}」` : ""}{c.decision.inherited ? "（前回runから引継ぎ）" : ""}</dd></>}
            </dl>
          </div>
        )}
        {shown === "code" && file && (
          <FilePane run={run} file={jump?.file || file} focus={jump?.file && jump.file !== file ? jump.line ?? null : (jump?.line ?? line)}
            range={!jump || jump.file === file ? (d?.node.line ? [d.node.line, d.node.end_line || d.node.line] : null) : null}
            hits={!jump || jump.file === file ? (d?.highlight || []) : []} excerpt={d?.code || []}
            onBack={jump && jump.file !== file ? () => setJump(null) : undefined} onOpen={(eid) => go(eid)} />
        )}
        {shown === "data" && data && <DataPane data={data} run={run} go={go} openAt={openAt} />}
        {shown === "flow" && ctx?.flow && <FlowPane flow={ctx.flow} focus={id} run={run} go={go} openAt={openAt} />}
        {shown === "links" && (ctx ? <LinksPane groups={ctx.links} run={run} go={go} onChanged={onChanged} from={name} />
          : <p className="muted small">読み込み中…</p>)}
        {shown === "doc" && (
          <div className="stack">
            {d?.docs.length ? d.docs.map((doc) => (
              <div key={doc.id} className="doc"><button className="link mono dim small" onClick={() => go(doc.id)}>{doc.file}:{doc.line}</button>
                <pre className="doctext">{doc.text}</pre></div>
            )) : <p className="muted small">この要素に触れている設計書は見つかっていません。</p>}
          </div>
        )}
        {shown === "test" && (
          <div className="stack">
            {d?.tests.length ? <ul className="list">{d.tests.map((t) => <li key={t.id}><button className="link" onClick={() => go(t.id)}>{t.name}</button></li>)}</ul>
              : <p className="warn small">この要素を検証するテストが見つかりません。修正時はテスト追加が必要です。</p>}
          </div>
        )}
        {shown === "hist" && (
          <ul className="list">
            {d?.history.length ? d.history.map((h) => (
              <li key={h.hash}><span className="mono dim small">{h.date} {h.hash}</span> {h.subject}</li>
            )) : <li className="muted small">変更履歴はありません。</li>}
          </ul>
        )}
        {shown === "comment" && c && <Comments run={run} c={c} onChanged={onChanged} />}
      </div>

      {c ? <Decide run={run} c={c} busy={busy} onChanged={onChanged} onSelect={isRoot ? undefined : () => onSelect([c.node_id], true)} />
        : <AddBar run={run} id={id} name={name} onChanged={onChanged} />}
    </>
  );
}

/** schema › table (logical name) › column, the way a DB person names it */
function DataTitle({ data }: { data: Exclude<DataContext, { type: "touches" }> }) {
  const t = data.table;
  const col = data.type === "column" ? data.column : null;
  const type = col ? Object.values(col.types)[0] : null;
  return (
    <div className="dtitle">
      <div className="dcrumb2 small">
        <span className={data.schema ? "" : "dim"} title={data.schema ? "DDL に書かれたスキーマ" : "DDL にスキーマ名がありません"}>{data.schema || "スキーマ不明"}</span>
        <span className="dim"> › </span>
        {t ? <span>{t.name}{t.logical && <span className="muted">（{t.logical}）</span>}</span> : <span className="dim">テーブル不明</span>}
      </div>
      <h2 className="mono title">{col ? col.name : t?.name}{col?.logical && <span className="sans muted lg">（{col.logical}）</span>}
        {type && <span className="tag mono">{type}</span>}</h2>
    </div>
  );
}

function OpsBadge({ ops }: { ops?: string | null }) {
  if (!ops) return null;
  return <span className="ops">{"CRUD".split("").map((o) => <i key={o} className={ops.includes(o) ? `on o-${o}` : ""}>{o}</i>)}</span>;
}

function Row({ b, run, go, children, label, sub }: { b: Brief; run: RunState; go: (id: string) => void; children?: React.ReactNode; label?: string; sub?: string }) {
  const inRun = run.candidates[b.id];
  return (
    <li className="brow">
      <KindChip kind={b.kind} file={b.file} compact />
      <div className="bmain">
        <div className="b1">
          <button className="link bname" title={b.name} onClick={() => go(b.id)}>{label || b.name}</button>
          {inRun && <span className={`dot s-${statusOf(inRun)}`} title={`候補: ${statusText(inRun)}`} />}
          {children}
        </div>
        {sub && <div className="mono dim tiny bfile" title={b.name}>{sub}</div>}
        {b.file && <div className="mono dim tiny bfile" title={b.file}>{shortPath(b.file)}{(b.at || b.line) ? `:${b.at || b.line}` : ""}</div>}
      </div>
    </li>
  );
}

function DataPane({ data, run, go, openAt }: { data: DataContext; run: RunState; go: (id: string) => void; openAt: (f: string, l?: number | null) => void }) {
  if (data.type === "touches") {
    return (
      <div className="stack">
        <p className="muted small">この処理が SQL を通して読み書きしているテーブル・ファイルです。</p>
        <table className="dtable">
          <thead><tr><th>テーブル</th><th>操作</th><th>列</th></tr></thead>
          <tbody>{data.tables.map((t) => (
            <tr key={t.id}><td><button className="link mono" onClick={() => go(t.id)}>{t.schema ? `${t.schema}.` : ""}{t.name}</button>
              {t.logical && <div className="muted tiny">{t.logical}</div>}</td>
              <td><OpsBadge ops={t.ops} /></td><td className="mono small">{t.columns.join(", ") || <span className="dim">（列の指定なし）</span>}</td></tr>
          ))}</tbody>
        </table>
      </div>
    );
  }
  const t = data.table;
  const defs = data.type === "column" ? data.column.definitions : t?.definitions || [];
  return (
    <div className="stack">
      <section>
        <h3 className="dh">定義</h3>
        {defs.length ? <ul className="list">{defs.map((x) => (
          <li key={x.file + x.line}><button className="link mono small" onClick={() => openAt(x.file, x.line)}>{x.file}:{x.line}</button>
            {x.type && <span className="tag mono">{x.type}</span>}</li>))}</ul>
          : <p className="muted small">DDL が見つかっていません（DB 辞書の取り込みで補えます）。</p>}
      </section>
      {data.type === "column" && (data.column.lengths?.facts.length || 0) > 1 && (
        <section>
          <h3 className="dh">桁の流れ {data.column.lengths!.mismatch && <span className="tag warn" title="DDL と異なる桁があります">桁の食い違いあり</span>}</h3>
          <p className="muted tiny">この値の桁を決めている場所です。DDL と違う桁は ⚠ で示します（切り捨て・桁あふれの原因になりやすい）。</p>
          <table className="dtable">
            <thead><tr><th>場所</th><th>桁</th><th>要素</th></tr></thead>
            <tbody>{data.column.lengths!.facts.map((f) => (
              <tr key={f.id + f.where} className={f.differs ? "differs" : ""}>
                <td className="small">{f.where}</td>
                <td className="mono">{f.differs ? "⚠ " : ""}{f.length}</td>
                <td><button className="link small" onClick={() => go(f.id)}>{f.name}</button>
                  {f.file && <div className="mono dim tiny"><button className="link" onClick={() => openAt(f.file!, f.line)}>{shortPath(f.file)}{f.line ? `:${f.line}` : ""}</button></div>}</td>
              </tr>))}</tbody>
          </table>
        </section>
      )}
      <section>
        <h3 className="dh">{data.type === "column" ? "この列を読み書きしている処理" : "このテーブルを読み書きしている処理"} <span className="dim">{data.users.length}</span></h3>
        <Users users={data.users} run={run} go={go} />
      </section>
      {data.type === "column" && data.table_users.length > 0 && (
        <details>
          <summary className="small">列を指定せずにテーブルを読み書きしている処理（SELECT *・列名なしの INSERT など） {data.table_users.length}</summary>
          <Users users={data.table_users} run={run} go={go} />
        </details>
      )}
      {data.docs.length > 0 && (
        <section>
          <h3 className="dh">設計書 <span className="dim">{data.docs.length}</span></h3>
          <ul className="list">{data.docs.map((b) => <Row key={b.id} b={b} run={run} go={go}><OpsBadge ops={b.ops} /><span className="muted tiny">{docName(b)}</span></Row>)}</ul>
        </section>
      )}
      {t && t.columns.length > 0 && (
        <details open={data.type === "table"}>
          <summary className="small">{t.name} の列 {t.columns.length}</summary>
          <table className="dtable">
            <thead><tr><th>列</th><th>型</th><th>論理名</th></tr></thead>
            <tbody>{t.columns.map((col) => (
              <tr key={col.id} className={col.focus ? "focus" : ""}><td><button className="link mono" onClick={() => go(col.id)}>{col.name}</button></td>
                <td className="mono small">{col.type || ""}</td><td className="small">{col.comment || ""}</td></tr>
            ))}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}

const shortPath = (f: string) => { const p = f.split("/"); return p.length > 4 ? "…/" + p.slice(-3).join("/") : f; };
const sqlVerb = (n: string) => (n.replace(/^SQL\s+/, "").match(/^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|WITH)/i)?.[1].toUpperCase() || "SQL");
const docName = (b: Brief) => (b.file || "").split("/").pop()?.replace(/#.*/, "") || "";

function Users({ users, run, go }: { users: DataUser[]; run: RunState; go: (id: string) => void }) {
  if (!users.length) return <p className="muted small">見つかっていません。</p>;
  return (
    <ul className="list">{users.map((u) => (
      <Row key={u.id} b={u} run={run} go={go} label={u.owner ? `${u.owner.name} の ${sqlVerb(u.name)}` : undefined}
        sub={u.owner ? u.name.replace(/^SQL\s+/, "") : undefined}>
        <OpsBadge ops={u.ops} />
        {u.via.length > 0 && <span className="dim tiny">{u.via.join(", ")}</span>}
      </Row>))}
    </ul>
  );
}

function FlowPane({ flow, focus, run, go, openAt }: { flow: FlowContext; focus: string; run: RunState; go: (id: string) => void; openAt: (f: string, l?: number | null) => void }) {
  if (flow.type === "jobnet") {
    return (
      <div className="stack">
        {flow.parent && <p className="small">上位: <button className="link" onClick={() => go(flow.parent!.id)}>{flow.parent.name}</button></p>}
        <NetJobs jobs={flow.jobs} focus={focus} go={go} />
      </div>
    );
  }
  if (!flow.flows.length) return <p className="muted small">この要素を実行するジョブは見つかっていません。</p>;
  return (
    <div className="stack">
      {flow.flows.map((f) => (
        <div key={f.job.id} className="flow">
          {f.jobnet && <p className="small"><span className="k">ジョブネット</span> <button className="link mono" onClick={() => go(f.jobnet!.id)}>{f.jobnet.name}</button>
            {f.jobnet.schedule && <span className="muted"> {f.jobnet.schedule}</span>}</p>}
          {f.net_jobs.length > 1 && <NetJobs jobs={f.net_jobs} focus={f.job.id} go={go} />}
          <div className="ba small">
            <span className="k">先行</span> {f.before.length ? f.before.map((b) => <button key={b.id} className="link mono" onClick={() => go(b.id)}>{b.name.split("/").pop()}</button>) : <span className="dim">なし</span>}
            <span className="k"> 後続</span> {f.after.length ? f.after.map((b) => <button key={b.id} className="link mono" onClick={() => go(b.id)}>{b.name.split("/").pop()}</button>) : <span className="dim">なし</span>}
          </div>
          <h3 className="dh">実行の流れ</h3>
          <ol className="chain">
            <li className={f.job.id === focus ? "me" : ""} style={{ paddingLeft: 0 }}>
              <KindChip kind={f.job.kind} compact /> <button className="link mono" onClick={() => go(f.job.id)}>{f.job.name.split("/").pop()}</button>
              {f.job.comment && <span className="muted small"> {f.job.comment}</span>}
              {f.job.file && <button className="link mono dim tiny" onClick={() => openAt(f.job.file!, f.job.line)}> {f.job.file.split("/").pop()}:{f.job.line}</button>}
            </li>
            {f.chain.map((s) => (
              <li key={s.id} className={`${s.id === focus ? "me" : ""}${s.common ? " common" : ""}`} style={{ paddingLeft: s.depth * 18 }}>
                <span className="dim">└ </span><KindChip kind={s.kind} file={s.file} compact /> <button className="link mono" onClick={() => go(s.id)}>{s.name}</button>
                {s.common && <span className="dim tiny"> 共通（中は省略）</span>}
                {s.at && <span className="dim tiny"> 呼び出し行 {s.at}</span>}
                {s.conf && s.conf < 0.8 && <span className="dim tiny" title="名前からの推定を含むつながり"> 推定 {s.conf}</span>}
              </li>
            ))}
          </ol>
          {f.data.length > 0 && <>
            <h3 className="dh">読み書きするテーブル・ファイル</h3>
            <table className="dtable"><thead><tr><th>対象</th><th>操作</th><th>処理</th></tr></thead>
              <tbody>{f.data.map((x) => (
                <tr key={x.id}><td><KindChip kind={x.kind} compact /> <button className="link mono" onClick={() => go(x.id)}>{x.name}</button>
                  {x.logical && <div className="muted tiny">{x.logical}</div>}</td><td><OpsBadge ops={x.ops} /></td><td className="small">{x.by.join("、")}</td></tr>
              ))}</tbody></table>
          </>}
          {f.downstream.length > 0 && <>
            <h3 className="dh">書いたファイルを次に読むもの</h3>
            <ul className="list">{f.downstream.map((x) => (
              <li key={x.file.id} className="stack0"><span><KindChip kind="DataFile" compact /> <button className="link mono" onClick={() => go(x.file.id)}>{x.file.name}</button></span>
                <ul className="list sub">{x.readers.map((r) => <li key={r.id}>→ <button className="link mono" onClick={() => go(r.id)}>{r.name}</button>
                  {r.jobs.length > 0 && <span className="muted tiny"> ジョブ {r.jobs.join("、")}</span>}</li>)}</ul></li>
            ))}</ul>
          </>}
        </div>
      ))}
      {flow.more_jobs > 0 && <p className="muted small">ほかに {flow.more_jobs} 個のジョブからも実行されます。</p>}
    </div>
  );
}

function NetJobs({ jobs, focus, go }: { jobs: JobFlow["net_jobs"]; focus: string; go: (id: string) => void }) {
  return (
    <ol className="netjobs" aria-label="ジョブネットの中の順序">
      {jobs.map((j, i) => (
        <li key={j.id} className={j.id === focus ? "me" : ""}>
          {i > 0 && <span className="dim">→</span>}
          <button className="link mono" title={j.comment || j.full} onClick={() => go(j.id)}>{j.name}</button>
        </li>
      ))}
    </ol>
  );
}

function LinksPane({ groups, run, go, onChanged, from }: { groups: LinkGroup[]; run: RunState; go: (id: string) => void; onChanged: () => void; from: string }) {
  if (!groups.length) return <p className="muted small">つながりは見つかっていません。</p>;
  const add = async (b: Brief) => {
    await api.add(run.id, { node_id: b.id, reason: `${from} のつながりから追加（${b.name}）` });
    onChanged();
  };
  return (
    <div className="stack">
      <p className="muted tiny">名前をクリックすると中身を開きます（解析結果は変わりません）。候補にない要素は「＋候補」で追加できます。</p>
      {groups.map((g) => (
        <section key={g.label}>
          <h3 className="dh">{g.label} <span className="dim">{g.total}{g.total > g.items.length ? `（先頭 ${g.items.length}）` : ""}</span></h3>
          <ul className="list">{g.items.map((b) => (
            <Row key={b.id} b={b} run={run} go={go} label={b.owner ? `${b.owner.name} の ${sqlVerb(b.name)}` : undefined}
              sub={b.owner ? b.name.replace(/^SQL\s+/, "") : undefined}>
              <OpsBadge ops={b.ops} />
              {b.src && b.src !== "parser" && <span className="dim tiny" title="つながりの根拠">{b.src.replace(/^rule:/, "規則 ")}{b.conf ? ` ${b.conf}` : ""}</span>}
              {!run.candidates[b.id] && <button className="btn tiny ghost addb" onClick={() => add(b)} title="影響候補に加える">＋候補</button>}
            </Row>))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** the whole file, scrolled to the element; outline to jump within the file */
function FilePane({ run, file, focus, range, hits, excerpt, onBack, onOpen }: {
  run: RunState; file: string; focus: number | null; range: number[] | null; hits: number[];
  excerpt: { n: number; text: string }[]; onBack?: () => void; onOpen: (id: string) => void;
}) {
  const [fv, setFv] = useState<FileView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const pre = useRef<HTMLPreElement>(null);
  useEffect(() => { setFv(null); api.file(file, run.id).then(setFv).catch((e) => setErr(e.message)); }, [file, run.id]);
  const target = focus || range?.[0] || hits[0] || null;
  useEffect(() => {
    const el = pre.current?.querySelector(`[data-n="${target}"]`) as HTMLElement | null;
    if (pre.current && el) pre.current.scrollTop = Math.max(0, el.offsetTop - pre.current.clientHeight / 4);
  }, [fv, target]);
  const hl = new Set(hits);
  const lines = fv && !fv.office ? fv.lines : excerpt;
  const find = (dir: 1 | -1) => {
    if (!q || !pre.current) return;
    const all = Array.from(pre.current.querySelectorAll(".ln")) as HTMLElement[];
    const top = pre.current.scrollTop + pre.current.clientHeight / 4 + (dir > 0 ? 2 : -2);
    const hitsQ = all.filter((el) => el.textContent?.toLowerCase().includes(q.toLowerCase()));
    const next = dir > 0 ? hitsQ.find((el) => el.offsetTop > top) || hitsQ[0] : [...hitsQ].reverse().find((el) => el.offsetTop < top) || hitsQ[hitsQ.length - 1];
    if (next) { pre.current.scrollTop = Math.max(0, next.offsetTop - pre.current.clientHeight / 4); next.classList.add("flash"); setTimeout(() => next.classList.remove("flash"), 900); }
  };
  return (
    <div className="filepane">
      <div className="row fbar">
        {onBack && <button className="btn small ghost" onClick={onBack}>← この要素のファイルへ</button>}
        <span className="mono small grow ellip" title={file}>{file}</span>
        {fv && !fv.office && <span className="dim tiny">{fv.total ?? fv.lines.length} 行{fv.truncated ? "（先頭のみ）" : ""}</span>}
      </div>
      {fv && fv.outline.length > 1 && !fv.office && (
        <div className="row fbar">
          <select className="input small" aria-label="ファイル内の要素へ移動" value=""
            onChange={(e) => { const el = pre.current?.querySelector(`[data-n="${e.target.value}"]`) as HTMLElement | null; if (el && pre.current) pre.current.scrollTop = el.offsetTop - 24; }}>
            <option value="">ファイル内の要素へ移動（{fv.outline.length}）</option>
            {fv.outline.map((o) => <option key={o.id} value={o.line}>{o.line}: {o.name}</option>)}
          </select>
          <input className="input small" placeholder="ファイル内を検索" value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") find(e.shiftKey ? -1 : 1); }} aria-label="ファイル内を検索" />
        </div>
      )}
      {err && <p className="warn small">{err}</p>}
      {fv?.office && <p className="muted small">Excel・Word は抜き出した内容を表示しています。原本は設計書の取込確認で確かめられます。</p>}
      {lines.length > 0 ? (
        <pre className="code full" ref={pre}>{lines.map((l) => {
          const inNode = range && l.n >= range[0] && l.n <= range[1];
          return <div key={l.n} data-n={l.n} className={hl.has(l.n) || (fv?.office && l.n === focus) ? "ln hit" : inNode ? "ln in" : "ln"}><span className="no">{l.n}</span>{l.text}</div>;
        })}</pre>
      ) : !fv && !err ? <p className="muted small">読み込み中…</p> : <p className="muted small">表示できる内容がありません。</p>}
      {fv && fv.outline.length > 0 && fv.office && (
        <ul className="list">{fv.outline.slice(0, 50).map((o) => <li key={o.id}><button className="link small" onClick={() => onOpen(o.id)}>{o.name}</button></li>)}</ul>
      )}
    </div>
  );
}

function Comments({ run, c, onChanged }: { run: RunState; c: Candidate; onChanged: () => void }) {
  const [comment, setComment] = useState("");
  return (
    <div className="stack">
      {c.comments.map((m, i) => <div key={i} className="comment"><b>{m.author}</b> {m.text}</div>)}
      <label className="k" htmlFor="assignee">確認担当</label>
      <input id="assignee" className="input" defaultValue={c.assignee || ""} placeholder="例: 請求チーム 佐藤"
        onBlur={(e) => { if ((e.target.value || null) !== (c.assignee || null)) api.comment(run.id, c.node_id, "", e.target.value).then(onChanged); }} />
      <label className="k" htmlFor="cm">コメント</label>
      <textarea id="cm" className="input" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
      <button className="btn" disabled={!comment.trim()} onClick={async () => { await api.comment(run.id, c.node_id, comment); setComment(""); onChanged(); }}>コメントを追加</button>
    </div>
  );
}

function Decide({ run, c, busy, onChanged, onSelect }: { run: RunState; c: Candidate; busy: boolean; onChanged: () => void; onSelect?: () => void }) {
  const [excluding, setExcluding] = useState(false);
  const [reason, setReason] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const decide = async (status: string, extra: Record<string, unknown> = {}) => {
    try {
      setErr(null);
      await api.decide(run.id, c.node_id, status, status === "excluded" ? reason : (reason || ""), extra);
      setExcluding(false); setReason("");
      onChanged();
    } catch (e: any) { setErr(e.message); }
  };
  return (
    <div className="dfoot">
      {c.decision?.status === "confirmed" && (
        <div className="row gap6 small">
          <span className="k">確定の種類</span>
          <div className="modes" role="radiogroup" aria-label="確定の種類">
            {AFFECTING.map((k) => (
              <button key={k} type="button" role="radio" aria-checked={effectiveImpact(c) === k} title={IMPACT_HELP[k]}
                className={effectiveImpact(c) === k ? "mode on" : "mode"} disabled={busy}
                onClick={() => decide("keep", { impact: k })}>{IMPACT_JA[k]}</button>
            ))}
          </div>
        </div>
      )}
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
          {!c.decision && AFFECTING.map((k) => {
            const suggested = (c.impact && AFFECTING.includes(c.impact as Impact) ? c.impact : "change") === k;
            return (
              <button key={k} className={suggested ? "btn primary" : "btn"} disabled={busy} title={IMPACT_HELP[k]}
                onClick={() => decide("confirmed", { impact: k })}>{IMPACT_JA[k]}で確定</button>
            );
          })}
          <button className="btn" disabled={busy} onClick={() => setExcluding(true)}>除外…</button>
          {c.decision && <button className="btn ghost" disabled={busy} onClick={() => decide("open")}>未判定に戻す</button>}
          {onSelect && <button className="btn ghost" onClick={onSelect}>一覧でこの候補を選ぶ</button>}
        </div>
      )}
      {err && <p className="warn small">{err}</p>}
    </div>
  );
}

function AddBar({ run, id, name, onChanged }: { run: RunState; id: string; name: string; onChanged: () => void }) {
  const [reason, setReason] = useState("");
  const [done, setDone] = useState(false);
  return (
    <div className="dfoot">
      <label className="k" htmlFor="add-reason">候補に加える理由（任意）</label>
      <div className="row">
        <input id="add-reason" className="input" value={reason} onChange={(e) => setReason(e.target.value)} placeholder={`例: ${name} も同じ区分値を見ている`} />
        <button className="btn" disabled={done} onClick={async () => {
          await api.add(run.id, { node_id: id, reason: reason.trim() || `確認中に追加（${name}）` });
          setDone(true); onChanged();
        }}>{done ? "追加しました" : "候補に追加"}</button>
      </div>
    </div>
  );
}

/** The kind of impact and its evidence (what to edit / what to test / whom to agree with). */
function ImpactBox({ c }: { c: Candidate }) {
  const k = effectiveImpact(c);
  if (!k) return null;
  const by = c.decision?.status === "confirmed" ? "人の確定" : c.judged_by === "ai" || c.judged_by === "reused" ? "AI の判定" : "規則による推定";
  const ev = Object.entries(c.evidence || {}).filter(([key, v]) => v && key !== "basis");
  return (
    <div className="impactbox">
      <div className="row gap6"><ImpactTag kind={k} /><span className="muted small">{IMPACT_HELP[k]}（{by}）</span></div>
      {ev.length > 0 && (
        <dl className="meta">
          {ev.map(([key, v]) => <Fragment key={key}><dt>{EVIDENCE_JA[key] || key}</dt><dd>{v}</dd></Fragment>)}
        </dl>
      )}
    </div>
  );
}
