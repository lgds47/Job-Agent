# AGENTS.md

## Cursor Cloud specific instructions

### Overview

Python CLI application — a multi-agent job search pipeline built on the Anthropic Claude API. No web server, no Docker, no frontend. Entry point is `job_agent/orchestrator.py` with subcommands: `search`, `apply`, `gaps`, `status`, `doctor`.

### Running the app

All commands must be run from inside `job_agent/` with the virtualenv activated:

```bash
cd /workspace/job_agent
source .venv/bin/activate
python orchestrator.py search                                    # discover + score jobs
python orchestrator.py apply --url URL                           # generate application package
python orchestrator.py gaps                                      # skill gap analysis
python orchestrator.py gaps --build                              # gap analysis + project scaffold
python orchestrator.py status                                    # read-only dashboard (text)
python orchestrator.py status --format json                      # machine-readable export
python orchestrator.py status --format html -o /tmp/status.html  # standalone HTML export
python orchestrator.py status --format text -o /tmp/status.txt   # text export to file
python orchestrator.py doctor                                  # env + resume + API probe
python orchestrator.py doctor --skip-api                       # offline checks only
```

### Apply URLs (ATS)

`apply` resolves Greenhouse, Lever, and Ashby job URLs via public ATS APIs (same sources as `SearchAgent`) before falling back to HTML scrape. Prefer live `absolute_url` values such as `https://job-boards.greenhouse.io/{board}/jobs/{id}`. Legacy `boards.greenhouse.io/.../jobs/{id}` links may 404 or show `?error=true` — failures surface as `job_not_found`, not `jd_parse_failed`.

Failed runs persist structured metadata: `error_type`, `user_message`, optional `http_status` / `request_id`. `claude_failures` increments only for invalid model JSON, not billing/auth/rate-limit errors.

### Thresholds (single source of truth)

- **Job targeting threshold: `50`** — `SearchAgent.run(min_score=50)` gates which scored postings are returned to the orchestrator and persisted as "qualified".
- **Skill gap analysis threshold: `35`** — `orchestrator.run_gaps` pulls recent jobs at `min_score=35` (`GAP_MIN_SCORE`) so gap analysis sees a broader sample of postings than the strict apply-now threshold. Surfaces gaps that show up in partial matches as well as strong ones.
- **Search early-exit threshold: `7` consecutive scores below `50`** — `EARLY_EXIT_CONSECUTIVE_LOW = 7` and `EARLY_EXIT_SCORE_THRESHOLD = 50` in `agents/search_agent.py`. Both are module-level constants AND optional `SearchAgent(consecutive_low_limit=..., low_score_threshold=...)` constructor arguments so they can be overridden per-instance without monkey-patching.

### SearchAgent — early-exit guardrail

`agents/search_agent.py` runs scoring with a failure-aware streak check. If `EARLY_EXIT_CONSECUTIVE_LOW` (default `7`) consecutive postings score below `EARLY_EXIT_SCORE_THRESHOLD` (default `50`), the agent halts the run and stops fetching/scoring additional listings.

Scoring failures (Claude API errors or invalid JSON) are tracked separately via `claude_failures` and do **not** advance the low-signal streak counter — that prevents API outages from masquerading as weak postings.

The agent surfaces a per-run `last_run_stats` dict that the orchestrator persists into `run_history.metadata_json`:

  - `companies_discovered`, `companies_processed`
  - `raw_postings`, `postings_after_role_filter`
  - `jobs_scored`, `qualified_jobs`
  - `early_exit_triggered` (bool)
  - `low_score_threshold`, `consecutive_low_score_limit`
  - `claude_failures`

Scoring is parallelised in fixed-size batches (`SCORING_BATCH_SIZE = 5`), intentionally decoupled from `EARLY_EXIT_CONSECUTIVE_LOW` so concurrency stays predictable when the streak threshold is tuned.

### ProjectBuilderAgent — finish-first

The builder writes a `meta.json` at the root of each scaffolded project under `data/projects/{slug}/`, with `status: "in_progress"` initially and `status: "completed"` only after every required artifact is on disk. The required source files are declared up front as `REQUIRED_SOURCE_FILES` (each entry pairs a path with its file-role description used by the code generator).

On every subsequent `build()` call the builder scans `data/projects/` first. A project counts as complete only if **either** signal is present:

  - a `completed.json` sentinel file exists at the project root, **OR**
  - `meta.json` `status` equals `"completed"`.

If any subdirectory fails both checks, the builder refines the most recently modified incomplete project (regenerating missing required files) instead of scaffolding the new brief. Refinement always finishes by writing `status: "completed"` to `meta.json` — including the no-brief and corrupt-brief shortcuts, which add a `refinement_note` explaining why nothing was regenerated. Only when no incomplete project exists does the builder accept a new brief from the planner.

`builder.last_action` is set to `"scaffolded"` or `"refined"` on every call, and `builder.last_build_info` exposes `{mode, project_dir, idea_key}` to the orchestrator so it can update the queued planner idea correctly.

