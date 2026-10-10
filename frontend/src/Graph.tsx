import cytoscape, { Core, ElementDefinition } from "cytoscape";
import { useEffect, useRef } from "react";
import type { Candidate, RunState } from "./types";

export type Filters = { layers: Record<string, boolean>; onlyOpen: boolean; minConf: number; query: string };

const RING: Record<string, number> = { feature: 2, code: 3, data: 4, test: 5, doc: 6 };
const BASE_R = [0, 70, 150, 250, 350, 440, 520];

/** Radial layout: rings by layer; within a ring, nodes keep the circular order of a
 * depth-first walk from the requirement so each subtree stays in one sector. */
function radialPositions(cy: Core): Record<string, { x: number; y: number }> {
  const children: Record<string, string[]> = {};
  cy.edges().forEach((e) => { (children[e.source().id()] ||= []).push(e.target().id()); });
  const order: Record<string, number> = {};
  let seq = 0;
  const seen = new Set<string>();
  const walk = (id: string) => {
    if (seen.has(id)) return;
    seen.add(id); order[id] = seq++;
    for (const c of children[id] || []) walk(c);
  };
  walk("__req");
  cy.nodes().forEach((n) => { if (!(n.id() in order)) order[n.id()] = seq++; });
  const rings: Record<number, string[]> = {};
  cy.nodes().forEach((n) => {
    const r = n.hasClass("requirement") ? 0 : n.hasClass("intent") ? 1 : (RING[n.data("layer")] ?? 3);
    (rings[r] ||= []).push(n.id());
  });
  const pos: Record<string, { x: number; y: number }> = {};
  let prevR = 0;
  for (let r = 0; r <= 6; r++) {
    const ids = (rings[r] || []).sort((a, b) => order[a] - order[b]);
    if (!ids.length) continue;
    const radius = r === 0 ? 0 : Math.max(BASE_R[r], prevR + 70, (ids.length * 30) / (2 * Math.PI));
    prevR = radius;
    ids.forEach((id, i) => {
      const a = -Math.PI / 2 + (2 * Math.PI * (i + 0.5)) / ids.length;
      pos[id] = { x: radius * Math.cos(a), y: radius * Math.sin(a) };
    });
  }
  return pos;
}

export function statusOf(c: Candidate): string {
  if (c.decision?.status === "confirmed") return c.origin === "human" ? "human" : "confirmed";
  if (c.decision?.status === "excluded") return "excluded";
  if (c.verdict === "impacted") return "impacted";
  if (c.verdict === "uncertain") return "uncertain";
  if (c.verdict === "not_impacted") return "low";
  return "pending";
}

function shortName(c: Candidate) {
  const n = c.name.replace(/\(\)$/, "()");
  if (c.kind === "Template") return n.split("/").pop() || n;
  if (n.length > 34) return n.slice(0, 32) + "…";
  return n;
}

