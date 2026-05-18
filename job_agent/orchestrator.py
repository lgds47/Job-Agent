"""
Job Search Orchestrator
=======================
Entry point for the agent pipeline. Coordinates all subagents:
  - SearchAgent          → discovers and scores job postings
  - ResumeAgent          → tailors resume bullets to a job description
  - CoverLetterAgent     → generates targeted cover letters
  - ProjectPlannerAgent  → triages skill gaps and drafts project briefs
  - ProjectBuilderAgent  → scaffolds portfolio repos from an approved brief

Run-history accounting
----------------------
Every subcommand captures ``started_at`` at the very top of the handler and
``finished_at`` immediately before persisting to ``run_history``. Both are
passed explicitly to ``StateStore.record_run`` — the state store does not
generate them on its own anymore, so the recorded duration reflects the
real wall-clock time of the run.

Thresholds
----------
- Job targeting threshold: ``50`` (``min_score=50`` in SearchAgent.run and
  the ``get_recent_jobs`` filter used to surface qualified jobs)
- Skill gap analysis threshold: ``35`` (``GAP_MIN_SCORE``) — kept lower so
  gap analysis sees a broader sample of postings than the strict "apply
  now" threshold, surfacing skill gaps that show up in partial matches.
- Search early-exit threshold: ``7`` consecutive scores below ``50``
  (see ``agents/search_agent.py``)

Usage:
  # Discover new jobs and score them
  python orchestrator.py search

  # Generate full application package for a specific job URL
  python orchestrator.py apply --url "https://jobs.example.com/ml-engineer"

  # Run skill gap analysis (and optional project scaffold)
  python orchestrator.py gaps

  # View pipeline outputs and performance summary
  python orchestrator.py status [--format text|json|html] [--output PATH]
  python orchestrator.py doctor [--skip-api]
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

from agents.search_agent import SearchAgent
from agents.resume_agent import ResumeAgent
from agents.cover_letter_agent import CoverLetterAgent
from agents.project_planner_agent import ProjectPlannerAgent
from agents.project_builder_agent import ProjectBuilderAgent
from tools.jd_parser import parse_jd
from tools.job_skills import enrich_jobs_skill_lists
from tools.state_store import StateStore
from tools import status_report
from tools.api_errors import claude_failure_count, failure_metadata
from tools.doctor import run_doctor

RESUME_PATH = Path("data/luke_ganalon_resume.json")
APPLICATIONS_DIR = Path("data/applications")
PROJECTS_DIR = Path("data/projects")
LOG_DIR = Path("logs")

# Skill gap analysis runs over a broader pool of postings than job
# targeting. Kept distinct from the job targeting threshold (50) so partial
# matches still contribute to skill-frequency tallies.
GAP_MIN_SCORE = 35
JOB_TARGET_MIN_SCORE = 50


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


def _now_iso() -> str:
    return datetime.now().isoformat()


def _is_project_complete_dir(project_dir: Path) -> bool:
    """Dual-signal completeness check, mirroring ProjectBuilderAgent."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Status command
# ─────────────────────────────────────────────────────────────────────────────


def run_doctor_cmd(args):
    """Offline-friendly environment checks (optional API probe)."""
    check_api = not getattr(args, "skip_api", False)
    raise SystemExit(run_doctor(resume_path=RESUME_PATH, check_api=check_api))


def run_status(args):
    """Read-only summary of agent output and performance. No API calls."""
    store = StateStore()
    data = status_report.collect_status(store)
    fmt = (getattr(args, "format", None) or "text").lower()
    if fmt == "json":
        rendered = status_report.format_json(data)
    elif fmt == "html":
        rendered = status_report.format_html(data)
    elif fmt == "text":
        rendered = status_report.format_text(data)
    else:
        raise SystemExit(f"Unknown --format: {fmt!r} (expected text | json | html)")

    out_path = getattr(args, "output", None)
    if out_path:
        Path(out_path).write_text(rendered)
        print(f"✅ Status report written to {out_path}")
    else:
        print(rendered)


# ─────────────────────────────────────────────────────────────────────────────
# Search command
# ─────────────────────────────────────────────────────────────────────────────


