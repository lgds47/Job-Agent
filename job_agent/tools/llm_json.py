"""
Utilities for parsing JSON returned by LLMs (optional markdown fences, clearer errors).
"""

from __future__ import annotations

import json
from typing import Any


def strip_json_fences(raw: str) -> str:
    """Remove common ``` / ```json wrappers from model output."""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    parts = text.split("```")
    inner = parts[1] if len(parts) > 1 else text
    inner = inner.strip()
    if inner.lower().startswith("json"):
        inner = inner[4:].lstrip()
    return inner.strip()


def loads_llm_json(raw: str) -> Any:
    """
    Parse JSON from an LLM message. Raises ValueError with a short snippet on failure.
    """
    text = strip_json_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        snippet = text[:400].replace("\n", " ")
        raise ValueError(f"Model returned invalid JSON ({e.msg} at pos {e.pos}): {snippet!r}...") from e
