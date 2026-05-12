"""
JD Parser
=========
Fetches a job posting URL and extracts structured data using Claude.
Returns a normalized JD dict that all agents consume.
"""

import json
import httpx
from bs4 import BeautifulSoup
from anthropic import Anthropic

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
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"Parse this job posting:\n\n{raw_text}"}
        ]
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    jd = json.loads(raw.strip())

    # Always preserve the source URL
    jd["source_url"] = url
    return jd


if __name__ == "__main__":
    import asyncio
    import sys

    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com/job"
    result = asyncio.run(parse_jd(url))
    print(json.dumps(result, indent=2))
