import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, openRunSocket } from "./api";
import Graph, { Filters } from "./Graph";
import Inspector from "./Inspector";
import { ChecksPanel, DiffPanel, EstimatePanel, LogPanel } from "./Panels";
import type { Candidate, RunEvent, RunState, RunSummary } from "./types";

const LAYERS: [string, string][] = [["feature", "機能"], ["code", "コード"], ["data", "データ"], ["test", "テスト"], ["doc", "設計書"]];
type Side = "checks" | "estimate" | "log" | "diff";

export default function App() {
  const [status, setStatus] = useState<any>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(location.search).get("run"));
  const [run, setRun] = useState<RunState | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [side, setSide] = useState<Side>("checks");
  const [showNew, setShowNew] = useState(false);
  const [busy, setBusy] = useState(false);
  const [filters, setFilters] = useState<Filters>({
    layers: { feature: true, code: true, data: true, test: true, doc: true }, onlyOpen: false, minConf: 0, query: "",
  });
  const refetchTimer = useRef<number | null>(null);
  const lastSeq = useRef(-1);

  const loadRuns = useCallback(() => api.runs().then(setRuns).catch(() => {}), []);
  useEffect(() => {
    api.status().then((s) => { setStatus(s); if (!s.repo) setShowNew(true); }).catch(() => {});
    loadRuns();
  }, [loadRuns]);
  useEffect(() => { if (!runId && runs.length) setRunId(runs[0].id); }, [runs, runId]);

  const refetch = useCallback((id: string) => {
    if (refetchTimer.current) window.clearTimeout(refetchTimer.current);
    refetchTimer.current = window.setTimeout(() => api.run(id).then(setRun).catch(() => {}), 150);
  }, []);

  // open run: load state, then stream events (live updates applied incrementally)
  useEffect(() => {
    if (!runId) return;
    const url = new URL(location.href); url.searchParams.set("run", runId); history.replaceState(null, "", url);
    setSelected([]); setEvents([]); lastSeq.current = -1;
    let alive = true;
    api.run(runId).then((r) => { if (alive) setRun(r); });
    const close = openRunSocket(runId, (ev) => {
      if (ev.seq <= lastSeq.current) return;
      lastSeq.current = ev.seq;
      setEvents((es) => [...es, ev]);
      setRun((r) => {
        if (!r || r.id !== runId) return r;
        if (ev.type === "candidate") {
          if (r.candidates[ev.data.node_id]) return r;
          const c: Candidate = { ...ev.data, path: [], comments: [], decision: null, verdict: null, confidence: null } as Candidate;
          const parent = c.parent ? r.candidates[c.parent] : null;
          c.path = [...(parent ? parent.path : []), c.node_id];
          return { ...r, candidates: { ...r.candidates, [c.node_id]: c } };
        }
        if (ev.type === "verdict") {
          const c = r.candidates[ev.data.node_id];
          if (!c) return r;
          return { ...r, candidates: { ...r.candidates, [c.node_id]: { ...c, ...ev.data } } };
        }
        if (ev.type === "phase") return { ...r, phase: ev.data.phase, progress: ev.data.progress };
        if (ev.type === "intent") return r.intents.some((i) => i.id === ev.data.id) ? r : { ...r, intents: [...r.intents, ev.data] };
        return r;
      });
      if (["checks", "decision", "run.done", "comment", "approved", "expand.done", "error", "inherited", "grep.done", "crud.done"].includes(ev.type)) {
        refetch(runId);
        if (ev.type === "run.done") loadRuns();
      }
    });
    return () => { alive = false; close(); };
  }, [runId, refetch, loadRuns]);

  const select = useCallback((ids: string[], focus = false) => {
    setSelected(ids);
    if (focus && ids[0]) setFocusId(ids[0] + "|" + Date.now());
  }, []);

  const stats = useMemo(() => {
    const cs = run ? Object.values(run.candidates) : [];
    return {
      ok: cs.filter((c) => c.decision?.status === "confirmed").length,
      open: cs.filter((c) => !c.decision).length,
      ex: cs.filter((c) => c.decision?.status === "excluded").length,
    };
  }, [run]);

  const changed = useCallback(() => { if (runId) refetch(runId); }, [runId, refetch]);

  const openReport = async () => {
    if (!run) return;
    const md = await api.report(run.id);
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `impact-${run.id}.md`; a.click();
  };

  return (
    <div className="app">
      <header className="top">
        <div className="brand"><span className="logo mono">IMPACT</span>
          <select className="runsel" aria-label="解析を選択" value={runId || ""} onChange={(e) => setRunId(e.target.value)}>
            {runs.map((r) => <option key={r.id} value={r.id}>{r.parent_id ? "↳ " : ""}{r.title}（{r.id.slice(0, 4)}）</option>)}
          </select>
          <button className="btn small" onClick={() => setShowNew(true)}>新しい解析</button>
        </div>
        <div className="progress">
          <div className="bar"><div className="fill amber" style={{ width: `${(run?.progress ?? 0) * 100}%` }} /></div>
          <span className="mono small amber-t">{run ? (run.status === "done" ? "完了" : run.status === "error" ? "エラー" : `${phaseLabel(run.phase)} ${Math.round(run.progress * 100)}%`) : "—"}</span>
        </div>
        <div className="stats">
          <Stat k="確定" v={stats.ok} cls="cyan-t" /><Stat k="未判定" v={stats.open} cls="amber-t" />
          <Stat k="除外" v={stats.ex} cls="dim" />
          <Stat k="規模" v={run?.estimate?.days ?? "—"} unit="人日" />
        </div>
        <button className="btn small" disabled={!run} onClick={openReport}>レポート</button>
      </header>

      <main className="main">
        <section className="pane graph-pane" aria-label="影響グラフ">
          <div className="graph-head">
            <div className="chips">
              {LAYERS.map(([k, label]) => (
                <button key={k} className={filters.layers[k] ? "chip on" : "chip"} aria-pressed={filters.layers[k]}
                  onClick={() => setFilters((f) => ({ ...f, layers: { ...f.layers, [k]: !f.layers[k] } }))}>{label}</button>
              ))}
              <button className={filters.onlyOpen ? "chip on amber" : "chip"} aria-pressed={filters.onlyOpen}
                onClick={() => setFilters((f) => ({ ...f, onlyOpen: !f.onlyOpen }))}>未判定だけ</button>
              <label className="slider"><span className="k">確度</span>
                <input type="range" min={0} max={0.9} step={0.05} value={filters.minConf}
                  onChange={(e) => setFilters((f) => ({ ...f, minConf: Number(e.target.value) }))} />
                <span className="mono small">{filters.minConf.toFixed(2)}+</span></label>
            </div>
            <input className="input search" placeholder="ノードを検索" aria-label="ノードを検索" value={filters.query}
              onChange={(e) => setFilters((f) => ({ ...f, query: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === "Enter" && run) {
                  const q = filters.query.toLowerCase();
                  const hit = Object.values(run.candidates).find((c) => c.name.toLowerCase().includes(q));
                  if (hit) select([hit.node_id], true);
                }
              }} />
          </div>
          <Graph run={run} filters={filters} selected={selected} onSelect={select} focusId={focusId?.split("|")[0]} />
          <div className="legend">
            <span><i className="dot cyan" />確定</span><span><i className="dot amber" />影響あり(AI)</span>
            <span><i className="dot amber dashed" />要確認(AI)</span><span><i className="dot" />影響低・未判定</span>
            <span><i className="dot violet" />人が追加</span><span className="muted">内側から 機能 / コード / データ / テスト / 設計書</span>
          </div>
        </section>

        <aside className="side">
          {run && <Inspector run={run} ids={selected} onSelect={select} onChanged={changed} busy={busy} />}
          {run && (
            <div className="pane sidepanel">
              <div className="tabs">
                {([["checks", "全数判定"], ["estimate", "規模"], ["log", "ログ"], ["diff", "差分"]] as [Side, string][]).map(([k, l]) => (
                  <button key={k} className={side === k ? "tab on" : "tab"} aria-selected={side === k} role="tab" onClick={() => setSide(k)}>{l}</button>
                ))}
              </div>
              <div className="tabbody">
                {side === "checks" && <ChecksPanel run={run} onSelect={select} onChanged={changed} />}
                {side === "estimate" && <EstimatePanel run={run} onSelect={select} />}
                {side === "log" && <LogPanel events={events} onSelect={select} />}
                {side === "diff" && <DiffPanel run={run} onOpenRun={setRunId} />}
              </div>
            </div>
          )}
        </aside>
      </main>

      {run && <CommandBar run={run} selected={selected} setBusy={setBusy} onRun={(id) => { loadRuns(); setRunId(id); }} onChanged={changed} onSelect={select} />}
      {showNew && <NewRun status={status} onClose={() => setShowNew(false)}
        onIndexed={() => api.status().then(setStatus)}
        onCreated={(id) => { setShowNew(false); loadRuns(); setRunId(id); }} />}
      {!run && !showNew && <div className="empty">解析がありません。「新しい解析」から要求を投入してください。</div>}
    </div>
  );
}

