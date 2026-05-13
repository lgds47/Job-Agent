"""
Search Agent
============
Discovers job postings through two complementary modes:

DISCOVERY MODE (periodic)
  Claude actively researches which companies are currently hiring ML engineers,
  evaluates each for early-career fit, and builds a dynamic company roster.
  No hardcoded lists — the agent decides where to look.

AD-HOC MODE (on demand)
  Pass any company name via --company CLI flag (or adhoc_companies arg) and
  the agent immediately researches it, detects its ATS, fetches open roles,
  scores them, and merges into the pipeline. No code changes required.
  Example: python orchestrator.py search --company "Glean"

ATS support: Greenhouse, Lever, Ashby (all have public JSON APIs).
             Workday and custom career pages scraped + Claude-parsed.

Scoring: Claude rates each JD 0-100 with early-career fit awareness.

Early-exit guardrail
--------------------
The scoring loop is failure-aware: a consecutive low-score streak halts
further scoring, but Claude API failures during scoring do NOT advance the
streak counter (they are tracked separately via ``claude_failures``). The
two knobs are exposed as module-level constants AND as optional constructor
parameters so callers/tests can override them without monkey-patching:

  - ``EARLY_EXIT_CONSECUTIVE_LOW`` (default ``7``): how many consecutive
    sub-threshold scores trigger early exit.
  - ``EARLY_EXIT_SCORE_THRESHOLD`` (default ``50``): a score strictly below
    this counts as a "low" score for streak purposes.
"""

import json
import asyncio
from datetime import datetime
import httpx
from anthropic import AsyncAnthropic

from tools.llm_json import loads_llm_json
from tools.job_skills import enrich_jobs_skill_lists

client = AsyncAnthropic()

# ── Early-exit guardrail constants ────────────────────────────────────────────
# Halt scoring early if a streak of consecutive postings score below the
# threshold. Prevents the agent from spending more tokens on a clearly weak
# batch of listings. Both knobs can also be overridden per-instance via
# ``SearchAgent(..., consecutive_low_limit=..., low_score_threshold=...)``.
EARLY_EXIT_CONSECUTIVE_LOW = 7   # how many low-scoring jobs in a row trigger exit
EARLY_EXIT_SCORE_THRESHOLD = 50  # a score < this counts as "low" for streak purposes

# Concurrency for the scoring loop. Decoupled from the streak threshold on
# purpose: streak detection is sequential per-batch, so a fixed batch size
# keeps parallelism predictable regardless of how the threshold is tuned.
SCORING_BATCH_SIZE = 5

# ── Prompts ───────────────────────────────────────────────────────────────────

DISCOVERY_SYSTEM = """You are a technical recruiter and ML industry analyst.

Given a candidate profile and target roles, identify 15-20 companies that are:
1. Actively hiring for those roles RIGHT NOW (not hypothetically)
2. Strong fits for an early-career ML engineer (0-3 years experience):
   - Real ML infrastructure, not just ML as a peripheral feature
   - Engineering-driven culture with mentorship norms
   - Clear growth path for junior/associate engineers
   - Well-funded or profitable (not at imminent shutdown risk)
3. Diverse across size and stage (startups to large tech)
4. Recognizable on a resume — name-brand value matters early-career

For each company, identify which ATS they use so we can fetch jobs directly.
Most companies use one of: greenhouse, lever, ashby, workday, custom.

Return ONLY a JSON array, no markdown fences:
[
  {
    "name": "Company Name",
    "ats": "greenhouse | lever | ashby | workday | custom",
    "slug": "their-ats-slug-if-known-else-null",
    "career_url": "https://company.com/careers",
    "why_good_fit": "one sentence — specific reason good for early-career ML",
    "ml_maturity": "core | growing | adjacent",
    "stage": "startup | growth | public | large-tech"
  }
]

ml_maturity:
  core     = ML is the product (Anthropic, HuggingFace, W&B, Scale AI)
  growing  = significant ML investment, expanding teams (Notion, Figma, Stripe)
  adjacent = uses ML but not primarily an ML company
"""

