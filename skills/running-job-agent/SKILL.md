---
name: running-job-agent
description: Use when a Cloud agent needs to run, test, or smoke-check this job_agent codebase — covers virtualenv setup, ANTHROPIC_API_KEY handling, mocking the gitignored resume JSON, choosing which orchestrator subcommand to exercise, and the offline-only modules that work without an API key
---

# Running & Testing the Job Agent (Cloud Starter Skill)

## Overview

This repo is a Python CLI multi-agent pipeline (`job_agent/orchestrator.py`) built on the Anthropic Claude API. There is **no web server, no Docker, no frontend, no linter, no test framework**. Every "agent" command calls `claude-sonnet-4-5`. The "feature flag" equivalent here is the **presence of `ANTHROPIC_API_KEY` + a valid resume JSON** — without them, agent commands fail at the first LLM call.

When testing in a fresh Cloud VM, start with the offline-only checks (no key required), then escalate to a single `apply --url` end-to-end run as the gold-standard integration test.

## Required Setup (Every Cloud Session)

```bash
cd /workspace/job_agent
source .venv/bin/activate          # venv is pre-built; if missing, recreate: python -m venv .venv && pip install -r requirements.txt
echo $ANTHROPIC_API_KEY | head -c 10   # confirm the secret is injected
ls data/luke_ganalon_resume.json   # gitignored — see "Mocking the resume JSON" below if missing
python orchestrator.py --help      # cheapest sanity check; works with no key, no resume
```

**Secrets:** `ANTHROPIC_API_KEY` must be added in **Cursor Dashboard → Cloud Agents → Secrets** (or in `job_agent/.env`). Cloud VMs inject it as an env var on startup. There is no login flow — the key is the only auth.

**Resume JSON:** `job_agent/data/luke_ganalon_resume.json` is gitignored (PII). A fresh Cloud VM will not have it. Either (a) ask the user to upload it, or (b) write a minimal mock — see below.

## Codebase Areas, Triggers, and Testing Workflows

### 1. `orchestrator.py` (CLI entry point)

**Touches:** argparse, command dispatch, file I/O for `data/applications/`, status printing.

**Test workflow:**
- `python orchestrator.py --help` and `python orchestrator.py <subcmd> --help` — verify argparse with no API key.
- Import smoke check: `python -c "from agents.search_agent import SearchAgent; from agents.resume_agent import ResumeAgent; from agents.cover_letter_agent import CoverLetterAgent; from agents.project_planner_agent import ProjectPlannerAgent; from agents.project_builder_agent import ProjectBuilderAgent; from tools.jd_parser import parse_jd; print('ok')"`. This is the project's de-facto lint check.
- End-to-end: `python orchestrator.py apply --url <real_greenhouse_or_lever_url>` is the most reliable full-pipeline test (parser + ResumeAgent + CoverLetterAgent in parallel, no discovery dependency).

### 2. `agents/` (LLM-backed)

**Files:** `search_agent.py`, `resume_agent.py`, `cover_letter_agent.py`, `project_planner_agent.py`, `project_builder_agent.py`. All use `AsyncAnthropic()` at import time — they need the key.

**Test workflow:**
- For changes to **one agent**, prefer driving it directly from a one-off script (see `README.md` "Advanced Usage") rather than running the full `apply`/`gaps` pipeline. Cheaper, faster, isolated.
- For changes to **ResumeAgent or CoverLetterAgent**, run `apply --url URL` — exercises both in parallel.
- For changes to **SearchAgent**, prefer `search --company "Glean"` (single ad-hoc company) over plain `search`. The full discovery step requests 15-20 companies but caps at `max_tokens=2000`, so JSON truncation is a known failure mode (see AGENTS.md gotchas). Discovery still runs first even with `--company`, so a truncation failure blocks ad-hoc merging.
- For changes to **ProjectPlannerAgent / ProjectBuilderAgent**, seed the SQLite DB with `search` (or hand-insert rows via `StateStore.save_jobs(...)`) and then run `gaps` or `gaps --build`.

### 3. `tools/` (mixed — offline + LLM)

**Offline (no key needed, ideal for fast Cloud iteration):**
- `tools/state_store.py` — SQLite CRUD. Auto-creates `data/job_agent.db`. Inspect with the Python API:
  ```python
  from tools.state_store import StateStore
  StateStore().summary()
  ```
- `tools/llm_json.py` — JSON fence stripping / parsing. Pure function, unit-testable inline.
- `tools/text_sanitize.py` — markdown code-fence stripping. Pure function, unit-testable inline.

**LLM-backed (needs key):**
- `tools/jd_parser.py` — fetches a URL via `httpx`, parses HTML with BeautifulSoup, then calls Claude. Test by calling `asyncio.run(parse_jd(URL))`.
- `tools/job_skills.py` — `enrich_jobs_skill_lists` backfills required/preferred skills on rows that lack them. Test by inserting a stub job row and calling the function.

