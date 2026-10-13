import { useEffect, useState } from "react";
import { api } from "./api";
import type { DocCheck, DocRow, DocSheet, DocSummary, DocTarget } from "./types";

/** How a header label was read (rules/office.yaml), in the report's words. */
const SEM_JA: Record<string, string> = {
  table: "テーブル", column: "項目（物理名）", logical: "論理名", type: "型", misc: "補足", screen: "画面",
  function: "機能", job: "ジョブ", program: "プログラム", file: "ファイル", message: "メッセージ", url: "URL",
};

/** 設計書の取込確認: each indexed design document read again by the extractor and laid
 *  next to what it contains — tables and how their headers were understood, content
 *  outside every table, drawings a parser cannot read, and links into the graph. */
export default function DocsCheckDialog({ graph, onClose, onSelect }: {
  graph?: string | null; onClose: () => void; onSelect?: (nodeId: string) => void;
}) {
  const [docs, setDocs] = useState<DocSummary[] | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const [rep, setRep] = useState<DocCheck | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.docs(graph).then((d) => {
      // the documents folder first, then documents found in the repositories
      d = [...d].sort((a, b) => Number(!a.path.startsWith("design:")) - Number(!b.path.startsWith("design:")) || a.path.localeCompare(b.path));
      setDocs(d);
      const office = d.find((x) => /\.(xlsx|xlsm|docx)$/i.test(x.path));
      if (d.length) setSel((office || d[0]).path);
    }).catch((e) => setErr(e.message));
  }, [graph]);
  useEffect(() => {
    if (!sel) return;
    setRep(null); setErr(null);
    api.docCheck(sel, graph).then(setRep).catch((e) => setErr(e.message));
  }, [sel, graph]);
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", k); return () => window.removeEventListener("keydown", k);
  }, [onClose]);

  return (
    <div className="modal-bg" role="dialog" aria-modal="true" aria-label="設計書の取込確認">
      <div className="modal pane docs-check">
        <div className="row between">
          <h2>設計書の取込確認</h2>
          <button className="icon" aria-label="閉じる" onClick={onClose}>×</button>
        </div>
        <p className="muted small">取り込んだ設計書をもう一度読み、表として読めたもの・読めなかったもの・コードとの結びつきを並べます（AI は使いません）。原本と見比べて、抜けがないかを確認してください。</p>
        <div className="dc-body">
          <nav className="dc-list" aria-label="設計書の一覧">
            {!docs && !err && <p className="muted small">読み込み中…</p>}
            {docs && docs.length === 0 && <p className="muted small">設計書（Excel・Word・Markdown）は取り込まれていません。ワークスペースの docs/ に置いて取り込み直してください。</p>}
            {docs?.map((d) => (
              <button key={d.path} className={sel === d.path ? "dc-doc on" : "dc-doc"} onClick={() => setSel(d.path)}>
                <span className="dc-name">{d.path.replace(/^design:/, "")}</span>
                <span className="dc-src">{d.path.startsWith("design:") ? "設計書フォルダ" : "リポジトリ内"}</span>
                <span className="muted small">行 {d.rows}・節 {d.sections}・結びつき {d.linked}</span>
              </button>
            ))}
          </nav>
          <div className="dc-detail">
            {err && <p className="warn small">{err}</p>}
            {sel && !rep && !err && <p className="muted small">読み直しています…</p>}
            {rep && <Report rep={rep} onSelect={onSelect} />}
          </div>
        </div>
      </div>
    </div>
  );
}

function Report({ rep, onSelect }: { rep: DocCheck; onSelect?: (id: string) => void }) {
  const s = rep.summary;
  const pct = s.cells ? Math.round((100 * s.read_cells) / s.cells) : null;
  return (
    <div className="stack">
      <div className="dc-sum">
        <Stat label="表から読んだ行" value={s.rows_read} />
        <Stat label="うち結びついた行" value={s.graph ? s.linked_rows : "—"} />
        <Stat label="節" value={s.sections} />
        {rep.format === "xlsx" && <Stat label="表として読んだセル" value={pct === null ? "—" : `${pct}%`} sub={`${s.read_cells} / ${s.cells}`} />}
        <Stat label="読めない図形・画像" value={s.unread_drawings} warn={s.unread_drawings > 0} />
      </div>
      <section>
        <h3 className="dc-h">要確認</h3>
        {rep.findings.length === 0 ? <p className="small cyan-t">読み残しは見つかりませんでした。</p> : (
          <ul className="dc-findings">
            {rep.findings.map((f, i) => <li key={i} className={`dc-f lv-${f.level}`}><span className="dc-badge">{f.level === "info" ? "参考" : f.level === "warn" ? "要確認" : "エラー"}</span>{f.message}</li>)}
          </ul>
        )}
      </section>
      {rep.sheets?.map((sh) => <Sheet key={sh.name} sh={sh} graph={s.graph} onSelect={onSelect} />)}
      {rep.tables && rep.tables.length > 0 && rep.tables.map((t) => (
        <section key={t.name}>
          <h3 className="dc-h">{t.name} <span className="muted small">{t.rows} 行・読んだ行 {t.read_rows}{t.recognised ? "" : "（見出しを認識できず）"}</span></h3>
          <Header cols={t.header} />
          {t.sample.length > 0 && <Rows rows={t.sample} graph={s.graph} onSelect={onSelect} />}
        </section>
      ))}
      {rep.sections && rep.sections.filter((x) => x.chars || x.targets.length).length > 0 && (
        <section>
          <h3 className="dc-h">節</h3>
          <table className="dc-table"><thead><tr><th>節</th><th>文字数</th><th>識別子</th><th>結びつき</th></tr></thead>
            <tbody>{rep.sections.filter((x) => x.chars || x.targets.length).map((x) => (
              <tr key={x.id}><td>{x.title}</td><td className="num">{x.chars}{x.cut ? "（途中まで）" : ""}</td><td className="num">{x.mentions}</td>
                <td><Targets ts={x.targets} graph={s.graph} onSelect={onSelect} /></td></tr>
            ))}</tbody></table>
        </section>
      )}
      {rep.textboxes && rep.textboxes.length > 0 && (
        <details><summary className="small">テキストボックスの文字（読んでいない）{rep.textboxes.length} 個</summary>
          <ul className="small">{rep.textboxes.map((t, i) => <li key={i}>{t}</li>)}</ul></details>
      )}
    </div>
  );
}

