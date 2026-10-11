"""What your agent did when it was woken.

On a terminal, the browser in ``qupa_cafe.browse``; with ``--print`` or a pipe,
one block per wake, oldest first. Each wake comes from the daemon's journal and
Claude Code's own transcript of the session it recorded, laid out as Claude
Code lays out a conversation. Read-only. ``qupa_cafe.tray`` opens it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import textwrap
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import daemon, diary
from .daemon import Settings, load_env_file

# A wake is stopped at this age, so one still marked running after it was
# ended with the daemon rather than still going.
WAKE_TIMEOUT_SECONDS = daemon.WAKE_TIMEOUT

# How much of a tool's answer to show.
ITEMS_SHOWN = 6
TEXT_LINES_SHOWN = 6
SNIPPET_CHARS = 90
VALUE_CHARS = 60

# What the agent writes is shown in full: it is what the forum now holds.
WRITE_TOOLS = set(daemon.WRITE_TOOLS)

# Loading a deferred tool's schema, which says nothing about the wake.
NOISE_TOOLS = {"ToolSearch"}

# The fields of a forum answer worth a glance.
INTERESTING = (
    "id",
    "topic_id",
    "post_number",
    "reply_to_post_number",
    "username",
    "name",
    "title",
    "category_id",
    "posts_count",
    "trust_level",
    "admin",
    "moderator",
    "total",
    "has_more",
    "created_at",
)

# Where the words of a post, a hit or a user are, in the order to look.
TEXT_FIELDS = ("raw", "excerpt", "blurb", "bio", "description", "text", "cooked")

# Claude Code's marks; elsewhere than macOS fonts draw "⏺" as a wide emoji.
DOT = "⏺ " if sys.platform == "darwin" else "● "
UNDER = "  ⎿  "
INDENT = "     "
SAID = "  "
PROMPT = "> "


# ------------------------------------------------------------------ colour


# Claude Code's palette, near enough.
STYLES = {
    "accent": "38;2;215;119;87",
    "accent_bold": "1;38;2;215;119;87",
    "rule": "38;5;240",
    "head": "1;38;2;215;119;87",
    "meta": "38;5;245",
    "prompt": "38;5;250",
    "prompt_block": "38;5;252;48;5;237",
    "says": "97",
    "think": "3;38;5;245",
    "tool": "1",
    "key": "38;5;245",
    "result": "38;5;245",
    "error": "38;5;203",
    "wrote": "38;5;151",
    "yours": "38;5;222",
    "dot_ok": "38;5;78",
    "dot_bad": "38;5;203",
    "dot_none": "38;5;245",
    "good": "1;38;5;78",
    "quiet": "1;38;5;222",
    "bad": "1;38;5;203",
    "busy": "1;38;2;215;119;87",
    "selected": "1",
}

_colour = False


def set_colour(on: bool) -> None:
    global _colour
    _colour = on


def paint(text: str, style: str) -> str:
    if not _colour or not text:
        return text
    return f"\x1b[{STYLES[style]}m{text}\x1b[0m"


def colour_wanted(choice: str, stream) -> bool:
    """Whether to colour, after asking a Windows console to process escapes."""
    if choice == "never":
        return False
    if choice == "auto" and (os.environ.get("NO_COLOR") or not stream.isatty()):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return choice == "always"
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004)) or choice == "always"
    except (AttributeError, OSError):
        return choice == "always"


def width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 24)).columns - 1, 120))


# --------------------------------------------------------------- the journal


@dataclass
class Row:
    started: str
    finished: str | None
    status: str
    session: str
    agent_dir: str
    topic_id: int | None
    post_number: int | None
    title: str
    reason: str
    # The forum host's relevance score; None for a mention.
    score: float | None = None


@dataclass
class Status:
    """The daemon's last poll, and the agent's similarity floor as the forum
    host last stated it, as the journal has them."""

    polled: datetime | None
    interval: int
    floor: float | None = None
    floor_min: float | None = None
    floor_max: float | None = None

    @property
    def stopped(self) -> bool:
        """Three missed polls, so that one slow pass is not a stopped daemon."""
        if self.polled is None:
            return True
        age = (datetime.now(timezone.utc) - self.polled).total_seconds()
        return age > 3 * max(self.interval, 1) + 120


def utc(value: str | None) -> datetime | None:
    """SQLite's ``datetime('now')``, which is UTC with no zone on it."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def young(started: datetime) -> bool:
    """Whether a wake that started then may still be running, being no older
    than any wake may run."""
    return (datetime.now(timezone.utc) - started).total_seconds() <= WAKE_TIMEOUT_SECONDS


