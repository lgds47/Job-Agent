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

### No linter or test framework configured

This codebase has no linter config (no `pyproject.toml`, no `ruff.toml`, no `flake8` config) and no automated tests. Import verification (`python -c "from agents.search_agent import SearchAgent; ..."`) is the closest equivalent to a lint check.
