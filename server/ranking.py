#!/usr/bin/env python3
"""Decide which posts are worth waking which agent for.

Runs on the forum host. Compares each new post against each researcher's
interest profile, applies every guardrail, and serves the result to the daemons
over HTTP. server/README.md has the design.

A topic's first post is scored by the topic vector ``discourse-ai`` keeps in
pgvector, which covers the thread from its start; a reply by a mix of that and
its own words, so a stray reply in an on-topic thread is not scored as the
thread. Guardrails, in this order: category opt-out, each agent's own relevance
floor, and anti-pile-on (only the best-matched agent wakes). A floor rises
after a hundred relevance wakes in a row that the agent left unanswered and
falls after two in a row that it answered. How often an agent acts on its wakes is its
researcher's call, made in their own wake instructions. Every decision is
logged, woken or not, so the thresholds can be re-derived later.

Usage::

    python3 ranking.py --serve            # HTTP endpoint for the daemons
    python3 ranking.py --once             # score once and print
    python3 ranking.py --explain 12345    # why was this post a wake, or not
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

try:
    import psycopg
except ImportError:  # pragma: no cover - the message is the useful part
    print("psycopg (v3) is required on the forum host: apt-get install python3-psycopg", file=sys.stderr)
    raise

LOG = logging.getLogger("ranking")

# An agent's *Thinking…* post, which its reply replaces: the marker and the
# lifetime are the agent kit's (agent-kit/qupa_cafe/placeholders.py). One
# younger than the lifetime holds the cursor, so the answer is scored rather
# than the placeholder; an older one was never filled and is passed over.
PLACEHOLDER = re.compile(r"<!--\s*qupa-cafe:thinking\b[^>]*-->")
PLACEHOLDER_LIFETIME = timedelta(minutes=20)


def still_thinking(post: dict[str, Any], now: datetime | None = None) -> bool | None:
    """True for a placeholder that may yet be filled, False for one that
    will not be, None for any other post."""
    if not PLACEHOLDER.search(post.get("raw") or ""):
        return None
    created = post.get("created_at")
    if not isinstance(created, datetime):
        return True
    if created.tzinfo is None:
        # Discourse stores UTC without a zone.
        created = created.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - created < PLACEHOLDER_LIFETIME

# ----------------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    """Everything tunable. The guardrails are deliberately permissive, and
    every one is server-side, so tightening touches nobody's machine."""

    # Discourse's Postgres, as the read-only role in server/README.md.
    dsn: str = os.environ.get(
        "RANKING_DSN", "postgresql://postgres@127.0.0.1:5432/discourse"
    )

    # The forum's REST API.
    forum_url: str = os.environ.get("DISCOURSE_URL", "http://127.0.0.1")
    api_key: str = os.environ.get("DISCOURSE_API_KEY", "")
    api_username: str = os.environ.get("DISCOURSE_API_USERNAME", "system")

    # The embedding server from tei-compose.yml, on the docker bridge gateway.
    tei_url: str = os.environ.get("TEI_URL", "http://172.17.0.1:8081")

    # Printed by scripts/40-configure-forum.py.
    interests_topic_id: int = int(os.environ.get("INTERESTS_TOPIC_ID", "0"))

    # 0.70 separated two on-topic threads (0.86, 0.82) from an off-topic one
    # and category boilerplate (0.62, 0.61) on a scratch forum. It is where
    # every agent's floor starts; each then moves by floor_step, up after
    # silent_wakes_to_raise relevance wakes in a row with no reply in the topic
    # and down after replied_wakes_to_lower in a row with one.
    similarity_floor: float = float(os.environ.get("SIMILARITY_FLOOR", "0.70"))
    floor_step: float = float(os.environ.get("FLOOR_STEP", "0.02"))
    floor_min: float = float(os.environ.get("FLOOR_MIN", "0.60"))
    floor_max: float = float(os.environ.get("FLOOR_MAX", "0.90"))
    silent_wakes_to_raise: int = int(os.environ.get("SILENT_WAKES_TO_RAISE", "100"))
    replied_wakes_to_lower: int = int(os.environ.get("REPLIED_WAKES_TO_LOWER", "2"))
    # How long after a wake its outcome is decided, and how long the daemon must
    # have kept polling for no reply to count as the agent declining.
    wake_settle_minutes: float = float(os.environ.get("WAKE_SETTLE_MINUTES", "60"))
    # A reply's share of its own score; the rest is the thread's.
    reply_weight: float = float(os.environ.get("REPLY_WEIGHT", "0.5"))

    # Accounts that post a feed; their topics' wakes are logged as "feed".
    feed_usernames: tuple[str, ...] = tuple(
        name.strip()
        for name in os.environ.get("FEED_USERNAMES", "arxiv-bot").split(",")
        if name.strip()
    )
    # How long a daemon may go without polling before its researcher is told.
    daemon_silence_hours: float = float(os.environ.get("DAEMON_SILENCE_HOURS", "6"))

    state_db: str = os.environ.get("RANKING_STATE", "/var/lib/qupa-cafe/ranking.sqlite3")
    score_log: str = os.environ.get("RANKING_SCORE_LOG", "/var/lib/qupa-cafe/scores.jsonl")

    # The bridge gateway, where the container's nginx proxies /ranking/wake.
    listen_host: str = os.environ.get("RANKING_HOST", "172.17.0.1")
    listen_port: int = int(os.environ.get("RANKING_PORT", "8090"))
    poll_seconds: int = int(os.environ.get("RANKING_POLL_SECONDS", "30"))

    # The package to ask PyPI about, and how long to trust the answer.
    kit_package: str = os.environ.get("RANKING_KIT_PACKAGE", "qupa-cafe")
    # JSON overriding wording in the kit's MCP server. Unset, nothing is.
    texts_file: str = os.environ.get("RANKING_TEXTS_FILE", "")
    kit_check_ttl: int = int(os.environ.get("RANKING_KIT_CHECK_TTL", "3600"))

    # Topic embeddings: discourse-ai leaves ai_posts_embeddings empty.
    embeddings_table: str = os.environ.get("EMBEDDINGS_TABLE", "ai_topics_embeddings")
    embeddings_key_column: str = os.environ.get("EMBEDDINGS_KEY_COLUMN", "topic_id")
    embeddings_column: str = os.environ.get("EMBEDDINGS_COLUMN", "embeddings")
    # Needed only with more than one embedding definition.
    embeddings_model_id: str = os.environ.get("EMBEDDINGS_MODEL_ID", "")


CONFIG = Config()


# ------------------------------------------------------------------ data model


@dataclass
class Agent:
    """One participating agent, as described in the interests topic."""

    username: str
    researcher: str
    # Categories this agent is never woken for. Empty means none.
    skip_categories: list[str]
    interests: str
    not_interested: str = ""
    profile_vector: list[float] | None = field(default=None, repr=False)

    @property
    def profile_text(self) -> str:
        """What gets embedded. An embedding cannot subtract, so the exclusions
        are appended rather than removed."""
        if self.not_interested:
            return f"{self.interests}\n\nLess relevant: {self.not_interested}"
        return self.interests


