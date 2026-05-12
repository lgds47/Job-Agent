---
name: cloud-agent-starter
description: Use when starting work in this repository on Cursor Cloud and you need immediate setup, execution, and testing commands by codebase area.
---

# Cloud Agent Starter

## 1) First 2 minutes (always run)

1. `cd /workspace/job_agent && source .venv/bin/activate`
2. `python --version` (expect 3.10+)
3. Auth/login check: `python -c "import os; print(bool(os.getenv('ANTHROPIC_API_KEY')))"`  
   - `True`: full pipeline commands are available.
   - `False`: run local-only workflows below (no Claude calls).
4. Resume presence check: `python -c "from pathlib import Path; print(Path('data/luke_ganalon_resume.json').exists())"` (must be `True` for agent flows)
5. CLI boot check: `python orchestrator.py --help`

## 2) Runtime switches (practical "feature flags")

- `python orchestrator.py search --company "Glean" "Cohere"`: inject ad-hoc companies.
- `python orchestrator.py gaps --build`: toggles project scaffold generation.
- No dedicated env feature-flag system exists today; use CLI switches above as execution toggles.

## 3) Testing workflows by codebase area

### A. CLI orchestration (`orchestrator.py`)

- Smoke test: `python orchestrator.py --help`
- Parse + subcommand wiring test: run one command path (`search`, `apply --url`, or `gaps`) and confirm command-specific banner prints.

### B. Search/discovery + JD parsing (`agents/search_agent.py`, `tools/jd_parser.py`, `tools/job_skills.py`)

- Preferred end-to-end test (requires key):  
  `python orchestrator.py apply --url "https://boards.greenhouse.io/anthropic/jobs/12345"`
- Reason: `apply` is more reliable than `search` for Cloud smoke tests and exercises parser + parallel agent execution.
- If key missing: run import/syntax check only:  
  `python -c "from agents.search_agent import SearchAgent; from tools.jd_parser import parse_jd; from tools.job_skills import enrich_jobs_skill_lists; print('ok')"`

### C. Application generation (`agents/resume_agent.py`, `agents/cover_letter_agent.py`)

- Run `apply` (same command above), then verify outputs in newest folder under `data/applications/`:
  - `jd.json`
  - `tailored_resume.json`
  - `cover_letter.md`
  - `meta.json`
- Quick DB check:  
  `python -c "from tools.state_store import StateStore; print(StateStore().get_applications()[:1])"`

### D. Local state + utility tools (`tools/state_store.py`, `tools/llm_json.py`, `tools/text_sanitize.py`)

- Local-only verification (no API key needed):  
  `python -c "from tools.state_store import StateStore; from tools.llm_json import loads_llm_json; from tools.text_sanitize import strip_code_fences; print(StateStore().summary()); print(loads_llm_json('{\"x\":1}')); print(strip_code_fences('```json\\n{}\\n```'))"`

## 4) Updating this skill when new runbook knowledge appears

When you discover a new trick, add it under the relevant area section in this format:

- **Trigger:** what failed or was slow
- **Command:** exact command that fixed/verified it
- **Expected result:** short success signal

Keep additions minimal and Cloud-first: prefer reproducible commands over long narrative.
