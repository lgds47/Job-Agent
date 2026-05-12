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
from pathlib import Path
from anthropic import Anthropic

client = Anthropic()

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
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system=CODE_GEN_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()

    async def _generate_readme(self, brief: dict) -> str:
        """Generate the project README."""
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=README_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(brief, indent=2)}]
        )
        return response.content[0].text.strip()

    async def _generate_requirements(self, brief: dict) -> str:
        """Generate requirements.txt from the brief's stack."""
        core = brief["stack"]["core"]
        optional = brief["stack"].get("optional", [])

        # Ask Claude to resolve package names and pin reasonable versions
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system="Return only a requirements.txt file content. Pin reasonable versions. No comments, no markdown.",
            messages=[{"role": "user", "content": f"Generate requirements.txt for: {core + optional}"}]
        )
        return response.content[0].text.strip()

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

    async def build(self, brief: dict, output_dir: Path = Path("data/projects")) -> Path:
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
        slug = brief["title"].lower().replace(" ", "_").replace("-", "_")[:40]
        project_dir = output_dir / slug
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

        # Generate code files concurrently
        print("  ⚙️  Generating source files...")
        tasks = [(fp, role) for fp, role in files_to_generate]

        async def gen(fp, role):
            content = await self._generate_file(fp, brief, role)
            return fp, content

        results = await asyncio.gather(*[gen(fp, role) for fp, role in tasks])

        for filepath, content in results:
            full_path = project_dir / filepath
            full_path.write_text(content)
            print(f"    ✅ {filepath}")

        # Generate README
        print("\n  📝 Generating README...")
        readme = await self._generate_readme(brief)
        (project_dir / "README.md").write_text(readme)
        print("    ✅ README.md")

        # Generate requirements.txt
        print("\n  📦 Generating requirements.txt...")
        reqs = await self._generate_requirements(brief)
        (project_dir / "requirements.txt").write_text(reqs)
        print("    ✅ requirements.txt")

        # Generate milestone tracker (no LLM needed)
        milestones = self._generate_milestone_tracker(brief)
        (project_dir / "MILESTONES.md").write_text(milestones)
        print("    ✅ MILESTONES.md")

        # Write brief JSON for reference
        (project_dir / "project_brief.json").write_text(json.dumps(brief, indent=2))
        print("    ✅ project_brief.json")

        # .gitignore
        gitignore = "data/\n*.pyc\n__pycache__/\n.env\n*.egg-info/\n.ipynb_checkpoints/\n"
        (project_dir / ".gitignore").write_text(gitignore)

        # Empty notebook stub
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

        # ── Handoff summary ───────────────────────────────────────────────────
        print(f"\n{'═'*60}")
        print(f"  ✅ Project scaffolded: {project_dir}\n")
        print(f"  🚀 To get started:\n")
        print(f"     cd {project_dir}")
        print(f"     pip install -r requirements.txt")
        print(f"     # Download dataset: {brief['dataset'].get('download_instructions', 'see README')}")
        print(f"     python src/train.py\n")
        print(f"  📅 Week 1 goal: {brief['milestones'][0]['goal']}")
        print(f"  📅 Week 1 deliverable: {brief['milestones'][0]['deliverable']}\n")
        print(f"  📝 Resume bullet (after completion):")
        print(f"     {brief['resume_bullet']}\n")
        print(f"  Track progress in MILESTONES.md\n")

        return project_dir