### 4. `data/` (state)

- `data/job_agent.db` — gitignored; auto-created on first `StateStore()`. Safe to delete to reset state between tests.
- `data/applications/<timestamp>_<slug>/` — gitignored; generated per `apply` run. Inspect `jd.json`, `tailored_resume.json`, `cover_letter.md`, `meta.json`.
- `data/projects/<slug>/` — generated by `gaps --build`.
- `data/luke_ganalon_resume.json` — gitignored, hand-managed. See mocking below.

## Mocking the Resume JSON (no PII required)

When the real resume is unavailable in a Cloud VM, drop this minimal mock at `job_agent/data/luke_ganalon_resume.json` so schema-dependent code can run. Every field below is read by at least one agent — removing any will break imports or LLM prompts.

```json
{
  "contact": { "name": "Test User", "title": "ML Engineer" },
  "summary": { "text": "Early-career ML engineer with Python, PyTorch, and Docker experience." },
  "agent_metadata": { "target_roles": ["ML Engineer", "MLOps Engineer"] },
  "skills": {
    "ml": { "label": "Machine Learning", "items": ["PyTorch", "scikit-learn"] },
    "infra": { "label": "Infrastructure", "items": ["Docker", "Kubernetes"] }
  },
  "experience": [
    {
      "company": "Acme",
      "title": "ML Engineer",
      "bullets": [
        { "id": "b_001_1", "text": "Trained a CV model in PyTorch, improved F1 by 12%.", "skills": ["PyTorch", "computer vision"] },
        { "id": "b_001_2", "text": "Deployed model to Kubernetes serving 1M requests/day.", "skills": ["Kubernetes", "Docker"] }
      ]
    }
  ],
  "education": [{ "institution": "Test University", "degree": "B.S.", "field": "Computer Science" }],
  "certifications": [{ "name": "AWS Certified ML Specialty" }]
}
```

Note the `id` fields on bullets are required by ResumeAgent's bullet-ranking step.

## Decision: Which Test to Run

| Change area | Cheapest signal | Real integration test |
|---|---|---|
| Argparse / CLI wiring | `orchestrator.py --help` | one full subcommand run |
| Any agent import / signature | Import smoke check (above) | one full subcommand run |
| `tools/state_store.py` | Python REPL: insert + read row | `apply --url` + inspect DB |
| `tools/llm_json.py`, `text_sanitize.py` | `python -c` with sample input | n/a — pure functions |
| `tools/jd_parser.py` | `asyncio.run(parse_jd(URL))` | full `apply --url` |
| ResumeAgent / CoverLetterAgent | one-off script driving the agent | `apply --url URL` (preferred — exercises both) |
| SearchAgent | `search --company "OneCompany"` | `search` (beware 2k-token truncation) |
| Planner / Builder | seed DB then `gaps --build` | `gaps --build` end-to-end |

## Common Failure Modes

- **`anthropic.AuthenticationError`** → `ANTHROPIC_API_KEY` is missing or invalid. Check `echo $ANTHROPIC_API_KEY | head -c 10`. If empty in a Cloud VM, ask the user to add it in Cursor Dashboard → Cloud Agents → Secrets.
- **`FileNotFoundError: data/luke_ganalon_resume.json`** → drop in the mock above (gitignored — never commit it).
- **`json.JSONDecodeError` during `search` discovery** → the `max_tokens=2000` truncation gotcha. Re-run with `--company` for ad-hoc, or switch to `apply --url` for end-to-end validation.
- **`ModuleNotFoundError`** → forgot to `source .venv/bin/activate`, or the venv is missing in a fresh VM (`python -m venv .venv && pip install -r requirements.txt`).
- **httpx errors in `jd_parser`** → some ATSes (workday, custom) block bot user-agents. Prefer Greenhouse / Lever / Ashby URLs when testing.

## Resetting State Between Test Runs

```bash
rm -f data/job_agent.db                    # wipe SQLite
rm -rf data/applications/* data/projects/* # wipe generated outputs
```

The resume JSON and `.venv` are never touched by the pipeline — leave them in place.

## Updating This Skill

This skill is a living runbook. **Whenever you discover a new testing trick, a mock that worked, an environment quirk, or a failure mode that wasted time, add it here in the same turn** — don't defer.

Update rules:
1. Edit `skills/running-job-agent/SKILL.md` directly. Add the new item under the most relevant existing section, or create a new short section if none fits.
2. Keep it minimal — one sentence per tip, command shown inline. Do **not** turn this into a tutorial.
3. If you change the resume schema, update the **Mocking the Resume JSON** block in lock-step.
4. If you add a new orchestrator subcommand, agent, or top-level tool, add a row to **Codebase Areas** and **Decision: Which Test to Run**.
5. If a known gotcha (e.g. the 2k-token truncation) is fixed in code, remove it from **Common Failure Modes** in the same commit.
6. Commit the skill change alongside the code change that motivated it — same PR, separate commit is fine.
