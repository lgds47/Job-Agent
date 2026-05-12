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
from anthropic import Anthropic

from tools.llm_json import loads_llm_json

client = Anthropic()

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


def _extract_skills_sync(title: str, company: str, description: str) -> dict:
    snippet = (description or "")[:4500]
    user = f"""Job title: {title}
Company: {company}

Posting text:
{snippet}
"""
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
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


async def extract_skills_from_posting(title: str, company: str, description: str) -> dict:
    """Infer required/preferred skills from posting text (used by Search + gaps backfill)."""
    return await asyncio.to_thread(_extract_skills_sync, title, company, description)


async def parse_jd(url: str) -> dict:
    """Fetch a job posting URL and return structured JD data."""

    # Fetch the page
    async with httpx.AsyncClient(follow_redirects=True, timeout=15) as http:
        resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()

    # Strip to text
    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    raw_text = soup.get_text(separator="\n", strip=True)

    # Trim to ~6000 chars to stay within token budget
    raw_text = raw_text[:6000]

    # Ask Claude to extract structure
    response = await asyncio.to_thread(
        client.messages.create,
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Parse this job posting:\n\n{raw_text}"}
        ],
    )

    raw = response.content[0].text.strip()
    jd = loads_llm_json(raw)

    if not isinstance(jd, dict):
        raise ValueError("parse_jd: expected JSON object from model")

    # Always preserve the source URL
    jd["source_url"] = url
    return jd


if __name__ == "__main__":
    import asyncio
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/job"
    result = asyncio.run(parse_jd(url))
    print(json.dumps(result, indent=2))