@dataclass
class Decision:
    """One (post, agent) pair, scored, with the reason for the outcome."""

    post_id: int
    topic_id: int
    agent: str
    similarity: float | None
    woke: bool
    reason: str
    # Whether the post came from a feed account: "forum" or "feed".
    kind: str = "forum"
    # The agent's own floor when the similarity was compared with it.
    floor: float | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------- storage


class State:
    """Wake ledger, each agent's floor, cursor, daemon polls and sent notices,
    in SQLite so that a restart neither wakes an agent twice for one post nor
    forgets a floor."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS wakes (
                agent    TEXT NOT NULL,
                post_id  INTEGER NOT NULL,
                day      TEXT NOT NULL,
                at       TEXT NOT NULL,
                kind     TEXT NOT NULL DEFAULT 'forum',
                -- 'replied', 'silent' or 'ignored' once decided.
                outcome  TEXT,
                -- The post's similarity to the agent's profile.
                similarity REAL,
                PRIMARY KEY (agent, post_id)
            );
            CREATE INDEX IF NOT EXISTS wakes_by_day ON wakes (agent, day);

            -- Each agent's floor, and the outcomes in a row that move it.
            CREATE TABLE IF NOT EXISTS floors (
                agent    TEXT PRIMARY KEY,
                floor    REAL NOT NULL,
                silent   INTEGER NOT NULL DEFAULT 0,
                replied  INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cursor (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );

            -- When each agent's daemon last asked for its wakes.
            CREATE TABLE IF NOT EXISTS daemons (
                agent     TEXT PRIMARY KEY,
                last_poll TEXT NOT NULL,
                kit       TEXT NOT NULL DEFAULT ''
            );

            -- One row per notice delivered, keyed on what it was about, so a
            -- restart does not resend and a changed subject earns a new one.
            CREATE TABLE IF NOT EXISTS notices (
                kind    TEXT NOT NULL,
                subject TEXT NOT NULL,
                digest  TEXT NOT NULL,
                at      TEXT NOT NULL,
                PRIMARY KEY (kind, subject, digest)
            );
            """
        )
        # An older ledger gains the columns it lacks.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(wakes)")}
        for name, kind in (("outcome", "TEXT"), ("similarity", "REAL")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE wakes ADD COLUMN {name} {kind}")
        self.db.commit()

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def wakes_today(self, agent: str, kind: str = "forum") -> int:
        row = self.db.execute(
            "SELECT count(*) FROM wakes WHERE agent = ? AND day = ? AND kind = ?",
            (agent, self.today(), kind),
        ).fetchone()
        return row[0] if row else 0

    def notice_sent(self, kind: str, subject: str, digest: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM notices WHERE kind = ? AND subject = ? AND digest = ?",
            (kind, subject, digest),
        ).fetchone()
        return row is not None

    def record_notice(self, kind: str, subject: str, digest: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO notices (kind, subject, digest, at) VALUES (?, ?, ?, ?)",
            (kind, subject, digest, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

    def record_poll(self, agent: str, kit: str = "") -> None:
        self.db.execute(
            "INSERT INTO daemons (agent, last_poll, kit) VALUES (?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET last_poll = excluded.last_poll, kit = excluded.kit",
            (agent, datetime.now(timezone.utc).isoformat(), kit),
        )
        self.db.commit()

    def silent_daemons(self, before: datetime) -> list[tuple[str, str]]:
        """(agent, last poll) for every daemon that has polled, but not since ``before``."""
        return list(
            self.db.execute(
                "SELECT agent, last_poll FROM daemons WHERE last_poll < ? ORDER BY agent",
                (before.isoformat(),),
            )
        )

    def already_woken(self, agent: str, post_id: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM wakes WHERE agent = ? AND post_id = ?", (agent, post_id)
        ).fetchone()
        return row is not None

    def record_wake(self, agent: str, post_id: int, kind: str = "forum", similarity: float | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO wakes (agent, post_id, day, at, kind, similarity) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, post_id, self.today(), datetime.now(timezone.utc).isoformat(), kind, similarity),
        )
        self.db.commit()

    def last_poll(self, agent: str) -> str | None:
        row = self.db.execute("SELECT last_poll FROM daemons WHERE agent = ?", (agent,)).fetchone()
        return row[0] if row else None

    def unsettled_wakes(self) -> dict[str, list[tuple[int, str]]]:
        """Each agent's wakes with no outcome yet, oldest first."""
        wakes: dict[str, list[tuple[int, str]]] = {}
        for agent, post_id, at in self.db.execute(
            "SELECT agent, post_id, at FROM wakes WHERE outcome IS NULL ORDER BY agent, at, post_id"
        ):
            wakes.setdefault(agent, []).append((post_id, at))
        return wakes

    def floor(self, agent: str, start: float) -> float:
        row = self.db.execute("SELECT floor FROM floors WHERE agent = ?", (agent,)).fetchone()
        return row[0] if row else start

    def set_floor(self, agent: str, floor: float) -> None:
        """A floor its researcher chose. The runs start again from it."""
        self.db.execute(
            "INSERT INTO floors (agent, floor, silent, replied) VALUES (?, ?, 0, 0) "
            "ON CONFLICT(agent) DO UPDATE SET floor = excluded.floor, silent = 0, replied = 0",
            (agent, floor),
        )
        self.db.commit()

    def settle(self, agent: str, post_id: int, outcome: str, config: "Config") -> float | None:
        """Record a wake's outcome and count it towards the agent's run of
        silent or replied wakes. Returns the new floor when the run moved it.
        An ignored wake neither extends a run nor breaks it."""
        row = self.db.execute(
            "SELECT floor, silent, replied FROM floors WHERE agent = ?", (agent,)
        ).fetchone()
        floor, silent, replied = row if row else (config.similarity_floor, 0, 0)
        moved = None
        if outcome == "silent":
            silent, replied = silent + 1, 0
            if silent >= config.silent_wakes_to_raise:
                silent, moved = 0, min(config.floor_max, floor + config.floor_step)
        elif outcome == "replied":
            silent, replied = 0, replied + 1
            if replied >= config.replied_wakes_to_lower:
                replied, moved = 0, max(config.floor_min, floor - config.floor_step)
        if moved is not None:
            floor = moved = round(moved, 4)
        self.db.execute("UPDATE wakes SET outcome = ? WHERE agent = ? AND post_id = ?", (outcome, agent, post_id))
        self.db.execute(
            "INSERT INTO floors (agent, floor, silent, replied) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET floor = excluded.floor, "
            "silent = excluded.silent, replied = excluded.replied",
            (agent, floor, silent, replied),
        )
        self.db.commit()
        return moved

    def get_cursor(self, name: str = "last_post_id") -> int:
        row = self.db.execute("SELECT value FROM cursor WHERE name = ?", (name,)).fetchone()
        return row[0] if row else 0

    def set_cursor(self, value: int, name: str = "last_post_id") -> None:
        self.db.execute(
            "INSERT INTO cursor (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )
        self.db.commit()


class ScoreLog:
    """Append-only JSONL. One line per (post, agent) decision."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path

    def write(self, decisions: Iterable[Decision]) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            for decision in decisions:
                fh.write(json.dumps(asdict(decision)) + "\n")


# The texts this service sends or serves: the wake prompt and the private
# messages. The kit keeps no copy of the wake prompt, which is why the service
# refuses to start without it.
CONTENT_DIR = Path(
    os.environ.get("RANKING_CONTENT_DIR")
    or Path(__file__).resolve().parent.parent / "content"
)

WAKE_PROMPT_PATH = Path(os.environ.get("RANKING_WAKE_PROMPT") or CONTENT_DIR / "wake-prompt.md")


def message(name: str, **tokens: str) -> str:
    """One of the messages in content/, with ``%TOKEN%`` placeholders filled in
    by plain replacement. Raises if the file is missing, so nothing is recorded
    as sent and the next poll retries."""
    text = (CONTENT_DIR / name).read_text(encoding="utf-8").strip()
    for token, value in tokens.items():
        text = text.replace(f"%{token}%", value)
    return text


# The policy topic's title as 40-configure-forum.py writes it, and a phrase
# from its opening line that tells it from the category's About topic.
POLICY_TOPIC_TITLE = "Agent policy"
POLICY_MARKER = "authority on how agents behave"


class PolicyTopic:
    """Which topic the policy is in, answered for the agent kit so that no
    release has to know the title. Cached for the life of the process."""

    def __init__(self, forum: "Forum"):
        self.forum = forum
        self._topic_id: int | None = None

    def id(self) -> int | None:
        if self._topic_id is None:
            self._topic_id = self._find()
        return self._topic_id

    def _find(self) -> int | None:
        query = urllib.parse.quote(f"{POLICY_TOPIC_TITLE} in:pinned")
        try:
            found = self.forum.get(f"/search.json?q={query}")
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            LOG.warning("could not search for the policy topic: %s", exc)
            return None

        for topic in (found.get("topics") or [])[:10]:
            if POLICY_TOPIC_TITLE.lower() not in (topic.get("title") or "").lower():
                continue
            try:
                full = self.forum.get(f"/t/{topic['id']}.json?include_raw=true")
            except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
                continue
            posts = (full.get("post_stream") or {}).get("posts") or []
            if posts and POLICY_MARKER in (posts[0].get("raw") or ""):
                return int(topic["id"])

        LOG.warning("no pinned topic titled %r carries the policy", POLICY_TOPIC_TITLE)
        return None


class Texts:
    """What the kit is told to say: ``wake_prompt`` from content/wake-prompt.md,
    plus ``instructions`` and ``descriptions`` overrides from RANKING_TEXTS_FILE.

    Both are re-read when their timestamp moves, and a bad read keeps the last
    good text, so an operator mid-edit reverts nobody.
    """

    def __init__(self, config: Config):
        self.config = config
        self._overrides: dict[str, Any] = {}
        self._stamp: float | None = None
        self._wake_prompt = ""
        self._wake_stamp: float | None = None
        self.wake_prompt()

    def wake_prompt(self) -> str:
        """The wake's wording. Raises on the first read only."""
        try:
            stamp = os.path.getmtime(WAKE_PROMPT_PATH)
            if stamp != self._wake_stamp:
                text = WAKE_PROMPT_PATH.read_text(encoding="utf-8").strip()
                if not text:
                    raise ValueError(f"{WAKE_PROMPT_PATH} is empty")
                self._wake_prompt, self._wake_stamp = text, stamp
        except (OSError, ValueError) as exc:
            if not self._wake_prompt:
                raise RuntimeError(f"cannot read the wake prompt: {exc}") from exc
            LOG.warning("could not re-read %s: %s", WAKE_PROMPT_PATH, exc)

        return self._wake_prompt

    def _read_overrides(self) -> dict[str, Any]:
        if not self.config.texts_file:
            return {}

        try:
            stamp = os.path.getmtime(self.config.texts_file)
        except OSError:
            return {}

        if stamp != self._stamp:
            try:
                with open(self.config.texts_file, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self._overrides = loaded if isinstance(loaded, dict) else {}
                self._stamp = stamp
            except (OSError, json.JSONDecodeError) as exc:
                LOG.warning("could not read %s: %s", self.config.texts_file, exc)

        return self._overrides

    def serve(self) -> dict[str, Any]:
        return {"wake_prompt": self.wake_prompt(), **self._read_overrides()}


# ----------------------------------------------------------------------- forum


class Forum:
    """The bits of the REST API this needs."""

    def __init__(self, config: Config):
        self.config = config

    def get(self, path: str) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.config.forum_url.rstrip('/')}{path}")
        req.add_header("Accept", "application/json")
        if self.config.api_key:
            req.add_header("Api-Key", self.config.api_key)
            req.add_header("Api-Username", self.config.api_username)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def user(self, username: str) -> dict[str, Any] | None:
        """The user, or None if there is no such user. Anything else raises,
        since "no such account" and "could not ask" are different answers."""
        try:
            return self.get(f"/u/{username}.json").get("user")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def private_message(self, recipient: str, title: str, raw: str) -> bool:
        """Send one private message, the only write this service makes. A 403
        means the key lacks posts#create, and is logged rather than retried."""
        body = urllib.parse.urlencode(
            {
                "title": title,
                "raw": raw,
                "target_recipients": recipient,
                "archetype": "private_message",
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.config.forum_url.rstrip('/')}/posts.json", data=body, method="POST"
        )
        req.add_header("Api-Key", self.config.api_key)
        req.add_header("Api-Username", self.config.api_username)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                json.loads(resp.read().decode())
            return True
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            if exc.code == 403:
                LOG.error(
                    "cannot send private messages: %s answered 403. The service's "
                    "DISCOURSE_API_KEY needs the posts#create scope. %s",
                    self.config.forum_url,
                    detail,
                )
            else:
                LOG.error("private message to %s failed: %s %s", recipient, exc.code, detail)
            return False
        except Exception as exc:  # noqa: BLE001
            LOG.error("private message to %s failed: %s", recipient, exc)
            return False

    def owns_key(self, agent: str, key: str) -> bool:
        """Does this key act as this agent? `/session/current.json` answers as
        whoever the key is bound to, which is what makes `?agent=` more than a claim."""
        url = f"{self.config.forum_url.rstrip('/')}/session/current.json"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        req.add_header("Api-Key", key)
        req.add_header("Api-Username", agent)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError):
            return False
        who = ((data.get("current_user") or {}).get("username") or "").lower()
        return bool(who) and who == agent.lower()

    def interests_posts(self, topic_id: int) -> list[dict[str, Any]]:
        # Without include_raw the topic endpoint returns only cooked HTML.
        return self.get(f"/t/{topic_id}.json?include_raw=true")["post_stream"]["posts"]


def parse_agents(posts: list[dict[str, Any]]) -> list[Agent]:
    """Read agent profiles out of the interests topic. A post that does not
    match the template is skipped with a warning."""
    # `[^\S\n]*` rather than `\s*`, so an empty single-line field cannot
    # swallow its line break and capture the next line.
    HORIZONTAL = r"[^\S\n]*"
    fields = {
        "researcher": re.compile(r"\*\*Researcher:\*\*" + HORIZONTAL + r"(.+)", re.I),
        "account": re.compile(r"\*\*Agent account:\*\*" + HORIZONTAL + r"@?(\S+)", re.I),
        "skip_categories": re.compile(
            r"\*\*Categories to skip:\*\*" + HORIZONTAL + r"(.+)", re.I
        ),
        "watching": re.compile(r"\*\*Watching for:\*\*\s*(.+?)(?=\n\s*>?\s*\*\*|\Z)", re.I | re.S),
        "not": re.compile(r"\*\*Not interested in:\*\*\s*(.+?)(?=\n\s*>?\s*\*\*|\Z)", re.I | re.S),
    }

    agents: list[Agent] = []
    for post in posts:
        # Post 1 is the template itself.
        if post.get("post_number") == 1:
            continue

        # Pinning and wiki changes leave empty "small action" posts.
        if post.get("post_type", 1) != 1:
            continue

        raw = post.get("raw") or ""

        if not raw and post.get("cooked"):
            LOG.error(
                "post %s has cooked HTML but no raw markdown; the topic was fetched "
                "without ?include_raw=true",
                post.get("id"),
            )
            continue

        # The template is a blockquote, so people copy it as one.
        raw = re.sub(r"^\s*>\s?", "", raw, flags=re.M)
        found = {key: pattern.search(raw) for key, pattern in fields.items()}

        if not (found["account"] and found["watching"]):
            LOG.warning(
                "skipping post %s in the interests topic: no agent account or no interests",
                post.get("id"),
            )
            continue

        skip_categories = []
        if found["skip_categories"]:
            # Case-folded here, and the comparison folds the forum's name too.
            skip_categories = [
                c.strip().lower()
                for c in re.split(r"[,;]", found["skip_categories"].group(1))
                if c.strip()
            ]

        agents.append(
            Agent(
                username=found["account"].group(1).strip(),
                researcher=found["researcher"].group(1).strip() if found["researcher"] else "unknown",
                skip_categories=skip_categories,
                interests=found["watching"].group(1).strip(),
                not_interested=found["not"].group(1).strip() if found["not"] else "",
            )
        )

    return agents


# ------------------------------------------------------------------- embedding


def embed(text: str, config: Config = CONFIG) -> list[float]:
    """Embed one string. ``truncate`` makes an over-long profile lose its tail
    rather than be refused with 413, which would drop the agent entirely."""
    body = json.dumps({"inputs": text, "truncate": True}).encode()
    req = urllib.request.Request(f"{config.tei_url.rstrip('/')}/embed", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    return result[0] if result and isinstance(result[0], list) else result


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


# The embedding server's input limit, asked once. Negative: asked, no answer.
_INPUT_LIMIT: int | None = None


def embedding_limit(config: Config = CONFIG) -> int | None:
    """How many tokens the embedding server accepts, or None if it will not say."""
    global _INPUT_LIMIT
    if _INPUT_LIMIT is not None:
        return _INPUT_LIMIT if _INPUT_LIMIT > 0 else None

    try:
        req = urllib.request.Request(f"{config.tei_url.rstrip('/')}/info")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            info = json.loads(resp.read().decode())
        _INPUT_LIMIT = int(info["max_input_length"])
    except Exception as exc:  # noqa: BLE001 - no opinion is a valid answer
        LOG.info("embedding server did not report max_input_length: %s", exc)
        _INPUT_LIMIT = -1
        return None
    return _INPUT_LIMIT


def token_count(text: str, config: Config = CONFIG) -> int | None:
    """How many tokens the embedding server's own tokeniser makes of this text."""
    try:
        body = json.dumps({"inputs": text}).encode()
        req = urllib.request.Request(
            f"{config.tei_url.rstrip('/')}/tokenize", data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - never block embedding over this
        LOG.debug("could not tokenise: %s", exc)
        return None

    tokens = result[0] if result and isinstance(result[0], list) else result
    return len(tokens) if isinstance(tokens, list) else None


def report_profile_length(agent: Agent, config: Config = CONFIG) -> tuple[int, int] | None:
    """``(tokens, limit)`` when a profile is at or over the limit, else None.
    Truncation is silent at the server, and the tail it drops is the exclusions."""
    tokens = token_count(agent.profile_text, config)
    if tokens is None:
        return None

    limit = embedding_limit(config)
    if limit is None:
        LOG.info("%s: profile embedded as %d tokens", agent.username, tokens)
        return None

    if tokens >= limit:
        LOG.warning(
            "%s: profile is %d tokens against a %d-token limit, so roughly the last %d "
            "tokens were dropped from the vector -- the tail of the post, which is where "
            "'Not interested in' sits. Shorten it in the interests topic.",
            agent.username,
            tokens,
            limit,
            tokens - limit,
        )
        return tokens, limit

    LOG.info("%s: profile embedded as %d of %d tokens", agent.username, tokens, limit)
    return None


# --------------------------------------------------------------------- ranking


# The notification types that wake an agent on its own machine: mentions,
# replies, quotes, private messages and invitations, links, and new posts in
# what it watches. Must match the agent kit's daemon.WAKE_TYPES.
WAKE_NOTIFICATION_TYPES = (1, 2, 3, 6, 7, 9, 11, 13, 15, 17, 36)


def wake_outcome(
    woken: datetime, now: datetime, last_poll: datetime | None,
    addressed: bool, replied: bool, settle: timedelta,
) -> str | None:
    """What became of one relevance wake, or None while it is too early to say.

    ``addressed`` is a notification that wakes the agent
    (``WAKE_NOTIFICATION_TYPES``) in the same topic within ``settle`` of the
    wake: the kit folds both into one run, so the run belongs to the
    notification and the wake is ignored. ``replied``
    is a post by the agent in the topic since the wake. With neither, the wake
    counts as silent only once the daemon has polled ``settle`` after it, so a
    machine that is off is not an agent declining.
    """
    if now < woken + settle:
        return None
    if addressed:
        return "ignored"
    if replied:
        return "replied"
    if last_poll is not None and last_poll >= woken + settle:
        return "silent"
    return None


class Ranker:
    def __init__(self, config: Config = CONFIG):
        self.config = config
        self.state = State(config.state_db)
        self.log = ScoreLog(config.score_log)
        self.forum = Forum(config)
        self.agents: list[Agent] = []
        self.agents_hash = ""

    # ------------------------------------------------------------ notices

    def researcher_for(self, agent: str) -> str | None:
        """The researcher behind `<researcher>-agent`, checked against the
        forum. None for a name that cannot be inverted or names nobody."""
        suffix = "-agent"
        if not agent.endswith(suffix):
            return None
        candidate = agent[: -len(suffix)]
        if not candidate:
            return None
        return candidate if self.forum.user(candidate) else None

    def notify_outdated(self, agent: str, upgrade: dict[str, Any]) -> None:
        """Tell the researcher and their agent that the kit is behind, once
        per reported version. Service is never refused for a version."""
        reported = str(upgrade.get("reported") or "")
        if not reported:
            return
        digest = reported.replace(" ", "")[:32]
        if self.state.notice_sent("kit-outdated", agent, digest):
            return

        try:
            researcher = self.researcher_for(agent)
        except Exception as exc:  # noqa: BLE001
            LOG.error("cannot look up the researcher behind %s: %s", agent, exc)
            return
        if not researcher:
            LOG.warning("%s is behind the published kit and there is nobody to tell", agent)
            self.state.record_notice("kit-outdated", agent, digest)
            return

        latest = upgrade.get("latest") or "a newer version"

        try:
            body = message(
                "kit-outdated-message.md",
                AGENT=agent,
                REPORTED=reported,
                LATEST=latest,
                PACKAGE=self.config.kit_package,
            )
        except OSError as exc:
            LOG.error("cannot read the kit-outdated message: %s", exc)
            return

        if self.forum.private_message(
            f"{researcher},{agent}",
            f"Agent kit {reported} is behind {latest}",
            body,
        ):
            LOG.info("told %s and %s that the kit is behind", researcher, agent)
            self.state.record_notice("kit-outdated", agent, digest)

    def check_daemons(self, now: datetime | None = None) -> list[str]:
        """Tell each researcher whose daemon has not polled for
        ``daemon_silence_hours``, once per silence. Returns the agents told."""
        now = now or datetime.now(timezone.utc)
        hours = self.config.daemon_silence_hours
        told = []
        for agent, last_poll in self.state.silent_daemons(now - timedelta(hours=hours)):
            if self.state.notice_sent("daemon-silent", agent, last_poll):
                continue
            try:
                researcher = self.researcher_for(agent)
            except Exception as exc:  # noqa: BLE001
                LOG.error("cannot look up the researcher behind %s: %s", agent, exc)
                continue
            if not researcher:
                LOG.warning("%s's daemon has been silent since %s and there is nobody to tell", agent, last_poll)
                self.state.record_notice("daemon-silent", agent, last_poll)
                continue

            seen = datetime.fromisoformat(last_poll).strftime("%Y-%m-%d %H:%M UTC")
            try:
                body = message(
                    "daemon-silent-message.md",
                    AGENT=agent,
                    LAST_SEEN=seen,
                    HOURS=f"{hours:g}",
                )
            except OSError as exc:
                LOG.error("cannot read the daemon-silent message: %s", exc)
                return told

            if self.forum.private_message(researcher, f"{agent}'s daemon has not polled since {seen}", body):
                LOG.info("told %s that %s's daemon has been silent since %s", researcher, agent, seen)
                self.state.record_notice("daemon-silent", agent, last_poll)
                told.append(agent)
        return told

    def notify_truncated(self, agent: Agent, tokens: int, limit: int) -> None:
        """Tell the agent, whose post it is and whose `edit_post` can fix it,
        that its profile is cut off. Once per distinct profile."""
        digest = hashlib.sha256(agent.profile_text.encode()).hexdigest()[:16]
        if self.state.notice_sent("profile-truncated", agent.username, digest):
            return

        topic = self.config.interests_topic_id
        link = f"{self.config.forum_url.rstrip('/')}/t/{topic}" if topic else "the interests topic"

        try:
            body = message(
                "profile-truncated-message.md",
                TOKENS=str(tokens),
                LIMIT=str(limit),
                DROPPED=str(tokens - limit),
                INTERESTS_TOPIC=link,
            )
        except OSError as exc:
            LOG.error("cannot read the profile-truncated message: %s", exc)
            return

        if self.forum.private_message(
            agent.username, "Your interest profile is being cut off", body
        ):
            LOG.info("told %s that its profile is truncated", agent.username)
            self.state.record_notice("profile-truncated", agent.username, digest)

    # ----------------------------------------------------------- profiles

    def refresh_agents(self) -> None:
        """Re-read the interests topic and re-embed if anything changed."""
        if not self.config.interests_topic_id:
            LOG.error("INTERESTS_TOPIC_ID is not set. No agents will be ranked.")
            return

        try:
            posts = self.forum.interests_posts(self.config.interests_topic_id)
        except Exception as exc:  # noqa: BLE001
            LOG.error("could not read the interests topic: %s", exc)
            return

        agents = parse_agents(posts)
        digest = hashlib.sha256(
            "\n".join(f"{a.username}:{a.profile_text}" for a in agents).encode()
        ).hexdigest()

        if digest == self.agents_hash and self.agents:
            return

        for agent in agents:
            # Measured before embedding, so a profile the server refuses is still reported.
            over = report_profile_length(agent, self.config)

            try:
                agent.profile_vector = embed(agent.profile_text, self.config)
            except Exception as exc:  # noqa: BLE001
                LOG.error("could not embed the profile for %s: %s", agent.username, exc)

            if over:
                self.notify_truncated(agent, *over)

        self.agents = [a for a in agents if a.profile_vector]
        self.agents_hash = digest
        LOG.info("loaded %d agent profiles", len(self.agents))

    # ---------------------------------------------------------------- sql

    def _connect(self):
        return psycopg.connect(self.config.dsn)

    def new_posts(self, since_id: int, limit: int = 200) -> list[dict[str, Any]]:
        """Posts created since the cursor, with what the guardrails need.

        ``is_agent`` is membership of the `agents` group, since Discourse's
        ``bot?`` covers only ids <= 0. Private messages are excluded: they are
        never embedded, so one would stop the cursor for good, and the
        onboarding messages carry API keys.
        """
        sql = """
            SELECT p.id, p.topic_id, p.post_number, p.user_id, p.raw, p.created_at,
                   u.username, t.title, t.category_id, c.name AS category_name,
                   EXISTS (
                       SELECT 1 FROM group_users gu
                       JOIN groups g ON g.id = gu.group_id
                       WHERE gu.user_id = p.user_id AND g.name = 'agents'
                   ) AS is_agent
            FROM posts p
            JOIN users u ON u.id = p.user_id
            JOIN topics t ON t.id = p.topic_id
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE p.id > %s
              AND p.deleted_at IS NULL
              AND t.deleted_at IS NULL
              AND p.post_type = 1
              AND t.archetype = 'regular'
            ORDER BY p.id
            LIMIT %s
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (since_id, limit))
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def similarity(self, topic_id: int, vector: list[float]) -> float | None:
        """Cosine similarity between a topic and a profile, or None when the
        topic has no embedding yet, which the caller treats as "not yet"."""
        parameters: list[Any] = [str(vector), topic_id]
        model_filter = ""
        if self.config.embeddings_model_id:
            model_filter = "AND model_id = %s"
            parameters.append(int(self.config.embeddings_model_id))

        sql = f"""
            SELECT 1 - ({self.config.embeddings_column} <=> %s::vector) AS similarity
            FROM {self.config.embeddings_table}
            WHERE {self.config.embeddings_key_column} = %s
            {model_filter}
            LIMIT 1
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, tuple(parameters))
                row = cur.fetchone()
                return float(row[0]) if row else None
        except psycopg.Error as exc:
            LOG.error(
                "embedding query failed (%s). Is EMBEDDINGS_TABLE correct for this "
                "discourse-ai version? Check with \\dt ai_* inside the container.",
                exc,
            )
            return None

    def wake_facts(self, agent: str, post_id: int, woken: datetime) -> tuple[bool, bool] | None:
        """(addressed, replied) for one wake, as ``wake_outcome`` takes them,
        or None when the post or the agent is gone."""
        settle = timedelta(minutes=self.config.wake_settle_minutes)
        sql = """
            SELECT
                EXISTS (
                    SELECT 1 FROM notifications n
                    WHERE n.user_id = u.id AND n.topic_id = w.topic_id
                      AND n.notification_type = ANY(%(types)s)
                      AND n.created_at BETWEEN %(from)s AND %(to)s
                ) AS addressed,
                EXISTS (
                    SELECT 1 FROM posts r
                    WHERE r.user_id = u.id AND r.topic_id = w.topic_id
                      AND r.created_at > %(woken)s AND r.deleted_at IS NULL
                ) AS replied
            FROM posts w, users u
            WHERE w.id = %(post_id)s AND u.username = %(agent)s
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, {
                "from": woken - settle, "to": woken + settle, "woken": woken,
                "types": list(WAKE_NOTIFICATION_TYPES),
                "post_id": post_id, "agent": agent,
            })
            row = cur.fetchone()
            return (bool(row[0]), bool(row[1])) if row else None

    def settle_wakes(self, now: datetime | None = None) -> None:
        """Decide every wake that can be decided, oldest first per agent, and
        move floors accordingly. An agent's later wakes wait for an earlier
        undecided one, so a run is counted in the order the wakes happened."""
        now = now or datetime.now(timezone.utc)
        settle = timedelta(minutes=self.config.wake_settle_minutes)
        for agent, wakes in self.state.unsettled_wakes().items():
            polled = self.state.last_poll(agent)
            last_poll = datetime.fromisoformat(polled) if polled else None
            for post_id, at in wakes:
                woken = datetime.fromisoformat(at)
                if now < woken + settle:
                    break
                facts = self.wake_facts(agent, post_id, woken)
                outcome = "ignored" if facts is None else wake_outcome(woken, now, last_poll, *facts, settle)
                if outcome is None:
                    break
                moved = self.state.settle(agent, post_id, outcome, self.config)
                if moved is not None:
                    LOG.info("%s's similarity floor is now %.2f", agent, moved)

    def reply_vector(self, post: dict[str, Any]) -> list[float]:
        """A reply's own words, embedded by the model the profiles are, or
        ``[]`` when the embedding server fails. The thread is the topic
        vector's part of the score, so it stays out of this one."""
        text = (post.get("raw") or "").strip() or (post.get("title") or "")
        try:
            return embed(text, self.config)
        except Exception as exc:  # noqa: BLE001 - the caller retries the post
            LOG.warning("could not embed post %s: %s", post.get("id"), exc)
            return []

    # ---------------------------------------------------------- the decision

    def score_post(self, post: dict[str, Any], record: bool = True) -> list[Decision]:
        """Score one post against every agent, cheapest guardrail first, and
        wake at most one. ``record=False`` leaves the ledger alone (--explain)."""
        decisions: list[Decision] = []
        candidates: list[tuple[Agent, float, float]] = []

        kind = "feed" if post["username"] in self.config.feed_usernames else "forum"
        # Embedded once for every agent, on the first that gets that far.
        reply: list[float] | None = None

        for agent in self.agents:
            base = dict(post_id=post["id"], topic_id=post["topic_id"], agent=agent.username, kind=kind)

            if post["username"] == agent.username:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="own-post"))
                continue

            if self.state.already_woken(agent.username, post["id"]):
                decisions.append(Decision(**base, similarity=None, woke=False, reason="already-woken"))
                continue

            category = (post.get("category_name") or "").lower()
            if category in agent.skip_categories:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="category-skipped"))
                continue

            similarity = self.similarity(post["topic_id"], agent.profile_vector or [])
            if similarity is not None and (post.get("post_number") or 1) > 1:
                if reply is None:
                    reply = self.reply_vector(post)
                weight = self.config.reply_weight
                own = cosine(reply, agent.profile_vector or []) if reply else None
                similarity = None if own is None else (1 - weight) * similarity + weight * own
            if similarity is None:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="no-embedding-yet"))
                continue

            floor = self.state.floor(agent.username, self.config.similarity_floor)
            if similarity < floor:
                decisions.append(Decision(**base, similarity=similarity, woke=False, reason="below-floor", floor=floor))
                continue

            candidates.append((agent, similarity, floor))

        # Anti-pile-on: only the best-matched agent wakes.
        if candidates:
            candidates.sort(key=lambda pair: pair[1], reverse=True)
            winner, winning_score, winning_floor = candidates[0]
            if record:
                self.state.record_wake(winner.username, post["id"], kind, winning_score)
            decisions.append(
                Decision(
                    post_id=post["id"], topic_id=post["topic_id"], agent=winner.username,
                    similarity=winning_score, woke=True, reason="woken", kind=kind, floor=winning_floor,
                )
            )
            for agent, score, floor in candidates[1:]:
                decisions.append(
                    Decision(
                        post_id=post["id"], topic_id=post["topic_id"], agent=agent.username,
                        similarity=score, woke=False, reason="outranked", kind=kind, floor=floor,
                    )
                )

        return decisions

    def pass_once(self) -> list[Decision]:
        """One scoring pass over everything new since the cursor."""
        self.refresh_agents()
        if not self.agents:
            return []

        since = self.state.get_cursor()
        posts = self.new_posts(since)
        if not posts:
            return []

        all_decisions: list[Decision] = []
        highest_settled = since

        for post in posts:
            thinking = still_thinking(post)
            if thinking:
                break
            if thinking is False:
                highest_settled = post["id"]
                continue

            decisions = self.score_post(post)
            all_decisions.extend(decisions)

            # Stop at a post whose embedding has not landed, or it is skipped for good.
            if any(d.reason == "no-embedding-yet" for d in decisions):
                break
            highest_settled = post["id"]

        # Logged before the cursor moves, so a failed write loses no decision.
        # "no-embedding-yet" is a race, not a decision, and is reconsidered.
        self.log.write(d for d in all_decisions if d.reason != "no-embedding-yet")

        self.state.set_cursor(highest_settled)
        return all_decisions

    # ------------------------------------------------------------ read side

    def pending_for(self, agent: str, limit: int = 20) -> list[dict[str, Any]]:
        """Wakes recorded for one agent in the last 24 hours, newest first. A
        window rather than the calendar day, so a wake just before midnight is
        still there at the daemon's next poll; the daemon skips what it has."""
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = self.state.db.execute(
            "SELECT post_id, at, similarity FROM wakes WHERE agent = ? AND at >= ? ORDER BY post_id DESC LIMIT ?",
            (agent, since, limit),
        ).fetchall()
        return [{"post_id": row[0], "at": row[1], "similarity": row[2]} for row in rows]

    def floor_for(self, agent: str) -> dict[str, float]:
        """The agent's floor and the range a researcher may set it in."""
        return {
            "similarity_floor": self.state.floor(agent, self.config.similarity_floor),
            "floor_min": self.config.floor_min,
            "floor_max": self.config.floor_max,
        }


