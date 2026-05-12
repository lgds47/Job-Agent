---
name: running-job-agent
description: Runbook for running/testing the job agent CLI in Cursor Cloud
---

# Running and Testing the Job Agent

## SearchAgent

- Search includes a rolling quality guardrail.
- Defaults in `agents/search_agent.py`:
  - `LOW_SCORE_THRESHOLD = 50`
  - `CONSECUTIVE_LOW_SCORE_LIMIT = 3`
- If 3 consecutive scored jobs are below 50, SearchAgent logs a warning and halts further scoring for that run.

## ProjectPlannerAgent

- Planner still generates the full list of project ideas and persists all ideas.
- It passes one idea to builder per run, preferring ideas not yet started.

## ProjectBuilderAgent

- Builder checks `data/projects/` before creating a new scaffold.
- If any project is incomplete (no `completed.json` and `meta.json` status is not `completed`), it selects the most recent incomplete project and refines that project.
- Only when there are no incomplete projects will it scaffold a new one.

## CLI Table

| Command | What it does |
|---|---|
| `python orchestrator.py search` | Discover + score jobs with low-signal early-exit guardrail |
| `python orchestrator.py apply --url "<url>"` | Generate application package |
| `python orchestrator.py gaps` | Analyze gaps, persist all project ideas, select one for next builder handoff |
| `python orchestrator.py gaps --build` | Build/refine one selected idea |
| `python orchestrator.py status` | Read-only dashboard of jobs, artifacts, ideas, run history |
| `python orchestrator.py status --format json` | JSON export of dashboard |
| `python orchestrator.py status --format html` | HTML export of dashboard |

## Decision: Which Test to Run

| Change area | Run this |
|---|---|
| SearchAgent scoring behavior | `python orchestrator.py search` |
| Planner/builder handoff behavior | `python orchestrator.py gaps` and `python orchestrator.py gaps --build` |
| Status dashboard output | `python orchestrator.py status --format text` and `python orchestrator.py status --format json` |
| StateStore-only changes | `python tools/state_store.py` |
