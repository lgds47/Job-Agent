"""
Project Builder Agent
=====================
Receives an approved ProjectBrief from ProjectPlannerAgent and executes:

  1. SCAFFOLD  — Creates the full repo directory structure with all
                 starter files: train.py, config, README, requirements,
                 dataset download script, and milestone tracker.

  2. GENERATE  — For each key file, generates substantive starter code —
                 not empty stubs. Includes the architectural skeleton,
                 correct imports, and inline comments explaining the WHY.

  3. REPORT    — Prints a handoff summary: what was built, what to run
                 first, and where to pick up in week 1.

This agent writes actual files to data/projects/{project_slug}/.
It communicates what it's building as it goes.

Finish-first guardrail
----------------------
Before scaffolding a new project, ``build()`` scans the output directory
for any subdir that is **not yet marked completed**. If one is found, the
agent refines that project instead of starting a new one. A project is
considered complete if **either** of these signals is present:

  - a ``completed.json`` marker file at the project root, OR
  - a ``meta.json`` whose ``status`` field equals ``STATUS_COMPLETED``.

This dual check lets older projects (which only used the marker file) and
newer ones (which carry status in ``meta.json``) coexist without false
positives in either direction.

Usage:
  builder = ProjectBuilderAgent(resume=resume)
  await builder.build(brief=brief, output_dir=Path("data/projects"))
"""

import json
import asyncio
import hashlib
import re
from datetime import datetime
from pathlib import Path
from anthropic import AsyncAnthropic

from tools.text_sanitize import strip_code_fences

client = AsyncAnthropic()

# ── Completion signal constants ───────────────────────────────────────────────
# A project is considered "completed" if EITHER a sentinel completed.json
# file exists at its root, OR meta.json contains status == STATUS_COMPLETED.
# Anything else (missing meta.json, status "in_progress", unparseable, etc.)
# is treated as incomplete and will be refined on the next builder run
# before any new project is scaffolded.
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
META_FILENAME = "meta.json"
COMPLETED_MARKER_FILENAME = "completed.json"
PROJECT_BRIEF_FILENAME = "project_brief.json"

# Source files every scaffolded project is expected to ship with, each
# paired with the file-role description fed into the code generator.
REQUIRED_SOURCE_FILES: dict[str, str] = {
    "src/train.py":     "Main training loop. Loads data, initializes model, runs training, logs metrics.",
    "src/model.py":     "Model definition and architecture. Should be importable standalone.",
    "src/dataset.py":   "Dataset class and data loading utilities. Handle download, preprocessing, batching.",
    "src/evaluate.py":  "Evaluation script. Load a checkpoint and compute metrics on a test set.",
    "configs/config.yaml": "Training configuration: hyperparameters, paths, model settings.",
}

# ── Prompts ───────────────────────────────────────────────────────────────────

CODE_GEN_SYSTEM = """You are a senior ML engineer writing clean, well-commented
starter code for a junior engineer's portfolio project.

Rules:
- Write substantive code — not empty stubs. Include real imports, real structure.
- Add inline comments that explain WHY, not just what (the code shows what).
- Include TODO markers for the parts the engineer needs to implement themselves
  — this is a learning project, not a complete solution.
- Use best practices for the stack specified.
- Keep files focused — one responsibility per file.
- Return ONLY the file content, no preamble, no markdown fences.
"""

README_SYSTEM = """You are a technical writer creating a professional README
for a portfolio project. This README will be seen by ML hiring managers.

Rules:
- Lead with what the project demonstrates, not just what it does
- Include a results/metrics section (with placeholders if not yet run)
- Make setup genuinely easy to follow
- Include a "Key learnings" or "Technical decisions" section — this shows depth
- Keep it under 500 words
- Return only the README content, no preamble
"""