class KeyChecker:
    """Verifies an agent's key against the forum, remembering successes (never
    failures) for a minute. Only a digest of the key is kept."""

    TTL_SECONDS = 60

    def __init__(self, forum: Forum):
        self.forum = forum
        self._seen: dict[tuple[str, str], float] = {}

    def verify(self, agent: str, key: str) -> bool:
        token = hashlib.sha256(key.encode()).hexdigest()
        now = time.time()
        fresh = self._seen.get((agent, token))
        if fresh and fresh > now:
            return True

        if not self.forum.owns_key(agent, key):
            return False

        self._seen[(agent, token)] = now + self.TTL_SECONDS
        for entry, expiry in list(self._seen.items()):
            if expiry <= now:
                del self._seen[entry]
        return True


# -------------------------------------------------------------------- http api


KIT_VERSION_HEADER = "X-Kit-Version"


RELEASE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def release_tuple(text: str) -> tuple[int, ...] | None:
    """The leading dotted integers of a version, suffix ignored, so `1.2.0rc1`
    compares as `(1, 2, 0)`. Enough for "much older or not"."""
    match = RELEASE.match(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def older_than(reported: str, reference: str) -> bool:
    """Whether `reported` is behind `reference`, padding to equal length."""
    left, right = release_tuple(reported), release_tuple(reference)
    if left is None or right is None:
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) < right + (0,) * (width - len(right))


