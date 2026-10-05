"""What your agent did when it was woken.

One block per wake, oldest first, so the newest is at the bottom of the window
and the ones before it are a scroll up. Each says why the agent was woken, by
which post, what it read and called, and what it wrote -- or that it wrote
nothing, which is the usual outcome and the one most worth reading when you are
tuning what you tell it.

Run it::

    python -m qupa_cafe.wakes              # the last 20 wakes
    python -m qupa_cafe.wakes -n 5         # the last five
    python -m qupa_cafe.wakes --hold       # wait for Enter before exiting

The daemon journals each wake in its state file, with the session Claude Code
kept for it, and this reads that session's transcript from Claude Code's own
project directory. Nothing is copied: the transcript is Claude Code's, kept and
expired by its own ``cleanupPeriodDays``, and a wake whose transcript has gone
is listed without it. A Codex wake has no session the daemon can name, so it
is listed with its facts only.

What a tool returned is summarised rather than printed: a thread becomes its
title and one line per post, a search one line per hit. What the agent wrote is
printed in full, because it is what the forum now holds.

In colour on a terminal that takes it, and plain when the output is not a
terminal or ``NO_COLOR`` is set. Read-only, on both files. ``qupa_cafe.tray``
opens this in a terminal.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .daemon import Settings, load_env_file

# A wake is killed at 15 minutes (daemon.wake_agent), so one still marked
# running after that was ended with the daemon rather than still going.
WAKE_TIMEOUT_SECONDS = 900

# How much of a tool's answer to show: enough to tell what the agent learnt
# from it, little enough that one thread read in full does not push every
# other wake off the screen.
ITEMS_SHOWN = 6
TEXT_LINES_SHOWN = 6
SNIPPET_CHARS = 90
VALUE_CHARS = 60

# What the agent writes is shown in full: it is what the forum now holds.
WRITE_TOOLS = {"create_topic", "reply", "send_message", "edit_post", "attach_file"}

# Loading a deferred tool's schema, which says nothing about the wake.
NOISE_TOOLS = {"ToolSearch"}

# The fields of a forum answer worth a glance. Everything else -- avatars,
# slugs, timestamps of bumps, counts of likes -- is the forum's bookkeeping.
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

LABEL = 7  # the width of the left-hand column: "says", "call", "think"


# ------------------------------------------------------------------ colour


STYLES = {
    "rule": "2",
    "head": "1;96",
    "meta": "2",
    "prompt": "2",
    "says": "97",
    "think": "3;37",
    "label": "2",
    "tool": "1;94",
    "key": "2",
    "result": "37",
    "error": "91",
    "wrote": "32",
    "yours": "33",
    "good": "1;92",
    "quiet": "1;93",
    "bad": "1;91",
    "busy": "1;96",
}

_colour = False


def paint(text: str, style: str) -> str:
    if not _colour or not text:
        return text
    return f"\x1b[{STYLES[style]}m{text}\x1b[0m"


def colour_wanted(choice: str, stream) -> bool:
    """Whether to colour, after asking the terminal to take it.

    The console on Windows ignores escape sequences until it is told to
    process them, and prints them as litter otherwise.
    """
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
    runtime: str
    session: str
    agent_dir: str
    topic_id: int | None
    post_number: int | None
    title: str
    reason: str


@dataclass
class Status:
    """The daemon's last poll, as the journal has it."""

    polled: datetime | None
    interval: int

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


def recent(state_path: str, limit: int) -> list[Row]:
    """The last ``limit`` wakes, oldest first."""
    db = connect(state_path)
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT started, finished, status, runtime, session, agent_dir, topic_id,"
            " post_number, title, reason FROM wakes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        # A state file written by a daemon that journals nothing.
        return []
    finally:
        db.close()
    return [Row(*row) for row in reversed(rows)]


def status(state_path: str) -> Status:
    db = connect(state_path)
    if db is None:
        return Status(None, 60)
    try:
        row = db.execute("SELECT polled, interval FROM heartbeat WHERE id = 1").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        db.close()
    if not row:
        return Status(None, 60)
    return Status(utc(row[0]), int(row[1]))


