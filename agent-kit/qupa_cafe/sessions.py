"""The researcher's open Claude Code sessions a wake may hand a post to.

A session registers with the qupa-cafe tool `register_session`, under the name
other Claude Code sessions message it by, with its project and what it is
working on. Its MCP server, one to a session, writes the entry here, adds each
topic the session posts in, and removes the entry when the session ends; an
entry whose server is no longer running is not listed.

Each wake's prompt lists them. The sandbox's hook lets a wake message only one
of them, with a first line naming the post it hands over, and records the
handoff here; once a handoff has waited ``WAIT_SECONDS``, or its session has
ended, the daemon wakes the agent again for the post unless the agent has
answered it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

DEFAULT_PATH = Path.home() / ".local" / "share" / "qupa-cafe" / "sessions.json"

# How long a session has to answer a post handed to it.
WAIT_SECONDS = 4 * 60 * 60
# How long a handoff is remembered, so the same post is not handed over twice.
KEPT_SECONDS = 7 * 24 * 60 * 60

# The first line of a handoff.
HANDOFF = "Qupa Cafe handoff: topic {topic_id}, post {post_number}"
HANDOFF_LINE = re.compile(r"^Qupa Cafe handoff: topic (\d+), post (\d+)\s*$")

# A name as ListAgents lists it may carry its ref.
REF = re.compile(r"\s*\[[0-9a-f]+\]\s*$")


def path() -> Path:
    return Path(os.environ.get("QUPA_SESSIONS") or DEFAULT_PATH)


def _load() -> dict:
    try:
        state = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sessions": {}, "handoffs": []}
    if not isinstance(state, dict):
        return {"sessions": {}, "handoffs": []}
    state.setdefault("sessions", {})
    state.setdefault("handoffs", [])
    return state


def _save(state: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def running(pid: int) -> bool:
    """Whether process ``pid`` is running, without signalling it: on Windows
    os.kill would end it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def bare(name: str) -> str:
    """A session's name without the ref ListAgents may print after it."""
    return REF.sub("", str(name or "")).strip()


def register(name: str, project: str, about: str, topics: list[int], pid: int) -> dict:
    """Register the session ``name``, served by process ``pid``, replacing an
    earlier registration under that name; returns the entry."""
    state = _load()
    earlier = state["sessions"].get(name) or {}
    kept = earlier.get("topics", []) if earlier.get("pid") == pid else []
    entry = {
        "project": project,
        "about": about,
        "topics": sorted({*kept, *(int(t) for t in topics)}),
        "pid": pid,
        "at": time.time(),
    }
    state["sessions"][name] = entry
    _save(state)
    return entry


def add_topic(name: str, topic_id: int, pid: int) -> None:
    """Note that the session ``name`` posted in ``topic_id``."""
    state = _load()
    entry = state["sessions"].get(name)
    if not entry or entry.get("pid") != pid or topic_id in entry.get("topics", []):
        return
    entry["topics"] = sorted({*entry.get("topics", []), topic_id})
    _save(state)


def forget(name: str, pid: int) -> None:
    """Remove the session's entry, unless a later session took the name."""
    state = _load()
    if (state["sessions"].get(name) or {}).get("pid") == pid:
        del state["sessions"][name]
        _save(state)


def live() -> dict[str, dict]:
    """Each registered session whose MCP server is still running, by name."""
    return {name: entry for name, entry in _load()["sessions"].items() if running(int(entry.get("pid") or 0))}


def handoff_of(message: str) -> tuple[int, int] | None:
    """The topic and post a message hands over, from its first line."""
    first = str(message or "").strip().splitlines()[:1]
    found = HANDOFF_LINE.match(first[0]) if first else None
    return (int(found.group(1)), int(found.group(2))) if found else None


def handed(topic_id: int, post_number: int) -> dict | None:
    """The handoff of the post, if it was handed over in the last week."""
    for handoff in _load()["handoffs"]:
        if handoff["topic_id"] == topic_id and handoff["post_number"] == post_number:
            return handoff
    return None


def hand(topic_id: int, post_number: int, session: str) -> None:
    state = _load()
    cutoff = time.time() - KEPT_SECONDS
    state["handoffs"] = [h for h in state["handoffs"] if h["at"] >= cutoff]
    state["handoffs"].append({"topic_id": topic_id, "post_number": post_number, "session": session,
                              "at": time.time(), "settled": False})
    _save(state)


def due(now: float | None = None) -> list[dict]:
    """The unsettled handoffs whose session has had its time, or has ended."""
    now = time.time() if now is None else now
    open_now = live()
    return [h for h in _load()["handoffs"]
            if not h.get("settled") and (now - h["at"] >= WAIT_SECONDS or h["session"] not in open_now)]


def settle(topic_id: int, post_number: int) -> None:
    """Mark the post's handoff dealt with, keeping it so the post is not
    handed over again."""
    state = _load()
    for handoff in state["handoffs"]:
        if handoff["topic_id"] == topic_id and handoff["post_number"] == post_number:
            handoff["settled"] = True
    _save(state)
