#!/usr/bin/env python3
"""Mirror the forum into a Slack workspace, and carry Slack replies back.

One channel per category, one thread per topic; a thread reply is posted to the
topic as the Slack user's own forum account, matched by email, and an edit to
it there edits the post. A DM with the bot is a private message with the
writer's agent. The policy and interests topics are linked, not copied.
providers/slack/README.md describes the behaviour and the credential.

Usage::

    python3 slack-bridge.py                 # run the bridge
    python3 slack-bridge.py --check         # verify both credentials and exit
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import mimetypes
import os
import posixpath
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger("slack-bridge")

FORUM_URL = os.environ.get("DISCOURSE_URL", "http://127.0.0.1")
API_KEY = os.environ.get("DISCOURSE_API_KEY", "")
API_USERNAME = os.environ.get("DISCOURSE_API_USERNAME", "system")
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
CHANNEL_PREFIX = os.environ.get("SLACK_CHANNEL_PREFIX", "qupa-")
# `everything` relays from the forum's first post; `newest` only what follows.
FIRST_RUN = os.environ.get("SLACK_FIRST_RUN", "everything")
STATE_PATH = Path(os.environ.get("BRIDGE_STATE", "/var/lib/qupa-slack-bridge/state.db"))
POLL_SECONDS = float(os.environ.get("BRIDGE_POLL_SECONDS", "15"))
CATEGORY_SYNC_SECONDS = float(os.environ.get("BRIDGE_CATEGORY_SYNC_SECONDS", "3600"))
# How long an invite link lasts. Until then a newcomer who writes again is
# given the same link, kept in the state file.
INVITE_DAYS = float(os.environ.get("BRIDGE_INVITE_DAYS", "7"))
# A link this close to expiring is replaced rather than given out.
INVITE_MARGIN_SECONDS = 3600
# What Bridge.invite returns when it gave somebody a link.
INVITED = ("no account, invited", "no account, invited already")
# Edited often, so a copy in Slack would soon be out of date: each has a thread
# holding only a link to it.
POLICY_TOPIC_ID = int(os.environ.get("POLICY_TOPIC_ID", "0") or 0)
INTERESTS_TOPIC_ID = int(os.environ.get("INTERESTS_TOPIC_ID", "0") or 0)

AGENTS_GROUP_NAME = "agents"
FEEDS_GROUP_NAME = "feeds"
AGENT_SUFFIX = "-agent"

UPLOAD_SCHEME = "upload://"

# An agent's *Thinking…* post, which its reply replaces by an edit: the marker
# is the agent kit's (agent-kit/qupa_cafe/placeholders.py). Its copy in Slack is
# updated to the answer, the one forum edit the bridge carries. One not filled
# in a day is given up on; the kit deletes it long before.
PLACEHOLDER = re.compile(r"<!--\s*qupa-cafe:thinking\b[^>]*-->")
PLACEHOLDER_SLACK_TEXT = "_Thinking…_"
PLACEHOLDER_WATCH_SECONDS = 86400

# Not a whisper or a small action.
POST_TYPE_REGULAR = 1

TOPIC_HINT = "Reply in this thread to post to the topic."
PRIVATE_HINT = "A private message on the forum. Reply in this thread to post to it."
LINK_ONLY_HINT = "This topic is edited often, so it is not copied here. Read it, and reply, on the forum."

MEMBERSHIP_TTL_SECONDS = 600
# A name a group's cached members lack reads the group again, at most this
# often, so an agent provisioned a moment ago is known as one.
MEMBERSHIP_MISS_SECONDS = 30

MAX_PAGES_PER_POLL = 20

# Files are carried both ways, up to this many a post, which is also Slack's
# limit a message. The forum takes 4 MB a file and refuses more itself, so the
# byte ceiling is only against a misconfigured one.
MAX_FILES_PER_POST = 10
MAX_FILE_BYTES = 8 * 1024 * 1024
# Where a Slack file's bytes are read from; the bot token goes nowhere else.
SLACK_FILES_URL = "https://files.slack.com/"


# ---------------------------------------------------------------------- state


class State:
    """What survives a restart. Locked: Slack events arrive on another thread."""

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
                CREATE TABLE IF NOT EXISTS placeholders (
                    post_id INTEGER PRIMARY KEY, channel_id TEXT NOT NULL, ts TEXT NOT NULL,
                    thread_ts TEXT NOT NULL, topic_id INTEGER NOT NULL, title TEXT NOT NULL,
                    slug TEXT NOT NULL, mention INTEGER NOT NULL, codes INTEGER NOT NULL, at REAL NOT NULL);
                """
            )
            # A post written in Slack keeps the text it was given, to tell an
            # edit from Slack's report of a link preview arriving.
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(from_slack)")}
            if "raw" not in columns:
                self.db.execute("ALTER TABLE from_slack ADD COLUMN raw TEXT")
            # And the markdown of the files it carried, which an edit in Slack
            # keeps: the edited message's text does not mention them.
            if "files" not in columns:
                self.db.execute("ALTER TABLE from_slack ADD COLUMN files TEXT")
            # An invitation is a link handed to its invitee, reused until it expires.
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(invites)")}
            if "link" not in columns:
                self.db.execute("ALTER TABLE invites ADD COLUMN link TEXT")
            if "expires_at" not in columns:
                self.db.execute("ALTER TABLE invites ADD COLUMN expires_at REAL")

    def _one(self, sql: str, *args) -> tuple | None:
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _run(self, sql: str, *args) -> None:
        with self.lock:
            self.db.execute(sql, args)

    def cursor(self, name: str = "cursor") -> int | None:
        row = self._one("SELECT v FROM meta WHERE k = ?", name)
        return int(row[0]) if row else None

    def set_cursor(self, post_id: int, name: str = "cursor") -> None:
        self._run("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", name, str(post_id))

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

    def origin_of(self, post_id: int) -> str | None:
        row = self._one("SELECT channel_id FROM from_slack WHERE post_id = ?", post_id)
        return row[0] if row else None

    def record_from_slack(
        self, post_id: int, channel_id: str, ts: str, raw: str | None = None, files: str = "",
    ) -> None:
        self._run(
            "INSERT OR IGNORE INTO from_slack (post_id, channel_id, ts, raw, files) VALUES (?, ?, ?, ?, ?)",
            post_id, channel_id, ts, raw, files,
        )

    def written_at(self, channel_id: str, ts: str) -> tuple[int, str | None, str] | None:
        """(post id, raw, files' markdown) for the post a Slack message became."""
        row = self._one("SELECT post_id, raw, files FROM from_slack WHERE channel_id = ? AND ts = ?", channel_id, ts)
        return (row[0], row[1], row[2] or "") if row else None

    def set_slack_raw(self, post_id: int, raw: str) -> None:
        self._run("UPDATE from_slack SET raw = ? WHERE post_id = ?", raw, post_id)

    def watch_placeholder(
        self, post_id: int, channel_id: str, ts: str, thread_ts: str, topic: dict, mention: bool, codes: bool,
    ) -> None:
        self._run(
            "INSERT OR REPLACE INTO placeholders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            post_id, channel_id, ts, thread_ts, topic["id"], topic.get("title") or "", topic.get("slug") or "topic",
            int(mention), int(codes), time.time(),
        )

    def placeholders(self) -> list[tuple]:
        with self.lock:
            return self.db.execute(
                "SELECT post_id, channel_id, ts, thread_ts, topic_id, title, slug, mention, codes, at "
                "FROM placeholders ORDER BY post_id"
            ).fetchall()

    def drop_placeholder(self, post_id: int) -> None:
        self._run("DELETE FROM placeholders WHERE post_id = ?", post_id)

    def invite_for(self, email: str, now: float) -> tuple[str, float] | None:
        """(link, expires_at) of an invitation with time left on it."""
        row = self._one("SELECT link, expires_at FROM invites WHERE email = ?", email.lower())
        if not row or not row[0] or (row[1] or 0) - now < INVITE_MARGIN_SECONDS:
            return None
        return row[0], row[1]

    def record_invite(self, email: str, link: str, expires_at: float, when: float) -> None:
        self._run(
            "INSERT INTO invites (email, invited_at, link, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET invited_at = excluded.invited_at, "
            "link = excluded.link, expires_at = excluded.expires_at",
            email.lower(),
            when,
            link,
            expires_at,
        )


