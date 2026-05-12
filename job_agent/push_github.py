#!/usr/bin/env python3
"""
Upload this directory's tracked files to https://github.com/lgds47/Job-Agent

Requires a GitHub personal access token (fine-grained or classic) with
**Contents: Read and write** on the Job-Agent repository.

Usage:
  export GITHUB_TOKEN=github_pat_...   # or classic ghp_...
  python3 push_github.py

Optional: put the token in a file (never commit it):
  echo 'github_pat_...' > .github_token && chmod 600 .github_token
  python3 push_github.py   # reads .github_token if GITHUB_TOKEN unset
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "lgds47"
REPO = "Job-Agent"
BRANCH = "main"
COMMIT_PREFIX = "Sync job_agent pipeline (skills extraction, SQLite wiring, robustness)"

LOCAL_ROOT = Path(__file__).resolve().parent
TOKEN_FILE = LOCAL_ROOT / ".github_token"

# Relative paths under LOCAL_ROOT to upload -> GitHub API path (None = skip)
# README at repo root matches current GitHub layout.
UPLOAD_MAP: list[tuple[str, str]] = [
    ("README.md", "README.md"),
    ("orchestrator.py", "job_agent/orchestrator.py"),
    ("requirements.txt", "job_agent/requirements.txt"),
    ("agents/search_agent.py", "job_agent/agents/search_agent.py"),
    ("agents/resume_agent.py", "job_agent/agents/resume_agent.py"),
    ("agents/cover_letter_agent.py", "job_agent/agents/cover_letter_agent.py"),
    ("agents/project_planner_agent.py", "job_agent/agents/project_planner_agent.py"),
    ("agents/project_builder_agent.py", "job_agent/agents/project_builder_agent.py"),
    ("tools/state_store.py", "job_agent/tools/state_store.py"),
    ("tools/jd_parser.py", "job_agent/tools/jd_parser.py"),
    ("tools/job_skills.py", "job_agent/tools/job_skills.py"),
    ("tools/llm_json.py", "job_agent/tools/llm_json.py"),
    ("tools/text_sanitize.py", "job_agent/tools/text_sanitize.py"),
    ("data/luke_ganalon_resume.json", "job_agent/data/luke_ganalon_resume.json"),
]


def _token() -> str:
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    if TOKEN_FILE.is_file():
        return TOKEN_FILE.read_text().strip()
    print(
        "Missing GITHUB_TOKEN. Export it or create ./.github_token (chmod 600).\n"
        "Fine-grained PAT: Resource=Repository, Job-Agent only, "
        "Permissions=Contents Read and write.",
        file=sys.stderr,
    )
    sys.exit(1)


def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "Job-Agent-push-github.py")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {err}") from e


def _get_sha(path: str, token: str) -> str | None:
    try:
        meta = _request("GET", path, token)
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    return meta.get("sha")


def _put_file(local_rel: str, github_path: str, token: str) -> None:
    local_path = LOCAL_ROOT / local_rel
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    content = local_path.read_bytes()
    b64 = base64.b64encode(content).decode("ascii")
    sha = _get_sha(github_path, token)
    body: dict = {
        "message": f"{COMMIT_PREFIX}: {github_path}",
        "content": b64,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha
    _request("PUT", github_path, token, body)
    print(f"  OK  {local_rel} -> {github_path}")


def main() -> None:
    token = _token()
    print(f"Pushing {len(UPLOAD_MAP)} file(s) to {OWNER}/{REPO} ({BRANCH})...")
    for local_rel, gh_path in UPLOAD_MAP:
        _put_file(local_rel, gh_path, token)
    print("Done. Open https://github.com/lgds47/Job-Agent/commits/main to verify.")


if __name__ == "__main__":
    main()
