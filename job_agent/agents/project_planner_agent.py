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
from anthropic import Anthropic
from tools.state_store import StateStore

client = Anthropic()

# ── Prompts ───────────────────────────────────────────────────────────────────

GAP_TRIAGE_SYSTEM = """You are a senior ML engineer and career coach.

Given a candidate's skill profile and a list of skill gaps from job postings,
reason through each gap openly — then classify and rank them.

For each gap, consider:
- Is this best addressed by a portfolio project, or by something else
  (certification, open-source contribution, coursework, on-the-job)?
- How visible is this skill on a resume vs. how demonstrable in a project?
- How long would a credible project realistically take?
- Does the candidate's existing stack give them a head start?

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
      "candidate_head_start": "what existing skills reduce the ramp-up time"
    }
  ]
}

Sort gaps by priority DESC, then frequency DESC. Include all gaps passed in.
Return only JSON, no markdown fences.
"""

OPTIONS_SYSTEM = """You are a senior ML engineer helping design portfolio projects.

Given a skill gap and a candidate's existing stack, generate 3 distinct project options.
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
"""


class ProjectPlannerAgent:
    def __init__(self, resume: dict):
        self.resume = resume
        self.store = StateStore()
        self.candidate_skills = self._extract_skills()

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
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=GAP_TRIAGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )

        result = json.loads(_clean_json(response.content[0].text))

        # Surface reasoning
        print(f"\n  💭 Planner reasoning:\n")
        for line in result["reasoning"].split(". "):
            if line.strip():
                print(f"     {line.strip()}.")
        print()

        # Print triage table
        print(f"  {'SKILL':<30} {'ACTION':<18} {'PRIORITY':<10} PROJECT?")
        print(f"  {'-'*30} {'-'*18} {'-'*10} {'-'*8}")
        for gap in result["gaps"]:
            marker = "✅" if gap["project_worthy"] else "⬜"
            print(
                f"  {gap['skill']:<30} {gap['recommended_action']:<18} "
                f"{gap['priority']:<10} {marker}"
            )

        print()
        project_gaps = [g for g in result["gaps"] if g["project_worthy"]]
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
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=OPTIONS_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        options = json.loads(_clean_json(response.content[0].text))

        # Print options with tradeoffs
        for i, opt in enumerate(options, 1):
            print(f"\n  Option {i}: {opt['title']}")
            print(f"  {'─'*56}")
            print(f"  {opt['elevator_pitch']}")
            print(f"\n  Stack:    {', '.join(opt['stack'])}")
            print(f"  Hours:    ~{opt['estimated_hours']}h  |  "
                  f"Difficulty: {opt['difficulty']}  |  "
                  f"Novelty: {opt['novelty']}  |  "
                  f"Compute: {opt['compute_cost']}")
            print(f"\n  ⚠️  Tradeoffs: {opt['tradeoffs']}")
            print(f"\n  📝 Resume bullet preview:")
            print(f"     {opt['resume_bullet_preview']}")

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
        print(f"\n  📋 Building brief for: {option['title']}...")

        prompt = f"""Skill to demonstrate: {gap['skill']}
Chosen option:
{json.dumps(option, indent=2)}

Candidate's existing skills (use as building blocks):
{json.dumps(self.candidate_skills)}
"""
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system=BRIEF_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        brief = json.loads(_clean_json(response.content[0].text))

        # Print summary
        print(f"\n  ✅ Brief ready: {brief['title']}")
        print(f"     Goal: {brief['goal']}")
        print(f"     Stack: {', '.join(brief['stack']['core'])}")
        print(f"     Milestones: {len(brief['milestones'])} weeks")
        print(f"     Estimated: ~{brief['estimated_hours']}h")
        print(f"\n  → Pass this brief to ProjectBuilderAgent to scaffold the repo\n")

        return brief


def _clean_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()
