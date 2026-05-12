---
name: cloud-agent-job-agent-runbook
description: Use when running or smoke-testing this repository from Cursor Cloud or other headless agents, especially when API keys, resume data, venv layout, or which commands need network access are unclear.
---

# Cloud agent runbook — job_agent

## Overview

Python CLI only (`job_agent/orchestrator.py`). **No web server, OAuth login, or in-code feature flags** — use CLI flags (`--build`, `--company`, `--url`) and env vars. Agents use `AsyncAnthropic()` → set **`ANTHROPIC_API_KEY`**. To narrow behavior, pick a subcommand (e.g. `apply` instead of `search`) or use local `python -c` checks for pure tools.

## First run

Work **inside** `job_agent/`; align details with `/workspace/AGENTS.md`.

```bash
cd /workspace/job_agent
test -d .venv || python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python orchestrator.py --help
```

| Item | Notes |
|------|--------|
| `ANTHROPIC_API_KEY` | Shell or `job_agent/.env` (`python-dotenv` when installed) |
| `GITHUB_TOKEN` / `.github_token` | Only `push_github.py` |
| `data/luke_ganalon_resume.json` | Gitignored; needs schema from `AGENTS.md` (`summary.text`, `agent_metadata.target_roles`, bullet `id`s) before `search` / `apply` |

---

## By codebase area

### `AGENTS.md`

Gotchas (discovery truncation, `search --company` still runs discovery), resume schema, no automated tests.

### `orchestrator.py` (CLI)

| Command | Key | Resume | Test |
|---------|-----|--------|------|
| default | no | no | `python orchestrator.py` → help |
| `search` | yes | yes | Full pipeline; if discovery JSON fails, use `apply` |
| `apply --url URL` | yes | yes | Main e2e: fetch JD + resume + cover letter |
| `gaps` | usually yes | yes | Needs jobs in `data/job_agent.db`; enrich + planner use API |
| `gaps --build` | yes | yes | Adds scaffold under `data/projects/` |

After edits: `--help`, then the smallest command that covers your change.

### `agents/`

```bash
cd /workspace/job_agent && source .venv/bin/activate
python -c "from agents.search_agent import SearchAgent; from agents.resume_agent import ResumeAgent; from agents.cover_letter_agent import CoverLetterAgent; from agents.project_planner_agent import ProjectPlannerAgent; from agents.project_builder_agent import ProjectBuilderAgent; print('imports ok')"
```

### `tools/`

| Module | Key? | Check |
|--------|------|--------|
| `state_store.py` | no | `python -c "from tools.state_store import StateStore; StateStore(); print('db')"` |
| `llm_json.py` | no | `python -c "from tools.llm_json import loads_llm_json; print(loads_llm_json('{\"x\":1}'))"` |
| `jd_parser.py`, `job_skills.py` | if LLM path runs | Prefer `apply --url` or ad-hoc snippets (no in-repo HTTP mocks) |

### `push_github.py`

`GITHUB_TOKEN` or `.github_token` (chmod 600); see script header. No Anthropic key.

---

## Updating this skill

Add new env vars, commands, or failure modes to the matching **table or subsection** with a **copy-paste command**. If `AGENTS.md` is the source of truth for a behavior, update there first, then one line here pointing agents to that section.
