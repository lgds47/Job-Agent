"""
Classify Anthropic and pipeline failures for run_history metadata.

Only model-output / JSON-parse failures should increment ``claude_failures``.
API billing, auth, and rate-limit errors are tracked separately via ``error_type``.
"""

from __future__ import annotations

from typing import Any

import httpx
from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)

# Run status labels written by orchestrator handlers
RUN_STATUS_FAILED = "failed"
RUN_STATUS_DEGRADED = "degraded"


class JobNotFoundError(Exception):
    """ATS public API returned 404 for the resolved job id."""


def counts_as_claude_failure(exc: BaseException) -> bool:
    """True when the model returned unusable output or JSON repair failed."""
    if isinstance(exc, ValueError) and "invalid JSON" in str(exc):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        if "json" in msg or "ranking" in msg or "expected" in msg:
            return True
    return False


def classify_api_error(exc: BaseException) -> dict[str, Any]:
    """
    Map an exception to structured metadata fields and error_type.

    Returns a dict suitable for merging into ``record_run(..., metadata=...)``.
    Keys: error_type, user_message, http_status, request_id, phase (optional).
    """
    if isinstance(exc, JobNotFoundError):
        return {
            "error_type": "job_not_found",
            "user_message": str(exc) or "Job posting not found (ATS returned 404).",
        }

    if isinstance(exc, httpx.HTTPStatusError):
        return {
            "error_type": "jd_fetch_failed",
            "user_message": f"Failed to fetch job URL (HTTP {exc.response.status_code}).",
            "http_status": exc.response.status_code,
        }

    if isinstance(exc, httpx.RequestError):
        return {
            "error_type": "jd_fetch_failed",
            "user_message": f"Failed to fetch job URL: {exc}",
        }

    if isinstance(exc, ValueError):
        msg = str(exc)
        if "invalid JSON" in msg or "expected JSON" in msg:
            return {
                "error_type": "jd_parse_invalid_json",
                "user_message": msg,
            }
        if "parse_jd" in msg:
            return {
                "error_type": "jd_parse_invalid_json",
                "user_message": msg,
            }
        return {
            "error_type": "validation_error",
            "user_message": msg,
        }

    if isinstance(exc, APIStatusError):
        meta: dict[str, Any] = {
            "user_message": exc.message,
            "http_status": exc.status_code,
            "request_id": getattr(exc, "request_id", None),
        }
        err_type = getattr(exc, "type", None)
        if err_type:
            meta["api_error_type"] = err_type

        if isinstance(exc, AuthenticationError):
            meta["error_type"] = "auth"
        elif isinstance(exc, PermissionDeniedError):
            meta["error_type"] = "auth"
        elif isinstance(exc, RateLimitError):
            meta["error_type"] = "rate_limit"
        elif isinstance(exc, BadRequestError):
            msg_lower = (exc.message or "").lower()
            body_msg = ""
            if isinstance(exc.body, dict):
                err = exc.body.get("error")
                if isinstance(err, dict):
                    body_msg = str(err.get("message", "")).lower()
            if err_type == "billing_error" or "credit balance" in msg_lower or "credit balance" in body_msg:
                meta["error_type"] = "billing"
            elif err_type == "invalid_request_error":
                meta["error_type"] = "model_invalid_request"
            else:
                meta["error_type"] = "model_invalid_request"
        elif err_type == "billing_error":
            meta["error_type"] = "billing"
        elif err_type == "rate_limit_error":
            meta["error_type"] = "rate_limit"
        elif err_type in ("authentication_error", "permission_error"):
            meta["error_type"] = "auth"
        elif err_type == "overloaded_error":
            meta["error_type"] = "rate_limit"
        else:
            meta["error_type"] = "api_error"
        return meta

    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return {
            "error_type": "api_connection",
            "user_message": str(exc),
        }

    if isinstance(exc, APIError):
        return {
            "error_type": "api_error",
            "user_message": exc.message,
        }

    return {
        "error_type": "unknown",
        "user_message": str(exc),
    }


def failure_metadata(
    exc: BaseException,
    *,
    phase: str | None = None,
    url: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build normalized metadata for ``record_run`` from an exception."""
    meta = classify_api_error(exc)
    if phase:
        meta["phase"] = phase
    if url:
        meta["url"] = url
    if extra:
        meta.update(extra)
    # Legacy single-line error for older dashboards
    meta["error"] = meta.get("user_message") or str(exc)
    return meta


def claude_failure_count(exc: BaseException) -> int:
    return 1 if counts_as_claude_failure(exc) else 0
