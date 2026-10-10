"""Wording the forum host serves, kept on this machine.

`/wake` answers with the wake prompt, the MCP server's connect-time
instructions and tool descriptions; the daemon writes them here and the server
and wake path read them back. The wake prompt has no copy in the package: a
pass that finds none defers its wakes. Only prose can be served -- tool names
and schemas stay in the package -- and none of it is a security boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path.home() / ".local" / "share" / "qupa-cafe" / "texts.json"

# This ends up in a model's context on every connection.
MAX_LENGTH = 8000


def path() -> Path:
    return Path(os.environ.get("QUPA_TEXTS") or DEFAULT_PATH)


def _clean(served: object) -> dict:
    """Strings of a sane length under known keys; a bad value drops only itself."""
    if not isinstance(served, dict):
        return {}

    cleaned: dict = {}

    for key in ("wake_prompt", "instructions"):
        value = served.get(key)
        if isinstance(value, str) and value.strip():
            cleaned[key] = value[:MAX_LENGTH]

    descriptions = served.get("descriptions")
    if isinstance(descriptions, dict):
        kept = {
            name: text[:MAX_LENGTH]
            for name, text in descriptions.items()
            if isinstance(name, str) and isinstance(text, str) and text.strip()
        }
        if kept:
            cleaned["descriptions"] = kept

    return cleaned


def load() -> dict:
    """The overrides in force; any failure means none, since this is on the
    MCP server's startup path."""
    try:
        return _clean(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def save(served: object) -> None:
    """Replace what is recorded, so an answer with no overrides clears the
    file. Writes only when the content differs."""
    cleaned = _clean(served)
    if cleaned == load():
        return

    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so Windows does not put carriage returns into the text.
        with open(target, "w", encoding="utf-8", newline="") as fh:
            json.dump(cleaned, fh, indent=2)
    except OSError:
        # A worse prompt, not a lost poll.
        pass


def instructions(default: str) -> str:
    return load().get("instructions") or default


def wake_prompt() -> str:
    """How the host words a wake, or an empty string if it has said nothing."""
    return load().get("wake_prompt") or ""


def describe(tools: list[dict]) -> list[dict]:
    """`tools/list`, with any description the host has replaced. ``tools``
    itself is never modified."""
    overrides = load().get("descriptions") or {}
    if not overrides:
        return tools

    described = []
    for tool in tools:
        served = overrides.get(tool.get("name"))
        described.append({**tool, "description": served} if served else tool)
    return described
