"""
Job Search Orchestrator
=======================
Entry point for the agent pipeline. Coordinates all subagents:
  - SearchAgent          → discovers and scores job postings
  - ResumeAgent          → tailors resume bullets to a job description
  - CoverLetterAgent     → generates targeted cover letters
  - ProjectPlannerAgent  → triages skill gaps and drafts project briefs
  - ProjectBuilderAgent  → scaffolds portfolio repos from an approved brief

Usage:
  # Discover new jobs and score them
  python orchestrator.py search

  # Generate full application package for a specific job URL
  python orchestrator.py apply --url "https://jobs.example.com/ml-engineer"

  # Run skill gap analysis (and optional project scaffold)
  python orchestrator.py gaps

  # View pipeline outputs and performance summary
  python orchestrator.py status [--format text|json|html]
"""

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import argparse
import json
import asyncio
from pathlib import Path
from datetime import datetime
from html import escape

from agents.search_agent import SearchAgent
from agents.resume_agent import ResumeAgent
from agents.cover_letter_agent import CoverLetterAgent
from agents.project_planner_agent import ProjectPlannerAgent
from agents.project_builder_agent import ProjectBuilderAgent
from tools.jd_parser import parse_jd
from tools.job_skills import enrich_jobs_skill_lists
from tools.state_store import StateStore

RESUME_PATH = Path("data/luke_ganalon_resume.json")
APPLICATIONS_DIR = Path("data/applications")
PROJECTS_DIR = Path("data/projects")
LOG_DIR = Path("logs")


def load_resume() -> dict:
    with open(RESUME_PATH) as f:
        return json.load(f)


