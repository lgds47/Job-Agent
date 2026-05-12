---
name: running-job-agent-in-cloud
description: Use when a Cursor Cloud agent needs to set up, run, test, or troubleshoot this Python job search agent repository.
---

# Running Job Agent in Cloud

## Overview

This repo is a Python CLI app, not a web server. Always run commands from `/workspace/job_agent` with the virtualenv activated because paths such as `data/luke_ganalon_resume.json`, `.env`, and `data/job_agent.db` are relative to that directory.

## Setup and auth

```bash
cd /workspace/job_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

There is no browser login. Auth is the Anthropic API key:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# or create /workspace/job_agent/.env containing ANTHROPIC_API_KEY=...
```

Never commit `.env`, `.github_token`, `data/job_agent.db`, generated applications, or `data/luke_ganalon_resume.json`; they are ignored because they contain secrets, state, or PII. Full LLM runs also require `data/luke_ganalon_resume.json` with `contact`, `summary.text`, `agent_metadata.target_roles`, `skills`, `experience[].bullets[].id`, `education`, and `certifications`.

## Feature flags and configuration

There are no feature-flag services to log into or mock. The only runtime switches are CLI flags:

| Area | Command flags |
|---|---|
| Search | `search --company "Name"` adds ad-hoc companies after discovery succeeds |
| Apply | `apply --url URL` runs the reliable end-to-end application flow |
| Gaps | `gaps --build` scaffolds the top portfolio project option |

Do not edit source to fake flags. For API-free checks, test local modules directly or set `ANTHROPIC_API_KEY=dummy` only for import smoke tests that do not call Claude.

## Testing workflows by codebase area

| Area | Fast check | End-to-end check |
|---|---|---|
| CLI/orchestrator | `python orchestrator.py --help` | With key + resume, `python orchestrator.py apply --url "<real job URL>"` and verify `data/applications/YYYYMMDD_company_role/{jd.json,tailored_resume.json,cover_letter.md,meta.json}` |
| Search agent | `python orchestrator.py search --help` | `python orchestrator.py search`; expect Claude discovery, ATS fetches, scoring, and SQLite job rows. If discovery JSON truncates, prefer `apply --url` for E2E validation |
| Gap planner/builder | `python orchestrator.py gaps --help`; without the gitignored resume JSON, `gaps` fails before DB checks | With resume present and a fresh DB, `python orchestrator.py gaps` should print "No jobs in state store yet"; after search or seeded jobs, add `--build` only when project scaffolding should be created under `data/projects/` |
| Local tools | Run `python -c "from tools.state_store import StateStore; print(StateStore().summary())"` | Exercise focused snippets for `tools.llm_json.loads_llm_json`, `tools.text_sanitize.strip_code_fences`, and SQLite CRUD; these do not need Claude |
| JD parser/LLM agents | Import with `ANTHROPIC_API_KEY=dummy python -c "from tools.jd_parser import parse_jd; from agents.resume_agent import ResumeAgent"` | Use `apply --url`; it fetches a real posting, parses it with Claude, runs resume and cover-letter agents, and saves outputs |

## Common Cloud workflow

1. `cd /workspace/job_agent && source .venv/bin/activate` for every run.
2. If `.venv` is missing, recreate it and install `requirements.txt`.
3. Confirm `ANTHROPIC_API_KEY` and `data/luke_ganalon_resume.json` exist before testing LLM flows or `gaps`.
4. Prefer `apply --url` for reliable full-pipeline testing; `search` can fail from model JSON truncation, and `search --company` still performs discovery first.
5. Treat generated `data/` outputs as test artifacts unless the user asks to preserve them.

## Updating this skill

When you discover a new setup trick, flaky command, missing dependency, API failure mode, or reliable test shortcut, update this skill in the same PR as the fix. Add only reusable runbook knowledge: the exact command, required cwd/env, expected success signal, and known failure message. Keep project-specific secrets, personal resume content, and one-off debugging notes out of the skill.