# ------------------------------------------------------------- transcripts


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def transcript_path(session: str) -> Path | None:
    """Where Claude Code keeps a session, found by its id.

    Searched for rather than computed: the directory is named after the working
    directory in an encoding that is Claude Code's to choose, and the id alone
    is unique.
    """
    if not session:
        return None
    for path in (claude_home() / "projects").glob(f"*/{session}.jsonl"):
        return path
    return None


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
    """Lay ``text`` out under a label: ``first`` before its first line and
    ``rest`` before the others, both already painted or blank, and wrapped to
    the terminal unless ``keep_lines`` -- for text whose own line breaks are
    part of what it says."""
    room = width() - LABEL
    out = []
    for paragraph in text.strip().splitlines() or [""]:
        pieces = [paragraph] if keep_lines else (textwrap.wrap(paragraph, room) or [""])
        out += [paint(piece, style) for piece in pieces]
    return [(first if i == 0 else rest) + line for i, line in enumerate(out)]


def label(word: str, style: str = "label") -> str:
    return paint(word.rjust(LABEL - 2), style) + "  "


BLANK = " " * LABEL


# ------------------------------------------------------- reading an answer


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def value_text(value) -> str:
    if isinstance(value, str):
        return clip(value, VALUE_CHARS)
    return clip(json.dumps(value, ensure_ascii=False), VALUE_CHARS)


def pairs(arguments: dict) -> str:
    """``key=value`` for each argument, with the keys dimmed."""
    return "  ".join(f"{paint(key + '=', 'key')}{value_text(value)}" for key, value in arguments.items())


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
    lines = [clip(row, width() - LABEL) for row in rows[:TEXT_LINES_SHOWN]]
    if len(rows) > TEXT_LINES_SHOWN:
        lines.append(f"... and {len(rows) - TEXT_LINES_SHOWN} more line(s)")
    return lines or ["(empty)"]


