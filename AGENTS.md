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
```

### Key environment requirement

`ANTHROPIC_API_KEY` must be set (env var or `.env` file in `job_agent/`). Every agent command calls Claude (`claude-sonnet-4-5`). Without the key, all agent commands fail at the first LLM call.

### Resume JSON

A structured resume file at `job_agent/data/luke_ganalon_resume.json` is required. It is gitignored (contains PII). For testing, create a minimal JSON with keys: `contact` (`name`, `title`), `agent_metadata` (`target_roles` list), `skills` (dict of category → `{label, items}`), `experience` (list with `bullets` containing `text`), `education` (list with `degree`, `field`), `certifications` (list with `name`).

### Local components that work without API key

- `tools/state_store.py` — SQLite CRUD (auto-creates `data/job_agent.db`)
- `tools/llm_json.py` — JSON fence stripping / parsing
- `tools/text_sanitize.py` — code fence stripping
- `orchestrator.py --help` — CLI entry point
- `orchestrator.py gaps` — runs locally if jobs exist in DB but no skills need LLM backfill

### No linter or test framework configured

This codebase has no linter config (no `pyproject.toml`, no `ruff.toml`, no `flake8` config) and no automated tests. Import verification (`python -c "from agents.search_agent import SearchAgent; ..."`) is the closest equivalent to a lint check.
