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

  # Read-only dashboard: jobs, applications, project ideas, run history
  python orchestrator.py status
  python orchestrator.py status --format json
  python orchestrator.py status --format html
"""

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import argparse
import json
import sqlite3
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

    if not target_roles:
        print("⚠️  No target_roles set in resume JSON agent_metadata. Add them first.")
        print('   Example: ["ML Engineer", "MLOps Engineer", "Senior Data Scientist"]')
        return

    adhoc = args.company if args.company else []
    agent = SearchAgent(resume=resume)
    results = await agent.run(roles=target_roles, adhoc_companies=adhoc)

    if not results:
        print("\nNo qualifying postings met the score threshold.")

    # Persist results and log run stats
    store = StateStore()
    if results:
        store.save_jobs(results)
        print(f"\nFound {len(results)} postings. Top matches:\n")
        for i, job in enumerate(results[:10], 1):
            score = float(job.get("score") or 0)
            title = job.get("title") or "Unknown title"
            company = job.get("company") or "Unknown company"
            url = job.get("url") or ""
            print(f"  {i:2}. [{score:.0f}%] {title} @ {company}")
            print(f"       {url}")
        print(f"\n✅ Saved {len(results)} jobs to state store.")

    store.log_run(
        command="search",
        jobs_scored=getattr(agent, "jobs_scored", 0),
        early_exit=getattr(agent, "early_exit_triggered", False),
        claude_failures=getattr(agent, "claude_failures", 0),
        notes=f"roles={target_roles}; adhoc={adhoc}",
    )


async def run_apply(args):
    """Generate a full application package for a job URL."""
    print("\n=== APPLICATION PIPELINE ===")
    resume = load_resume()
    store = StateStore()

    # Step 1: Parse the job description
    print(f"📄 Parsing job description from: {args.url}")
    jd = await parse_jd(args.url)
    print(f"   Role: {jd['title']} @ {jd['company']}")

    # Step 2: Tailor resume in parallel with cover letter generation
    print("\n⚙️  Running Resume + Cover Letter agents in parallel...")
    resume_agent = ResumeAgent(resume=resume)
    cover_agent = CoverLetterAgent(resume=resume)

    tailored_resume, cover_letter = await asyncio.gather(
        resume_agent.run(jd=jd),
        cover_agent.run(jd=jd)
    )

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


async def run_gaps(args):
    """Analyze recent job postings for skill gaps, plan projects, optionally build."""
    print("\n=== PROJECT PLANNER AGENT ===")
    resume = load_resume()
    store = StateStore()
    recent_jobs = store.get_recent_jobs(n=50, min_score=50)

    if not recent_jobs:
        print("⚠️  No jobs in state store yet. Run `python orchestrator.py search` first.")
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
        store.log_run(command="gaps", notes="No skill gaps detected")
        return

    # Step 2: Planner analyzes and triages — pass store so ideas get persisted
    planner = ProjectPlannerAgent(resume=resume, store=store)
    try:
        analyzed_gaps = await planner.analyze(gaps=raw_gaps)
    except RuntimeError as e:
        print(f"❌ Planner analyze step failed: {e}")
        store.log_run(command="gaps", notes=f"analyze failed: {e}")
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
        store.log_run(command="gaps", notes="No project-worthy gaps")
        return

    # Step 3: Generate options for the top gap.
    # generate_options stores all ideas and returns only the next unstarted one.
    top_gap = project_gaps[0]
    try:
        options = await planner.generate_options(gap=top_gap)
    except RuntimeError as e:
        print(f"❌ Planner options step failed: {e}")
        store.log_run(command="gaps", notes=f"options failed: {e}")
        return

    if not options:
        print("⚠️  Planner returned no project options — nothing to build.")
        store.log_run(command="gaps", notes="No options returned")
        return

    store.log_run(command="gaps", notes=f"top_gap={top_gap.get('skill')}; options={len(options)}")

    if args.build:
        print("\n  🏗️  --build flag set, proceeding with Option 1...")
        try:
            brief = await planner.build_brief(option=options[0], gap=top_gap)
        except RuntimeError as e:
            print(f"❌ Planner brief step failed: {e}")
            return
        builder = ProjectBuilderAgent(resume=resume)
        await builder.build(brief=brief, output_dir=Path("data/projects"))
    else:
        print("  💡 Run with --build to auto-scaffold Option 1,")
        print("     or call planner/builder manually for full control.\n")


def _fmt_table(headers: list[str], rows: list[list[str]], col_sep: str = "  ") -> str:
    """Render a plain-text table with auto-width columns."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    lines = []
    header_line = col_sep.join(str(h).ljust(widths[i]) for i, h in enumerate(headers))
    sep_line = col_sep.join("-" * w for w in widths)
    lines.append(header_line)
    lines.append(sep_line)
    for row in rows:
        lines.append(col_sep.join(str(row[i] if i < len(row) else "").ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def run_status(args):
    """Read-only dashboard: jobs, applications, project ideas, run history."""
    store = StateStore()
    fmt = getattr(args, "format", "text") or "text"

    # ── Collect all data ──────────────────────────────────────────────────────

    # Jobs
    with store._conn() as conn:
        conn.row_factory = sqlite3.Row
        job_rows = conn.execute(
            "SELECT title, company, location, score, status, discovered_at FROM jobs "
            "ORDER BY score DESC NULLS LAST, discovered_at DESC"
        ).fetchall()
    jobs_data = [dict(r) for r in job_rows]

    # Applications
    app_records = store.get_applications()
    apps_enriched = []
    for app in app_records:
        app_dir = Path(app["app_dir"]) if app.get("app_dir") else None
        files_present = []
        if app_dir and app_dir.exists():
            for fname in ("jd.json", "tailored_resume.json", "cover_letter.md", "meta.json"):
                if (app_dir / fname).exists():
                    files_present.append(fname)
        apps_enriched.append({**app, "files": files_present})

    # Project ideas
    ideas = store.get_all_project_ideas()

    # Run logs
    run_logs = store.get_run_logs(limit=20)

    # ── Format: JSON ──────────────────────────────────────────────────────────
    if fmt == "json":
        payload = {
            "jobs": jobs_data,
            "applications": apps_enriched,
            "project_ideas": ideas,
            "run_logs": run_logs,
        }
        print(json.dumps(payload, indent=2, default=str))
        return

    # ── Format: HTML ──────────────────────────────────────────────────────────
    if fmt == "html":
        def _html_table(headers, rows):
            th = "".join(f"<th>{h}</th>" for h in headers)
            body = ""
            for row in rows:
                body += "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>"
            return f"<table border='1'><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"

        sections = ["<html><body>"]
        sections.append("<h1>Job Agent Dashboard</h1>")

        sections.append("<h2>Jobs</h2>")
        job_html_rows = [
            [j.get("title",""), j.get("company",""), j.get("location",""),
             f"{j.get('score') or 0:.0f}", j.get("status",""), j.get("discovered_at","")[:10]]
            for j in jobs_data
        ]
        sections.append(_html_table(["Title","Company","Location","Score","Status","Discovered"], job_html_rows))

        sections.append("<h2>Applications</h2>")
        app_html_rows = [
            [Path(a.get("app_dir","")).name if a.get("app_dir") else "",
             a.get("status",""), a.get("created_at","")[:10], ", ".join(a.get("files",[]))]
            for a in apps_enriched
        ]
        sections.append(_html_table(["Directory","Status","Created","Files"], app_html_rows))

        sections.append("<h2>Project Ideas</h2>")
        idea_html_rows = [
            [i.get("title",""), i.get("skill_gap",""), i.get("status",""), i.get("created_at","")[:10]]
            for i in ideas
        ]
        sections.append(_html_table(["Title","Skill Gap","Status","Created"], idea_html_rows))

        sections.append("<h2>Run History (last 20)</h2>")
        log_html_rows = [
            [r.get("command",""), r.get("started_at","")[:19], str(r.get("jobs_scored",0)),
             "yes" if r.get("early_exit") else "no", str(r.get("claude_failures",0)),
             r.get("notes","") or ""]
            for r in run_logs
        ]
        sections.append(_html_table(
            ["Command","Started","Scored","Early Exit","Failures","Notes"], log_html_rows
        ))

        sections.append("</body></html>")
        print("\n".join(sections))
        return

    # ── Format: text (default) ────────────────────────────────────────────────
    W = 78
    print("\n" + "=" * W)
    print("  JOB AGENT — STATUS DASHBOARD")
    print("=" * W)

    # ── Jobs ──────────────────────────────────────────────────────────────────
    print(f"\n  JOBS  ({len(jobs_data)} total)\n")
    if jobs_data:
        job_table_rows = [
            [
                f"{j.get('score') or 0:.0f}",
                (j.get("title") or "")[:38],
                (j.get("company") or "")[:22],
                (j.get("location") or "")[:18],
                j.get("status") or "",
                (j.get("discovered_at") or "")[:10],
            ]
            for j in jobs_data
        ]
        tbl = _fmt_table(
            ["Score", "Title", "Company", "Location", "Status", "Discovered"],
            job_table_rows,
        )
        for line in tbl.splitlines():
            print(f"  {line}")

        # Score buckets
        buckets = {"90-100": 0, "70-89": 0, "50-69": 0, "<50": 0}
        for j in jobs_data:
            s = float(j.get("score") or 0)
            if s >= 90: buckets["90-100"] += 1
            elif s >= 70: buckets["70-89"] += 1
            elif s >= 50: buckets["50-69"] += 1
            else: buckets["<50"] += 1
        print(f"\n  Score buckets: " + "  ".join(f"{k}: {v}" for k, v in buckets.items()))
    else:
        print("  (no jobs — run `search` first)")

    # ── Applications ──────────────────────────────────────────────────────────
    print(f"\n  APPLICATIONS  ({len(apps_enriched)} total)\n")
    if apps_enriched:
        app_table_rows = [
            [
                Path(a.get("app_dir","")).name[:40] if a.get("app_dir") else "(no dir)",
                a.get("status") or "",
                (a.get("created_at") or "")[:10],
                ", ".join(a.get("files", [])) or "(none)",
            ]
            for a in apps_enriched
        ]
        tbl = _fmt_table(
            ["Directory", "Status", "Created", "Files Present"],
            app_table_rows,
        )
        for line in tbl.splitlines():
            print(f"  {line}")
    else:
        print("  (no applications — run `apply --url URL` first)")

    # ── Project Ideas ─────────────────────────────────────────────────────────
    print(f"\n  PROJECT IDEAS  ({len(ideas)} total)\n")
    if ideas:
        idea_table_rows = [
            [
                str(i.get("id", "")),
                (i.get("title") or "")[:42],
                (i.get("skill_gap") or "")[:20],
                i.get("status") or "",
                (i.get("created_at") or "")[:10],
            ]
            for i in ideas
        ]
        tbl = _fmt_table(
            ["ID", "Title", "Skill Gap", "Status", "Created"],
            idea_table_rows,
        )
        for line in tbl.splitlines():
            print(f"  {line}")
        pending = sum(1 for i in ideas if i.get("status") == "pending")
        started = sum(1 for i in ideas if i.get("status") == "started")
        complete = sum(1 for i in ideas if i.get("status") == "complete")
        print(f"\n  Counts: pending={pending}  started={started}  complete={complete}")
    else:
        print("  (no ideas — run `gaps --build` first)")

    # ── Run History ───────────────────────────────────────────────────────────
    print(f"\n  RUN HISTORY  (last {len(run_logs)})\n")
    if run_logs:
        log_table_rows = [
            [
                r.get("command") or "",
                (r.get("started_at") or "")[:19],
                str(r.get("jobs_scored") or 0),
                "yes" if r.get("early_exit") else "no",
                str(r.get("claude_failures") or 0),
                (r.get("notes") or "")[:40],
            ]
            for r in run_logs
        ]
        tbl = _fmt_table(
            ["Command", "Started", "Scored", "Early Exit", "Failures", "Notes"],
            log_table_rows,
        )
        for line in tbl.splitlines():
            print(f"  {line}")
    else:
        print("  (no runs logged yet)")

    print("\n" + "=" * W + "\n")


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
        help="Read-only dashboard: jobs, applications, project ideas, run history"
    )
    status_p.add_argument(
        "--format", choices=["text", "json", "html"], default="text",
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