function phaseLabel(p: string) {
  return ({ queued: "待機", structure: "要求を構造化", seed: "起点を探索", expand: "依存を追跡", judge: "影響を判定",
    grep: "横断検索", shadow: "独立再探索", crud: "CRUD確認", estimate: "規模算出", done: "完了" } as Record<string, string>)[p] || p;
}

function Stat({ k, v, cls = "", unit }: { k: string; v: number | string; cls?: string; unit?: string }) {
  return <div className="stat"><span className="k">{k}</span><span className={`mono val ${cls}`}>{v}{unit && <small> {unit}</small>}</span></div>;
}

function CommandBar({ run, selected, setBusy, onRun, onChanged, onSelect }: {
  run: RunState; selected: string[]; setBusy: (b: boolean) => void; onRun: (id: string) => void; onChanged: () => void;
  onSelect: (ids: string[], f?: boolean) => void;
}) {
  const [mode, setMode] = useState<"reinvestigate" | "ask" | "add">("reinvestigate");
  const [text, setText] = useState("");
  const [answer, setAnswer] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const names = selected.map((id) => run.candidates[id]?.name).filter(Boolean);
  const placeholder = mode === "reinvestigate"
    ? (names.length ? `${names[0]}${names.length > 1 ? ` ほか${names.length - 1}件` : ""} を起点に再調査: 例「設計書と照合して」「DBまで追って」` : "再調査の指示: 例「再発行の処理も対象に含めて調べて」")
    : mode === "ask" ? (names.length ? `選択中の${names.length}件について質問` : "ノードを選んでから質問してください")
    : "影響ありとして追加する要素: 例「OwnerController.processCreationForm」";
  const submit = async () => {
    if (!text.trim()) return;
    setErr(null); setBusy(true);
    try {
      if (mode === "reinvestigate") {
        const r = await api.reinvestigate(run.id, selected, text);
        setText(""); onRun(r.id);
      } else if (mode === "ask") {
        if (!selected.length) throw new Error("ノードを選択してください");
        const r = await api.ask(run.id, selected, text);
        setAnswer(r.answer);
      } else {
        const c = await api.add(run.id, { query: text, reason: "人が漏れを指摘" });
        setText(""); onChanged(); onSelect([c.node_id], true);
      }
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  };
  return (
    <div className="cmdwrap">
      {answer && <div className="answer pane"><button className="icon close" aria-label="閉じる" onClick={() => setAnswer(null)}>×</button><pre>{answer}</pre></div>}
      <form className="cmd" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <div className="modes" role="radiogroup" aria-label="指示の種類">
          {([["reinvestigate", "再調査"], ["ask", "質問"], ["add", "漏れを追加"]] as const).map(([k, l]) => (
            <button type="button" key={k} role="radio" aria-checked={mode === k} className={mode === k ? "mode on" : "mode"} onClick={() => setMode(k)}>{l}</button>
          ))}
        </div>
        <label htmlFor="cmd" className="sr">指示</label>
        <input id="cmd" className="cmdinput" value={text} onChange={(e) => setText(e.target.value)} placeholder={placeholder} />
        <button type="submit" className="btn light">実行</button>
      </form>
      {err && <p className="warn small cmderr">{err}</p>}
    </div>
  );
}

function NewRun({ status, onClose, onCreated, onIndexed }: { status: any; onClose: () => void; onCreated: (id: string) => void; onIndexed: () => void }) {
  const [repo, setRepo] = useState(status?.repo || "");
  const [docs, setDocs] = useState(status?.docs_dir || "");
  const [req, setReq] = useState("");
  const [title, setTitle] = useState("");
  const [depth, setDepth] = useState(3);
  const [pace, setPace] = useState(0.03);
  const [idx, setIdx] = useState<any>(status?.index);
  const [err, setErr] = useState<string | null>(null);
  const reindex = async () => {
    setErr(null);
    try {
      await api.index(repo, docs);
      const poll = async () => {
        const s = await api.indexStatus();
        setIdx(s);
        if (s.status === "running") setTimeout(poll, 500); else onIndexed();
      };
      poll();
    } catch (e: any) { setErr(e.message); }
  };
  const create = async () => {
    setErr(null);
    try { const r = await api.createRun(req, { title: title || undefined, depth, pace }); onCreated(r.id); }
    catch (e: any) { setErr(e.message); }
  };
  return (
    <div className="modal-bg" role="dialog" aria-modal="true" aria-label="新しい解析">
      <div className="modal pane">
        <div className="row between"><h2>新しい解析</h2><button className="icon" aria-label="閉じる" onClick={onClose}>×</button></div>
        <fieldset>
          <legend className="k">対象リポジトリ</legend>
          <label className="field"><span className="k">リポジトリのパス</span><input className="input mono" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="/path/to/repo" /></label>
          <label className="field"><span className="k">設計書フォルダ（任意・Markdown/テキスト）</span><input className="input mono" value={docs} onChange={(e) => setDocs(e.target.value)} placeholder="/path/to/design-docs" /></label>
          <div className="row between">
            <span className="muted small">{idx?.status === "running" ? "インデックス作成中…" : idx?.stats ? `ノード ${idx.stats.nodes}・関係 ${idx.stats.edges}（${idx.stats.seconds}秒）` : "未インデックス"}</span>
            <button className="btn small" disabled={!repo || idx?.status === "running"} onClick={reindex}>インデックス作成</button>
          </div>
        </fieldset>
        <label className="field"><span className="k">要求（仕様書の該当箇所やチャットの内容を貼り付け）</span>
          <textarea className="input" rows={5} value={req} onChange={(e) => setReq(e.target.value)}
            placeholder="例: オーナーに電話番号に加えてメールアドレスを登録できるようにする。メールアドレスはオーナー詳細画面に表示する。" /></label>
        <div className="grid3">
          <label className="field"><span className="k">タイトル</span><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <label className="field"><span className="k">探索の深さ</span><input className="input" type="number" min={1} max={6} value={depth} onChange={(e) => setDepth(Number(e.target.value))} /></label>
          <label className="field"><span className="k">表示ウェイト(秒)</span><input className="input" type="number" min={0} max={1} step={0.01} value={pace} onChange={(e) => setPace(Number(e.target.value))} /></label>
        </div>
        <p className="muted small">判定方式: {status?.llm === "claude" ? "Claude API" : "ルールベース（ANTHROPIC_API_KEY を設定するとClaudeで判定）"}・ストア: {status?.store}</p>
        {err && <p className="warn small">{err}</p>}
        <div className="actions end"><button className="btn ghost" onClick={onClose}>閉じる</button>
          <button className="btn primary" disabled={!req.trim() || !status?.repo && !idx?.stats} onClick={create}>解析を開始</button></div>
      </div>
    </div>
  );
}
