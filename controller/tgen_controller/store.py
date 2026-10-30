# SPDX-License-Identifier: MIT
"""SQLite history (docs/08 §5). Small, short statements; WAL so readers never wait for the engine."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, name TEXT, state TEXT, created_ms INTEGER,
  started_ms INTEGER, ended_ms INTEGER, scenario TEXT, dut TEXT, summary TEXT, error TEXT, dir TEXT);
CREATE TABLE IF NOT EXISTS phases (run_id TEXT, idx INTEGER, type TEXT, params TEXT, at_ms INTEGER, end_ms INTEGER,
  PRIMARY KEY (run_id, idx));
CREATE TABLE IF NOT EXISTS events (run_id TEXT, t_ms INTEGER, kind TEXT, severity TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS ticks (run_id TEXT, t_ms INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, t_ms);
CREATE INDEX IF NOT EXISTS ticks_run ON ticks (run_id, t_ms);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _x(self, sql: str, args=()):
        with self.lock:
            return self.db.execute(sql, args)

    # runs
    def create_run(self, rid: str, name: str, scenario: dict, rdir: str) -> None:
        self._x("INSERT INTO runs (id, name, state, created_ms, scenario, dir) VALUES (?,?,?,?,?,?)",
                (rid, name, "created", now_ms(), json.dumps(scenario), rdir))

    def update_run(self, rid: str, **kw) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in kw.values()]
        self._x(f"UPDATE runs SET {cols} WHERE id=?", (*vals, rid))

    def run(self, rid: str) -> dict | None:
        r = self._x("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
        return _run(r) if r else None

    def runs(self, limit: int = 50) -> list[dict]:
        return [_run(r) for r in self._x("SELECT * FROM runs ORDER BY created_ms DESC LIMIT ?", (limit,))]

    def delete_run(self, rid: str) -> None:
        for t in ("runs", "phases", "events", "ticks"):
            self._x(f"DELETE FROM {t} WHERE {'id' if t == 'runs' else 'run_id'}=?", (rid,))

    def fail_unfinished(self) -> list[str]:
        """A controller restart during a run: mark it failed (the agents' watchdog stopped them)."""
        ids = [r["id"] for r in self._x("SELECT id FROM runs WHERE state IN ('created','preparing','running','finishing')")]
        for rid in ids:
            self.update_run(rid, state="failed", error="controller restarted during the run", ended_ms=now_ms())
        return ids

    # phases, events, ticks
    def phase(self, rid: str, idx: int, typ: str, params: dict, at_ms: int) -> None:
        self._x("INSERT OR REPLACE INTO phases VALUES (?,?,?,?,?,NULL)", (rid, idx, typ, json.dumps(params), at_ms))

    def phase_end(self, rid: str, idx: int, end_ms: int) -> None:
        self._x("UPDATE phases SET end_ms=? WHERE run_id=? AND idx=?", (end_ms, rid, idx))

    def phases(self, rid: str) -> list[dict]:
        return [dict(r) | {"params": json.loads(r["params"])}
                for r in self._x("SELECT idx, type, params, at_ms, end_ms FROM phases WHERE run_id=? ORDER BY idx", (rid,))]

    def event(self, rid: str, kind: str, severity: str, text: str, t_ms: int | None = None) -> dict:
        e = {"t_ms": t_ms or now_ms(), "kind": kind, "severity": severity, "text": text}
        self._x("INSERT INTO events VALUES (?,?,?,?,?)", (rid, e["t_ms"], kind, severity, text))
        return e

    def events(self, rid: str) -> list[dict]:
        return [dict(r) for r in self._x("SELECT t_ms, kind, severity, text FROM events WHERE run_id=? ORDER BY t_ms", (rid,))]

    def tick(self, rid: str, t_ms: int, data: dict) -> None:
        self._x("INSERT INTO ticks VALUES (?,?,?)", (rid, t_ms, json.dumps(data)))

    def ticks(self, rid: str) -> list[dict]:
        return [json.loads(r["data"]) for r in self._x("SELECT data FROM ticks WHERE run_id=? ORDER BY t_ms", (rid,))]


def _run(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("scenario", "dut", "summary"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d
