import { useEffect, useState } from "react";
import { api } from "./api";
import type { Handoff, RunState, Verification } from "./types";

type Tab = "order" | "agent" | "verify";

/** Hand-off to development: the reviewed scope as a work order, how to implement it with
 *  Claude Code over MCP, and the check of the actual change against the prediction. */
export default function HandoffDialog({ run, graphName, status, onClose, onChanged }: {
  run: RunState; graphName?: string; status: any; onClose: () => void; onChanged: () => void;
}) {
  const [tab, setTab] = useState<Tab>("order");
  const [h, setH] = useState<Handoff | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  useEffect(() => { api.handoff(run.id).then(setH).catch((e) => setErr(e.message)); }, [run.id]);

  const copy = async (key: string, text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(key); setTimeout(() => setCopied(null), 1500); }
    catch { setErr("コピーできませんでした。テキストを選択してコピーしてください。"); }
  };
  const download = () => {
    if (!h) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([h.markdown], { type: "text/markdown;charset=utf-8" }));
    a.download = `作業指示-${run.id}.md`; a.click();
  };
  const graph = status?.graphs?.[graphName || status?.graph || ""] || {};
  const workspace: string = status?.workspace || "<ワークスペースのフォルダ>";
  const nTargets = h ? h.files.reduce((n, f) => n + f.items.length, 0) + h.other_targets.length : 0;
  const mcpCmd = `claude mcp add --transport stdio --env HIMOTOKI_DATA_DIR=${workspace} himotoki -- himotoki mcp`;
  const prompt = `himotoki の get_handoff で run ${run.id} の作業指示を取得し、その範囲で実装してください。\n`
    + `・作業指示の「修正対象」以外を直す必要が出たら、理由を書いて止まり、確認を求めてください。\n`
    + `・「除外」の要素は変更しないでください。「トランザクション・連携の注意」がある箇所は、影響を説明してから変更してください。\n`
    + `・終わったらコミットし、verify_change（run_id=${run.id}、base=作業前のブランチ、head=作業後のブランチ`
    + `${graph.repos ? "、repo=変更したリポジトリ名" : ""}）で予測と突き合わせ、予測漏れがあれば報告してください。`;

  return (
    <div className="modal-bg" role="dialog" aria-modal="true" aria-label="開発へ渡す">
      <div className="modal pane handoff">
        <div className="row between"><h2>開発へ渡す</h2><button className="icon" aria-label="閉じる" onClick={onClose}>×</button></div>
        <div className="tabs" role="tablist">
          {([["order", "作業指示"], ["agent", "Claude Code で実装"], ["verify", "実績と照合"]] as [Tab, string][]).map(([k, l]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? "tab on" : "tab"} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>
        {err && <p className="warn small">{err}</p>}

        {tab === "order" && (h ? (
          <div className="stack">
            <div className="row hs-sum">
              <span className={run.approved ? "hs-badge ok" : "hs-badge"}>{run.approved ? "承認済み" : "未承認"}</span>
              <span className="small">修正対象 <b>{nTargets}</b> ・未判定 {h.pending.length}・除外 {h.excluded.length}・テスト {h.tests_to_run.length}
                ・見込み {h.estimate_days ?? "—"} 人日</span>
            </div>
            {h.warnings.map((w) => <p key={w} className="warn small">{w}</p>)}
            <p className="muted small">確定した要素をファイルごとにまとめ、理由・コメント・トランザクションの注意・関連する設計書・呼び出し元・実行するテスト・除外を載せた作業指示です。開発会社への依頼にもそのまま使えます。</p>
            <pre className="md-preview">{h.markdown}</pre>
            <div className="row">
              <button className="btn small" onClick={() => copy("md", h.markdown)}>{copied === "md" ? "コピーしました" : "コピー"}</button>
              <button className="btn small" onClick={download}>Markdown でダウンロード</button>
            </div>
          </div>
        ) : <p className="muted small">作成中…</p>)}

        {tab === "agent" && (
          <div className="stack">
            <p className="small">Claude Code から himotoki の MCP サーバを使うと、エージェントが作業指示を直接取得し、その範囲で実装して、最後に予測と照合します。</p>
            <ol className="steps small">
              <li>開発する PC に himotoki を入れ、Claude Code に一度だけ登録します（ワークスペースは解析に使ったフォルダ）。
                <div className="hcmd"><code>{mcpCmd}</code><button className="btn small ghost" onClick={() => copy("mcp", mcpCmd)}>{copied === "mcp" ? "コピーしました" : "コピー"}</button></div>
                <span className="muted">チームで共有する場合は <code>--scope project</code> を付けると、リポジトリの <code>.mcp.json</code> に保存されます。</span>
              </li>
              <li>修正するリポジトリで Claude Code を開き、次の依頼を貼り付けます。
                <div className="hcmd"><pre>{prompt}</pre><button className="btn small ghost" onClick={() => copy("prompt", prompt)}>{copied === "prompt" ? "コピーしました" : "コピー"}</button></div>
              </li>
              <li>エージェントは <code>get_handoff</code> → 実装 → <code>verify_change</code> の順に進めます。照合の結果は「実績と照合」タブにも出ます。</li>
            </ol>
            <p className="muted small">使えるツール: list_runs（解析の一覧）、get_handoff（作業指示）、impact_files（要求から変更が要りそうなファイル）、get_element / neighbors（要素の詳細とつながり）、verify_change（予測と実績の照合）ほか。</p>
          </div>
        )}

        {tab === "verify" && <VerifyTab run={run} repos={graph.repos} onChanged={onChanged} />}
      </div>
    </div>
  );
}

function VerifyTab({ run, repos, onChanged }: { run: RunState; repos?: Record<string, string>; onChanged: () => void }) {
  const [base, setBase] = useState("main");
  const [head, setHead] = useState("");
  const [repo, setRepo] = useState("");
  const [days, setDays] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [v, setV] = useState<Verification | null>(run.verification || null);
  const go = async () => {
    setBusy(true); setErr(null);
    try {
      setV(await api.verify(run.id, { base, head, repo: repo || null, actual_days: days.trim() ? Number(days) : null }));
      onChanged();
    } catch (e: any) { setErr(e.message); }
    setBusy(false);
  };
  const pct = (x: number | null) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  return (
    <div className="stack">
      <p className="muted small">実装が終わったら、作業前と作業後のブランチ（またはコミット）を指定してください。実際に変更された要素を、この解析の確定内容と突き合わせます。</p>
      <div className="grid3">
        <label className="field"><span className="k">作業前</span><input className="input mono" value={base} onChange={(e) => setBase(e.target.value)} /></label>
        <label className="field"><span className="k">作業後</span><input className="input mono" value={head} placeholder="feature/xxx" onChange={(e) => setHead(e.target.value)} /></label>
        {repos ? (
          <label className="field"><span className="k">変更したリポジトリ</span>
            <select className="input mono" value={repo} onChange={(e) => setRepo(e.target.value)}>
              <option value="">選択してください</option>
              {Object.keys(repos).map((n) => <option key={n} value={n}>{n}</option>)}
            </select></label>
        ) : (
          <label className="field"><span className="k">実績の人日（任意・規模の補正に使う）</span>
            <input className="input mono" value={days} inputMode="decimal" onChange={(e) => setDays(e.target.value)} /></label>
        )}
      </div>
      {repos && <label className="field"><span className="k">実績の人日（任意・規模の補正に使う）</span>
        <input className="input mono" value={days} inputMode="decimal" onChange={(e) => setDays(e.target.value)} /></label>}
      <div className="row"><button className="btn small" disabled={busy || !base || !head || (!!repos && !repo)} onClick={go}>{busy ? "照合中…" : "照合する"}</button>
        {err && <span className="warn small">{err}</span>}</div>
      {v && (
        <div className="stack">
          <div className="row hs-sum small">
            <span>{v.base}..{v.head}</span>
            <span>変更された要素 <b>{v.changed_elements}</b></span>
            <span>予測で当たった割合 <b>{pct(v.recall)}</b></span>
            <span>予測のうち変更された割合 <b>{pct(v.precision)}</b></span>
            {v.calibration && <span>規模の補正係数 <b>{v.calibration.calibration}</b>（{v.calibration.samples} 件）</span>}
          </div>
          {v.missed.length > 0 && <>
            <div className="k">予測漏れ {v.missed.length}</div>
            <ul className="vlist small">{v.missed.map((m) => (
              <li key={m.node_id}><b>{m.name}</b> <span className="muted">{m.kind}</span> — <span className={m.why === "not_found" ? "warn" : "amber-t"}>{m.why_ja}</span>
                {m.candidate_reason ? <span className="muted">（{m.candidate_reason}）</span> : null}</li>))}</ul>
            <p className="muted small">「候補に無かった」はグラフが届いていない箇所です（ルールやデータの追加で改善）。「候補にあったが…」は判断の見直し点です。</p>
          </>}
          {v.untouched.length > 0 && <>
            <div className="k">予測したが変更されなかった {v.untouched.length}</div>
            <ul className="vlist small">{v.untouched.map((u) => <li key={u.node_id}>{u.name} <span className="muted">{u.kind}</span></li>)}</ul>
          </>}
          {v.missed.length === 0 && v.untouched.length === 0 && <p className="small cyan-t">予測と実績が一致しました。</p>}
        </div>
      )}
    </div>
  );
}