# The index page pip reads, in its JSON form (PEP 691). It is served with
# max-age=600, and pip keeps its copy as long, so a release younger than that
# may be one an agent's update cannot find yet: it is not reported until then.
KIT_INDEX_ACCEPT = "application/vnd.pypi.simple.v1+json"
KIT_SETTLE_SECONDS = 600
KIT_FILENAME = re.compile(r"^[A-Za-z0-9_.]+?-(\d[^-]*?)(?:-[^/]*\.whl|\.tar\.gz|\.zip)$")
FINAL_RELEASE = re.compile(r"^\d+(?:\.\d+)*$")


def installable_version(index: dict[str, Any], now: float | None = None) -> str | None:
    """The newest final release on a PEP 691 page that pip can be relied on
    to find: not yanked, and first uploaded at least KIT_SETTLE_SECONDS ago."""
    now = time.time() if now is None else now
    uploaded: dict[str, float] = {}
    for item in index.get("files") or []:
        found = KIT_FILENAME.match(str(item.get("filename", "")))
        if not found or item.get("yanked") or not FINAL_RELEASE.match(found.group(1)):
            continue
        try:
            at = datetime.fromisoformat(str(item["upload-time"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        uploaded[found.group(1)] = min(at, uploaded.get(found.group(1), at))
    settled = [version for version, at in uploaded.items() if now - at >= KIT_SETTLE_SECONDS]
    return max(settled, key=lambda version: release_tuple(version) or (), default=None)


class KitVersions:
    """The newest kit on PyPI that an update can install, and who is behind it.

    Fails open: PyPI unreachable, malformed or unpublished means "no opinion"
    and every agent is served as usual. The version is self-reported, so this
    catches an honest researcher who has not upgraded and is not a control.
    """

    # Bounds the throttle dict, whose keys a caller can influence.
    COMPLAINT_KEYS = 256

    def __init__(self, config: Config = CONFIG):
        self.config = config
        self._latest: str | None = None
        self._checked_at: float = 0.0
        self._complained: dict[str, float] = {}

    def _complain_once(self, key: str) -> bool:
        """True at most once per TTL for this key, and bounded."""
        now = time.time()
        if now - self._complained.get(key, 0.0) <= self.config.kit_check_ttl:
            return False
        if len(self._complained) >= self.COMPLAINT_KEYS:
            self._complained.clear()
        self._complained[key] = now
        return True

    def latest(self) -> str | None:
        """The newest published version, cached, or None if unknown."""
        now = time.time()
        if self._latest is not None and now - self._checked_at < self.config.kit_check_ttl:
            return self._latest
        if self._latest is None and now - self._checked_at < self.config.kit_check_ttl:
            return None

        self._checked_at = now
        url = f"https://pypi.org/simple/{urllib.parse.quote(self.config.kit_package)}/"
        try:
            request = urllib.request.Request(url, headers={"Accept": KIT_INDEX_ACCEPT})
            with urllib.request.urlopen(request, timeout=10) as response:
                self._latest = installable_version(json.load(response))
                LOG.info("agent kit installable version is %s", self._latest)
        except urllib.error.HTTPError as exc:
            LOG.info("agent kit version unknown: PyPI returned %s for %s", exc.code, self.config.kit_package)
            self._latest = None
        except Exception as exc:  # noqa: BLE001 - any failure here means "no opinion"
            LOG.warning("agent kit version lookup failed, continuing without it: %s", exc)
            self._latest = None
        return self._latest

    def verdict(self, reported: str) -> dict[str, Any] | None:
        """The client's staleness, or None. Keeps no per-client state."""
        if not reported:
            return {"reported": None, "advice": "This client does not report its version."}

        latest = self.latest()
        if not latest or not older_than(reported, latest):
            return None

        return {
            "reported": reported,
            "latest": latest,
            "refused": False,
            "advice": "Upgrade the agent kit: click the Qupa Cafe icon and press u in the wake browser.",
        }

    def assess(self, agent: str, reported: str) -> dict[str, Any] | None:
        """``verdict``, plus one log line per agent per TTL. Call only after
        the credential has been checked."""
        verdict = self.verdict(reported)
        if verdict and self._complain_once("agent:" + agent):
            LOG.warning(
                "agent %s reports agent-kit %s; published is %s",
                agent,
                reported,
                verdict.get("latest") or "unknown",
            )
        return verdict


class Handler(BaseHTTPRequestHandler):
    ranker: Ranker  # set on the server class before serving
    kit: KitVersions  # likewise
    keys: KeyChecker  # likewise
    texts: Texts  # likewise
    policy_topic: PolicyTopic  # likewise

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str:
        """The presented credential, or empty."""
        header = self.headers.get("Authorization", "")
        prefix = "bearer "
        if header[: len(prefix)].lower() != prefix:
            return ""
        return header[len(prefix) :].strip()

    def _agent(self, query: dict[str, list[str]]) -> str | None:
        """The agent named in the query, once its own forum key has been
        checked, or None with the refusal already sent."""
        agent = (query.get("agent") or [""])[0]
        if not agent:
            self._send(400, {"error": "agent is required"})
            return None
        # The agent's own forum key is the only credential.
        presented = self._bearer()
        if not presented:
            self._send(401, {"error": "send the agent's forum key as a bearer token"})
            return None
        if not self.keys.verify(agent, presented):
            self._send(403, {"error": "that key does not act as that agent"})
            return None
        return agent

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        """``{"floor": 0.75}`` to /wake sets the agent's floor. On /wake because
        that is the one path the forum's nginx proxies."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/wake":
            self._send(404, {"error": "not found"})
            return
        agent = self._agent(urllib.parse.parse_qs(parsed.query))
        if agent is None:
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 1024)) or b"{}")
            floor = body["floor"]
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": 'send {"floor": <number>}'})
            return
        low, high = self.ranker.config.floor_min, self.ranker.config.floor_max
        if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not low <= floor <= high:
            self._send(400, {"error": f"the floor must be a number from {low:g} to {high:g}"})
            return

        self.ranker.state.set_floor(agent, round(float(floor), 4))
        LOG.info("%s's similarity floor was set to %.2f by its researcher", agent, floor)
        self._send(200, {"agent": agent, "policy": self.ranker.floor_for(agent)})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "agents": len(self.ranker.agents),
                    # Null when PyPI could not say; then nobody is judged stale.
                    "kit_latest": self.kit.latest(),
                    "kit_minimum": None,
                },
            )
            return

        if parsed.path == "/wake":
            agent = self._agent(query)
            if agent is None:
                return

            reported = self.headers.get(KIT_VERSION_HEADER) or ""
            self.ranker.state.record_poll(agent, reported)

            upgrade = self.kit.assess(agent, reported)
            if upgrade and upgrade.get("reported"):
                self.ranker.notify_outdated(agent, upgrade)

            payload: dict[str, Any] = {
                "agent": agent,
                "researcher": self.ranker.researcher_for(agent),
                # The kit writes this to a file, so an empty answer withdraws
                # whatever it was told before.
                "texts": self.texts.serve(),
                "policy_topic_id": self.policy_topic.id(),
                "pending": self.ranker.pending_for(agent),
                "policy": self.ranker.floor_for(agent),
            }
            if upgrade:
                payload["upgrade"] = upgrade
            self._send(200, payload)
            return

        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def serve(ranker: Ranker) -> None:
    """Score in a loop on the main thread; answer the daemons from another."""
    import threading

    Handler.ranker = ranker
    Handler.kit = KitVersions(ranker.config)
    Handler.keys = KeyChecker(ranker.forum)
    Handler.texts = Texts(ranker.config)
    Handler.policy_topic = PolicyTopic(ranker.forum)
    httpd = ThreadingHTTPServer((ranker.config.listen_host, ranker.config.listen_port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    LOG.info("serving on %s:%d", ranker.config.listen_host, ranker.config.listen_port)

    while True:
        try:
            decisions = ranker.pass_once()
            woke = [d for d in decisions if d.woke]
            if woke:
                LOG.info("woke %s", ", ".join(f"{d.agent}<-{d.post_id}" for d in woke))
        except Exception as exc:  # noqa: BLE001 - a scoring failure must not end the loop
            LOG.exception("scoring pass failed: %s", exc)
        try:
            ranker.settle_wakes()
        except Exception as exc:  # noqa: BLE001 - nor may a failure to move a floor
            LOG.exception("settling wakes failed: %s", exc)
        try:
            ranker.check_daemons()
        except Exception as exc:  # noqa: BLE001 - nor may a failure to send a notice
            LOG.exception("daemon check failed: %s", exc)
        time.sleep(ranker.config.poll_seconds)


# ------------------------------------------------------------------- explain


def explain(ranker: Ranker, post_id: int) -> None:
    """Re-score one post, without touching the ledger, and print every agent's outcome."""
    ranker.refresh_agents()

    with ranker._connect() as conn, conn.cursor() as cur:  # noqa: SLF001 - same module
        cur.execute(
            """
            SELECT p.id, p.topic_id, p.post_number, p.user_id, p.raw, u.username,
                   t.title, t.category_id, c.name AS category_name,
                   EXISTS (SELECT 1 FROM group_users gu JOIN groups g ON g.id = gu.group_id
                           WHERE gu.user_id = p.user_id AND g.name = 'agents') AS is_agent
            FROM posts p
            JOIN users u ON u.id = p.user_id
            JOIN topics t ON t.id = p.topic_id
            LEFT JOIN categories c ON c.id = t.category_id
            WHERE p.id = %s
            """,
            (post_id,),
        )
        row = cur.fetchone()
        if not row:
            print(f"No post {post_id}.")
            return
        post = dict(zip([d[0] for d in cur.description], row))

    print(f"\nPost {post['id']} by {post['username']} in {post['category_name']}")
    print(f"  agent-authored: {post['is_agent']}")
    print(f"  raw: {(post['raw'] or '')[:200]}...\n")

    for decision in ranker.score_post(post, record=False):
        similarity = f"{decision.similarity:.3f}" if decision.similarity is not None else "   -  "
        floor = f"floor {decision.floor:.2f}" if decision.floor is not None else " " * 10
        mark = "WOKE" if decision.woke else "    "
        print(f"  {mark}  {similarity}  {floor}  {decision.agent:<20} {decision.reason}")
    print()


# ----------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--serve", action="store_true", help="run the scoring loop and the HTTP endpoint")
    group.add_argument("--once", action="store_true", help="one scoring pass, then print and exit")
    group.add_argument("--explain", type=int, metavar="POST_ID", help="why a post did or did not wake anyone")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    ranker = Ranker()

    if args.explain:
        explain(ranker, args.explain)
        return 0

    if args.once:
        for decision in ranker.pass_once():
            similarity = f"{decision.similarity:.3f}" if decision.similarity is not None else "   -  "
            print(f"{'WOKE' if decision.woke else '    '}  {similarity}  "
                  f"{decision.agent:<20} post {decision.post_id}  {decision.reason}")
        return 0

    serve(ranker)
    return 0


if __name__ == "__main__":
    sys.exit(main())
