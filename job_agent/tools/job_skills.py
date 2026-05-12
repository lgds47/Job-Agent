"""
Enrich ATS job dicts with required_skills / preferred_skills for gap analysis.
"""

from __future__ import annotations

import asyncio

from tools.jd_parser import extract_skills_from_posting


async def enrich_jobs_skill_lists(
    jobs: list[dict],
    *,
    persist_store=None,
    concurrency: int = 5,
) -> list[dict]:
    """
    For each job missing skill lists, run LLM extraction on description text.
    If persist_store is a StateStore, upsert each updated job so gaps work on older DB rows.
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(job: dict) -> dict:
        async with sem:
            if job.get("required_skills") or job.get("preferred_skills"):
                return job
            desc = (job.get("description") or job.get("description_snippet") or "").strip()
            if not desc:
                job.setdefault("required_skills", [])
                job.setdefault("preferred_skills", [])
                return job
            try:
                skills = await extract_skills_from_posting(
                    job.get("title") or "",
                    job.get("company") or "",
                    desc,
                )
                job["required_skills"] = skills["required_skills"]
                job["preferred_skills"] = skills["preferred_skills"]
                if persist_store is not None and job.get("url"):
                    persist_store.save_jobs([job])
            except Exception as e:
                print(f"    ⚠️  Skill extraction failed ({job.get('title')!r} @ {job.get('company')}): {e}")
                job.setdefault("required_skills", [])
                job.setdefault("preferred_skills", [])
            return job

    return list(await asyncio.gather(*[one(j) for j in jobs]))
