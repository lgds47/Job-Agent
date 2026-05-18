"""
Resolve job posting URLs to ATS-backed plain text when possible.

Reuses the same public APIs as SearchAgent (Greenhouse, Lever, Ashby) so apply
does not scrape board chrome from stale HTML pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from typing import Any

import httpx
from bs4 import BeautifulSoup

from tools.api_errors import JobNotFoundError

# job-boards.greenhouse.io/{slug}/jobs/{id}
# boards.greenhouse.io/{slug}/jobs/{id} (legacy)
_GH_BOARD_JOB = re.compile(
    r"(?:https?://)?(?:job-boards|boards)\.greenhouse\.io/(?P<slug>[^/]+)/jobs/(?P<job_id>\d+)",
    re.I,
)
# embed / query variants are not handled here — fall back to HTML scrape

# jobs.lever.co/{company}/{posting_id}
_LEVER_JOB = re.compile(
    r"(?:https?://)?jobs\.lever\.co/(?P<slug>[^/]+)/(?P<posting_id>[a-f0-9-]{36})",
    re.I,
)

# jobs.ashbyhq.com/{org}/{job_id}
_ASHBY_JOB = re.compile(
    r"(?:https?://)?jobs\.ashbyhq\.com/(?P<slug>[^/]+)/(?P<job_id>[a-f0-9-]{36})",
    re.I,
)


@dataclass(frozen=True)
class AtsResolved:
    ats: str
    board_slug: str
    job_id: str
    title: str
    company: str
    location: str
    description: str
    canonical_url: str


def parse_job_url(url: str) -> dict[str, str] | None:
    """Return {ats, slug, job_id} if URL matches a known ATS job pattern."""
    for pattern, ats in (
        (_GH_BOARD_JOB, "greenhouse"),
        (_LEVER_JOB, "lever"),
        (_ASHBY_JOB, "ashby"),
    ):
        m = pattern.search(url.strip())
        if m:
            job_id = m.groupdict().get("job_id") or m.groupdict().get("posting_id")
            return {"ats": ats, "slug": m.group("slug"), "job_id": job_id}
    return None


def _strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator="\n", strip=True)


async def fetch_ats_job(url: str) -> AtsResolved | None:
    """
    If ``url`` is a supported ATS job link, fetch structured posting text.

    Raises ``JobNotFoundError`` when the ATS API returns 404.
    Returns ``None`` when the URL is not a recognized ATS job pattern (caller
    should fall back to generic HTML scrape).
    """
    parsed = parse_job_url(url)
    if not parsed:
        return None

    ats = parsed["ats"]
    if ats == "greenhouse":
        return await _fetch_greenhouse_job(parsed["slug"], parsed["job_id"], url)
    if ats == "lever":
        return await _fetch_lever_job(parsed["slug"], parsed["job_id"], url)
    if ats == "ashby":
        return await _fetch_ashby_job(parsed["slug"], parsed["job_id"], url)
    return None


async def _fetch_greenhouse_job(slug: str, job_id: str, source_url: str) -> AtsResolved:
    list_url = f"https://boards.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(list_url)
        if resp.status_code == 404:
            raise JobNotFoundError(
                f"Greenhouse board not found: {slug} (check slug / URL)"
            )
        resp.raise_for_status()
        payload = resp.json()

    jobs = payload.get("jobs") or []
    match = next((j for j in jobs if str(j.get("id")) == str(job_id)), None)
    if not match:
        raise JobNotFoundError(
            f"Greenhouse job id {job_id} not found on board {slug}"
        )

    content = match.get("content") or ""
    description = _strip_html(unescape(content)) if content else ""
    loc = match.get("location") or {}
    location = loc.get("name", "Remote") if isinstance(loc, dict) else str(loc)

    return AtsResolved(
        ats="greenhouse",
        board_slug=slug,
        job_id=str(job_id),
        title=match.get("title") or "",
        company=payload.get("name") or slug.replace("-", " ").title(),
        location=location,
        description=description[:8000],
        canonical_url=match.get("absolute_url") or source_url,
    )


async def _fetch_lever_job(slug: str, posting_id: str, source_url: str) -> AtsResolved:
    api_url = f"https://api.lever.co/v0/postings/{slug}/{posting_id}"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(api_url)
        if resp.status_code == 404:
            raise JobNotFoundError(f"Lever posting not found: {slug}/{posting_id}")
        resp.raise_for_status()
        j: dict[str, Any] = resp.json()

    desc = (j.get("descriptionPlain") or "") or _strip_html(j.get("description", "") or "")
    cats = j.get("categories") or {}

    return AtsResolved(
        ats="lever",
        board_slug=slug,
        job_id=posting_id,
        title=j.get("text") or "",
        company=j.get("categories", {}).get("team") or slug.replace("-", " ").title(),
        location=cats.get("location", "Remote") if isinstance(cats, dict) else "Remote",
        description=desc[:8000],
        canonical_url=j.get("hostedUrl") or source_url,
    )


async def _fetch_ashby_job(slug: str, job_id: str, source_url: str) -> AtsResolved:
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.get(api_url)
        if resp.status_code == 404:
            raise JobNotFoundError(f"Ashby board not found: {slug}")
        resp.raise_for_status()
        payload = resp.json()

    jobs = payload.get("jobs") or []
    match = next((j for j in jobs if str(j.get("id")) == str(job_id)), None)
    if not match:
        raise JobNotFoundError(f"Ashby job id {job_id} not found on board {slug}")

    html_desc = match.get("descriptionHtml") or ""
    description = _strip_html(html_desc)[:8000]

    return AtsResolved(
        ats="ashby",
        board_slug=slug,
        job_id=str(job_id),
        title=match.get("title") or "",
        company=payload.get("companyName") or slug.replace("-", " ").title(),
        location=match.get("locationName", "Remote"),
        description=description,
        canonical_url=match.get("jobUrl") or source_url,
    )
