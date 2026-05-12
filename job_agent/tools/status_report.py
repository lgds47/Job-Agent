"""
Status Report
=============
Read-only dashboard data collector + formatters for the job-agent pipeline.

The `status` subcommand calls `collect_status()` to assemble everything we
know about the pipeline's output and behavior (jobs scored, application
packages on disk, project ideas + completion state, recent runs) and then
hands it to one of the formatters (`format_text`, `format_json`,
`format_html`).

This module is self-contained — no third-party dependencies, no LLM
calls. Safe to run without ``ANTHROPIC_API_KEY``. It is the only consumer
of the state_store schema for the dashboard, so it stays in sync with
the ``last_run_stats`` shape that ``SearchAgent`` emits (see
``agents/search_agent.py``):

  - companies_discovered / companies_processed
  - raw_postings / postings_after_role_filter
  - jobs_scored / qualified_jobs
  - early_exit_triggered (bool)
  - low_score_threshold / consecutive_low_score_limit
  - claude_failures
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from tools.state_store import StateStore

APPLICATIONS_DIR = Path("data/applications")
PROJECTS_DIR = Path("data/projects")
APP_REQUIRED_FILES = ("jd.json", "tailored_resume.json", "cover_letter.md", "meta.json")
PROJECT_COMPLETED_STATUS = "completed"
COMPLETED_MARKER_FILENAME = "completed.json"


# ── Collection ───────────────────────────────────────────────────────────────


def collect_status(
    store: StateStore | None = None,
    *,
    applications_dir: Path = APPLICATIONS_DIR,
    projects_dir: Path = PROJECTS_DIR,
    run_history_limit: int = 20,
) -> dict:
    """Gather all pipeline state into a single serializable dict."""
    store = store or StateStore()

    jobs = store.get_all_jobs()
    applications_db = store.get_applications()
    project_ideas = store.get_project_ideas(include_completed=True)
    run_history = store.get_run_history(limit=run_history_limit)
    run_history_totals = store.get_run_history_summary()

    app_dirs = _scan_application_dirs(applications_dir, applications_db)
    project_dirs = _scan_project_dirs(projects_dir)

    # Cross-reference idea → on-disk completion state so the dashboard
    # surfaces drift between the DB row and what actually exists.
    for idea in project_ideas:
        idea["completion_state"] = _project_completion_state(idea.get("project_dir"))

    return {
        "generated_at": datetime.now().isoformat(),
        "jobs": jobs,
        "job_summary": _summarize_jobs(jobs),
        "applications": app_dirs,
        "applications_db": applications_db,
        "project_ideas": project_ideas,
        "project_ideas_summary": _summarize_project_ideas(project_ideas),
        "project_dirs": project_dirs,
        "run_history": run_history,
        "run_history_summary": _summarize_run_history(run_history),
        "run_history_totals_by_command": run_history_totals,
    }


def _project_completion_state(project_dir: str | None) -> str:
    if not project_dir:
        return "not_started"
    path = Path(project_dir)
    if not path.exists():
        return "missing_dir"
    if (path / COMPLETED_MARKER_FILENAME).exists():
        return "completed"
    meta = _safe_load_json(path / "meta.json")
    if str(meta.get("status", "")).lower() == PROJECT_COMPLETED_STATUS:
        return "completed"
    return "in_progress"


def _safe_load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _summarize_jobs(jobs: list[dict]) -> dict:
    buckets = {"90-100": 0, "70-89": 0, "50-69": 0, "<50": 0}
    for j in jobs:
        try:
            s = float(j.get("score") or 0)
        except (TypeError, ValueError):
            s = 0.0
        if s >= 90:
            buckets["90-100"] += 1
        elif s >= 70:
            buckets["70-89"] += 1
        elif s >= 50:
            buckets["50-69"] += 1
        else:
            buckets["<50"] += 1
    return {"total": len(jobs), "by_bucket": buckets}


def _summarize_project_ideas(ideas: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    for i in ideas:
        key = i.get("status") or "unknown"
        by_status[key] = by_status.get(key, 0) + 1
    return {"total": len(ideas), "by_status": by_status}


def _summarize_run_history(runs: list[dict]) -> dict:
    by_command: dict[str, int] = {}
    totals = {
        "runs": len(runs),
        "jobs_scored": 0,
        "early_exits": 0,
        "claude_failures": 0,
        "errors": 0,
    }
    for r in runs:
        cmd = r.get("command") or "unknown"
        by_command[cmd] = by_command.get(cmd, 0) + 1
        totals["jobs_scored"] += int(r.get("jobs_scored") or 0)
        totals["early_exits"] += int(r.get("early_exit_triggered") or 0)
        totals["claude_failures"] += int(r.get("claude_failures") or 0)
        status = (r.get("status") or "").lower()
        if status in {"error", "failed"}:
            totals["errors"] += 1
    return {"by_command": by_command, "totals": totals}


def _scan_application_dirs(applications_dir: Path, db_rows: list[dict]) -> list[dict]:
    """Inspect data/applications/ on disk and merge in DB metadata by app_dir."""
    by_dir: dict[str, dict] = {}
    for row in db_rows:
        if row.get("app_dir"):
            by_dir[row["app_dir"]] = row

    out: list[dict] = []
    if not applications_dir.exists():
        return out
    for child in sorted(applications_dir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        files = {f: (child / f).exists() for f in APP_REQUIRED_FILES}
        company = role = url = None
        match_score = None
        applied_status = None
        try:
            jd_path = child / "jd.json"
            if jd_path.exists():
                jd = json.loads(jd_path.read_text())
                company = jd.get("company")
                role = jd.get("title")
        except (OSError, json.JSONDecodeError):
            pass
        try:
            meta_path = child / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
                url = meta.get("url")
                match_score = meta.get("match_score")
                applied_status = meta.get("status")
        except (OSError, json.JSONDecodeError):
            pass

        db_row = by_dir.get(str(child)) or by_dir.get(child.as_posix()) or {}
        out.append({
            "dir": str(child),
            "company": company,
            "role": role,
            "url": url,
            "match_score": match_score,
            "files": files,
            "meta_status": applied_status,
            "db_status": db_row.get("status"),
            "applied_at": db_row.get("applied_at"),
            "created_at": db_row.get("created_at"),
        })
    return out


def _scan_project_dirs(projects_dir: Path) -> list[dict]:
    """Inspect data/projects/ on disk and report completion state.

    Completion is dual-signal — either ``completed.json`` exists or
    meta.json status equals "completed".
    """
    out: list[dict] = []
    if not projects_dir.exists():
        return out
    for child in sorted(projects_dir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        meta = _safe_load_json(meta_path)
        title = meta.get("title") or child.name
        marker_exists = (child / COMPLETED_MARKER_FILENAME).exists()
        meta_status = meta.get("status") or "unknown"
        effective_status = (
            "completed"
            if marker_exists or meta_status == PROJECT_COMPLETED_STATUS
            else (meta_status if meta_status != "unknown" else "in_progress" if meta else "unknown")
        )
        out.append({
            "dir": str(child),
            "title": title,
            "status": effective_status,
            "meta_status": meta_status,
            "completed_marker": marker_exists,
            "idea_key": meta.get("idea_key"),
            "skill_demonstrated": meta.get("skill_demonstrated"),
            "created_at": meta.get("created_at"),
            "completed_at": meta.get("completed_at"),
            "refined_at": meta.get("refined_at"),
            "regenerated_files": meta.get("regenerated_files") or [],
        })
    return out


# ── Text formatter ───────────────────────────────────────────────────────────


def format_text(status: dict) -> str:
    lines: list[str] = []

    def section(title: str) -> None:
        lines.append("")
        lines.append("═" * 72)
        lines.append(f"  {title}")
        lines.append("═" * 72)

    lines.append(f"Job Agent Status — generated {status['generated_at']}")

    # Jobs
    section("JOBS")
    summary = status["job_summary"]
    lines.append(f"Total: {summary['total']}")
    buckets = summary["by_bucket"]
    lines.append(
        "Score distribution: "
        f"90-100={buckets['90-100']}  70-89={buckets['70-89']}  "
        f"50-69={buckets['50-69']}  <50={buckets['<50']}"
    )
    jobs = status["jobs"]
    if jobs:
        rows = [["Score", "Title", "Company", "Loc", "Status", "Discovered", "URL"]]
        for j in jobs[:50]:
            score = j.get("score")
            score_str = f"{float(score):.0f}" if score is not None else "—"
            rows.append([
                score_str,
                _truncate(j.get("title") or "—", 38),
                _truncate(j.get("company") or "—", 22),
                _truncate(j.get("location") or "—", 18),
                _truncate(j.get("status") or "—", 10),
                _truncate(_short_dt(j.get("discovered_at")), 10),
                _truncate(j.get("url") or "—", 50),
            ])
        lines.append(_format_table(rows))
        if len(jobs) > 50:
            lines.append(f"… and {len(jobs) - 50} more")
    else:
        lines.append("(no jobs in state store)")

    # Applications
    section("APPLICATIONS  (data/applications/)")
    apps = status["applications"]
    if apps:
        rows = [["Company", "Role", "JD", "Resume", "Cover", "Meta", "Status", "Created"]]
        for a in apps:
            files = a["files"]
            rows.append([
                _truncate(a.get("company") or "—", 22),
                _truncate(a.get("role") or "—", 32),
                "✓" if files.get("jd.json") else "✗",
                "✓" if files.get("tailored_resume.json") else "✗",
                "✓" if files.get("cover_letter.md") else "✗",
                "✓" if files.get("meta.json") else "✗",
                _truncate(a.get("db_status") or a.get("meta_status") or "—", 12),
                _truncate(_short_dt(a.get("created_at")), 10),
            ])
        lines.append(_format_table(rows))
    else:
        lines.append("(no application directories on disk)")

    # Project ideas
    section("PROJECT IDEAS")
    pi_summary = status["project_ideas_summary"]
    lines.append(f"Total: {pi_summary['total']}")
    if pi_summary["by_status"]:
        lines.append(
            "By status: "
            + ", ".join(f"{k}={v}" for k, v in sorted(pi_summary["by_status"].items()))
        )
    ideas = status["project_ideas"]
    if ideas:
        rows = [["ID", "Skill", "Title", "Status", "On-disk", "Selected", "Project Dir", "Updated"]]
        for i in ideas:
            rows.append([
                str(i.get("id", "")),
                _truncate(i.get("skill") or (i.get("gap") or {}).get("skill") or "—", 18),
                _truncate(i.get("title") or "—", 38),
                _truncate(i.get("status") or "—", 12),
                _truncate(i.get("completion_state") or "—", 12),
                str(int(i.get("selected_count") or 0)),
                _truncate(i.get("project_dir") or "—", 28),
                _truncate(_short_dt(i.get("updated_at")), 10),
            ])
        lines.append(_format_table(rows))
    else:
        lines.append("(no project ideas stored)")

    # Project directories on disk
    section("PROJECTS ON DISK  (data/projects/)")
    pdirs = status["project_dirs"]
    if pdirs:
        rows = [["Title", "Status", "Marker", "Skill", "Created", "Completed", "Dir"]]
        for p in pdirs:
            rows.append([
                _truncate(p.get("title") or "—", 28),
                _truncate(p.get("status") or "—", 12),
                "✓" if p.get("completed_marker") else "✗",
                _truncate(p.get("skill_demonstrated") or "—", 18),
                _truncate(_short_dt(p.get("created_at")), 10),
                _truncate(_short_dt(p.get("completed_at")), 10),
                _truncate(p.get("dir") or "—", 30),
            ])
        lines.append(_format_table(rows))
    else:
        lines.append("(no project directories on disk)")

    # Run history
    section("RUN HISTORY")
    rh_summary = status["run_history_summary"]
    totals = rh_summary["totals"]
    lines.append(
        f"Recent runs: {totals['runs']}  |  "
        f"jobs scored: {totals['jobs_scored']}  |  "
        f"early exits: {totals['early_exits']}  |  "
        f"Claude failures: {totals['claude_failures']}  |  "
        f"errors: {totals['errors']}"
    )
    if rh_summary["by_command"]:
        lines.append(
            "By command (recent window): "
            + ", ".join(f"{k}={v}" for k, v in sorted(rh_summary["by_command"].items()))
        )
    by_cmd_all_time = status.get("run_history_totals_by_command") or []
    if by_cmd_all_time:
        lines.append("By command (all-time):")
        rows = [["Command", "Runs", "Jobs scored", "Early exits", "Claude failures"]]
        for r in by_cmd_all_time:
            rows.append([
                r.get("command") or "—",
                str(int(r.get("runs") or 0)),
                str(int(r.get("jobs_scored") or 0)),
                str(int(r.get("early_exits") or 0)),
                str(int(r.get("claude_failures") or 0)),
            ])
        lines.append(_format_table(rows))
    runs = status["run_history"]
    if runs:
        rows = [["ID", "Cmd", "Started", "Finished", "Status", "Scored", "Early", "Fails"]]
        for r in runs:
            rows.append([
                str(r.get("id", "")),
                _truncate(r.get("command") or "—", 8),
                _truncate(_short_dt(r.get("started_at"), with_time=True), 16),
                _truncate(_short_dt(r.get("finished_at"), with_time=True), 16),
                _truncate(r.get("status") or "—", 10),
                str(r.get("jobs_scored") or 0),
                "Y" if int(r.get("early_exit_triggered") or 0) else "N",
                str(r.get("claude_failures") or 0),
            ])
        lines.append(_format_table(rows))
    else:
        lines.append("(no runs logged yet)")

    return "\n".join(lines) + "\n"


# ── JSON formatter ───────────────────────────────────────────────────────────


def format_json(status: dict) -> str:
    return json.dumps(status, indent=2, default=str)


# ── HTML formatter ───────────────────────────────────────────────────────────


def format_html(status: dict) -> str:
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<title>Job Agent Status</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:1200px;margin:2rem auto;padding:0 1rem;color:#222;}",
        "h1{margin-bottom:0.2rem;} .ts{color:#888;font-size:0.85em;margin-bottom:2rem;}",
        "h2{border-bottom:2px solid #eee;padding-bottom:0.2rem;margin-top:2rem;}",
        "table{border-collapse:collapse;width:100%;margin:0.5rem 0 1rem;font-size:0.9em;}",
        "th,td{border:1px solid #ddd;padding:0.35rem 0.5rem;text-align:left;"
        "vertical-align:top;}",
        "th{background:#f6f6f6;} tr:nth-child(even) td{background:#fafafa;}",
        ".muted{color:#888;} .ok{color:#16794b;} .bad{color:#b00020;}",
        ".chip{display:inline-block;padding:0.05rem 0.45rem;border-radius:0.5rem;"
        "background:#eef;margin-right:0.3rem;font-size:0.8em;}",
        "</style></head><body>",
        "<h1>Job Agent Status</h1>",
        f'<div class="ts">Generated {html.escape(status["generated_at"])}</div>',
    ]

    # Jobs section
    parts.append("<h2>Jobs</h2>")
    js = status["job_summary"]
    parts.append(f'<p>Total: <b>{js["total"]}</b>. Score buckets: ')
    for k, v in js["by_bucket"].items():
        parts.append(f'<span class="chip">{html.escape(k)}: {v}</span>')
    parts.append("</p>")
    parts.append(_html_table(
        ["Score", "Title", "Company", "Location", "Status", "Discovered", "URL"],
        [
            [
                _fmt_score(j.get("score")),
                j.get("title") or "—",
                j.get("company") or "—",
                j.get("location") or "—",
                j.get("status") or "—",
                _short_dt(j.get("discovered_at")),
                _html_url(j.get("url")),
            ]
            for j in status["jobs"]
        ],
        raw_cols={6},  # URL column carries HTML
    ))

    # Applications
    parts.append("<h2>Applications (data/applications/)</h2>")
    parts.append(_html_table(
        ["Company", "Role", "JD", "Resume", "Cover", "Meta", "Status", "Created", "Dir"],
        [
            [
                a.get("company") or "—",
                a.get("role") or "—",
                "✓" if a["files"].get("jd.json") else "✗",
                "✓" if a["files"].get("tailored_resume.json") else "✗",
                "✓" if a["files"].get("cover_letter.md") else "✗",
                "✓" if a["files"].get("meta.json") else "✗",
                a.get("db_status") or a.get("meta_status") or "—",
                _short_dt(a.get("created_at")),
                a.get("dir") or "—",
            ]
            for a in status["applications"]
        ],
    ))

    # Project ideas
    parts.append("<h2>Project Ideas</h2>")
    pis = status["project_ideas_summary"]
    parts.append(f'<p>Total: <b>{pis["total"]}</b>. ')
    for k, v in sorted(pis["by_status"].items()):
        parts.append(f'<span class="chip">{html.escape(k)}: {v}</span>')
    parts.append("</p>")
    parts.append(_html_table(
        ["ID", "Skill", "Title", "Status", "On-disk", "Selected", "Project Dir", "Updated"],
        [
            [
                str(i.get("id", "")),
                i.get("skill") or (i.get("gap") or {}).get("skill") or "—",
                i.get("title") or "—",
                i.get("status") or "—",
                i.get("completion_state") or "—",
                str(int(i.get("selected_count") or 0)),
                i.get("project_dir") or "—",
                _short_dt(i.get("updated_at")),
            ]
            for i in status["project_ideas"]
        ],
    ))

    # Projects on disk
    parts.append("<h2>Projects on Disk (data/projects/)</h2>")
    parts.append(_html_table(
        ["Title", "Status", "Marker", "Skill", "Created", "Completed", "Refined", "Dir"],
        [
            [
                p.get("title") or "—",
                p.get("status") or "—",
                "✓" if p.get("completed_marker") else "✗",
                p.get("skill_demonstrated") or "—",
                _short_dt(p.get("created_at")),
                _short_dt(p.get("completed_at")),
                _short_dt(p.get("refined_at")),
                p.get("dir") or "—",
            ]
            for p in status["project_dirs"]
        ],
    ))

    # Run history
    parts.append("<h2>Run History</h2>")
    rhs = status["run_history_summary"]
    t = rhs["totals"]
    parts.append(
        f'<p>Runs: <b>{t["runs"]}</b>. '
        f'Jobs scored: <b>{t["jobs_scored"]}</b>. '
        f'Early exits: <b>{t["early_exits"]}</b>. '
        f'Claude failures: <b>{t["claude_failures"]}</b>. '
        f'Errors: <b>{t["errors"]}</b>.</p>'
    )
    parts.append(_html_table(
        ["ID", "Command", "Started", "Finished", "Status", "Scored", "Early", "Fails"],
        [
            [
                str(r.get("id", "")),
                r.get("command") or "—",
                _short_dt(r.get("started_at"), with_time=True),
                _short_dt(r.get("finished_at"), with_time=True),
                r.get("status") or "—",
                str(r.get("jobs_scored") or 0),
                "Y" if int(r.get("early_exit_triggered") or 0) else "N",
                str(r.get("claude_failures") or 0),
            ]
            for r in status["run_history"]
        ],
    ))

    parts.append("</body></html>")
    return "".join(parts)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _truncate(s: str, width: int) -> str:
    s = str(s or "")
    if len(s) <= width:
        return s
    if width <= 1:
        return s[:width]
    return s[: width - 1] + "…"


def _short_dt(value, with_time: bool = False) -> str:
    if not value:
        return "—"
    s = str(value)
    if "T" in s:
        if with_time:
            return s[:16].replace("T", " ")
        return s[:10]
    return s[:16]


def _format_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    widths = [0] * len(rows[0])
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    out_lines: list[str] = []
    for ri, row in enumerate(rows):
        line = "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
        out_lines.append(line.rstrip())
        if ri == 0:
            out_lines.append("  ".join("-" * w for w in widths))
    return "\n".join(out_lines)


def _fmt_score(score) -> str:
    if score is None:
        return "—"
    try:
        return f"{float(score):.0f}"
    except (TypeError, ValueError):
        return "—"


def _html_url(url: str | None) -> str:
    if not url:
        return "—"
    escaped = html.escape(url)
    short = html.escape(url if len(url) <= 60 else url[:57] + "…")
    return f'<a href="{escaped}" target="_blank" rel="noopener">{short}</a>'


def _html_table(headers: list[str], rows: list[list], raw_cols: set[int] | None = None) -> str:
    raw_cols = raw_cols or set()
    if not rows:
        return '<p class="muted">(empty)</p>'
    out = ["<table><thead><tr>"]
    for h in headers:
        out.append(f"<th>{html.escape(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for i, cell in enumerate(row):
            if i in raw_cols:
                out.append(f"<td>{cell}</td>")
            else:
                out.append(f"<td>{html.escape(str(cell))}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")
    return "".join(out)