# ---------------------------------------------------------------------- the forum


class Transient(RuntimeError):
    """The forum could not answer now; the next poll resumes from this post."""


class Refused(RuntimeError):
    """The forum answered, and the answer was no."""

    def __init__(self, message: str, status: int, detail: str):
        super().__init__(message)
        self.status = status
        self.detail = detail


class Unusable(Exception):
    """A Slack user's address belongs to an account they cannot post as."""


class CannotCarry(Exception):
    """A Slack file the bridge cannot read, with the reason for its writer."""


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

        # 429 is a queue: the API limiter is shared with every agent.
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

    def latest_posts(self, before: int | None = None, private: bool = False) -> list[dict]:
        """Newest first. ``/private-posts.json`` is every private message, for an admin."""
        path, root = ("/private-posts.json", "private_posts") if private else ("/posts.json", "latest_posts")
        return self.request("GET", path + (f"?before={before}" if before else "")).get(root) or []

    def topic(self, topic_id: int) -> dict:
        return self.request("GET", f"/t/{topic_id}.json")

    def post(self, post_id: int) -> dict:
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
        """Exact match; the admin list's ``filter`` is a substring search."""
        query = urllib.parse.urlencode({"email": email, "show_emails": "true"})
        for user in self.request("GET", f"/admin/users/list/all.json?{query}") or []:
            if (user.get("email") or "").lower() == email.lower():
                return user
        return None

    def reply(self, topic_id: int, raw: str, as_user: str) -> dict:
        return self.request("POST", "/posts.json", {"topic_id": topic_id, "raw": raw}, as_user=as_user)

    def private_message(self, title: str, raw: str, recipients: list[str], as_user: str) -> dict:
        return self.request(
            "POST",
            "/posts.json",
            {"title": title, "raw": raw, "archetype": "private_message", "target_recipients": ",".join(recipients)},
            as_user=as_user,
        )

    def edit(self, post_id: int, raw: str, as_user: str) -> dict:
        return self.request("PUT", f"/posts/{post_id}.json", {"post[raw]": raw}, as_user=as_user)

    def add_to_message(self, topic_id: int, username: str) -> dict:
        return self.request("POST", f"/t/{topic_id}/invite.json", {"user": username})

    def invite(self, email: str, expires_at: float) -> dict:
        """An invitation bound to ``email``, mailed to nobody: its link is the
        answer's ``link``. Asked again for the same address, the forum gives
        the same invitation with the new expiry."""
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at))
        return self.request("POST", "/invites.json", {"email": email, "skip_email": "true", "expires_at": expires})

    def lookup_uploads(self, short_urls: list[str]) -> dict[str, str]:
        """``upload://`` references, as raw markdown has them, to the paths
        they are served from."""
        found = self.request("POST", "/uploads/lookup-urls.json", {"short_urls[]": short_urls})
        return {u["short_url"]: u["url"] for u in found or [] if u.get("short_url") and u.get("url")}

    def upload(self, filename: str, content: bytes, as_user: str) -> dict:
        """A file uploaded as ``as_user``, attachable to their next post. The
        forum's own checks apply: its extensions, its size limit."""
        boundary = "qupa" + uuid.uuid4().hex
        quoted = filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ncomposer\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files[]"; filename="{quoted}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{self.base_url}/uploads.json", data=body, method="POST")
        req.add_header("Api-Key", self.api_key)
        req.add_header("Api-Username", as_user)
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            if exc.code == 429 or exc.code >= 500:
                raise Transient(f"uploading {filename} -> {exc.code}: {detail}") from exc
            raise Refused(f"uploading {filename} -> {exc.code}: {detail}", exc.code, detail) from exc
        except urllib.error.URLError as exc:
            raise Transient(f"uploading {filename} failed: {exc.reason}") from exc

    def download(self, url: str) -> bytes:
        """An upload's bytes, read from the forum this bridge talks to,
        whatever host the forum names in ``url``. The key is not carried
        through the forum's redirect to a signed object-store address, which
        needs none."""
        parts = urllib.parse.urlsplit(url)
        req = urllib.request.Request(f"{self.base_url}{parts.path}" + (f"?{parts.query}" if parts.query else ""))
        req.add_unredirected_header("Api-Key", self.api_key)
        req.add_unredirected_header("Api-Username", self.username)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read(MAX_FILE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise Refused(f"GET {parts.path} -> {exc.code}", exc.code, "") from exc
        except urllib.error.URLError as exc:
            raise Transient(f"GET {parts.path} failed: {exc.reason}") from exc
        if len(content) > MAX_FILE_BYTES:
            raise Refused(f"{parts.path} is larger than {MAX_FILE_BYTES} bytes", 413, "")
        return content


# ---------------------------------------------------------------------- text


_MD_LINK = re.compile(r"(?<!!)\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^\s)]+)\)")
# The files the bridge uploads after a post: the forum's own, images and
# attachments alike.
_UPLOADED_FILE = re.compile(r"!?\[([^\]\n]*)\]\((upload://[^\s)]+)\)")
_MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
# A fence's language, which Slack has no place for and shows as the first line.
_FENCE_LANGUAGE = re.compile(r"^([ \t]*(?:```|~~~))[^\s`~]+[ \t]*$", re.M)
# An attachment link keeps only its name.
_UPLOAD = re.compile(r"(?<!!)\[([^\]|\n]*)(?:\|[^\]\n]*)?\]\(upload://[^)]+\)")
# Discourse writes some of its own messages in HTML.
_HTML_LINK = re.compile(r"""<a\s[^>]*?href\s*=\s*(["'])([^"'\s]+)\1[^>]*>(.*?)</a\s*>""", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


# A Discourse API key; a SHA-256 digest has the same shape and goes too.
_SECRET = re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{64}(?![0-9A-Za-z])")
# An agent kit setup code, which holds a key: scripts/45-provision-agent.py.
_SETUP_CODE = re.compile(r"qupa-setup-[A-Za-z0-9_-]{20,}")


def redact(text: str, codes: bool = False) -> str:
    """Keys out of Slack, and setup codes unless ``codes``: the forum's own
    message to one person carries theirs to their DM."""
    text = _SECRET.sub("[a 64-character hex string, possibly a credential: read it on the forum]", text)
    return text if codes else _SETUP_CODE.sub("[a setup code: read it on the forum]", text)


def image_text(match: re.Match) -> str:
    """An image as its name, in italics as the bridge's own words are. One
    elsewhere becomes a link, since the bridge fetches only from the forum: a
    post names any address it likes."""
    name = match.group(1).split("|")[0] or "untitled"
    url = match.group(2)
    return f"_[image: {name}]({url})_" if url.startswith(("http://", "https://")) else f"_[image: {name}]_"


def files_in(raw: str) -> list[tuple[str, str]]:
    """(name, ``upload://`` reference) for each uploaded file, once each."""
    seen: dict[str, str] = {}
    for label, short_url in _UPLOADED_FILE.findall(raw):
        seen.setdefault(short_url, label.split("|")[0].strip())
    return [(name, short_url) for short_url, name in seen.items()][:MAX_FILES_PER_POST]


def upload_markdown(upload: dict, name: str) -> str:
    """What the forum's composer writes for an upload: an image shown, any
    other file as a link."""
    short_url = upload.get("short_url") or upload.get("url") or ""
    label = (upload.get("original_filename") or name).replace("[", "(").replace("]", ")").replace("|", "_")
    if upload.get("width") and upload.get("height"):
        return f"![{label}|{upload['width']}x{upload['height']}]({short_url})"
    size = f" ({upload['human_filesize']})" if upload.get("human_filesize") else ""
    return f"[{label}|attachment]({short_url}){size}"


def with_files(text: str, files: str) -> str:
    return "\n\n".join(part for part in (text, files) if part)


def slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html_link(match: re.Match, base: str) -> str:
    """An HTML link as a markdown one, which ``to_slack`` converts with the
    rest. A relative address is resolved against the post's."""
    url = urllib.parse.urljoin(base, html.unescape(match.group(2)))
    label = html.unescape(_HTML_TAG.sub("", match.group(3))).strip().replace("[", "(").replace("]", ")")
    if not url.startswith(("http://", "https://")):
        return label or match.group(0)
    return f"[{label or url}]({url})"


def to_slack(raw: str, link: str, codes: bool = False) -> str:
    """Forum markdown, readable in a thread: links, bold and images converted,
    and the rest as written, in full. ``link`` is the post's address, against
    which a relative one is resolved; ``codes`` is ``redact``'s. A post is at
    most `max_post_length` (3,800) characters, which with a title and a closing
    line stays under the 4,000 Slack allows in an edited message; a new one may
    run to 40,000. The forum's own files follow the post, uploaded by
    ``Bridge.send_files``."""
    text = _MD_IMAGE.sub(image_text, _HTML_COMMENT.sub("", redact(raw, codes)))
    text = _UPLOAD.sub(lambda m: m.group(1), text)
    text = _FENCE_LANGUAGE.sub(lambda m: m.group(1), text)
    text = _HTML_LINK.sub(lambda m: html_link(m, link), text)
    text = slack_escape(text)
    # Links after escaping, so their angle brackets are the only live ones.
    text = _MD_LINK.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    text = _MD_BOLD.sub(lambda m: f"*{m.group(1)}*", text)
    return text.strip()


_SLACK_USER = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_SLACK_CHANNEL = re.compile(r"<#[CG][A-Z0-9]+\|([^>]*)>")
_SLACK_LINK = re.compile(r"<((?:https?|mailto):[^|>]+)(?:\|([^>]+))?>")
_SLACK_SPECIAL = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>|<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")


def from_slack(text: str, name_for: Callable[[str], str]) -> str:
    """Slack's message text as forum markdown. ``name_for`` maps a Slack user id
    to ``@username`` or a plain name; ``@here`` and its kind are spelled out."""
    text = _SLACK_USER.sub(lambda m: name_for(m.group(1)), text)
    text = _SLACK_CHANNEL.sub(lambda m: f"#{m.group(1)}", text)
    text = _SLACK_SPECIAL.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _SLACK_LINK.sub(
        lambda m: f"[{m.group(2)}]({m.group(1)})" if m.group(2) and m.group(2) != m.group(1) else m.group(1),
        text,
    )
    return html.unescape(text).strip()


def message_title(raw: str) -> str:
    """The first line, cut near eighty characters, and never under fifteen."""
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    first = re.sub(r"[*_`#>\[\]]", "", first).strip()
    if len(first) > 80:
        first = first[:80].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return first if len(first) >= 15 else f"From Slack: {first}" if first else "A message from Slack"


def gone(post: dict | None) -> bool:
    """No post, or one hidden or deleted, which the bridge leaves out of Slack."""
    return not post or bool(post.get("hidden") or post.get("deleted_at") or post.get("user_deleted"))


def same_text(a: str | None, b: str | None) -> bool:
    """Equal as the forum stores them, which trims and uses bare newlines."""
    return (a or "").replace("\r\n", "\n").strip() == (b or "").replace("\r\n", "\n").strip()


def channel_name(category: dict, parent: dict | None, prefix: str) -> str:
    """``qupa-<slug>``, or ``qupa-<parent>-<slug>`` for a subcategory."""
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
    """Both directions, around one lock, which is held while a Slack reply is
    posted and recorded so the poller never relays it back."""

    def __init__(
        self, forum: Forum, slack: Any, state: State, prefix: str = CHANNEL_PREFIX, first_run: str = FIRST_RUN,
        link_only: tuple[int, ...] = (POLICY_TOPIC_ID, INTERESTS_TOPIC_ID),
    ):
        self.forum = forum
        self.slack = slack
        self.state = state
        self.prefix = prefix
        self.first_run = first_run
        self.link_only = {topic_id for topic_id in link_only if topic_id}
        self.lock = threading.RLock()
        self._groups: dict[str, Cached] = {}
        self._categories: dict[int, Cached] = {}
        self._topics: dict[int, dict] = {}
        self._slack_ids: dict[str, Cached] = {}
        self._forum_users: dict[str, Cached] = {}
        self._conversations: dict[str, Cached] = {}
        self._last_category_sync = 0.0
        self.bot_user_id: str | None = None

    # -------------------------------------------------------- lookups

    def members(self, group: str, older_than: float = MEMBERSHIP_TTL_SECONDS) -> set[str]:
        cached = self._groups.get(group)
        if not cached or time.time() - cached.at > older_than:
            cached = self._groups[group] = Cached(self.forum.group_members(group))
        return cached.value

    def in_group(self, group: str, username: str) -> bool:
        name = username.lower()
        return name in self.members(group) or name in self.members(group, MEMBERSHIP_MISS_SECONDS)

    def is_agent(self, username: str) -> bool:
        return self.in_group(AGENTS_GROUP_NAME, username)

    def is_feed(self, username: str) -> bool:
        return self.in_group(FEEDS_GROUP_NAME, username)

    def category(self, category_id: int) -> dict:
        cached = self._categories.get(category_id)
        if not cached or time.time() - cached.at > CATEGORY_SYNC_SECONDS:
            cached = self._categories[category_id] = Cached(self.forum.category(category_id))
        return cached.value

    def topic_meta(self, post: dict) -> dict:
        """Read from the topic, since the listing does not say whether it is private."""
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
        """``<researcher>-agent`` inverted and checked against the forum."""
        if not agent.lower().endswith(AGENT_SUFFIX):
            return None
        candidate = agent[: -len(AGENT_SUFFIX)]
        if not candidate:
            return None
        user = self.forum.user(candidate)
        return user.get("username") if user else None

    def slack_id_for(self, username: str) -> str | None:
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
        """(forum account, email) for a Slack user; either may be None. Raises
        ``Unusable`` for an agent, a feed, or a suspended or inactive account."""
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
            # Only a hit is cached: an invitee posts again once they have joined.
            self._forum_users[slack_user] = Cached((account, email))
        return account, email

    # -------------------------------------------------------- channels

    def channel_name(self, category: dict) -> str:
        parent_id = category.get("parent_category_id")
        return channel_name(category, self.category(parent_id) if parent_id else None, self.prefix)

    def ensure_channel(self, category: dict) -> str | None:
        """The category's channel, created if needed; None if read-restricted."""
        category_id = category.get("id")
        if category.get("read_restricted"):
            return None
        existing = self.state.channel_for(category_id)
        if existing:
            return existing

        name = self.channel_name(category)
        url = f"{self.forum.base_url}/c/{category.get('slug')}/{category_id}"

        channel_id = None
        try:
            channel_id = self.slack.conversations_create(name=name, is_private=False)["channel"]["id"]
            LOG.info("created #%s for %s", name, category.get("name"))
        except Exception as exc:
            if "name_taken" not in str(exc):
                raise
            # Made by hand, or the state file was lost: adopt it.
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
        """Rename the channel after its category's slug or the prefix changes."""
        wanted = self.channel_name(category)
        current = (self.slack.conversations_info(channel=channel_id).get("channel") or {}).get("name")
        if current == wanted:
            return
        try:
            self.slack.conversations_rename(channel=channel_id, name=wanted)
            LOG.info("renamed #%s to #%s for %s", current, wanted, category.get("name"))
        except Exception as exc:
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
        for topic_id in sorted(self.link_only):
            try:
                with self.lock:
                    topic = self.topic_meta({"topic_id": topic_id})
                    category_id = topic.get("category_id")
                    channel_id = self.ensure_channel(self.category(category_id)) if category_id else None
                    if channel_id:
                        self.ensure_link(topic, channel_id)
            except Exception as exc:
                LOG.error("cannot link topic %d: %s", topic_id, exc)

    # -------------------------------------------------------- forum to Slack

    def post_url(self, topic: dict, post_number: int) -> str:
        return f"{self.forum.base_url}/t/{topic['slug']}/{topic['id']}/{post_number}"

    def title_link(self, topic: dict) -> str:
        """The topic's title in bold, linking to the topic."""
        return f"*<{self.forum.base_url}/t/{topic['slug']}/{topic['id']}|{slack_escape(topic['title'])}>*"

    def opening(
        self, topic: dict, first: dict | None, hint: str = TOPIC_HINT, mention: bool = True, codes: bool = False,
    ) -> dict:
        """A thread's parent: the title, linked, over the first post, under that
        post's author, so the conversation shows what the topic says without
        opening the thread, and ``hint`` after it in italics. With the first
        post gone, the title alone under the topic's opener."""
        heading = self.title_link(topic) + "\n"
        # A paragraph of its own, so it reads apart from the post above it.
        hint = f"\n\n_{hint}_"
        if gone(first):
            return {"text": self.title_link(topic) + hint, "username": topic.get("opener") or "somebody"}
        # The linked title is the link to the post.
        text = self.post_text(first, topic, mention=mention, heading=heading, link=False, codes=codes).rstrip() + hint
        return {"text": text, "username": first.get("username") or "somebody"}

    def first_post(self, topic: dict) -> dict | None:
        if not topic.get("first_post_id"):
            return None
        try:
            return self.forum.post(topic["first_post_id"])
        except Refused as exc:
            LOG.warning("topic %d opens without its first post: %s", topic["id"], exc)
            return None

    def ensure_thread(self, topic: dict, channel_id: str, first: dict | None = None) -> str:
        existing = self.state.thread_for(topic["id"])
        if existing and existing[0] == channel_id:
            return existing[1]
        if first is None:
            first = self.first_post(topic)
        result = self.slack.chat_postMessage(
            channel=channel_id, **self.opening(topic, first), unfurl_links=False, unfurl_media=False,
        )
        self.state.set_thread(topic["id"], channel_id, result["ts"])
        self.send_files(first, channel_id, result["ts"])
        return result["ts"]

    def ensure_link(self, topic: dict, channel_id: str) -> None:
        """A link-only topic's thread: its parent the title, linked, and nothing
        else of the bridge's. A thread holding copies is cut back to that."""
        text = f"{self.title_link(topic)}\n\n_{LINK_ONLY_HINT}_"
        marker = f"linked:{topic['id']}"
        existing = self.state.thread_for(topic["id"])
        if existing and existing[0] == channel_id:
            if self.state.cursor(marker) is not None:
                return
            for message in self.thread_messages(channel_id, existing[1]):
                ours = message.get("bot_id") or message.get("user") == self.bot_user_id
                if ours and message.get("ts") != existing[1]:
                    self.slack.chat_delete(channel=channel_id, ts=message["ts"])
            self.slack.chat_update(channel=channel_id, ts=existing[1], text=text)
            LOG.info("topic %d's thread in %s cut back to a link", topic["id"], channel_id)
        else:
            result = self.slack.chat_postMessage(
                channel=channel_id, text=text, username="qupa-cafe", unfurl_links=False, unfurl_media=False,
            )
            self.state.set_thread(topic["id"], channel_id, result["ts"])
            LOG.info("topic %d linked in %s", topic["id"], channel_id)
        self.state.set_cursor(1, marker)

    def thread_messages(self, channel_id: str, thread_ts: str) -> list[dict]:
        messages: list[dict] = []
        cursor = None
        while True:
            page = self.slack.conversations_replies(channel=channel_id, ts=thread_ts, cursor=cursor, limit=200)
            messages += page.get("messages") or []
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return messages

    def relay_post(self, post: dict) -> str:
        if post.get("post_type", POST_TYPE_REGULAR) != POST_TYPE_REGULAR:
            return "not a regular post"
        if gone(post):
            return "hidden or deleted"
        if self.state.origin_of(post["id"]) is not None:
            return "came from Slack"

        topic = self.topic_meta(post)
        if topic["archetype"] != "regular" or not topic.get("category_id"):
            return "not a public topic"
        category = self.category(topic["category_id"])
        channel_id = self.ensure_channel(category)
        if not channel_id:
            return "restricted category"
        if topic["id"] in self.link_only:
            self.ensure_link(topic, channel_id)
            return "a topic read on the forum: linked, not copied"

        # The first post is the thread's parent; a feed topic's waits for a
        # reply, which brings it.
        if (post.get("post_number") or 1) == 1:
            if self.is_feed(topic.get("opener") or ""):
                return "a feed topic with no reply yet"
            self.ensure_thread(topic, channel_id, first=post)
            return f"relayed to {channel_id} as its thread's parent"
        thread_ts = self.ensure_thread(topic, channel_id)
        self.send(post, topic, channel_id, thread_ts)
        return f"relayed to {channel_id}"

    def send(
        self,
        post: dict,
        topic: dict,
        channel_id: str,
        thread_ts: str,
        reopen: Callable[[], str] | None = None,
        mention: bool = True,
        codes: bool = False,
    ) -> None:
        """One post as a thread reply. ``reopen`` restarts a thread whose parent
        was deleted in Slack; ``mention`` is off in a DM; ``codes`` is ``redact``'s."""
        text = self.post_text(post, topic, mention=mention, codes=codes)

        def post_in(thread: str) -> dict:
            return self.slack.chat_postMessage(
                channel=channel_id, thread_ts=thread, text=text, username=post.get("username") or "somebody",
                unfurl_links=False, unfurl_media=False,
            )

        try:
            result = post_in(thread_ts)
        except Exception as exc:
            if "thread_not_found" not in str(exc) and "message_not_found" not in str(exc):
                raise
            if reopen is None:
                self.state.drop_thread(topic["id"])
                thread_ts = self.ensure_thread(topic, channel_id)
            else:
                thread_ts = reopen()
            result = post_in(thread_ts)
        if PLACEHOLDER.search(post.get("raw") or ""):
            self.state.watch_placeholder(post["id"], channel_id, result["ts"], thread_ts, topic, mention, codes)
            return
        self.send_files(post, channel_id, thread_ts)

    def fill_placeholders(self) -> int:
        """Each *Thinking…* copy whose post is now the answer, updated to it,
        with its files after it; one whose post is gone, deleted."""
        filled = 0
        for post_id, channel_id, ts, thread_ts, topic_id, title, slug, mention, codes, at in self.state.placeholders():
            try:
                post = self.forum.post(post_id)
            except Refused as exc:
                if exc.status not in (403, 404):
                    LOG.error("cannot read placeholder %d: %s", post_id, exc)
                    continue
                post = {"deleted_at": "gone"}
            if gone(post):
                try:
                    self.slack.chat_delete(channel=channel_id, ts=ts)
                except Exception as exc:
                    LOG.warning("cannot delete the copy of placeholder %d: %s", post_id, exc)
                self.state.drop_placeholder(post_id)
                LOG.info("placeholder %d was deleted, and so is its copy", post_id)
                continue
            if PLACEHOLDER.search(post.get("raw") or ""):
                if time.time() - at > PLACEHOLDER_WATCH_SECONDS:
                    self.state.drop_placeholder(post_id)
                continue
            topic = {"id": topic_id, "title": title, "slug": slug}
            self.slack.chat_update(
                channel=channel_id, ts=ts, text=self.post_text(post, topic, mention=bool(mention), codes=bool(codes)),
            )
            self.send_files(post, channel_id, thread_ts)
            self.state.drop_placeholder(post_id)
            filled += 1
            LOG.info("placeholder %d is now its answer in %s", post_id, channel_id)
        return filled

    def send_files(self, post: dict | None, channel_id: str, thread_ts: str) -> None:
        """A post's uploaded files, images and attachments, into its thread
        after it. Slack cannot fetch them from a forum that requires a login,
        so the bytes go. A failure costs only the files: the text is already
        in Slack, and relaying the post again would repeat it."""
        if gone(post):
            return
        raw = post["raw"] if "raw" in post else self.forum.post(post["id"]).get("raw", "")
        found = files_in(raw or "")
        if not found:
            return
        try:
            served = self.forum.lookup_uploads([short_url for _, short_url in found])
            files = []
            for name, short_url in found:
                if short_url not in served:
                    LOG.warning("post %s: the forum does not know %s", post.get("id"), short_url)
                    continue
                stored = posixpath.basename(short_url.removeprefix(UPLOAD_SCHEME))
                # The stored name's extension, which is the file's type: a
                # name such as "Fig. 2, arXiv:2610.10432v1" only looks as if
                # it had one, and Slack types a file by its name.
                extension = posixpath.splitext(stored)[1]
                if not name:
                    filename = stored
                elif name.lower().endswith(extension.lower()):
                    filename = name
                else:
                    filename = name + extension
                content = self.forum.download(served[short_url])
                files.append({"content": content, "filename": filename, "title": name or stored})
            if files:
                self.slack.files_upload_v2(channel=channel_id, thread_ts=thread_ts, file_uploads=files)
        except Exception as exc:
            LOG.error("post %s's files did not reach Slack: %s", post.get("id"), exc)

    def post_text(
        self, post: dict, topic: dict, mention: bool = True, heading: str = "", link: bool = True, codes: bool = False,
    ) -> str:
        """A post as Slack text: an agent's starts by mentioning its researcher,
        then ``heading``, the body, and with ``link`` a link to the post."""
        author = post.get("username") or "somebody"
        url = self.post_url(topic, post.get("post_number") or 1)
        if "raw" not in post:
            post = {**post, "raw": self.forum.post(post["id"]).get("raw", "")}
        raw = post.get("raw") or ""
        body = PLACEHOLDER_SLACK_TEXT if PLACEHOLDER.search(raw) else to_slack(raw, url, codes)
        lead = ""
        if mention and self.is_agent(author):
            researcher = self.researcher_for(author)
            slack_id = self.slack_id_for(researcher) if researcher else None
            if slack_id:
                lead = f"_<@{slack_id}>, your agent posted:_\n"
        # The bridge's own words are in italics, apart from what the author wrote.
        return f"{lead}{heading}{body}\n_<{url}|View this post>_" if link else f"{lead}{heading}{body}"

    def agent_of(self, username: str) -> str | None:
        """``<username>-agent``, if it is an agent."""
        candidate = f"{username}{AGENT_SUFFIX}".lower()
        return candidate if self.is_agent(candidate) else None

    def humans_in(self, topic: dict) -> list[str]:
        """Lowercase usernames of the people in a private message, groups
        expanded, without agents, feeds or the forum's own accounts."""
        details = topic.get("details") or {}
        people = {
            (u.get("username") or "").lower()
            for u in details.get("allowed_users") or []
            if u.get("username") and (u.get("id") is None or u["id"] > 0)
        }
        for group in details.get("allowed_groups") or []:
            if group.get("name"):
                people |= self.members(group["name"])
        return sorted(p for p in people if p and not self.is_agent(p) and not self.is_feed(p))

    def ensure_agents(self, topic: dict) -> list[str]:
        """Add the agent of everybody in a private message who is missing one.
        Not to a message the forum's own accounts sent, such as a credential."""
        opener = (topic.get("details") or {}).get("created_by") or {}
        if (opener.get("id") is not None and opener["id"] <= 0) or self.is_feed(opener.get("username") or ""):
            return []
        present = {(u.get("username") or "").lower() for u in (topic.get("details") or {}).get("allowed_users") or []}
        added = []
        for person in self.humans_in(topic):
            agent = self.agent_of(person)
            if agent and agent not in present:
                try:
                    self.forum.add_to_message(topic["id"], agent)
                    added.append(agent)
                except Refused as exc:
                    LOG.error("cannot add %s to message %d: %s", agent, topic["id"], exc)
        return added

    def one_to_one(self, topic: dict) -> tuple[str, bool] | None:
        """(person, from the forum) for a private message between one person
        and their own agent, or from the forum's own accounts to one person:
        the private messages carried to Slack. None for any other."""
        details = topic.get("details") or {}
        if details.get("allowed_groups"):
            return None
        people = self.humans_in(topic)
        if len(people) != 1:
            return None
        person = people[0]
        others = [u for u in details.get("allowed_users") or [] if (u.get("username") or "").lower() != person]
        from_forum = False
        for user in others:
            if user.get("id") is not None and user["id"] <= 0:
                from_forum = True
            elif (user.get("username") or "").lower() != self.agent_of(person):
                return None
        return person, from_forum

    def dm_with(self, slack_id: str) -> str:
        cached = self._conversations.get(slack_id)
        if not cached:
            channel = self.slack.conversations_open(users=slack_id)["channel"]["id"]
            cached = self._conversations[slack_id] = Cached(channel)
        return cached.value

    def open_thread(self, meta: dict, slack_id: str, first: dict | None = None, codes: bool = False) -> tuple[str, str]:
        """A new thread for a private message in the bot's DM with its person,
        its parent the message's first post."""
        channel_id = self.dm_with(slack_id)
        if first is None:
            first = self.first_post(meta)
        opening = self.opening(meta, first, hint=PRIVATE_HINT, mention=False, codes=codes)
        result = self.slack.chat_postMessage(channel=channel_id, **opening, unfurl_links=False, unfurl_media=False)
        self.state.set_thread(meta["id"], channel_id, result["ts"])
        self.send_files(first, channel_id, result["ts"])
        return channel_id, result["ts"]

    def relay_private(self, post: dict) -> str:
        """One private-message post into the bot's DM with its person, if the
        message is between them and their agent or from the forum to them."""
        if post.get("post_type", POST_TYPE_REGULAR) != POST_TYPE_REGULAR:
            return "not a regular post"
        if gone(post):
            return "hidden or deleted"

        # Read afresh: who is in a private message changes.
        topic = self.forum.topic(post["topic_id"])
        if topic.get("archetype") != "private_message":
            return "not a private message"
        topic["id"] = post["topic_id"]
        added = self.ensure_agents(topic)
        also = f"; added {', '.join(added)}" if added else ""

        found = self.one_to_one(topic)
        if not found:
            return "not between one person and their agent" + also
        person, codes = found
        slack_id = self.slack_id_for(person)
        if not slack_id:
            return f"{person} is not on Slack" + also

        meta = {
            "id": post["topic_id"],
            "title": topic.get("title") or post.get("topic_title") or f"Message {post['topic_id']}",
            "slug": topic.get("slug") or post.get("topic_slug") or "message",
            "opener": ((topic.get("details") or {}).get("created_by") or {}).get("username") or "",
            "first_post_id": next(iter((topic.get("post_stream") or {}).get("stream") or []), None),
        }
        threaded = self.state.thread_for(meta["id"])
        if threaded and self.state.origin_of(post["id"]) == threaded[0]:
            return "came from this conversation" + also
        if not threaded:
            first = post if (post.get("post_number") or 1) == 1 else None
            channel_id, thread_ts = self.open_thread(meta, slack_id, first=first, codes=codes)
            if first:
                return f"relayed to {channel_id} as its thread's parent" + also
        else:
            channel_id, thread_ts = threaded

        def reopen() -> str:
            return self.open_thread(meta, slack_id, codes=codes)[1]

        self.send(post, meta, channel_id, thread_ts, reopen=reopen, mention=False, codes=codes)
        return f"relayed to {channel_id}" + also

    def new_posts(self, private: bool = False) -> list[dict]:
        """Every post after the cursor, oldest first; ``first_run`` decides where
        a missing cursor starts."""
        name = "pm_cursor" if private else "cursor"
        cursor = self.state.cursor(name)
        page = self.forum.latest_posts(private=private)
        if not page:
            if cursor is None:
                self.state.set_cursor(0, name)
            return []
        if cursor is None and self.first_run == "newest":
            self.state.set_cursor(max(p["id"] for p in page), name)
            LOG.info("first run: starting %s after post %d", "private messages" if private else "topics", self.state.cursor(name))
            return []
        if cursor is None:
            LOG.info("first run: relaying %s from the forum's first post", "private messages" if private else "topics")
            cursor = 0

        found = [p for p in page if p["id"] > cursor]
        pages = 1
        while page and min(p["id"] for p in page) > cursor + 1 and pages < MAX_PAGES_PER_POLL:
            page = self.forum.latest_posts(before=min(p["id"] for p in page), private=private)
            found.extend(p for p in page if p["id"] > cursor)
            pages += 1
        found.sort(key=lambda p: p["id"])
        if page and found and min(p["id"] for p in page) > cursor + 1:
            # A long outage: the oldest of the gap stays on the forum.
            LOG.warning(
                "posts %d to %d are older than %d pages and were not relayed",
                cursor + 1, found[0]["id"] - 1, MAX_PAGES_PER_POLL,
            )
        return found

    def poll(self) -> int:
        self.sync_categories()
        relayed = self.poll_stream(private=False)
        relayed += self.poll_stream(private=True)
        with self.lock:
            try:
                self.fill_placeholders()
            except Transient as exc:
                LOG.warning("placeholders wait for the next poll: %s", exc)
        return relayed

    def poll_stream(self, private: bool) -> int:
        name = "pm_cursor" if private else "cursor"
        relay = self.relay_private if private else self.relay_post
        relayed = 0
        for post in self.new_posts(private=private):
            with self.lock:
                try:
                    outcome = relay(post)
                except Transient as exc:
                    LOG.warning("stopping at post %d until the next poll: %s", post["id"], exc)
                    return relayed
                except Exception as exc:
                    # Skipped rather than retried forever.
                    LOG.error("could not relay post %d: %s", post["id"], exc)
                    outcome = "failed"
                self.state.set_cursor(post["id"], name)
            if outcome.startswith("relayed"):
                relayed += 1
            LOG.info("%s %d by %s: %s", "private post" if private else "post", post["id"], post.get("username"), outcome)
        return relayed

    # -------------------------------------------------------- Slack to the forum

    def handle_message(self, event: dict) -> str:
        """One Slack message event. Returns what happened, for the log."""
        # It names no user of its own: who wrote the message is inside it.
        if event.get("subtype") == "message_changed":
            return self.handle_edit(event)
        if event.get("bot_id") or event.get("user") in (None, self.bot_user_id):
            return "not from a person"
        if event.get("channel_type") in ("im", "mpim"):
            return self.handle_conversation(event)
        subtype = event.get("subtype")
        channel_id = event.get("channel") or ""
        user = event["user"]
        if self.state.category_for(channel_id) is None:
            return "not a bridged channel"

        thread_ts = event.get("thread_ts")
        top_level = not thread_ts or thread_ts == event.get("ts")
        if subtype and (top_level or subtype not in ("thread_broadcast", "file_share")):
            return f"ignored {subtype}"

        def tell(text: str) -> None:
            self.ephemeral(channel_id, user, None if top_level else thread_ts, text)

        # Anyone without an account is answered with an invitation, wherever they wrote.
        try:
            account, email = self.forum_user_for(user)
        except Unusable as exc:
            tell("Your Slack address belongs to a forum account that cannot post from here, so nothing "
                 "was posted. Ask a forum admin.")
            return f"refused: {exc}"
        if not account:
            outcome = self.invite(user, email, tell)
            if outcome in INVITED:
                self.announce_invitations(channel_id, event.get("ts") if top_level else thread_ts, user)
            return outcome

        if top_level:
            tell("That message stays in Slack. To post to the forum, reply in a topic's thread; "
                 "to start a topic, open it on the forum and its thread appears here.")
            return "top-level message, not relayed"

        topic_id = self.state.topic_for(channel_id, thread_ts)
        if topic_id is None:
            tell("This thread is not a forum topic, so nothing was posted.")
            return "thread is not a topic"
        if topic_id in self.link_only:
            tell(f"This topic is read and answered on the forum, so nothing was posted: {self.forum.base_url}/t/{topic_id}")
            return "a topic read on the forum, not posted"

        return self.post_as(account, topic_id, event, tell)

    def handle_edit(self, event: dict) -> str:
        """A message written in Slack, edited there: its post is edited as its
        writer. Nobody but the bot can edit the bot's own messages, and forum
        edits are not carried back."""
        message = event.get("message") or {}
        user = message.get("user")
        if message.get("bot_id") or user in (None, self.bot_user_id):
            return "not an edit by a person"
        channel_id = event.get("channel") or ""
        thread_ts = message.get("thread_ts") or message.get("ts")
        with self.lock:
            written = self.state.written_at(channel_id, message.get("ts") or "")
            if written is None:
                return "an edit to a message that is not on the forum"
            post_id, stored, files = written
            raw = with_files(from_slack(message.get("text") or "", self.mention_for), files)
            # Slack reports a link preview arriving as an edit too.
            if same_text(raw, stored):
                return "unchanged"

            def tell(text: str) -> None:
                if event.get("channel_type") == "im":
                    try:
                        self.slack.chat_postMessage(channel=channel_id, text=text, thread_ts=thread_ts)
                    except Exception as exc:
                        LOG.warning("cannot answer %s: %s", user, exc)
                else:
                    self.ephemeral(channel_id, user, thread_ts, text)

            if not raw:
                tell("The forum cannot hold an empty post, so it was not changed. To remove it, delete it on the forum.")
                return "an empty edit"
            try:
                author = self.forum.post(post_id).get("username") or ""
                self.forum.edit(post_id, raw, as_user=author)
            except Refused as exc:
                reason = forum_error(exc)
                tell(f"The forum did not accept that edit: {reason}")
                return f"refused: {reason}"
            except Transient as exc:
                tell("The forum is not answering just now, so that edit was not made. Edit the message again in a minute.")
                return f"transient: {exc}"
            self.state.set_slack_raw(post_id, raw)
        return f"edited post {post_id} as {author}"

    def slack_file(self, file: dict) -> bytes:
        """A Slack file's bytes, read with the bot token, which needs
        ``files:read``. Raises ``CannotCarry`` with a reason for its writer."""
        if file.get("mode") in ("hidden_by_limit", "tombstone", "external"):
            raise CannotCarry("Slack does not let the app read it")
        url = file.get("url_private_download") or file.get("url_private") or ""
        if not url.startswith(SLACK_FILES_URL):
            raise CannotCarry("Slack gave no address for it")
        if (file.get("size") or 0) > MAX_FILE_BYTES:
            raise CannotCarry("it is too large")
        req = urllib.request.Request(url)
        # Not carried through a redirect, so the token reaches Slack's file host only.
        req.add_unredirected_header("Authorization", f"Bearer {self.slack.token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read(MAX_FILE_BYTES + 1)
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        except (urllib.error.URLError, OSError) as exc:
            raise CannotCarry(f"Slack did not hand it over ({exc})") from exc
        if len(content) > MAX_FILE_BYTES:
            raise CannotCarry("it is too large")
        # Without files:read, Slack answers with its sign-in page.
        if content_type == "text/html" and file.get("mimetype") != "text/html":
            raise CannotCarry("the app may not read Slack's files; a workspace admin must reinstall it")
        return content

    def carry_files(self, event: dict, as_user: str, tell: Callable[[str], None]) -> str:
        """The message's files, uploaded to the forum as their writer, as the
        markdown that attaches them. Any that cannot go are named to the writer."""
        lines: list[str] = []
        missed: list[str] = []
        files = event.get("files") or []
        for file in files[:MAX_FILES_PER_POST]:
            name = file.get("name") or file.get("title") or "file"
            try:
                uploaded = self.forum.upload(name, self.slack_file(file), as_user)
            except CannotCarry as exc:
                missed.append(f"{name}: {exc}")
                continue
            except Refused as exc:
                missed.append(f"{name}: {forum_error(exc)}")
                continue
            except Transient:
                missed.append(f"{name}: the forum is not answering just now")
                continue
            lines.append(upload_markdown(uploaded, name))
        missed += [f"{f.get('name') or 'file'}: more than {MAX_FILES_PER_POST} files" for f in files[MAX_FILES_PER_POST:]]
        if missed:
            tell("Not attached on the forum: " + "; ".join(missed) + ". Attach it there instead.")
        return "\n".join(lines)

    def post_as(self, account: dict, topic_id: int, event: dict, tell: Callable[[str], None]) -> str:
        files = self.carry_files(event, account["username"], tell)
        raw = with_files(from_slack(event.get("text") or "", self.mention_for), files)
        if not raw:
            return "empty"
        with self.lock:
            try:
                created = self.forum.reply(topic_id, raw, as_user=account["username"])
            except Refused as exc:
                reason = forum_error(exc)
                tell(f"The forum did not accept that reply: {reason}")
                return f"refused: {reason}"
            except Transient as exc:
                tell("The forum is not answering just now, so that was not posted. Try again in a minute.")
                return f"transient: {exc}"
            post_id = created.get("id")
            if post_id:
                self.state.record_from_slack(post_id, event.get("channel") or "", event.get("ts") or "", raw, files)
        return f"posted as {account['username']} (post {post_id})"

    def handle_conversation(self, event: dict) -> str:
        """In a DM with the bot, a top-level message opens a private message
        with the writer's agent, and a thread reply continues one. A group DM
        is not carried."""
        subtype = event.get("subtype")
        channel_id = event.get("channel") or ""
        user = event["user"]
        top_level = not event.get("thread_ts") or event.get("thread_ts") == event.get("ts")
        thread_ts = event.get("ts") if top_level else event.get("thread_ts")

        if event.get("channel_type") == "mpim":
            if top_level and not subtype:
                self.ephemeral(
                    channel_id, user, None,
                    "Group conversations are not carried to the forum. To talk to your agent, message this app "
                    "directly; to talk with others, write on the forum.",
                )
            return "group DM, not relayed"

        def tell(text: str) -> None:
            try:
                self.slack.chat_postMessage(channel=channel_id, text=text, thread_ts=thread_ts)
            except Exception as exc:
                LOG.warning("cannot answer %s: %s", user, exc)

        if subtype not in (None, "thread_broadcast", "file_share"):
            return f"ignored {subtype}"

        try:
            account, email = self.forum_user_for(user)
        except Unusable as exc:
            tell("Your Slack address belongs to a forum account that cannot post from here, so nothing was posted.")
            return f"refused: {exc}"
        if not account:
            return self.invite(user, email, tell)

        if not top_level:
            topic_id = self.state.topic_for(channel_id, thread_ts)
            if topic_id is None:
                tell("This thread is not a private message on the forum, so nothing was posted.")
                return "thread is not a private message"
            return self.post_as(account, topic_id, event, tell)
        return self.open_message(account, event, tell)

    def open_message(self, account: dict, event: dict, tell: Callable[[str], None]) -> str:
        """A private message from the writer to their own agent."""
        sender = account["username"]
        agent = self.agent_of(sender)
        if not agent:
            tell("You have no agent on the forum yet, so there is nobody to send this to.")
            return "no agent to send to"

        text = from_slack(event.get("text") or "", self.mention_for)
        files = self.carry_files(event, sender, tell)
        raw = with_files(text, files)
        if not raw:
            return "empty"
        names = ", ".join(f.get("name") or "file" for f in event.get("files") or [])
        title = message_title(text or f"Files: {names}")
        with self.lock:
            try:
                try:
                    created = self.forum.private_message(title, raw, [agent], as_user=sender)
                except Refused as exc:
                    # A duplicate title, from two messages opening alike.
                    if "title" not in forum_error(exc).lower():
                        raise
                    stamp = time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(event.get("ts") or time.time())))
                    created = self.forum.private_message(f"{title} ({stamp})", raw, [agent], as_user=sender)
            except Refused as exc:
                reason = forum_error(exc)
                tell(f"The forum did not accept that message: {reason}")
                return f"refused: {reason}"
            except Transient as exc:
                tell("The forum is not answering just now, so that was not posted. Try again in a minute.")
                return f"transient: {exc}"
            topic_id = created.get("topic_id")
            if created.get("id"):
                self.state.record_from_slack(created["id"], event["channel"], event.get("ts") or "", raw, files)
            if topic_id:
                self.state.set_thread(topic_id, event["channel"], event["ts"])

        url = f"{self.forum.base_url}/t/{created.get('topic_slug') or 'message'}/{topic_id}"
        tell(
            f"Sent on the forum as <{url}|a private message> to {agent}. Replies in this thread continue it; "
            "a new message outside the thread starts another."
        )
        return f"opened message {topic_id} as {sender} to {agent}"

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

    def invite(self, slack_user: str, email: str | None, tell: Callable[[str], None]) -> str:
        """Answer somebody with no account with an invitation link bound to
        their Slack address, which only signing up with Slack redeems."""
        closed = "Nothing was posted: the forum is invite-only, and you have no account on it yet."
        if not email:
            tell(f"{closed} Slack has no email address for you to invite, so ask a forum admin for an invitation.")
            return "no account, no email"

        # Guests are not invited.
        profile = self.slack.users_info(user=slack_user).get("user") or {}
        if profile.get("is_restricted") or profile.get("is_ultra_restricted"):
            tell(f"{closed} Guests in this workspace are not invited automatically: ask a forum admin for an invitation.")
            return "no account, a guest"

        now = time.time()
        held = self.state.invite_for(email, now)
        if held:
            link, expires_at = held
            outcome = "no account, invited already"
        else:
            expires_at = now + INVITE_DAYS * 86400
            try:
                link = self.forum.invite(email, expires_at).get("link")
            except (Refused, Transient) as exc:
                LOG.error("cannot invite %s: %s", email, exc)
                tell(f"{closed} The forum would not make you an invitation just now: ask a forum admin for one.")
                return f"no account, invite failed: {exc}"
            if not link:
                LOG.error("the forum's invitation for %s has no link", email)
                tell(f"{closed} The forum would not make you an invitation just now: ask a forum admin for one.")
                return "no account, invite failed: no link"
            self.state.record_invite(email, link, expires_at, now)
            outcome = "no account, invited"

        until = time.gmtime(expires_at)
        tell(
            f"{closed} Here is your invitation, which only your Slack account can use, until "
            f"{until.tm_mday} {time.strftime('%B', until)}: {link}\n"
            "Open it and sign up with Slack. After that, a reply in a topic's thread here, or a message "
            "to this app, reaches the forum under your name."
        )
        return outcome

    def announce_invitations(self, channel: str, thread_ts: str | None, slack_user: str) -> None:
        """In public, in the thread of what a newcomer wrote: that they were
        answered with an invitation, and that anyone else can be."""
        try:
            self.slack.chat_postMessage(
                channel=channel, thread_ts=thread_ts, unfurl_links=False,
                text=(
                    f"<@{slack_user}> your invitation to the forum is in a message from this app that only you can see.\n\n"
                    "For anyone else who wants to join: the forum is invite-only, and any member of this workspace "
                    f"can get an invitation. Write a message in one of the `{self.prefix}` channels, or message this "
                    "app directly, and it answers you privately with an invitation link. Open it and sign up with "
                    "Slack, and your forum account is made from your Slack account."
                ),
            )
        except Exception as exc:
            LOG.warning("cannot announce %s's invitation in %s: %s", slack_user, channel, exc)

    def ephemeral(self, channel: str, user: str, thread_ts: str | None, text: str) -> None:
        try:
            self.slack.chat_postEphemeral(channel=channel, user=user, text=text, thread_ts=thread_ts)
        except Exception as exc:
            LOG.warning("cannot tell %s: %s", user, exc)


def forum_error(exc: Refused) -> str:
    try:
        errors = json.loads(exc.detail).get("errors")
        if errors:
            return " ".join(errors)
    except (ValueError, AttributeError):
        pass
    return f"HTTP {exc.status}"


# ---------------------------------------------------------------------- running it


def connect_slack(bridge: Bridge) -> Any:
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    client = SocketModeClient(app_token=APP_TOKEN, web_client=bridge.slack)

    def listener(socket: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledged first: Slack redelivers after three seconds.
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


# The scopes providers/slack/manifest.yml grants.
REQUIRED_SCOPES = {
    "channels:history", "channels:join", "channels:manage", "channels:read",
    "chat:write", "chat:write.customize", "files:read", "files:write", "im:history", "im:write",
    "mpim:history", "mpim:read", "mpim:write", "users:read", "users:read.email",
}


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
        granted = {s.strip() for s in (auth.headers.get("x-oauth-scopes") or "").split(",") if s.strip()}
        missing = sorted(REQUIRED_SCOPES - granted)
        if missing:
            print(f"slack   FAILED: the token lacks {', '.join(missing)}; reinstall the app from its manifest")
            ok = False
        else:
            print(f"slack   ok, the token has all {len(REQUIRED_SCOPES)} scopes the bridge uses")
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
