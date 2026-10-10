export type Decision = { status: "confirmed" | "excluded"; reason: string; actor: string; at: number; inherited?: boolean } | null;

export interface Candidate {
  node_id: string;
  kind: string;
  layer: string;
  name: string;
  file?: string | null;
  line?: number | null;
  score: number;
  origin: string;
  intent_ids: string[];
  parent?: string | null;
  via_rel?: string | null;
  path: string[];
  seed_reason?: string | null;
  verdict?: "impacted" | "uncertain" | "not_impacted" | null;
  confidence?: number | null;
  reason?: string | null;
  change_type?: string | null;
  difficulty?: string | null;
  decision: Decision;
  assignee?: string | null;
  comments: { author: string; text: string; at: number }[];
}

export interface Check {
  key: string;
  title: string;
  detail: string;
  ok: boolean;
  open: string[];
}

export interface EstimateLine {
  node_id: string;
  name: string;
  kind: string;
  change_type: string;
  hours: number;
  notes: string[];
  tested: boolean;
  probability?: number;
}

export interface RunState {
  id: string;
  parent_id?: string | null;
  title: string;
  requirement: string;
  instruction?: string | null;
  status: "running" | "done" | "error";
  phase: string;
  progress: number;
  llm: string;
  intents: { id: string; summary: string; change_type: string; keywords: string[]; new_terms: string[]; pattern?: string | null }[];
  candidates: Record<string, Candidate>;
  checks: Check[];
  estimate: {
    days?: number; low?: number; high?: number; confirmed_hours?: number; pending_expected_hours?: number;
    overhead?: number; calibration?: number; calibration_samples?: number;
    lines?: EstimateLine[]; pending?: EstimateLine[];
  };
  grep: { terms?: string[]; hits?: { file: string; line: number; term: string; node_id: string; text: string }[] };
  shadow: { node_ids?: string[]; only?: string[] };
  approved: { actor: string; at: number; estimate_days: number } | null;
  error?: string;
  created_at: number;
}

export interface RunSummary {
  id: string;
  parent_id?: string | null;
  title: string;
  requirement: string;
  created_at: number;
  status: string;
  phase: string;
  progress: number;
}

export interface RunEvent {
  seq: number;
  ts: number;
  type: string;
  data: any;
}

export interface NodeDetail {
  node: { id: string; kind: string; name: string; file?: string; line?: number; end_line?: number; layer: string; props: any; text?: string };
  code: { n: number; text: string }[];
  highlight?: number[];
  neighbors: { dir: string; rel: string; id: string; name: string; kind: string; line?: number; via?: string }[];
  callers?: NodeDetail["neighbors"];
  callees?: NodeDetail["neighbors"];
  docs: { id: string; name: string; file: string; line: number; text: string }[];
  tests: NodeDetail["neighbors"];
  history: { hash: string; date: string; author: string; subject: string }[];
}
