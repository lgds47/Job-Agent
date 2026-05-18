"""
Project Planner Agent
=====================
Sits between gap analysis and project execution. Responsible for:

  1. ANALYZE  — Takes raw skill gaps from job postings, reasons openly about
                which gaps are worth closing via a project vs. other means
                (e.g. certifications, contributions, coursework).

  2. SELECT   — For projectworthy gaps, generates multiple candidate project
                options with tradeoffs. Communicates its reasoning so you can
                redirect before anything gets built.

  3. PLAN     — For the selected project, produces a structured ProjectBrief
                (JSON) covering goal, stack, dataset, milestones, and
                the resume bullet it will generate.

This agent is verbose by design — it surfaces its thinking at each step
so you stay in control of the decision. The ProjectBuilderAgent consumes
the approved ProjectBrief and handles execution.

Usage:
  planner = ProjectPlannerAgent(resume=resume)

  # Step 1: Get reasoning + ranked gaps
  analysis = await planner.analyze(gaps=gaps)
  # → prints reasoning, returns list of PlannerGap objects

  # Step 2: Get project options for a specific gap
  options = await planner.generate_options(gap=analysis[0])
  # → prints options + tradeoffs, returns list of ProjectOption objects

  # Step 3: Produce a full brief for a chosen option
  brief = await planner.build_brief(option=options[0], gap=analysis[0])
  # → returns ProjectBrief dict (feed this to ProjectBuilderAgent)
"""

import json
from anthropic import AsyncAnthropic

from tools.api_errors import counts_as_claude_failure
from tools.llm_json import loads_llm_json
from tools.state_store import StateStore

client = AsyncAnthropic()

# ── Prompts ───────────────────────────────────────────────────────────────────

GAP_TRIAGE_SYSTEM = """You are a senior ML engineer and career coach.

Given a candidate's skill profile and a list of skill gaps from job postings,
reason through each gap openly — then classify and rank them.

Candidate context (use this to calibrate gap severity accurately):
- Current level: Associate ML Engineer, ~1-2 years production experience
- Background: Applied consulting (Trace3) — shipped production ML systems
  for real clients, not research projects
- Deployment experience: Kubernetes, Docker, PyTorch in production CV systems
- Do NOT flag a skill as missing if it appears in the candidate's skill list —
  treat it as present even if the JD frames it differently
- Distinguish between truly missing skills vs. skills the candidate has but
  hasn't demonstrated publicly (the latter may only need a portfolio project,
  not a full learning ramp)

For each gap, consider:
- Is this best addressed by a portfolio project, or by something else
  (certification, open-source contribution, coursework, on-the-job)?
- How visible is this skill on a resume vs. how demonstrable in a project?
- How long would a credible project realistically take given this candidate's
  existing stack?
- Does a project here risk looking contrived vs. genuinely interesting work?

Be direct about your reasoning. Hiring managers can tell when a project
was built just to fill a gap vs. when someone genuinely engaged with a problem.

Return a JSON object with two keys:
{
  "reasoning": "Your open narrative reasoning — 3-5 sentences covering the
                overall pattern you see and your key decision factors",
  "gaps": [
    {
      "skill": "skill name",
      "frequency": <int>,
      "gap_level": "missing | weak | needs_recency",
      "recommended_action": "project | certification | contribution | coursework | on-the-job",
      "action_rationale": "one sentence why this action is best for this skill",
      "project_worthy": true | false,
      "priority": "high | medium | low",
      "candidate_head_start": "specific existing skills or experience that reduce
                               ramp-up — be concrete, not generic"
    }
  ]
}

Sort gaps by priority DESC, then frequency DESC. Include all gaps passed in.
Return only JSON, no markdown fences.
"""

OPTIONS_SYSTEM = """You are a senior ML engineer helping design portfolio projects.

Given a skill gap and a candidate's existing stack, generate 3 distinct project options.

Candidate positioning context:
- Production deployment background (Kubernetes, Docker, real inference pipelines)
- Applied ML, not research — projects should reflect this where possible
- Projects that include a serving layer, API, or real inference endpoint are
  more credible than notebooks alone for this candidate's profile
- Avoid options that are saturated tutorial rehashes — the candidate needs
  work that reads as genuine problem engagement, not gap-filling

Each option should:
- Be meaningfully different (not just the same idea with different datasets)
- Be completable solo in the estimated time range
- Produce something concrete and linkable (GitHub repo, demo, writeup)
- Naturally demonstrate the target skill to a technical hiring manager

For each option, be honest about tradeoffs — difficulty, time, novelty,
whether it's been done to death, compute cost, etc.

Return ONLY a JSON array, no markdown:
[
  {
    "id": "option_1",
    "title": "Short descriptive title",
    "elevator_pitch": "One sentence what this is and why it's interesting",
    "approach": "2-3 sentences on the technical approach",
    "dataset": {
      "name": "dataset name",
      "url": "link if known",
      "size_estimate": "e.g. '5GB', '100k rows'"
    },
    "stack": ["list", "of", "specific", "tools"],
    "estimated_hours": <int>,
    "difficulty": "beginner | intermediate | advanced",
    "novelty": "saturated | common | fresh",
    "compute_cost": "free | low | medium | high",
    "tradeoffs": "honest assessment — what's hard, what's been done before, what might go wrong",
    "resume_bullet_preview": "Draft of how this would appear as a resume bullet"
  }
]
"""