ATS_DETECT_SYSTEM = """You are a technical recruiter. Given a company name,
determine which ATS they use and what their careers page URL is.

Common patterns:
  greenhouse → boards.greenhouse.io/{slug}
  lever      → jobs.lever.co/{slug}
  ashby      → jobs.ashbyhq.com/{slug}
  workday    → {company}.wd{n}.myworkdayjobs.com/careers

Return ONLY JSON, no markdown:
{
  "ats": "greenhouse | lever | ashby | workday | custom",
  "slug": "slug-if-applicable-else-null",
  "career_url": "https://full-url-to-jobs-page",
  "confidence": "high | medium | low"
}
"""

SCORE_SYSTEM = """You are a resume-to-job matcher for early-career ML/data roles.
Return ONLY JSON (no markdown):
{
  "score": <integer 0-100>,
  "match_reasons": ["top 3 specific reasons this is a good match"],
  "gap_reasons": ["top 2 specific gaps or concerns"],
  "seniority_fit": "under | good | over",
  "early_career_fit": "strong | moderate | weak"
}

Score rubric:
  90-100: Near-perfect — apply immediately
  70-89:  Strong match — apply with minor tailoring
  50-69:  Partial — worth applying
  <50:    Weak — skip

early_career_fit: is this role genuinely accessible to someone with ~1-2 yrs exp,
or does it functionally require 5+ despite not saying so?
"""

CUSTOM_CAREERS_SYSTEM = """You are a job listing extractor.
Given text from a company careers page, extract ML/data/engineering job listings.
Return ONLY a JSON array (empty array if none found), no markdown:
[
  {
    "title": "exact job title",
    "location": "location or Remote",
    "url": "direct application URL if visible, else null",
    "description_snippet": "first 200 chars of description if available, else null"
  }
]
"""