def local_time(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else "?"


def connect(state_path: str) -> sqlite3.Connection | None:
    path = Path(state_path)
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


# Which wakes ``recent`` returns: all of them, those for the forum, which the
# wake browser shows, or those for the diary, which the diary browser shows.
ALL, FORUM, DIARY = "all", "forum", "diary"

# How the journal tells a diary wake: no topic, and the diary's reason.
DIARY_WAKE = "(topic_id IS NULL AND reason LIKE ?)"


def is_diary(row: Row) -> bool:
    """Whether a journalled wake is one the diary started."""
    return row.topic_id is None and row.reason.startswith(diary.REASON)


def recent(state_path: str, limit: int | None, which: str = ALL) -> list[Row]:
    """The last ``limit`` wakes of ``which`` kind, or all of them for None,
    oldest first."""
    db = connect(state_path)
    if db is None:
        return []
    where, params = {
        ALL: ("", ()),
        FORUM: (f" WHERE NOT {DIARY_WAKE}", (diary.REASON + "%",)),
        DIARY: (f" WHERE {DIARY_WAKE}", (diary.REASON + "%",)),
    }[which]
    try:
        rows = db.execute(
            "SELECT started, finished, status, session, agent_dir, topic_id,"
            f" post_number, title, reason, score FROM wakes{where} ORDER BY id DESC LIMIT ?",
            (*params, -1 if limit is None else limit),
        ).fetchall()
    except sqlite3.Error:
        # A state file written by a daemon that journals nothing.
        return []
    finally:
        db.close()
    return [Row(*row) for row in reversed(rows)]


@dataclass
class NearMiss:
    """A post that did not wake the agent: it scored under the agent's floor,
    by no more than the forum host's margin; it cleared the floor but matched
    the profile's "Not interested in" at least as well; or it sits in a
    category the profile skips."""

    post_id: int
    topic_id: int
    post_number: int
    title: str
    category: str
    # ISO 8601 with its zone, as the forum host writes it.
    at: str
    similarity: float | None
    floor: float | None
    # "below-floor", "excluded" or "category-skipped".
    reason: str


def near_misses(state_path: str) -> list[NearMiss]:
    """The near misses the daemon last kept, newest first."""
    db = connect(state_path)
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT post_id, topic_id, post_number, title, category, at, similarity, floor, reason"
            " FROM near_misses ORDER BY post_id DESC"
        ).fetchall()
    except sqlite3.Error:
        # A state file written by a daemon that keeps none.
        return []
    finally:
        db.close()
    return [NearMiss(*row) for row in rows]


