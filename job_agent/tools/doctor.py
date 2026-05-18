"""
Environment and configuration checks (no discovery / scoring spend).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RESUME_REQUIRED_TOP = (
    "contact",
    "summary",
    "agent_metadata",
    "skills",
    "experience",
    "education",
    "certifications",
)


def run_doctor(*, resume_path: Path, check_api: bool = True) -> int:
    """Print diagnostics; return 0 if healthy, 1 if actionable issues found."""
    ok = True

    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 10):
        print("  ✗ Python 3.10+ required")
        ok = False
    else:
        print("  ✓ Python version OK")

    if not resume_path.exists():
        print(f"  ✗ Resume missing: {resume_path}")
        ok = False
    else:
        print(f"  ✓ Resume file: {resume_path}")
        try:
            data = json.loads(resume_path.read_text())
        except (OSError, ValueError) as e:
            print(f"  ✗ Resume JSON invalid: {e}")
            ok = False
            data = {}
        if data:
            missing = [k for k in RESUME_REQUIRED_TOP if k not in data]
            if missing:
                print(f"  ✗ Resume missing top-level keys: {', '.join(missing)}")
                ok = False
            else:
                if not (data.get("summary") or {}).get("text"):
                    print("  ✗ summary.text is required")
                    ok = False
                roles = (data.get("agent_metadata") or {}).get("target_roles")
                if not roles:
                    print("  ⚠ agent_metadata.target_roles empty (search will skip)")
                else:
                    print(f"  ✓ target_roles: {len(roles)} role(s)")
                bullet_ids = [
                    b.get("id")
                    for exp in data.get("experience") or []
                    for b in (exp.get("bullets") or [])
                ]
                if len(bullet_ids) != len(set(bullet_ids)):
                    print("  ⚠ Duplicate bullet ids — ResumeAgent ranking may break")

    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        print("  ✗ ANTHROPIC_API_KEY not set (env or job_agent/.env)")
        ok = False
    else:
        print("  ✓ ANTHROPIC_API_KEY present")

    if check_api and key:
        ok = _probe_api(key) and ok

    if ok:
        print("\n✅ Doctor: environment looks ready for agent commands.")
    else:
        print(
            "\n❌ Doctor: fix the issues above before search/apply/gaps.\n"
            "   Billing/credits: https://console.anthropic.com/"
        )
    return 0 if ok else 1


def _probe_api(api_key: str) -> bool:
    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=api_key)
        result = client.messages.count_tokens(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "ping"}],
        )
        print(f"  ✓ API reachable (count_tokens input={result.input_tokens})")
        return True
    except Exception as e:
        from tools.api_errors import classify_api_error

        meta = classify_api_error(e)
        et = meta.get("error_type", "api_error")
        print(f"  ✗ API probe failed [{et}]: {meta.get('user_message', e)}")
        if et == "billing":
            print("     Account may be out of credits — add billing at console.anthropic.com")
        return False
