# Job Search Agent System

A personal multi-agent pipeline built on the Anthropic API. Automates job discovery,
resume tailoring, cover letter generation, skill gap analysis, and portfolio project
scaffolding — with you staying in the decision loop at every meaningful step.

Built for an early-career ML/data engineer.

---

## How It Works

The system is organized around a single `orchestrator.py` that coordinates five
specialized agents. Each agent has a focused responsibility and communicates its
reasoning before doing anything irreversible.

```
orchestrator.py
│
├── agents/
│   ├── search_agent.py          # Company discovery + job scoring
│   ├── resume_agent.py          # Bullet reranking + summary rewriting
│   ├── cover_letter_agent.py    # Four-slot targeted cover letters
│   ├── project_planner_agent.py # Gap triage + project selection
│   └── project_builder_agent.py # Repo scaffolding + starter code
│
├── tools/
│   ├── jd_parser.py             # Fetch + structure any job URL via Claude
│   └── state_store.py           # SQLite persistence (jobs, apps, gaps)
│
└── data/
    ├── luke_ganalon_resume.json       # Structured resume — source of truth
    ├── job_agent.db                   # SQLite state (auto-created on first run)
    ├── applications/                  # One folder per application package
    │   └── YYYYMMDD_company_role/
    │       ├── jd.json                # Parsed job description
    │       ├── tailored_resume.json   # Reranked bullets for this JD
    │       ├── cover_letter.md        # Ready to review and send
    │       └── meta.json              # Status tracker
    └── projects/                      # Scaffolded portfolio projects
        └── project_slug/
            ├── README.md
            ├── requirements.txt
            ├── MILESTONES.md
            ├── project_brief.json
            ├── src/
            │   ├── train.py
            │   ├── model.py
            │   ├── dataset.py
            │   └── evaluate.py
            ├── configs/
            │   └── config.yaml
            └── notebooks/
                └── 01_exploration.ipynb
```

---

## Setup

```bash
# 1. Enter the project directory
cd job_agent

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."

# 4. Set your target roles in the resume JSON
# Edit data/luke_ganalon_resume.json → agent_metadata.target_roles
# Example:
# "target_roles": ["ML Engineer", "MLOps Engineer", "AI Engineer", "Data Scientist"]
```

The resume JSON (`data/luke_ganalon_resume.json`) is the only file you need to
configure manually before first run. The SQLite database is created automatically.

---

## Commands

### `search` — Discover and score job postings

```bash
python orchestrator.py search
```

The Search Agent uses Claude to research which companies are actively hiring for
your target roles right now — no hardcoded company lists. It evaluates each company
for early-career fit (ML maturity, eng culture, mentorship norms, funding health),
detects their ATS (Greenhouse, Lever, Ashby, or custom career page), fetches open
roles, and scores each posting against your resume on a 0–100 scale.

**Adding a specific company on the fly:**

```bash
python orchestrator.py search --company "Glean"
python orchestrator.py search --company "Glean" "Cohere" "Replit"
```

The `--company` flag takes one or more company names. The agent researches each,
detects its ATS, fetches open roles, scores them, and merges into results — no
config changes, no code edits. Use this whenever you hear about a company worth
checking.

Results are saved to the SQLite state store for use by the gap analysis pipeline.

---

### `apply` — Generate a full application package

```bash
python orchestrator.py apply --url "https://boards.greenhouse.io/anthropic/jobs/12345"
```

Runs the Resume Agent and Cover Letter Agent in parallel against the job URL.
The JD Parser fetches and structures the posting first, then:

- **Resume Agent** scores all resume bullets for relevance to this JD, reranks
  them within each role block, rewrites your professional summary to mirror the
  JD's language, and surfaces which skills to emphasize.
- **Cover Letter Agent** generates a targeted letter using a four-slot structure:
  hook → why them → why you (with proof points from your resume) → CTA.
  Under 350 words.

Output lands in `data/applications/YYYYMMDD_company_role/`:

| File | Contents |
|---|---|
| `jd.json` | Parsed and structured job description |
| `tailored_resume.json` | Bullets reranked + summary rewritten for this JD |
| `cover_letter.md` | Ready to review — verify all facts before sending |
| `meta.json` | Status tracker — update `status` as you progress |

**Tracking application status:**

```python
from tools.state_store import StateStore
store = StateStore()

store.update_application_status("https://...", "applied")
store.update_application_status("https://...", "interview", notes="Phone screen Fri 2pm")
store.update_application_status("https://...", "offer")
store.update_application_status("https://...", "rejected")
# Valid flow: draft → applied → interview → offer → rejected
```

---

### `gaps` — Analyze skill gaps and plan portfolio projects

```bash
python orchestrator.py gaps
```

Requires at least one `search` run first (reads from the SQLite state store).

This command runs a two-agent pipeline with a human review gate between them.

**Stage 1 — Triage (ProjectPlannerAgent)**

