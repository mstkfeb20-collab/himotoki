"""Impact analysis engine.

A run goes through these phases, emitting an event for every step so the
viewer can show the investigation as it happens:

  1. structure   requirement -> change intents (LLM or rule-based)
  2. seed        intents -> starting nodes (text similarity, exact identifiers,
                 change patterns such as "add an attribute to an entity")
  3. expand      dependency closure over the code graph with typed propagation
  4. judge       verdict / confidence / reason per candidate (LLM or score-based)
  5. grep        keyword sweep over every text file (static-analysis blind spots)
  6. shadow      independent re-exploration with a different strategy
  7. crud        every element touching an affected table
  8. estimate    effort breakdown
Then people (or an agent) confirm / exclude candidates, add missing ones,
re-investigate, and finally approve the result once every completeness
condition holds.
"""
from __future__ import annotations

import logging
from functools import lru_cache
import os
import re
import threading
import time
import uuid
from collections import defaultdict

from ..model import EDITABLE, Kind, LAYER, Rel
from ..security import max_file_bytes, safe_join
from ..settings import LLMBudget, load_llm_budget
from ..store.base import GraphStore
from .estimate import Estimator
from .llm import RuleBasedLLM, make_llm
from .runstore import EventBus, RunStore
from .search import NodeSearch
from ..rules import load_rules

log = logging.getLogger("codegraph.engine")

SINKS = {Kind.TEST_CLASS, Kind.TEST_METHOD, Kind.DOC, Kind.DOC_ENTITY, Kind.FEATURE, Kind.SQL_FILE, Kind.CONFIG,
         Kind.RUNTIME_SQL}
SEED_EXCLUDE = {Kind.TEST_CLASS, Kind.TEST_METHOD, Kind.DOC, Kind.DOC_ENTITY, Kind.CONFIG, Kind.RUNTIME_SQL,
                Kind.XML_ELEMENT, Kind.FEATURE, Kind.SQL_FILE, Kind.BEAN, Kind.REQUIREMENT, Kind.INTENT}


def seedable(n) -> bool:
    return n.kind not in SEED_EXCLUDE and not (n.file and ("src/test/" in n.file or "/test/" in n.file))


STOP_TERMS = {"name", "type", "date", "list", "page", "view", "form", "find", "new", "edit", "update",
              "owner", "owners", "pet", "pets", "error", "message", "value", "data", "info", "show",
              "create", "add", "get", "set", "the", "and", "for", "with", "this", "that", "from", "string",
              "integer", "id", "description", "field", "column", "validation", "required", "address",
              "first", "last", "test", "tests", "main", "java", "html"}

REL_JA = {
    Rel.BINDS: "を画面で表示・入力している",
    Rel.MAPS_TO: "が対応付けられた列・テーブル",
    Rel.DEFINES: "を定義しているSQL",
    Rel.POPULATES: "に初期データを投入している",
    Rel.TESTS: "を検証しているテスト",
    Rel.HANDLES: "を処理するエンドポイント",
    Rel.DESCRIBES: "を記述している設計書",
    Rel.CALLS: "を呼び出している",
    Rel.REFERENCES: "を参照している",
    Rel.CONTAINS: "を含んでいる",
    Rel.EXTENDS: "を継承している",
    Rel.INCLUDES: "を部品として読み込んでいる",
    Rel.EXECUTES: "を実行している",
    Rel.READS: "を読んでいる",
    Rel.WRITES: "を書き込んでいる",
    Rel.IMPLEMENTED_BY: "の実装",
    Rel.RUNS: "を起動している",
    Rel.PRECEDES: "の後続ジョブ",
    Rel.OBSERVED_AS: "として実行時に観測されたSQL",
    Rel.DOC_CRUD: "をCRUD表で扱っている設計書",
    Rel.INJECTS: "を注入している",
    Rel.CONFIGURES: "を設定している",
    Rel.USES_MESSAGE: "を使っている",
}
TEXT_EXT = (".java", ".html", ".sql", ".properties", ".yml", ".yaml", ".xml", ".md", ".txt", ".js",
            ".ts", ".css", ".scss", ".json", ".kt", ".jsp", ".jmx", ".gradle")


def _now() -> float:
    return time.time()


_OFFICE_EXT = (".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pptx", ".pdf")


def _is_office(rel: str | None) -> bool:
    return bool(rel) and rel.lower().endswith(_OFFICE_EXT)


def _office_summary(n) -> str:
    """Readable text of an Office row or section from the index (no file access)."""
    fields = n.props.get("fields")
    crud = n.props.get("crud")
    head = [f"{n.props.get('cell') or n.props.get('title') or ''}".strip()]
    if isinstance(fields, dict) and fields:
        head.append("  ".join(f"{k}: {v}" for k, v in fields.items()))
    if isinstance(crud, dict) and crud:
        head.append("CRUD  " + "  ".join(f"{k}={v}" for k, v in crud.items()))
    return "\n".join([h for h in head if h] + [n.text or ""])


@lru_cache(maxsize=16)
def _xlsx_rows(path: str, mtime: float, sheet: str) -> list[str]:
    """Cell text of one sheet, one string per row (cached per file version)."""
    try:
        import openpyxl
        from ..security import office_zip_ok
        data = open(path, "rb").read()
        if office_zip_ok(data):
            return []
        import io
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        if sheet not in wb.sheetnames:
            return []
        out = []
        for row in wb[sheet].iter_rows(values_only=True):
            out.append(" | ".join("" if v is None else str(v) for v in row).rstrip(" |"))
            if len(out) >= 20000:
                break
        return out
    except Exception:
        log.warning("could not read %s", path, exc_info=True)
        return []


def _new_usage() -> dict:
    """Per-run AI usage: how candidates were judged and the tokens the API reported."""
    return {"ai_items": 0, "reused": 0, "rule_low_score": 0, "rule_cap": 0, "rule": 0, "fallback": 0,
            "calls": 0, "input_tokens": 0, "output_tokens": 0}


