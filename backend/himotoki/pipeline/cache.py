"""Content-addressed cache of extracted facts.

Key = (path, content hash, extractor name, extractor version, rule-pack hash).
For git repositories the content hash is the blob id from `git ls-files -s`,
so deciding what changed costs no file reads at all.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib


class FactCache:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        try:
            self._open()
        except sqlite3.DatabaseError:
            # the cache only saves time: a damaged file is set aside and rebuilt
            os.replace(path, f"{path}.corrupt-{int(time.time())}")
            self._open()

    def _open(self):
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS facts (path TEXT, hash TEXT, extractor TEXT, "
                            "version TEXT, data BLOB, PRIMARY KEY (path, hash, extractor, version))")
            self.db.execute("PRAGMA quick_check").fetchone()
            self.db.commit()

    def get(self, path, h, extractor, version):
        with self.lock:
            row = self.db.execute("SELECT data FROM facts WHERE path=? AND hash=? AND extractor=? AND version=?",
                                  (path, h, extractor, version)).fetchone()
        if not row:
            return None
        try:
            return json.loads(zlib.decompress(row[0]))
        except (zlib.error, ValueError):
            return None            # unreadable entry: extract the file again

    def put_many(self, rows):
        """rows: iterable of (path, hash, extractor, version, facts_dict)"""
        data = [(p, h, e, v, zlib.compress(json.dumps(f, ensure_ascii=False).encode("utf-8")))
                for p, h, e, v, f in rows]
        with self.lock:
            self.db.executemany("INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?)", data)
            self.db.commit()

    def stats(self):
        with self.lock:
            return {"entries": self.db.execute("SELECT count(*) FROM facts").fetchone()[0]}