Aggregates skills from recent job postings, compares against your profile, and
reasons openly about each gap. For every gap it recommends an action: portfolio
project, certification, open-source contribution, coursework, or on-the-job
exposure. Prints its reasoning, then shows a triage table:

```
  SKILL                          ACTION             PRIORITY   PROJECT?
  ------------------------------ ------------------ ---------- --------
  distributed training           project            high       ✅
  vllm                           project            high       ✅
  terraform                      certification      medium     ⬜
  spark                          on-the-job         low        ⬜
```

**Stage 2 — Option generation (your decision point)**

For each project-worthy gap, the Planner generates three meaningfully different
project options with honest tradeoffs — difficulty, time estimate, compute cost,
whether the idea is saturated, and a preview of the resume bullet each would
produce. You review before anything gets built.

**Stage 3 — Build (optional)**

```bash
python orchestrator.py gaps --build
```

With `--build`, the Planner produces a full structured brief for Option 1 and
passes it to the Project Builder Agent, which scaffolds the entire repo: directory
structure, substantive starter code with architectural comments, README, requirements,
milestone tracker, and exploration notebook.

For full control over which option gets built, use the manual flow in
Advanced Usage below.

---

## Advanced Usage

### Choose a specific project option before building

```python
import asyncio, json
from agents.project_planner_agent import ProjectPlannerAgent
from agents.project_builder_agent import ProjectBuilderAgent
from pathlib import Path

with open("data/luke_ganalon_resume.json") as f:
    resume = json.load(f)

async def main():
    planner = ProjectPlannerAgent(resume=resume)

    # Define a gap manually or pull from a gaps run
    gap = {
        "skill": "distributed training",
        "frequency": 14,
        "gap_level": "missing",
        "candidate_head_start": "strong PyTorch base, Docker/Kubernetes experience"
    }

    # Step 1: Triage and see options with tradeoffs
    analyzed = await planner.analyze(gaps=[gap])
    options = await planner.generate_options(gap=analyzed[0])

    # Step 2: Pick your option (0, 1, or 2)
    chosen = options[1]

    # Step 3: Get the full structured brief
    brief = await planner.build_brief(option=chosen, gap=analyzed[0])

    # Step 4: Scaffold the project
    builder = ProjectBuilderAgent(resume=resume)
    project_dir = await builder.build(brief=brief, output_dir=Path("data/projects"))
    print(f"Project ready: {project_dir}")

asyncio.run(main())
```

### Parse a job description without running the full pipeline

Useful for inspecting what the Resume and Cover Letter agents will receive:

```python
import asyncio
from tools.jd_parser import parse_jd

jd = asyncio.run(parse_jd("https://jobs.lever.co/somecompany/some-role"))
print(jd["required_skills"])
print(jd["keywords"])
print(jd["culture_signals"])
```

### Query the state store

```python
from tools.state_store import StateStore
store = StateStore()

# Pipeline summary
print(store.summary())
# → {"jobs_discovered": 47, "applications": {"draft": 3, "applied": 8, "interview": 2}}

# Top-scored jobs above a threshold
jobs = store.get_recent_jobs(n=20, min_score=75)

# All active applications at a given status
apps = store.get_applications(status="interview")

# Current skill gaps from last analysis
gaps = store.get_skill_gaps()
```

---

## Design Decisions

**Resume as structured JSON, not PDF.** Every bullet is tagged with skills,
impact type, and domain. Agents select and reorder bullets programmatically
rather than rewriting from scratch — keeping output grounded and factually accurate.
The PDF is generated downstream from this JSON, never the other way around.

**No hardcoded company lists.** The Search Agent uses Claude to research which
companies are actively hiring right now, evaluated against early-career fit criteria.
The roster refreshes every run. One-off companies are added via `--company` without
touching any config or code.

**Two project agents, not one.** Planning (which project, why, which option) and
building (scaffold, code, files) are separated with a human review gate between
them. Collapsing both into one agent produces worse output on both tasks and removes
your ability to redirect before a repo is built around the wrong idea.

**Agents communicate before acting.** The Search Agent surfaces its company
reasoning. The Planner prints its gap triage and option tradeoffs. The Builder
reports each file as it writes it. Nothing happens silently.

**SQLite over flat files for state.** All jobs, applications, and skill gaps live
in a single `job_agent.db`. Queryable, durable across runs, and inspectable with
any SQLite viewer or the Python API.

---

## Roadmap

- [ ] Render `tailored_resume.json` to a formatted PDF for submission
- [ ] Weekly digest: email or Slack summary of new high-score postings
- [ ] Interview prep agent: generates likely questions from a JD + your resume
- [ ] Outcome tracking: correlate resume match scores with actual response rates
- [ ] LinkedIn data export ingestion: merge projects and recommendations into resume JSON
- [ ] Claude Code integration: hand off `project_brief.json` directly to Claude Code for agentic execution