class SearchAgent:
    def __init__(
        self,
        resume: dict,
        consecutive_low_limit: int | None = None,
        low_score_threshold: int | None = None,
    ):
        self.resume = resume
        self.resume_summary = self._build_resume_summary()
        # Per-run guardrail config. Module-level constants are defaults so
        # callers can keep relying on the global tuning unless they pass an
        # override.
        self.consecutive_low_limit = (
            EARLY_EXIT_CONSECUTIVE_LOW if consecutive_low_limit is None else int(consecutive_low_limit)
        )
        self.low_score_threshold = (
            EARLY_EXIT_SCORE_THRESHOLD if low_score_threshold is None else int(low_score_threshold)
        )
        self.claude_failures = 0
        self.last_run_stats: dict = {}

    def _record_claude_failure(self) -> None:
        self.claude_failures += 1

    def _build_resume_summary(self) -> str:
        r = self.resume
        skills = [item for cat in r["skills"].values() for item in cat["items"]]
        bullets = [b["text"] for exp in r["experience"] for b in exp["bullets"]]
        return json.dumps({
            "name": r["contact"]["name"],
            "current_title": r["contact"]["title"],
            "skills": skills,
            "recent_experience_bullets": bullets[:8],
            "education": [f"{e['degree']} {e['field']}" for e in r["education"]],
            "certifications": [c["name"] for c in r["certifications"]]
        })

    # ── Company Discovery ─────────────────────────────────────────────────────

    async def _discover_companies(self, roles: list[str]) -> list[dict]:
        """Ask Claude to research and recommend companies currently hiring."""
        print("  🌐 Discovering companies via Claude research...")
        today = datetime.now().strftime("%Y-%m-%d")
        prompt = json.dumps({
            "today": today,
            "target_roles": roles,
            "candidate": json.loads(self.resume_summary)
        }, indent=2)

        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=8000,
                system=DISCOVERY_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
        except Exception as e:
            self._record_claude_failure()
            print(f"  ❌ Company discovery call failed: {e}")
            return []
        try:
            companies = loads_llm_json(response.content[0].text)
        except ValueError as e:
            self._record_claude_failure()
            print(f"  ❌ Failed to parse company discovery JSON: {e}")
            return []
        if not isinstance(companies, list):
            print("  ❌ Discovery model did not return a JSON array.")
            return []
        print(f"  📍 Discovered {len(companies)} companies")
        return companies

    async def _research_adhoc_company(self, company_name: str) -> dict:
        """Research a single named company and detect its ATS."""
        print(f"  🔎 Researching: {company_name}")
        response = None
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                system=ATS_DETECT_SYSTEM,
                messages=[{"role": "user", "content": f"Company: {company_name}"}]
            )
        except Exception as e:
            self._record_claude_failure()
            print(f"    ⚠️  ATS detect call failed for {company_name}: {e}")
        info: dict = {}
        if response is not None:
            try:
                info = loads_llm_json(response.content[0].text)
            except ValueError as e:
                self._record_claude_failure()
                print(f"    ⚠️  ATS detect JSON invalid for {company_name}: {e}")
                info = {}
        if not isinstance(info, dict):
            info = {}
        return {
            "name": company_name,
            "ats": info.get("ats", "custom"),
            "slug": info.get("slug"),
            "career_url": info.get("career_url", ""),
            "why_good_fit": "Ad-hoc — user requested",
            "ml_maturity": "unknown",
            "stage": "unknown"
        }

    # ── ATS Fetchers ──────────────────────────────────────────────────────────

    async def _fetch_greenhouse(self, company: dict) -> list[dict]:
        slug = company.get("slug") or company["name"].lower().replace(" ", "-")
        url = f"https://boards.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(url)
                if resp.status_code != 200:
                    return []
                payload = resp.json()
            return [
                {
                    "title": j.get("title", ""),
                    "company": company["name"],
                    "location": j.get("location", {}).get("name", "Remote"),
                    "url": j.get("absolute_url", ""),
                    "description": j.get("content", "")[:2000],
                    "source": "greenhouse",
                    "company_meta": {
                        "why_good_fit": company.get("why_good_fit"),
                        "ml_maturity": company.get("ml_maturity"),
                        "stage": company.get("stage")
                    }
                }
                for j in payload.get("jobs", [])
            ]
        except Exception as e:
            print(f"    ⚠️  Greenhouse failed ({slug}): {e}")
            return []

    async def _fetch_lever(self, company: dict) -> list[dict]:
        slug = company.get("slug") or company["name"].lower().replace(" ", "-")
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(url)
                if resp.status_code != 200:
                    return []
                payload = resp.json()
            return [
                {
                    "title": j.get("text", ""),
                    "company": company["name"],
                    "location": j.get("categories", {}).get("location", "Remote"),
                    "url": j.get("hostedUrl", ""),
                    "description": j.get("descriptionPlain", "")[:2000],
                    "source": "lever",
                    "company_meta": {
                        "why_good_fit": company.get("why_good_fit"),
                        "ml_maturity": company.get("ml_maturity"),
                        "stage": company.get("stage")
                    }
                }
                for j in payload
            ]
        except Exception as e:
            print(f"    ⚠️  Lever failed ({slug}): {e}")
            return []

    async def _fetch_ashby(self, company: dict) -> list[dict]:
        slug = company.get("slug") or company["name"].lower().replace(" ", "-")
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(url)
                if resp.status_code != 200:
                    return []
                payload = resp.json()
            return [
                {
                    "title": j.get("title", ""),
                    "company": company["name"],
                    "location": j.get("locationName", "Remote"),
                    "url": j.get("jobUrl", ""),
                    "description": j.get("descriptionHtml", "")[:2000],
                    "source": "ashby",
                    "company_meta": {
                        "why_good_fit": company.get("why_good_fit"),
                        "ml_maturity": company.get("ml_maturity"),
                        "stage": company.get("stage")
                    }
                }
                for j in payload.get("jobs", [])
            ]
        except Exception as e:
            print(f"    ⚠️  Ashby failed ({slug}): {e}")
            return []

    async def _fetch_custom(self, company: dict) -> list[dict]:
        """Scrape a custom careers page and extract listings via Claude."""
        career_url = company.get("career_url", "")
        if not career_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
                resp = await http.get(career_url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    return []

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)[:5000]

            try:
                response = await client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=800,
                    system=CUSTOM_CAREERS_SYSTEM,
                    messages=[{"role": "user", "content": f"Company: {company['name']}\n\n{text}"}]
                )
            except Exception as e:
                self._record_claude_failure()
                print(f"    ⚠️  Custom listings Claude call failed ({company['name']}): {e}")
                return []
            try:
                listings = loads_llm_json(response.content[0].text)
            except ValueError as e:
                self._record_claude_failure()
                print(f"    ⚠️  Custom listings JSON invalid ({company['name']}): {e}")
                return []
            if not isinstance(listings, list):
                print(f"    ⚠️  Custom listings model did not return an array ({company['name']}).")
                return []
            return [
                {
                    **j,
                    "company": company["name"],
                    "source": "custom",
                    "company_meta": {
                        "why_good_fit": company.get("why_good_fit"),
                        "ml_maturity": company.get("ml_maturity"),
                        "stage": company.get("stage")
                    }
                }
                for j in listings
            ]
        except Exception as e:
            print(f"    ⚠️  Custom scrape failed ({company['name']}): {e}")
            return []

    async def _fetch_company_jobs(self, company: dict) -> list[dict]:
        ats = company.get("ats", "custom")
        dispatchers = {
            "greenhouse": self._fetch_greenhouse,
            "lever": self._fetch_lever,
            "ashby": self._fetch_ashby,
        }
        fetcher = dispatchers.get(ats, self._fetch_custom)
        return await fetcher(company)

    # ── Scoring ───────────────────────────────────────────────────────────────

    async def _score_job(self, job: dict) -> dict:
        title = job.get("title") or "Unknown role"
        company = job.get("company") or "Unknown company"
        prompt = f"""Resume:
{self.resume_summary}

Job: {title} @ {company}
Location: {job.get('location', 'Unknown')}
Company context: {json.dumps(job.get('company_meta', {}))}
Description:
{job.get('description', '')[:1500]}
"""
        try:
            response = await client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=400,
                system=SCORE_SYSTEM,
                messages=[{"role": "user", "content": prompt}]
            )
            scoring = loads_llm_json(response.content[0].text)
            if not isinstance(scoring, dict):
                raise ValueError("scoring payload is not a JSON object")
            return {**job, **scoring, "scoring_failed": False}
        except Exception as e:
            self._record_claude_failure()
            print(f"    ⚠️  Scoring failed ({title}): {e}")
            return {
                **job,
                "score": 0,
                "match_reasons": [],
                "gap_reasons": [],
                "scoring_failed": True,
            }

    # ── Main Entry ────────────────────────────────────────────────────────────

    async def run(
        self,
        roles: list[str],
        adhoc_companies: list[str] = None,
        min_score: int = 50
    ) -> list[dict]:
        """
        Full pipeline:
          1. Discover companies via Claude research
          2. Merge in any ad-hoc companies from --company flag
          3. Fetch open roles from each company's ATS
          4. Filter to target roles
          5. Extract required/preferred skills for gap analysis
          6. Score each against resume (with failure-aware early-exit)
          7. Return sorted results (orchestrator persists to SQLite)
        """
        self.claude_failures = 0
        self.last_run_stats = {
            "companies_discovered": 0,
            "companies_processed": 0,
            "raw_postings": 0,
            "postings_after_role_filter": 0,
            "jobs_scored": 0,
            "qualified_jobs": 0,
            "early_exit_triggered": False,
            "low_score_threshold": self.low_score_threshold,
            "consecutive_low_score_limit": self.consecutive_low_limit,
            "claude_failures": 0,
        }

        # 1. Discover
        companies = await self._discover_companies(roles)
        if not companies:
            print("  ⚠️  No companies discovered — aborting search run.")
            self.last_run_stats["claude_failures"] = self.claude_failures
            return []
        self.last_run_stats["companies_discovered"] = len(companies)

        # 2. Ad-hoc merge
        if adhoc_companies:
            print(f"\n  ➕ Researching {len(adhoc_companies)} ad-hoc companies...")
            adhoc = await asyncio.gather(
                *[self._research_adhoc_company(n) for n in adhoc_companies]
            )
            companies.extend(adhoc)
            print(f"  📍 Total companies: {len(companies)}")
        self.last_run_stats["companies_processed"] = len(companies)

        # 3. Fetch jobs
        print(f"\n  📥 Fetching from {len(companies)} companies...")
        all_jobs: list[dict] = []
        for batch in await asyncio.gather(*[self._fetch_company_jobs(c) for c in companies]):
            all_jobs.extend(batch)
        print(f"  📋 Raw postings: {len(all_jobs)}")
        self.last_run_stats["raw_postings"] = len(all_jobs)

        # 4. Role filter
        keywords = [r.lower() for r in roles]
        filtered = [
            j for j in all_jobs
            if any(kw in (j.get("title") or "").lower() for kw in keywords)
        ]
        print(f"  🎯 After role filter: {len(filtered)}")
        self.last_run_stats["postings_after_role_filter"] = len(filtered)
        if not filtered:
            self.last_run_stats["claude_failures"] = self.claude_failures
            return []

        # 5. Skill lists for downstream gap analysis
        print("  🧠 Extracting skills from posting text (LLM)...")
        filtered = await enrich_jobs_skill_lists(filtered, persist_store=None)

        # 6. Score (parallel batches, failure-aware streak halt)
        #    Batch size is independent of the streak threshold — see SCORING_BATCH_SIZE.
        print(
            f"  ⚡ Scoring up to {len(filtered)} postings "
            f"(early-exit after {self.consecutive_low_limit} consecutive "
            f"scores < {self.low_score_threshold}, batch={SCORING_BATCH_SIZE})..."
        )
        scored: list[dict] = []
        consecutive_low = 0
        halted = False
        for batch_start in range(0, len(filtered), SCORING_BATCH_SIZE):
            batch = filtered[batch_start:batch_start + SCORING_BATCH_SIZE]
            batch_scored = await asyncio.gather(*[self._score_job(j) for j in batch])
            for job in batch_scored:
                scored.append(job)
                self.last_run_stats["jobs_scored"] += 1
                try:
                    score_val = float(job.get("score") or 0)
                except (TypeError, ValueError):
                    score_val = 0.0
                scoring_failed = bool(job.get("scoring_failed"))

                if scoring_failed:
                    # Failures are tracked via claude_failures; do NOT advance
                    # the low-signal streak counter — that would let API
                    # outages masquerade as "weak postings" and stop runs
                    # prematurely.
                    print(
                        "    ↳ Scoring failure encountered; excluded from low-signal streak."
                    )
                    continue

                if score_val < self.low_score_threshold:
                    consecutive_low += 1
                    print(
                        f"    ↳ Low score streak: {consecutive_low}/"
                        f"{self.consecutive_low_limit} "
                        f"(score={score_val:.0f}, threshold={self.low_score_threshold})"
                    )
                    if consecutive_low >= self.consecutive_low_limit:
                        self.last_run_stats["early_exit_triggered"] = True
                        remaining = len(filtered) - len(scored)
                        print(
                            f"  ⛔ Early exit: {consecutive_low} consecutive postings "
                            f"scored < {self.low_score_threshold}. "
                            f"Halting after {len(scored)} of {len(filtered)} "
                            f"({remaining} skipped)."
                        )
                        halted = True
                        break
                else:
                    consecutive_low = 0
            if halted:
                break

        # 7. Return qualified (persistence handled by orchestrator)
        qualified = sorted(
            [j for j in scored if j.get("score", 0) >= min_score],
            key=lambda x: x.get("score", 0),
            reverse=True
        )
        self.last_run_stats["qualified_jobs"] = len(qualified)
        self.last_run_stats["claude_failures"] = self.claude_failures
        print(f"  ✅ Qualified (score ≥ {min_score}): {len(qualified)}")
        return qualified
