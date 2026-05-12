"""
Resume Agent
============
Given a structured resume JSON and a parsed JD, produces a tailored
resume by:
  1. Scoring and reranking bullets by relevance to the JD
  2. Rewriting the professional summary to mirror JD language
  3. Flagging which skills to surface vs. de-emphasize
  4. Returning a match_score for the application tracker

The output is a tailored_resume.json — a modified copy of the base
resume, never modifying the original.
"""

import json
import copy
from anthropic import AsyncAnthropic

from tools.llm_json import loads_llm_json
from tools.text_sanitize import strip_code_fences

client = AsyncAnthropic()

BULLET_SCORE_SYSTEM = """You are a resume tailoring expert.
Given a job description and a list of resume bullet points (each with an id),
return ONLY a JSON array ranking the bullets from most to least relevant.

Format:
[
  {"id": "b_001_2", "relevance": 95, "reason": "direct PyTorch + CV match"},
  {"id": "b_002_1", "relevance": 60, "reason": "analytics experience, indirect"},
  ...
]

Include ALL bullet ids. Score 0-100. Return only JSON, no markdown.
"""

SUMMARY_REWRITE_SYSTEM = """You are a resume writer.
Rewrite the professional summary to align with the job description.
Rules:
- Keep it under 4 sentences
- Mirror key terms from the JD naturally (not keyword stuffing)
- Preserve all factual claims — do not invent experience
- Return only the summary text, no quotes, no labels
"""


class ResumeAgent:
    def __init__(self, resume: dict):
        self.resume = resume

    def _all_bullets(self) -> list[dict]:
        bullets = []
        for exp in self.resume["experience"]:
            for b in exp["bullets"]:
                bullets.append({
                    **b,
                    "company": exp["company"],
                    "title": exp["title"],
                    "current": exp.get("current", False)
                })
        return bullets

    async def _rank_bullets(self, jd: dict) -> list[dict]:
        """Ask Claude to rank all bullets by JD relevance."""
        bullets = self._all_bullets()
        bullet_list = [{"id": b["id"], "text": b["text"]} for b in bullets]

        jd_summary = json.dumps({
            "title": jd.get("title"),
            "required_skills": jd.get("required_skills", []),
            "preferred_skills": jd.get("preferred_skills", []),
            "responsibilities": jd.get("responsibilities", []),
            "keywords": jd.get("keywords", [])
        })

        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            system=BULLET_SCORE_SYSTEM,
            messages=[{"role": "user", "content": f"JD:\n{jd_summary}\n\nBullets:\n{json.dumps(bullet_list)}"}]
        )

        try:
            ranked = loads_llm_json(response.content[0].text)
        except ValueError as e:
            raise RuntimeError(f"ResumeAgent bullet ranking failed: {e}") from e
        if not isinstance(ranked, list):
            raise RuntimeError("ResumeAgent expected a JSON array of bullet rankings from the model.")
        # Merge relevance scores back onto full bullet objects
        score_map = {r["id"]: r for r in ranked if isinstance(r, dict) and "id" in r}
        missing = [b["id"] for b in bullets if b["id"] not in score_map]
        if missing:
            print(f"  ⚠️  ResumeAgent: model omitted {len(missing)} bullet(s) from ranking — defaulting to relevance=0: {missing}")
        for b in bullets:
            b["relevance"] = score_map.get(b["id"], {}).get("relevance", 0)
            b["relevance_reason"] = score_map.get(b["id"], {}).get("reason", "")
        return sorted(bullets, key=lambda x: x["relevance"], reverse=True)

    async def _rewrite_summary(self, jd: dict) -> str:
        """Rewrite the summary to match JD language."""
        original = self.resume["summary"]["text"]
        jd_text = f"""
Title: {jd.get('title')}
Required skills: {', '.join(jd.get('required_skills', []))}
Responsibilities: {', '.join(jd.get('responsibilities', [])[:5])}
Keywords: {', '.join(jd.get('keywords', []))}
"""
        response = await client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            system=SUMMARY_REWRITE_SYSTEM,
            messages=[{"role": "user", "content": f"Original summary:\n{original}\n\nTarget JD:\n{jd_text}"}]
        )
        return strip_code_fences(response.content[0].text)

    async def run(self, jd: dict) -> dict:
        """
        Produce a tailored resume dict.
        Returns a deep copy of the base resume with:
          - reranked bullets (top 2 per role surfaced first)
          - rewritten summary
          - match_score (avg of top-5 bullet relevances)
          - jd metadata attached
        """
        import asyncio

        ranked_bullets, new_summary = await asyncio.gather(
            self._rank_bullets(jd),
            self._rewrite_summary(jd)
        )

        # Build tailored resume as a deep copy
        tailored = copy.deepcopy(self.resume)
        tailored["summary"]["text"] = new_summary
        tailored["summary"]["tailored_for"] = jd.get("title")

        # Reorder bullets within each experience block by relevance
        bullet_order = {b["id"]: b["relevance"] for b in ranked_bullets}
        for exp in tailored["experience"]:
            exp["bullets"].sort(
                key=lambda b: bullet_order.get(b["id"], 0),
                reverse=True
            )
            # Attach relevance score to each bullet for downstream use
            for b in exp["bullets"]:
                b["relevance"] = bullet_order.get(b["id"], 0)

        # Compute overall match score from top 5 bullets
        top5 = sorted(ranked_bullets, key=lambda x: x["relevance"], reverse=True)[:5]
        match_score = round(sum(b["relevance"] for b in top5) / len(top5)) if top5 else 0
        tailored["match_score"] = match_score

        # Surface which skills to emphasize
        jd_skills = set(s.lower() for s in jd.get("required_skills", []) + jd.get("preferred_skills", []))
        all_skills = []
        for cat in tailored["skills"].values():
            all_skills.extend(cat["items"])
        tailored["emphasized_skills"] = [s for s in all_skills if s.lower() in jd_skills]

        tailored["tailored_for_jd"] = {
            "title": jd.get("title"),
            "company": jd.get("company"),
            "url": jd.get("source_url")
        }

        return tailored