### ProjectPlannerAgent — pass-one-per-run

The planner still produces a full list of project options (one set per top gap), but the orchestrator persists every option to the `project_ideas` table in `data/job_agent.db` and passes **exactly one** idea to the builder per `gaps --build` run. Selection (`pick_next_project_idea`) is FIFO within each status group: `not_started` first, then `in_progress`, oldest `created_at` ASC within each. `peek_next_project_idea` is a genuinely read-only preview — no rows are mutated. `pick_next_project_idea` increments `selected_count` and stamps `last_selected_at`/`updated_at` on the picked row.

The chosen idea is marked `in_progress` while the builder runs. If the builder scaffolded a new project it is moved to `completed`; if the builder refined an older incomplete project instead, the original idea stays `in_progress` (its `project_dir` is updated to point at the refined project).

### Status dashboard

`python orchestrator.py status` is a read-only dashboard (no API key required) implemented as a self-contained module at `tools/status_report.py`. It reports:

  - jobs in `job_agent.db` with scores and metadata
  - generated application directories under `data/applications/` (with per-file presence checks against `APP_REQUIRED_FILES`)
  - all stored project ideas with their DB status and on-disk completion state
  - projects on disk with completion state (uses the same dual-signal check as the builder)
  - a `run_history` summary showing what ran, how many jobs were scored, how many early exits triggered, Claude failures, and per-command totals

Supported `--format` flags: `text` (default), `json`, `html`. Combine with `--output PATH` / `-o PATH` to write the report to a file instead of stdout.

### Run history accounting

`StateStore.record_run` takes explicit `started_at` and `finished_at` ISO-timestamp parameters. The orchestrator captures `started_at` at the top of each command handler and `finished_at` immediately before persisting — `record_run` no longer generates both from a single `datetime.now()` call, so the row reflects real wall-clock duration. (If a caller omits the arguments, `_now_iso()` is used as a fallback, but every orchestrator path passes both explicitly.)

### Key environment requirement

`ANTHROPIC_API_KEY` must be set (env var or `.env` file in `job_agent/`). Every agent command calls Claude (`claude-sonnet-4-5`). Without the key, all agent commands fail at the first LLM call. `status` is the one exception — it is fully offline and never calls Claude.

### Resume JSON

A structured resume file at `job_agent/data/luke_ganalon_resume.json` is required. It is gitignored (contains PII). The full schema must include: `contact` (`name`, `title`), `summary` (`text` — required by both ResumeAgent and CoverLetterAgent), `agent_metadata` (`target_roles` list), `skills` (dict of category → `{label, items}`), `experience` (list with `bullets` containing `id`, `text`, `skills`), `education` (list with `institution`, `degree`, `field`), `certifications` (list with `name`). Bullets must have unique `id` fields (e.g. `b_001_1`) for the ResumeAgent bullet ranking to work.

### Gotchas

- The `search` command's discovery step requests 15-20 companies. The discovery Claude call uses `max_tokens=8000` (in `agents/search_agent.py::_discover_companies`), comfortably above the ~6 KB JSON that prompt empirically produces. Earlier the cap was `2000` and the JSON would truncate mid-string — if you ever see `❌ Failed to parse company discovery JSON` again, the first thing to check is whether that cap has been lowered back.
- `search --company` merges ad-hoc companies *after* `_discover_companies` returns. If discovery itself returns `[]` for any reason (Claude error, parse failure), the entire run aborts before the ad-hoc list is processed — the override does not bypass discovery.
- The `apply` command is the best end-to-end test: it exercises the JD parser (real HTTP fetch), ResumeAgent (bullet ranking + summary rewrite), and CoverLetterAgent (generation) in parallel, with no discovery dependency.

### Local components that work without API key

- `tools/state_store.py` — SQLite CRUD (auto-creates `data/job_agent.db`)
- `tools/status_report.py` — dashboard collector/formatters (text/json/html)
- `tools/llm_json.py` — JSON fence stripping / parsing
- `tools/text_sanitize.py` — code fence stripping
- `orchestrator.py --help` — CLI entry point
- `orchestrator.py status` — fully offline dashboard over the SQLite store and on-disk artifacts
- `orchestrator.py doctor` — offline env/resume checks; optional API probe via `count_tokens`
- `orchestrator.py gaps` — runs locally if jobs exist in DB but no skills need LLM backfill

### No linter or test framework configured

This codebase has no linter config (no `pyproject.toml`, no `ruff.toml`, no `flake8` config) and no automated tests. Import verification (`python -c "from agents.search_agent import SearchAgent; ..."`) is the closest equivalent to a lint check.

### Starter skill

A hands-on runbook for Cloud agents is at `skills/running-and-testing-job-agent/SKILL.md`. Read it before running any commands or debugging a failure — it covers environment setup, per-area testing workflows, common failure modes, and instructions for keeping the skill up to date.
