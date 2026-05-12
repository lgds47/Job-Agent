---
name: running-and-testing-job-agent
description: Use when starting work in this job-search pipeline codebase, before running commands, debugging a failure, or verifying a change works end-to-end
---

# Running and Testing the Job Agent

## Overview

Python CLI — no server, no Docker. Entry point: `job_agent/orchestrator.py`. Three subcommands: `search`, `apply`, `gaps`. All LLM calls use `claude-sonnet-4-5` via `ANTHROPIC_API_KEY`.

## Required Setup (Every Cloud Session)

```bash
cd /workspace/job_agent
source .venv/bin/activate
# API key: set ANTHROPIC_API_KEY env var OR create job_agent/.env with ANTHROPIC_API_KEY=sk-...
```

Run these three checks in order after activation:

1. `python --version` — expect `3.10+`; if lower, the venv is pointed at the wrong interpreter.
2. `python -c "import os; print(bool(os.getenv('ANTHROPIC_API_KEY')))"` — `True`: full pipeline available; `False`: use local-only workflows (no Claude calls).
3. `python -c "from pathlib import Path; print(Path('data/luke_ganalon_resume.json').exists())"` — must be `True` for any agent flow; if `False`, drop in the mock JSON from the Mocking section below.

**Resume JSON** is required at `job_agent/data/luke_ganalon_resume.json` (gitignored, contains PII).  
Required schema fields: `contact.name/title`, `summary.text`, `agent_metadata.target_roles[]`,  
`skills{category → {label, items[]}}`, `experience[].bullets[].id/text/skills`,  
`education[].institution/degree/field`, `certifications[].name`.  
Bullet `id` fields must be globally unique (e.g. `b_001_1`) — `ResumeAgent` uses them as sort keys.

## Codebase Areas

### tools/ — no API key needed

These modules run locally. Use them to sanity-check the environment without burning API credits.

```bash
# Import check for all three utility modules
python -c "
from tools.state_store import StateStore
from tools.llm_json import loads_llm_json, strip_json_fences
from tools.text_sanitize import strip_code_fences
s = StateStore()          # auto-creates data/job_agent.db
print(s.summary())        # jobs_discovered, score_distribution, applications
"

# StateStore standalone summary (useful after a search/apply run)
python tools/state_store.py
```

### agents/ — require API key + resume JSON

Import check (fast, no API call):

```bash
python -c "from agents.search_agent import SearchAgent; print('OK')"
python -c "from agents.resume_agent import ResumeAgent; print('OK')"
python -c "from agents.cover_letter_agent import CoverLetterAgent; print('OK')"
python -c "from agents.project_planner_agent import ProjectPlannerAgent; print('OK')"
python -c "from agents.project_builder_agent import ProjectBuilderAgent; print('OK')"
```

**End-to-end test** (`apply` is the most reliable — no discovery step, no truncation risk):

```bash
python orchestrator.py apply --url "https://jobs.lever.co/anthropic/SOME-JOB-ID"
# Writes: data/applications/YYYYMMDD_company_role/{jd.json, tailored_resume.json, cover_letter.md, meta.json}
# Also upserts an applications row in data/job_agent.db
```

### orchestrator.py — subcommands

```bash
python orchestrator.py --help          # always safe, no API key required

python orchestrator.py search          # discovery + scoring (may truncate at max_tokens=2000)
python orchestrator.py search --company "Glean" "Cohere"  # append ad-hoc companies

python orchestrator.py gaps            # gap analysis — requires jobs already in DB
python orchestrator.py gaps --build    # gap analysis + auto-scaffold Option 1 project
```

## Common Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `❌ Failed to parse company discovery JSON` | `search` discovery truncated at max_tokens=2000 | Use `apply --url` instead |
| `FileNotFoundError: data/luke_ganalon_resume.json` | Resume JSON not present | Create it from the schema above |
| `AuthenticationError` or missing key message | `ANTHROPIC_API_KEY` not set | Add `job_agent/.env` or export the var |
| `ResumeAgent bullet ranking failed` | Bullet `id` fields missing or duplicated | Fix `id` fields in resume JSON |
| `No jobs in state store yet` on `gaps` | `search` hasn't run yet | Run `search` first or seed DB manually |
| `⚠️ No target_roles set` on `search` | Missing `agent_metadata.target_roles` in resume JSON | Add role list to resume JSON |

## Mocking

When `data/luke_ganalon_resume.json` is absent (fresh clone, CI, no PII available), create a minimal stub that satisfies the full schema:

```bash
mkdir -p data
cat > data/luke_ganalon_resume.json << 'EOF'
{
  "contact": {"name": "Test User", "title": "ML Engineer"},
  "summary": {"text": "ML engineer with experience in model training and deployment."},
  "agent_metadata": {"target_roles": ["ML Engineer", "MLOps Engineer"]},
  "skills": {
    "ml": {"label": "Machine Learning", "items": ["Python", "PyTorch", "scikit-learn"]}
  },
  "experience": [
    {
      "company": "ACME Corp",
      "title": "ML Engineer",
      "current": true,
      "bullets": [
        {"id": "b_001_1", "text": "Trained and deployed CV models reducing latency 40%.", "skills": ["PyTorch"]},
        {"id": "b_001_2", "text": "Built data pipelines processing 10M records/day.", "skills": ["Python"]}
      ]
    }
  ],
  "education": [{"institution": "State University", "degree": "BS", "field": "Computer Science"}],
  "certifications": [{"name": "AWS ML Specialty"}]
}
EOF
```

Sufficient for `apply --url` end-to-end tests. Do not commit — `data/` is gitignored.

## Updating This Skill

When you find a new failure mode, workaround, or testing shortcut:

1. Add a row to Common Failure Modes, or a command block to the relevant area section.
2. Keep it brief — one bullet or one table row per discovery. No narrative.
3. Commit: `git add skills/ && git commit -m "skill(running-and-testing): <what changed>"`

**Do not** add step-by-step descriptions of how you solved a one-off problem.  
**Do** generalize it into a reusable pattern that helps the next agent hit the ground running.
