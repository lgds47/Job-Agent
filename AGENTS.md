# AGENTS.md

## Cursor Cloud specific instructions

### Overview

Python CLI application — a multi-agent job search pipeline built on the Anthropic Claude API. No web server, no Docker, no frontend. Entry point is `job_agent/orchestrator.py` with subcommands: `search`, `apply`, `gaps`, `status`.

### Running the app

All commands must be run from inside `job_agent/` with the virtualenv activated:

```bash
cd /workspace/job_agent
source .venv/bin/activate
python orchestrator.py search                       # discover + score jobs
python orchestrator.py apply --url URL              # generate application package
python orchestrator.py gaps                         # skill gap analysis
python orchestrator.py gaps --build                 # gap analysis + project scaffold
python orchestrator.py status                       # read-only dashboard (text)
python orchestrator.py status --format json         # machine-readable export
python orchestrator.py status --format html -o /tmp/status.html  # HTML export
```

### SearchAgent — early-exit guardrail

`agents/search_agent.py` runs scoring with a rolling quality check. If
`EARLY_EXIT_CONSECUTIVE_LOW` (default `3`) consecutive postings score below
`EARLY_EXIT_SCORE_THRESHOLD` (default `50`), the agent logs a warning and
halts the run — it does not continue fetching/scoring additional listings.
Both constants are exposed at the top of the file; the agent surfaces a
per-run `stats` dict (`jobs_scored`, `early_exits`, `claude_failures`) that
the orchestrator persists to `run_history`. Search is processed in ordered
batches of size `EARLY_EXIT_CONSECUTIVE_LOW` so the streak check has the
right granularity while still permitting some parallelism.

### ProjectBuilderAgent — finish-first

The builder writes a `meta.json` at the root of each scaffolded project
under `data/projects/{slug}/`, with `status: "in_progress"` initially and
`status: "completed"` only after every required artifact is on disk. On
every subsequent `build()` call it scans `data/projects/` first: if any
subdirectory lacks `status == "completed"`, the builder refines the most
recently modified incomplete project (regenerating any missing required
files) instead of scaffolding the new brief. Only when no incomplete
project exists does it accept a new brief from the planner.

### ProjectPlannerAgent — pass-one-per-run

The planner still produces a full list of project options (one set per top
gap), but the orchestrator now persists every option to the new
`project_ideas` table in `data/job_agent.db` and passes exactly one idea to
the builder per `gaps --build` run. Selection prefers older pending ideas
(`status = "pending"`) over freshly generated ones, so leftover options
from previous runs are drained before any new option is built. The chosen
idea is marked `in_progress` while the builder runs, `completed` if the
builder scaffolded it, and reverted to `pending` if the builder refined an
existing project instead.

### Status dashboard

`python orchestrator.py status` is a read-only dashboard (no API key
required) that reports: jobs in `job_agent.db` with scores and metadata,
generated application directories under `data/applications/` (with
per-file presence checks), all stored project ideas with their status,
projects on disk with completion state, and a run-history summary
(`run_history` table) showing what ran, how many jobs were scored, how
many early exits triggered, and Claude call failures. Supports
`--format text|json|html` and `--output PATH`.

### Key environment requirement

`ANTHROPIC_API_KEY` must be set (env var or `.env` file in `job_agent/`). Every agent command calls Claude (`claude-sonnet-4-5`). Without the key, all agent commands fail at the first LLM call.

### Resume JSON

A structured resume file at `job_agent/data/luke_ganalon_resume.json` is required. It is gitignored (contains PII). The full schema must include: `contact` (`name`, `title`), `summary` (`text` — required by both ResumeAgent and CoverLetterAgent), `agent_metadata` (`target_roles` list), `skills` (dict of category → `{label, items}`), `experience` (list with `bullets` containing `id`, `text`, `skills`), `education` (list with `institution`, `degree`, `field`), `certifications` (list with `name`). Bullets must have unique `id` fields (e.g. `b_001_1`) for the ResumeAgent bullet ranking to work.

### Gotchas

- The `search` command's discovery step requests 15-20 companies but the LLM max_tokens is 2000, which can cause JSON truncation. If discovery fails, use `apply --url` instead for reliable end-to-end testing.
- The `apply` command is the best end-to-end test: it exercises the JD parser (real HTTP fetch), ResumeAgent (bullet ranking + summary rewrite), and CoverLetterAgent (generation) in parallel, with no discovery dependency.
- `search --company` still runs the full discovery step first; ad-hoc companies are merged after discovery succeeds.

### Local components that work without API key

- `tools/state_store.py` — SQLite CRUD (auto-creates `data/job_agent.db`)
- `tools/llm_json.py` — JSON fence stripping / parsing
- `tools/text_sanitize.py` — code fence stripping
- `orchestrator.py --help` — CLI entry point
- `orchestrator.py gaps` — runs locally if jobs exist in DB but no skills need LLM backfill
- `orchestrator.py status` — fully offline dashboard over the SQLite store and on-disk artifacts

### No linter or test framework configured

This codebase has no linter config (no `pyproject.toml`, no `ruff.toml`, no `flake8` config) and no automated tests. Import verification (`python -c "from agents.search_agent import SearchAgent; ..."`) is the closest equivalent to a lint check.

### Starter skill

A hands-on runbook for Cloud agents is at `skills/running-and-testing-job-agent/SKILL.md`. Read it before running any commands or debugging a failure — it covers environment setup, per-area testing workflows, common failure modes, and instructions for keeping the skill up to date.