async def run_search(args):
    """Discover and score new job postings."""
    print("\n=== JOB SEARCH AGENT ===")
    started_at = _now_iso()
    store = StateStore()
    resume = load_resume()
    target_roles = resume["agent_metadata"].get("target_roles", [])

    if not target_roles:
        print("⚠️  No target_roles set in resume JSON agent_metadata. Add them first.")
        print('   Example: ["ML Engineer", "MLOps Engineer", "Senior Data Scientist"]')
        store.record_run(
            command="search",
            status="skipped",
            metadata={"reason": "missing_target_roles"},
            started_at=started_at,
            finished_at=_now_iso(),
        )
        return

    adhoc = args.company if args.company else []
    agent = SearchAgent(resume=resume)
    try:
        results = await agent.run(
            roles=target_roles,
            adhoc_companies=adhoc,
            min_score=JOB_TARGET_MIN_SCORE,
        )
    except Exception as e:
        stats = agent.last_run_stats or {}
        store.record_run(
            command="search",
            status="failed",
            jobs_scored=int(stats.get("jobs_scored") or 0),
            early_exit_triggered=bool(stats.get("early_exit_triggered")),
            claude_failures=int(agent.claude_failures or 0) + claude_failure_count(e),
            metadata=failure_metadata(e, phase="search"),
            started_at=started_at,
            finished_at=_now_iso(),
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

        store.save_jobs(results)
        print(f"\n✅ Saved {len(results)} jobs to state store.")

    stats = agent.last_run_stats or {}
    claude_failures = int(stats.get("claude_failures") or agent.claude_failures or 0)
    if results:
        run_status_label = "success" if claude_failures == 0 else "degraded"
    elif claude_failures > 0 and int(stats.get("jobs_scored") or 0) == 0:
        run_status_label = "failed"
    elif claude_failures > 0:
        run_status_label = "degraded"
    else:
        run_status_label = "empty"

    store.record_run(
        command="search",
        status=run_status_label,
        jobs_scored=int(stats.get("jobs_scored") or 0),
        early_exit_triggered=bool(stats.get("early_exit_triggered")),
        claude_failures=claude_failures,
        metadata={
            **stats,
            "qualified_returned": len(results),
            "adhoc_companies": adhoc,
        },
        started_at=started_at,
        finished_at=_now_iso(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Apply command
# ─────────────────────────────────────────────────────────────────────────────


async def run_apply(args):
    """Generate a full application package for a job URL."""
    print("\n=== APPLICATION PIPELINE ===")
    started_at = _now_iso()
    store = StateStore()
    resume = load_resume()

    # Step 1: Parse the job description
    print(f"📄 Parsing job description from: {args.url}")
    try:
        jd = await parse_jd(args.url)
    except Exception as e:
        meta = failure_metadata(e, phase="parse_jd", url=args.url)
        store.record_run(
            command="apply",
            status="failed",
            claude_failures=claude_failure_count(e),
            metadata=meta,
            started_at=started_at,
            finished_at=_now_iso(),
        )
        err_type = meta.get("error_type", "unknown")
        print(f"❌ Apply failed [{err_type}]: {meta.get('user_message', e)}")
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
        meta = failure_metadata(e, phase="resume_or_cover", url=args.url)
        store.record_run(
            command="apply",
            status="failed",
            claude_failures=claude_failure_count(e),
            metadata=meta,
            started_at=started_at,
            finished_at=_now_iso(),
        )
        err_type = meta.get("error_type", "unknown")
        print(f"❌ Application generation failed [{err_type}]: {meta.get('user_message', e)}")
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
            "created_at": _now_iso(),
            "match_score": tailored_resume.get("match_score"),
            "company": jd.get("company"),
            "role": jd.get("title"),
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
            },
        },
        started_at=started_at,
        finished_at=_now_iso(),
    )

    print(f"\n✅ Application package saved to: {app_dir}")
    print(f"   - jd.json              (parsed job description)")
    print(f"   - tailored_resume.json (reordered + highlighted bullets)")
    print(f"   - cover_letter.md      (ready to copy-paste)")
    print(f"   - meta.json            (status tracker)")
    print("   - SQLite applications row updated (see tools.state_store.StateStore)")


# ─────────────────────────────────────────────────────────────────────────────
# Gaps command
# ─────────────────────────────────────────────────────────────────────────────