const STYLE: any[] = [
  { selector: "node", style: {
      "background-color": "#0D121B", "border-width": 1, "border-color": "#4A5870", "label": "data(label)",
      "color": "#B9C3D2", "font-size": 8, "font-family": "JetBrains Mono, monospace", "text-valign": "bottom",
      "text-margin-y": 4, "width": "mapData(score, 0, 1, 10, 26)", "height": "mapData(score, 0, 1, 10, 26)",
      "min-zoomed-font-size": 11, "text-background-color": "#0A0E15", "text-background-opacity": 0.7,
      "text-background-padding": "1px", "transition-property": "background-color, border-color, opacity, width, height",
      "transition-duration": 300 } },
  { selector: "node.requirement", style: { "background-color": "#E6EBF2", "border-color": "#E6EBF2", width: 54, height: 54,
      color: "#0A0E15", "text-valign": "center", "text-margin-y": 0, "font-size": 10, "font-weight": 500, "text-background-opacity": 0 } },
  { selector: "node.intent", style: { "background-color": "#1C2636", "border-color": "#9AA6B8", width: 24, height: 24, shape: "diamond",
      color: "#E6EBF2", "font-size": 10 } },
  { selector: "node.confirmed", style: { "background-color": "#0F2A30", "border-color": "#6FE3E8", color: "#6FE3E8", "border-width": 1.5 } },
  { selector: "node.human", style: { "background-color": "#1F1838", "border-color": "#A78BFA", color: "#A78BFA", "border-width": 1.5 } },
  { selector: "node.impacted", style: { "background-color": "#2E220F", "border-color": "#F5B451", color: "#F5B451" } },
  { selector: "node.uncertain", style: { "background-color": "#1A1610", "border-color": "#B98A3E", "border-style": "dashed", color: "#D9B37A" } },
  { selector: "node.low", style: { "border-style": "dashed", "border-color": "#3A4558", color: "#5E6A7E" } },
  { selector: "node.pending", style: { "border-color": "#4A5870" } },
  { selector: "node.excluded", style: { opacity: 0.35, "border-color": "#2A3548", color: "#4F5B6E" } },
  { selector: "node.layer-test", style: { shape: "round-rectangle" } },
  { selector: "node.layer-data", style: { shape: "barrel" } },
  { selector: "node.layer-doc", style: { shape: "tag" } },
  { selector: "node.layer-feature", style: { shape: "hexagon" } },
  { selector: "node.major", style: { "font-size": 10, "min-zoomed-font-size": 4 } },
  { selector: "node.dim", style: { opacity: 0.12 } },
  { selector: "node.hit", style: { "underlay-color": "#6FE3E8", "underlay-opacity": 0.25, "underlay-padding": 6 } },
  { selector: "node:selected", style: { "overlay-color": "#E6EBF2", "overlay-opacity": 0.12, "overlay-padding": 6,
      "border-width": 2.5, color: "#E6EBF2", "z-index": 99 } },
  { selector: "edge", style: { width: 0.6, "line-color": "#1C2636", "curve-style": "straight", opacity: 0.8,
      "target-arrow-shape": "none" } },
  { selector: "edge.path", style: { width: 1.6, "line-color": "#F5B451", opacity: 1, "z-index": 50,
      "target-arrow-shape": "triangle", "target-arrow-color": "#F5B451", "arrow-scale": 0.6 } },
  { selector: ".hidden", style: { display: "none" } },
];