BRIEF_SYSTEM = """You are a senior ML engineer writing a complete project brief
for a junior engineer to execute.

The brief should be detailed enough that the engineer knows exactly what to build,
but not so prescriptive that it removes learning. Explain the WHY behind
architectural choices, not just the WHAT.

Return ONLY a JSON object, no markdown fences:
{
  "title": "Project title",
  "skill_demonstrated": "primary skill this proves",
  "goal": "One sentence project goal",
  "motivation": "Why this problem? Why this approach? 2-3 sentences.",
  "dataset": {
    "name": "dataset name",
    "url": "link",
    "download_instructions": "how to get it",
    "size_estimate": "size"
  },
  "architecture": {
    "description": "Technical approach in plain language",
    "key_components": ["component 1", "component 2"],
    "why_this_approach": "Why this architecture over alternatives"
  },
  "stack": {
    "core": ["required tools"],
    "optional": ["nice to have"]
  },
  "milestones": [
    {
      "week": 1,
      "goal": "what you should have working by end of week",
      "deliverable": "concrete artifact (notebook, script, checkpoint)"
    }
  ],
  "repo_structure": {
    "directories": ["src/", "data/", "notebooks/", "configs/", "tests/"],
    "key_files": ["README.md", "requirements.txt", "src/train.py"],
    "readme_sections": ["Overview", "Setup", "Usage", "Results", "Next Steps"]
  },
  "success_criteria": [
    "Specific, measurable outcomes that make this project credible"
  ],
  "common_pitfalls": [
    "Things that commonly go wrong and how to avoid them"
  ],
  "resume_bullet": "Final polished resume bullet — specific numbers where estimable",
  "estimated_hours": <int>,
  "difficulty": "beginner | intermediate | advanced"
}

The resume_bullet must:
- Start with a strong action verb (Built, Deployed, Developed, Implemented)
- Include at least one concrete metric or scale indicator where estimable
- Name the specific tools used
- End with the business or technical outcome
- Match the style: "Deployed X using Y to achieve Z" not "Worked on X"
"""


