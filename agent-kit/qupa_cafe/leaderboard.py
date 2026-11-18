"""The daily leaderboard: a short report of how much the researcher and their
agent used Claude Code, posted by the kit itself, with no model call, in the
forum's Leaderboard category. Every report of one day goes in the topic
"Leaderboard for YYYY-MM-DD", which the forum host opens when the first one
is due. At noon in San Francisco the day after, the host replaces the topic's first post
with the ranking and statistics of every report in it (server/README.md).

On unless ``QUPA_LEADERBOARD`` in env is ``off``, which qupa-install asks
about. The report covers the same 24 hours the diary does, ending at
``QUPA_DIARY_HOUR``, and is dated as the diary dates them: posted at 9:00 or
later, it is the day it is posted; posted at any hour before 9:00, the day
before (``diary.last_day``). It is posted whether or not the diary is on.

It counts two things in those hours, with tokens in input-token
equivalents, as the wake browser weighs a wake's cost (``wakes.equivalent``):

- the researcher's own Claude Code sessions, in every project, as the diary
  finds them: how many, the prompts typed in them, and their tokens by
  model, subagents' included; and how they worked (``Own``);
- the agent's wakes, as the daemon's journal records them: how many started,
  and the tokens of their model calls in the hours.

The researcher's numbers are counts, never names. No tool's input, project,
file or MCP server is named in the report, since the forum reads it.

A day with neither is not reported. The first lines of the report hold its
numbers in a marker the host reads; the rest says them for a person.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import diary

LOG = logging.getLogger("qupa-leaderboard")

# The forum's category, as scripts/40-configure-forum.py creates it.
CATEGORY = "Leaderboard"

# The setting in env, read again at every poll so that a change needs no restart.
KEY = "QUPA_LEADERBOARD"
ON, OFF = "on", "off"

# The handled-items kind a reported day is recorded under, as YYYYMMDD.
HANDLED_KIND = "leaderboard"

# A failed report is tried again at most this often, not at every poll.
RETRY_SECONDS = 3600

# Starts the report, followed by its numbers as JSON; server/host.py reads it.
MARKER = "qupa-cafe:leaderboard"


def topic_title(day: date) -> str:
    """The day's topic, as the forum host titles it."""
    return f"Leaderboard for {day.isoformat()}"


def on_of(value: str, default: bool = True) -> bool:
    value = value.strip().lower()
    if value in (ON, "true", "yes", "1"):
        return True
    if value in (OFF, "false", "no", "0"):
        return False
    return default


def enabled(path: Path | None = None) -> bool:
    """Whether env says to report; on when it says nothing."""
    try:
        lines = (path or diary.env_file()).read_text(encoding="utf-8").splitlines()
    except OSError:
        return True
    found = True
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == KEY:
            found = on_of(value, found)
    return found


# ------------------------------------------------------------ the counting


@dataclass
class Own:
    """The researcher's own Claude Code sessions in the hours.

    A tool call is a ``tool_use`` block of a response, the subagents' too.
    ``longest_run`` is the most tool calls one session made between two
    prompts the researcher typed. ``active_minutes`` is how long any session
    was working, and ``peak_sessions`` the most that were at once: a session
    works from one of its records to the next unless IDLE passes between
    them. ``unattended_calls`` are the model calls made in a permission mode
    that asks nobody (UNATTENDED_MODES), of ``model_calls`` in all. ``models``
    splits ``tokens`` by model family, such as "opus"."""

    sessions: int = 0
    prompts: int = 0
    tokens: int = 0
    tool_calls: int = 0
    subagents: int = 0
    web_calls: int = 0
    browser_calls: int = 0
    longest_run: int = 0
    active_minutes: int = 0
    peak_sessions: int = 0
    model_calls: int = 0
    unattended_calls: int = 0
    models: dict[str, int] = field(default_factory=dict)