class Engine:
    def __init__(self, store: GraphStore, data_dir: str, repo: str | None = None,
                 docs_dir: str | None = None, settings_dir: str | None = None):
        self.store = store
        self.data_dir = data_dir
        # where settings.yaml lives (the workspace root); falls back to CODEGRAPH_DATA_DIR
        self.settings_dir = settings_dir
        self.repo = repo
        self.docs_dir = docs_dir
        self.runs = RunStore(data_dir)
        self.bus = EventBus()
        self.estimator = Estimator(data_dir)
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)
        self._states: dict[str, dict] = {}
        self.reload()

    # ------------------------------------------------------------ indexes
    def reload(self) -> None:
        nodes = self.store.nodes()
        self.search = NodeSearch(n for n in nodes if n.kind not in (Kind.REQUIREMENT, Kind.INTENT, Kind.CONFIG)
                                 or not n.file)
        vocab: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            if n.kind == Kind.MESSAGE:
                for v in n.props.get("values", {}).values():
                    v = v.strip()
                    if 1 < len(v) <= 20:
                        vocab[v].append(n.name)
        idents = set()
        from ..text import split_ident
        for n in nodes:
            if n.kind in (Kind.FIELD, Kind.CLASS, Kind.METHOD, Kind.COLUMN, Kind.TABLE, Kind.MESSAGE, Kind.TEMPLATE):
                base = n.name.split(".")[-1].replace("()", "").split("/")[-1]
                idents.add(base.lower())
                idents.update(split_ident(base))
        self.llm = make_llm(dict(vocab), idents)
        self._fan_in: dict[str, int] = {}
        self._by_file = None
        if hasattr(self, "_runtime_present"):
            del self._runtime_present
        rules = load_rules()
        self.propagation = rules.propagation
        self.prop_by_dir = {"in": defaultdict(list), "out": defaultdict(list)}
        for r in self.propagation:
            self.prop_by_dir[r["dir"]][r["rel"]].append(r)

    def fan_in(self, node_id: str) -> int:
        if node_id not in self._fan_in:
            self._fan_in[node_id] = len(self.store.in_edges(node_id, [Rel.CALLS, Rel.REFERENCES]))
        return self._fan_in[node_id]

    # ------------------------------------------------------------- state io
    def state(self, run_id: str) -> dict | None:
        if run_id in self._states:
            return self._states[run_id]
        st = self.runs.load(run_id)
        if st:
            self._states[run_id] = st
        return st

    def _save(self, st: dict) -> None:
        self.runs.save(st)

    def emit(self, st: dict, etype: str, data: dict, pace: bool = True) -> None:
        st["seq"] = st.get("seq", -1) + 1
        ev = self.runs.append_event(st["id"], st["seq"], etype, data)
        self.bus.publish(st["id"], ev)
        if pace and st["params"].get("pace"):
            time.sleep(st["params"]["pace"])

    # ----------------------------------------------------------- run entry
    def start_run(self, requirement: str, title: str | None = None, params: dict | None = None,
                  parent_id: str | None = None, focus: list[str] | None = None,
                  instruction: str | None = None, background: bool = True) -> str:
        p = {"depth": 3, "min_score": 0.2, "pace": 0.0, "seed_k": 10, "boost": []}
        p.update(params or {})
        # AI-judgement budget: settings file < environment < this run's parameters
        budget = load_llm_budget(self.settings_dir).with_run_params(params or {})
        p["llm"] = budget.to_dict()
        for k in [k for k in p if k.startswith("llm_")]:
            p.pop(k)
        run_id = uuid.uuid4().hex[:10]
        st = {
            "id": run_id, "parent_id": parent_id, "title": title or requirement[:40],
            "requirement": requirement, "instruction": instruction, "focus": focus or [],
            "created_at": _now(), "status": "running", "phase": "queued", "progress": 0.0,
            "params": p, "llm": self.llm.name, "intents": [], "candidates": {}, "grep": {},
            "shadow": {}, "crud": {}, "checks": [], "estimate": {}, "approved": None, "seq": -1,
            "repo": self.repo, "llm_usage": _new_usage(),
        }
        self._states[run_id] = st
        self._save(st)
        if background:
            threading.Thread(target=self._run, args=(run_id,), daemon=True).start()
        else:
            self._run(run_id)
        return run_id

    def _phase(self, st, phase, progress, label):
        st["phase"] = phase
        st["progress"] = progress
        self.emit(st, "phase", {"phase": phase, "progress": progress, "label": label})

    def _run(self, run_id: str) -> None:
        st = self.state(run_id)
        lock = self._locks[run_id]
        try:
            self.emit(st, "run.started", {"requirement": st["requirement"], "llm": self.llm.name,
                                          "parent_id": st["parent_id"]})
            with lock:
                if st["parent_id"]:
                    self._copy_parent(st)
                self._phase(st, "structure", 0.05, "要求を構造化中")
                if st["params"].get("mode") == "diff":
                    seeds = self._diff_seeds(st)
                else:
                    self._structure(st)
                    self._phase(st, "seed", 0.15, "起点を探索中")
                    seeds = self._seed(st)
                self._phase(st, "expand", 0.3, "依存関係を追跡中")
                self._expand(st, seeds, st["params"]["depth"])
                self._phase(st, "judge", 0.5, "影響を判定中")
                self._judge(st, list(st["candidates"]))
                if st["params"].get("mode") != "diff":
                    self._phase(st, "grep", 0.65, "横断検索で盲点を確認中")
                    self._grep(st)
                    self._phase(st, "shadow", 0.75, "独立した再探索と突き合わせ中")
                    self._shadow(st)
                self._phase(st, "crud", 0.85, "影響テーブルのCRUDを確認中")
                self._crud(st)
                if st["parent_id"]:
                    self._inherit_decisions(st)
                self._phase(st, "estimate", 0.95, "規模を算出中")
                self.recompute(st, emit=True)
                st["status"] = "done"
                st["phase"] = "done"
                st["progress"] = 1.0
                self._save(st)
            self.emit(st, "run.done", self.summary(st), pace=False)
        except Exception as e:  # pragma: no cover - surfaced to the viewer
            st["status"] = "error"
            st["error"] = str(e)
            self._save(st)
            log.exception("run %s failed", st.get("id"))
            # the stack trace stays in the server log; clients get the message only
            self.emit(st, "error", {"message": str(e)[:500]}, pace=False)

    # ------------------------------------------------------------ phase 1
    def _structure(self, st):
        text = st["requirement"]
        if st.get("instruction"):
            text = st["instruction"] if st["parent_id"] else text + "\n" + st["instruction"]
        intents = self.llm.structure_requirement(text, meter=st.setdefault("llm_usage", _new_usage()))
        offset = len(st["intents"])
        for i, it in enumerate(intents, offset + 1):
            it["id"] = f"I{i}"
            it["instruction"] = bool(st["parent_id"])   # a re-investigation instruction, not a requirement
            it["pattern"] = None
            st["intents"].append(it)
            self.emit(st, "intent", it)
        flags = (st.get("instruction") or "")
        boost = set(st["params"].get("boost") or [])
        if re.search(r"設計書|仕様書|doc", flags, re.I):
            boost.add("doc")
        if re.search(r"DB|テーブル|SQL|データ", flags, re.I):
            boost.add("data")
        if re.search(r"テスト|test", flags, re.I):
            boost.add("test")
        st["params"]["boost"] = sorted(boost)

    # ------------------------------------------------------------ phase 2
    def _add_candidate(self, st, node_id, score, origin, intent_id, parent=None, rel=None,
                       reason=None, virtual=None) -> bool:
        cands = st["candidates"]
        node = self.store.get(node_id)
        if node is None and virtual is None:
            return False
        if node_id in cands:
            c = cands[node_id]
            if intent_id and intent_id not in c["intent_ids"]:
                c["intent_ids"].append(intent_id)
            if score > c["score"] + 1e-6:
                c["score"] = round(score, 3)
                c["parent"], c["via_rel"] = parent, rel
                c["path"] = (cands[parent]["path"] if parent in cands else []) + [node_id]
                if reason:
                    c["seed_reason"] = reason
            return False
        info = virtual or {"kind": node.kind, "name": node.name, "file": node.file, "line": node.line,
                           "layer": node.layer, "group": node.props.get("group")}
        kind = info["kind"]
        cands[node_id] = {
            "node_id": node_id, "kind": kind, "layer": info.get("layer") or LAYER.get(kind, "code"),
            "group": info.get("group"), "name": info["name"],
            "file": info.get("file"), "line": info.get("line"), "score": round(score, 3),
            "origin": origin, "intent_ids": [intent_id] if intent_id else [], "parent": parent,
            "via_rel": rel, "path": (cands[parent]["path"] if parent in cands else []) + [node_id],
            "seed_reason": reason, "verdict": None, "confidence": None, "reason": None,
            "change_type": None, "difficulty": None, "decision": None, "assignee": None,
            "comments": [], "found_at": _now(),
        }
        self.emit(st, "candidate", {k: cands[node_id][k] for k in
                                    ("node_id", "kind", "layer", "group", "name", "score", "origin", "parent",
                                     "via_rel", "intent_ids", "seed_reason")})
        return True

    def _entity_for(self, intent) -> list:
        ents = []
        terms = {t.lower() for t in intent.get("keywords", [])}
        for n in self.store.nodes([Kind.CLASS]):
            if "Entity" in n.props.get("annotations", []) and n.name.lower() in terms:
                ents.append(n)
        return ents

    def _seed(self, st) -> list[tuple[str, float]]:
        seeds = []
        k = st["params"]["seed_k"]
        for fid in st.get("focus") or []:
            if self._add_candidate(st, fid, 0.95, "focus", None, reason="再調査の起点として指定"):
                pass
            seeds.append((fid, 0.95))
        by_name = defaultdict(list)
        for n in self.store.nodes():
            if seedable(n):
                by_name[n.name.split(".")[-1].replace("()", "").split("/")[-1].lower()].append(n)
        for it in st["intents"]:
            # (a) exact identifier matches from the intent's keywords
            for kw in it.get("keywords", []):
                if kw.lower() in STOP_TERMS:
                    continue
                for n in by_name.get(kw.lower(), []):
                    if n.kind in (Kind.CLASS,) and it["change_type"] == "add":
                        continue  # handled by the entity pattern below
                    score = 0.9 if n.kind in (Kind.FIELD, Kind.COLUMN, Kind.MESSAGE) else 0.75
                    if self._add_candidate(st, n.id, score, "seed", it["id"],
                                           reason=f"要求の語「{kw}」と識別子が一致"):
                        seeds.append((n.id, score))
            # (b) change pattern: add an attribute to an entity
            if it["change_type"] == "add" and it.get("new_terms") and not (it.get("values") or {}).get("old"):
                for ent in self._entity_for(it):
                    it["pattern"] = "add-attribute"
                    reason = f"新しい項目（{'・'.join(it['new_terms'][:3])}）を持たせる対象のエンティティ"
                    if self._add_candidate(st, ent.id, 0.9, "pattern", it["id"], reason=reason):
                        seeds.append((ent.id, 0.9))
                    for e in self.store.out_edges(ent.id, [Rel.MAPS_TO]):
                        if self._add_candidate(st, e.dst, 0.9, "pattern", it["id"], parent=ent.id,
                                               rel=Rel.MAPS_TO, reason="新しい列を追加するテーブル"):
                            seeds.append((e.dst, 0.9))
                    for b in self.store.nodes([Kind.CONFIG]):
                        if b.props.get("bundle"):
                            self._add_candidate(st, b.id, 0.7, "pattern", it["id"],
                                                reason="新しい項目の表示ラベルを全ロケールに追加")
                self.emit(st, "pattern", {"intent": it["id"], "pattern": it["pattern"]})
            # (d) old values: "5件から10件" -> where does 5 appear among related elements?
            olds = (it.get("values") or {}).get("old") or []
            if olds:
                sims = self.search.scores(" ".join([it["summary"]] + it.get("keywords", [])))
                for n in self.store.nodes([Kind.METHOD, Kind.FIELD, Kind.MESSAGE, Kind.DOC, Kind.TEMPLATE, Kind.CONFIG,
                                           Kind.SQL_STATEMENT, Kind.DB_OBJECT, Kind.DOC_ENTITY, Kind.SCRIPT, Kind.JOB]):
                    if n.file and ("src/test/" in n.file or "/test/" in n.file):
                        continue
                    blob = " ".join([n.text, " ".join(n.props.get("validations", [])),
                                     " ".join(n.props.get("values", {}).values()) if isinstance(n.props.get("values"), dict) else ""])
                    for v in olds:
                        if re.search(r"(?<![\d.])" + re.escape(v) + r"(?![\d.])", blob):
                            sim = sims.get(n.id, 0.0)
                            if sim < 0.04:
                                continue
                            score = min(0.92, 0.6 + sim)
                            if self._add_candidate(st, n.id, score, "seed", it["id"],
                                                   reason=f"変更前の値「{v}」が出現（要求との類似度 {sim:.2f}）"):
                                seeds.append((n.id, score))
                            break
            # (c) text similarity (skipped for bare instructions such as "テストも確認して")
            if it.get("instruction") and not it.get("keywords"):
                continue
            q = " ".join([it["summary"]] + it.get("keywords", []) * 2 + it.get("new_terms", []))
            for n, s in self.search.query(q, k=k + 30, min_score=0.15):
                if not seedable(n):
                    continue  # tests and documents are reached through what they describe
                score = min(0.8, 0.3 + s)
                if self._add_candidate(st, n.id, score, "seed", it["id"],
                                       reason=f"要求文との類似度 {s:.2f}"):
                    seeds.append((n.id, score))
        return seeds

    def _diff_seeds(self, st):
        """Change-set mode: the changed lines are the starting points."""
        p = st["params"]
        it = {"id": "I1", "summary": st["requirement"], "change_type": "modify", "keywords": [], "new_terms": [],
              "values": {}, "pattern": "diff"}
        st["intents"] = [it]
        self.emit(st, "intent", it)
        seeds = []
        for d in p.get("diff_nodes", []):
            if self._add_candidate(st, d["node_id"], 0.95, "diff", "I1", reason=d["reason"]):
                c = st["candidates"][d["node_id"]]
                c["decision"] = {"status": "confirmed", "reason": "変更差分に含まれる", "actor": "git", "at": _now()}
                seeds.append((d["node_id"], 0.95))
        return seeds

    def diff_nodes(self, hunks: dict) -> tuple[list[dict], list[str]]:
        """Map changed line ranges to the smallest indexed elements containing them."""
        out, added = [], []
        for path, ranges in hunks.items():
            if path.startswith("+"):
                added.append(path[1:])
                continue
            seen = set()
            for a, b in ranges:
                best = None
                for n in self._file_nodes().get(path, []):
                    if n.line and n.end_line and n.line <= b and a <= n.end_line and n.kind not in (Kind.CONFIG,):
                        if best is None or (n.end_line - n.line) < (best.end_line - best.line):
                            best = n
                nid = best.id if best else (f"file:{path}" if self.store.get(f"file:{path}") else None)
                if nid and nid not in seen:
                    seen.add(nid)
                    out.append({"node_id": nid, "reason": f"{path}:{a}-{b} を変更"})
        return out, added

    def regression_scope(self, st, min_conf: float = 0.3) -> dict:
        """What a change set reaches, grouped for test planning."""
        groups = {"entry": [], "screen": [], "batch": [], "data": [], "test": [], "doc": []}
        for c in sorted(st["candidates"].values(), key=lambda c: -(c.get("confidence") or c["score"])):
            if c.get("decision") and c["decision"]["status"] == "excluded":
                continue
            if (c.get("confidence") or c["score"]) < min_conf:
                continue
            k = c["kind"]
            row = {"node_id": c["node_id"], "name": c["name"], "conf": round(c.get("confidence") or c["score"], 2)}
            if k == Kind.ENDPOINT:
                groups["entry"].append(row)
            elif k in (Kind.JOB, Kind.JOBNET, Kind.CRON, Kind.BATCH_JOB, Kind.SCRIPT):
                groups["batch"].append(row)
            elif k == Kind.TEMPLATE:
                groups["screen"].append(row)
            elif k in (Kind.TABLE, Kind.DB_OBJECT, Kind.DATA_FILE, Kind.RUNTIME_SQL):
                groups["data"].append(row)
            elif k in (Kind.TEST_METHOD, Kind.TEST_CLASS):
                groups["test"].append(row)
            elif k in (Kind.DOC, Kind.DOC_ENTITY):
                groups["doc"].append(row)
        return groups

    def scope_markdown(self, st, title: str = "") -> str:
        g = self.regression_scope(st)
        label = {"entry": "画面・APIの入口", "screen": "画面テンプレート", "batch": "バッチ・ジョブ", "data": "テーブル・DBオブジェクト",
                 "test": "実行すべきテスト", "doc": "見直す設計書"}
        out = [f"### 影響分析 {title}".rstrip(), "", f"変更起点 {sum(1 for c in st['candidates'].values() if c['origin'] == 'diff')} 要素から"
               f" {len(st['candidates'])} 要素を追跡しました（判定: {st['llm']}）。", ""]
        for k, rows in g.items():
            if rows:
                out.append(f"**{label[k]}**（{len(rows)}）")
                out += [f"- {r['name']}（確度 {r['conf']}）" for r in rows[:12]]
                if len(rows) > 12:
                    out.append(f"- ほか {len(rows) - 12} 件")
                out.append("")
        return "\n".join(out)

    # ------------------------------------------------------------ phase 3
    def _expand(self, st, seeds, depth, origin="graph"):
        """Breadth-first closure over the propagation rules. Neighbours of a whole
        frontier are fetched in one call per direction, so graph-database stores
        answer each level with a single query."""
        boost = set(st["params"].get("boost") or [])
        min_score = st["params"]["min_score"]
        frontier = list(seeds)
        in_rels = list(self.prop_by_dir["in"])
        out_rels = list(self.prop_by_dir["out"])
        for _ in range(depth):
            ids = []
            for nid, score in frontier:
                c = st["candidates"].get(nid)
                if c and c.get("decision") and c["decision"]["status"] == "excluded":
                    continue
                ids.append(nid)
            if not ids:
                break
            nodes = self.store.get_many(ids)
            # sinks (documents, tests, SQL files...) end a path, except that whatever *runs*
            # them (a job script running a SQL file) is still affected
            sinks = [i for i in ids if i in nodes and nodes[i].kind in SINKS]
            ids = [i for i in ids if i in nodes and nodes[i].kind not in SINKS]
            score_of = dict(frontier)
            ins = self.store.neighbors_many(ids, "in", in_rels)
            outs = self.store.neighbors_many(ids, "out", out_rels)
            if sinks and Rel.RUNS in self.prop_by_dir["in"]:
                ins.update(self.store.neighbors_many(sinks, "in", [Rel.RUNS]))
                ids += sinks
            others = {e.src for es in ins.values() for e in es} | {e.dst for es in outs.values() for e in es}
            onodes = self.store.get_many(others)
            nxt = []
            for nid in ids:
                node = nodes[nid]
                score = score_of[nid]
                c = st["candidates"].get(nid)
                iid = (c["intent_ids"] or [None])[0] if c else None
                hub = self.fan_in(nid) > 15
                for direction, edges in (("in", ins.get(nid, [])), ("out", outs.get(nid, []))):
                    for e in edges:
                        other = e.src if direction == "in" else e.dst
                        on = onodes.get(other)
                        if on is None or on.kind in (Kind.FEATURE,):
                            continue
                        for rule in self.prop_by_dir[direction].get(e.rel, []):
                            if rule.get("from_kinds") and node.kind not in rule["from_kinds"]:
                                continue
                            weight = rule["weight"]
                            if e.props.get("positional") and rule.get("positional_weight"):
                                weight = rule["positional_weight"]
                            if e.rel == Rel.REFERENCES and node.kind == Kind.CLASS:
                                weight = 0.85 if on.kind == Kind.TEMPLATE else 0.5
                            if hub and e.rel in (Rel.CALLS, Rel.REFERENCES):
                                weight *= 0.8
                            if ("doc" in boost and on.kind in (Kind.DOC, Kind.DOC_ENTITY)) or \
                               ("test" in boost and on.kind in (Kind.TEST_CLASS, Kind.TEST_METHOD)) or \
                               ("data" in boost and on.layer == "data"):
                                weight = min(0.95, weight * 1.2)
                            s2 = score * weight * e.conf
                            if s2 < min_score:
                                continue
                            if self._add_candidate(st, other, s2, origin, iid, parent=nid, rel=e.rel):
                                nxt.append((other, s2))
                            break
                # design documents usually describe the class, not each member
                if node.kind in (Kind.METHOD, Kind.FIELD):
                    for ce in self.store.in_edges(nid, [Rel.CONTAINS]):
                        for de in self.store.in_edges(ce.src, [Rel.DESCRIBES]):
                            s2 = score * 0.6 * de.conf * (1.2 if "doc" in boost else 1.0)
                            if s2 >= min_score:
                                self._add_candidate(st, de.src, s2, origin, iid, parent=nid, rel=Rel.DESCRIBES)
            frontier = nxt
            if not frontier:
                break

    # ------------------------------------------------------------ phase 4
    def _snippet(self, node_id: str, max_lines: int = 25) -> str:
        n = self.store.get(node_id)
        if not n or not n.file or not self.repo:
            return (n.text[:600] if n else "")
        if _is_office(n.file):
            return _office_summary(n)[:600]       # never send binary file content to the AI
        path = self._source_path(n.file)
        if not path or not os.path.isfile(path) or os.path.getsize(path) > max_file_bytes():
            return n.text[:600]
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        a = max(0, (n.line or 1) - 1)
        b = min(len(lines), (n.end_line or a + max_lines))
        return "\n".join(lines[a:min(b, a + max_lines)])

    def path_reason(self, st, node_id) -> str:
        c = st["candidates"][node_id]
        if c.get("seed_reason") and not c.get("parent"):
            return c["seed_reason"]
        p = st["candidates"].get(c.get("parent"))
        if p:
            phrase = REL_JA.get(c.get("via_rel"), "と関連している")
            if c.get("via_rel") == Rel.MAPS_TO and c["parent"]:
                return f"{p['name']} {phrase}"
            return f"{p['name']} {phrase}"
        return c.get("seed_reason") or ""

    def _default_change_type(self, st, c) -> str:
        if c["kind"] in (Kind.TEST_CLASS, Kind.TEST_METHOD):
            return "test"
        if c["kind"] == Kind.DOC:
            return "doc"
        intents = [i for i in st["intents"] if i["id"] in c["intent_ids"]]
        if intents and intents[0]["change_type"] == "add" and c["kind"] in (Kind.TABLE, Kind.CLASS):
            return "modify"
        if intents and intents[0]["change_type"] == "delete" and c["origin"] == "seed":
            return "delete"
        return "modify"

    def _budget(self, st) -> LLMBudget:
        return LLMBudget().with_run_params(st["params"])

    def _judge(self, st, node_ids):
        """Judge candidates. Three ways to avoid spending AI tokens, all configurable
        (settings.yaml / CODEGRAPH_LLM_* / per-run params, see settings.py):
          - reuse: a re-investigation keeps the parent's AI verdict unless the
            candidate gained evidence (score up by >= rescore_delta);
          - min_score: weakly connected candidates are judged by rules;
          - max_items: at most this many candidates per run go to the AI (highest
            score first); the rest are judged by rules. None = no cap."""
        budget = self._budget(st)
        usage = st.setdefault("llm_usage", _new_usage())
        cands = st["candidates"]
        ai_items, rule_items = defaultdict(list), defaultdict(list)
        for nid in sorted(set(node_ids), key=lambda n: -cands[n]["score"]):
            c = cands[nid]
            c["change_type"] = c.get("change_type") or self._default_change_type(st, c)
            if (budget.reuse_verdicts and c.get("verdict") and c.get("judged_by") in ("ai", "reused")
                    and c["score"] < (c.get("judged_score") or 0) + budget.rescore_delta):
                if c["judged_by"] == "ai" and st.get("parent_id"):
                    c["judged_by"] = "reused"
                usage["reused"] += 1
                continue
            iid = (c["intent_ids"] or ["I1"])[0]
            item = {"id": nid, "kind": c["kind"], "name": c["name"], "score": c["score"],
                    "path_reason": self.path_reason(st, nid), "change_type": c["change_type"]}
            if not self.llm.available:
                rule_items[iid].append((item, "rule"))
            elif c["score"] < budget.min_score:
                rule_items[iid].append((item, "rule:low_score"))
            elif budget.max_items is not None and usage["ai_items"] >= budget.max_items:
                rule_items[iid].append((item, "rule:cap"))
            else:
                item["snippet"] = self._snippet(nid)
                ai_items[iid].append(item)
                usage["ai_items"] += 1
        intents = {i["id"]: i for i in st["intents"]} or {"I1": {"summary": st["requirement"], "change_type": "modify"}}
        pick = lambda iid: intents.get(iid) or next(iter(intents.values()))
        for iid, items in ai_items.items():
            for r in self.llm.judge(pick(iid), items, batch_size=budget.batch_size, meter=usage):
                if r.get("_by") == "rule:fallback":
                    usage["fallback"] += 1
                self._apply_verdict(st, r, r.get("_by") or "ai")
        for iid, pairs in rule_items.items():
            how = {it["id"]: why for it, why in pairs}
            for k in ("rule", "rule:low_score", "rule:cap"):
                usage[k.replace("rule:", "rule_") if k != "rule" else "rule"] += sum(1 for v in how.values() if v == k)
            for r in RuleBasedLLM.judge(self.llm, pick(iid), [it for it, _ in pairs]):
                self._apply_verdict(st, r, how[r["id"]])

    def _apply_verdict(self, st, r, by):
        c = st["candidates"].get(r["id"])
        if not c:
            return
        c["verdict"], c["confidence"] = r["verdict"], r["confidence"]
        c["reason"] = r.get("reason") or self.path_reason(st, r["id"])
        if r.get("change_type") in ("add", "modify", "delete", "test", "doc"):
            c["change_type"] = r["change_type"]
        c["judged_by"], c["judged_score"] = by, c["score"]
        self.emit(st, "verdict", {"node_id": r["id"], "verdict": c["verdict"], "confidence": c["confidence"],
                                  "reason": c["reason"], "judged_by": by})

    # ------------------------------------------------------------ phase 5
    def _grep_terms(self, st) -> list[str]:
        terms = set()
        for c in st["candidates"].values():
            if c["origin"] not in ("seed", "pattern", "focus", "human"):
                continue
            n = self.store.get(c["node_id"])
            if not n:
                continue
            base = n.name.split(".")[-1].replace("()", "").split("/")[-1]
            if n.kind == Kind.ENDPOINT:
                continue
            if n.kind == Kind.TABLE or n.kind == Kind.CLASS:
                terms.add(base)
            elif n.kind in (Kind.FIELD, Kind.COLUMN, Kind.MESSAGE):
                terms.add(base)
        for it in st["intents"]:
            for kw in it.get("keywords", []) + it.get("new_terms", []):
                if re.fullmatch(r"[A-Za-z_]\w{3,}", kw):
                    terms.add(kw)
        return sorted(t for t in terms if t.lower() not in STOP_TERMS and len(t) >= 4)

    def _locate(self, file: str, line: int):
        """Smallest indexed element that contains file:line."""
        best = None
        for n in self._file_nodes().get(file, []):
            if n.line and n.end_line and n.line <= line <= n.end_line:
                if best is None or (n.end_line - n.line) < (best.end_line - best.line):
                    best = n
        return best

    def _file_nodes(self):
        if not hasattr(self, "_by_file") or self._by_file is None:
            idx = defaultdict(list)
            for n in self.store.nodes():
                if n.file:
                    idx[n.file].append(n)
            self._by_file = idx
        return self._by_file

    def _grep(self, st):
        from ..sources.codesearch import make_search
        terms = self._grep_terms(st)
        searcher = make_search(self.repo)
        if searcher is None or not terms:
            st["grep"] = {"terms": terms, "hits": [], "backend": None}
            return
        try:
            raw = searcher.search(terms)
        except Exception as e:  # remote search down: record it, the check stays open
            st["grep"] = {"terms": terms, "hits": [], "backend": searcher.name, "error": str(e)}
            self.emit(st, "grep.done", {"terms": terms, "hits": 0, "new_candidates": 0, "error": str(e)})
            return
        hits = []
        for h in raw:
            node = self._locate(h["file"], h["line"])
            h["node_id"] = node.id if node else f"file:{h['file']}"
            hits.append(h)
        st["grep"] = {"terms": terms, "hits": hits[:3000], "backend": searcher.name, "total": len(hits)}
        new = 0
        added = []
        for h in hits:
            nid = h["node_id"]
            if nid in st["candidates"]:
                continue
            virtual = None
            if not self.store.get(nid):
                virtual = {"kind": Kind.CONFIG, "name": h["file"].split("/")[-1], "file": h["file"], "line": h["line"]}
            if self._add_candidate(st, nid, 0.35, "grep", None, reason=f"キーワード「{h['term']}」が出現"
                                   f"（{h['file']}:{h['line']}）。依存グラフからは未到達", virtual=virtual):
                new += 1
                added.append(nid)
        self._judge(st, added)
        self.emit(st, "grep.done", {"terms": terms, "hits": len(hits), "new_candidates": new, "backend": searcher.name})

    # ------------------------------------------------------------ phase 6
    def _shadow(self, st):
        """Different strategy: no intents, no graph — plain similarity over the whole
        requirement text plus exact identifier hits in all node names."""
        q = st["requirement"] + " " + (st.get("instruction") or "")
        found = {}
        for n, s in self.search.query(q, k=25, min_score=0.1):
            if seedable(n) or n.kind in (Kind.TEST_METHOD, Kind.DOC, Kind.DOC_ENTITY, Kind.SQL_FILE):
                found[n.id] = s
        only = [nid for nid in found if nid not in st["candidates"]]
        for nid in only:
            self._add_candidate(st, nid, min(0.45, 0.2 + found[nid]), "shadow", None,
                                reason=f"独立した再探索（全文類似度 {found[nid]:.2f}）でのみ検出")
        self._judge(st, [n for n in only if n in st["candidates"]])
        st["shadow"] = {"node_ids": list(found), "only": only}
        self.emit(st, "shadow.done", {"found": len(found), "only": len(only)})

    # ------------------------------------------------------------ phase 7
    def _crud(self, st):
        """Everything that defines, loads, maps, reads or writes an affected table —
        code, SQL statements, stored code, design-doc CRUD rows and SQL seen at runtime."""
        tables = {}
        crud_new = []
        rels = [Rel.DEFINES, Rel.POPULATES, Rel.MAPS_TO, Rel.READS, Rel.WRITES, Rel.DOC_CRUD]
        for c in list(st["candidates"].values()):
            if c["kind"] != Kind.TABLE:
                continue
            if c["verdict"] == "not_impacted" and not c.get("decision"):
                continue
            tid = c["node_id"]
            touch = set()
            for e in self.store.in_edges(tid, rels):
                touch.add(e.src)
            cols = [e.dst for e in self.store.out_edges(tid, [Rel.CONTAINS])]
            for col in cols:
                for e in self.store.in_edges(col, [Rel.WRITES]):
                    touch.add(e.src)
            for sid in list(touch):
                n = self.store.get(sid)
                if n and n.kind == Kind.SQL_STATEMENT:
                    for e in self.store.in_edges(sid, [Rel.EXECUTES]):
                        touch.add(e.src)
            tables[tid] = sorted(touch)
            for nid in sorted(touch):
                if nid not in st["candidates"]:
                    n = self.store.get(nid)
                    origin = "runtime" if n and n.kind == Kind.RUNTIME_SQL else "crud"
                    reason = (f"{c['name']} に実行時に触れたSQL（コード上の出所が不明）" if origin == "runtime" and
                              not n.props.get("matched") else f"{c['name']} を読み書きしている箇所（CRUD確認）")
                    if self._add_candidate(st, nid, 0.4, origin, (c["intent_ids"] or [None])[0], parent=tid,
                                           reason=reason):
                        crud_new.append(nid)
        self._judge(st, crud_new)
        st["crud"] = {"tables": tables}
        self.emit(st, "crud.done", {"tables": {k: len(v) for k, v in tables.items()}})

    # ------------------------------------------------------------ derived
    def _copy_parent(self, st):
        """A re-investigation builds on the parent run: same intents and candidates
        (decisions included), then adds what the new instruction and focus find."""
        import copy
        parent = self.state(st["parent_id"])
        if not parent:
            return
        st["intents"] = copy.deepcopy(parent["intents"])
        st["candidates"] = copy.deepcopy(parent["candidates"])
        for c in st["candidates"].values():
            if c.get("decision"):
                c["decision"]["inherited"] = True
        self.emit(st, "inherited", {"intents": len(st["intents"]), "candidates": len(st["candidates"])}, pace=False)

    def _inherit_decisions(self, st):
        parent = self.state(st["parent_id"])
        if not parent:
            return
        for nid, pc in parent["candidates"].items():
            if pc.get("decision") and nid in st["candidates"]:
                st["candidates"][nid]["decision"] = dict(pc["decision"], inherited=True)

    def checks(self, st) -> list[dict]:
        cands = st["candidates"]
        decided = lambda nid: bool(cands.get(nid, {}).get("decision"))
        confirmed = lambda c: c.get("decision") and c["decision"]["status"] == "confirmed"
        # 1 every intent has a confirmed editable element
        per_intent = []
        for it in [i for i in st["intents"] if not i.get("instruction")]:
            ok = any(confirmed(c) and it["id"] in c["intent_ids"] and c["kind"] in EDITABLE
                     for c in cands.values())
            per_intent.append({"intent": it["id"], "summary": it["summary"], "ok": ok})
        c1 = all(p["ok"] for p in per_intent) and bool(per_intent)
        # 2 closure: every graph-derived candidate decided
        closure = [nid for nid, c in cands.items() if c["origin"] in ("graph", "seed", "pattern", "focus")]
        open2 = [nid for nid in closure if not decided(nid)]
        # 3 keyword sweep: every hit's element decided
        hit_nodes = sorted({h["node_id"] for h in st.get("grep", {}).get("hits", [])})
        open3 = [nid for nid in hit_nodes if not decided(nid)]
        # 4 CRUD of affected tables
        crud_nodes = sorted({n for v in st.get("crud", {}).get("tables", {}).values() for n in v})
        open4 = [nid for nid in crud_nodes if not decided(nid)]
        # 5 independent re-exploration
        shadow_nodes = st.get("shadow", {}).get("node_ids", [])
        open5 = [nid for nid in shadow_nodes if not decided(nid)]
        # 6 runtime evidence: SQL observed on affected tables whose origin is unknown
        runtime_nodes = sorted(nid for nid, c in cands.items() if c["kind"] == Kind.RUNTIME_SQL)
        open6 = [nid for nid in runtime_nodes if not decided(nid)]
        has_runtime = self._has_runtime()
        extra = []
        if has_runtime:
            extra.append({"key": "runtime", "title": "実行時SQLとの突き合わせ",
                          "detail": f"影響テーブルに実行時に触れたSQL {len(runtime_nodes)} 件のうち未判定 {len(open6)} 件"
                                    f"（コードに対応が無いものは動的SQLや外部ツールの可能性）",
                          "ok": not open6, "open": open6})
        base = [
            {"key": "intents", "title": "変更意図ごとの起点",
             "detail": f"変更意図 {len(per_intent)} 件のうち、確定した修正箇所があるもの {sum(p['ok'] for p in per_intent)} 件",
             "ok": c1, "open": [p["intent"] for p in per_intent if not p["ok"]], "items": per_intent},
            {"key": "closure", "title": "依存の閉包に未判定がない",
             "detail": f"依存グラフから辿った {len(closure)} 件のうち未判定 {len(open2)} 件",
             "ok": not open2, "open": open2},
            {"key": "grep", "title": "キーワード・SQL文字列の横断検索",
             "detail": f"語 {len(st.get('grep', {}).get('terms', []))} 個・ヒット {len(st.get('grep', {}).get('hits', []))} 行"
                       f"（{len(hit_nodes)} 要素）のうち未判定 {len(open3)} 件",
             "ok": not open3, "open": open3},
            {"key": "crud", "title": "影響テーブルのCRUD全箇所",
             "detail": f"{len(st.get('crud', {}).get('tables', {}))} テーブルに触れる {len(crud_nodes)} 箇所のうち未判定 {len(open4)} 件",
             "ok": not open4, "open": open4},
            {"key": "shadow", "title": "独立した再探索との突き合わせ",
             "detail": f"別戦略で見つかった {len(shadow_nodes)} 件（うち本探索に無かった "
                       f"{len(st.get('shadow', {}).get('only', []))} 件）のうち未判定 {len(open5)} 件",
             "ok": not open5, "open": open5},
        ]
        return base + extra

    def _has_runtime(self) -> bool:
        if not hasattr(self, "_runtime_present"):
            self._runtime_present = bool(self.store.nodes([Kind.RUNTIME_SQL]))
        return self._runtime_present

    def estimate(self, st) -> dict:
        lines, pending = [], []
        for nid, c in st["candidates"].items():
            if c["kind"] not in EDITABLE:
                continue
            node = self.store.get(nid)
            props = node.props if node else {}
            tested = bool(self.store.in_edges(nid, [Rel.TESTS])) or any(
                (self.store.get(e.src) or node).kind in (Kind.TEST_METHOD, Kind.TEST_CLASS)
                for e in self.store.in_edges(nid, [Rel.CALLS, Rel.REFERENCES])) if node else False
            hours, notes = self.estimator.element_hours(c, props, self.fan_in(nid), tested)
            if hours <= 0:
                continue
            row = {"node_id": nid, "name": c["name"], "kind": c["kind"], "change_type": c["change_type"],
                   "hours": hours, "notes": notes, "tested": tested}
            dec = c.get("decision")
            if dec and dec["status"] == "confirmed":
                lines.append(row)
            elif not dec and c["verdict"] in ("impacted", "uncertain"):
                row["probability"] = round(c["confidence"] or c["score"], 2)
                pending.append(row)
        return self.estimator.summarize(lines, pending)

    def recompute(self, st, emit=True) -> None:
        st["checks"] = self.checks(st)
        st["estimate"] = self.estimate(st)
        if st.get("approved") and not all(c["ok"] for c in st["checks"]):
            st["approved"] = None
        self._save(st)
        if emit:
            self.emit(st, "checks", {"checks": [{k: c[k] for k in ("key", "ok", "detail")} for c in st["checks"]],
                                     "estimate": {k: st["estimate"][k] for k in ("days", "low", "high")}},
                      pace=False)

    def summary(self, st) -> dict:
        cands = st["candidates"].values()
        return {
            "candidates": len(st["candidates"]),
            "confirmed": sum(1 for c in cands if c.get("decision") and c["decision"]["status"] == "confirmed"),
            "excluded": sum(1 for c in cands if c.get("decision") and c["decision"]["status"] == "excluded"),
            "open": sum(1 for c in cands if not c.get("decision")),
            "checks_passed": sum(1 for c in st["checks"] if c["ok"]),
            "estimate_days": st["estimate"].get("days"),
        }

    # ------------------------------------------------------- human actions
    def decide(self, run_id, node_id, status, reason="", actor="user", change_type=None, difficulty=None):
        st = self.state(run_id)
        with self._locks[run_id]:
            c = st["candidates"].get(node_id)
            if not c:
                raise KeyError(node_id)
            if status in ("confirmed", "excluded"):
                c["decision"] = {"status": status, "reason": reason, "actor": actor, "at": _now()}
            elif status == "open":
                c["decision"] = None
            if change_type:
                c["change_type"] = change_type
            if difficulty:
                c["difficulty"] = difficulty
            self.emit(st, "decision", {"node_id": node_id, "decision": c["decision"], "actor": actor,
                                       "change_type": c["change_type"], "difficulty": c["difficulty"]}, pace=False)
            self.recompute(st)
        return c

    def bulk_decide(self, run_id, items: list[dict], actor="user"):
        st = self.state(run_id)
        with self._locks[run_id]:
            for it in items:
                c = st["candidates"].get(it["node_id"])
                if not c:
                    continue
                if it["status"] == "open":
                    c["decision"] = None
                else:
                    c["decision"] = {"status": it["status"], "reason": it.get("reason", ""), "actor": actor, "at": _now()}
                if it.get("change_type"):
                    c["change_type"] = it["change_type"]
                self.emit(st, "decision", {"node_id": c["node_id"], "decision": c["decision"], "actor": actor,
                                           "change_type": c["change_type"]}, pace=False)
            self.recompute(st)
        return self.summary(st)

    def add_candidate(self, run_id, node_id, reason, actor="user", expand=True):
        st = self.state(run_id)
        with self._locks[run_id]:
            if node_id in st["candidates"]:
                c = st["candidates"][node_id]
            else:
                if not self._add_candidate(st, node_id, 0.9, "human", None, reason=f"{actor}が追加: {reason}"):
                    raise KeyError(node_id)
                c = st["candidates"][node_id]
                c["change_type"] = self._default_change_type(st, c)
                c["verdict"], c["confidence"], c["reason"] = "impacted", 0.9, f"{actor}が追加: {reason}"
            c["decision"] = {"status": "confirmed", "reason": reason, "actor": actor, "at": _now()}
            self.emit(st, "decision", {"node_id": node_id, "decision": c["decision"], "actor": actor}, pace=False)
            if expand:
                before = set(st["candidates"])
                self._expand(st, [(node_id, 0.9)], st["params"]["depth"])
                new = [n for n in st["candidates"] if n not in before]
                self._judge(st, new)
                self.emit(st, "expand.done", {"from": node_id, "new": new}, pace=False)
            self.recompute(st)
        return c

    def comment(self, run_id, node_id, text, author="user", assignee=None):
        st = self.state(run_id)
        with self._locks[run_id]:
            c = st["candidates"][node_id]
            if text:
                c["comments"].append({"author": author, "text": text, "at": _now()})
            if assignee is not None:
                c["assignee"] = assignee or None
            self._save(st)
            self.emit(st, "comment", {"node_id": node_id, "comments": c["comments"], "assignee": c["assignee"]}, pace=False)
        return c

    def approve(self, run_id, actor="user"):
        st = self.state(run_id)
        with self._locks[run_id]:
            self.recompute(st, emit=False)
            failing = [c["key"] for c in st["checks"] if not c["ok"]]
            if failing:
                raise ValueError("未達の条件があります: " + ", ".join(failing))
            st["approved"] = {"actor": actor, "at": _now(), "estimate_days": st["estimate"]["days"]}
            self._save(st)
            self.emit(st, "approved", st["approved"], pace=False)
        return st["approved"]

    def reinvestigate(self, run_id, node_ids, instruction, depth=None, actor="user", pace=0.0):
        parent = self.state(run_id)
        params = dict(parent["params"])
        params["pace"] = pace
        if depth:
            params["depth"] = depth
        return self.start_run(parent["requirement"], title=f"再調査: {instruction[:30]}", params=params,
                              parent_id=run_id, focus=node_ids, instruction=instruction)

    def diff(self, a_id, b_id) -> dict:
        a, b = self.state(a_id), self.state(b_id)
        ak, bk = set(a["candidates"]), set(b["candidates"])
        changed = []
        for nid in ak & bk:
            ca, cb = a["candidates"][nid], b["candidates"][nid]
            if ca["verdict"] != cb["verdict"]:
                changed.append({"node_id": nid, "name": cb["name"], "from": ca["verdict"], "to": cb["verdict"]})
        pick = lambda st, ids: [{"node_id": i, "name": st["candidates"][i]["name"], "kind": st["candidates"][i]["kind"],
                                 "verdict": st["candidates"][i]["verdict"]} for i in sorted(ids)]
        return {"a": a_id, "b": b_id, "added": pick(b, bk - ak), "removed": pick(a, ak - bk), "changed": changed}

    # -------------------------------------------------------- node detail
    def node_detail(self, node_id: str, run_id: str | None = None) -> dict:
        n = self.store.get(node_id)
        if n is None and node_id.startswith("file:"):
            rel = node_id[5:]
            if not self._source_path(rel):
                return {}
            return {"node": {"id": node_id, "kind": Kind.CONFIG, "name": rel.split("/")[-1], "file": rel,
                             "line": 1, "layer": "code", "props": {}},
                    "code": self._read_lines(rel, 1, 40), "neighbors": [], "docs": [], "tests": [], "history": []}
        if n is None:
            return {}
        neighbors = []
        for e in self.store.out_edges(node_id):
            o = self.store.get(e.dst)
            if o:
                neighbors.append({"dir": "out", "rel": e.rel, "id": o.id, "name": o.name, "kind": o.kind,
                                  "line": e.props.get("line"), "via": e.props.get("via"),
                                  "src": e.props.get("src"), "conf": e.conf})
        for e in self.store.in_edges(node_id):
            o = self.store.get(e.src)
            if o:
                neighbors.append({"dir": "in", "rel": e.rel, "id": o.id, "name": o.name, "kind": o.kind,
                                  "line": e.props.get("line"), "via": e.props.get("via"),
                                  "src": e.props.get("src"), "conf": e.conf})
        docs = [x for x in neighbors if x["rel"] == Rel.DESCRIBES and x["dir"] == "in"]
        docs_full = []
        for d in docs[:6]:
            dn = self.store.get(d["id"])
            docs_full.append({"id": dn.id, "name": dn.name, "file": dn.file, "line": dn.line,
                              "text": self._doc_text(dn)})
        tests = [x for x in neighbors if x["dir"] == "in" and
                 (x["rel"] == Rel.TESTS or x["kind"] in (Kind.TEST_METHOD, Kind.TEST_CLASS))]
        hl = []
        if run_id:
            st = self.state(run_id)
            c = (st or {}).get("candidates", {}).get(node_id)
            if c:
                # lines where this element touches the element that led to it
                rel_ids = {c.get("parent")} | set(c.get("path", [])[:-1])
                for e in self.store.out_edges(node_id) + self.store.in_edges(node_id):
                    other = e.dst if e.src == node_id else e.src
                    if other in rel_ids and e.props.get("line"):
                        hl.append(e.props["line"])
                for h in st.get("grep", {}).get("hits", []):
                    if h["node_id"] == node_id:
                        hl.append(h["line"])
        hl = sorted(set(hl))
        code = []
        if n.file:
            start, end = (n.line or 1), (n.end_line or (n.line or 1))
            if hl and (end - start > 50 or not (start <= hl[0] <= end)):
                start, end = max(1, hl[0] - 12), hl[-1] + 12
            if _is_office(n.file):
                code = self._office_excerpt(n)
                hl = sorted(set(hl) | ({n.line} if n.line and n.props.get("cell") else set()))
            else:
                code = self._read_lines(n.file, max(1, start - 2), min(end + 2, start + 70))
        callers = [x for x in neighbors if x["dir"] == "in" and x["rel"] in (Rel.CALLS, Rel.REFERENCES, Rel.BINDS)]
        callees = [x for x in neighbors if x["dir"] == "out" and x["rel"] in (Rel.CALLS, Rel.REFERENCES, Rel.MAPS_TO,
                                                                             Rel.RENDERS, Rel.USES_MESSAGE)]
        return {"node": n.to_dict(), "code": code, "highlight": hl, "neighbors": neighbors, "callers": callers,
                "callees": callees, "docs": docs_full, "tests": tests, "history": n.props.get("history", [])}

    def _doc_text(self, dn) -> str:
        if _is_office(dn.file):
            # the row / section as extracted (header: value), not the binary file
            return _office_summary(dn)
        lines = self._read_lines(dn.file, dn.line or 1, min((dn.end_line or 1), (dn.line or 1) + 40))
        return "\n".join(l["text"] for l in lines)

    def _office_excerpt(self, n) -> list[dict]:
        """Rows around an Excel row (with row numbers, so the row itself can be
        highlighted), or the text of a Word / Markdown section. Read locally; no AI."""
        path = self._source_path(n.file)
        if path and n.file.lower().endswith(".xlsx") and n.props.get("sheet") and n.line:
            rows = _xlsx_rows(path, os.path.getmtime(path) if os.path.exists(path) else 0, n.props["sheet"])
            if rows:
                hr = int(n.props.get("header_row") or 1)       # the table's own header row
                a, b = max(hr + 1, n.line - 6), min(len(rows), n.line + 3)
                header = [{"n": hr, "text": rows[hr - 1]}] if 0 < hr <= len(rows) else []
                return header + [{"n": i, "text": rows[i - 1]} for i in range(a, b + 1)]
        return [{"n": (n.line or 1) + i, "text": t} for i, t in enumerate((_office_summary(n) or "").splitlines()[:40])]

    def _source_path(self, rel: str | None) -> str | None:
        """Path of a graph file inside the repository / documents folder; None when the
        name would escape them (../, absolute paths, symlinks pointing outside)."""
        if not rel:
            return None
        if rel.startswith("design:"):
            return safe_join(self.docs_dir, rel[len("design:"):]) if self.docs_dir else None
        return safe_join(self.repo, rel) if self.repo else None

    def _read_lines(self, rel: str, a: int, b: int) -> list[dict]:
        if _is_office(rel):
            return []           # binary formats are shown through _office_excerpt
        path = self._source_path(rel)
        if not path or not os.path.isfile(path) or os.path.getsize(path) > max_file_bytes():
            return []
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        return [{"n": i, "text": lines[i - 1]} for i in range(max(1, a), min(len(lines), b) + 1)]

    def ask(self, run_id, node_ids, question) -> str:
        parts = []
        st = self.state(run_id) if run_id else None
        for nid in node_ids[:8]:
            d = self.node_detail(nid, run_id)
            if not d:
                continue
            c = (st or {}).get("candidates", {}).get(nid, {})
            parts.append(f"## {d['node']['name']} ({d['node'].get('file')}:{d['node'].get('line')})\n"
                         f"判定: {c.get('verdict')} {c.get('reason') or ''}\n" +
                         "\n".join(f"{l['n']}: {l['text']}" for l in d["code"][:30]))
        meter = st.setdefault("llm_usage", _new_usage()) if st else None
        out = self.llm.answer(question, "\n\n".join(parts), meter=meter)
        if st:
            self._save(st)
        return out

    def report(self, run_id) -> str:
        st = self.state(run_id)
        out = [f"# 影響分析レポート: {st['title']}", "", f"要求: {st['requirement']}", "",
               f"判定方式: {st['llm']} / run {st['id']}", "", "## 変更意図"]
        for it in st["intents"]:
            out.append(f"- {it['id']} {it['summary']}（{it['change_type']}）")
        out += ["", "## 全数判定の条件"]
        for c in st["checks"]:
            out.append(f"- [{'x' if c['ok'] else ' '}] {c['title']}: {c['detail']}")
        out.append(f"\n承認: {st['approved']['actor'] if st.get('approved') else '未承認'}")
        e = st["estimate"]
        out += ["", f"## 規模: {e.get('days')} 人日（{e.get('low')}〜{e.get('high')}）", "",
                "| 対象 | 種別 | 変更 | 工数(h) | 内訳 |", "|---|---|---|---|---|"]
        for l in e.get("lines", []):
            out.append(f"| {l['name']} | {l['kind']} | {l['change_type']} | {l['hours']} | {' / '.join(l['notes'])} |")
        out += ["", "## 影響箇所（確定）", ""]
        for c in sorted(st["candidates"].values(), key=lambda c: (c["layer"], c["name"])):
            if c.get("decision") and c["decision"]["status"] == "confirmed":
                out.append(f"- {c['name']}（{c['kind']}） {c.get('file') or ''}:{c.get('line') or ''} — {c.get('reason') or ''}")
        out += ["", "## 除外", ""]
        for c in st["candidates"].values():
            if c.get("decision") and c["decision"]["status"] == "excluded":
                out.append(f"- {c['name']} — {c['decision'].get('reason') or ''}（{c['decision']['actor']}）")
        return "\n".join(out)