export default function Graph({ run, filters, selected, onSelect, focusId }: {
  run: RunState | null;
  filters: Filters;
  selected: string[];
  onSelect: (ids: string[]) => void;
  focusId?: string | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const countRef = useRef(0);
  const layoutTimer = useRef<number | null>(null);
  const runIdRef = useRef<string | null>(null);
  const selectCb = useRef(onSelect);
  selectCb.current = onSelect;

  useEffect(() => {
    const cy = cytoscape({
      container: ref.current!, style: STYLE, minZoom: 0.15, maxZoom: 3.5,
      boxSelectionEnabled: true, selectionType: "additive",
    });
    const emit = () => selectCb.current(cy.$("node:selected").filter((n) => !n.hasClass("requirement") && !n.hasClass("intent")).map((n) => n.id()));
    cy.on("tap", "node", (e) => {
      const n = e.target;
      const multi = (e.originalEvent as MouseEvent)?.shiftKey || (e.originalEvent as MouseEvent)?.metaKey;
      if (!multi) { cy.$("node:selected").unselect(); }
      if (multi && n.selected()) n.unselect(); else n.select();
      setTimeout(emit, 0);
    });
    cy.on("tap", (e) => { if (e.target === cy) { cy.$("node:selected").unselect(); emit(); } });
    cy.on("boxend", () => setTimeout(emit, 0));
    cyRef.current = cy;
    return () => cy.destroy();
  }, []);

  // sync elements
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    if (!run) { cy.elements().remove(); countRef.current = 0; return; }
    if (runIdRef.current !== run.id) { cy.elements().remove(); countRef.current = 0; runIdRef.current = run.id; }
    const add: ElementDefinition[] = [];
    if (cy.$id("__req").empty()) {
      add.push({ group: "nodes", data: { id: "__req", label: run.title.slice(0, 14), score: 1 }, classes: "requirement" });
    }
    for (const it of run.intents) {
      const id = `intent:${it.id}`;
      if (cy.$id(id).empty()) {
        add.push({ group: "nodes", data: { id, label: `${it.id} ${it.summary.slice(0, 14)}`, score: 1 }, classes: "intent" });
        add.push({ group: "edges", data: { id: `e:__req:${id}`, source: "__req", target: id } });
      }
    }
    const cands = Object.values(run.candidates);
    for (const c of cands) {
      const st = statusOf(c);
      const major = c.score >= 0.6 || st === "confirmed" || st === "human";
      const cls = `${st} layer-${c.layer}${major ? " major" : ""}`;
      const ex = cy.$id(c.node_id);
      if (ex.empty()) {
        const parent = c.parent ? cy.$id(c.parent) : null;
        const p0 = parent && parent.nonempty() ? parent.position() : { x: 0, y: 0 };
        add.push({ group: "nodes", data: { id: c.node_id, label: shortName(c), score: c.score, layer: c.layer }, classes: cls,
          position: { x: p0.x + (Math.random() - 0.5) * 20, y: p0.y + (Math.random() - 0.5) * 20 } });
      } else {
        ex.data("score", c.score);
        ex.classes(cls + (ex.selected() ? "" : ""));
      }
    }
    if (add.length) cy.add(add.filter((a) => a.group === "nodes"));
    const edges: ElementDefinition[] = [];
    for (const c of cands) {
      const src = c.parent && run.candidates[c.parent] ? c.parent : (c.intent_ids[0] ? `intent:${c.intent_ids[0]}` : "__req");
      const id = `e:${src}:${c.node_id}`;
      if (cy.$id(id).empty() && !cy.$id(src).empty()) edges.push({ group: "edges", data: { id, source: src, target: c.node_id } });
    }
    cy.add([...add.filter((a) => a.group === "edges" && cy.$id(a.data.id!).empty()), ...edges]);

    if (cy.nodes().length !== countRef.current) {
      countRef.current = cy.nodes().length;
      if (!layoutTimer.current) {
        layoutTimer.current = window.setTimeout(() => {
          layoutTimer.current = null;
          const pos = radialPositions(cy);
          cy.layout({ name: "preset", positions: (n: any) => pos[n.id()], animate: true, animationDuration: 500,
            fit: true, padding: 50 } as any).run();
        }, 350);
      }
    }
  }, [run]);

  // filters
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !run) return;
    const q = filters.query.trim().toLowerCase();
    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const c = run.candidates[n.id()];
        if (!c) return;
        let hidden = !filters.layers[c.layer];
        if (filters.onlyOpen && c.decision) hidden = true;
        if ((c.confidence ?? c.score) < filters.minConf && !c.decision) hidden = true;
        n.toggleClass("hidden", hidden);
        n.toggleClass("hit", !!q && (c.name.toLowerCase().includes(q) || (c.file || "").toLowerCase().includes(q)));
      });
    });
  }, [filters, run]);

  // selection + path highlight
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !run) return;
    cy.batch(() => {
      cy.edges().removeClass("path");
      cy.nodes().forEach((n) => { if (selected.includes(n.id()) !== n.selected()) { if (selected.includes(n.id())) n.select(); else n.unselect(); } });
      for (const id of selected) {
        const c = run.candidates[id];
        if (!c) continue;
        const chain = [c.intent_ids[0] ? `intent:${c.intent_ids[0]}` : "__req", ...c.path];
        for (let i = 0; i < chain.length - 1; i++) cy.$id(`e:${chain[i]}:${chain[i + 1]}`).addClass("path");
        if (!c.parent) cy.$id(`e:${chain[0]}:${c.node_id}`).addClass("path");
      }
    });
  }, [selected, run]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !focusId) return;
    const n = cy.$id(focusId);
    if (n.nonempty()) cy.animate({ center: { eles: n }, zoom: Math.max(cy.zoom(), 1.2) } as any, { duration: 400 });
  }, [focusId]);

  return (
    <div className="graph-wrap">
      <div ref={ref} className="graph" />
      <div className="graph-tools">
        <button className="icon" aria-label="縮小" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() * 0.8)}>−</button>
        <button className="icon" aria-label="全体表示" onClick={() => cyRef.current?.fit(undefined, 40)}>⤢</button>
        <button className="icon" aria-label="拡大" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.25)}>＋</button>
      </div>
    </div>
  );
}
