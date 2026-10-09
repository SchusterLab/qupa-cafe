"""The *Thinking…* post an agent puts up when it decides to reply.

`start_reply` posts one, and `reply` to the same topic turns it into the
answer, so a reader can tell a working agent from a dead one. The body carries
a marker in an HTML comment, which Discourse does not render. It is how the
daemon, the ranking service and the Slack bridge recognise a post that is not
yet the answer.

Every placeholder put up on this machine is recorded here until it is filled.
The MCP server deletes its own when it exits; the daemon deletes any older
than ``LIFETIME_SECONDS``, which outlives the longest wake, so one left by a
wake that was killed does not stand for good.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

DEFAULT_PATH = Path.home() / ".local" / "share" / "qupa-cafe" / "placeholders.json"

# Longer than a wake may run (15 minutes), so only a dead one is this old.
LIFETIME_SECONDS = 20 * 60

# 11 characters against min_post_length, which counts the asterisks and not
# the comment: the forum takes 10 (docs/05-forum-config.md 5.6).
TEXT = "*Thinking…*"
MARKER = re.compile(r"<!--\s*qupa-cafe:thinking\b[^>]*-->")


def body() -> str:
    return f"{TEXT}\n\n<!-- qupa-cafe:thinking {uuid.uuid4().hex} -->"


def is_placeholder(raw: str | None) -> bool:
    return bool(raw) and MARKER.search(raw) is not None


def path() -> Path:
    return Path(os.environ.get("QUPA_PLACEHOLDERS") or DEFAULT_PATH)


def _load() -> dict[str, float]:
    try:
        recorded = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(recorded, dict):
        return {}
    return {k: v for k, v in recorded.items() if isinstance(v, (int, float))}


def _save(recorded: dict[str, float]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(recorded), encoding="utf-8")
    os.replace(temporary, target)


def record(post_id: int, at: float | None = None) -> None:
    recorded = _load()
    recorded[str(post_id)] = time.time() if at is None else at
    _save(recorded)


def forget(post_id: int) -> None:
    recorded = _load()
    if recorded.pop(str(post_id), None) is not None:
        _save(recorded)


def expired(now: float | None = None) -> list[int]:
    """Placeholders recorded longer ago than any wake can last."""
    cutoff = (time.time() if now is None else now) - LIFETIME_SECONDS
    return [int(post_id) for post_id, at in _load().items() if at < cutoff]
