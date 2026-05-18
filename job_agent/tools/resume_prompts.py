"""
Build resume-derived prompt fragments (positioning, discovery context).
"""

from __future__ import annotations

import json
from typing import Any


def top_skills_by_category(resume: dict, *, per_category: int = 4) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, cat in (resume.get("skills") or {}).items():
        if not isinstance(cat, dict):
            continue
        items = cat.get("items") or []
        label = cat.get("label") or key
        out[str(label)] = [str(i) for i in items[:per_category]]
    return out


def build_positioning_block(resume: dict) -> str:
    """
    Persona / differentiator text for ResumeAgent and CoverLetterAgent.

    Uses ``agent_metadata.positioning_notes`` or ``differentiators[]`` when set;
    otherwise derives neutral guidance from summary and skills.
    """
    meta = resume.get("agent_metadata") or {}
    notes = meta.get("positioning_notes")
    if notes:
        return str(notes).strip()

    diffs = meta.get("differentiators")
    if isinstance(diffs, list) and diffs:
        bullets = "\n".join(f"- {d}" for d in diffs if str(d).strip())
        return f"Candidate differentiators (from resume metadata):\n{bullets}"

    summary = (resume.get("summary") or {}).get("text", "").strip()
    skills = top_skills_by_category(resume)
    parts = [
        "Emphasize the strongest overlap between the candidate's materials and the job description.",
        "Do not invent experience, metrics, tools, or titles not supported by the resume or JD.",
    ]
    if summary:
        parts.append(f"Professional summary (source of truth): {summary}")
    if skills:
        parts.append("Top skills by area: " + json.dumps(skills))
    return "\n".join(parts)


def build_discovery_context(resume: dict, target_roles: list[str]) -> dict[str, Any]:
    """Candidate-specific discovery hints injected into the user message only."""
    meta = resume.get("agent_metadata") or {}
    contact = resume.get("contact") or {}

    preferred_locations = meta.get("preferred_locations")
    if not preferred_locations:
        region = meta.get("region") or contact.get("location") or contact.get("region")
        if region:
            preferred_locations = [region] if isinstance(region, str) else region

    years = meta.get("years_experience") or meta.get("experience_years")
    seniority = meta.get("seniority_target") or meta.get("career_level")

    return {
        "target_roles": target_roles,
        "preferred_locations": preferred_locations or [],
        "seniority_target": seniority,
        "years_experience": years,
        "candidate_summary": (resume.get("summary") or {}).get("text", ""),
        "current_title": contact.get("title"),
    }
