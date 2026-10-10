import type { Filters } from "./Graph";
import type { Candidate, RunState } from "./types";

/** Candidates shown under the current filters (the list and the totals use the same rule). */
export function visibleCandidates(run: RunState, f: Filters): Candidate[] {
  const q = f.query.trim().toLowerCase();
  return Object.values(run.candidates)
    .filter((c) => f.layers[c.layer] !== false)
    .filter((c) => !f.onlyOpen || !c.decision)
    .filter((c) => c.decision || (c.confidence ?? c.score) >= f.minConf)
    .filter((c) => !q || c.name.toLowerCase().includes(q) || (c.file || "").toLowerCase().includes(q)
      || (c.reason || "").toLowerCase().includes(q));
}

export function isFiltered(f: Filters): boolean {
  return Object.values(f.layers).some((on) => !on) || f.onlyOpen || f.minConf > 0 || !!f.query.trim();
}

export type ElementSize = { hours: number; expected: number; status: "confirmed" | "pending" | "none";
  probability?: number; notes: string[] };

/** One element's share of the estimate: confirmed hours, or hours x likelihood while undecided. */
export function elementSize(run: RunState, id: string): ElementSize {
  const p = run.estimate?.per_node?.[id];
  if (!p) return { hours: 0, expected: 0, status: "none", notes: [] };
  return { hours: p.h, expected: p.e, status: p.s, probability: p.p, notes: p.n || [] };
}

export type Sizing = { count: number; decided: number; confirmedH: number; expectedH: number; maxH: number;
  days: number; low: number; high: number };

/** Effort of any subset, with the same coefficients as the whole-run estimate. */
export function sizeOf(run: RunState, cs: Candidate[]): Sizing {
  const e = run.estimate || {};
  const k = (e.overhead ?? 1.3) * (e.calibration ?? 1);
  const hpd = e.hours_per_day ?? 8;
  let confirmedH = 0, expectedH = 0, pendingMax = 0;
  for (const c of cs) {
    const s = elementSize(run, c.node_id);
    if (s.status === "confirmed") confirmedH += s.hours;
    else if (s.status === "pending") { expectedH += s.expected; pendingMax += s.hours; }
  }
  const r = (x: number) => Math.round(x * 10) / 10;
  return { count: cs.length, decided: cs.filter((c) => c.decision).length, confirmedH: r(confirmedH),
    expectedH: r(expectedH), maxH: r(confirmedH + pendingMax),
    days: r(((confirmedH + expectedH) * k) / hpd), low: r((confirmedH * k) / hpd), high: r(((confirmedH + pendingMax) * k) / hpd) };
}

export const LAYERS_JA: [string, string][] = [["feature", "機能"], ["code", "コード"], ["data", "データ"], ["test", "テスト"], ["doc", "設計書"]];
