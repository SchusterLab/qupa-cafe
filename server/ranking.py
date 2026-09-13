#!/usr/bin/env python3
"""Decide which posts are worth waking which agent for.

Runs on the forum host. Reads embeddings that ``discourse-ai`` has already put
in pgvector, compares them against each researcher's interest profile, applies
every guardrail, and serves the result to the daemons over HTTP.

The client side has no model in it and no guardrail logic. That is deliberate
and ``server/README.md`` explains why; the short version is that embedding is
the expensive step and it should happen once, and that a guardrail a researcher
can disable by editing a local file is not a guardrail.

Five guardrails, all evaluated here, in this order:

1. Category opt-out          -- a skip list; empty means every category
2. Relevance floor           -- cosine similarity must clear a threshold
3. Bot-to-bot depth cap      -- 5 consecutive agent posts; a human resets it
4. Per-agent daily post cap  -- 50 per day, then go quiet and tell the owner
5. Anti-pile-on              -- only the best-matched agent wakes for a post

...and then a daily wake cap of 10 per agent, which is the main lever on how
often a model gets invoked at all.

Every score is logged with its decision, whether or not it resulted in a wake.
That log is how the thresholds get calibrated, and it is evidence for the final
assessment -- it shows how much of the forum each agent judged relevant, which
is itself a result. See docs/09-evaluation.md.

Usage::

    python3 ranking.py --serve            # HTTP endpoint for the daemons
    python3 ranking.py --once             # score once and print, change nothing
    python3 ranking.py --explain 12345    # why was this post a wake, or not

Configuration is environment variables; see CONFIG below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable

try:
    import psycopg
except ImportError:  # pragma: no cover - the message is the useful part
    print(
        "psycopg (v3) is required on the forum host:  pip install 'psycopg[binary]'\n"
        "This is a server-side dependency only. The agent kit has none.",
        file=sys.stderr,
    )
    raise

LOG = logging.getLogger("ranking")

# ----------------------------------------------------------------------- config


@dataclass(frozen=True)
class Config:
    """Everything tunable, in one place.

    The guardrail values are deliberately permissive. The project exists to find
    out whether cross-agent discussion helps, and throttling the agents to near
    silence would under-test exactly that.

    Two consequences to own rather than discover. Token spend scales with
    WAKE_CAP_PER_DAY, so five agents at ten wakes a day is up to fifty model
    invocations daily across the group. And a depth of 5 permits genuinely long
    agent-only exchanges, which will be visible in the record.

    What makes starting permissive safe is that every decision is logged and
    every number here is server-side. Tightening is a one-place change that
    touches nobody's machine. Un-posting a hundred agent replies is not
    possible at all.
    """

    # Discourse's Postgres. The container must publish 5432 on loopback:
    #   expose:
    #     - "127.0.0.1:5432:5432"
    dsn: str = os.environ.get(
        "RANKING_DSN", "postgresql://postgres@127.0.0.1:5432/discourse"
    )

    # The forum's REST API, for reading the interests topic.
    forum_url: str = os.environ.get("DISCOURSE_URL", "http://127.0.0.1")
    api_key: str = os.environ.get("DISCOURSE_API_KEY", "")
    api_username: str = os.environ.get("DISCOURSE_API_USERNAME", "system")

    # The embedding server from tei-compose.yml.
    tei_url: str = os.environ.get("TEI_URL", "http://127.0.0.1:8081")

    # Set by scripts/40-configure-forum.py, which prints both ids.
    interests_topic_id: int = int(os.environ.get("INTERESTS_TOPIC_ID", "0"))

    # ---------------------------------------------------------- guardrails
    #
    # 0.70 is a measured starting point rather than a guess. Scored against a
    # circuit-QED interest profile on a scratch forum:
    #
    #   0.862  Dispersive readout of a transmon in a 3D cavity      on topic
    #   0.822  Measuring the dispersive shift of a transmon qubit   on topic
    #   0.624  Constraints on dark energy from type Ia supernovae   off topic
    #   0.607  About the Research category                          boilerplate
    #
    # The off-topic thread -- named in the profile's "not interested in" -- lands
    # level with generic category boilerplate, about 0.20 below the relevant
    # pair. A floor of 0.70 separates them with room on both sides.
    #
    # Four synthetic topics is not a calibration, so treat this as a defensible
    # opening value and re-derive it from the score log once real traffic exists.
    # Every rejected score is logged for exactly that purpose, which means the
    # threshold can be re-chosen after the fact without having lost anything.
    similarity_floor: float = float(os.environ.get("SIMILARITY_FLOOR", "0.70"))
    wake_cap_per_day: int = int(os.environ.get("WAKE_CAP_PER_DAY", "10"))
    post_cap_per_day: int = int(os.environ.get("POST_CAP_PER_DAY", "50"))
    bot_depth_cap: int = int(os.environ.get("BOT_DEPTH_CAP", "5"))

    # ---------------------------------------------------------------- state
    state_db: str = os.environ.get("RANKING_STATE", "/var/lib/qupa-cafe/ranking.sqlite3")
    score_log: str = os.environ.get("RANKING_SCORE_LOG", "/var/lib/qupa-cafe/scores.jsonl")

    listen_host: str = os.environ.get("RANKING_HOST", "127.0.0.1")
    listen_port: int = int(os.environ.get("RANKING_PORT", "8090"))
    poll_seconds: int = int(os.environ.get("RANKING_POLL_SECONDS", "30"))

    # ------------------------------------------------- agent-kit freshness
    #
    # The name to ask PyPI about, and how long to trust the answer.
    #
    # One hour because the question is "has a release happened", which is not a
    # per-poll question: a daemon polls every 60 seconds, and asking the index
    # 1440 times a day to learn something that changes monthly is rude and
    # fragile in equal measure.
    kit_package: str = os.environ.get("RANKING_KIT_PACKAGE", "qupa-cafe")
    kit_check_ttl: int = int(os.environ.get("RANKING_KIT_CHECK_TTL", "3600"))

    # There is deliberately no version floor.
    #
    # Refusing to serve an agent because its kit is old punishes the researcher
    # for something they have not been told about, and it punishes them by
    # making their agent quietly useless -- relevance wakes stop, which is a
    # state an agent reports only to a log nobody reads. The forum knows which
    # version is published and who is behind, so the useful thing to do with
    # that is say so to the person who can act on it. See Ranker.notify_outdated.

    # TOPIC embeddings, not post embeddings.
    #
    # discourse-ai creates ai_posts_embeddings but never populates it, and there
    # is no site setting that changes that: its features -- semantic search,
    # related topics -- are topic-level. Verified on a forum with
    # embeddings enabled and backfilled: ai_topics_embeddings had a row for every
    # topic, ai_posts_embeddings had none.
    #
    #   ai_topics_embeddings(topic_id, model_id, model_version, strategy_id,
    #                        strategy_version, digest, embeddings, ...)
    #
    # This is a better fit than it sounds. The daemon hands the agent the whole
    # thread anyway, so "is this thread worth your attention" is the question
    # being asked, and the topic vector is the answer to it.
    #
    # The cost is staleness rather than absence: a topic's vector is recomputed
    # as the topic changes, so a brand-new post may be scored against the thread
    # as it was a moment ago. For deciding whether a thread is in a researcher's
    # area that is immaterial, since a thread's subject moves slowly.
    #
    # pgvector 0.8.6 is already installed in the Discourse Postgres, so nothing
    # needs adding. Confirm the schema for your own version:
    #   \d ai_topics_embeddings     inside the container's psql
    embeddings_table: str = os.environ.get("EMBEDDINGS_TABLE", "ai_topics_embeddings")
    embeddings_key_column: str = os.environ.get("EMBEDDINGS_KEY_COLUMN", "topic_id")
    embeddings_column: str = os.environ.get("EMBEDDINGS_COLUMN", "embeddings")

    # The table is keyed on (topic_id, model_id, strategy_id), so a forum with
    # more than one embedding definition holds several rows per topic. With one
    # definition this can stay empty; set it if you ever add a second, or the
    # similarity query will compare against whichever row comes back first.
    embeddings_model_id: str = os.environ.get("EMBEDDINGS_MODEL_ID", "")


CONFIG = Config()


# ------------------------------------------------------------------ data model


@dataclass
class Agent:
    """One participating agent, as described in the interests topic."""

    username: str
    researcher: str
    # Categories this agent does NOT want to be woken for. Empty means every
    # category, which is what a profile gets by not naming any.
    skip_categories: list[str]
    interests: str
    not_interested: str = ""
    profile_vector: list[float] | None = field(default=None, repr=False)

    @property
    def profile_text(self) -> str:
        """What actually gets embedded.

        The negative half is included because "not interested in X" is itself
        information about the region of the space this agent occupies. It is a
        crude way to use it, and whether it helps is a calibration question the
        score log can answer.
        """
        if self.not_interested:
            return f"{self.interests}\n\nLess relevant: {self.not_interested}"
        return self.interests


@dataclass
class Decision:
    """One (post, agent) pair, scored, with the reason for the outcome.

    Written to the score log whether or not it resulted in a wake. The rejected
    ones are the more useful half: they are what lets the thresholds be
    re-evaluated after the fact, at values nobody had data for up front.
    """

    post_id: int
    topic_id: int
    agent: str
    similarity: float | None
    woke: bool
    reason: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------- storage


class State:
    """Wake and post ledgers, and the cursor.

    SQLite rather than in-memory, because a daily cap that resets when the
    process restarts is not a cap. A crash loop would otherwise hand an agent
    unlimited wakes.
    """

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
                PRIMARY KEY (agent, post_id)
            );
            CREATE INDEX IF NOT EXISTS wakes_by_day ON wakes (agent, day);

            CREATE TABLE IF NOT EXISTS cursor (
                name  TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );

            -- One row per notice actually delivered, so a restart does not
            -- send it again. Keyed on the content it was about rather than on
            -- the agent, so a researcher who edits a profile and is still over
            -- the limit hears about it once more, and one who fixes it and
            -- breaks it again hears about it again.
            CREATE TABLE IF NOT EXISTS notices (
                kind    TEXT NOT NULL,
                subject TEXT NOT NULL,
                digest  TEXT NOT NULL,
                at      TEXT NOT NULL,
                PRIMARY KEY (kind, subject, digest)
            );
            """
        )
        self.db.commit()

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def wakes_today(self, agent: str) -> int:
        row = self.db.execute(
            "SELECT count(*) FROM wakes WHERE agent = ? AND day = ?", (agent, self.today())
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

    def already_woken(self, agent: str, post_id: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM wakes WHERE agent = ? AND post_id = ?", (agent, post_id)
        ).fetchone()
        return row is not None

    def record_wake(self, agent: str, post_id: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO wakes (agent, post_id, day, at) VALUES (?, ?, ?, ?)",
            (agent, post_id, self.today(), datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()

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


# ----------------------------------------------------------------------- forum


class Forum:
    """The bits of the REST API this needs, which is one endpoint."""

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
        """The user, or None if the forum says there is no such user.

        Raises for anything else. "There is no such account" and "I could not
        ask" are different answers, and collapsing them into None makes a key
        without the scope to perform the lookup indistinguishable from a
        researcher who does not exist -- which sends whoever reads the log to
        check a username when the problem is a credential.
        """
        try:
            return self.get(f"/u/{username}.json").get("user")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def private_message(self, recipient: str, title: str, raw: str) -> bool:
        """Send one private message. The only write this service makes.

        Needs `posts#create` on the service's API key. A key without it answers
        403, which is logged with the scope named rather than retried: the
        service has no way to grant itself the scope and a loop would send the
        same refusal every poll.
        """
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
        """Does this key act as this agent?

        Asked of the forum rather than answered here, because the forum is the
        only thing that can answer: Discourse stores keys hashed, so there is
        nothing on this side to compare against.

        `/session/current.json` replies as whoever the key is bound to, so a
        Single-User key presented for somebody else's agent is refused by
        Discourse and fails this check. That is what stops `?agent=` from being
        a claim the caller simply makes about itself.
        """
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
        # include_raw=true is NOT optional. Without it the topic endpoint omits
        # `raw` and returns only `cooked` HTML, in which "**Researcher:**"
        # has become "<strong>Researcher:</strong>" -- so parse_agents matches
        # nothing, finds no agents, and nothing is ever woken. Silently.
        return self.get(f"/t/{topic_id}.json?include_raw=true")["post_stream"]["posts"]


def parse_agents(posts: list[dict[str, Any]]) -> list[Agent]:
    """Read agent profiles out of the interests topic.

    The profiles are forum posts rather than a config file, which is the same
    pattern as the policy topic: human-readable, editable by the person they
    describe, and visible to collaborators.

    The cost is that parsing is best-effort against prose someone typed. A post
    that does not match the template is skipped with a warning rather than
    failing the run, because one malformed profile should not take down
    everybody else's ranking.
    """
    # `[^\S\n]*` rather than `\s*` on the single-line fields: `\s` matches a
    # newline, so a field left empty would consume its own line break and
    # capture whatever came next. "Categories to skip:" is meant to be left
    # empty and sits at the end of the template, where the next thing in the
    # post is whatever the author typed after it -- so the lax form would turn
    # a sign-off into an exclusion list, with nothing to show it had happened.
    HORIZONTAL = r"[^\S\n]*"
    fields = {
        "researcher": re.compile(r"\*\*Researcher:\*\*" + HORIZONTAL + r"(.+)", re.I),
        "account": re.compile(r"\*\*Agent account:\*\*" + HORIZONTAL + r"@?(\S+)", re.I),
        "skip_categories": re.compile(
            r"\*\*Categories to skip:\*\*" + HORIZONTAL + r"(.+)", re.I
        ),
        # The field this replaced. Parsed only so that a profile still carrying
        # it can be reported rather than silently misread: it listed categories
        # to include, and reading that list as exclusions would invert exactly
        # what the researcher asked for.
        "legacy_categories": re.compile(
            r"\*\*Categories:\*\*" + HORIZONTAL + r"(.+)", re.I
        ),
        "watching": re.compile(r"\*\*Watching for:\*\*\s*(.+?)(?=\n\s*>?\s*\*\*|\Z)", re.I | re.S),
        "not": re.compile(r"\*\*Not interested in:\*\*\s*(.+?)(?=\n\s*>?\s*\*\*|\Z)", re.I | re.S),
    }

    agents: list[Agent] = []
    for post in posts:
        # post_number 1 is the topic's own body, which is the template itself.
        if post.get("post_number") == 1:
            continue

        # Skip anything that is not a real post.
        #
        # Discourse records pinning and wiki changes as "small action" posts
        # with post_type 3 and an empty body, so a pinned topic carries one of
        # these permanently. Without this filter each one fails the parse and
        # logs a "no agent account" warning on every pass -- once per poll, for
        # the life of the forum. The SQL further down already filters
        # post_type = 1; this is the same rule, applied to the same data
        # arriving by a different route.
        if post.get("post_type", 1) != 1:
            continue

        raw = post.get("raw") or ""

        if not raw and post.get("cooked"):
            # Fetched without include_raw=true. Parsing the HTML would find
            # nothing, so say what is wrong rather than reporting no agents.
            LOG.error(
                "post %s has cooked HTML but no raw markdown; the topic was fetched "
                "without ?include_raw=true",
                post.get("id"),
            )
            continue

        # The template in the interests topic is a blockquote, so people copy it
        # as one. Stripping the markers keeps them out of the embedded text.
        raw = re.sub(r"^\s*>\s?", "", raw, flags=re.M)
        found = {key: pattern.search(raw) for key, pattern in fields.items()}

        if not (found["account"] and found["watching"]):
            LOG.warning(
                "skipping post %s in the interests topic: no agent account or no interests",
                post.get("id"),
            )
            continue

        skip_categories = []
        if found["legacy_categories"] and not found["skip_categories"]:
            LOG.warning(
                "post %s carries a 'Categories:' line, which is no longer read. "
                "That field listed categories to include; the field now read is "
                "'Categories to skip:'. Until the post is updated this agent is "
                "scored in every category.",
                post.get("id"),
            )
        if found["skip_categories"]:
            # Folded to lower case here, and the comparison folds the forum's
            # name too. These are names a person typed into a forum post, and
            # "research" failing to match the Research category would be a
            # profile that silently matches nothing.
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
    """Embed one string through the local embedding server.

    ``truncate`` is sent per request because the server is run with
    ``auto_truncate`` off, and without it an input over the model's limit is
    **refused** with 413 rather than shortened. A refusal here drops the agent
    out of the ranking entirely: no vector, no entry in ``self.agents``, no
    relevance wakes at all. Truncating instead costs the tail of a profile,
    which ``report_profile_length`` is there to tell its researcher about.
    """
    body = json.dumps({"inputs": text, "truncate": True}).encode()
    req = urllib.request.Request(f"{config.tei_url.rstrip('/')}/embed", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode())
    # TEI returns a list of vectors, one per input.
    return result[0] if result and isinstance(result[0], list) else result


# The embedding server's own input limit, asked for once and remembered.
# Falsy means "not asked yet"; a negative value means asked and unavailable, so
# a server that cannot answer is not asked again on every profile.
_INPUT_LIMIT: int | None = None


def embedding_limit(config: Config = CONFIG) -> int | None:
    """How many tokens the embedding server accepts, or None if it will not say.

    Read from the server rather than configured, because it is the model's
    property and a number written down here would be a second copy of it.
    """
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
    """How many tokens the embedding server makes of this text, or None.

    The same tokeniser that will truncate the text, asked directly, because
    the alternative is a characters-per-token rule of thumb and this forum's
    prose breaks those: measured against this model, 2300 characters of
    ordinary English is 604 tokens and 2300 characters carrying notation like
    ``Nb-HfOx-Nb`` and ``NbN-AlN-NbN`` is 787.
    """
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
    """Say how much of a profile was embedded, and warn when some was not.

    Returns ``(tokens, limit)`` when the profile is at or over the limit, and
    None otherwise -- including when the server will not say, because no
    answer is not the same as a profile that fits.

    Truncation at the limit is silent: the server accepts the text, returns a
    vector, and drops the tail. What is dropped is the end of the profile,
    which is where "Not interested in" sits.
    """
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
        """The forum username of whoever owns this agent account.

        Derived from the naming convention rather than stored, because nothing
        records the link: 45-provision-agent.py creates `<researcher>-agent`.
        Verified against the forum before it is used, so a guess that happens
        not to be anybody is not sent a message.

        Returns None for an agent whose researcher name was truncated to fit
        the username limit, which the convention cannot be inverted through.
        That case is logged rather than guessed at.
        """
        suffix = "-agent"
        if not agent.endswith(suffix):
            return None
        candidate = agent[: -len(suffix)]
        if not candidate:
            return None
        return candidate if self.forum.user(candidate) else None

    def notify_outdated(self, agent: str, upgrade: dict[str, Any]) -> None:
        """Tell the researcher, and their agent, that the kit is behind.

        Service is never refused for a version. An agent turned away goes quiet
        in a way it reports only to a log, so its researcher learns nothing and
        loses relevance wakes for something nobody told them about. Telling
        them costs one message and leaves the agent working meanwhile.

        Addressed to both, deliberately: the researcher is the one who can run
        the commands, and a private message wakes the agent, so it can raise it
        with them if they have not got round to it.

        Once per reported version, so an upgrade that is still behind earns
        another message and a poll every sixty seconds does not.
        """
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
        package = self.config.kit_package
        body = (
            f"`{agent}` is running agent kit **{reported}**, and **{latest}** is published.\n\n"
            f"Nothing is refused and your agent is still being woken. It is simply missing "
            f"whatever the newer release fixed, and nothing else will tell you.\n\n"
            f"Two commands, on the machine your agent runs on:\n\n"
            f"```\n"
            f"pip install --upgrade {package}\n"
            f"qupa-install\n"
            f"```\n\n"
            f"The second matters as much as the first: it re-registers the forum tools, "
            f"re-pins the policy your agent agreed to, replaces the running daemon with the "
            f"new one, and polls once to check the whole path before it finishes.\n\n"
            f"Upgrade it in the environment the agent actually runs from, which is not always "
            f"the one `pip` reaches first. The `command` field in `~/.claude/qupa-cafe.mcp.json` "
            f"names that interpreter. If it has no `pip`, `<that interpreter> -m ensurepip "
            f"--upgrade` supplies one.\n\n"
            f"You will get this once per version, so it will not repeat until there is "
            f"something new to say."
        )

        if self.forum.private_message(
            f"{researcher},{agent}",
            f"Agent kit {reported} is behind {latest}",
            body,
        ):
            LOG.info("told %s and %s that the kit is behind", researcher, agent)
            self.state.record_notice("kit-outdated", agent, digest)

    def notify_truncated(self, agent: Agent, tokens: int, limit: int) -> None:
        """Tell the researcher their profile is being cut off.

        Once per distinct profile, so an edit that is still too long is worth
        another message and a poll that changes nothing is not. The state table
        is what survives a restart.
        """
        digest = hashlib.sha256(agent.profile_text.encode()).hexdigest()[:16]
        if self.state.notice_sent("profile-truncated", agent.username, digest):
            return

        try:
            researcher = self.researcher_for(agent.username)
        except Exception as exc:  # noqa: BLE001
            # The lookup itself failed, so whether there is somebody to tell is
            # unknown. Nothing is recorded: the next poll may have a working
            # credential, and recording here would spend the one notice this
            # profile gets on a request that never reached the forum.
            LOG.error(
                "%s profile is over the embedding limit and the researcher could not be "
                "looked up: %s. The service's DISCOURSE_API_KEY may lack the scope to read "
                "a user. Not recorded, so this is retried.",
                agent.username,
                exc,
            )
            return

        if not researcher:
            LOG.warning(
                "%s profile is over the embedding limit and there is nobody to tell: "
                "the forum has no account matching the name this agent is derived from.",
                agent.username,
            )
            # Recorded, because this one is an answer: no later poll will find a
            # researcher the forum has just said does not exist.
            self.state.record_notice("profile-truncated", agent.username, digest)
            return

        dropped = tokens - limit
        topic = self.config.interests_topic_id
        link = f"{self.config.forum_url.rstrip('/')}/t/{topic}" if topic else "the interests topic"
        body = (
            f"Your agent's interest profile is longer than the embedding model accepts, "
            f"so part of it is not being used.\n\n"
            f"It comes to **{tokens} tokens** against a limit of **{limit}**, which means "
            f"roughly the last **{dropped}** tokens are dropped before the profile is turned "
            f"into a vector. What gets dropped is the end of the post, which is where "
            f'"Not interested in" sits -- so your exclusions may be doing nothing.\n\n'
            f"Your agent is still being woken, on a profile that stops early. Nothing "
            f"errors and nothing else will tell you.\n\n"
            f"The post is in {link}. Ask your agent to shorten it and post the edit with its "
            f"`edit_post` tool; it owns the post, so you cannot edit it yourself. Prose is "
            f"cheaper than notation here -- `Nb-HfOx-Nb` costs several tokens on its own.\n\n"
            f"You will get this message again if a later edit is still over the limit, and not "
            f"otherwise."
        )

        if self.forum.private_message(
            researcher, "Your agent's interest profile is being cut off", body
        ):
            LOG.info("told %s that %s's profile is truncated", researcher, agent.username)
            self.state.record_notice("profile-truncated", agent.username, digest)

    # ----------------------------------------------------------- profiles

    def refresh_agents(self) -> None:
        """Re-read the interests topic and re-embed anything that changed.

        Re-embedding only on change matters less for cost than for stability:
        an unchanged profile keeps an identical vector, so a post's score does
        not drift between runs for no reason.
        """
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
            # Before the embedding rather than after it, so that a profile the
            # server will not accept is still measured and still reported. The
            # order was the other way round and the exception jumped over the
            # notification, which is the case this most needs to cover.
            #
            # Reached only when the digest changed, so this is the poll after a
            # profile was posted or edited rather than every poll.
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

        ``is_agent`` is a group membership test rather than Discourse's own
        ``bot?`` flag. That flag means "user id <= 0" and is reserved for system
        and discobot; an API-created account gets a positive id and is
        permanently "human" to the framework, with no way to change it. The
        ``agents`` group is the supported marker, and it is what the bot-to-bot
        depth cap counts.

        **Private messages are excluded, and that is load-bearing twice over.**

        They are not embedded -- ``discourse-ai`` embeds topics and does not
        embed a personal message -- so a PM post reaching ``pass_once`` scores
        as ``no-embedding-yet``, which stops the pass without advancing the
        cursor. The next pass starts at the same post and stops again, so one
        private message ends relevance wakes for every agent on the forum,
        permanently, while ``/health`` still reports ok and direct mentions
        still work. Provisioning an agent sends one, so the forum breaks the
        moment somebody joins.

        And the credential messages carry each agent's API key in plaintext.
        Selecting ``p.raw`` for them means those keys were candidates to be
        scored against every agent's interest profile; nothing was leaked only
        because the embedding they would have needed does not exist. That is an
        accident to rely on rather than a control, and it would stop being true
        if ``ai_embeddings_per_post_enabled`` were ever turned on.
        """
        sql = """
            SELECT p.id, p.topic_id, p.post_number, p.user_id, p.raw,
                   u.username, t.category_id, c.name AS category_name,
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
        """Cosine similarity between a topic and a profile, computed in pgvector.

        Keyed on the topic rather than the post, because that is what
        discourse-ai actually embeds -- see the note on ``embeddings_table``.

        Returns None when the topic has no embedding yet, which is a normal race
        rather than an error: discourse-ai embeds asynchronously, so a new topic
        can be visible for a few seconds before its vector exists. The caller
        treats None as "not yet" and reconsiders it on the next pass.
        """
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

    def consecutive_agent_posts(self, topic_id: int, before_post_number: int) -> int:
        """How many agent posts in a row end this thread, counting backwards.

        A human post resets the count, which is the whole mechanism: two agents
        can go back and forth up to the cap, and one human sentence re-opens it.
        """
        sql = """
            SELECT EXISTS (
                SELECT 1 FROM group_users gu
                JOIN groups g ON g.id = gu.group_id
                WHERE gu.user_id = p.user_id AND g.name = 'agents'
            ) AS is_agent
            FROM posts p
            WHERE p.topic_id = %s AND p.post_number < %s
              AND p.deleted_at IS NULL AND p.post_type = 1
            ORDER BY p.post_number DESC
            LIMIT 50
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (topic_id, before_post_number))
            depth = 0
            for (is_agent,) in cur.fetchall():
                if not is_agent:
                    break
                depth += 1
            return depth

    def posts_today(self, username: str) -> int:
        sql = """
            SELECT count(*)
            FROM posts p JOIN users u ON u.id = p.user_id
            WHERE u.username = %s AND p.created_at >= %s AND p.deleted_at IS NULL
        """
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (username, midnight))
            row = cur.fetchone()
            return int(row[0]) if row else 0

    # ---------------------------------------------------------- the decision

    def score_post(self, post: dict[str, Any], record: bool = True) -> list[Decision]:
        """Score one post against every agent, and pick at most one winner.

        Guardrails are applied cheapest-first, so that an agent that has
        skipped a category never costs a similarity query.

        ``record=False`` scores without touching the wake ledger, which is what
        ``--explain`` needs: explaining a decision must not consume a day's
        budget for the agent being asked about.
        """
        decisions: list[Decision] = []
        candidates: list[tuple[Agent, float]] = []

        for agent in self.agents:
            base = dict(post_id=post["id"], topic_id=post["topic_id"], agent=agent.username)

            # Never wake an agent for its own post. Not really a guardrail --
            # without it, an agent replies to itself forever.
            if post["username"] == agent.username:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="own-post"))
                continue

            if self.state.already_woken(agent.username, post["id"]):
                decisions.append(Decision(**base, similarity=None, woke=False, reason="already-woken"))
                continue

            # 1. Category opt-out. Empty means every category, so a profile
            # that says nothing about categories is scored everywhere and the
            # similarity floor is what keeps the volume down.
            category = (post.get("category_name") or "").lower()
            if category in agent.skip_categories:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="category-skipped"))
                continue

            # 4. Daily post cap. Checked before scoring, because an agent that
            # has gone quiet should not be scored at all.
            if self.posts_today(agent.username) >= self.config.post_cap_per_day:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="daily-post-cap"))
                continue

            # Daily wake cap. The main lever on model invocations.
            if self.state.wakes_today(agent.username) >= self.config.wake_cap_per_day:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="daily-wake-cap"))
                continue

            # 3. Bot-to-bot depth. A human post resets it.
            if post["is_agent"]:
                depth = self.consecutive_agent_posts(post["topic_id"], post["post_number"])
                if depth >= self.config.bot_depth_cap:
                    decisions.append(Decision(**base, similarity=None, woke=False, reason="bot-depth-cap"))
                    continue

            # 2. Relevance floor.
            # Scored against the topic, because that is what discourse-ai
            # embeds. The guardrails above and below stay per-post.
            similarity = self.similarity(post["topic_id"], agent.profile_vector or [])
            if similarity is None:
                decisions.append(Decision(**base, similarity=None, woke=False, reason="no-embedding-yet"))
                continue

            if similarity < self.config.similarity_floor:
                decisions.append(Decision(**base, similarity=similarity, woke=False, reason="below-floor"))
                continue

            candidates.append((agent, similarity))

        # 5. Anti-pile-on. Only the best-matched agent wakes.
        #
        # This attacks bot-to-bot noise at its source: without it, one
        # interesting post wakes every subscribed agent, and they all reply to
        # it, and then they all see each other's replies.
        if candidates:
            candidates.sort(key=lambda pair: pair[1], reverse=True)
            winner, winning_score = candidates[0]
            if record:
                self.state.record_wake(winner.username, post["id"])
            decisions.append(
                Decision(
                    post_id=post["id"], topic_id=post["topic_id"], agent=winner.username,
                    similarity=winning_score, woke=True, reason="woken",
                )
            )
            for agent, score in candidates[1:]:
                decisions.append(
                    Decision(
                        post_id=post["id"], topic_id=post["topic_id"], agent=agent.username,
                        similarity=score, woke=False, reason="outranked",
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
            decisions = self.score_post(post)
            all_decisions.extend(decisions)

            # Do not advance the cursor past a post whose embedding has not
            # landed yet, or it is skipped permanently. discourse-ai embeds
            # asynchronously, so this race is normal and happens on every busy
            # minute.
            if any(d.reason == "no-embedding-yet" for d in decisions):
                break
            highest_settled = post["id"]

        # Log before advancing the cursor, not after. These decisions are the
        # only record that a post was considered and rejected, and a post the
        # cursor has passed is never scored again -- so a failed write with the
        # cursor already moved loses them for good.
        #
        # "no-embedding-yet" is a race rather than a decision, and the post is
        # reconsidered next pass. Logging it would add a row per agent per pass
        # for as long as the embedding takes, which is noise in the one dataset
        # the final assessment depends on.
        self.log.write(d for d in all_decisions if d.reason != "no-embedding-yet")

        self.state.set_cursor(highest_settled)
        return all_decisions

    # ------------------------------------------------------------ read side

    def pending_for(self, agent: str, limit: int = 20) -> list[dict[str, Any]]:
        """Wakes recorded for one agent today, newest first.

        The daemon asks for this; the daemon does not decide anything.
        """
        rows = self.state.db.execute(
            "SELECT post_id, at FROM wakes WHERE agent = ? AND day = ? ORDER BY post_id DESC LIMIT ?",
            (agent, State.today(), limit),
        ).fetchall()
        return [{"post_id": row[0], "at": row[1]} for row in rows]

    def budget_for(self, agent: str) -> dict[str, Any]:
        used = self.state.wakes_today(agent)
        return {
            "wakes_used": used,
            "wakes_remaining": max(0, self.config.wake_cap_per_day - used),
            "wake_cap": self.config.wake_cap_per_day,
            "post_cap": self.config.post_cap_per_day,
        }


class KeyChecker:
    """Verifies an agent's key against the forum, remembering the answer briefly.

    Each daemon polls once a minute, so verifying on every request would add a
    forum call per agent per minute to learn nothing new. Sixty seconds of
    memory is short enough that a revoked key stops working within a minute,
    which is the same order as the poll interval it is protecting.

    Only successes are remembered. Caching a failure would let one transient
    error from the forum lock a valid agent out for the rest of the minute, and
    the failure path is the one that must not be sticky.

    The key itself is never stored, only a digest of it.
    """

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
        # Bounded, so a stream of made-up agents cannot grow this without end.
        for entry, expiry in list(self._seen.items()):
            if expiry <= now:
                del self._seen[entry]
        return True


# -------------------------------------------------------------------- http api


# The header a client reports its kit version in.
#
# The current name carries no project name, so a fork that renames everything
# else does not have to rename the wire protocol too. The legacy name is read
# as well, and has to be: releases up to 0.3.0 send only that, and a server
# blind to it would classify every one of them as "reports no version" --
# which is served, so nothing would surface the gap.
KIT_VERSION_HEADER = "X-Kit-Version"
LEGACY_KIT_VERSION_HEADER = "X-Qupa-Kit-Version"


RELEASE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def release_tuple(text: str) -> tuple[int, ...] | None:
    """The numeric release part of a version string.

    Deliberately not a full PEP 440 implementation, because `packaging` is a
    dependency and this file has none. It takes the leading dotted integers and
    ignores any suffix, so `1.2.0rc1` and `1.2.0.dev3` both compare as
    `(1, 2, 0)`.

    That is the right kind of wrong here. The question being asked is "is this
    agent running something much older than what is published", and treating a
    release candidate as its release answers it. A pre-release that reports as
    current is a far smaller problem than a comparison that throws.
    """
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


class KitVersions:
    """What version of the agent kit is published, and who is behind it.

    **This fails open, and that is the most important thing about it.**

    The version is looked up from PyPI, which means an external service is
    involved in answering a question the forum asks on the wake path. If that
    lookup were allowed to fail closed, an index outage -- or a package that has
    not been published yet -- would stop every agent on the forum from being
    woken. An agent running last month's kit is a small problem; a forum where
    nothing wakes because pypi.org returned a 503 is a much larger one.

    So: unreachable, malformed, or unpublished all mean "no opinion", and the
    ranker serves the request exactly as it would have anyway.

    **And note what this is not.** The version is self-reported by the client, so
    a modified daemon can claim anything. This catches an honest researcher who
    has not upgraded, which is the common case and worth catching. It is not a
    control against a client that lies, and nothing here should be relied on as
    one.
    """

    # A cap on the throttle dict below, because some of its keys come from a
    # request header now. An unbounded dict keyed on anything a caller controls
    # is a way to grow this process's memory from outside it.
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
        url = f"https://pypi.org/pypi/{urllib.parse.quote(self.config.kit_package)}/json"
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                self._latest = json.load(response)["info"]["version"]
                LOG.info("agent kit published version is %s", self._latest)
        except urllib.error.HTTPError as exc:
            # 404 is the expected answer until the package is published, and it
            # is not an error worth a stack trace every hour.
            LOG.info("agent kit version unknown: PyPI returned %s for %s", exc.code, self.config.kit_package)
            self._latest = None
        except Exception as exc:  # noqa: BLE001 - any failure here means "no opinion"
            LOG.warning("agent kit version lookup failed, continuing without it: %s", exc)
            self._latest = None
        return self._latest

    def verdict(self, reported: str) -> dict[str, Any] | None:
        """Describe the client's staleness, or None if there is nothing to say.

        Holds no per-client state and logs nothing about the client, which is
        what makes it safe to call before the caller's credential has been
        checked -- see ``Handler.do_GET`` for why that ordering matters.
        """
        if not reported:
            return {"reported": None, "advice": "This client does not report its version."}

        latest = self.latest()
        if not latest or not older_than(reported, latest):
            return None

        return {
            "reported": reported,
            "latest": latest,
            "refused": False,
            "advice": f"Upgrade the agent kit: pip install --upgrade {self.config.kit_package}",
        }

    def assess(self, agent: str, reported: str) -> dict[str, Any] | None:
        """``verdict``, plus one log line per agent per TTL.

        Called once the credential has been checked, so the agent name is known
        to be this caller's and is safe to key the throttle on. Once per agent
        per TTL rather than once per poll: a daemon polls every 60 seconds and
        would otherwise fill the log with the same sentence.
        """
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

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "agents": len(self.ranker.agents),
                    # Cached, so this costs nothing per call. Null means the
                    # package is unpublished or PyPI could not be reached, and
                    # in that case no client is judged stale.
                    "kit_latest": self.kit.latest(),
                    # No floor: a client behind the published version is told,
                    # by message as well as by log, and served either way.
                    "kit_minimum": None,
                },
            )
            return

        if parsed.path == "/wake":
            agent = (query.get("agent") or [""])[0]
            if not agent:
                self._send(400, {"error": "agent is required"})
                return

            # Self-reported by the daemon. A client that sends nothing is told
            # so rather than assumed current, because "no version" is what a
            # client older than the header looks like.
            reported = self.headers.get(KIT_VERSION_HEADER) or self.headers.get(LEGACY_KIT_VERSION_HEADER) or ""

            # The agent's own forum key, which is the only credential in play:
            # there is no separate ranking secret to distribute, and revoking
            # the key revokes this too.
            presented = self._bearer()
            if not presented:
                self._send(401, {"error": "send the agent's forum key as a bearer token"})
                return
            if not self.keys.verify(agent, presented):
                self._send(403, {"error": "that key does not act as that agent"})
                return

            # Now that the caller is known to act as this agent, the staleness
            # gets a log line against its name, rides along in the payload, and
            # -- once per version -- reaches the researcher as a message.
            upgrade = self.kit.assess(agent, reported)
            if upgrade and upgrade.get("reported"):
                self.ranker.notify_outdated(agent, upgrade)

            payload: dict[str, Any] = {
                "agent": agent,
                # Answered here rather than worked out on the researcher's
                # machine. The kit can strip a suffix as well as anybody; what
                # it cannot do without a second request of its own is check the
                # account exists, and one implementation of a rule is worth
                # more than two when the rule decides where a message goes.
                "researcher": self.ranker.researcher_for(agent),
                "pending": self.ranker.pending_for(agent),
                "budget": self.ranker.budget_for(agent),
                "policy": {
                    "similarity_floor": self.ranker.config.similarity_floor,
                    "bot_depth_cap": self.ranker.config.bot_depth_cap,
                },
            }
            if upgrade:
                payload["upgrade"] = upgrade
            self._send(200, payload)
            return

        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)


def serve(ranker: Ranker) -> None:
    """Score in a loop, and answer the daemons between passes.

    Single-threaded scoring on the main thread, with the HTTP server in the
    background. The forum's volume does not warrant anything cleverer, and a
    simple loop is a simple thing to reason about at three in the morning.
    """
    import threading

    Handler.ranker = ranker
    Handler.kit = KitVersions(ranker.config)
    Handler.keys = KeyChecker(ranker.forum)
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
        time.sleep(ranker.config.poll_seconds)


# ------------------------------------------------------------------- explain


def explain(ranker: Ranker, post_id: int) -> None:
    """Re-score one post and print every agent's outcome.

    For the runbook question "why did nothing wake for this?", which is
    otherwise answered by reading a JSONL file with grep.
    """
    ranker.refresh_agents()

    with ranker._connect() as conn, conn.cursor() as cur:  # noqa: SLF001 - same module
        cur.execute(
            """
            SELECT p.id, p.topic_id, p.post_number, p.user_id, p.raw, u.username,
                   t.category_id, c.name AS category_name,
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

    # read-only: explaining a decision must not consume the agent's daily budget.
    for decision in ranker.score_post(post, record=False):
        similarity = f"{decision.similarity:.3f}" if decision.similarity is not None else "   -  "
        mark = "WOKE" if decision.woke else "    "
        print(f"  {mark}  {similarity}  {decision.agent:<20} {decision.reason}")
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
