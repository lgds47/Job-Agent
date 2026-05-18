"""
JD Parser
=========
Fetches a job posting URL and extracts structured data using Claude.
Returns a normalized JD dict that all agents consume.
"""

import asyncio
import json
import httpx
from bs4 import BeautifulSoup
from anthropic import AsyncAnthropic

from tools.api_errors import JobNotFoundError
from tools.ats_resolve import fetch_ats_job
from tools.llm_json import loads_llm_json

client = AsyncAnthropic()

SYSTEM_PROMPT = """You are a job description parser. Extract structured data from the job posting text provided.
Return ONLY valid JSON, no preamble, no markdown fences.

Schema:
{
  "title": "exact job title",
  "company": "company name",
  "location": "location or Remote",
  "employment_type": "full-time | part-time | contract",
  "experience_level": "entry | mid | senior | staff | principal",
  "required_skills": ["list of explicitly required skills"],
  "preferred_skills": ["list of nice-to-have skills"],
  "responsibilities": ["key responsibilities as short phrases"],
  "keywords": ["important terms that should appear in a tailored resume"],
  "culture_signals": ["any signals about team culture, values, ways of working"],
  "compensation": "salary range if mentioned, else null"
}
"""

EXTRACT_SKILLS_SYSTEM = """You extract technical skills from a job posting body (may be partial HTML text).
Return ONLY valid JSON, no preamble, no markdown fences.

Schema:
{
  "required_skills": ["skills explicitly required or must-have"],
  "preferred_skills": ["nice-to-have or bonus skills"]
}

Rules:
- Use short canonical phrases (e.g. "PyTorch", "Kubernetes", "distributed training").
- If the text is too thin to tell, return empty arrays.
- Do not invent skills not supported by the text.
"""


async def extract_skills_from_posting(title: str, company: str, description: str) -> dict:
    """Infer required/preferred skills from posting text (used by Search + gaps backfill)."""
    snippet = (description or "")[:4500]
    user = f"""Job title: {title}
Company: {company}

Posting text:
{snippet}
"""
    response = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        system=EXTRACT_SKILLS_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    raw = response.content[0].text
    data = loads_llm_json(raw)
    if not isinstance(data, dict):
        raise ValueError("extract_skills_from_posting: expected JSON object from model")
    req = data.get("required_skills") or []
    pref = data.get("preferred_skills") or []
    return {
        "required_skills": [str(s).strip() for s in req if str(s).strip()],
        "preferred_skills": [str(s).strip() for s in pref if str(s).strip()],
    }


async def _fetch_html_text(url: str) -> str:
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as http:
        resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)[:6000]


async def _extract_jd_from_text(raw_text: str, *, source_url: str, hints: dict | None = None) -> dict:
    header = ""
    if hints:
        header = (
            f"Known title: {hints.get('title') or 'unknown'}\n"
            f"Known company: {hints.get('company') or 'unknown'}\n"
            f"Known location: {hints.get('location') or 'unknown'}\n\n"
        )

    response = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Parse this job posting:\n\n{header}{raw_text}",
            }
        ],
    )

    raw = response.content[0].text.strip()
    jd = loads_llm_json(raw)

    if not isinstance(jd, dict):
        raise ValueError("parse_jd: expected JSON object from model")

    jd["source_url"] = source_url
    if hints:
        if hints.get("canonical_url"):
            jd["canonical_url"] = hints["canonical_url"]
        if hints.get("ats"):
            jd["ats_source"] = hints["ats"]
    return jd


async def parse_jd(url: str) -> dict:
    """Fetch a job posting URL and return structured JD data."""

    ats = await fetch_ats_job(url)
    if ats is not None:
        body = ats.description
        if not body.strip():
            body = f"{ats.title}\n{ats.company}\n{ats.location}"
        hints = {
            "title": ats.title,
            "company": ats.company,
            "location": ats.location,
            "canonical_url": ats.canonical_url,
            "ats": ats.ats,
        }
        return await _extract_jd_from_text(body, source_url=url, hints=hints)

    # Generic HTML scrape (custom sites, legacy board pages)
    try:
        raw_text = await _fetch_html_text(url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise JobNotFoundError(f"Job page not found (HTTP 404): {url}") from e
        raise

    if "?error=true" in url.lower() or "error=true" in raw_text[:500].lower():
        raise JobNotFoundError(
            "Job board returned an error page — use a live absolute_url from the "
            "ATS JSON API (e.g. job-boards.greenhouse.io/.../jobs/{id})"
        )

    return await _extract_jd_from_text(raw_text, source_url=url)


if __name__ == "__main__":
    import sys

    job_url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/job"
    result = asyncio.run(parse_jd(job_url))
    print(json.dumps(result, indent=2))