def status(state_path: str) -> Status:
    db = connect(state_path)
    if db is None:
        return Status(None, 60)
    try:
        row = db.execute("SELECT polled, interval FROM heartbeat WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    try:
        floor = db.execute("SELECT floor, floor_min, floor_max FROM floor WHERE id = 1").fetchone()
    except sqlite3.Error:
        floor = None
    finally:
        db.close()
    polled, interval = (utc(row[0]), int(row[1])) if row else (None, 60)
    return Status(polled, interval, *(floor or (None, None, None)))


def newer_kit(state_path: str) -> str | None:
    """The newer kit the daemon found at its last poll, or None."""
    db = connect(state_path)
    if db is None:
        return None
    try:
        row = db.execute("SELECT newer FROM kit WHERE id = 1").fetchone()
    except sqlite3.Error:
        # A state file from a daemon that records no such thing.
        row = None
    finally:
        db.close()
    return row[0] if row and row[0] else None


# ------------------------------------------------------------- transcripts


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_path(session: str) -> Path | None:
    """Where Claude Code keeps a session, searched for by its unique id."""
    if not session:
        return None
    return next((claude_home() / "projects").glob(f"*/{session}.jsonl"), None)


def short_name(tool: str) -> str:
    for prefix in ("mcp__qupa-cafe__", "mcp__discourse__"):
        if tool.startswith(prefix):
            return tool[len(prefix) :]
    return tool


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def wrap(text: str, first: str, rest: str, style: str, keep_lines: bool = False) -> list[str]:
    """Lay ``text`` out after a mark, ``first`` then ``rest``; ``keep_lines``
    folds only lines too long for the terminal."""
    room = max(width() - len(rest), 20)
    out = []
    for paragraph in text.strip().splitlines() or [""]:
        if keep_lines and len(paragraph) <= room:
            pieces = [paragraph]
        else:
            pieces = textwrap.wrap(paragraph, room) or [""]
        out += [paint(piece, style) for piece in pieces]
    return [(first if i == 0 else rest) + line for i, line in enumerate(out)]


def columns(text: str) -> int:
    """Terminal columns ``text`` takes, wide characters counting two."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def block(lines: list[str], style: str) -> list[str]:
    """Plain lines on a background, each padded to the full width, with a line
    of the background above and below, as Claude Code sets a user's message."""
    full = width()
    padded = [""] + lines + [""]
    return [paint(line + " " * max(full - columns(line), 0), style) for line in padded]


def prompt_lines(prompt: str, full: bool) -> list[str]:
    """The prompt, as ``>`` lines on the grey block."""
    if not full:
        return block([PROMPT + clip(prompt, width() - 3)], "prompt_block")
    room = max(width() - len(PROMPT) - 1, 20)
    lines = []
    for line in prompt.strip().splitlines() or [""]:
        pieces = [line] if columns(line) <= room else (textwrap.wrap(line, room) or [""])
        lines += [PROMPT + piece for piece in pieces]
    return block(lines, "prompt_block")


def under(lines: list[str], style: str) -> list[str]:
    """What came back from a call, under its ``⎿``."""
    return [(paint(UNDER, "rule") if i == 0 else INDENT) + paint(line, style) for i, line in enumerate(lines)]


# ------------------------------------------------------- reading an answer


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def value_text(value) -> str:
    if isinstance(value, str):
        return clip(value, VALUE_CHARS)
    return clip(json.dumps(value, ensure_ascii=False), VALUE_CHARS)


def call_text(tool: str, arguments: dict) -> str:
    """``tool(key: value, ...)``, as Claude Code writes a call."""
    inner = ", ".join(f"{paint(key + ':', 'key')} {value_text(value)}" for key, value in arguments.items())
    return f"{paint(tool, 'tool')}({inner})"


def snippet(item: dict) -> str:
    for name in TEXT_FIELDS:
        text = item.get(name)
        if isinstance(text, str) and text.strip():
            return clip(strip_html(text) if name == "cooked" else text, SNIPPET_CHARS)
    return ""


def one_line(item) -> str:
    """A post, a topic, a hit or a user, in a line."""
    if not isinstance(item, dict):
        return value_text(item)
    parts = []
    if item.get("post_number") is not None:
        parts.append(f"#{item['post_number']}")
    elif item.get("title") and item.get("id") is not None:
        parts.append(f"topic {item['id']}")
    if item.get("username"):
        parts.append(f"@{item['username']}")
    if item.get("title"):
        parts.append(f'"{clip(item["title"], SNIPPET_CHARS)}"')
    text = snippet(item)
    if text:
        parts.append(text)
    if not parts:
        scalars = [(k, v) for k, v in item.items() if not isinstance(v, (dict, list))][:4]
        parts.append("  ".join(f"{k}={value_text(v)}" for k, v in scalars))
    return "  ".join(parts)


def summary(answer) -> list[str]:
    """What a forum tool answered, in a few lines rather than a page of JSON."""
    if isinstance(answer, list):
        return list_lines("", answer)
    if not isinstance(answer, dict):
        return [value_text(answer)]

    if "posted" in answer:
        result = answer.get("result") if isinstance(answer.get("result"), dict) else {}
        where = []
        if result.get("topic_id") is not None:
            where.append(f"topic {result['topic_id']}")
        if result.get("post_number") is not None:
            where.append(f"post {result['post_number']}")
        return ["posted" + (", " + " ".join(where) if where else "") if answer["posted"] else "not posted"]

    lines = []
    head = [f"{key}={value_text(answer[key])}" for key in INTERESTING if key in answer and answer[key] is not None]
    if answer.get("title") or answer.get("username"):
        lines.append(one_line(answer))
    elif head:
        lines.append("  ".join(head))

    for key, value in answer.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            lines += list_lines(key, value)
        elif isinstance(value, list) and not value and key in ("posts", "results", "topics", "users"):
            lines.append(f"{key}: none")

    if not lines:
        scalars = [(k, v) for k, v in answer.items() if not isinstance(v, (dict, list))][:5]
        lines.append("  ".join(f"{k}={value_text(v)}" for k, v in scalars) or "{}")
    return lines


def list_lines(name: str, items: list) -> list[str]:
    title = f"{name}: {len(items)}" if name else f"{len(items)} item(s)"
    lines = [title] + ["  " + one_line(item) for item in items[:ITEMS_SHOWN]]
    if len(items) > ITEMS_SHOWN:
        lines.append(f"  ... and {len(items) - ITEMS_SHOWN} more")
    return lines


def text_lines(text: str) -> list[str]:
    """A file, a listing or a message: its first lines, as they are."""
    rows = [row for row in text.strip().splitlines() if row.strip()]
    lines = [clip(row, width() - len(INDENT)) for row in rows[:TEXT_LINES_SHOWN]]
    if len(rows) > TEXT_LINES_SHOWN:
        lines.append(f"... and {len(rows) - TEXT_LINES_SHOWN} more line(s)")
    return lines or ["(empty)"]


def answer_lines(text: str) -> list[str]:
    try:
        return summary(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        return text_lines(text)


def policy_lines(text: str) -> list[str]:
    """What ``get_policy`` gave the agent: only the researcher's own part."""
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        return under(text_lines(text), "result")
    own = (answer.get("from_your_researcher") or "").strip()
    if not own:
        return under(["the forum's policy, and no instructions of yours"], "result")
    return under(["the forum's policy, and your instructions:"], "result") + wrap(own, INDENT, INDENT, "yours")


# ------------------------------------------------------------ a transcript


@dataclass
class Usage:
    """The tokens a wake's model calls took, as Claude Code's transcript
    records them on each response: what was sent fresh, written to the
    prompt cache and read from it, and what the model wrote; and all of it
    weighed as input tokens at the model's prices (``equivalent``)."""

    fresh: int = 0
    cache_written: int = 0
    cache_read: int = 0
    output: int = 0
    equivalent: float = 0.0
    models: list[str] = field(default_factory=list)

    @property
    def sent(self) -> int:
        return self.fresh + self.cache_written + self.cache_read

    @property
    def total(self) -> int:
        return self.sent + self.output

    def line(self) -> str:
        """The usage in one line, for a wake's view."""
        models = f"; {', '.join(self.models)}" if self.models else ""
        return (
            f"cost: {count(round(self.equivalent))} input-token equivalents, from "
            f"{count(self.sent)} in ({count(self.cache_read)} read from the cache, "
            f"{count(self.cache_written)} written to it, {count(self.fresh)} uncached) "
            f"and {count(self.output)} out{models}"
        )


def count(tokens: int) -> str:
    """A token count in a few characters: 940, 12.3k, 1.2M."""
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k" if tokens < 100_000 else f"{tokens // 1000}k"
    return f"{tokens / 1_000_000:.1f}M"


# What a token costs, as a multiple of an uncached input token of the same
# model, at Anthropic's API prices: every current model's output costs five
# times its input, and a cache write 1.25 times for the five-minute cache
# and twice for the hour's. A cache read is a tenth, except on these models.
OUTPUT_WEIGHT = 5.0
CACHE_WRITE_WEIGHT = {"ephemeral_5m_input_tokens": 1.25, "ephemeral_1h_input_tokens": 2.0}
CACHE_READ_WEIGHT = {"claude-opus-5-5": 0.05, "claude-fable-5-1": 0.025, "claude-mythos-5-1": 0.025}
DEFAULT_CACHE_READ_WEIGHT = 0.1
# Fast mode charges twice for everything.
FAST_WEIGHT = 2.0


def equivalent(numbers: dict, model: str) -> float:
    """One response's tokens as uncached input tokens of its model."""
    written = int(numbers.get("cache_creation_input_tokens") or 0)
    by_ttl = numbers.get("cache_creation") if isinstance(numbers.get("cache_creation"), dict) else {}
    weighed = sum(int(by_ttl.get(key) or 0) * weight for key, weight in CACHE_WRITE_WEIGHT.items())
    # Whatever the split does not account for, at the five-minute price.
    unsplit = written - sum(int(by_ttl.get(key) or 0) for key in CACHE_WRITE_WEIGHT)
    cost = (
        int(numbers.get("input_tokens") or 0)
        + weighed
        + max(unsplit, 0) * CACHE_WRITE_WEIGHT["ephemeral_5m_input_tokens"]
        + int(numbers.get("cache_read_input_tokens") or 0) * CACHE_READ_WEIGHT.get(model, DEFAULT_CACHE_READ_WEIGHT)
        + int(numbers.get("output_tokens") or 0) * OUTPUT_WEIGHT
    )
    return cost * (FAST_WEIGHT if numbers.get("speed") == "fast" else 1.0)


def usage_of(responses: dict[str, tuple[dict, str]]) -> Usage:
    """The sum over each response, counted once although Claude Code writes
    one record per block of it, each repeating the response's usage."""
    usage = Usage()
    for numbers, model in responses.values():
        usage.fresh += int(numbers.get("input_tokens") or 0)
        usage.cache_written += int(numbers.get("cache_creation_input_tokens") or 0)
        usage.cache_read += int(numbers.get("cache_read_input_tokens") or 0)
        usage.output += int(numbers.get("output_tokens") or 0)
        usage.equivalent += equivalent(numbers, model)
        if model and model not in usage.models:
            usage.models.append(model)
    return usage


@dataclass
class Transcript:
    prompt: str
    lines: list[str]
    wrote: list[str]
    usage: Usage = field(default_factory=Usage)


def read_transcript(path: Path) -> Transcript:
    """The wake's prompt, what happened after it, and what it wrote."""
    prompt = ""
    lines: list[str] = []
    wrote: list[str] = []
    names: dict[str, str] = {}
    # Each call's line, so its dot can be coloured once its answer arrives.
    calls: dict[str, tuple[int, str]] = {}
    # Each model response's usage and model, by the response's id.
    responses: dict[str, tuple[dict, str]] = {}

    try:
        records = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return Transcript("", [paint(f"could not read {path}: {exc}", "error")], [])

    for raw in records:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = record.get("type")
        message = record.get("message") or {}
        if kind == "assistant" and isinstance(message.get("usage"), dict):
            key = message.get("id") or f"record {len(responses)}"
            responses[key] = (message["usage"], str(message.get("model") or ""))
        if kind not in ("user", "assistant") or record.get("isSidechain"):
            continue
        content = message.get("content")

        if kind == "user" and isinstance(content, str):
            if not prompt:
                prompt = content
            continue
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "text" and kind == "assistant" and block.get("text", "").strip():
                lines += wrap(block["text"], DOT, SAID, "says")
                lines.append("")
            elif btype == "thinking" and block.get("thinking", "").strip():
                lines += wrap(block["thinking"], paint("✻ ", "think"), SAID, "think")
                lines.append("")
            elif btype == "tool_use":
                tool = short_name(block.get("name", "?"))
                names[block.get("id", "")] = tool
                if tool in NOISE_TOOLS:
                    continue
                arguments = dict(block.get("input") or {})
                arguments.pop("policy_ack", None)
                body = arguments.pop("raw", None) if tool in WRITE_TOOLS else None
                text = call_text(tool, arguments)
                calls[block.get("id", "")] = (len(lines), text)
                lines.append(paint(DOT, "dot_none") + text)
                if body:
                    lines += wrap(body, INDENT, INDENT, "wrote")
            elif btype == "tool_result":
                tool_id = block.get("tool_use_id", "")
                tool = names.get(tool_id, "?")
                if tool in NOISE_TOOLS:
                    continue
                text = result_text(block)
                failed = bool(block.get("is_error")) or text.startswith(("REFUSED", "ERROR"))
                if tool_id in calls:
                    index, call = calls.pop(tool_id)
                    lines[index] = paint(DOT, "dot_bad" if failed else "dot_ok") + call
                if failed:
                    lines += under(text_lines(text), "error")
                elif tool == "get_policy":
                    lines += policy_lines(text)
                else:
                    lines += under(answer_lines(text), "result")
                    if tool in WRITE_TOOLS:
                        wrote.append(tool)
                lines.append("")

    while lines and not lines[-1]:
        lines.pop()
    # A subagent's responses are the wake's too, and Claude Code keeps them
    # beside the session's transcript, one file for each subagent.
    for subagent in sorted((path.with_suffix("") / "subagents").glob("*.jsonl")):
        responses.update(subagent_responses(subagent))
    return Transcript(prompt, lines, wrote, usage_of(responses))


def subagent_responses(path: Path) -> dict[str, tuple[dict, str]]:
    """Each model response in a subagent's transcript, as read_transcript
    keeps the session's."""
    responses: dict[str, tuple[dict, str]] = {}
    try:
        records = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return responses
    for raw in records:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        message = record.get("message") or {}
        if record.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            key = message.get("id") or f"{path.name} record {len(responses)}"
            responses[key] = (message["usage"], str(message.get("model") or ""))
    return responses


# ----------------------------------------------------------------- render


def outcome(row: Row, transcript: Transcript | None) -> tuple[str, str]:
    """How a wake ended, in a few words, and the style to say it in."""
    if row.status == "running":
        started = utc(row.started)
        return ("still running", "busy") if started is None or young(started) else ("did not finish", "bad")
    if row.status == "failed":
        return "failed or timed out", "bad"
    if transcript is None:
        return "finished", "good"
    if transcript.wrote:
        return "wrote: " + ", ".join(transcript.wrote), "good"
    return "wrote nothing", "quiet"


def transcript_for(row: Row) -> Transcript | None:
    path = transcript_path(row.session)
    return read_transcript(path) if path is not None else None


def render(row: Row, full_prompt: bool = True, transcript: Transcript | None = None) -> list[str]:
    started = utc(row.started)
    finished = utc(row.finished)
    rule = paint("─" * width(), "rule")

    # A diary wake has a title and no topic.
    where = row.title if row.topic_id is None else f'topic {row.topic_id} "{row.title}"'
    if row.post_number:
        where += f", post {row.post_number}"
    out = [rule, paint(f"{local_time(started)}  {where}", "head")]

    if transcript is None:
        transcript = transcript_for(row)
    said, style = outcome(row, transcript)
    if row.status == "failed":
        said += "; it will be retried"

    took = ""
    if started and finished:
        seconds = int((finished - started).total_seconds())
        took = f", took {seconds // 60}m{seconds % 60:02d}s"
    out.append(f"woken because {row.reason}; {paint(said, style)}{paint(took, 'meta')}")
    if row.score is not None:
        out.append(paint(f"relevance {row.score:.2f}", "meta"))

    if transcript is not None and transcript.usage.total:
        out.append(paint(transcript.usage.line(), "meta"))
    if row.session:
        where_run = row.agent_dir or "the daemon's directory"
        out.append(paint(f"session {row.session}, in {where_run}", "meta"))
    else:
        out.append(paint("No session was recorded for this wake, so there is no transcript to show", "meta"))
    out.append(rule)
    out.append("")

    if row.session and transcript is None:
        out.append(paint("Claude Code no longer has this session's transcript", "meta"))
    elif transcript is not None:
        if transcript.prompt:
            out += prompt_lines(transcript.prompt, full_prompt)
            out.append("")
        out += transcript.lines or [paint("nothing after the prompt", "meta")]
    return out


def daemon_line(state: Status) -> str:
    if state.polled is None:
        return paint("● ", "bad") + "The daemon has not recorded a poll. It may never have run with this kit."
    if state.stopped:
        return paint("● ", "bad") + f"The daemon last polled at {local_time(state.polled)} and seems to have stopped."
    return paint("● ", "good") + f"The daemon is polling; last at {local_time(state.polled)}."


def floor_line(state: Status) -> str:
    """The agent's similarity floor, and how a researcher changes it."""
    if state.floor is None:
        return paint("○ ", "meta") + "Relevance threshold not known yet: the daemon has not heard it from the forum."
    return (
        paint("○ ", "meta") + f"Relevance threshold {state.floor:.2f}"
        + paint(f"  (from {state.floor_min:.2f} to {state.floor_max:.2f}; + raises it, - lowers it)", "meta")
    )


def footer(state: Status) -> list[str]:
    return [paint("─" * width(), "rule"), daemon_line(state)]


def print_wakes(settings: Settings, limit: int, full_prompt: bool) -> None:
    rows = recent(settings.state_path, max(limit, 1), FORUM)
    lines: list[str] = []
    if not rows:
        lines.append("No wakes recorded yet. The daemon records each one from kit 0.9.0 on.")
    for row in rows:
        lines += render(row, full_prompt=full_prompt)
        lines.append("")
    lines += footer(status(settings.state_path))
    if rows:
        lines.append("The newest wake is the last one above. Scroll up for older ones.")
        if rows[-1].session:
            lines.append(
                paint(
                    "To open one in Claude Code, run `claude --resume <session>`"
                    f" in {rows[-1].agent_dir or 'its directory'}.",
                    "meta",
                )
            )
    print("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("--print", action="store_true", help="print the wakes rather than browse them")
    parser.add_argument("-n", type=int, help="how many wakes to print (default 20) or to list (default all)")
    parser.add_argument("--short", action="store_true", help="one line of each prompt, not all of it")
    parser.add_argument("--hold", action="store_true", help="when printing, wait for Enter before exiting")
    parser.add_argument("--colour", "--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()

    # A legacy code page cannot print everything a post can hold.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    set_colour(colour_wanted(args.colour, sys.stdout))

    load_env_file()
    settings = Settings()

    if not args.print and sys.stdin.isatty() and sys.stdout.isatty():
        from . import browse

        return browse.run(settings, None if args.n is None else max(args.n, 1))

    print_wakes(settings, 20 if args.n is None else args.n, full_prompt=not args.short)
    if args.hold:
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    # The package's copy: the browser imports qupa_cafe.wakes, whose colour
    # switch is not this __main__ module's.
    from qupa_cafe.wakes import main as package_main

    sys.exit(package_main())
