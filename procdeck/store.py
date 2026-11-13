"""SQLite persistence: runs, steps, history samples, per-script stats."""
from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, name TEXT, description TEXT, spec_path TEXT, spec_text TEXT,
  submitted_at REAL, submitted_by TEXT, priority INTEGER, max_parallel INTEGER,
  state TEXT, paused INTEGER DEFAULT 0, finished_at REAL
);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT, key TEXT, name TEXT, item TEXT, seq INTEGER,
  cmd TEXT, cwd TEXT, env TEXT, profile TEXT, mem_gb REAL, threads INTEGER, retries INTEGER,
  timeout_s REAL, on_fail TEXT, shell INTEGER, env_vars TEXT, needs TEXT, start_at REAL, when_idle INTEGER,
  step_max_parallel INTEGER, script_key TEXT,
  state TEXT, attempt INTEGER DEFAULT 0, pid INTEGER, pid_ctime REAL, child_pid INTEGER,
  started_at REAL, ended_at REAL, exit_code INTEGER, error TEXT, held_reason TEXT,
  peak_rss REAL DEFAULT 0, cpu_seconds REAL DEFAULT 0, log_path TEXT, status_path TEXT, job_name TEXT,
  PRIMARY KEY (run_id, key)
);
CREATE TABLE IF NOT EXISTS sys_samples (ts REAL, cpu REAL, ram_used REAL, ram_pct REAL, commit_pct REAL);
CREATE INDEX IF NOT EXISTS sys_samples_ts ON sys_samples(ts);
CREATE TABLE IF NOT EXISTS job_samples (ts REAL, gid TEXT, label TEXT, kind TEXT, cpu REAL, rss REAL);
CREATE INDEX IF NOT EXISTS job_samples_ts ON job_samples(ts);
"""

STEP_COLS = ["run_id", "key", "name", "item", "seq", "cmd", "cwd", "env", "profile", "mem_gb", "threads", "retries",
             "timeout_s", "on_fail", "shell", "env_vars", "needs", "start_at", "when_idle", "step_max_parallel",
             "script_key", "state", "attempt", "pid", "pid_ctime", "child_pid", "started_at", "ended_at", "exit_code",
             "error", "held_reason", "peak_rss", "cpu_seconds", "log_path", "status_path", "job_name"]
JSON_COLS = {"env_vars", "needs"}


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "hidden" not in cols:  # added after the first release; 1 = forgotten (kept for history, not listed)
            self.db.execute("ALTER TABLE runs ADD COLUMN hidden INTEGER DEFAULT 0")

    # ---------------------------------------------------------------- generic
    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for c in JSON_COLS & d.keys():
                d[c] = json.loads(d[c]) if d[c] else ([] if c == "needs" else {})
            out.append(d)
        return out

    def x(self, sql: str, args=()) -> None:
        with self.lock:
            self.db.execute(sql, args)

    # ---------------------------------------------------------------- runs
    def add_run(self, run: dict, steps: list[dict]) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    "INSERT INTO runs (id,name,description,spec_path,spec_text,submitted_at,submitted_by,priority,"
                    "max_parallel,state,paused) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                    (run["id"], run["name"], run.get("description", ""), run.get("spec_path"), run.get("spec_text"),
                     run["submitted_at"], run.get("submitted_by", ""), run.get("priority", 0),
                     run.get("max_parallel"), "queued"))
                for s in steps:
                    row = {c: s.get(c) for c in STEP_COLS}
                    row["run_id"] = run["id"]
                    row["state"] = "waiting"
                    row["attempt"] = 0
                    row["shell"] = int(bool(s.get("shell")))
                    row["when_idle"] = int(bool(s.get("when_idle")))
                    for c in JSON_COLS:
                        row[c] = json.dumps(s.get(c) or ([] if c == "needs" else {}))
                    cols = ",".join(row)
                    self.db.execute(f"INSERT INTO steps ({cols}) VALUES ({','.join('?' * len(row))})",
                                    tuple(row.values()))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def runs(self, limit: int = 50, include_hidden: bool = False) -> list[dict]:
        where = "" if include_hidden else "WHERE COALESCE(hidden,0)=0 "
        return self.q("SELECT id,name,description,spec_path,submitted_at,submitted_by,priority,max_parallel,state,"
                      f"paused,finished_at,COALESCE(hidden,0) AS hidden FROM runs {where}"
                      "ORDER BY submitted_at DESC LIMIT ?", (limit,))

    def set_hidden(self, run_ids: list[str], hidden: bool) -> None:
        with self.lock:
            self.db.executemany("UPDATE runs SET hidden=? WHERE id=?", [(int(hidden), i) for i in run_ids])

    def active_runs(self) -> list[dict]:
        return self.q("SELECT * FROM runs WHERE state IN ('queued','running') ORDER BY priority DESC, submitted_at")

    def run(self, run_id: str) -> dict | None:
        r = self.q("SELECT * FROM runs WHERE id=?", (run_id,))
        return r[0] if r else None

    def find_run(self, ref: str) -> dict | None:
        """By id, id prefix, or name (most recent)."""
        r = self.run(ref)
        if r:
            return r
        r = self.q("SELECT * FROM runs WHERE id LIKE ? ORDER BY submitted_at DESC LIMIT 1", (ref + "%",))
        if r:
            return r[0]
        r = self.q("SELECT * FROM runs WHERE name=? ORDER BY COALESCE(hidden,0), submitted_at DESC LIMIT 1", (ref,))
        return r[0] if r else None

    def update_run(self, run_id: str, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE runs SET {sets} WHERE id=?", (*kw.values(), run_id))

    def steps(self, run_id: str) -> list[dict]:
        return self.q("SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,))

    def apply_layout(self, run_id: str, order: list[str] | None, needs: dict[str, list[str]]) -> None:
        """Atomically renumber step `seq` (queue order) and/or rewrite some steps' `needs`."""
        with self.lock:
            self.db.execute("BEGIN")
            try:
                if order is not None:
                    self.db.executemany("UPDATE steps SET seq=? WHERE run_id=? AND key=?",
                                        [(i, run_id, k) for i, k in enumerate(order)])
                self.db.executemany("UPDATE steps SET needs=?, held_reason=NULL WHERE run_id=? AND key=?",
                                    [(json.dumps(v), run_id, k) for k, v in needs.items()])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def running_steps(self) -> list[dict]:
        return self.q("SELECT s.*, r.name AS run_name FROM steps s JOIN runs r ON r.id=s.run_id "
                      "WHERE s.state='running'")

    def update_step(self, run_id: str, key: str, **kw) -> None:
        for c in JSON_COLS & kw.keys():
            kw[c] = json.dumps(kw[c])
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE steps SET {sets} WHERE run_id=? AND key=?", (*kw.values(), run_id, key))

    # ---------------------------------------------------------------- history
    def add_samples(self, ts: float, system: dict, groups: list[dict]) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("INSERT INTO sys_samples VALUES (?,?,?,?,?)",
                            (ts, system["cpu"], system["ram_used"], system["ram_pct"], system["commit_pct"]))
            self.db.executemany("INSERT INTO job_samples VALUES (?,?,?,?,?,?)",
                                [(ts, g["id"], g["label"], g["kind"], g["cpu"], g["rss"]) for g in groups])
            self.db.execute("COMMIT")

    def prune(self, days: float) -> None:
        cut = time.time() - days * 86400
        self.x("DELETE FROM sys_samples WHERE ts<?", (cut,))
        self.x("DELETE FROM job_samples WHERE ts<?", (cut,))

    def sys_history(self, since: float, max_points: int = 600) -> list[dict]:
        rows = self.q("SELECT ts,cpu,ram_pct,commit_pct FROM sys_samples WHERE ts>=? ORDER BY ts", (since,))
        if len(rows) > max_points:
            step = len(rows) / max_points
            rows = [rows[int(i * step)] for i in range(max_points)]
        return rows

    def top_jobs_history(self, since: float, n: int = 8) -> list[dict]:
        """Peak RSS / mean CPU of the biggest jobs since `since`."""
        return self.q("SELECT gid, label, kind, MAX(rss) AS peak_rss, AVG(cpu) AS mean_cpu, MIN(ts) AS first, "
                      "MAX(ts) AS last FROM job_samples WHERE ts>=? AND kind IN ('job','managed') "
                      "GROUP BY gid ORDER BY peak_rss DESC LIMIT ?", (since, n))

    def script_stats(self, pattern: str | None = None) -> list[dict]:
        rows = self.q("SELECT script_key, ended_at-started_at AS dur, peak_rss, cpu_seconds FROM steps "
                      "WHERE state='done' AND script_key!='' AND started_at IS NOT NULL")
        by: dict[str, list[dict]] = {}
        for r in rows:
            if pattern and pattern.lower() not in r["script_key"].lower():
                continue
            by.setdefault(r["script_key"], []).append(r)
        out = []
        for k, rs in by.items():
            out.append({
                "script_key": k, "runs": len(rs),
                "median_s": statistics.median(r["dur"] for r in rs),
                "peak_rss": max(r["peak_rss"] or 0 for r in rs),
                "p90_rss": sorted(r["peak_rss"] or 0 for r in rs)[int(0.9 * (len(rs) - 1))],
                "cpu_hours": sum(r["cpu_seconds"] or 0 for r in rs) / 3600,
            })
        return sorted(out, key=lambda r: -r["runs"])

    def estimate(self, script_key: str) -> dict | None:
        """Typical duration and peak RAM for a script from past successful runs."""
        if not script_key:
            return None
        rows = self.q("SELECT ended_at-started_at AS dur, peak_rss FROM steps WHERE state='done' AND script_key=? "
                      "ORDER BY ended_at DESC LIMIT 20", (script_key,))
        if not rows:
            return None
        return {"n": len(rows), "median_s": statistics.median(r["dur"] for r in rows),
                "peak_gb": max(r["peak_rss"] or 0 for r in rows) / 1024 ** 3}