async def run_gaps(args):
    """Analyze recent job postings for skill gaps, plan projects, optionally build."""
    print("\n=== PROJECT PLANNER AGENT ===")
    started_at = _now_iso()
    resume = load_resume()
    store = StateStore()

    # Skill gap analysis uses GAP_MIN_SCORE (35), intentionally lower than
    # JOB_TARGET_MIN_SCORE (50). Gaps surface in partial-match postings as
    # well as strong matches, so a broader pool gives a more accurate
    # skill-frequency picture without expanding the apply backlog.
    recent_jobs = store.get_recent_jobs(n=50, min_score=GAP_MIN_SCORE)

    if not recent_jobs:
        print("⚠️  No jobs in state store yet. Run `python orchestrator.py search` first.")
        store.record_run(
            command="gaps",
            status="empty",
            metadata={"reason": "no_recent_jobs"},
            started_at=started_at,
            finished_at=_now_iso(),
        )
        return

    print("🧠 Ensuring postings have skill lists (backfills older SQLite rows)...")
    recent_jobs = await enrich_jobs_skill_lists(recent_jobs, persist_store=store)

    # Step 1: Raw gap extraction
    from collections import Counter
    skill_counter: Counter = Counter()
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
            metadata={
                "reason": "no_significant_gaps",
                "gap_min_score": GAP_MIN_SCORE,
            },
            started_at=started_at,
            finished_at=_now_iso(),
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
            claude_failures=planner.claude_failures + claude_failure_count(e),
            metadata=failure_metadata(e, phase="analyze"),
            started_at=started_at,
            finished_at=_now_iso(),
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
            metadata={"project_gaps": 0, "analyzed_gaps": len(analyzed_gaps)},
            started_at=started_at,
            finished_at=_now_iso(),
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
            claude_failures=planner.claude_failures + claude_failure_count(e),
            metadata=failure_metadata(e, phase="generate_options"),
            started_at=started_at,
            finished_at=_now_iso(),
        )
        return

    if not options:
        print("⚠️  Planner returned no project options — nothing to build.")
        store.record_run(
            command="gaps",
            status="empty",
            claude_failures=planner.claude_failures,
            metadata={"reason": "no_options"},
            started_at=started_at,
            finished_at=_now_iso(),
        )
        return

    selected_idea = None
    selected_option = options[0]
    selected_gap = top_gap

    if args.build:
        # Finish-first override: if an incomplete project exists, keep
        # refining it (regardless of which idea is at the top of the queue).
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
                claude_failures=planner.claude_failures + claude_failure_count(e),
                metadata=failure_metadata(e, phase="build_brief"),
                started_at=started_at,
                finished_at=_now_iso(),
            )
            return
        builder = ProjectBuilderAgent(resume=resume)
        idea_key_in = (selected_idea or {}).get("idea_key")
        project_dir = await builder.build(
            brief=brief,
            output_dir=PROJECTS_DIR,
            idea_key=idea_key_in,
        )
        # The builder records what it actually did. If it refined an
        # existing project the active idea_key may come from that project's
        # meta.json instead of the one we passed in.
        active_idea_key = builder.last_build_info.get("idea_key") or idea_key_in
        if active_idea_key:
            new_status = "completed" if builder.last_action == "scaffolded" else "in_progress"
            store.update_project_idea_status(
                idea_key=active_idea_key,
                status=new_status,
                project_dir=str(project_dir),
            )
        store.record_run(
            command="gaps",
            status="success",
            claude_failures=planner.claude_failures,
            metadata={
                "analyzed_gaps": len(analyzed_gaps),
                "project_gaps": len(project_gaps),
                "selected_idea_key": active_idea_key,
                "builder_mode": builder.last_build_info.get("mode"),
                "builder_action": builder.last_action,
                "project_dir": str(project_dir),
                "build": True,
                "gap_min_score": GAP_MIN_SCORE,
            },
            started_at=started_at,
            finished_at=_now_iso(),
        )
    else:
        # No --build: surface the next queued idea WITHOUT mutating it.
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
                "gap_min_score": GAP_MIN_SCORE,
            },
            started_at=started_at,
            finished_at=_now_iso(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Job Search Orchestrator")
    subparsers = parser.add_subparsers(dest="command")

    search_p = subparsers.add_parser("search", help="Discover and score job postings")
    search_p.add_argument(
        "--company", nargs="+", metavar="NAME",
        help='Ad-hoc companies to add (e.g. --company "Glean" "Cohere")'
    )

    apply_p = subparsers.add_parser("apply", help="Generate application package for a job URL")
    apply_p.add_argument("--url", required=True, help="Job posting URL")

    gaps_p = subparsers.add_parser("gaps", help="Analyze skill gaps and plan portfolio projects")
    gaps_p.add_argument(
        "--build", action="store_true",
        help="Pass one stored project idea to the builder (prefers ideas not yet started)."
    )

    status_p = subparsers.add_parser(
        "status",
        help="Read-only dashboard of jobs, applications, project ideas, and run history."
    )
    status_p.add_argument(
        "--format", choices=["text", "json", "html"], default="text",
        help="Output format (default: text)."
    )
    status_p.add_argument(
        "--output", "-o", metavar="PATH",
        help="Write the report to a file instead of stdout."
    )

    doctor_p = subparsers.add_parser(
        "doctor",
        help="Check Python, resume schema, API key, and optional API connectivity.",
    )
    doctor_p.add_argument(
        "--skip-api",
        action="store_true",
        help="Do not call Anthropic (only local checks).",
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
    elif args.command == "doctor":
        run_doctor_cmd(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
