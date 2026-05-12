# AGENTS.md

## Cursor Cloud specific instructions

### Overview

Python CLI application — a multi-agent job search pipeline built on the Anthropic Claude API. No web server, no Docker, no frontend. Entry point is `job_agent/orchestrator.py` with subcommands: `search`, `apply`, `gaps`.

### Running the app

All commands must be run from inside `job_agent/` with the virtualenv activated:

```bash
cd /workspace/job_agent
source .venv/bin/activate
python orchestrator.py search          # discover + score jobs
python orchestrator.py apply --url URL # generate application package
python orchestrator.py gaps            # skill gap analysis
python orchestrator.py gaps --build    # gap analysis + project scaffold
python orchestrator.py status          # read-only dashboard (text)
python orchestrator.py status --format json   # JSON export
python orchestrator.py status --format html   # HTML export
```

### Key environment requirement

`ANTHROPIC_API_KEY` must be set (env var or `.env` file in `job_agent/`). Every agent command calls Claude (`claude-sonnet-4-5`). Without the key, all agent commands fail at the first LLM call.

### Resume JSON

A structured resume file at `job_agent/data/luke_ganalon_resume.json` is required. It is gitignored (contains PII). The full schema must include: `contact` (`name`, `title`), `summary` (`text` — required by both ResumeAgent and CoverLetterAgent), `agent_metadata` (`target_roles` list), `skills` (dict of category → `{label, items}`), `experience` (list with `bullets` containing `id`, `text`, `skills`), `education` (list with `institution`, `degree`, `field`), `certifications` (list with `name`). Bullets must have unique `id` fields (e.g. `b_001_1`) for the ResumeAgent bullet ranking to work.

### SearchAgent — early-exit guardrail

During job scoring, the agent tracks a rolling count of consecutive jobs that score below `EARLY_EXIT_SCORE_THRESHOLD` (default: 50). If `EARLY_EXIT_CONSECUTIVE_LOW` consecutive jobs (default: 3) all fall below that threshold, the agent logs a warning and halts further scoring rather than burning API credits on a low-signal batch. Both constants live at the top of `agents/search_agent.py` for easy tuning. Per-run stats (`jobs_scored`, `early_exit_triggered`, `claude_failures`) are stored on the `SearchAgent` instance after each run and persisted to the `run_logs` table.

### ProjectPlannerAgent — store all ideas, pass one at a time

`generate_options` generates all project ideas as usual, but when a `StateStore` is provided (which the orchestrator always passes now), **all options are persisted** to the `project_ideas` table. Only the oldest pending idea is returned to the builder per run — ideas that are already started or complete are skipped, and newly generated ideas that are not selected remain stored for future runs. `build_brief` marks the chosen idea as `started` and persists the full brief JSON.

### ProjectBuilderAgent — finish before starting

Before scaffolding a new project, `build` scans `data/projects/` for any directory that lacks a `completed.json` marker. If incomplete projects exist, the builder selects the most recently modified one and refines it (generates missing source files, regenerates README and `requirements.txt`) rather than starting fresh. A `completed.json` is written when all expected files are present — both after initial scaffold and after a refinement pass. Only when every existing project is marked complete will the builder accept a new brief and scaffold from scratch.

### `status` subcommand

`python orchestrator.py status` is a read-only dashboard requiring no API key. It renders four sections: all jobs with scores, all application packages with file-presence indicators, all project ideas with their completion state, and the last 20 run-log entries (jobs scored, early-exit flag, Claude failures). Optional `--format json` and `--format html` flags emit machine-readable or browser-viewable output.

### Gotchas

- The `search` command's discovery step requests 15-20 companies but the LLM max_tokens is 2000, which can cause JSON truncation. If discovery fails, use `apply --url` instead for reliable end-to-end testing.
- The `apply` command is the best end-to-end test: it exercises the JD parser (real HTTP fetch), ResumeAgent (bullet ranking + summary rewrite), and CoverLetterAgent (generation) in parallel, with no discovery dependency.
- `search --company` still runs the full discovery step first; ad-hoc companies are merged after discovery succeeds.
- `status` works with an empty or partial database — all sections gracefully show "(none)" when data is absent.

### Local components that work without API key

- `tools/state_store.py` — SQLite CRUD (auto-creates `data/job_agent.db`)
- `tools/llm_json.py` — JSON fence stripping / parsing
- `tools/text_sanitize.py` — code fence stripping
- `orchestrator.py --help` — CLI entry point
- `orchestrator.py gaps` — runs locally if jobs exist in DB but no skills need LLM backfill
- `orchestrator.py status` — always local; reads directly from SQLite and `data/` filesystem

### No linter or test framework configured

This codebase has no linter config (no `pyproject.toml`, no `ruff.toml`, no `flake8` config) and no automated tests. Import verification (`python -c "from agents.search_agent import SearchAgent; ..."`) is the closest equivalent to a lint check.

### Starter skill

A hands-on runbook for Cloud agents is at `skills/running-and-testing-job-agent/SKILL.md`. Read it before running any commands or debugging a failure — it covers environment setup, per-area testing workflows, common failure modes, and instructions for keeping the skill up to date.
