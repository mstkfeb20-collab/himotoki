import cytoscape, { Core } from "cytoscape";
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { statusOf } from "./Graph";
import { kindClass, CLASS_LABEL } from "./kinds";
import type { Brief, LinkGroup, RunState } from "./types";

/** Following connections from one element: callers and whoever uses it on the left,
 *  callees and whatever it uses on the right. Clicking a node opens its detail; the
 *  "＋" on a node (or double click) unfolds its own connections one step further.
 *  Nothing here changes the analysis. */

const FAMILY: Record<string, string> = {
  CALLS: "call", REFERENCES: "call",
  READS: "data", WRITES: "data", POPULATES: "data", MAPS_TO: "data", DEFINES: "data", EXECUTES: "data", OBSERVED_AS: "data",
  RUNS: "job", PRECEDES: "job",
  RENDERS: "screen", BINDS: "screen", HANDLES: "screen", USES_MESSAGE: "screen", INCLUDES: "screen",
  DESCRIBES: "doc", DOC_CRUD: "doc",
  CONTAINS: "struct", EXTENDS: "struct", IMPLEMENTED_BY: "struct", INJECTS: "struct", CONFIGURES: "struct",
  TESTS: "test",
};
const FAMILY_LABEL: [string, string][] = [
  ["call", "呼び出し"], ["data", "データ"], ["job", "ジョブ"], ["screen", "画面"], ["doc", "設計書"], ["struct", "構造"], ["test", "テスト"],
];
// how the edge reads, source -> target
const EDGE_WORD: Record<string, string> = {
  CALLS: "呼ぶ", REFERENCES: "参照", READS: "読む", WRITES: "書く", POPULATES: "値を入れる", MAPS_TO: "対応", DEFINES: "定義",
  EXECUTES: "実行", RUNS: "実行", PRECEDES: "後続", RENDERS: "表示", BINDS: "結びつく", HANDLES: "処理", USES_MESSAGE: "メッセージ",
  INCLUDES: "取込", DESCRIBES: "説明", DOC_CRUD: "CRUD", CONTAINS: "含む", EXTENDS: "継承", IMPLEMENTED_BY: "実装", INJECTS: "注入",
  CONFIGURES: "設定", TESTS: "テスト", OBSERVED_AS: "観測",
};
const PER_SIDE = 14;
const COL_W = 300, ROW_H = 46;

const STYLE: any[] = [
  { selector: "node", style: {
      "background-color": "#0D121B", "border-width": 1, "border-color": "#4A5870", label: "data(label)", color: "#B9C3D2",
      "font-size": 10, "font-family": "JetBrains Mono, monospace", "text-valign": "center", "text-halign": "right",
      "text-margin-x": 6, width: 14, height: 14, "text-wrap": "ellipsis", "text-max-width": "230px" } },
  { selector: "node.left", style: { "text-halign": "left", "text-margin-x": -6 } },
  { selector: "node.center", style: { width: 22, height: 22, "border-color": "#E6EBF2", "border-width": 2, color: "#E6EBF2",
      "font-size": 12, "text-valign": "top", "text-halign": "center", "text-margin-y": -8, "text-margin-x": 0 } },
  { selector: "node.out", style: { "border-style": "dotted", color: "#8592A8" } },
  { selector: "node.confirmed", style: { "background-color": "#0F2A30", "border-color": "#6FE3E8", color: "#6FE3E8" } },
  { selector: "node.human", style: { "background-color": "#0F2A30", "border-color": "#6FE3E8", color: "#6FE3E8" } },
  { selector: "node.impacted", style: { "background-color": "#142238", "border-color": "#8FB8FF", color: "#8FB8FF" } },
  { selector: "node.uncertain", style: { "border-color": "#B98A3E", "border-style": "dashed", color: "#F5B451" } },
  { selector: "node.excluded", style: { opacity: 0.45 } },
  { selector: "node.k-db, node.k-sql", style: { shape: "barrel" } },
  { selector: "node.k-job", style: { shape: "hexagon" } },
  { selector: "node.k-file", style: { shape: "rhomboid" } },
  { selector: "node.k-doc", style: { shape: "tag" } },
  { selector: "node.k-screen", style: { shape: "round-rectangle" } },
  { selector: "node.k-test", style: { shape: "round-triangle" } },
  { selector: "node.expanded", style: { "border-width": 2 } },
  { selector: "node.more", style: { shape: "round-rectangle", width: 8, height: 8, "background-color": "#161F2D", "border-color": "#2A3548", color: "#74829A", "font-size": 9 } },
  { selector: "node:selected", style: { "overlay-color": "#E6EBF2", "overlay-opacity": 0.12, "overlay-padding": 6, color: "#E6EBF2" } },
  { selector: "edge", style: { width: 1, "line-color": "#2A3548", "curve-style": "bezier", "target-arrow-shape": "triangle",
      "target-arrow-color": "#4A5870", "arrow-scale": 0.7, label: "data(label)", "font-size": 8, color: "#74829A",
      "text-rotation": "autorotate", "text-background-color": "#0A0E15", "text-background-opacity": 1, "text-background-padding": "1px" } },
  { selector: "edge.hl", style: { "line-color": "#8FB8FF", "target-arrow-color": "#8FB8FF", color: "#B9C3D2", width: 1.6 } },
];

