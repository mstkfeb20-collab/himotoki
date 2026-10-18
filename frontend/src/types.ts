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
  tx_risks?: TxRisk[];
  learned?: { type: string; factor: number; message: string }[];
  data_flow?: boolean;
  tx?: { boundary: string; source: string; members: number; tables: string[]; remote: string[] };
}

export interface TxRisk {
  code: string;
  level: "high" | "medium" | "low";
  message: string;
  at: string;
  root?: string | null;
  line?: number | null;
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
    overhead?: number; calibration?: number; calibration_samples?: number; hours_per_day?: number;
    per_node?: Record<string, { h: number; e: number; s: "confirmed" | "pending"; p?: number; n?: string[] }>;
    lines?: EstimateLine[]; pending?: EstimateLine[];
  };
  grep: { terms?: string[]; hits?: { file: string; line: number; term: string; node_id: string; text: string }[] };
  shadow: { node_ids?: string[]; only?: string[] };
  approved: { actor: string; at: number; estimate_days: number } | null;
  verification?: Verification | null;
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

export interface GraphInfo { repo: string | null; repos?: Record<string, string>; ref?: string | null; snapshot?: string; docs_dir?: string | null; commit?: string; nodes?: number; edges?: number }

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

export interface VerifyRow { node_id: string; name: string; kind: string; file?: string | null; change?: string; why?: string; why_ja?: string; candidate_reason?: string | null }
export interface Verification {
  base: string; head: string; at: number; changed_elements: number; added_files: string[];
  hit: VerifyRow[]; missed: VerifyRow[]; untouched: VerifyRow[];
  recall: number | null; precision: number | null; missed_by_reason: Record<string, number>;
  calibration?: { calibration: number; samples: number }; predicted_days?: number; actual_days?: number;
  markdown?: string;
}
export interface Handoff {
  run_id: string; title: string; requirement: string; approved: RunState["approved"]; warnings: string[];
  estimate_days?: number; files: { file: string; items: { node_id: string; name: string }[] }[];
  other_targets: { node_id: string; name: string }[]; pending: { node_id: string; name: string }[];
  excluded: { node_id: string; name: string }[]; tests_to_run: { name: string }[]; markdown: string;
}

export interface KnowledgeSummary {
  decisions: number; analyses: number; verifications: number;
  thresholds: { pattern: number; edge: number; node: number };
  patterns: { key: string; rel: string; parent_kind: string; kind: string; confirmed: number; excluded: number;
    mult: number; active: boolean; ignored: boolean }[];
  nodes: { node_id: string; name: string; mult: number; notes: { type: string; message: string }[];
    confirmed: number; excluded: number; missed: number; untouched: number; ignored: boolean }[];
  links: { key: string; src: string; src_name: string; dst: string; dst_name: string; count: number; weight: number; ignored: boolean }[];
}


/** Design documents: what the extractor read from each one (himotoki/doccheck.py). */
export interface DocSummary { path: string; name: string; rows: number; sections: number; linked: number; }
export interface DocTarget { id: string; name: string; kind: string; file?: string | null; conf: number; rel: string; }
export interface DocRow { id: string; cell: string; name: string; fields: Record<string, string>; targets: DocTarget[]; named: string[]; }
export interface DocColumn { cell?: string; label: string; as: string | null; as_ja?: string | null; used?: boolean; }
export interface DocSheet {
  name: string; rows: number; cells: number; merged: number; read_cells: number;
  tables: { header_row: number; columns: DocColumn[]; unrecognised: string[]; rows: number; linked: number; sample: DocRow[] }[];
  crud_rows: number; crud_tables: string[];
  uncovered: { row: number; cells: [string, string][] }[]; uncovered_rows: number; uncovered_cells: number;
  header_candidates: { row: number; labels: string[] }[];
  shapes: number; shape_texts: string[]; pictures: number; charts: number; connectors: number; comments: number;
}
export interface DocCheck {
  path: string; format: "xlsx" | "docx" | "text"; errors: string[];
  summary: { rows_read: number; sections: number; linked_rows: number; linked_sections: number; graph: boolean;
             cells: number; read_cells: number; unread_drawings: number };
  findings: { level: "error" | "warn" | "info"; where: string; message: string }[];
  sheets?: DocSheet[];
  sections?: { id: string; title: string; chars: number; cut: boolean; targets: DocTarget[]; mentions: number }[];
  tables?: { name: string; rows: number; read_rows: number; linked: number; header: DocColumn[]; recognised: boolean; sample: DocRow[] }[];
  textboxes?: string[]; pictures?: number; charts?: number;
}

/** one element as shown in lists of related elements */
export interface Brief {
  id: string; name: string; kind: string; layer: string; file?: string | null; line?: number | null;
  at?: number | null; ops?: string | null; src?: string | null; conf?: number | null;
  owner?: { id: string; name: string; kind: string } | null;
}
export interface LinkGroup { label: string; rel: string; dir: "in" | "out"; total: number; items: Brief[] }
export interface TableView {
  id: string; name: string; logical?: string | null; schema?: string | null;
  definitions: { id: string; file: string; line?: number | null; type?: string | null }[];
  columns: { id: string; name: string; type?: string | null; comment?: string | null; focus?: boolean }[];
}
export interface DataUser extends Brief { ops: string; via: string[] }
export type DataContext =
  | { type: "column"; schema?: string | null; table: TableView | null;
      column: { id: string; name: string; logical?: string | null; types: Record<string, string>; definitions: TableView["definitions"] };
      users: DataUser[]; table_users: DataUser[]; docs: Brief[] }
  | { type: "table"; schema?: string | null; table: TableView; users: DataUser[]; docs: Brief[] }
  | { type: "touches"; tables: { id: string; name: string; kind: string; logical?: string | null; schema?: string | null; ops: string; columns: string[]; at: number[] }[] };
export interface ChainStep extends Brief { depth: number; common?: boolean }
export interface JobFlow {
  job: Brief & { comment?: string | null };
  jobnet: (Brief & { schedule?: string | null }) | null;
  net_jobs: { id: string; name: string; full: string; kind: string; comment?: string | null; next: string[] }[];
  before: Brief[]; after: Brief[]; chain: ChainStep[];
  data: { id: string; name: string; kind: string; logical?: string | null; ops: string; columns: string[]; by: string[] }[];
  downstream: { file: Brief; readers: (Brief & { jobs: string[] })[] }[];
}
export type FlowContext =
  | { type: "job"; focus: string; flows: JobFlow[]; more_jobs: number }
  | { type: "jobnet"; jobnet: Brief & { schedule?: string | null }; parent: Brief | null; jobs: JobFlow["net_jobs"] };
export interface NodeContext { links: LinkGroup[]; data: DataContext | null; flow: FlowContext | null }
export interface FileView {
  path: string; office: boolean; lines: { n: number; text: string }[]; truncated: boolean; total?: number;
  outline: { id: string; name: string; kind: string; line: number; end_line?: number | null }[];
}
export interface IssueDrafts {
  run_id: string; split: "one" | "subsystem"; warnings: string[];
  issues: { key: string; title: string; labels: string[]; body: string }[];
}
