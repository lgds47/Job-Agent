"""
Job Search Orchestrator
=======================
Entry point for the agent pipeline. Coordinates all subagents:
  - SearchAgent     → discovers and scores job postings
  - ResumeAgent     → tailors resume bullets to a job description
  - CoverLetterAgent → generates targeted cover letters
  - ProjectAgent    → identifies skill gaps and suggests ML projects

Usage:
  # Discover new jobs and score them
  python orchestrator.py search

  # Generate full application package for a specific job URL
  python orchestrator.py apply --url "https://jobs.example.com/ml-engineer"

  # Run weekly skill gap analysis
  python orchestrator.py gaps
"""

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

    print(f"\nFound {len(results)} postings. Top matches:\n")
    for i, job in enumerate(results[:10], 1):
        print(f"  {i:2}. [{job['score']:.0f}%] {job['title']} @ {job['company']}")
        print(f"       {job['url']}")

    # Persist results to state store
    store = StateStore()
    store.save_jobs(results)
    print(f"\n✅ Saved {len(results)} jobs to state store.")


async def run_apply(args):
    """Generate a full application package for a job URL."""
    print("\n=== APPLICATION PIPELINE ===")
    resume = load_resume()

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

    print(f"\n✅ Application package saved to: {app_dir}")
    print(f"   - jd.json              (parsed job description)")
    print(f"   - tailored_resume.json (reordered + highlighted bullets)")
    print(f"   - cover_letter.md      (ready to copy-paste)")
    print(f"   - meta.json            (status tracker)")


async def run_gaps(args):
    """Analyze recent job postings for skill gaps, plan projects, optionally build."""
    print("\n=== PROJECT PLANNER AGENT ===")
    resume = load_resume()
    store = StateStore()
    recent_jobs = store.get_recent_jobs(n=50)

    if not recent_jobs:
        print("⚠️  No jobs in state store yet. Run `python orchestrator.py search` first.")
        return

    # Step 1: Raw gap extraction (reuse skill counter from old ProjectAgent logic)
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
        return

    # Step 2: Planner analyzes and triages
    planner = ProjectPlannerAgent(resume=resume)
    analyzed_gaps = await planner.analyze(gaps=raw_gaps)
    project_gaps = [g for g in analyzed_gaps if g.get("project_worthy")]

    if not project_gaps:
        print("ℹ️  Planner recommends no portfolio projects for current gaps.")
        print("   Consider certifications or contributions instead.")
        return

    # Step 3: Generate options for the top gap (or iterate manually)
    top_gap = project_gaps[0]
    options = await planner.generate_options(gap=top_gap)

    if args.build:
        # Auto-select option 1 and build — useful for non-interactive runs
        print("\n  🏗️  --build flag set, proceeding with Option 1...")
        brief = await planner.build_brief(option=options[0], gap=top_gap)
        builder = ProjectBuilderAgent(resume=resume)
        await builder.build(brief=brief, output_dir=Path("data/projects"))
    else:
        print("  💡 Run with --build to auto-scaffold Option 1,")
        print("     or call planner/builder manually for full control.\n")


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

    args = parser.parse_args()

    if args.command == "search":
        asyncio.run(run_search(args))
    elif args.command == "apply":
        asyncio.run(run_apply(args))
    elif args.command == "gaps":
        asyncio.run(run_gaps(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