type Pos = { x: number; y: number };

export default function NeighborGraph({ run, centerId, onSelect, onCenter }: {
  run: RunState; centerId: string | null; onSelect: (id: string) => void; onCenter: (id: string) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const levels = useRef<Record<string, number>>({});
  const expanded = useRef<Set<string>>(new Set());
  const cache = useRef<Record<string, LinkGroup[]>>({});
  const [fam, setFam] = useState<Record<string, boolean>>(() => Object.fromEntries(FAMILY_LABEL.map(([k]) => [k, k !== "test"])));
  const famRef = useRef(fam); famRef.current = fam;
  const [busy, setBusy] = useState(false);
  const selectCb = useRef(onSelect); selectCb.current = onSelect;
  const centerCb = useRef(onCenter); centerCb.current = onCenter;

  const nodeClasses = (b: Brief, side: number, center = false) => {
    const c = run.candidates[b.id];
    const st = c ? statusOf(c) : "out";
    return `${st} k-${kindClass(b.kind, b.file)}${side < 0 ? " left" : ""}${center ? " center" : ""}`;
  };
  const label = (b: Brief) => {
    const c = run.candidates[b.id];
    const verb = b.name.replace(/^SQL\s+/, "").match(/^\s*(SELECT|INSERT|UPDATE|DELETE|MERGE|WITH)/i)?.[1].toUpperCase();
    const n = b.owner ? `${b.owner.name} の ${verb || "SQL"}` : (c?.name || b.name);
    const t = b.kind === "Template" ? n.split("/").pop() || n : n;
    return t.length > 38 ? t.slice(0, 37) + "…" : t;
  };

  const layout = () => {
    const cy = cyRef.current!;
    // stack nodes of each level, in insertion order, centred on y = 0
    const cols: Record<number, string[]> = {};
    cy.nodes().forEach((n) => { (cols[levels.current[n.id()] ?? 0] ||= []).push(n.id()); });
    const pos: Record<string, Pos> = {};
    for (const [lv, ids] of Object.entries(cols)) {
      const L = Number(lv);
      ids.forEach((id, i) => { pos[id] = { x: L * COL_W, y: (i - (ids.length - 1) / 2) * ROW_H }; });
    }
    cy.layout({ name: "preset", positions: (n: any) => pos[n.id()], animate: true, animationDuration: 300, fit: true, padding: 40 } as any).run();
  };

  const expand = async (id: string) => {
    const cy = cyRef.current;
    if (!cy || expanded.current.has(id)) return;
    setBusy(true);
    try {
      const groups = cache.current[id] || (cache.current[id] = (await api.context(id, run.id)).links);
      expanded.current.add(id);
      const L = levels.current[id] ?? 0;
      const on = famRef.current;
      const add: any[] = [];
      // keep going outwards: left of the centre only further callers / users, right only further callees
      const dirs = L < 0 ? (["in"] as const) : L > 0 ? (["out"] as const) : (["in", "out"] as const);
      for (const dir of dirs) {
        const items: { b: Brief; rel: string }[] = [];
        for (const g of groups) {
          if (g.dir !== dir || !on[FAMILY[g.rel] || "struct"]) continue;
          for (const b of g.items) items.push({ b, rel: g.rel });
        }
        const side = dir === "in" ? -1 : 1;
        let shown = 0;
        for (const { b, rel } of items) {
          const exists = cy.$id(b.id).nonempty() || add.some((a) => a.data.id === b.id);
          if (!exists) {
            if (shown >= PER_SIDE) continue;
            shown++;
            levels.current[b.id] = L + side;
            add.push({ group: "nodes", data: { id: b.id, label: label(b), kind: b.kind }, classes: nodeClasses(b, side) });
          }
          const [s, t] = dir === "in" ? [b.id, id] : [id, b.id];
          const eid = `${s}>${t}>${rel}`;
          if (cy.$id(eid).empty() && !add.some((a) => a.data.id === eid)) {
            add.push({ group: "edges", data: { id: eid, source: s, target: t, label: EDGE_WORD[rel] || rel.toLowerCase() } });
          }
        }
        const rest = items.filter(({ b }) => cy.$id(b.id).empty() && !add.some((a) => a.data.id === b.id)).length;
        if (rest > 0) {
          const mid = `more:${id}:${dir}`;
          levels.current[mid] = L + side;
          add.push({ group: "nodes", data: { id: mid, label: `ほか ${rest} 件（詳細のつながりタブ）`, for: id }, classes: `more${side < 0 ? " left" : ""}` });
          add.push({ group: "edges", data: { id: `${mid}>e`, source: dir === "in" ? mid : id, target: dir === "in" ? id : mid, label: "" } });
        }
      }
      cy.add(add.filter((a) => a.group === "nodes"));
      cy.add(add.filter((a) => a.group === "edges"));
      cy.$id(id).addClass("expanded");
      layout();
    } finally { setBusy(false); }
  };

  useEffect(() => {
    const cy = cytoscape({ container: ref.current!, style: STYLE, minZoom: 0.2, maxZoom: 3, autoungrabify: true });
    cy.on("tap", "node", (e) => {
      const n = e.target;
      if (n.hasClass("more")) { selectCb.current(n.data("for")); return; }
      cy.edges().removeClass("hl"); n.connectedEdges().addClass("hl");
      selectCb.current(n.id());
    });
    cy.on("dbltap", "node", (e) => { if (!e.target.hasClass("more")) expandRef.current(e.target.id()); });
    cyRef.current = cy;
    return () => cy.destroy();
  }, []);
  const expandRef = useRef(expand); expandRef.current = expand;

  // a new centre (or other relation families): start over from it
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !centerId) return;
    cy.elements().remove();
    levels.current = { [centerId]: 0 };
    expanded.current = new Set();
    const c = run.candidates[centerId];
    const b: Brief = { id: centerId, name: c?.name || centerId.replace(/^[a-z]+:/, ""), kind: c?.kind || "", layer: c?.layer || "code", file: c?.file };
    cy.add({ group: "nodes", data: { id: centerId, label: label(b) }, classes: nodeClasses(b, 0, true) });
    if (!c) api.node(centerId, run.id).then((d) => {
      const n = cy.$id(centerId);
      if (n.nonempty()) { n.data("label", label({ ...b, name: d.node.name, kind: d.node.kind })); n.classes(nodeClasses({ ...b, kind: d.node.kind, file: d.node.file }, 0, true)); }
    }).catch(() => {});
    expand(centerId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [centerId, fam]);

  const sel = centerId ? null : <p className="ng-empty muted">リストかグラフで要素を選ぶと、そこからのつながり（左に呼び出し元・使っている側、右に呼び出し先・使っているもの）をたどれます。</p>;
  return (
    <div className="graph-wrap ng">
      <div className="ng-bar">
        {FAMILY_LABEL.map(([k, l]) => (
          <button key={k} className={fam[k] ? "chip on" : "chip"} aria-pressed={fam[k]} onClick={() => setFam({ ...fam, [k]: !fam[k] })}>{l}</button>
        ))}
        <span className="muted tiny">クリックで詳細・ダブルクリックでその先を展開{busy ? "（読み込み中…）" : ""}</span>
        {centerId && <button className="btn small ghost" onClick={() => { const s = cyRef.current?.$("node:selected"); if (s && s.nonempty()) centerCb.current(s.id()); }}>選択中を中心にする</button>}
      </div>
      <div ref={ref} className="graph" />
      {sel}
      <div className="ng-legend tiny muted">
        <span>← 呼び出し元・使っている側</span><span>中心</span><span>呼び出し先・使っているもの →</span>
        <span className="sep" />{(["code", "screen", "db", "job", "file", "doc"] as const).map((k) => <span key={k}>{shapeWord(k)} {CLASS_LABEL[k]}</span>)}
        <span>点線の枠 = 候補外</span>
      </div>
    </div>
  );
}

const shapeWord = (k: string) => ({ code: "●", screen: "▢", db: "⬭", job: "⬡", file: "▱", doc: "⬠" } as Record<string, string>)[k] || "●";