function Stat({ label, value, sub, warn }: { label: string; value: string | number; sub?: string; warn?: boolean }) {
  return <div className="dc-stat"><span className="muted small">{label}</span><b className={warn ? "amber-t" : ""}>{value}</b>{sub && <span className="muted small">{sub}</span>}</div>;
}

function Sheet({ sh, graph, onSelect }: { sh: DocSheet; graph: boolean; onSelect?: (id: string) => void }) {
  const drawn = sh.shapes + sh.pictures + sh.charts + sh.connectors;
  return (
    <section>
      <h3 className="dc-h">シート「{sh.name}」 <span className="muted small">{sh.rows} 行・文字のあるセル {sh.cells}・うち表として読んだ {sh.read_cells}{sh.merged ? `・結合 ${sh.merged}` : ""}</span></h3>
      {sh.tables.length === 0 && sh.crud_rows === 0 && sh.cells > 0 && (
        <p className="small amber-t">表として読めていません。{sh.header_candidates.length > 0 && <>見出しらしい行: {sh.header_candidates.map((c) => `${c.row}行目（${c.labels.slice(0, 6).join("・")}）`).join(" / ")}</>}</p>
      )}
      {sh.tables.map((t) => (
        <div key={t.header_row} className="stack">
          <p className="small">{t.header_row} 行目を見出しとする表: <b>{t.rows}</b> 行{graph ? `（結びついた行 ${t.linked}）` : ""}</p>
          <Header cols={t.columns} />
          <Rows rows={t.sample} graph={graph} onSelect={onSelect} />
        </div>
      ))}
      {sh.crud_rows > 0 && <p className="small">CRUD 表: {sh.crud_rows} 行（テーブル {sh.crud_tables.slice(0, 12).join("・")}）</p>}
      {drawn > 0 && (
        <p className="small amber-t">読めないもの: 図形 {sh.shapes}・画像 {sh.pictures}・グラフ {sh.charts}・線 {sh.connectors}</p>
      )}
      {sh.shape_texts.length > 0 && (
        <details><summary className="small">図形の中の文字（読んでいない）{sh.shape_texts.length} 個</summary>
          <ul className="small">{sh.shape_texts.map((t, i) => <li key={i}>{t}</li>)}</ul></details>
      )}
      {sh.uncovered.length > 0 && (
        <details><summary className="small">表の外にある文字（検索の対象外）{sh.uncovered_cells} セル</summary>
          <table className="dc-table"><tbody>{sh.uncovered.map((u) => (
            <tr key={u.row}><td className="muted num">{u.row}</td>
              <td>{u.cells.map(([a, t]) => <span key={a} className="dc-cell"><span className="muted">{a}</span> {t}</span>)}</td></tr>
          ))}</tbody></table>
        </details>
      )}
      {sh.comments > 0 && <p className="muted small">セルのコメント {sh.comments} 件（読んでいない）</p>}
    </section>
  );
}

function Header({ cols }: { cols: { label: string; as: string | null; as_ja?: string | null }[] }) {
  return (
    <div className="dc-header">
      {cols.map((c, i) => (
        <span key={i} className={c.as ? "dc-col" : "dc-col miss"} title={c.as ? `${c.as_ja || c.as} として読む` : "読まない列（rules/office.yaml の headers に足すと読めます）"}>
          <b>{c.label}</b><span className="small">{c.as ? c.as_ja || c.as : "読まない"}</span>
        </span>
      ))}
    </div>
  );
}

function Rows({ rows, graph, onSelect }: { rows: DocRow[]; graph: boolean; onSelect?: (id: string) => void }) {
  return (
    <table className="dc-table">
      <thead><tr><th>セル</th><th>読んだ名前</th><th>読んだ値</th><th>結びつき</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.id}>
          <td className="muted mono nw">{r.cell.split("!").pop()}</td>
          <td>{r.name}</td>
          <td className="small">{Object.entries(r.fields).map(([k, v]) => `${SEM_JA[k] || k}: ${v}`).join(" / ")}</td>
          <td><Targets ts={r.targets} graph={graph} onSelect={onSelect} named={r.named} /></td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function Targets({ ts, graph, onSelect, named }: { ts: DocTarget[]; graph: boolean; onSelect?: (id: string) => void; named?: string[] }) {
  if (!graph) return <span className="muted small">—</span>;
  if (!ts.length) return <span className="amber-t small" title={named?.length ? `探した名前: ${named.join(", ")}` : undefined}>なし{named?.length ? `（${named.slice(0, 2).join(", ")}）` : ""}</span>;
  return (
    <span className="small">
      {ts.slice(0, 4).map((t, i) => (
        <span key={t.id}>{i > 0 && "、"}
          {onSelect ? <button className="linkish" onClick={() => onSelect(t.id)}>{t.name}</button> : t.name}
          <span className="muted"> {t.kind}</span></span>
      ))}
      {ts.length > 4 && <span className="muted"> ほか {ts.length - 4}</span>}
    </span>
  );
}