class ProjectPlannerAgent:
    def __init__(self, resume: dict, store: StateStore | None = None):
        self.resume = resume
        self.store = store
        self.candidate_skills = self._extract_skills()
        self.claude_failures = 0

    def _record_claude_failure(self):
        self.claude_failures += 1

    def _extract_skills(self) -> list[str]:
        return [
            item
            for cat in self.resume["skills"].values()
            for item in cat["items"]
        ]

    # ── Step 1: Analyze and triage gaps ──────────────────────────────────────

    async def analyze(self, gaps: list[dict]) -> list[dict]:
        """
        Reason through gaps openly. Classify each as project-worthy or not.
        Prints the agent's reasoning so you can course-correct before selecting.

        Returns list of enriched gap dicts (only project-worthy ones flagged).
        """
        print("\n" + "═" * 60)
        print("  PROJECT PLANNER — Gap Analysis")
        print("═" * 60)

        prompt = f"""Candidate's existing skills:
{json.dumps(self.candidate_skills, indent=2)}

Skill gaps from job postings:
{json.dumps(gaps, indent=2)}
"""
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=8000,
                system=GAP_TRIAGE_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
        except Exception as e:
            if counts_as_claude_failure(e):
                self._record_claude_failure()
            raise RuntimeError(f"ProjectPlannerAgent analyze call failed: {e}") from e

        try:
            result = loads_llm_json(response.content[0].text)
        except ValueError as e:
            self._record_claude_failure()
            raise RuntimeError(f"ProjectPlannerAgent analyze failed: {e}") from e
        if not isinstance(result, dict) or "gaps" not in result or "reasoning" not in result:
            raise RuntimeError("ProjectPlannerAgent analyze: model JSON missing required keys.")
        if not isinstance(result["gaps"], list):
            raise RuntimeError("ProjectPlannerAgent analyze: model JSON 'gaps' is not an array.")

        # Surface reasoning
        print(f"\n  💭 Planner reasoning:\n")
        reasoning = result.get("reasoning") or ""
        if not isinstance(reasoning, str):
            reasoning = str(reasoning)
        for line in reasoning.split(". "):
            if line.strip():
                print(f"     {line.strip()}.")
        print()

        # Print triage table
        print(f"  {'SKILL':<30} {'ACTION':<18} {'PRIORITY':<10} PROJECT?")
        print(f"  {'-'*30} {'-'*18} {'-'*10} {'-'*8}")
        for gap in result["gaps"]:
            marker = "✅" if gap.get("project_worthy") else "⬜"
            print(
                f"  {str(gap.get('skill', '')):<30} {str(gap.get('recommended_action', '')):<18} "
                f"{str(gap.get('priority', '')):<10} {marker}"
            )

        print()
        project_gaps = [g for g in result["gaps"] if g.get("project_worthy")]
        print(f"  → {len(project_gaps)} gap(s) recommended for portfolio projects\n")

        return result["gaps"]

    # ── Step 2: Generate project options ─────────────────────────────────────

    async def generate_options(self, gap: dict) -> list[dict]:
        """
        For a single gap, generate 3 distinct project options with tradeoffs.
        Prints options clearly so you can choose (or redirect) before briefing.

        Returns list of ProjectOption dicts.
        """
        print("\n" + "═" * 60)
        print(f"  PROJECT OPTIONS — {gap['skill'].upper()}")
        print("═" * 60)

        prompt = f"""Skill to demonstrate: {gap['skill']}
Gap level: {gap['gap_level']}
Candidate head start: {gap.get('candidate_head_start', 'none noted')}
Candidate's existing stack: {json.dumps(self.candidate_skills)}
"""
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=8000,
                system=OPTIONS_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
        except Exception as e:
            if counts_as_claude_failure(e):
                self._record_claude_failure()
            raise RuntimeError(f"ProjectPlannerAgent generate_options call failed: {e}") from e
        try:
            options = loads_llm_json(response.content[0].text)
        except ValueError as e:
            self._record_claude_failure()
            raise RuntimeError(f"ProjectPlannerAgent generate_options failed: {e}") from e
        if not isinstance(options, list) or not options:
            raise RuntimeError("ProjectPlannerAgent generate_options: expected a non-empty JSON array.")

        if self.store is not None:
            saved = self.store.save_project_ideas(gap=gap, options=options, run_source="gaps")
            print(f"\n  💾 Persisted {saved} project idea(s) for future runs.")

        # Print options with tradeoffs
        for i, opt in enumerate(options, 1):
            if not isinstance(opt, dict):
                print(f"\n  Option {i}: <invalid JSON object — skipping>")
                continue
            title = opt.get("title", "Untitled")
            pitch = opt.get("elevator_pitch", "")
            stack = opt.get("stack") or []
            hours = opt.get("estimated_hours", "?")
            difficulty = opt.get("difficulty", "?")
            novelty = opt.get("novelty", "?")
            compute = opt.get("compute_cost", "?")
            tradeoffs = opt.get("tradeoffs", "")
            preview = opt.get("resume_bullet_preview", "")
            print(f"\n  Option {i}: {title}")
            print(f"  {'─'*56}")
            print(f"  {pitch}")
            print(f"\n  Stack:    {', '.join(stack)}")
            print(f"  Hours:    ~{hours}h  |  "
                  f"Difficulty: {difficulty}  |  "
                  f"Novelty: {novelty}  |  "
                  f"Compute: {compute}")
            print(f"\n  ⚠️  Tradeoffs: {tradeoffs}")
            print(f"\n  📝 Resume bullet preview:")
            print(f"     {preview}")

        print(f"\n  To proceed: call build_brief(option=options[N], gap=gap)")
        print(f"  To redirect: describe what you want differently\n")

        return options

    # ── Step 3: Build full project brief ─────────────────────────────────────

    async def build_brief(self, option: dict, gap: dict) -> dict:
        """
        Produce a full structured ProjectBrief for the chosen option.
        This is the artifact consumed by ProjectBuilderAgent.

        Returns a ProjectBrief dict.
        """
        print(f"\n  📋 Building brief for: {option.get('title', '—')}...")

        prompt = f"""Skill to demonstrate: {gap['skill']}
Chosen option:
{json.dumps(option, indent=2)}

Candidate's existing skills (use as building blocks):
{json.dumps(self.candidate_skills)}
"""
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=8000,
                system=BRIEF_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
        except Exception as e:
            if counts_as_claude_failure(e):
                self._record_claude_failure()
            raise RuntimeError(f"ProjectPlannerAgent build_brief call failed: {e}") from e
        try:
            brief = loads_llm_json(response.content[0].text)
        except ValueError as e:
            self._record_claude_failure()
            raise RuntimeError(f"ProjectPlannerAgent build_brief failed: {e}") from e
        if not isinstance(brief, dict):
            raise RuntimeError("ProjectPlannerAgent build_brief: model did not return a JSON object.")

        # Print summary
        core_stack = (brief.get("stack") or {}).get("core") or []
        milestones = brief.get("milestones") or []
        print(f"\n  ✅ Brief ready: {brief.get('title', '—')}")
        print(f"     Goal: {brief.get('goal', '—')}")
        print(f"     Stack: {', '.join(core_stack) if core_stack else '—'}")
        print(f"     Milestones: {len(milestones)} weeks")
        print(f"     Estimated: ~{brief.get('estimated_hours', '?')}h")
        print(f"\n  → Pass this brief to ProjectBuilderAgent to scaffold the repo\n")

        return brief

    def pick_idea_for_builder(self) -> dict | None:
        """
        Return exactly one persisted idea for the next builder handoff.
        Preference: ideas not yet started.
        """
        if self.store is None:
            return None
        return self.store.pick_next_project_idea()

    def peek_idea_for_builder(self) -> dict | None:
        """Preview the next builder idea without mutating queue state."""
        if self.store is None:
            return None
        return self.store.peek_next_project_idea()
