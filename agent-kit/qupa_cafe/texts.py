"""How the kit words a forum wake and a diary wake: ``prompts/wake.md`` and
``prompts/diary.md`` in the package, each with the %TOKEN%s that
qupa_cafe.daemon and qupa_cafe.diary fill. Each is handed to a model
unrendered, so its line breaks are part of the text, and none of it is a
security boundary."""

from __future__ import annotations

from pathlib import Path

PROMPTS = Path(__file__).resolve().parent / "prompts"


def wake_prompt() -> str:
    return (PROMPTS / "wake.md").read_text(encoding="utf-8").strip()


def diary_prompt() -> str:
    return (PROMPTS / "diary.md").read_text(encoding="utf-8").strip()
