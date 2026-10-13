"""Persistence for runs and their event streams (SQLite), plus an in-process
event bus that fans events out to WebSocket subscribers from any thread."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time


class RunStore:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "runs.db"), check_same_thread=False)
        self.lock = threading.Lock()
        self._last_commit = 0.0
        self._pending: list[tuple] = []      # events not yet written (no transaction is left open)
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created REAL, state TEXT);
            CREATE TABLE IF NOT EXISTS events (run_id TEXT, seq INTEGER, ts REAL, type TEXT, data TEXT,
                                               PRIMARY KEY (run_id, seq));
            """)
            self.db.commit()

    def save(self, state: dict) -> None:
        with self.lock:
            self._flush()
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",
                            (state["id"], state["created_at"], json.dumps(state, ensure_ascii=False)))
            self.db.commit()

    def load(self, run_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT state FROM runs ORDER BY created DESC").fetchall()
        out = []
        for (s,) in rows:
            d = json.loads(s)
            out.append({k: d.get(k) for k in ("id", "parent_id", "title", "requirement", "created_at",
                                              "status", "phase", "progress")})
        return out

    def append_event(self, run_id: str, seq: int, etype: str, data: dict) -> dict:
        ev = {"seq": seq, "ts": time.time(), "type": etype, "data": data}
        with self.lock:
            # a run emits thousands of events; one transaction per event cost ~1 ms each.
            # They are written in batches (each batch its own short transaction, so other
            # processes sharing runs.db are never locked out) and at the end of a run.
            self._pending.append((run_id, seq, ev["ts"], etype, json.dumps(data, ensure_ascii=False)))
            if ev["ts"] - self._last_commit > 0.5 or etype in ("run.done", "error"):
                self._flush()
        return ev

    def _flush(self) -> None:
        if self._pending:
            self.db.executemany("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)", self._pending)
            self.db.commit()
            self._pending = []
        self._last_commit = time.time()

    def events(self, run_id: str, after: int = -1) -> list[dict]:
        with self.lock:
            self._flush()
            rows = self.db.execute("SELECT seq, ts, type, data FROM events WHERE run_id=? AND seq>? "
                                   "ORDER BY seq", (run_id, after)).fetchall()
        return [{"seq": r[0], "ts": r[1], "type": r[2], "data": json.loads(r[3])} for r in rows]


class EventBus:
    def __init__(self):
        self.subs: dict[str, set] = {}
        self.lock = threading.Lock()

    def subscribe(self, run_id: str, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self.lock:
            self.subs.setdefault(run_id, set()).add((loop, q))
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        with self.lock:
            self.subs[run_id] = {s for s in self.subs.get(run_id, set()) if s[1] is not q}

    def publish(self, run_id: str, event: dict) -> None:
        with self.lock:
            subs = list(self.subs.get(run_id, ()))
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:
                pass