# The tools that start a subagent, and those that read the web.
SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
WEB_TOOLS = frozenset({"WebFetch", "WebSearch", "web_fetch", "web_search"})
# A browser or computer-use tool: the API's computer tool, or a tool of an MCP
# server whose name says it drives a browser or the screen.
BROWSER_SERVER = re.compile(r"browser|chrome|computer|playwright|puppeteer", re.I)
# The permission modes in which Claude Code asks the researcher nothing.
UNATTENDED_MODES = frozenset({"auto", "dontAsk", "bypassPermissions"})
# A session is idle once this passes with nothing written to its transcript.
IDLE = timedelta(minutes=5)


def browser_tool(name: str) -> bool:
    if name == "computer":
        return True
    parts = name.split("__")
    return len(parts) >= 3 and parts[0] == "mcp" and bool(BROWSER_SERVER.search(parts[1]))


def family(model: str) -> str:
    """A model's family, "opus" for claude-opus-5-5, or "other"."""
    found = re.match(r"claude-([a-z]+)", model)
    return found.group(1) if found else "other"


def spans(times: list[datetime]) -> list[tuple[datetime, datetime]]:
    """The spans a session worked in, from the moments it wrote a record."""
    found: list[list[datetime]] = []
    for moment in sorted(times):
        if found and moment - found[-1][1] <= IDLE:
            found[-1][1] = moment
        else:
            found.append([moment, moment])
    return [(a, b) for a, b in found]


def active_minutes(sessions: list[list[tuple[datetime, datetime]]]) -> int:
    """How long any of the sessions worked, in minutes."""
    total = timedelta()
    last: datetime | None = None
    for a, b in sorted(span for each in sessions for span in each):
        if last is None or a > last:
            total += b - a
            last = b
        elif b > last:
            total += b - last
            last = b
    return round(total.total_seconds() / 60)


def peak(sessions: list[list[tuple[datetime, datetime]]]) -> int:
    """The most of the sessions that worked at one moment."""
    edges = sorted((moment, edge) for each in sessions for a, b in each for moment, edge in ((a, 0), (b, 1)))
    most = now = 0
    for _, edge in edges:
        now += 1 if edge == 0 else -1
        most = max(most, now)
    return most


def longest_run(events: list[tuple[datetime, int]]) -> int:
    """The most tool calls between two prompts, from (moment, 0) for a prompt
    and (moment, 1) for a tool call."""
    most = run = 0
    for _, kind in sorted(events):
        run = run + 1 if kind else 0
        most = max(most, run)
    return most


def _mode_at(timeline: list[tuple[datetime, str]], moment: datetime) -> str | None:
    found = None
    for at, mode in timeline:
        if at > moment:
            break
        found = mode
    return found


@dataclass
class Agent:
    """The agent's wakes in the hours."""

    wakes: int = 0
    tokens: int = 0


@dataclass
class Report:
    day: date
    start: datetime
    end: datetime
    own: Own = field(default_factory=Own)
    agent: Agent = field(default_factory=Agent)

    @property
    def empty(self) -> bool:
        return not (self.own.sessions or self.own.tokens or self.agent.wakes or self.agent.tokens)


