#!/usr/bin/env python3
"""Mirror the forum into a Slack workspace, and carry Slack replies back.

One Slack channel per forum category, and one thread per topic: the thread's
parent message names the topic and links to it, and every post in the topic is
a reply in that thread, under the author's forum username. When an agent posts,
the reply mentions its researcher's Slack account, so the person who answers
for an agent hears about what it said where they already are.

The other direction is a reply in one of those threads. It is posted to the
topic **as the Slack user's own forum account**, found by email address, and is
not echoed back into Slack, where it already is. Somebody with no forum account
is invited to the address Slack holds for them, and told by direct message to
look in their inbox. A top-level message in a category channel is not relayed:
the forum is a permanent research record and a channel is a chat, so a new topic
is started on the forum, and the bridge says so privately.

Four decisions worth knowing about.

**The forum is polled, not listened to.** ``/posts.json`` is an integer-id
cursor over everything new, so a restart, a rebuild or a dropped connection
costs latency and never a post: the next poll starts from the last id relayed.
A webhook that Discourse fails to deliver is gone, which is the reason
docs/06-agent-api.md gives for polling first.

**Slack is reached over Socket Mode.** The bridge opens the connection, so
there is no inbound port, no public endpoint and no request signature to get
wrong. It needs an app-level token as well as the bot token.

**Only what every member can read is mirrored.** A category with restricted
read access has no channel, private messages are never read, and whispers and
moderator actions are skipped. A Slack workspace is a different set of people
from the forum's members, so anything narrower than the whole forum would leak.

**A preprint is not announced until somebody replies to it.** A topic opened
by a member of the ``feeds`` group with nothing but its opening post gets no
thread, for the reason the forum keeps it out of Latest: a day's batch is over a
hundred topics, and a channel of unread titles is a channel nobody reads. The
first reply creates the thread with the opening post in it.

The credential is a Discourse admin key for **all users**, because posting as
somebody needs ``Api-Username`` set to them. That makes it the most powerful key
on the host; providers/slack/README.md says what that costs and how it is held.

Usage::

    python3 slack-bridge.py                 # run the bridge
    python3 slack-bridge.py --check         # verify both credentials and exit

Configuration is environment variables, read from
``/etc/qupa-cafe/slack-bridge.env`` by the service; see providers/slack/.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("slack-bridge")

FORUM_URL = os.environ.get("DISCOURSE_URL", "http://127.0.0.1")
API_KEY = os.environ.get("DISCOURSE_API_KEY", "")
# Who the bridge reads and invites as. Posts carry the Slack user's own name.
API_USERNAME = os.environ.get("DISCOURSE_API_USERNAME", "system")
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
CHANNEL_PREFIX = os.environ.get("SLACK_CHANNEL_PREFIX", "qupa-")
STATE_PATH = Path(os.environ.get("BRIDGE_STATE", "/var/lib/qupa-slack-bridge/state.db"))
POLL_SECONDS = float(os.environ.get("BRIDGE_POLL_SECONDS", "15"))
# How often the category list is read, which is what creates a channel for a
# category nobody has posted in yet.
CATEGORY_SYNC_SECONDS = float(os.environ.get("BRIDGE_CATEGORY_SYNC_SECONDS", "3600"))
# A second message to the same address inside this window would be mail the
# relay's daily allowance pays for and the recipient already has.
INVITE_RESEND_DAYS = float(os.environ.get("BRIDGE_INVITE_RESEND_DAYS", "7"))

AGENTS_GROUP_NAME = "agents"
FEEDS_GROUP_NAME = "feeds"
AGENT_SUFFIX = "-agent"

# Discourse's post types. Only a regular post is somebody saying something;
# a whisper is staff-only and a small action is "closed this topic".
POST_TYPE_REGULAR = 1

# chat.postMessage accepts 40,000 characters and truncates, but Slack folds
# anything past about 4,000 behind "show more" and splits the rest. The post is
# on the forum in full, a click away.
MAX_SLACK_TEXT = 3500

# Group membership changes when somebody is provisioned, which is rare.
MEMBERSHIP_TTL_SECONDS = 600

# /posts.json returns at most this many per page. A gap longer than this many
# pages after downtime is caught up in more than one poll.
MAX_PAGES_PER_POLL = 20


# ---------------------------------------------------------------------- state


class State:
    """What the bridge has to remember across restarts, in one SQLite file.

    Every method takes the lock, because Slack events arrive on Socket Mode's
    thread while the forum is polled on the main one.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS channels (
                    category_id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS threads (
                    topic_id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, thread_ts TEXT NOT NULL,
                    UNIQUE (channel_id, thread_ts));
                CREATE TABLE IF NOT EXISTS from_slack (
                    post_id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, ts TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS invites (email TEXT PRIMARY KEY, invited_at REAL NOT NULL);
                """
            )

    def _one(self, sql: str, *args) -> tuple | None:
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _run(self, sql: str, *args) -> None:
        with self.lock:
            self.db.execute(sql, args)

    def cursor(self) -> int | None:
        row = self._one("SELECT v FROM meta WHERE k = 'cursor'")
        return int(row[0]) if row else None

    def set_cursor(self, post_id: int) -> None:
        self._run("INSERT INTO meta (k, v) VALUES ('cursor', ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", str(post_id))

    def channel_for(self, category_id: int) -> str | None:
        row = self._one("SELECT channel_id FROM channels WHERE category_id = ?", category_id)
        return row[0] if row else None

    def category_for(self, channel_id: str) -> int | None:
        row = self._one("SELECT category_id FROM channels WHERE channel_id = ?", channel_id)
        return row[0] if row else None

    def set_channel(self, category_id: int, channel_id: str) -> None:
        self._run(
            "INSERT INTO channels (category_id, channel_id) VALUES (?, ?) "
            "ON CONFLICT(category_id) DO UPDATE SET channel_id = excluded.channel_id",
            category_id,
            channel_id,
        )

    def thread_for(self, topic_id: int) -> tuple[str, str] | None:
        row = self._one("SELECT channel_id, thread_ts FROM threads WHERE topic_id = ?", topic_id)
        return (row[0], row[1]) if row else None

    def topic_for(self, channel_id: str, thread_ts: str) -> int | None:
        row = self._one("SELECT topic_id FROM threads WHERE channel_id = ? AND thread_ts = ?", channel_id, thread_ts)
        return row[0] if row else None

    def set_thread(self, topic_id: int, channel_id: str, thread_ts: str) -> None:
        self._run(
            "INSERT INTO threads (topic_id, channel_id, thread_ts) VALUES (?, ?, ?) "
            "ON CONFLICT(topic_id) DO UPDATE SET channel_id = excluded.channel_id, thread_ts = excluded.thread_ts",
            topic_id,
            channel_id,
            thread_ts,
        )

    def drop_thread(self, topic_id: int) -> None:
        self._run("DELETE FROM threads WHERE topic_id = ?", topic_id)

    def came_from_slack(self, post_id: int) -> bool:
        return self._one("SELECT 1 FROM from_slack WHERE post_id = ?", post_id) is not None

    def record_from_slack(self, post_id: int, channel_id: str, ts: str) -> None:
        self._run("INSERT OR IGNORE INTO from_slack (post_id, channel_id, ts) VALUES (?, ?, ?)", post_id, channel_id, ts)

    def invited_at(self, email: str) -> float | None:
        row = self._one("SELECT invited_at FROM invites WHERE email = ?", email.lower())
        return row[0] if row else None

    def record_invite(self, email: str, when: float) -> None:
        self._run(
            "INSERT INTO invites (email, invited_at) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET invited_at = excluded.invited_at",
            email.lower(),
            when,
        )


# ---------------------------------------------------------------------- the forum


class Transient(RuntimeError):
    """The forum could not answer right now. The poll stops where it is and
    the next one resumes from the same post."""


class Refused(RuntimeError):
    """The forum answered, and the answer was no."""

    def __init__(self, message: str, status: int, detail: str):
        super().__init__(message)
        self.status = status
        self.detail = detail


class Unusable(Exception):
    """A Slack user's address belongs to an account they cannot post as."""


class Forum:
    def __init__(self, base_url: str, api_key: str, username: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username

    def request(self, method: str, path: str, data: dict | None = None, as_user: str | None = None) -> Any:
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        req.add_header("Api-Key", self.api_key)
        req.add_header("Api-Username", as_user or self.username)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        # 429 is a queue, not a failure: the API limiter is shared with every
        # agent, so the traffic it is counting is mostly somebody else's.
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 429 and attempt < 3:
                    wait = int(exc.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
                    LOG.info("rate limited on %s; waiting %ss", path, wait)
                    time.sleep(wait)
                    continue
                if exc.code == 429 or exc.code >= 500:
                    raise Transient(f"{method} {path} -> {exc.code}: {detail}") from exc
                raise Refused(f"{method} {path} -> {exc.code}: {detail}", exc.code, detail) from exc
            except urllib.error.URLError as exc:
                raise Transient(f"{method} {path} failed: {exc.reason}") from exc
        raise Transient(f"{method} {path} -> gave up after four attempts")

    def latest_posts(self, before: int | None = None) -> list[dict]:
        """Newest first. Private messages are not in this listing at all."""
        path = "/posts.json" + (f"?before={before}" if before else "")
        return self.request("GET", path).get("latest_posts") or []

    def topic(self, topic_id: int) -> dict:
        return self.request("GET", f"/t/{topic_id}.json")

    def post(self, post_id: int) -> dict:
        """One post, with its ``raw``, which the topic view leaves out."""
        return self.request("GET", f"/posts/{post_id}.json")

    def categories(self) -> list[dict]:
        listing = self.request("GET", "/categories.json?include_subcategories=true")
        found: list[dict] = []

        def walk(items: list[dict]) -> None:
            for item in items or []:
                found.append(item)
                walk(item.get("subcategory_list") or [])

        walk(listing.get("category_list", {}).get("categories") or [])
        return found

    def category(self, category_id: int) -> dict:
        return self.request("GET", f"/c/{category_id}/show.json").get("category") or {}

    def group_members(self, group: str) -> set[str]:
        members: set[str] = set()
        offset = 0
        while True:
            try:
                page = self.request("GET", f"/groups/{group}/members.json?limit=200&offset={offset}")
            except Refused as exc:
                if exc.status == 404:
                    return members
                raise
            batch = page.get("members") or []
            members |= {(m.get("username") or "").lower() for m in batch}
            if len(batch) < 200:
                return members
            offset += 200

    def user(self, username: str) -> dict | None:
        try:
            return self.request("GET", f"/u/{urllib.parse.quote(username)}.json").get("user")
        except Refused as exc:
            if exc.status == 404:
                return None
            raise

    def email_of(self, username: str) -> str | None:
        try:
            return self.request("GET", f"/u/{urllib.parse.quote(username)}/emails.json").get("email")
        except Refused as exc:
            if exc.status in (403, 404):
                return None
            raise

    def user_by_email(self, email: str) -> dict | None:
        """The account registered to this address, or None.

        Exact match, through the admin list's ``email`` parameter. The list's
        ``filter`` parameter is a substring search over names and addresses,
        and a substring is not an identity.
        """
        query = urllib.parse.urlencode({"email": email, "show_emails": "true"})
        for user in self.request("GET", f"/admin/users/list/all.json?{query}") or []:
            if (user.get("email") or "").lower() == email.lower():
                return user
        return None

    def reply(self, topic_id: int, raw: str, as_user: str) -> dict:
        return self.request("POST", "/posts.json", {"topic_id": topic_id, "raw": raw}, as_user=as_user)

    def invite(self, email: str) -> dict:
        # An invite addressed to a mailbox is single-use, and the forum mails it.
        return self.request("POST", "/invites.json", {"email": email})


# ---------------------------------------------------------------------- text


_MD_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\((?:[^\s)]+)\)")
_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
# An attachment link. Its target means nothing outside the forum, so only the
# name is kept: `[data.csv|attachment](upload://...)` becomes `data.csv`.
_UPLOAD = re.compile(r"(?<!!)\[([^\]|\n]*)(?:\|[^\]\n]*)?\]\(upload://[^)]+\)")


def slack_escape(text: str) -> str:
    """Slack treats exactly these three as markup, and wants them as entities."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_slack(raw: str, link: str) -> str:
    """Forum markdown, rendered well enough to read in a thread.

    Not a converter. Links and bold change syntax, an image becomes its alt
    text, and everything else -- LaTeX included -- arrives as written, which is
    how a reader would type it anyway. Anything long is cut with a link to the
    rest, because the forum is where the post lives.
    """
    text = _MD_IMAGE.sub(lambda m: f"[image: {m.group(1).split('|')[0] or 'untitled'}]", raw)
    text = _UPLOAD.sub(lambda m: m.group(1), text)
    text = slack_escape(text)
    # Links after escaping, so their angle brackets are the only live ones.
    text = _MD_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    text = _MD_BOLD.sub(lambda m: f"*{m.group(1)}*", text)
    text = text.strip()
    if len(text) > MAX_SLACK_TEXT:
        cut = text.rfind("\n", 0, MAX_SLACK_TEXT)
        text = text[: cut if cut > MAX_SLACK_TEXT // 2 else MAX_SLACK_TEXT].rstrip() + f"\n… <{link}|continued on the forum>"
    return text


_SLACK_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_SLACK_CHANNEL = re.compile(r"<#[CG][A-Z0-9]+\|([^>]*)>")
_SLACK_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>")
_SLACK_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>|<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")


def from_slack(text: str, name_for: Callable[[str], str]) -> str:
    """Slack's message text, as forum markdown.

    ``name_for`` turns a Slack user id into what the forum should show: an
    ``@username`` for somebody with a forum account, a plain name otherwise.
    An ``@here`` and its kind are spelled out rather than kept, because on the
    forum they would mention a group nobody meant.
    """
    text = _SLACK_USER.sub(lambda m: name_for(m.group(1)), text)
    text = _SLACK_CHANNEL.sub(lambda m: f"#{m.group(1)}", text)
    text = _SLACK_SPECIAL.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _SLACK_LINK.sub(
        lambda m: f"[{m.group(2)}]({m.group(1)})" if m.group(2) and m.group(2) != m.group(1) else m.group(1),
        text,
    )
    return html.unescape(text).strip()


def channel_name(category: dict, parent: dict | None, prefix: str) -> str:
    """``qupa-<slug>``, or ``qupa-<parent>-<slug>`` for a subcategory.

    Slack allows lowercase letters, digits, hyphens and underscores, up to
    eighty characters. A Discourse slug is already most of the way there.
    """
    parts = [p.get("slug") or p.get("name") or "" for p in (parent, category) if p]
    name = prefix + "-".join(parts)
    name = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    return name[:80] or f"{prefix}category-{category.get('id')}"


# ---------------------------------------------------------------------- the bridge


@dataclass
class Cached:
    value: Any
    at: float = field(default_factory=time.time)


class Bridge:
    """Both directions, around one lock.

    The lock is what keeps a Slack reply from coming back to Slack. A reply is
    posted to the forum and its post id recorded while the lock is held, and
    the poller asks about each post while holding it too, so it can never see
    a post the bridge wrote before the bridge has said that it wrote it.
    """

    def __init__(self, forum: Forum, slack: Any, state: State, prefix: str = CHANNEL_PREFIX):
        self.forum = forum
        self.slack = slack
        self.state = state
        self.prefix = prefix
        self.lock = threading.RLock()
        self._groups: dict[str, Cached] = {}
        self._categories: dict[int, Cached] = {}
        self._topics: dict[int, dict] = {}
        self._slack_ids: dict[str, Cached] = {}
        self._forum_users: dict[str, Cached] = {}
        self._last_category_sync = 0.0
        self.bot_user_id: str | None = None

    # -------------------------------------------------------- lookups

    def members(self, group: str) -> set[str]:
        cached = self._groups.get(group)
        if not cached or time.time() - cached.at > MEMBERSHIP_TTL_SECONDS:
            cached = self._groups[group] = Cached(self.forum.group_members(group))
        return cached.value

    def is_agent(self, username: str) -> bool:
        return username.lower() in self.members(AGENTS_GROUP_NAME)

    def is_feed(self, username: str) -> bool:
        return username.lower() in self.members(FEEDS_GROUP_NAME)

    def category(self, category_id: int) -> dict:
        cached = self._categories.get(category_id)
        if not cached or time.time() - cached.at > CATEGORY_SYNC_SECONDS:
            cached = self._categories[category_id] = Cached(self.forum.category(category_id))
        return cached.value

    def topic_meta(self, post: dict) -> dict:
        """Category, title, slug and opener for the post's topic.

        Read from the topic itself the first time, because the post listing
        does not say whether a topic is a private message, and a guess about
        that is the one guess here that would leak something.
        """
        topic_id = post["topic_id"]
        if topic_id not in self._topics:
            topic = self.forum.topic(topic_id)
            opener = ((topic.get("details") or {}).get("created_by") or {}).get("username") or ""
            stream = (topic.get("post_stream") or {}).get("stream") or []
            self._topics[topic_id] = {
                "id": topic_id,
                "title": topic.get("title") or post.get("topic_title") or f"Topic {topic_id}",
                "slug": topic.get("slug") or post.get("topic_slug") or "topic",
                "category_id": topic.get("category_id"),
                "archetype": topic.get("archetype") or "regular",
                "opener": opener,
                "first_post_id": stream[0] if stream else None,
            }
        return self._topics[topic_id]

    def researcher_for(self, agent: str) -> str | None:
        """The agent's researcher, by the convention 45-provision-agent.py names
        agents with, checked against the forum. The same derivation as
        ``Ranker.researcher_for`` in ranking.py, and the same limit: a name
        that was shortened to fit cannot be inverted, and gets no mention."""
        if not agent.lower().endswith(AGENT_SUFFIX):
            return None
        candidate = agent[: -len(AGENT_SUFFIX)]
        if not candidate:
            return None
        user = self.forum.user(candidate)
        return user.get("username") if user else None

    def slack_id_for(self, username: str) -> str | None:
        """A forum user's Slack id, matched through their email address."""
        key = username.lower()
        cached = self._slack_ids.get(key)
        if cached and time.time() - cached.at < MEMBERSHIP_TTL_SECONDS:
            return cached.value
        found = None
        email = self.forum.email_of(username)
        if email:
            try:
                found = (self.slack.users_lookupByEmail(email=email).get("user") or {}).get("id")
            except Exception as exc:  # users_not_found arrives as an exception
                if "users_not_found" not in str(exc):
                    LOG.warning("cannot look %s up in Slack: %s", username, exc)
        self._slack_ids[key] = Cached(found)
        return found

    def forum_user_for(self, slack_user: str) -> tuple[dict | None, str | None]:
        """(forum account, email) for a Slack user. Either may be None.

        Raises ``Unusable`` when the address does belong to an account, but
        not one a person may post as from here: an agent, a feed, or an
        account that is suspended or not yet active. Agent and feed addresses
        are on the forum's own domain and no Slack user should hold one, but
        posting as an agent is the one thing this bridge must never do, so it
        is not left to the addresses.
        """
        cached = self._forum_users.get(slack_user)
        if cached and time.time() - cached.at < MEMBERSHIP_TTL_SECONDS:
            return cached.value
        profile = self.slack.users_info(user=slack_user).get("user") or {}
        if profile.get("is_bot") or profile.get("deleted"):
            return None, None
        email = (profile.get("profile") or {}).get("email")
        account = self.forum.user_by_email(email) if email else None
        if account:
            name = account.get("username") or ""
            if self.is_agent(name) or self.is_feed(name):
                raise Unusable(f"{name} is not a person's account")
            if not account.get("active", True) or account.get("suspended_till"):
                raise Unusable(f"{name} is suspended or not yet active")
            # Only a hit is remembered. Somebody told to redeem an invite will
            # post again straight after, and has to be found when they do.
            self._forum_users[slack_user] = Cached((account, email))
        return account, email

    # -------------------------------------------------------- channels

    def ensure_channel(self, category: dict) -> str | None:
        """The category's channel, created if it has none. None if the category
        is not one everybody can read."""
        category_id = category.get("id")
        if category.get("read_restricted"):
            return None
        existing = self.state.channel_for(category_id)
        if existing:
            return existing

        parent = None
        if category.get("parent_category_id"):
            parent = self.category(category["parent_category_id"])
        name = channel_name(category, parent, self.prefix)
        url = f"{self.forum.base_url}/c/{category.get('slug')}/{category_id}"

        channel_id = None
        try:
            channel_id = self.slack.conversations_create(name=name, is_private=False)["channel"]["id"]
            LOG.info("created #%s for %s", name, category.get("name"))
        except Exception as exc:
            if "name_taken" not in str(exc):
                raise
            # Somebody made it by hand, or the state file was lost. Adopt it.
            channel_id = self.find_channel(name)
            if not channel_id:
                raise
            try:
                self.slack.conversations_join(channel=channel_id)
            except Exception as join_exc:
                if "already_in_channel" not in str(join_exc):
                    raise
            LOG.info("adopted the existing #%s for %s", name, category.get("name"))
        try:
            self.slack.conversations_setPurpose(
                channel=channel_id, purpose=f"Mirror of the forum category {category.get('name')}: {url}"
            )
        except Exception as exc:
            LOG.info("could not set the purpose of #%s: %s", name, exc)
        self.state.set_channel(category_id, channel_id)
        return channel_id

    def find_channel(self, name: str) -> str | None:
        cursor = None
        while True:
            page = self.slack.conversations_list(types="public_channel", limit=200, cursor=cursor, exclude_archived=True)
            for channel in page.get("channels") or []:
                if channel.get("name") == name:
                    return channel.get("id")
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None

    def ensure_name(self, category: dict, channel_id: str) -> None:
        """Rename the category's channel if its name is not the one the
        category's slug and the prefix give, as after either one changes.

        Only on the hourly sync, not per post, since a name is not urgent and
        each check is a call to Slack.
        """
        parent = None
        if category.get("parent_category_id"):
            parent = self.category(category["parent_category_id"])
        wanted = channel_name(category, parent, self.prefix)
        current = (self.slack.conversations_info(channel=channel_id).get("channel") or {}).get("name")
        if current == wanted:
            return
        try:
            self.slack.conversations_rename(channel=channel_id, name=wanted)
            LOG.info("renamed #%s to #%s for %s", current, wanted, category.get("name"))
        except Exception as exc:
            # Most often name_taken: somebody made a channel with that name by
            # hand. The bridge keeps using the one it has.
            LOG.warning("cannot rename #%s to #%s: %s", current, wanted, exc)

    def sync_categories(self, force: bool = False) -> None:
        """A channel for every category everybody can read, under its current name."""
        if not force and time.time() - self._last_category_sync < CATEGORY_SYNC_SECONDS:
            return
        self._last_category_sync = time.time()
        for category in self.forum.categories():
            self._categories[category["id"]] = Cached(category)
        for category in list(self._categories.values()):
            try:
                with self.lock:
                    channel_id = self.ensure_channel(category.value)
                    if channel_id:
                        self.ensure_name(category.value, channel_id)
            except Exception as exc:
                LOG.error("cannot make a channel for %s: %s", category.value.get("name"), exc)

    # -------------------------------------------------------- forum to Slack

    def post_url(self, topic: dict, post_number: int) -> str:
        return f"{self.forum.base_url}/t/{topic['slug']}/{topic['id']}/{post_number}"

    def ensure_thread(self, topic: dict, channel_id: str) -> str:
        """The topic's thread in its category's channel: an opening message
        naming the topic, which every post then replies under."""
        existing = self.state.thread_for(topic["id"])
        if existing and existing[0] == channel_id:
            return existing[1]
        url = f"{self.forum.base_url}/t/{topic['slug']}/{topic['id']}"
        opener = topic.get("opener")
        text = f"*<{url}|{slack_escape(topic['title'])}>*"
        if opener:
            text += f"\nopened by {slack_escape(opener)}. Reply in this thread to post to the topic."
        result = self.slack.chat_postMessage(channel=channel_id, text=text, unfurl_links=False, unfurl_media=False)
        self.state.set_thread(topic["id"], channel_id, result["ts"])
        return result["ts"]

    def relay_post(self, post: dict) -> str:
        """Put one forum post into Slack. Returns what happened, for the log."""
        if post.get("post_type", POST_TYPE_REGULAR) != POST_TYPE_REGULAR:
            return "not a regular post"
        if post.get("hidden") or post.get("deleted_at") or post.get("user_deleted"):
            return "hidden or deleted"
        if self.state.came_from_slack(post["id"]):
            return "came from Slack"

        topic = self.topic_meta(post)
        if topic["archetype"] != "regular" or not topic.get("category_id"):
            return "not a public topic"
        category = self.category(topic["category_id"])
        channel_id = self.ensure_channel(category)
        if not channel_id:
            return "restricted category"

        number = post.get("post_number") or 1
        threaded = self.state.thread_for(topic["id"])

        if not threaded and self.is_feed(topic.get("opener") or ""):
            if number == 1:
                return "a feed topic with no reply yet"
            # The first reply to a preprint. The thread gets the preprint too,
            # because a reply with nothing above it means nothing.
            thread_ts = self.ensure_thread(topic, channel_id)
            if topic.get("first_post_id"):
                self.send(self.forum.post(topic["first_post_id"]), topic, channel_id, thread_ts)
        thread_ts = self.ensure_thread(topic, channel_id)
        self.send(post, topic, channel_id, thread_ts)
        return f"relayed to {channel_id}"

    def send(self, post: dict, topic: dict, channel_id: str, thread_ts: str) -> None:
        author = post.get("username") or "somebody"
        url = self.post_url(topic, post.get("post_number") or 1)
        if "raw" not in post:
            post = {**post, "raw": self.forum.post(post["id"]).get("raw", "")}
        body = to_slack(post.get("raw") or "", url)
        lead = ""
        if self.is_agent(author):
            researcher = self.researcher_for(author)
            slack_id = self.slack_id_for(researcher) if researcher else None
            if slack_id:
                lead = f"<@{slack_id}>, your agent posted <{url}|in this topic>:\n"
        text = f"{lead}{body}" if lead else f"{body}\n<{url}|on the forum>"
        try:
            self.slack.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=text,
                username=author,
                unfurl_links=False,
                unfurl_media=False,
            )
        except Exception as exc:
            # The thread's parent was deleted in Slack. Start a new one rather
            # than failing on every later post in the topic.
            if "thread_not_found" in str(exc) or "message_not_found" in str(exc):
                self.state.drop_thread(topic["id"])
                fresh = self.ensure_thread(topic, channel_id)
                self.slack.chat_postMessage(
                    channel=channel_id, thread_ts=fresh, text=text, username=author,
                    unfurl_links=False, unfurl_media=False,
                )
                return
            raise

    def new_posts(self) -> list[dict]:
        """Every post after the cursor, oldest first.

        On the very first run the cursor is set to the newest post and nothing
        is relayed, so connecting a forum with history does not post the
        history into Slack.
        """
        cursor = self.state.cursor()
        page = self.forum.latest_posts()
        if not page:
            return []
        if cursor is None:
            self.state.set_cursor(max(p["id"] for p in page))
            LOG.info("first run: starting after post %d", self.state.cursor())
            return []

        found = [p for p in page if p["id"] > cursor]
        pages = 1
        while page and min(p["id"] for p in page) > cursor + 1 and pages < MAX_PAGES_PER_POLL:
            page = self.forum.latest_posts(before=min(p["id"] for p in page))
            found.extend(p for p in page if p["id"] > cursor)
            pages += 1
        found.sort(key=lambda p: p["id"])
        if page and found and min(p["id"] for p in page) > cursor + 1:
            # More than MAX_PAGES_PER_POLL pages behind, which is a long outage.
            # Relaying the whole gap would bury the channels, so the oldest of
            # it is left on the forum, and the log says how much.
            LOG.warning(
                "posts %d to %d are older than %d pages and were not relayed",
                cursor + 1, found[0]["id"] - 1, MAX_PAGES_PER_POLL,
            )
        return found

    def poll(self) -> int:
        """Relay what is new. Returns how many posts were relayed."""
        self.sync_categories()
        relayed = 0
        for post in self.new_posts():
            with self.lock:
                try:
                    outcome = self.relay_post(post)
                except Transient as exc:
                    LOG.warning("stopping at post %d until the next poll: %s", post["id"], exc)
                    return relayed
                except Exception as exc:
                    # Slack refusing one post is not a reason to hold up the
                    # rest, and retrying it forever would hold them up.
                    LOG.error("could not relay post %d: %s", post["id"], exc)
                    outcome = "failed"
                self.state.set_cursor(post["id"])
            if outcome.startswith("relayed"):
                relayed += 1
            LOG.info("post %d by %s: %s", post["id"], post.get("username"), outcome)
        return relayed

    # -------------------------------------------------------- Slack to the forum

    def handle_message(self, event: dict) -> str:
        """One Slack message event. Returns what happened, for the log."""
        # An edit, a deletion and a bot's own post all arrive without a user,
        # or with the bot as the user, and none of them is somebody replying.
        if event.get("bot_id") or event.get("user") in (None, self.bot_user_id):
            return "not from a person"
        subtype = event.get("subtype")
        channel_id = event.get("channel") or ""
        user = event["user"]
        if self.state.category_for(channel_id) is None:
            return "not a bridged channel"

        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == event.get("ts"):
            if subtype:
                return f"ignored {subtype}"
            self.ephemeral(
                channel_id, user, None,
                "That message stays in Slack. To post to the forum, reply in a topic's thread; "
                "to start a topic, open it on the forum and its thread appears here.",
            )
            return "top-level message, not relayed"

        if subtype == "file_share":
            self.ephemeral(
                channel_id, user, thread_ts,
                "Files are not carried to the forum, so this was not posted. Attach it on the forum instead.",
            )
            return "file share, not relayed"
        if subtype not in (None, "thread_broadcast"):
            return f"ignored {subtype}"

        topic_id = self.state.topic_for(channel_id, thread_ts)
        if topic_id is None:
            self.ephemeral(channel_id, user, thread_ts, "This thread is not a forum topic, so nothing was posted.")
            return "thread is not a topic"

        try:
            account, email = self.forum_user_for(user)
        except Unusable as exc:
            self.ephemeral(
                channel_id, user, thread_ts,
                "Your Slack address belongs to a forum account that cannot post from here, so nothing "
                "was posted. Ask a forum admin.",
            )
            return f"refused: {exc}"
        if not account:
            return self.invite(user, email)

        raw = from_slack(event.get("text") or "", self.mention_for)
        if not raw:
            return "empty"
        with self.lock:
            try:
                created = self.forum.reply(topic_id, raw, as_user=account["username"])
            except Refused as exc:
                reason = forum_error(exc)
                self.ephemeral(channel_id, user, thread_ts, f"The forum did not accept that reply: {reason}")
                return f"refused: {reason}"
            except Transient as exc:
                self.ephemeral(channel_id, user, thread_ts, "The forum is not answering just now, so that was not posted. Try again in a minute.")
                return f"transient: {exc}"
            post_id = created.get("id")
            if post_id:
                self.state.record_from_slack(post_id, channel_id, event.get("ts") or "")
        return f"posted as {account['username']} (post {post_id})"

    def mention_for(self, slack_user: str) -> str:
        try:
            account, _ = self.forum_user_for(slack_user)
        except Exception:  # Unusable included: the name is still shown
            account = None
        if account:
            return f"@{account['username']}"
        try:
            profile = self.slack.users_info(user=slack_user).get("user") or {}
            return (profile.get("profile") or {}).get("display_name") or profile.get("real_name") or "someone"
        except Exception:
            return "someone"

    def invite(self, slack_user: str, email: str | None) -> str:
        """Invite somebody with no forum account, and tell them by DM."""
        if not email:
            self.dm(
                slack_user,
                "Your reply was not posted, because you have no account on the forum and Slack has no "
                "email address for you to invite. Ask a forum admin for an invite.",
            )
            return "no account, no email"

        # A Slack guest was let into some channels, not into the workspace, so
        # being one is no reason to be let into the forum.
        profile = self.slack.users_info(user=slack_user).get("user") or {}
        if profile.get("is_restricted") or profile.get("is_ultra_restricted"):
            self.dm(
                slack_user,
                "Your reply was not posted, because you have no account on the forum. Guests in this "
                "workspace are not invited automatically: ask a forum admin for an invite.",
            )
            return "no account, a guest"

        last = self.state.invited_at(email)
        if last and time.time() - last < INVITE_RESEND_DAYS * 86400:
            self.dm(
                slack_user,
                f"Your reply was not posted, because you have no account on the forum yet. An invitation "
                f"was sent to {email} already: look in that inbox, including spam, and once you have "
                f"joined, post your reply again.",
            )
            return "no account, invited already"

        try:
            self.forum.invite(email)
        except (Refused, Transient) as exc:
            LOG.error("cannot invite %s: %s", email, exc)
            self.dm(
                slack_user,
                "Your reply was not posted, because you have no account on the forum, and the forum "
                "would not send you an invitation just now. Ask a forum admin for one.",
            )
            return f"no account, invite failed: {exc}"
        self.state.record_invite(email, time.time())
        self.dm(
            slack_user,
            f"Your reply was not posted, because you have no account on the forum yet. An invitation "
            f"has just been sent to {email}: check that inbox, including spam, and follow the link to "
            f"create your account. Then post your reply again and it will appear under your name.",
        )
        return "no account, invited"

    def ephemeral(self, channel: str, user: str, thread_ts: str | None, text: str) -> None:
        try:
            self.slack.chat_postEphemeral(channel=channel, user=user, text=text, thread_ts=thread_ts)
        except Exception as exc:
            LOG.warning("cannot tell %s: %s", user, exc)

    def dm(self, user: str, text: str) -> None:
        try:
            channel = self.slack.conversations_open(users=user)["channel"]["id"]
            self.slack.chat_postMessage(channel=channel, text=text)
        except Exception as exc:
            LOG.warning("cannot message %s directly: %s", user, exc)


def forum_error(exc: Refused) -> str:
    """The sentence Discourse gave, which is written for the person posting."""
    try:
        errors = json.loads(exc.detail).get("errors")
        if errors:
            return " ".join(errors)
    except (ValueError, AttributeError):
        pass
    return f"HTTP {exc.status}"


# ---------------------------------------------------------------------- running it


def connect_slack(bridge: Bridge) -> Any:
    """Start the Socket Mode connection on its own thread, and return it."""
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    client = SocketModeClient(app_token=APP_TOKEN, web_client=bridge.slack)

    def listener(socket: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledged first, because Slack redelivers an event it has not
        # heard back about within three seconds, and posting can take longer.
        socket.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        if event.get("type") != "message":
            return
        try:
            outcome = bridge.handle_message(event)
        except Exception:
            LOG.exception("could not handle a Slack message")
            return
        LOG.info("slack message from %s in %s: %s", event.get("user"), event.get("channel"), outcome)

    client.socket_mode_request_listeners.append(listener)
    client.connect()
    return client


def make_slack() -> Any:
    from slack_sdk import WebClient
    from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

    client = WebClient(token=BOT_TOKEN)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return client


def check(forum: Forum, slack: Any) -> int:
    ok = True
    try:
        me = forum.request("GET", f"/u/{API_USERNAME}.json").get("user", {})
        print(f"forum   ok, reading as {me.get('username')}")
        forum.request("GET", "/admin/users/list/all.json?email=nobody%40example.invalid")
        print("forum   ok, the key can look users up by email")
    except Exception as exc:
        print(f"forum   FAILED: {exc}")
        ok = False
    try:
        auth = slack.auth_test()
        print(f"slack   ok, the bot is {auth.get('user')} in {auth.get('team')}")
    except Exception as exc:
        print(f"slack   FAILED: {exc}")
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="verify both credentials and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    missing = [n for n, v in (("DISCOURSE_API_KEY", API_KEY), ("SLACK_BOT_TOKEN", BOT_TOKEN), ("SLACK_APP_TOKEN", APP_TOKEN)) if not v]
    if missing:
        print(f"ERROR: set {', '.join(missing)}.", file=sys.stderr)
        return 2

    forum = Forum(FORUM_URL, API_KEY, API_USERNAME)
    slack = make_slack()
    if args.check:
        return check(forum, slack)

    bridge = Bridge(forum, slack, State(STATE_PATH))
    bridge.bot_user_id = slack.auth_test().get("user_id")
    bridge.sync_categories(force=True)
    connect_slack(bridge)
    LOG.info("bridging %s to Slack, polling every %ss", FORUM_URL, POLL_SECONDS)

    while True:
        try:
            bridge.poll()
        except Transient as exc:
            LOG.warning("forum unavailable: %s", exc)
        except Exception:
            LOG.exception("poll failed")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
