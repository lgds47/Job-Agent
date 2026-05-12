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

Usage:
  builder = ProjectBuilderAgent(resume=resume)
  await builder.build(brief=brief, output_dir=Path("data/projects"))
"""

import json
import asyncio
import hashlib
import re
from pathlib import Path
from datetime import datetime
from anthropic import AsyncAnthropic

from tools.text_sanitize import strip_code_fences

client = AsyncAnthropic()
PROJECT_META_FILENAME = "meta.json"
PROJECT_BRIEF_FILENAME = "project_brief.json"
PROJECT_COMPLETED_FILENAME = "completed.json"

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
        self.last_build_info = {}

    @staticmethod
    def _load_json(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _load_project_meta(self, project_dir: Path) -> dict:
        return self._load_json(project_dir / PROJECT_META_FILENAME)

    def _is_project_complete(self, project_dir: Path) -> bool:
        completed_marker = project_dir / PROJECT_COMPLETED_FILENAME
        if completed_marker.exists():
            return True
        meta = self._load_project_meta(project_dir)
        return str(meta.get("status", "")).lower() == "completed"

    def _most_recent_incomplete_project(self, output_dir: Path) -> Path | None:
        if not output_dir.exists():
            return None
        candidates = [p for p in output_dir.iterdir() if p.is_dir()]
        incomplete = [p for p in candidates if not self._is_project_complete(p)]
        if not incomplete:
            return None
        return sorted(incomplete, key=lambda p: p.stat().st_mtime, reverse=True)[0]

    def _load_existing_brief(self, project_dir: Path) -> dict:
        return self._load_json(project_dir / PROJECT_BRIEF_FILENAME)

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

        # Ask Claude to resolve package names and pin reasonable versions
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
        idea_key: str | None = None
    ) -> Path:
        """
        Scaffold the full project from a ProjectBrief.

        Creates:
          data/projects/{slug}/
            README.md
            requirements.txt
            MILESTONES.md
            src/
              train.py
              model.py
              dataset.py
              evaluate.py
            configs/
              config.yaml
            notebooks/
              01_exploration.ipynb
            data/        (empty, gitignored)
            tests/       (empty)

        Returns the project directory path.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        mode = "new"
        active_idea_key = idea_key

        existing_incomplete = self._most_recent_incomplete_project(output_dir)
        if existing_incomplete:
            mode = "refine"
            project_dir = existing_incomplete
            stored_brief = self._load_existing_brief(project_dir)
            if stored_brief:
                brief = stored_brief
            existing_meta = self._load_project_meta(project_dir)
            active_idea_key = existing_meta.get("idea_key") or active_idea_key
            print("\n  ⚠️  Found unfinished project. Refining it before starting a new one.")
            print(f"  ↺ Selected existing project: {project_dir}")
        else:
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

        # Create subdirectories
        for subdir in ["src", "configs", "notebooks", "data", "tests"]:
            (project_dir / subdir).mkdir(exist_ok=True)

        print("\n" + "═" * 60)
        print(f"  PROJECT BUILDER — {brief['title']}")
        print("═" * 60)
        print(f"\n  📁 Scaffolding to: {project_dir}\n")

        # Define files to generate with their roles
        files_to_generate = [
            ("src/train.py",     "Main training loop. Loads data, initializes model, runs training, logs metrics."),
            ("src/model.py",     "Model definition and architecture. Should be importable standalone."),
            ("src/dataset.py",   "Dataset class and data loading utilities. Handle download, preprocessing, batching."),
            ("src/evaluate.py",  "Evaluation script. Load a checkpoint and compute metrics on a test set."),
            ("configs/config.yaml", "Training configuration: hyperparameters, paths, model settings."),
        ]

        # Generate code files (missing/empty only during refinement)
        print("  ⚙️  Generating source files...")
        tasks = []
        for fp, role in files_to_generate:
            full_path = project_dir / fp
            if mode == "refine" and full_path.exists() and full_path.read_text().strip():
                print(f"    ↺ Keeping existing {fp}")
                continue
            tasks.append((fp, role))

        async def gen(fp, role):
            content = await self._generate_file(fp, brief, role)
            return fp, content

        results = await asyncio.gather(*[gen(fp, role) for fp, role in tasks]) if tasks else []

        for filepath, content in results:
            full_path = project_dir / filepath
            full_path.write_text(content)
            print(f"    ✅ {filepath}")

        # Generate README
        readme_path = project_dir / "README.md"
        if mode == "refine" and readme_path.exists() and readme_path.read_text().strip():
            print("\n  ↺ Keeping existing README.md")
        else:
            print("\n  📝 Generating README...")
            readme = await self._generate_readme(brief)
            readme_path.write_text(readme)
            print("    ✅ README.md")

        # Generate requirements.txt
        reqs_path = project_dir / "requirements.txt"
        if mode == "refine" and reqs_path.exists() and reqs_path.read_text().strip():
            print("\n  ↺ Keeping existing requirements.txt")
        else:
            print("\n  📦 Generating requirements.txt...")
            reqs = await self._generate_requirements(brief)
            reqs_path.write_text(reqs)
            print("    ✅ requirements.txt")

        # Generate milestone tracker (no LLM needed)
        milestones_path = project_dir / "MILESTONES.md"
        if mode == "refine" and milestones_path.exists() and milestones_path.read_text().strip():
            print("    ↺ Keeping existing MILESTONES.md")
        else:
            milestones = self._generate_milestone_tracker(brief)
            milestones_path.write_text(milestones)
            print("    ✅ MILESTONES.md")

        # Write brief JSON for reference
        brief_path = project_dir / PROJECT_BRIEF_FILENAME
        if not brief_path.exists() or not brief_path.read_text().strip():
            brief_path.write_text(json.dumps(brief, indent=2))
            print("    ✅ project_brief.json")
        else:
            print("    ↺ Keeping existing project_brief.json")

        meta = self._load_project_meta(project_dir)
        now = datetime.now().isoformat()
        meta.update({
            "title": brief.get("title"),
            "status": "in_progress",
            "idea_key": active_idea_key,
            "updated_at": now,
            "last_builder_mode": mode,
        })
        if "created_at" not in meta:
            meta["created_at"] = now
        (project_dir / PROJECT_META_FILENAME).write_text(json.dumps(meta, indent=2))
        print(f"    ✅ {PROJECT_META_FILENAME}")

        # .gitignore
        gitignore = "data/\n*.pyc\n__pycache__/\n.env\n*.egg-info/\n.ipynb_checkpoints/\n"
        gitignore_path = project_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text(gitignore)

        # Empty notebook stub
        notebook_path = project_dir / "notebooks/01_exploration.ipynb"
        if not notebook_path.exists():
            notebook_stub = {
                "nbformat": 4,
                "nbformat_minor": 5,
                "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
                "cells": [
                    {"cell_type": "markdown", "metadata": {}, "source": [f"# {brief['title']} — Exploration\n\n{brief['goal']}"]},
                    {"cell_type": "code", "metadata": {}, "source": ["# Setup\nimport sys\nsys.path.insert(0, '../src')\n"], "outputs": [], "execution_count": None}
                ]
            }
            notebook_path.write_text(json.dumps(notebook_stub, indent=2))
            print("    ✅ notebooks/01_exploration.ipynb")
        else:
            print("    ↺ Keeping existing notebooks/01_exploration.ipynb")

        # ── Handoff summary ───────────────────────────────────────────────────
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

        self.last_build_info = {
            "mode": mode,
            "project_dir": str(project_dir),
            "idea_key": active_idea_key,
        }
        return project_dir
