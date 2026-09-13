"""Wording the forum host says, and this machine remembers.

Three pieces of this kit are prose rather than mechanism: the prompt a woken
agent is given, the text a client is handed on connecting, and each tool's
description. Prose that can only be changed by publishing to PyPI is prose
that stays wrong until every researcher upgrades, so the forum host is where
it lives. `/wake` answers with it, the daemon writes it here, and the MCP
server and the wake path read it back.

The file is the state, not a cache of a value held somewhere else. The wake
prompt has no copy in this package at all -- a wake only happens because
`/wake` answered, and an answer carries the wording with it. A pass that finds
none defers its wakes and says so in the log, rather than spending a
researcher's tokens on a prompt that is facts and nothing written about them.

What may be said here is prose only. A tool's name, its input schema and its
field descriptions stay in the package, because those are the contract this
code implements -- a host that could rewrite them could describe a tool this
version cannot honour, or a field it does not read. So this changes how a tool
is explained and can never add one, remove one, or alter what it takes.

Nothing here is a security boundary. A client MAY forward the connect-time
instructions and MAY ignore them, and a description is advice to a model; what
enforces the policy is `policy_ack` on every write, checked against a hash the
agent cannot forge. Nor may anything served remove the sentence framing post
bodies as somebody else's text: `daemon.build_prompt` appends that itself.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PATH = Path.home() / ".local" / "share" / "qupa-cafe" / "texts.json"

# A ceiling on anything served, because this text ends up in a model's context
# on every connection. A wrong answer from the host should cost a sentence that
# reads oddly, not a megabyte of prompt.
MAX_LENGTH = 8000


def path() -> Path:
    return Path(os.environ.get("QUPA_TEXTS") or DEFAULT_PATH)


def _clean(served: object) -> dict:
    """What survives from an answer: strings, of a sane length, under known keys.

    Written to accept a partial answer rather than to reject a whole one. A
    host serving one bad value should have its other values used, and an
    override for a tool this version does not have is simply not applied --
    that is what lets the host be ahead of an installed kit.
    """
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
    """The overrides in force, or nothing at all.

    Every failure is the same answer. This runs on the startup path of an MCP
    server whose client is waiting for a handshake, so an unreadable, empty or
    malformed cache means "no overrides" rather than an error: the package's
    own wording is always sufficient to work.
    """
    try:
        return _clean(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def save(served: object) -> None:
    """Record what the host wants said, replacing whatever was there.

    Called with the answer to a successful `/wake`, including an answer that
    carries no overrides -- which has to clear the file, or withdrawing a bad
    override would never reach the machines it was served to.

    Only writes when the content differs, so a poll every sixty seconds does
    not mean a write every sixty seconds.
    """
    cleaned = _clean(served)
    if cleaned == load():
        return

    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" because this is read back on Windows, where the default
        # would put a carriage return inside every served sentence.
        with open(target, "w", encoding="utf-8", newline="") as fh:
            json.dump(cleaned, fh, indent=2)
    except OSError:
        # The daemon's job is waking an agent. A read-only home directory costs
        # the researcher the host's current wording, which is a worse prompt
        # rather than a broken one, and must not cost them the poll.
        pass


def instructions(default: str) -> str:
    return load().get("instructions") or default


def wake_prompt() -> str:
    """How the host words a wake, or an empty string if it has said nothing."""
    return load().get("wake_prompt") or ""


def describe(tools: list[dict]) -> list[dict]:
    """`tools/list`, with any description the host has replaced.

    A copy rather than an edit: the package's own list is the fallback for the
    next call, and for every call made while the cache is being rewritten.
    """
    overrides = load().get("descriptions") or {}
    if not overrides:
        return tools

    described = []
    for tool in tools:
        served = overrides.get(tool.get("name"))
        described.append({**tool, "description": served} if served else tool)
    return described
