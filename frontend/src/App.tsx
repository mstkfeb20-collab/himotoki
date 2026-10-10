import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, openRunSocket } from "./api";
import Graph, { AGGREGATE_ABOVE, Filters } from "./Graph";
import Inspector from "./Inspector";
import ListView from "./ListView";
import ReqAid from "./ReqAid";
import Tour from "./Tour";
import { isFiltered, sizeOf, visibleCandidates } from "./sizing";
import { ChecksPanel, DiffPanel, EstimatePanel, IndexPanel, LogPanel } from "./Panels";
import type { Candidate, RunEvent, RunState, RunSummary, WorkspaceUsage } from "./types";

const LAYERS: [string, string][] = [["feature", "機能"], ["code", "コード"], ["data", "データ"], ["test", "テスト"], ["doc", "設計書"]];
type Side = "checks" | "estimate" | "log" | "diff" | "index";

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
  const [grouping, setGrouping] = useState(true);
  // null = automatic: the graph while a run is in progress (to watch it grow), the list once it is done
  const [view, setView] = useState<"list" | "graph" | null>(null);
  const TOUR_KEY = "codegraph.tour.v1";
  const [tour, setTour] = useState(false);
  const [practice, setPractice] = useState<string | null>(null);
  useEffect(() => {
    // first visit: offer the walkthrough (browser storage may be unavailable; then it simply shows once per load)
    let seen = false;
    try { seen = localStorage.getItem(TOUR_KEY) === "1"; } catch { /* storage blocked */ }
    if (!seen) setTour(true);
  }, []);
  const closeTour = () => { setTour(false); try { localStorage.setItem(TOUR_KEY, "1"); } catch { /* ignore */ } };
  const startPractice = async () => {
    closeTour();
    let text = "";
    try { text = (await api.exampleRequirement(status?.graph)).requirement || ""; } catch { /* no graph yet */ }
    setPractice(text || "予約入力画面の備考の入力文字数の上限を80文字から200文字に変更する。");
    setShowNew(true);
  };
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filters, setFilters] = useState<Filters>({
    layers: { feature: true, code: true, data: true, test: true, doc: true }, onlyOpen: false, minConf: 0, query: "",
  });
  const refetchTimer = useRef<number | null>(null);
  const [wsUsage, setWsUsage] = useState<WorkspaceUsage | null>(null);
  const usageTimer = useRef<number | null>(null);
  const loadUsage = useCallback(() => {
    // throttled: many AI calls in a row cause one refresh
    if (usageTimer.current) return;
    usageTimer.current = window.setTimeout(() => {
      usageTimer.current = null;
      api.usage().then(setWsUsage).catch(() => {});
    }, 1500);
  }, []);
  useEffect(() => {
    api.usage().then(setWsUsage).catch(() => {});
    const t = window.setInterval(() => api.usage().then(setWsUsage).catch(() => {}), 60_000);
    return () => window.clearInterval(t);
  }, []);
  const lastSeq = useRef(-1);

  const loadRuns = useCallback(() => api.runs().then(setRuns).catch(() => {}), []);
  useEffect(() => {
    api.status().then((s) => { setStatus(s); if (!s.repo && !s.repos) setShowNew(true); }).catch(() => {});
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
    setSelected([]); setEvents([]); setExpanded(new Set()); setView(null); lastSeq.current = -1;
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
        if (ev.type === "llm.usage") return { ...r, llm_usage: ev.data };
        if (ev.type === "intent") return r.intents.some((i) => i.id === ev.data.id) ? r : { ...r, intents: [...r.intents, ev.data] };
        return r;
      });
      if (ev.type === "llm.usage") loadUsage();
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

  const shownView: "list" | "graph" = view ?? (run && run.status === "running" ? "graph" : "list");
  const showInGraph = useCallback((id: string) => { setView("graph"); setSelected([id]); setFocusId(id + "|" + Date.now()); }, []);
  const runGraph = useMemo(() => runs.find((r) => r.id === runId)?.graph ?? status?.graph ?? null, [runs, runId, status]);
  const nCands = run ? Object.keys(run.candidates).length : 0;

  const changed = useCallback(() => { if (runId) refetch(runId); }, [runId, refetch]);

  const openReport = async () => {
    if (!run) return;
    const md = await api.report(run.id);
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `codegraph-${run.id}.md`; a.click();
  };

  return (
    <div className="app">
      <header className="top">
        <div className="brand"><span className="logo mono">CODEGRAPH</span>
          <select className="runsel" aria-label="解析を選択" value={runId || ""} onChange={(e) => setRunId(e.target.value)}>
            {runs.map((r) => <option key={r.id} value={r.id}>{r.parent_id ? "↳ " : ""}{r.title}（{r.graph ? `${r.graph}・` : ""}{r.id.slice(0, 4)}）</option>)}
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
          <AiCost run={run} ws={wsUsage} />
        </div>
        <button className="btn small" disabled={!run} onClick={openReport}>レポート</button>
        <button className="btn small ghost" onClick={() => setTour(true)} title="画面の使い方を順に案内します">使い方</button>
      </header>

      <main className="main">
        <section className="pane graph-pane" aria-label="影響グラフ">
          <div className={`graph-head${shownView === "list" ? " static" : ""}`}>
            <div className="viewtabs" role="tablist" aria-label="表示">
              {([["list", "リスト"], ["graph", "グラフ"]] as const).map(([k, l]) => (
                <button key={k} role="tab" aria-selected={shownView === k} className={shownView === k ? "vtab on" : "vtab"}
                  onClick={() => setView(k)}>{l}</button>
              ))}
            </div>
            <div className="chips">
              {LAYERS.map(([k, label]) => (
                <button key={k} className={filters.layers[k] ? "chip on" : "chip"} aria-pressed={filters.layers[k]}
                  onClick={() => setFilters((f) => ({ ...f, layers: { ...f.layers, [k]: !f.layers[k] } }))}>{label}</button>
              ))}
              <button className={filters.onlyOpen ? "chip on amber" : "chip"} aria-pressed={filters.onlyOpen}
                onClick={() => setFilters((f) => ({ ...f, onlyOpen: !f.onlyOpen }))}>未判定だけ</button>
              {shownView === "graph" && nCands > AGGREGATE_ABOVE && (
                <>
                  <button className={grouping ? "chip on" : "chip"} aria-pressed={grouping} title="確度の低い候補をモジュール単位にまとめる"
                    onClick={() => setGrouping((g) => !g)}>まとめ表示</button>
                  {grouping && expanded.size > 0 && <button className="chip" onClick={() => setExpanded(new Set())}>畳み直す</button>}
                </>
              )}
              <label className="slider"><span className="k">確度</span>
                <input type="range" min={0} max={0.9} step={0.05} value={filters.minConf}
                  onChange={(e) => setFilters((f) => ({ ...f, minConf: Number(e.target.value) }))} />
                <span className="mono small">{filters.minConf.toFixed(2)}+</span></label>
            </div>
            {shownView === "graph" && run && isFiltered(filters) && <FilteredSize run={run} filters={filters}
              onReset={() => setFilters({ layers: { feature: true, code: true, data: true, test: true, doc: true }, onlyOpen: false, minConf: 0, query: "" })} />}
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
          {shownView === "list" && run ? (
            <ListView run={run} filters={filters} selected={selected} onSelect={select} onChanged={changed}
              onShowInGraph={showInGraph} />
          ) : (
          <Graph run={run} filters={filters} selected={selected} onSelect={select} focusId={focusId?.split("|")[0]}
            expanded={expanded} grouping={grouping} onExpand={(k) => setExpanded((e) => new Set(e).add(k))} />
          )}
          {shownView === "graph" && <div className="legend">
            <span><i className="dot cyan" />確定</span><span><i className="dot amber" />影響あり(AI)</span>
            <span><i className="dot amber dashed" />要確認(AI)</span><span><i className="dot" />影響低・未判定</span>
            <span><i className="dot violet" />人が追加</span>
            {nCands > AGGREGATE_ABOVE && grouping && <span><i className="dot agg" />まとめ（クリックで展開）</span>}<span className="muted">内側から 機能 / コード / データ / テスト / 設計書</span>
          </div>}
        </section>

        <aside className="side">
          {run && <Inspector run={run} ids={selected} onSelect={select} onChanged={changed} busy={busy} />}
          {run && (
            <div className="pane sidepanel">
              <div className="tabs">
                {([["checks", "全数判定"], ["estimate", "規模"], ["log", "ログ"], ["diff", "差分"], ["index", "取込状況"]] as [Side, string][]).map(([k, l]) => (
                  <button key={k} className={side === k ? "tab on" : "tab"} aria-selected={side === k} role="tab" onClick={() => setSide(k)}>{l}</button>
                ))}
              </div>
              <div className="tabbody">
                {side === "checks" && <ChecksPanel run={run} onSelect={select} onChanged={changed} />}
                {side === "estimate" && <EstimatePanel run={run} onSelect={select}
                  onOnlyLayer={(layer) => setFilters((f) => ({ ...f, layers: Object.fromEntries(Object.keys(f.layers).map((k) => [k, k === layer])) }))} />}
                {side === "log" && <LogPanel events={events} onSelect={select} />}
                {side === "diff" && <DiffPanel run={run} onOpenRun={setRunId} />}
                {side === "index" && <IndexPanel graph={runGraph} />}
              </div>
            </div>
          )}
        </aside>
      </main>

      {run && <CommandBar run={run} selected={selected} setBusy={setBusy} onRun={(id) => { loadRuns(); setRunId(id); }} onChanged={changed} onSelect={select} />}
      {tour && <Tour onClose={closeTour} onPractice={startPractice} steps={[
        { title: "codegraph へようこそ", body: "要求（仕様書の一文やチャットの内容）から、影響するソースコード・SQL・画面・ジョブ・設計書を洗い出し、修正の規模を見積もるツールです。コードは書き換えません。画面の各部分を順に紹介します。" },
        { sel: ".brand .btn", title: "1. 新しい解析", body: "ここから要求を入力して解析を始めます。テンプレートと書き方の確認があるので、どう書けばよいか迷ったら使ってください。" },
        { sel: ".runsel", title: "2. 解析の切り替え", body: "これまでの解析と再調査の結果を選べます。「↳」は再調査（前回の判断を引き継いだやり直し）です。" },
        { sel: ".viewtabs", title: "3. リストとグラフ", before: () => setView("list"), body: "リストは確認作業用、グラフは全体像や説明用です。解析中はグラフ、終わるとリストに自動で切り替わります。" },
        { sel: ".lv-rows", before: () => setView("list"), title: "4. 上から順に確認する", body: "「影響ありの可能性が高い」から順に並んでいます。行を選ぶと右に根拠・コード・設計書が出ます。確定（c キー）・除外（x キー、理由を記録）で判断すると、次の行に進みます。" },
        { sel: ".chips", title: "5. 絞り込み", body: "機能・コード・データ・テスト・設計書の層や、未判定だけ・確度で絞り込めます。絞り込むと、その範囲の件数と規模（人日）も出ます。" },
        { sel: ".side .tabs", title: "6. 全数判定・規模・取込状況", body: "全数判定タブの条件がすべて満たされると、影響範囲として承認できます。規模タブには層ごとの内訳、取込状況タブには解析の死角（読めなかった参照など）が出ます。" },
        { sel: ".cmd", title: "7. 再調査・質問・漏れの追加", body: "「設計書と照合して」「DBまで追って」などの指示で再調査したり、漏れている要素を追加したりできます。" },
        { sel: ".stats", title: "8. 進み具合と費用", body: "確定・未判定・除外の件数、規模（人日）、AIの費用がここに出ます。AIを使うたびに費用が更新されます。" },
        { title: "練習してみましょう", body: "このリポジトリから作った例文で解析を始めます。結果をリストで上から確認し、確定・除外を何件か試してみてください（練習の解析はあとから無視して構いません）。" },
      ]} />}
      {showNew && <NewRun status={status} initialReq={practice ?? undefined} onClose={() => { setShowNew(false); setPractice(null); }}
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

const yen = (v?: number | null) => v == null ? "—" : `¥${v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString("ja-JP")}`;
const ktok = (n?: number) => n == null ? "—" : n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;

/** Live AI cost: this run (updated on every API call) and this month for the workspace. */
function AiCost({ run, ws }: { run: RunState | null; ws: WorkspaceUsage | null }) {
  const u = run?.llm_usage;
  const tokens = u ? u.input_tokens + u.output_tokens : undefined;
  const title = u ? `この解析: 入力 ${u.input_tokens.toLocaleString("ja-JP")}・出力 ${u.output_tokens.toLocaleString("ja-JP")} トークン、` +
    `API ${u.calls} 回、${u.model ?? "モデル未使用"}、$${(u.cost_usd ?? 0).toFixed(4)} × ${u.jpy_per_usd ?? ws?.jpy_per_usd ?? "—"}円` +
    (u.unpriced_calls ? `（単価未設定のモデルの呼び出し ${u.unpriced_calls} 回は含まず）` : "") +
    (ws ? `\n今日: ${yen(ws.today.cost_jpy)}（${ktok(ws.today.input_tokens + ws.today.output_tokens)} トークン）` +
      `\n今月: ${yen(ws.month.cost_jpy)}（${ktok(ws.month.input_tokens + ws.month.output_tokens)} トークン）\n単価: ${ws.pricing_source}` : "")
    : "";
  return (
    <div className="stat ai-cost" title={title} aria-label={title}>
      <span className="k">AI</span>
      <span className={`mono val${run?.status === "running" && u?.calls ? " live" : ""}`}>{u?.calls ? yen(u.cost_jpy) : "—"}
        {!!u?.calls && <small> {ktok(tokens)}tok</small>}</span>
      {ws && <span className="mono sub">今月 {yen(ws.month.cost_jpy)}</span>}
    </div>
  );
}

/** In the graph view: how many candidates and how much effort the current filter shows. */
function FilteredSize({ run, filters, onReset }: { run: RunState; filters: Filters; onReset: () => void }) {
  const s = sizeOf(run, visibleCandidates(run, filters));
  return (
    <div className="fsize" title={`確定 ${s.confirmedH}h ＋ 見込み ${s.expectedH}h`}>
      <span className="k">表示中</span><span className="mono">{s.count}</span><span className="muted small">件・</span>
      <span className="mono">{s.days}</span><span className="muted small">人日 ／ 全体 {run.estimate?.days ?? "—"}人日</span>
      <button className="link small" onClick={onReset}>絞り込み解除</button>
    </div>
  );
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

/** Repository field: one path, or one line per repository of a system graph ("name=path"). */
function repoText(x: any): string {
  if (x?.repos) return Object.entries(x.repos as Record<string, string>).map(([n, p]) => `${n}=${p}`).join("\n");
  return x?.repo || "";
}

function parseRepos(lines: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const l of lines) {
    const i = l.indexOf("=");
    const [n, p] = i > 0 ? [l.slice(0, i).trim(), l.slice(i + 1).trim()] : [l.replace(/\/+$/, "").split("/").pop() || "repo", l];
    out[n] = p;
  }
  return out;
}

function NewRun({ status, onClose, onCreated, onIndexed, initialReq }: { status: any; onClose: () => void; onCreated: (id: string) => void; onIndexed: () => void; initialReq?: string }) {
  const graphs: Record<string, any> = status?.graphs || {};
  const [graph, setGraph] = useState<string>(status?.graph || "");
  const g = graphs[graph] || {};
  const [repo, setRepo] = useState(repoText(g) || status?.repo || "");
  const [sysName, setSysName] = useState(g.repos ? graph : "system");
  const [changedRepo, setChangedRepo] = useState("");
  const repoLines = repo.split("\n").map((l: string) => l.trim()).filter(Boolean);
  const isSystem = repoLines.length > 1 || !!g.repos;
  const [docs, setDocs] = useState(g.docs_dir || status?.docs_dir || "");
  const [ref, setRef] = useState(g.ref || "");
  const [mode, setMode] = useState<"req" | "diff">("req");
  const [base, setBase] = useState(g.ref || "main");
  const [head, setHead] = useState("");
  const [req, setReq] = useState(initialReq || "");
  const [title, setTitle] = useState("");
  const [depth, setDepth] = useState(3);
  const [pace, setPace] = useState(0.03);
  const [aiMax, setAiMax] = useState("");          // empty = server setting
  const [aiMin, setAiMin] = useState("");
  const [aiDefault, setAiDefault] = useState<{ min_score: number; max_items: number | "unlimited" } | null>(null);
  useEffect(() => { api.llmSettings().then((r) => setAiDefault(r.effective)).catch(() => {}); }, []);
  const [idx, setIdx] = useState<any>(status?.index);
  const [err, setErr] = useState<string | null>(null);
  const reindex = async () => {
    setErr(null);
    try {
      const r = isSystem ? await api.indexSystem(parseRepos(repoLines), sysName.trim(), docs)
        : await api.index(repo.trim(), docs, ref);
      setGraph(r.graph);
      const poll = async () => {
        const s = await api.indexStatus(r.graph);
        setIdx(s);
        if (s.status === "running") setTimeout(poll, 500); else onIndexed();
      };
      poll();
    } catch (e: any) { setErr(e.message); }
  };
  const create = async () => {
    setErr(null);
    try {
      const r = mode === "diff"
        ? await api.diffRun(base, head, graph || null, title || undefined, changedRepo || null)
        : await api.createRun(req, { title: title || undefined, depth, pace, graph: graph || null,
            ...(aiMax.trim() ? { llm_max_items: aiMax.trim() } : {}),
            ...(aiMin.trim() ? { llm_min_score: Number(aiMin) } : {}) });
      onCreated(r.id);
    }
    catch (e: any) { setErr(e.message); }
  };
  return (
    <div className="modal-bg" role="dialog" aria-modal="true" aria-label="新しい解析">
      <div className="modal pane">
        <div className="row between"><h2>新しい解析</h2><button className="icon" aria-label="閉じる" onClick={onClose}>×</button></div>
        <fieldset>
          <legend className="k">対象リポジトリ</legend>
          {Object.keys(graphs).length > 0 && (
            <label className="field"><span className="k">インデックス済みのグラフ</span>
              <select className="input mono graphsel" value={graph} onChange={(e) => {
                const n = e.target.value; setGraph(n); const x = graphs[n] || {};
                setRepo(repoText(x)); setSysName(x.repos ? n : "system"); setChangedRepo(""); setDocs(x.docs_dir || ""); setRef(x.ref || ""); setBase(x.ref || "main"); setIdx(null);
              }}>
                {Object.entries(graphs).map(([n, x]) => <option key={n} value={n}>{n}（{x.nodes ?? "?"} ノード）</option>)}
              </select></label>
          )}
          <label className="field"><span className="k">リポジトリのパス（複数サーバで1つの業務を構成する場合は1行に1つ。<span className="mono">名前=パス</span> も可）</span>
            <textarea className="input mono" rows={Math.min(5, Math.max(1, repoLines.length + (repo.endsWith("\n") ? 1 : 0)))} value={repo}
              onChange={(e) => setRepo(e.target.value)} placeholder="/path/to/repo" /></label>
          {isSystem && <label className="field"><span className="k">システム名（複数リポジトリを1つのグラフにまとめます）</span>
            <input className="input mono" value={sysName} onChange={(e) => setSysName(e.target.value)} placeholder="sor" /></label>}
          <label className="field"><span className="k">設計書・運用資産フォルダ（任意・Excel/Word/Markdown、ジョブ定義、シェル）</span><input className="input mono" value={docs} onChange={(e) => setDocs(e.target.value)} placeholder="/path/to/design-docs" /></label>
          <label className="field"><span className="k">ブランチ・タグ（任意。指定するとその時点のスナップショットを別グラフとして作成）</span><input className="input mono" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main / release/1.2" /></label>
          <div className="row between">
            <span className="muted small">{idx?.status === "running" ? "インデックス作成中…" : idx?.stats ? `ノード ${idx.stats.nodes}・関係 ${idx.stats.edges}（${idx.stats.seconds}秒）` : "未インデックス"}</span>
            <button className="btn small" disabled={!repo || idx?.status === "running"} onClick={reindex}>インデックス作成</button>
          </div>
        </fieldset>
        <div className="modes" role="radiogroup" aria-label="解析の起点">
          {([["req", "要求から"], ["diff", "変更差分から"]] as const).map(([k, l]) => (
            <button type="button" key={k} role="radio" aria-checked={mode === k} className={mode === k ? "mode on" : "mode"} onClick={() => setMode(k)}>{l}</button>
          ))}
        </div>
        {mode === "req" ? (<>
          <label className="field"><span className="k">要求（仕様書の該当箇所やチャットの内容を貼り付け）</span>
            {initialReq && <p className="practice-note">練習用の例文です（このリポジトリの実際の項目から作成）。そのまま「解析を開始」を押してください。</p>}
            <textarea className="input" rows={5} value={req} onChange={(e) => setReq(e.target.value)}
              placeholder="例: 予約の備考欄の文字数上限を80文字から200文字に変更する。" /></label>
          <ReqAid text={req} setText={setReq} graph={graph || status?.graph} />
        </>) : (
          <div className="grid3">
            <label className="field"><span className="k">比較元（グラフの時点）</span><input className="input mono" value={base} onChange={(e) => setBase(e.target.value)} /></label>
            <label className="field"><span className="k">変更後（ブランチ・コミット）</span><input className="input mono" value={head} onChange={(e) => setHead(e.target.value)} placeholder="feature/xxx" /></label>
            {g.repos && <label className="field"><span className="k">変更したリポジトリ</span>
              <select className="input mono" value={changedRepo} onChange={(e) => setChangedRepo(e.target.value)}>
                <option value="">選択してください</option>
                {Object.keys(g.repos).map((n) => <option key={n} value={n}>{n}</option>)}
              </select></label>}
            <p className="muted small">変更された行を含む要素を起点に、回帰確認の範囲（テスト・画面・ジョブ・設計書）を出します。</p>
          </div>
        )}
        <div className="grid3">
          <label className="field"><span className="k">タイトル</span><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
          <label className="field"><span className="k">探索の深さ</span><input className="input" type="number" min={1} max={6} value={depth} onChange={(e) => setDepth(Number(e.target.value))} /></label>
          <label className="field"><span className="k">表示ウェイト(秒)</span><input className="input" type="number" min={0} max={1} step={0.01} value={pace} onChange={(e) => setPace(Number(e.target.value))} /></label>
        </div>
        {status?.llm === "claude" && mode === "req" && (
          <div className="grid3">
            <label className="field"><span className="k">AI判定の上限件数</span>
              <input className="input mono" value={aiMax} onChange={(e) => setAiMax(e.target.value)}
                placeholder={aiDefault ? `既定 ${aiDefault.max_items}（unlimited で上限なし）` : "unlimited で上限なし"} /></label>
            <label className="field"><span className="k">AIに回す最低スコア</span>
              <input className="input mono" type="number" min={0} max={1} step={0.05} value={aiMin}
                onChange={(e) => setAiMin(e.target.value)} placeholder={aiDefault ? `既定 ${aiDefault.min_score}` : ""} /></label>
            <p className="muted small">上限を超えた候補と低スコアの候補はルールで判定し、要確認として残ります。</p>
          </div>
        )}
        <p className="muted small">判定方式: {status?.llm === "claude" ? "Claude API" : "ルールベース（ANTHROPIC_API_KEY を設定するとClaudeで判定）"}・ストア: {status?.store}</p>
        {err && <p className="warn small">{err}</p>}
        <div className="actions end"><button className="btn ghost" onClick={onClose}>閉じる</button>
          <button className="btn primary" disabled={(mode === "req" ? !req.trim() : !head.trim()) || (!status?.repo && !idx?.stats)} onClick={create}>解析を開始</button></div>
      </div>
    </div>
  );
}
