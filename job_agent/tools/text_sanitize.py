"""
Strip markdown/code fences from generated file content.
"""

from __future__ import annotations


def strip_code_fences(text: str) -> str:
    """If the model wrapped content in ``` or ```python fences, remove them."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if not lines:
        return t
    # Drop opening fence
    first = lines[0].strip("`")
    if first.lower() in ("", "python", "py", "yaml", "text", "markdown", "md", "json"):
        lines = lines[1:]
    # Drop closing fence
    while lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
