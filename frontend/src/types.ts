export type Decision = { status: "confirmed" | "excluded"; reason: string; actor: string; at: number; inherited?: boolean } | null;

export interface Candidate {
  node_id: string;
  kind: string;
  layer: string;
  group?: string | null;
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
  judged_by?: string | null;
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
  llm_usage?: { ai_items: number; reused: number; rule_low_score: number; rule_cap: number; rule: number;
    fallback: number; calls: number; input_tokens: number; output_tokens: number;
    cost_usd?: number; cost_jpy?: number; unpriced_calls?: number; model?: string | null; jpy_per_usd?: number | null };
  params?: { llm?: { min_score: number; max_items: number | "unlimited"; reuse_verdicts: boolean } } & Record<string, any>;
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
  graph?: string;
}

export interface GraphInfo { repo: string; ref?: string | null; snapshot?: string; docs_dir?: string | null; commit?: string; nodes?: number; edges?: number }

export interface IndexReport {
  nodes: number; edges: number; files: number; parsed: number; cached: number; seconds: number;
  seconds_extract?: number; seconds_link?: number; commit?: string;
  by_kind: Record<string, number>; by_extractor?: Record<string, number>; rules?: string[];
  unresolved: Record<string, number>; unresolved_samples: Record<string, { key: string; via: string; file: string }[]>;
  external?: Record<string, number>;
  runtime?: { statements: number; matched: number; unmatched: number; unmatched_samples?: { id: string; sql: string; calls?: number }[] };
  drift?: { columns_only_in_db?: string[]; columns_only_in_ddl?: string[]; tables_not_in_catalog?: string[]; counts?: Record<string, number>; note?: string };
  errors: number; error_samples?: { file: string; error: string }[];
}

export interface UsageTotals { calls: number; input_tokens: number; output_tokens: number; cost_usd: number;
  cost_jpy: number; unpriced_calls?: number }
export interface WorkspaceUsage { today: UsageTotals; month: UsageTotals; by_day: (UsageTotals & { date: string })[];
  jpy_per_usd: number; pricing_source: string }

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
  neighbors: { dir: string; rel: string; id: string; name: string; kind: string; line?: number; via?: string; src?: string; conf?: number }[];
  callers?: NodeDetail["neighbors"];
  callees?: NodeDetail["neighbors"];
  docs: { id: string; name: string; file: string; line: number; text: string }[];
  tests: NodeDetail["neighbors"];
  history: { hash: string; date: string; author: string; subject: string }[];
}