def _safe_load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _truncate(text: str, width: int) -> str:
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= width else f"{text[:max(0, width - 1)]}…"


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(str(cell)))
    header_line = " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * widths[i] for i in range(len(headers)))
    body_lines = [
        " | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *body_lines])


def _project_completion_state(project_dir: str | None) -> str:
    if not project_dir:
        return "not_started"
    path = Path(project_dir)
    if not path.exists():
        return "missing_dir"
    completed_path = path / "completed.json"
    if completed_path.exists():
        return "completed"
    meta = _safe_load_json(path / "meta.json")
    if str(meta.get("status", "")).lower() == "completed":
        return "completed"
    return "in_progress"


def _is_project_complete_dir(project_dir: Path) -> bool:
    if (project_dir / "completed.json").exists():
        return True
    meta = _safe_load_json(project_dir / "meta.json")
    return str(meta.get("status", "")).lower() == "completed"


def _most_recent_incomplete_project(projects_dir: Path) -> Path | None:
    if not projects_dir.exists():
        return None
    dirs = [p for p in projects_dir.iterdir() if p.is_dir()]
    incomplete = [p for p in dirs if not _is_project_complete_dir(p)]
    if not incomplete:
        return None
    return sorted(incomplete, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def _collect_application_outputs(store: StateStore) -> list[dict]:
    db_apps = {str(app.get("app_dir")): app for app in store.get_applications()}
    outputs = []
    if not APPLICATIONS_DIR.exists():
        return outputs

    dirs = sorted([p for p in APPLICATIONS_DIR.iterdir() if p.is_dir()], reverse=True)
    for app_dir in dirs:
        meta = _safe_load_json(app_dir / "meta.json")
        jd = _safe_load_json(app_dir / "jd.json")
        db_row = db_apps.get(str(app_dir))
        outputs.append({
            "app_dir": str(app_dir),
            "company": jd.get("company") or meta.get("company") or "unknown",
            "role": jd.get("title") or meta.get("role") or "unknown",
            "resume_exists": (app_dir / "tailored_resume.json").exists(),
            "cover_letter_exists": (app_dir / "cover_letter.md").exists(),
            "jd_exists": (app_dir / "jd.json").exists(),
            "status": (db_row or {}).get("status") or meta.get("status") or "unknown",
            "created_at": (db_row or {}).get("created_at") or meta.get("created_at"),
            "url": (db_row or {}).get("job_url") or meta.get("url"),
        })
    return outputs


def collect_status_snapshot() -> dict:
    store = StateStore()
    jobs = store.get_all_jobs()
    applications = _collect_application_outputs(store)
    ideas = store.get_project_ideas(include_completed=True)
    run_history = store.get_run_history(limit=200)
    run_summary = store.get_run_history_summary()

    for idea in ideas:
        idea["completion_state"] = _project_completion_state(idea.get("project_dir"))

    return {
        "generated_at": datetime.now().isoformat(),
        "jobs": jobs,
        "applications": applications,
        "project_ideas": ideas,
        "run_history": run_history,
        "run_history_summary": run_summary,
    }


def render_status_text(snapshot: dict) -> str:
    sections = [f"\n=== JOB AGENT STATUS ({snapshot['generated_at']}) ==="]

    jobs_rows = []
    for job in snapshot["jobs"]:
        metadata = job.get("metadata") or {}
        jobs_rows.append([
            _truncate(job.get("discovered_at", ""), 19),
            f"{float(job.get('score') or 0):.0f}",
            _truncate(job.get("company", ""), 20),
            _truncate(job.get("title", ""), 34),
            _truncate(job.get("status", ""), 10),
            _truncate(metadata.get("source", ""), 10),
            str(len(metadata.get("required_skills", []) or [])),
            _truncate(job.get("url", ""), 42),
        ])
    sections.append("\n[Jobs]")
    sections.append(_format_table(
        ["discovered_at", "score", "company", "title", "status", "source", "req#", "url"],
        jobs_rows
    ))

    app_rows = []
    for app in snapshot["applications"]:
        app_rows.append([
            _truncate(app.get("created_at", ""), 19),
            _truncate(app.get("company", ""), 20),
            _truncate(app.get("role", ""), 32),
            "Y" if app.get("resume_exists") else "N",
            "Y" if app.get("cover_letter_exists") else "N",
            "Y" if app.get("jd_exists") else "N",
            _truncate(app.get("status", ""), 12),
            _truncate(app.get("app_dir", ""), 42),
        ])
    sections.append("\n[Application Artifacts]")
    sections.append(_format_table(
        ["created_at", "company", "role", "resume", "cover", "jd", "status", "dir"],
        app_rows
    ))

    idea_rows = []
    for idea in snapshot["project_ideas"]:
        idea_rows.append([
            idea.get("idea_key", ""),
            _truncate((idea.get("gap") or {}).get("skill", idea.get("skill", "")), 20),
            _truncate(idea.get("title", ""), 34),
            _truncate(idea.get("status", ""), 12),
            _truncate(idea.get("completion_state", ""), 12),
            str(int(idea.get("selected_count") or 0)),
            _truncate(idea.get("project_dir", ""), 42),
        ])
    sections.append("\n[Project Ideas]")
    sections.append(_format_table(
        ["idea_key", "skill", "title", "state", "completion", "selected", "project_dir"],
        idea_rows
    ))

    run_rows = []
    for row in snapshot["run_history"]:
        run_rows.append([
            _truncate(row.get("finished_at", ""), 19),
            _truncate(row.get("command", ""), 8),
            _truncate(row.get("status", ""), 10),
            str(int(row.get("jobs_scored") or 0)),
            str(int(row.get("early_exit_triggered") or 0)),
            str(int(row.get("claude_failures") or 0)),
        ])
    sections.append("\n[Run History — Recent]")
    sections.append(_format_table(
        ["finished_at", "command", "status", "jobs_scored", "early_exits", "claude_failures"],
        run_rows
    ))

    summary_rows = []
    for row in snapshot["run_history_summary"]:
        summary_rows.append([
            row.get("command", ""),
            str(int(row.get("runs") or 0)),
            str(int(row.get("jobs_scored") or 0)),
            str(int(row.get("early_exits") or 0)),
            str(int(row.get("claude_failures") or 0)),
        ])
    sections.append("\n[Run History — Summary]")
    sections.append(_format_table(
        ["command", "runs", "jobs_scored", "early_exits", "claude_failures"],
        summary_rows
    ))

    return "\n".join(sections) + "\n"


def _render_html_table(headers: list[str], rows: list[list[str]]) -> str:
    header_html = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    row_html = ""
    for row in rows:
        row_html += "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>"
    return f"<table border='1' cellspacing='0' cellpadding='4'><thead><tr>{header_html}</tr></thead><tbody>{row_html}</tbody></table>"


def render_status_html(snapshot: dict) -> str:
    jobs_rows = [
        [
            job.get("discovered_at", ""),
            f"{float(job.get('score') or 0):.0f}",
            job.get("company", ""),
            job.get("title", ""),
            job.get("status", ""),
            (job.get("metadata") or {}).get("source", ""),
            len((job.get("metadata") or {}).get("required_skills", []) or []),
            job.get("url", ""),
        ]
        for job in snapshot["jobs"]
    ]
    app_rows = [
        [
            app.get("created_at", ""),
            app.get("company", ""),
            app.get("role", ""),
            app.get("resume_exists"),
            app.get("cover_letter_exists"),
            app.get("jd_exists"),
            app.get("status", ""),
            app.get("app_dir", ""),
        ]
        for app in snapshot["applications"]
    ]
    idea_rows = [
        [
            idea.get("idea_key", ""),
            (idea.get("gap") or {}).get("skill", idea.get("skill", "")),
            idea.get("title", ""),
            idea.get("status", ""),
            idea.get("completion_state", ""),
            idea.get("selected_count", 0),
            idea.get("project_dir", ""),
        ]
        for idea in snapshot["project_ideas"]
    ]
    run_rows = [
        [
            row.get("finished_at", ""),
            row.get("command", ""),
            row.get("status", ""),
            row.get("jobs_scored", 0),
            row.get("early_exit_triggered", 0),
            row.get("claude_failures", 0),
        ]
        for row in snapshot["run_history"]
    ]

    return (
        "<html><body>"
        f"<h1>Job Agent Status</h1><p>Generated at: {escape(snapshot['generated_at'])}</p>"
        f"<h2>Jobs</h2>{_render_html_table(['discovered_at', 'score', 'company', 'title', 'status', 'source', 'required_skills', 'url'], jobs_rows)}"
        f"<h2>Application Artifacts</h2>{_render_html_table(['created_at', 'company', 'role', 'resume', 'cover_letter', 'jd', 'status', 'dir'], app_rows)}"
        f"<h2>Project Ideas</h2>{_render_html_table(['idea_key', 'skill', 'title', 'state', 'completion', 'selected', 'project_dir'], idea_rows)}"
        f"<h2>Run History</h2>{_render_html_table(['finished_at', 'command', 'status', 'jobs_scored', 'early_exits', 'claude_failures'], run_rows)}"
        "</body></html>"
    )


def run_status(args):
    snapshot = collect_status_snapshot()
    if args.format == "json":
        print(json.dumps(snapshot, indent=2))
        return
    if args.format == "html":
        print(render_status_html(snapshot))
        return
    print(render_status_text(snapshot))


async def run_search(args):
    """Discover and score new job postings."""
    print("\n=== JOB SEARCH AGENT ===")
    store = StateStore()
    resume = load_resume()
    target_roles = resume["agent_metadata"].get("target_roles", [])

    if not target_roles:
        print("⚠️  No target_roles set in resume JSON agent_metadata. Add them first.")
        print('   Example: ["ML Engineer", "MLOps Engineer", "Senior Data Scientist"]')
        store.record_run(
            command="search",
            status="skipped",
            metadata={"reason": "missing_target_roles"}
        )
        return

    adhoc = args.company if args.company else []
    agent = SearchAgent(resume=resume)
    try:
        results = await agent.run(roles=target_roles, adhoc_companies=adhoc)
    except Exception as e:
        stats = agent.last_run_stats or {}
        store.record_run(
            command="search",
            status="failed",
            jobs_scored=int(stats.get("jobs_scored") or 0),
            early_exit_triggered=bool(stats.get("early_exit_triggered")),
            claude_failures=int(agent.claude_failures or 0),
            metadata={"error": str(e)}
        )
        print(f"\n❌ Search run failed: {e}")
        return

    if not results:
        print("\nNo qualifying postings met the score threshold.")
    else:
        print(f"\nFound {len(results)} postings. Top matches:\n")
        for i, job in enumerate(results[:10], 1):
            score = float(job.get("score") or 0)
            title = job.get("title") or "Unknown title"
            company = job.get("company") or "Unknown company"
            url = job.get("url") or ""
            print(f"  {i:2}. [{score:.0f}%] {title} @ {company}")
            print(f"       {url}")

        # Persist results to state store (single persistence path)
        store.save_jobs(results)
        print(f"\n✅ Saved {len(results)} jobs to state store.")

    stats = agent.last_run_stats or {}
    claude_failures = int(stats.get("claude_failures") or agent.claude_failures or 0)
    if results:
        run_status = "success" if claude_failures == 0 else "degraded"
    elif claude_failures > 0 and int(stats.get("jobs_scored") or 0) == 0:
        run_status = "failed"
    elif claude_failures > 0:
        run_status = "degraded"
    else:
        run_status = "empty"

    store.record_run(
        command="search",
        status=run_status,
        jobs_scored=int(stats.get("jobs_scored") or 0),
        early_exit_triggered=bool(stats.get("early_exit_triggered")),
        claude_failures=claude_failures,
        metadata={
            **stats,
            "qualified_returned": len(results),
            "adhoc_companies": adhoc,
        }
    )


async def run_apply(args):
    """Generate a full application package for a job URL."""
    print("\n=== APPLICATION PIPELINE ===")
    store = StateStore()
    resume = load_resume()

    # Step 1: Parse the job description
    print(f"📄 Parsing job description from: {args.url}")
    try:
        jd = await parse_jd(args.url)
    except Exception as e:
        store.record_run(
            command="apply",
            status="failed",
            claude_failures=1,
            metadata={"error": f"jd_parse_failed: {e}", "url": args.url}
        )
        print(f"❌ Failed to parse JD: {e}")
        return
    print(f"   Role: {jd['title']} @ {jd['company']}")

    # Step 2: Tailor resume in parallel with cover letter generation
    print("\n⚙️  Running Resume + Cover Letter agents in parallel...")
    resume_agent = ResumeAgent(resume=resume)
    cover_agent = CoverLetterAgent(resume=resume)

    try:
        tailored_resume, cover_letter = await asyncio.gather(
            resume_agent.run(jd=jd),
            cover_agent.run(jd=jd)
        )
    except Exception as e:
        store.record_run(
            command="apply",
            status="failed",
            claude_failures=1,
            metadata={"error": f"resume_or_cover_generation_failed: {e}", "url": args.url}
        )
        print(f"❌ Application generation failed: {e}")
        return

    # Step 3: Save outputs to applications directory
    slug = f"{jd['company'].lower().replace(' ', '_')}_{jd['title'].lower().replace(' ', '_')}"
    timestamp = datetime.now().strftime("%Y%m%d")
    app_dir = APPLICATIONS_DIR / f"{timestamp}_{slug}"
    app_dir.mkdir(parents=True, exist_ok=True)

    with open(app_dir / "jd.json", "w") as f:
        json.dump(jd, f, indent=2)

    with open(app_dir / "tailored_resume.json", "w") as f:
        json.dump(tailored_resume, f, indent=2)

    with open(app_dir / "cover_letter.md", "w") as f:
        f.write(cover_letter)

    with open(app_dir / "meta.json", "w") as f:
        json.dump({
            "url": args.url,
            "applied_at": None,
            "status": "draft",
            "created_at": datetime.now().isoformat(),
            "match_score": tailored_resume.get("match_score")
        }, f, indent=2)

    store.save_application(args.url, str(app_dir))
    store.record_run(
        command="apply",
        status="success",
        metadata={
            "url": args.url,
            "app_dir": str(app_dir),
            "company": jd.get("company"),
            "role": jd.get("title"),
            "files": {
                "jd_json": True,
                "tailored_resume_json": True,
                "cover_letter_md": True,
                "meta_json": True,
            }
        }
    )

    print(f"\n✅ Application package saved to: {app_dir}")
    print(f"   - jd.json              (parsed job description)")
    print(f"   - tailored_resume.json (reordered + highlighted bullets)")
    print(f"   - cover_letter.md      (ready to copy-paste)")
    print(f"   - meta.json            (status tracker)")
    print("   - SQLite applications row updated (see tools.state_store.StateStore)")


async def run_gaps(args):
    """Analyze recent job postings for skill gaps, plan projects, optionally build."""
    print("\n=== PROJECT PLANNER AGENT ===")
    resume = load_resume()
    store = StateStore()
    recent_jobs = store.get_recent_jobs(n=50, min_score=50)

    if not recent_jobs:
        print("⚠️  No jobs in state store yet. Run `python orchestrator.py search` first.")
        store.record_run(
            command="gaps",
            status="empty",
            metadata={"reason": "no_recent_jobs"}
        )
        return

    print("🧠 Ensuring postings have skill lists (backfills older SQLite rows)...")
    recent_jobs = await enrich_jobs_skill_lists(recent_jobs, persist_store=store)

    # Step 1: Raw gap extraction
    from collections import Counter
    skill_counter = Counter()
    for job in recent_jobs:
        for skill in job.get("required_skills", []):
            skill_counter[skill.lower()] += 1
        for skill in job.get("preferred_skills", []):
            skill_counter[skill.lower()] += 0.5

    candidate_skills = set(
        item.split("(")[0].strip().lower()
        for cat in resume["skills"].values()
        for item in cat["items"]
    )
    raw_gaps = [
        {"skill": skill, "frequency": round(freq), "gap_level": "missing"}
        for skill, freq in skill_counter.most_common(30)
        if not any(skill in cs or cs in skill for cs in candidate_skills)
        and round(freq) >= 2
    ]

    if not raw_gaps:
        print("✅ No significant skill gaps detected in recent postings.")
        store.record_run(
            command="gaps",
            status="success",
            metadata={"reason": "no_significant_gaps"}
        )
        return

    # Step 2: Planner analyzes and triages
    planner = ProjectPlannerAgent(resume=resume, store=store)
    try:
        analyzed_gaps = await planner.analyze(gaps=raw_gaps)
    except RuntimeError as e:
        print(f"❌ Planner analyze step failed: {e}")
        store.record_run(
            command="gaps",
            status="failed",
            claude_failures=max(1, planner.claude_failures),
            metadata={"error": str(e), "phase": "analyze"}
        )
        return

    freq_by_skill = {(g["skill"] or "").lower(): int(g.get("frequency") or 0) for g in raw_gaps}
    for g in analyzed_gaps:
        key = (g.get("skill") or "").lower()
        raw_f = freq_by_skill.get(key, 0)
        try:
            model_f = int(g.get("frequency") or 0)
        except (TypeError, ValueError):
            model_f = 0
        g["frequency"] = max(model_f, raw_f)

    store.save_skill_gaps([
        {
            "skill": g.get("skill"),
            "frequency": int(g.get("frequency") or 0),
            "project_idea": " | ".join(
                x for x in [g.get("recommended_action"), g.get("action_rationale")] if x
            ) or None,
        }
        for g in analyzed_gaps
    ])

    project_gaps = [g for g in analyzed_gaps if g.get("project_worthy")]

    if not project_gaps:
        print("ℹ️  Planner recommends no portfolio projects for current gaps.")
        print("   Consider certifications or contributions instead.")
        store.record_run(
            command="gaps",
            status="success",
            claude_failures=planner.claude_failures,
            metadata={"project_gaps": 0, "analyzed_gaps": len(analyzed_gaps)}
        )
        return

    # Step 3: Generate options for the top gap (or iterate manually)
    top_gap = project_gaps[0]
    try:
        options = await planner.generate_options(gap=top_gap)
    except RuntimeError as e:
        print(f"❌ Planner options step failed: {e}")
        store.record_run(
            command="gaps",
            status="failed",
            claude_failures=max(1, planner.claude_failures),
            metadata={"error": str(e), "phase": "generate_options"}
        )
        return

    if not options:
        print("⚠️  Planner returned no project options — nothing to build.")
        store.record_run(
            command="gaps",
            status="empty",
            claude_failures=planner.claude_failures,
            metadata={"reason": "no_options"}
        )
        return

    selected_idea = None
    selected_option = options[0]
    selected_gap = top_gap

    if args.build:
        # Finish-first override: if an incomplete project exists, keep refining it.
        existing_incomplete = _most_recent_incomplete_project(PROJECTS_DIR)
        if existing_incomplete:
            meta = _safe_load_json(existing_incomplete / "meta.json")
            existing_key = meta.get("idea_key")
            if existing_key:
                for idea in store.get_project_ideas(include_completed=True):
                    if idea.get("idea_key") == existing_key:
                        selected_idea = idea
                        break
            if selected_idea is None:
                selected_idea = {
                    "idea_key": existing_key,
                    "title": meta.get("title") or existing_incomplete.name,
                    "status": "in_progress",
                    "option": selected_option,
                    "gap": selected_gap,
                }
            selected_option = selected_idea.get("option") or selected_option
            selected_gap = selected_idea.get("gap") or selected_gap
        else:
            selected_idea = planner.pick_idea_for_builder()
            if selected_idea:
                selected_option = selected_idea.get("option") or selected_option
                selected_gap = selected_idea.get("gap") or selected_gap

        if selected_idea:
            print("\n  🎯 Builder handoff (one idea only):")
            print(f"     idea_key: {selected_idea.get('idea_key')}")
            print(f"     title: {selected_idea.get('title')}")
            print(f"     status: {selected_idea.get('status')}")

        print("\n  🏗️  --build flag set, proceeding with Option 1...")
        try:
            brief = await planner.build_brief(option=selected_option, gap=selected_gap)
        except RuntimeError as e:
            print(f"❌ Planner brief step failed: {e}")
            store.record_run(
                command="gaps",
                status="failed",
                claude_failures=max(1, planner.claude_failures),
                metadata={"error": str(e), "phase": "build_brief"}
            )
            return
        builder = ProjectBuilderAgent(resume=resume)
        project_dir = await builder.build(
            brief=brief,
            output_dir=PROJECTS_DIR,
            idea_key=(selected_idea or {}).get("idea_key")
        )
        active_idea_key = builder.last_build_info.get("idea_key")
        if active_idea_key:
            store.update_project_idea_status(
                idea_key=active_idea_key,
                status="in_progress",
                project_dir=str(project_dir)
            )
        store.record_run(
            command="gaps",
            status="success",
            claude_failures=planner.claude_failures,
            metadata={
                "analyzed_gaps": len(analyzed_gaps),
                "project_gaps": len(project_gaps),
                "selected_idea_key": active_idea_key or (selected_idea or {}).get("idea_key"),
                "builder_mode": builder.last_build_info.get("mode"),
                "project_dir": str(project_dir),
                "build": True,
            }
        )
    else:
        selected_idea = planner.peek_idea_for_builder()
        if selected_idea:
            print("\n  🎯 Next builder handoff preview (no queue mutation):")
            print(f"     idea_key: {selected_idea.get('idea_key')}")
            print(f"     title: {selected_idea.get('title')}")
            print(f"     status: {selected_idea.get('status')}")
        print("  💡 Run with --build to auto-scaffold Option 1,")
        print("     or call planner/builder manually for full control.\n")
        store.record_run(
            command="gaps",
            status="success",
            claude_failures=planner.claude_failures,
            metadata={
                "analyzed_gaps": len(analyzed_gaps),
                "project_gaps": len(project_gaps),
                "selected_idea_key": (selected_idea or {}).get("idea_key"),
                "build": False,
            }
        )


def main():
    parser = argparse.ArgumentParser(description="Job Search Orchestrator")
    subparsers = parser.add_subparsers(dest="command")

    # search command
    search_p = subparsers.add_parser("search", help="Discover and score job postings")
    search_p.add_argument(
        "--company", nargs="+", metavar="NAME",
        help='Ad-hoc companies to add (e.g. --company "Glean" "Cohere")'
    )

    # apply command
    apply_p = subparsers.add_parser("apply", help="Generate application package for a job URL")
    apply_p.add_argument("--url", required=True, help="Job posting URL")

    # gaps command
    gaps_p = subparsers.add_parser("gaps", help="Analyze skill gaps and plan portfolio projects")
    gaps_p.add_argument(
        "--build", action="store_true",
        help="Auto-scaffold the top recommended project (Option 1) without manual review"
    )

    # status command
    status_p = subparsers.add_parser(
        "status",
        help="Show output/performance dashboard for jobs, applications, ideas, and run history"
    )
    status_p.add_argument(
        "--format",
        choices=["text", "json", "html"],
        default="text",
        help="Output format (default: text)"
    )

    args = parser.parse_args()

    if args.command == "search":
        asyncio.run(run_search(args))
    elif args.command == "apply":
        asyncio.run(run_apply(args))
    elif args.command == "gaps":
        asyncio.run(run_gaps(args))
    elif args.command == "status":
        run_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
