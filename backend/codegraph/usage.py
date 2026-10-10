"""AI usage metering: per-run counters that update live, and a workspace-wide
ledger of every API call (for daily / monthly totals)."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Callable

from .settings import Pricing


class UsageLedger:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL, graph TEXT, run_id TEXT, purpose TEXT, "
                            "model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts)")
            self.db.commit()

    def record(self, graph, run_id, purpose, model, input_tokens, output_tokens, cost_usd) -> None:
        with self.lock:
            self.db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                            (time.time(), graph, run_id, purpose, model, input_tokens, output_tokens, cost_usd))
            self.db.commit()

    def summary(self, since: float, until: float | None = None) -> dict:
        q = ("SELECT COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
             "COALESCE(SUM(cost_usd),0), COALESCE(SUM(cost_usd IS NULL),0) FROM calls WHERE ts>=? AND ts<?")
        with self.lock:
            n, i, o, usd, unpriced = self.db.execute(q, (since, until or time.time() + 1)).fetchone()
        return {"calls": n, "input_tokens": i, "output_tokens": o, "cost_usd": usd, "unpriced_calls": unpriced}

    def by_day(self, since: float) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') d, COUNT(*), SUM(input_tokens), SUM(output_tokens), "
                "COALESCE(SUM(cost_usd),0) FROM calls WHERE ts>=? GROUP BY d ORDER BY d", (since,)).fetchall()
        return [{"date": d, "calls": n, "input_tokens": i, "output_tokens": o, "cost_usd": c} for d, n, i, o, c in rows]


def new_usage() -> dict:
    return {"ai_items": 0, "reused": 0, "rule_low_score": 0, "rule_cap": 0, "rule": 0, "fallback": 0,
            "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "cost_jpy": 0.0,
            "unpriced_calls": 0, "model": None, "jpy_per_usd": None}


class UsageMeter(dict):
    """A run's usage counters (a plain dict when saved) that also prices each API
    call, writes it to the ledger and notifies listeners (the viewer) right away."""

    def __init__(self, data: dict, pricing: Pricing, ledger: UsageLedger | None = None,
                 graph: str | None = None, run_id: str | None = None,
                 on_update: Callable[[dict], None] | None = None):
        super().__init__(new_usage())
        self.update(data or {})
        self.pricing, self.ledger, self.graph, self.run_id, self.on_update = pricing, ledger, graph, run_id, on_update
        self["jpy_per_usd"] = pricing.jpy_per_usd

    def on_call(self, model: str, input_tokens: int, output_tokens: int, purpose: str = "") -> None:
        cost = self.pricing.cost_usd(model, input_tokens, output_tokens)
        self["calls"] += 1
        self["input_tokens"] += input_tokens
        self["output_tokens"] += output_tokens
        self["model"] = model
        if cost is None:
            self["unpriced_calls"] += 1
        else:
            self["cost_usd"] = round(self["cost_usd"] + cost, 6)
        self["jpy_per_usd"] = self.pricing.jpy_per_usd
        self["cost_jpy"] = round(self["cost_usd"] * self.pricing.jpy_per_usd, 2)
        if self.ledger:
            try:
                self.ledger.record(self.graph, self.run_id, purpose, model, input_tokens, output_tokens, cost)
            except sqlite3.Error:
                pass      # the ledger is for reporting; a failure must not stop the analysis
        if self.on_update:
            self.on_update(dict(self))
