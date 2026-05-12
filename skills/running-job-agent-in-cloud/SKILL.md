---
name: running-job-agent-in-cloud
description: Use when a Cursor Cloud agent needs to set up, run, test, or troubleshoot this Python job search agent repository.
---

# Running Job Agent in Cloud

## Overview

This repo is a Python 3.10+ CLI app, not a web server. Always run commands from `/workspace/job_agent` with the virtualenv activated because paths such as `data/luke_ganalon_resume.json`, `.env`, and `data/job_agent.db` are relative to that directory.

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

Never commit `.env`, `.github_token`, `*.db`, generated applications, or `data/luke_ganalon_resume.json`; they contain secrets, state, or PII. The resume and DB are ignored, but `job_agent/data/applications/` may still appear in `git status`, so remove or leave it unstaged unless the user explicitly asks to keep outputs. Full LLM runs also require a user-supplied `data/luke_ganalon_resume.json` with `contact`, `summary.text`, `agent_metadata.target_roles`, `skills`, `experience[].bullets[].id`, `education`, and `certifications`.

## Feature flags and configuration

There are no feature-flag services to log into or mock. Core orchestrator behavior is controlled by `ANTHROPIC_API_KEY`, the resume JSON, and CLI flags:

| Area | Command flags |
|---|---|
| Search | `search --company "Name"` adds ad-hoc companies after discovery succeeds |
| Apply | `apply --url URL` runs the reliable end-to-end application flow |
| Gaps | `gaps --build` scaffolds the top portfolio project option |

Do not edit source to fake flags. For API-free checks, test local modules directly or set `ANTHROPIC_API_KEY=dummy` only for import smoke tests that do not call Claude.

`push_github.py` is outside the core orchestrator flow; it separately reads `GITHUB_TOKEN` or `.github_token` only when that script is run, and its upload map includes the resume JSON. Do not run it unless the user explicitly wants that sync and understands the PII exposure.

## Testing workflows by codebase area

| Area | Fast check | End-to-end check |
|---|---|---|
| CLI/orchestrator | `python orchestrator.py --help` | With key + resume, `python orchestrator.py apply --url "<real job URL>"` and verify `data/applications/YYYYMMDD_company_title_slug/{jd.json,tailored_resume.json,cover_letter.md,meta.json}` |
| Search agent | `python orchestrator.py search --help` | `python orchestrator.py search`; `agent_metadata.target_roles` must be non-empty or it exits early. Expect Claude discovery, ATS fetches, scoring, and SQLite job rows. If discovery JSON truncates, prefer `apply --url` for E2E validation |
| Gap planner/builder | `python orchestrator.py gaps --help`; without the gitignored resume JSON, `gaps` fails before DB checks | With resume present, `python orchestrator.py gaps` prints "No jobs in state store yet" when `get_recent_jobs(n=50, min_score=50)` returns empty: either no job rows exist or all scores are below 50. Inspect `StateStore().summary()` if unclear. With qualifying jobs, it may call Claude to backfill missing skill lists before planning |
| Local tools | Run `python -c "from tools.state_store import StateStore; print(StateStore().summary())"` | Exercise focused snippets for `tools.llm_json.loads_llm_json`, `tools.text_sanitize.strip_code_fences`, and SQLite CRUD; these do not need Claude |
| JD parser/LLM agents | Import with `ANTHROPIC_API_KEY=dummy python -c "from tools.jd_parser import parse_jd; from agents.resume_agent import ResumeAgent"` | Use `apply --url`; it fetches a real posting, parses it with Claude, runs resume and cover-letter agents, and saves outputs |

## Common Cloud workflow

1. `cd /workspace/job_agent && source .venv/bin/activate` for every run.
2. If `.venv` is missing, recreate it and install `requirements.txt`.
3. Confirm `ANTHROPIC_API_KEY` and `data/luke_ganalon_resume.json` exist before testing LLM flows or `gaps`.
4. Prefer `apply --url` for reliable full-pipeline testing, but expect real job URLs to fail when a board blocks HTTP fetches, returns thin HTML, or rate-limits.
5. Treat `search` as fragile: discovery can fail from model JSON truncation, and `search --company` still performs discovery first.
6. Treat generated `data/` outputs as test artifacts unless the user asks to preserve them.

## Updating this skill

When you discover a new setup trick, flaky command, missing dependency, API failure mode, or reliable test shortcut, update this skill in the same PR as the fix. Add only reusable runbook knowledge: the exact command, required cwd/env, expected success signal, and known failure message. Keep project-specific secrets, personal resume content, and one-off debugging notes out of the skill.
