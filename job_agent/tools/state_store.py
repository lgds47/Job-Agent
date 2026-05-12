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
