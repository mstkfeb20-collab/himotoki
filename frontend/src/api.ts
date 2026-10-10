import type { GraphInfo, IndexReport, NodeDetail, RunEvent, RunState, RunSummary } from "./types";

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    throw new Error("認証が必要です。管理者から受け取った ?token=… 付きのURLでビュワーを開き直してください。");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail ?? msg; } catch { /* plain text */ }
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  status: (graph?: string | null) => req<any>("GET", `/api/status${graph ? `?graph=${encodeURIComponent(graph)}` : ""}`),
  graphs: () => req<{ default: string | null; graphs: Record<string, GraphInfo> }>("GET", "/api/graphs"),
  index: (repo: string, docs_dir?: string, ref?: string, graph?: string) =>
    req<{ status: string; graph: string }>("POST", "/api/index",
      { repo, docs_dir: docs_dir || null, ref: ref || null, graph: graph || null }),
  indexStatus: (graph?: string | null) => req<any>("GET", `/api/index${graph ? `?graph=${encodeURIComponent(graph)}` : ""}`),
  indexReport: (graph?: string | null) =>
    req<IndexReport>("GET", `/api/index/report${graph ? `?graph=${encodeURIComponent(graph)}` : ""}`),
  diffRun: (base: string, head: string, graph?: string | null, title?: string) =>
    req<{ id: string; changed_elements: number; added_files: string[] }>("POST", "/api/runs/diff", { base, head, graph, title }),
  scope: (id: string) => req<{ groups: any; markdown: string }>("GET", `/api/runs/${id}/scope`),
  runs: () => req<RunSummary[]>("GET", "/api/runs"),
  run: (id: string) => req<RunState>("GET", `/api/runs/${id}`),
  createRun: (requirement: string, opts: { title?: string; depth?: number; pace?: number; graph?: string | null }) =>
    req<{ id: string }>("POST", "/api/runs", { requirement, ...opts }),
  decide: (id: string, node_id: string, status: string, reason = "", extra: Record<string, unknown> = {}) =>
    req<any>("POST", `/api/runs/${id}/decisions`, { node_id, status, reason, ...extra }),
  bulk: (id: string, items: { node_id: string; status: string; reason?: string }[]) =>
    req<any>("POST", `/api/runs/${id}/decisions/bulk`, { items }),
  add: (id: string, body: { node_id?: string; query?: string; reason: string }) =>
    req<any>("POST", `/api/runs/${id}/candidates`, body),
  comment: (id: string, node_id: string, text: string, assignee?: string | null) =>
    req<any>("POST", `/api/runs/${id}/comments`, { node_id, text, assignee }),
  reinvestigate: (id: string, node_ids: string[], instruction: string, pace = 0.03) =>
    req<{ id: string }>("POST", `/api/runs/${id}/reinvestigate`, { node_ids, instruction, pace }),
  approve: (id: string) => req<any>("POST", `/api/runs/${id}/approve`),
  diff: (a: string, b: string) => req<any>("GET", `/api/runs/${a}/diff/${b}`),
  ask: (id: string, node_ids: string[], question: string) =>
    req<{ answer: string; llm: string }>("POST", `/api/runs/${id}/ask`, { node_ids, question }),
  report: (id: string) => req<string>("GET", `/api/runs/${id}/report`),
  node: (nodeId: string, runId?: string) =>
    req<NodeDetail>("GET", `/api/nodes/${encodeURIComponent(nodeId)}${runId ? `?run_id=${runId}` : ""}`),
  search: (q: string) => req<{ id: string; name: string; kind: string; file?: string; score: number }[]>(
    "GET", `/api/search?q=${encodeURIComponent(q)}`),
  events: (id: string) => req<RunEvent[]>("GET", `/api/runs/${id}/events`),
};

export function openRunSocket(id: string, onEvent: (e: RunEvent) => void): () => void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws: WebSocket | null = null;
  let closed = false;
  let retry = 0;
  const connect = () => {
    ws = new WebSocket(`${proto}://${location.host}/ws/runs/${id}`);
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
    ws.onopen = () => { retry = 0; };
    ws.onclose = () => {
      if (!closed && retry < 5) setTimeout(connect, 500 * ++retry);
    };
  };
  connect();
  return () => { closed = true; ws?.close(); };
}