class ProjectBuilderAgent:
    def __init__(self, resume: dict):
        self.resume = resume
        # Set on every build() call to indicate what the agent did, so the
        # orchestrator can update planner-idea state accordingly.
        # One of: "scaffolded", "refined", None.
        self.last_action: str | None = None
        # Detailed handoff info for the orchestrator (mode, project_dir, idea_key).
        self.last_build_info: dict = {}

    # ── Completion-state helpers ──────────────────────────────────────────────

    @staticmethod
    def _read_meta(project_dir: Path) -> dict:
        meta_path = project_dir / META_FILENAME
        if not meta_path.exists():
            return {}
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    @classmethod
    def _is_project_completed(cls, project_dir: Path) -> bool:
        """Dual-signal completion check.

        A project counts as complete if EITHER the sentinel
        ``completed.json`` marker exists OR ``meta.json`` status is
        ``STATUS_COMPLETED``. This keeps both old-style (marker file only)
        and new-style (status in meta) projects compatible.
        """
        if (project_dir / COMPLETED_MARKER_FILENAME).exists():
            return True
        return cls._read_meta(project_dir).get("status") == STATUS_COMPLETED

    @classmethod
    def _find_incomplete_projects(cls, output_dir: Path) -> list[Path]:
        """Return all project subdirs in output_dir that lack a completion
        signal, sorted most-recently-modified first.
        """
        if not output_dir.exists():
            return []
        incomplete: list[Path] = []
        for child in output_dir.iterdir():
            if not child.is_dir():
                continue
            if not cls._is_project_completed(child):
                incomplete.append(child)
        incomplete.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return incomplete

    def _write_meta(self, project_dir: Path, **fields) -> None:
        meta = self._read_meta(project_dir)
        meta.update(fields)
        (project_dir / META_FILENAME).write_text(json.dumps(meta, indent=2))

    async def _generate_file(self, filepath: str, brief: dict, file_role: str) -> str:
        """Ask Claude to generate the content of a specific project file."""
        prompt = f"""Project: {brief['title']}
Goal: {brief['goal']}
Stack: {json.dumps(brief['stack'])}
Architecture: {json.dumps(brief['architecture'])}

Generate the file: {filepath}
File role: {file_role}

Dataset: {brief['dataset']['name']}
Key components: {json.dumps(brief['architecture']['key_components'])}
"""
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=CODE_GEN_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        return strip_code_fences(response.content[0].text)

    async def _generate_readme(self, brief: dict) -> str:
        """Generate the project README."""
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=README_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(brief, indent=2)}]
        )
        return strip_code_fences(response.content[0].text)

    async def _generate_requirements(self, brief: dict) -> str:
        """Generate requirements.txt from the brief's stack."""
        core = brief["stack"]["core"]
        optional = brief["stack"].get("optional", [])

        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            system="Return only a requirements.txt file content. Pin reasonable versions. No comments, no markdown.",
            messages=[{"role": "user", "content": f"Generate requirements.txt for: {core + optional}"}]
        )
        return strip_code_fences(response.content[0].text)

    def _generate_milestone_tracker(self, brief: dict) -> str:
        """Generate a markdown milestone tracker from the brief."""
        lines = [
            f"# {brief['title']} — Milestone Tracker\n",
            f"**Goal:** {brief['goal']}\n",
            f"**Estimated hours:** ~{brief['estimated_hours']}h\n\n",
            "---\n"
        ]
        for m in brief["milestones"]:
            lines.append(f"## Week {m['week']}: {m['goal']}\n")
            lines.append(f"**Deliverable:** {m['deliverable']}\n")
            lines.append(f"- [ ] In progress\n")
            lines.append(f"- [ ] Complete\n\n")

        lines.append("---\n\n")
        lines.append("## Success Criteria\n")
        for criterion in brief.get("success_criteria", []):
            lines.append(f"- [ ] {criterion}\n")

        lines.append("\n## Resume Bullet (finalize after completion)\n")
        lines.append(f"> {brief['resume_bullet']}\n")
        return "".join(lines)

    async def build(
        self,
        brief: dict,
        output_dir: Path = Path("data/projects"),
        idea_key: str | None = None,
    ) -> Path:
        """
        Build a portfolio project.

        Finish-first policy: before accepting ``brief``, scan ``output_dir``
        for any existing project that has not been marked completed (no
        ``completed.json`` marker AND meta.json status != "completed"). If
        one is found, refine the most-recent incomplete project instead of
        scaffolding a new one. Only when no incomplete project exists does
        the agent scaffold from the new brief.

        Creates (when scaffolding):
          data/projects/{slug}/
            README.md
            requirements.txt
            MILESTONES.md
            meta.json        ← completion signal (status: in_progress | completed)
            project_brief.json
            src/{train,model,dataset,evaluate}.py
            configs/config.yaml
            notebooks/01_exploration.ipynb
            data/        (empty, gitignored)
            tests/       (empty)

        Returns the project directory path.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        incomplete = self._find_incomplete_projects(output_dir)
        if incomplete:
            target = incomplete[0]
            print("\n" + "═" * 60)
            print("  PROJECT BUILDER — Finish-first guardrail")
            print("═" * 60)
            print(
                f"\n  🔁 Found {len(incomplete)} incomplete project(s) under "
                f"{output_dir}. Refining the most recent one instead of "
                f"scaffolding a new project for '{brief.get('title', '—')}'.\n"
                f"     → {target}\n"
            )
            self.last_action = "refined"
            # Inherit the original idea_key from the in-progress project so the
            # orchestrator can resolve it back to the right queue row.
            existing_meta = self._read_meta(target)
            inherited_idea_key = existing_meta.get("idea_key") or idea_key
            project_dir = await self._refine(target, brief_idea_key=inherited_idea_key)
            self.last_build_info = {
                "mode": "refine",
                "project_dir": str(project_dir),
                "idea_key": inherited_idea_key,
            }
            return project_dir

        self.last_action = "scaffolded"
        project_dir = await self._scaffold(brief, output_dir, idea_key=idea_key)
        self.last_build_info = {
            "mode": "scaffold",
            "project_dir": str(project_dir),
            "idea_key": idea_key,
        }
        return project_dir

    async def _scaffold(
        self,
        brief: dict,
        output_dir: Path,
        idea_key: str | None = None,
    ) -> Path:
        """Scaffold a brand-new project from a brief."""
        title = brief.get("title") or "project"
        base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:28] or "project"
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        slug = f"{base}_{digest}"
        project_dir = output_dir / slug
        n = 0
        while project_dir.exists():
            n += 1
            project_dir = output_dir / f"{slug}_{n}"
        project_dir.mkdir(parents=True, exist_ok=True)

        for subdir in ["src", "configs", "notebooks", "data", "tests"]:
            (project_dir / subdir).mkdir(exist_ok=True)

        print("\n" + "═" * 60)
        print(f"  PROJECT BUILDER — {brief['title']}")
        print("═" * 60)
        print(f"\n  📁 Scaffolding to: {project_dir}\n")

        # Drop an early in_progress marker so an interrupted run leaves a
        # clear "not finished" signal for the next build() invocation.
        self._write_meta(
            project_dir,
            status=STATUS_IN_PROGRESS,
            title=brief.get("title"),
            slug=project_dir.name,
            idea_key=idea_key,
            created_at=datetime.now().isoformat(),
            skill_demonstrated=brief.get("skill_demonstrated"),
        )

        files_to_generate = list(REQUIRED_SOURCE_FILES.items())

        print("  ⚙️  Generating source files...")

        async def gen(fp, role):
            content = await self._generate_file(fp, brief, role)
            return fp, content

        results = await asyncio.gather(*[gen(fp, role) for fp, role in files_to_generate])

        for filepath, content in results:
            full_path = project_dir / filepath
            full_path.write_text(content)
            print(f"    ✅ {filepath}")

        print("\n  📝 Generating README...")
        readme = await self._generate_readme(brief)
        (project_dir / "README.md").write_text(readme)
        print("    ✅ README.md")

        print("\n  📦 Generating requirements.txt...")
        reqs = await self._generate_requirements(brief)
        (project_dir / "requirements.txt").write_text(reqs)
        print("    ✅ requirements.txt")

        milestones = self._generate_milestone_tracker(brief)
        (project_dir / "MILESTONES.md").write_text(milestones)
        print("    ✅ MILESTONES.md")

        (project_dir / PROJECT_BRIEF_FILENAME).write_text(json.dumps(brief, indent=2))
        print(f"    ✅ {PROJECT_BRIEF_FILENAME}")

        gitignore = "data/\n*.pyc\n__pycache__/\n.env\n*.egg-info/\n.ipynb_checkpoints/\n"
        (project_dir / ".gitignore").write_text(gitignore)

        notebook_stub = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
            "cells": [
                {"cell_type": "markdown", "metadata": {}, "source": [f"# {brief['title']} — Exploration\n\n{brief['goal']}"]},
                {"cell_type": "code", "metadata": {}, "source": ["# Setup\nimport sys\nsys.path.insert(0, '../src')\n"], "outputs": [], "execution_count": None}
            ]
        }
        (project_dir / "notebooks/01_exploration.ipynb").write_text(json.dumps(notebook_stub, indent=2))
        print("    ✅ notebooks/01_exploration.ipynb")

        # Mark the project completed once all required artifacts are on disk.
        self._write_meta(
            project_dir,
            status=STATUS_COMPLETED,
            completed_at=datetime.now().isoformat(),
        )

        print(f"\n{'═'*60}")
        print(f"  ✅ Project scaffolded: {project_dir}\n")
        print(f"  🚀 To get started:\n")
        print(f"     cd {project_dir}")
        print(f"     pip install -r requirements.txt")
        print(f"     # Download dataset: {brief['dataset'].get('download_instructions', 'see README')}")
        print(f"     python src/train.py\n")
        if brief.get("milestones"):
            print(f"  📅 Week 1 goal: {brief['milestones'][0].get('goal', '—')}")
            print(f"  📅 Week 1 deliverable: {brief['milestones'][0].get('deliverable', '—')}\n")
        print(f"  📝 Resume bullet (after completion):")
        print(f"     {brief['resume_bullet']}\n")
        print(f"  Track progress in MILESTONES.md\n")

        return project_dir

    async def _refine(
        self,
        project_dir: Path,
        brief_idea_key: str | None = None,
    ) -> Path:
        """Fill in any missing required artifacts in an incomplete project,
        then mark it completed. If no stored brief is available, we still
        mark the project completed with a note so the finish-first guardrail
        doesn't loop on a stale half-built project.

        Every refine pass — including the no-brief and corrupt-brief
        shortcuts — finishes by writing ``status=STATUS_COMPLETED`` to
        meta.json. That is the signal the next ``build()`` call uses to
        decide whether refinement is still needed.
        """
        print(f"  🛠️  Refining incomplete project: {project_dir.name}")

        brief_path = project_dir / PROJECT_BRIEF_FILENAME
        regenerated: list[str] = []

        if not brief_path.exists():
            print(
                "    ⚠️  No project_brief.json found — marking complete with note. "
                "Nothing to regenerate from."
            )
            self._write_meta(
                project_dir,
                status=STATUS_COMPLETED,
                completed_at=datetime.now().isoformat(),
                refinement_note="Marked complete: no project_brief.json available to refine from.",
                **({"idea_key": brief_idea_key} if brief_idea_key else {}),
            )
            return project_dir

        try:
            brief = json.loads(brief_path.read_text())
        except json.JSONDecodeError as e:
            print(f"    ⚠️  project_brief.json is invalid ({e}); marking complete with note.")
            self._write_meta(
                project_dir,
                status=STATUS_COMPLETED,
                completed_at=datetime.now().isoformat(),
                refinement_note=f"Marked complete: project_brief.json invalid ({e}).",
                **({"idea_key": brief_idea_key} if brief_idea_key else {}),
            )
            return project_dir

        for subdir in ["src", "configs", "notebooks", "data", "tests"]:
            (project_dir / subdir).mkdir(exist_ok=True)

        missing_source = [
            (fp, role) for fp, role in REQUIRED_SOURCE_FILES.items()
            if not (project_dir / fp).exists() or not (project_dir / fp).read_text().strip()
        ]
        if missing_source:
            print(f"  ⚙️  Regenerating {len(missing_source)} missing source file(s)...")

            async def gen(fp, role):
                content = await self._generate_file(fp, brief, role)
                return fp, content

            results = await asyncio.gather(*[gen(fp, role) for fp, role in missing_source])
            for filepath, content in results:
                full_path = project_dir / filepath
                full_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.write_text(content)
                regenerated.append(filepath)
                print(f"    ✅ {filepath}")

        readme_path = project_dir / "README.md"
        if not readme_path.exists() or not readme_path.read_text().strip():
            print("  📝 Regenerating README...")
            readme = await self._generate_readme(brief)
            readme_path.write_text(readme)
            regenerated.append("README.md")
            print("    ✅ README.md")

        reqs_path = project_dir / "requirements.txt"
        if not reqs_path.exists() or not reqs_path.read_text().strip():
            print("  📦 Regenerating requirements.txt...")
            reqs = await self._generate_requirements(brief)
            reqs_path.write_text(reqs)
            regenerated.append("requirements.txt")
            print("    ✅ requirements.txt")

        milestones_path = project_dir / "MILESTONES.md"
        if not milestones_path.exists() or not milestones_path.read_text().strip():
            milestones = self._generate_milestone_tracker(brief)
            milestones_path.write_text(milestones)
            regenerated.append("MILESTONES.md")
            print("    ✅ MILESTONES.md")

        completion_fields: dict = {
            "status": STATUS_COMPLETED,
            "completed_at": datetime.now().isoformat(),
            "refined_at": datetime.now().isoformat(),
            "regenerated_files": regenerated,
            "refinement_note": (
                f"Refined: regenerated {len(regenerated)} missing artifact(s)."
                if regenerated else
                "Refined: all required artifacts already present; marked complete."
            ),
        }
        if brief_idea_key:
            completion_fields["idea_key"] = brief_idea_key
        self._write_meta(project_dir, **completion_fields)

        print(f"\n  ✅ Project marked completed: {project_dir}")
        if regenerated:
            print(f"     Regenerated: {', '.join(regenerated)}")
        else:
            print("     No regeneration needed — all required artifacts were already present.")
        print()
        return project_dir