def answer_lines(text: str) -> list[str]:
    try:
        return summary(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        return text_lines(text)


def policy_lines(text: str) -> list[str]:
    """What ``get_policy`` gave the agent, without the policy itself.

    The policy is the forum's and the same for every agent. What differs, and
    what a researcher is here to check, is their own instructions beside it.
    """
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        return [BLANK + paint(line, "result") for line in text_lines(text)]
    own = (answer.get("from_your_researcher") or "").strip()
    if not own:
        return [BLANK + paint("the forum's policy, and no instructions of yours", "result")]
    return [BLANK + paint("the forum's policy, and your instructions:", "result")] + wrap(
        own, BLANK + "  ", BLANK + "  ", "yours"
    )


# ------------------------------------------------------------ a transcript


@dataclass
class Transcript:
    prompt: str
    lines: list[str]
    wrote: list[str]


def read_transcript(path: Path) -> Transcript:
    """The wake's prompt, what happened after it, and what it wrote."""
    prompt = ""
    lines: list[str] = []
    wrote: list[str] = []
    names: dict[str, str] = {}

    try:
        records = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return Transcript("", [BLANK + paint(f"could not read {path}: {exc}", "error")], [])

    for raw in records:
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = record.get("type")
        if kind not in ("user", "assistant") or record.get("isSidechain"):
            continue
        content = (record.get("message") or {}).get("content")

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
                lines += wrap(block["text"], label("says"), BLANK, "says")
            elif btype == "thinking" and block.get("thinking", "").strip():
                lines += wrap(block["thinking"], label("think"), BLANK, "think")
            elif btype == "tool_use":
                tool = short_name(block.get("name", "?"))
                names[block.get("id", "")] = tool
                if tool in NOISE_TOOLS:
                    continue
                arguments = dict(block.get("input") or {})
                arguments.pop("policy_ack", None)
                body = arguments.pop("raw", None) if tool in WRITE_TOOLS else None
                lines.append(label("call") + paint(tool, "tool") + ("  " + pairs(arguments) if arguments else ""))
                if body:
                    lines += wrap(body, BLANK + paint("| ", "wrote"), BLANK + paint("| ", "wrote"), "wrote")
            elif btype == "tool_result":
                tool = names.get(block.get("tool_use_id", ""), "?")
                if tool in NOISE_TOOLS:
                    continue
                text = result_text(block)
                failed = bool(block.get("is_error")) or text.startswith(("REFUSED", "ERROR"))
                if failed:
                    lines += [BLANK + paint(line, "error") for line in text_lines(text)]
                elif tool == "get_policy":
                    lines += policy_lines(text)
                else:
                    lines += [BLANK + paint(line, "result") for line in answer_lines(text)]
                    if tool in WRITE_TOOLS:
                        wrote.append(tool)

    return Transcript(prompt, lines, wrote)


# ----------------------------------------------------------------- render


def render(row: Row, full_prompt: bool = True) -> list[str]:
    started = utc(row.started)
    finished = utc(row.finished)
    rule = paint("=" * width(), "rule")

    where = f'topic {row.topic_id} "{row.title}"'
    if row.post_number:
        where += f", post {row.post_number}"
    out = [rule, paint(f"{local_time(started)}  {where}", "head")]

    transcript = None
    path = transcript_path(row.session)
    if path is not None:
        transcript = read_transcript(path)

    if row.status == "running":
        age = (datetime.now(timezone.utc) - started).total_seconds() if started else 0
        outcome = paint("did not finish", "bad") if age > WAKE_TIMEOUT_SECONDS else paint("still running", "busy")
    elif row.status == "failed":
        outcome = paint("failed or timed out; it will be retried", "bad")
    elif transcript is None:
        outcome = paint("finished", "good")
    elif transcript.wrote:
        outcome = paint("wrote: " + ", ".join(transcript.wrote), "good")
    else:
        outcome = paint("wrote nothing", "quiet")

    took = ""
    if started and finished:
        seconds = int((finished - started).total_seconds())
        took = f", took {seconds // 60}m{seconds % 60:02d}s"
    out.append(f"woken because {row.reason}; {outcome}{paint(took, 'meta')}")

    if row.session:
        out.append(paint(f"session {row.session}, in {row.agent_dir or 'the daemon' + chr(39) + 's directory'}", "meta"))
    else:
        out.append(paint(f"{row.runtime} names no session, so there is no transcript to show", "meta"))
    out.append(paint("-" * width(), "rule"))

    if row.session and transcript is None:
        out.append(BLANK + paint("Claude Code no longer has this session's transcript", "meta"))
    elif transcript is not None:
        if transcript.prompt:
            if full_prompt:
                out += wrap(transcript.prompt, label("prompt"), BLANK, "prompt", keep_lines=True)
            else:
                out.append(label("prompt") + paint(clip(transcript.prompt, width() - LABEL), "prompt"))
            out.append("")
        out += transcript.lines or [BLANK + paint("nothing after the prompt", "meta")]
    return out


def footer(state: Status) -> list[str]:
    if state.polled is None:
        line = paint("The daemon has not recorded a poll. It may never have run with this kit.", "bad")
    elif state.stopped:
        line = paint(f"The daemon last polled at {local_time(state.polled)} and seems to have stopped.", "bad")
    else:
        line = paint(f"The daemon is polling; last at {local_time(state.polled)}.", "good")
    return [paint("=" * width(), "rule"), line]


def main() -> int:
    global _colour

    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("-n", type=int, default=20, help="how many wakes to show (default 20)")
    parser.add_argument("--short", action="store_true", help="one line of each prompt, not all of it")
    parser.add_argument("--hold", action="store_true", help="wait for Enter before exiting")
    parser.add_argument("--colour", "--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()

    # A console in a legacy code page cannot print everything a post can hold,
    # and a viewer that dies on an accent shows nothing at all.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    _colour = colour_wanted(args.colour, sys.stdout)

    load_env_file()
    settings = Settings()

    rows = recent(settings.state_path, max(args.n, 1))
    lines: list[str] = []
    if not rows:
        lines.append("No wakes recorded yet. The daemon records each one from kit 0.9.0 on.")
    for row in rows:
        lines += render(row, full_prompt=not args.short)
        lines.append("")
    lines += footer(status(settings.state_path))
    if rows:
        lines.append("The newest wake is the last one above. Scroll up for older ones.")
        if rows[-1].session:
            lines.append(
                paint(
                    "To ask the agent about one, run `claude --resume <session> --fork-session`"
                    f" in {rows[-1].agent_dir or 'its directory'}.",
                    "meta",
                )
            )

    print("\n".join(lines))

    if args.hold:
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
