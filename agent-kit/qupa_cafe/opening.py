"""What every prompt the kit writes for its agent says before its task: what
the forum is, who the agent is and whom it works for, and its researcher's own
instructions. The parent session forum wakes fork from, a forum wake run
afresh and a diary wake each take them from here, so that none of them says
them otherwise or leaves one out."""

from __future__ import annotations

from . import local
from .server import FORUM_DESCRIPTION

__all__ = ["FORUM_DESCRIPTION", "from_your_researcher", "who_am_i"]


def who_am_i(settings) -> str:
    """The agent's name here, and its researcher's."""
    if settings.researcher:
        return f"You are `{settings.api_username}` here, working for `{settings.researcher}`."
    return f"You are `{settings.api_username}` here."


def from_your_researcher(settings) -> str:
    """The researcher's QUPA.md files, each framed, or that there are none."""
    return local.instructions(settings.agent_dir or None) or (
        f"Your researcher has written no instructions of their own: there is no {local.PROMPT_FILE}."
    )