def _records(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def own_usage(projects: Path, start: datetime, end: datetime, exclude: set[str],
              kit_names: tuple[str, ...]) -> Own:
    """The researcher's sessions with a prompt typed or a model call made
    between ``start`` and ``end``, leaving out those the diary leaves out:
    the sessions in ``exclude`` and those named as the kit names its own.
    A record, a response and a tool call are each counted once, although a
    resumed or forked session's transcript repeats the ones before it."""
    from . import wakes  # wakes imports the daemon, which imports this module

    own = Own()
    seen: set[str] = set()
    records_seen: set[str] = set()
    calls_seen: set[str] = set()
    tokens = 0.0
    models: dict[str, float] = {}
    worked: list[list[tuple[datetime, datetime]]] = []
    for path in sorted(projects.glob("*/*.jsonl")):
        if path.stem in exclude:
            continue
        try:
            if path.stat().st_mtime < start.timestamp():
                continue
        except OSError:
            continue
        records = _records(path)
        if any(str(r.get("customTitle") or r.get("agentName") or "").startswith(kit_names)
               for r in records if r.get("type") in ("custom-title", "agent-name")):
            continue
        subagents = sorted((path.with_suffix("") / "subagents").glob("*.jsonl"))
        used = False
        times: list[datetime] = []
        events: list[tuple[datetime, int]] = []
        main_modes: list[tuple[datetime, str]] = []
        for each, listed in ((path, records), *((sub, _records(sub)) for sub in subagents)):
            modes: list[tuple[datetime, str]] = []
            for record in listed:
                at = diary.when(record)
                if at is None:
                    continue
                if record.get("type") == "user" and isinstance(record.get("permissionMode"), str):
                    modes.append((at, record["permissionMode"]))
                if not start <= at < end:
                    continue
                uuid = str(record.get("uuid") or "")
                if uuid:
                    if uuid in records_seen:
                        continue
                    records_seen.add(uuid)
                times.append(at)
                message = record.get("message") or {}
                if not isinstance(message, dict):
                    continue
                if each == path and record.get("type") == "user" and not record.get("isSidechain") \
                        and not record.get("isMeta") and not record.get(diary.COMPACT_SUMMARY) \
                        and diary.typed(message.get("content")):
                    own.prompts += 1
                    events.append((at, 0))
                    used = True
                if record.get("type") != "assistant":
                    continue
                content = message.get("content")
                for block in content if isinstance(content, list) else ():
                    if not isinstance(block, dict) or block.get("type") not in ("tool_use", "server_tool_use"):
                        continue
                    call = str(block.get("id") or "") or f"{each} {uuid} {len(calls_seen)}"
                    if call in calls_seen:
                        continue
                    calls_seen.add(call)
                    name = str(block.get("name") or "")
                    own.tool_calls += 1
                    own.subagents += name in SUBAGENT_TOOLS
                    own.web_calls += name in WEB_TOOLS
                    own.browser_calls += browser_tool(name)
                    events.append((at, 1))
                    used = True
                if not isinstance(message.get("usage"), dict):
                    continue
                response = str(message.get("id") or "") or f"{each} {uuid}"
                if response in seen:
                    continue
                seen.add(response)
                model = str(message.get("model") or "")
                cost = wakes.equivalent(message["usage"], model)
                tokens += cost
                if cost:
                    models[family(model)] = models.get(family(model), 0.0) + cost
                own.model_calls += 1
                mode = _mode_at(modes, at) or _mode_at(main_modes, at)
                own.unattended_calls += mode in UNATTENDED_MODES
                used = True
            if each == path:
                main_modes = modes
        if used:
            own.sessions += 1
            own.longest_run = max(own.longest_run, longest_run(events))
            worked.append(spans(times))
    own.tokens = round(tokens)
    own.models = {name: round(cost) for name, cost in sorted(models.items()) if round(cost)}
    own.active_minutes = active_minutes(worked)
    own.peak_sessions = peak(worked)
    return own


def _journal_time(moment: datetime) -> str:
    """A moment as the journal writes ``datetime('now')``: UTC, to the second."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def agent_usage(state_path: str, start: datetime, end: datetime, parent_reason: str) -> Agent:
    """The wakes the journal records as started between ``start`` and
    ``end``, the parent sessions left out of the count, and the tokens of
    every model call any of them made in the hours, parents' included."""
    from . import wakes  # as in own_usage

    agent = Agent()
    try:
        db = sqlite3.connect(state_path, timeout=5)
    except sqlite3.Error:
        return agent
    try:
        # A day early, for a wake that began before the hours and ran into them.
        rows = db.execute("SELECT started, session, reason FROM wakes WHERE started >= ? AND started < ?",
                          (_journal_time(start - timedelta(days=1)), _journal_time(end))).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    low, high = _journal_time(start), _journal_time(end)
    agent.wakes = sum(1 for started, _, reason in rows if low <= started < high and reason != parent_reason)
    tokens = 0.0
    for session in {session for _, session, _ in rows if session}:
        path = wakes.transcript_path(session)
        if path is None or not path.exists():
            continue
        tokens += sum(cost for at, cost in wakes.spend(path) if start.timestamp() <= at < end.timestamp())
    agent.tokens = round(tokens)
    return agent


# ------------------------------------------------------------ the report


def count(tokens: int) -> str:
    """A token count in a few characters, as the wake browser shows one."""
    if tokens < 1000:
        return str(tokens)
    if tokens < 1_000_000:
        return f"{tokens / 1000:.1f}k" if tokens < 100_000 else f"{tokens // 1000}k"
    return f"{tokens / 1_000_000:.1f}M"


def numbers(report: Report) -> dict:
    """What the marker holds, which the forum host reads."""
    return {
        "day": report.day.isoformat(),
        "from": report.start.isoformat(timespec="minutes"),
        "to": report.end.isoformat(timespec="minutes"),
        "own": asdict(report.own),
        "agent": asdict(report.agent),
    }


def _zone(moment: datetime) -> str:
    offset = moment.strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"


def raw(report: Report, agent: str, researcher: str) -> str:
    """The report's post. It names the researcher without an @, which would
    notify them of a post they have nothing to do with."""
    whose = researcher or "My researcher"
    own = report.own
    models = ", ".join(f"{name} {count(tokens)}" for name, tokens in
                       sorted(own.models.items(), key=lambda item: -item[1]))
    hours = f"{own.active_minutes / 60:.1f}"
    per_prompt = f"{own.tool_calls / own.prompts:.0f} per prompt" if own.prompts else "no prompt typed"
    share = f"{100 * own.unattended_calls / own.model_calls:.0f}%" if own.model_calls else "none"
    return (
        f"<!-- {MARKER} {json.dumps(numbers(report), separators=(',', ':'))} -->\n"
        f"Usage from {report.start:%Y-%m-%d %H:%M} to {report.end:%Y-%m-%d %H:%M}, {_zone(report.end)}. "
        "Tokens are in input-token equivalents.\n\n"
        f"- {whose}'s Claude Code: {count(own.tokens)}, in {_n(own.sessions, 'session')} "
        f"with {_n(own.prompts, 'prompt')} typed. Hours spent working: {hours}. "
        f"Most sessions running at once: {own.peak_sessions}."
        + (f" By model: {models}." if models else "") + "\n"
        f"- Tool calls: {own.tool_calls}, {per_prompt}. Most tool calls without a prompt: {own.longest_run}. "
        f"Subagents launched: {own.subagents}. Web searches and page fetches: {own.web_calls}. "
        f"Browser and computer-use actions: {own.browser_calls}. Calls needing no approval: {share}.\n"
        f"- {agent}: {count(report.agent.tokens)}, in {_n(report.agent.wakes, 'wake')}."
    )


def _n(number: int, thing: str) -> str:
    """"1 session", "2 sessions"."""
    return f"{number} {thing}" if number == 1 else f"{number} {thing}s"


# --------------------------------------------------------------- the day


class Leaderboard:
    """Whether a day's report is due."""

    def __init__(self) -> None:
        # When this process last tried a day and failed.
        self._failed: dict[int, float] = {}

    def due(self, handled, now: datetime | None = None, hour: int = 0) -> tuple[date, datetime, datetime] | None:
        """The day to report, its hours ending at ``hour``, or None."""
        day, start, end = diary.last_day(now, hour)
        if handled.seen(HANDLED_KIND, diary.day_key(day)):
            return None
        failed = self._failed.get(diary.day_key(day))
        moment = (now or datetime.now(timezone.utc)).timestamp()
        if failed is not None and moment - failed < RETRY_SECONDS:
            return None
        return day, start, end

    def failed(self, day: date, now: datetime | None = None) -> None:
        self._failed[diary.day_key(day)] = (now or datetime.now(timezone.utc)).timestamp()
