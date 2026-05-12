"""
State Store
===========
SQLite-backed persistence layer for jobs, applications, skill gaps,
project-idea backlog, and run history.
Single source of truth for the orchestrator across runs.
"""

import json
import sqlite3
import hashlib
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
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    idea_key        TEXT UNIQUE NOT NULL,
                    skill           TEXT,
                    title           TEXT,
                    status          TEXT DEFAULT 'not_started',
                    option_json     TEXT NOT NULL,
                    gap_json        TEXT,
                    project_dir     TEXT,
                    selected_count  INTEGER DEFAULT 0,
                    created_at      TEXT,
                    updated_at      TEXT,
                    last_selected_at TEXT,
                    run_source      TEXT
                );

                CREATE TABLE IF NOT EXISTS run_history (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    command             TEXT NOT NULL,
                    status              TEXT NOT NULL,
                    jobs_scored         INTEGER DEFAULT 0,
                    early_exit_triggered INTEGER DEFAULT 0,
                    claude_failures     INTEGER DEFAULT 0,
                    metadata_json       TEXT,
                    started_at          TEXT,
                    finished_at         TEXT
                );
            """)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _loads_json_safe(value: str | None):
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

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

    def get_all_jobs(self) -> list[dict]:
        """Return all jobs with parsed metadata from raw_json."""
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT url, title, company, location, score, status, raw_json, discovered_at
                FROM jobs
                ORDER BY discovered_at DESC
            """).fetchall()

        jobs = []
        for row in rows:
            item = dict(row)
            raw = self._loads_json_safe(item.get("raw_json")) or {}
            item["metadata"] = {
                "source": raw.get("source"),
                "required_skills": raw.get("required_skills", []),
                "preferred_skills": raw.get("preferred_skills", []),
                "match_reasons": raw.get("match_reasons", []),
                "gap_reasons": raw.get("gap_reasons", []),
                "early_career_fit": raw.get("early_career_fit"),
                "seniority_fit": raw.get("seniority_fit"),
            }
            jobs.append(item)
        return jobs

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

    def save_project_ideas(self, gap: dict, options: list[dict], run_source: str | None = None) -> int:
        """Upsert planner-generated options as project ideas."""
        now = datetime.now().isoformat()
        skill = str(gap.get("skill") or "").strip()
        gap_json = json.dumps(gap)
        saved = 0

        with self._conn() as conn:
            for opt in options:
                if not isinstance(opt, dict):
                    continue
                title = str(opt.get("title") or "").strip() or "Untitled project idea"
                key_seed = f"{skill.lower()}::{title.lower()}::{opt.get('elevator_pitch', '')}"
                idea_key = hashlib.sha256(key_seed.encode("utf-8")).hexdigest()[:16]
                conn.execute("""
                    INSERT INTO project_ideas (
                        idea_key, skill, title, status, option_json, gap_json,
                        created_at, updated_at, run_source
                    )
                    VALUES (?, ?, ?, 'not_started', ?, ?, ?, ?, ?)
                    ON CONFLICT(idea_key) DO UPDATE SET
                        skill = excluded.skill,
                        title = excluded.title,
                        option_json = excluded.option_json,
                        gap_json = excluded.gap_json,
                        updated_at = excluded.updated_at,
                        run_source = excluded.run_source
                """, (
                    idea_key, skill, title, json.dumps(opt), gap_json, now, now, run_source
                ))
                saved += 1
        return saved

    def get_project_ideas(self, include_completed: bool = True) -> list[dict]:
        query = "SELECT * FROM project_ideas"
        if not include_completed:
            query += " WHERE status != 'completed'"
        query += " ORDER BY created_at DESC, id DESC"
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()

        ideas = []
        for row in rows:
            item = dict(row)
            item["option"] = self._loads_json_safe(item.get("option_json")) or {}
            item["gap"] = self._loads_json_safe(item.get("gap_json")) or {}
            ideas.append(item)
        return ideas

    def pick_next_project_idea(self) -> dict | None:
        """
        Pick one idea for the next builder handoff.
        Preference order: not_started -> in_progress -> everything else (except completed).
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT *
                FROM project_ideas
                WHERE status != 'completed'
                ORDER BY
                    CASE status
                        WHEN 'not_started' THEN 0
                        WHEN 'in_progress' THEN 1
                        ELSE 2
                    END,
                    COALESCE(last_selected_at, created_at) ASC,
                    created_at ASC,
                    id ASC
                LIMIT 1
            """).fetchone()
            if not row:
                return None

            now = datetime.now().isoformat()
            conn.execute("""
                UPDATE project_ideas
                SET selected_count = selected_count + 1,
                    last_selected_at = ?,
                    updated_at = ?
                WHERE idea_key = ?
            """, (now, now, row["idea_key"]))

            picked = dict(row)
            picked["selected_count"] = int(picked.get("selected_count") or 0) + 1
            picked["last_selected_at"] = now
            picked["option"] = self._loads_json_safe(picked.get("option_json")) or {}
            picked["gap"] = self._loads_json_safe(picked.get("gap_json")) or {}
            return picked

    def update_project_idea_status(self, idea_key: str, status: str, project_dir: str | None = None):
        now = datetime.now().isoformat()
        with self._conn() as conn:
            if project_dir:
                conn.execute("""
                    UPDATE project_ideas
                    SET status = ?, project_dir = ?, updated_at = ?
                    WHERE idea_key = ?
                """, (status, project_dir, now, idea_key))
            else:
                conn.execute("""
                    UPDATE project_ideas
                    SET status = ?, updated_at = ?
                    WHERE idea_key = ?
                """, (status, now, idea_key))

    # ── Run History ────────────────────────────────────────────────────────────

    def record_run(
        self,
        command: str,
        status: str,
        jobs_scored: int = 0,
        early_exit_triggered: bool = False,
        claude_failures: int = 0,
        metadata: dict | None = None,
    ):
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO run_history (
                    command, status, jobs_scored, early_exit_triggered, claude_failures,
                    metadata_json, started_at, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                command,
                status,
                int(jobs_scored or 0),
                1 if early_exit_triggered else 0,
                int(claude_failures or 0),
                json.dumps(metadata or {}),
                now,
                now,
            ))

    def get_run_history(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT *
                FROM run_history
                ORDER BY finished_at DESC, id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._loads_json_safe(item.get("metadata_json")) or {}
            history.append(item)
        return history

    def get_run_history_summary(self) -> list[dict]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT
                    command,
                    COUNT(*) AS runs,
                    SUM(jobs_scored) AS jobs_scored,
                    SUM(early_exit_triggered) AS early_exits,
                    SUM(claude_failures) AS claude_failures
                FROM run_history
                GROUP BY command
                ORDER BY command ASC
            """).fetchall()
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
