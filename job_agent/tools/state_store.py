"""
State Store
===========
SQLite-backed persistence layer for jobs, applications, and skill gap history.
Single source of truth for the orchestrator across runs.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/job_agent.db")


class StateStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    url         TEXT UNIQUE NOT NULL,
                    title       TEXT,
                    company     TEXT,
                    location    TEXT,
                    score       REAL,
                    status      TEXT DEFAULT 'new',
                    raw_json    TEXT,
                    discovered_at TEXT
                );

                CREATE TABLE IF NOT EXISTS applications (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_url     TEXT NOT NULL UNIQUE,
                    app_dir     TEXT,
                    status      TEXT DEFAULT 'draft',
                    applied_at  TEXT,
                    created_at  TEXT,
                    notes       TEXT
                );

                CREATE TABLE IF NOT EXISTS skill_gaps (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    skill       TEXT,
                    frequency   INTEGER,
                    project_idea TEXT,
                    recorded_at TEXT
                );

                CREATE TABLE IF NOT EXISTS project_ideas (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    gap_skill   TEXT,
                    title       TEXT,
                    option_json TEXT,
                    gap_json    TEXT,
                    status      TEXT DEFAULT 'pending',
                    project_dir TEXT,
                    created_at  TEXT,
                    updated_at  TEXT,
                    UNIQUE (gap_skill, title)
                );

                CREATE TABLE IF NOT EXISTS run_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    command         TEXT NOT NULL,
                    started_at      TEXT,
                    ended_at        TEXT,
                    status          TEXT DEFAULT 'running',
                    jobs_scored     INTEGER DEFAULT 0,
                    early_exits     INTEGER DEFAULT 0,
                    claude_failures INTEGER DEFAULT 0,
                    notes           TEXT
                );
            """)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    def save_jobs(self, jobs: list[dict]):
        """Upsert a list of scored job dicts."""
        now = datetime.now().isoformat()
        with self._conn() as conn:
            for job in jobs:
                if not job.get("url"):
                    continue
                conn.execute("""
                    INSERT INTO jobs (url, title, company, location, score, raw_json, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        company = excluded.company,
                        location = excluded.location,
                        score = excluded.score,
                        raw_json = excluded.raw_json,
                        discovered_at = excluded.discovered_at
                """, (
                    job.get("url"),
                    job.get("title"),
                    job.get("company"),
                    job.get("location"),
                    job.get("score"),
                    json.dumps(job),
                    now
                ))

    def get_recent_jobs(self, n: int = 50, min_score: float = 0.0) -> list[dict]:
        """Return the n most recently discovered jobs above a score threshold."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT raw_json FROM jobs
                WHERE score >= ?
                ORDER BY discovered_at DESC
                LIMIT ?
            """, (min_score, n)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def update_job_status(self, url: str, status: str):
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET status = ? WHERE url = ?", (status, url))

    # ── Applications ──────────────────────────────────────────────────────────

    def save_application(self, job_url: str, app_dir: str):
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO applications (job_url, app_dir, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_url) DO UPDATE SET
                    app_dir    = excluded.app_dir,
                    created_at = excluded.created_at
            """, (job_url, app_dir, now))

    def update_application_status(self, job_url: str, status: str, notes: str = None):
        """Update status: draft → applied → interview → offer → rejected."""
        with self._conn() as conn:
            conn.execute("""
                UPDATE applications
                SET status = ?,
                    applied_at = CASE WHEN ? = 'applied' THEN ? ELSE applied_at END,
                    notes = COALESCE(?, notes)
                WHERE job_url = ?
            """, (status, status, datetime.now().isoformat(), notes, job_url))

    def get_applications(self, status: str = None) -> list[dict]:
        query = "SELECT * FROM applications"
        params = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Skill Gaps ────────────────────────────────────────────────────────────

    def save_skill_gaps(self, gaps: list[dict]):
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM skill_gaps")  # replace with latest analysis
            for gap in gaps:
                conn.execute("""
                    INSERT INTO skill_gaps (skill, frequency, project_idea, recorded_at)
                    VALUES (?, ?, ?, ?)
                """, (gap.get("skill"), gap.get("frequency"), gap.get("project_idea"), now))

    def get_skill_gaps(self) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM skill_gaps ORDER BY frequency DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Project Ideas ─────────────────────────────────────────────────────────

    def save_project_ideas(self, gap: dict, options: list[dict]) -> int:
        """Insert each planner-generated option as a pending project idea.
        Existing (gap_skill, title) rows are preserved untouched so we never
        clobber a pending or completed idea. Returns the number of new rows.
        """
        now = datetime.now().isoformat()
        gap_skill = (gap.get("skill") or "").strip()
        gap_blob = json.dumps(gap)
        inserted = 0
        with self._conn() as conn:
            for opt in options or []:
                if not isinstance(opt, dict):
                    continue
                title = (opt.get("title") or "").strip()
                if not title:
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO project_ideas
                        (gap_skill, title, option_json, gap_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT (gap_skill, title) DO NOTHING
                    """,
                    (gap_skill, title, json.dumps(opt), gap_blob, now, now),
                )
                if cur.rowcount:
                    inserted += 1
        return inserted

    def get_pending_project_idea(self) -> dict | None:
        """Return the most recently created pending idea (or None)."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM project_ideas
                WHERE status = 'pending'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def get_project_ideas(self, status: str | None = None) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            if status:
                rows = conn.execute(
                    "SELECT * FROM project_ideas WHERE status = ? ORDER BY created_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM project_ideas ORDER BY created_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def update_project_idea(
        self,
        idea_id: int,
        status: str,
        project_dir: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE project_ideas
                SET status = ?,
                    project_dir = COALESCE(?, project_dir),
                    updated_at = ?
                WHERE id = ?
                """,
                (status, project_dir, datetime.now().isoformat(), idea_id),
            )

    # ── Run History ───────────────────────────────────────────────────────────

    def start_run(self, command: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO run_history (command, started_at, status)
                VALUES (?, ?, 'running')
                """,
                (command, datetime.now().isoformat()),
            )
            return int(cur.lastrowid)

    def end_run(
        self,
        run_id: int,
        status: str = "ok",
        jobs_scored: int = 0,
        early_exits: int = 0,
        claude_failures: int = 0,
        notes: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE run_history
                SET ended_at = ?,
                    status = ?,
                    jobs_scored = ?,
                    early_exits = ?,
                    claude_failures = ?,
                    notes = ?
                WHERE id = ?
                """,
                (
                    datetime.now().isoformat(),
                    status,
                    int(jobs_scored or 0),
                    int(early_exits or 0),
                    int(claude_failures or 0),
                    notes,
                    run_id,
                ),
            )

    def get_run_history(self, n: int = 20) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?",
                (n,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_jobs(self) -> list[dict]:
        """Return all jobs, most-recently-discovered first, with their raw payloads merged."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT url, title, company, location, score, status,
                       raw_json, discovered_at
                FROM jobs
                ORDER BY discovered_at DESC
                """
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            row = dict(r)
            try:
                raw = json.loads(row.pop("raw_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                raw = {}
            # Top-level row fields win over raw_json copies, so the canonical
            # discovered_at/score from the column is what callers see.
            merged = {**raw, **{k: v for k, v in row.items() if v is not None}}
            out.append(merged)
        return out

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        with self._conn() as conn:
            jobs_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            apps_by_status = dict(conn.execute("""
                SELECT status, COUNT(*) FROM applications GROUP BY status
            """).fetchall())
            score_dist = dict(conn.execute("""
                SELECT
                    CASE
                        WHEN score >= 90 THEN '90-100'
                        WHEN score >= 70 THEN '70-89'
                        WHEN score >= 50 THEN '50-69'
                        ELSE '<50'
                    END AS bucket,
                    COUNT(*) AS n
                FROM jobs
                GROUP BY bucket
            """).fetchall())
        return {
            "jobs_discovered": jobs_total,
            "score_distribution": score_dist,
            "applications": apps_by_status,
        }


if __name__ == "__main__":
    store = StateStore()
    print(json.dumps(store.summary(), indent=2))
