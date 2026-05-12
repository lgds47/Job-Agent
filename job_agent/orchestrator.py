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

  # Read-only dashboard of jobs, applications, project ideas, and run history
  python orchestrator.py status [--format text|json|html] [--output PATH]
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

RESUME_PATH = Path("data/luke_ganalon_resume.json")
APPLICATIONS_DIR = Path("data/applications")
LOG_DIR = Path("logs")


def load_resume() -> dict:
    with open(RESUME_PATH) as f:
        return json.load(f)


async def run_search(args):
    """Discover and score new job postings."""
    print("\n=== JOB SEARCH AGENT ===")
    resume = load_resume()
    target_roles = resume["agent_metadata"].get("target_roles", [])

    store = StateStore()
    run_id = store.start_run("search")
    run_status = "ok"
    notes: str | None = None

    if not target_roles:
        print("⚠️  No target_roles set in resume JSON agent_metadata. Add them first.")
        print('   Example: ["ML Engineer", "MLOps Engineer", "Senior Data Scientist"]')
        store.end_run(run_id, status="skipped", notes="No target_roles in resume.")
        return

    adhoc = args.company if args.company else []
    agent = SearchAgent(resume=resume)

    try:
        results = await agent.run(roles=target_roles, adhoc_companies=adhoc)
    except Exception as e:
        store.end_run(
            run_id,
            status="error",
            jobs_scored=agent.stats.get("jobs_scored", 0),
            early_exits=agent.stats.get("early_exits", 0),
            claude_failures=agent.stats.get("claude_failures", 0),
            notes=f"{type(e).__name__}: {e}",
        )
        raise

    if not results:
        print("\nNo qualifying postings met the score threshold.")
        notes = "No qualifying postings."
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

    if agent.stats.get("early_exits", 0):
        run_status = "early_exit"

    store.end_run(
        run_id,
        status=run_status,
        jobs_scored=agent.stats.get("jobs_scored", 0),
        early_exits=agent.stats.get("early_exits", 0),
        claude_failures=agent.stats.get("claude_failures", 0),
        notes=notes,
    )


async def run_apply(args):
    """Generate a full application package for a job URL."""
    print("\n=== APPLICATION PIPELINE ===")
    resume = load_resume()
    store = StateStore()
    run_id = store.start_run("apply")

    try:
        print(f"📄 Parsing job description from: {args.url}")
        jd = await parse_jd(args.url)
        print(f"   Role: {jd['title']} @ {jd['company']}")

        print("\n⚙️  Running Resume + Cover Letter agents in parallel...")
        resume_agent = ResumeAgent(resume=resume)
        cover_agent = CoverLetterAgent(resume=resume)

        tailored_resume, cover_letter = await asyncio.gather(
            resume_agent.run(jd=jd),
            cover_agent.run(jd=jd)
        )
    except Exception as e:
        store.end_run(
            run_id,
            status="error",
            notes=f"{type(e).__name__}: {e}",
        )
        raise

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

    print(f"\n✅ Application package saved to: {app_dir}")
    print(f"   - jd.json              (parsed job description)")
    print(f"   - tailored_resume.json (reordered + highlighted bullets)")
    print(f"   - cover_letter.md      (ready to copy-paste)")
    print(f"   - meta.json            (status tracker)")
    print("   - SQLite applications row updated (see tools.state_store.StateStore)")

    store.end_run(
        run_id,
        status="ok",
        notes=f"{jd['company']} — {jd['title']}",
    )


async def run_gaps(args):
    """Analyze recent job postings for skill gaps, plan projects, optionally build."""
    print("\n=== PROJECT PLANNER AGENT ===")
    resume = load_resume()
    store = StateStore()
    run_id = store.start_run("gaps")
    recent_jobs = store.get_recent_jobs(n=50, min_score=50)

    if not recent_jobs:
        print("⚠️  No jobs in state store yet. Run `python orchestrator.py search` first.")
        store.end_run(run_id, status="skipped", notes="No jobs in state store.")
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
        store.end_run(run_id, status="ok", notes="No significant skill gaps detected.")
        return

    planner = ProjectPlannerAgent(resume=resume)
    try:
        analyzed_gaps = await planner.analyze(gaps=raw_gaps)
    except RuntimeError as e:
        print(f"❌ Planner analyze step failed: {e}")
        store.end_run(run_id, status="error", claude_failures=1,
                      notes=f"analyze: {e}")
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
        store.end_run(run_id, status="ok",
                      notes="No project-worthy gaps detected.")
        return

    # Step 3: Generate options for the top gap and store ALL of them as ideas.
    # Even though the planner produces multiple candidates, we only ever pass
    # one to the builder per run — leftover options stay queued for future runs.
    top_gap = project_gaps[0]
    try:
        options = await planner.generate_options(gap=top_gap)
    except RuntimeError as e:
        print(f"❌ Planner options step failed: {e}")
        store.end_run(run_id, status="error", claude_failures=1,
                      notes=f"generate_options: {e}")
        return

    if not options:
        print("⚠️  Planner returned no project options — nothing to build.")
        store.end_run(run_id, status="ok",
                      notes="Planner returned no project options.")
        return

    new_ideas = store.save_project_ideas(top_gap, options)
    total_pending = len(store.get_project_ideas(status="pending"))
    print(
        f"\n  💾 Stored {new_ideas} new project idea(s) "
        f"({total_pending} pending total)."
    )

    if not args.build:
        print("  💡 Run with --build to auto-scaffold one pending idea per run,")
        print("     or call planner/builder manually for full control.\n")
        store.end_run(run_id, status="ok",
                      notes=f"Stored {new_ideas} idea(s); --build not set.")
        return

    # --build set: pass exactly one idea to the builder, preferring older
    # pending ideas not yet started over the freshly generated batch.
    selected = store.get_pending_project_idea()
    if not selected:
        print("\n  ℹ️  No pending project ideas found to build.")
        store.end_run(run_id, status="ok",
                      notes="No pending ideas to build.")
        return

    try:
        option = json.loads(selected["option_json"])
        gap = json.loads(selected["gap_json"])
    except (TypeError, json.JSONDecodeError) as e:
        print(f"❌ Stored idea {selected['id']} has invalid JSON: {e}")
        store.end_run(run_id, status="error", notes=f"bad-idea-json: {e}")
        return

    print(f"\n  🏗️  --build flag set, passing one idea to the builder:")
    print(f"      [{selected['id']}] {selected['title']} "
          f"(gap: {selected['gap_skill']})\n")

    store.update_project_idea(selected["id"], status="in_progress")

    try:
        brief = await planner.build_brief(option=option, gap=gap)
    except RuntimeError as e:
        print(f"❌ Planner brief step failed: {e}")
        store.update_project_idea(selected["id"], status="pending")
        store.end_run(run_id, status="error", claude_failures=1,
                      notes=f"build_brief: {e}")
        return

    builder = ProjectBuilderAgent(resume=resume)
    project_dir = await builder.build(brief=brief, output_dir=Path("data/projects"))

    if builder.last_action == "scaffolded":
        store.update_project_idea(
            selected["id"], status="completed", project_dir=str(project_dir)
        )
        notes = f"Scaffolded idea #{selected['id']}: {selected['title']}"
    else:
        # The builder refined an older incomplete project instead. The
        # selected idea is still un-built, so requeue it for the next run.
        store.update_project_idea(selected["id"], status="pending")
        notes = (
            f"Refined existing project at {project_dir}; "
            f"idea #{selected['id']} requeued."
        )

    store.end_run(run_id, status="ok", notes=notes)


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
