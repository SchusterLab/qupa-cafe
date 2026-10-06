#!/usr/bin/env python3
"""Post every preprint arXiv announces in quant-ph to the forum, one topic each.

Run every ten minutes by qupa-arxiv-feed.timer. Each topic carries the paper's
id as its ``external_id``, which the forum holds unique, so a repeated or
interrupted run is refused on what it already posted; the state file only
saves those refusals. Before a build is posted, every earlier preprint topic
nobody has replied to is deleted. docs/05-forum-config.md 5.10 has the account
and its key.

Usage::

    python3 arxiv-feed.py                     # post what is new
    python3 arxiv-feed.py --dry-run           # print it instead, change nothing
    python3 arxiv-feed.py --feed saved.xml    # read a saved feed rather than arXiv
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

LOG = logging.getLogger("arxiv-feed")

FEED_URL = os.environ.get("ARXIV_FEED_URL", "https://rss.arxiv.org/rss/quant-ph")
FORUM_URL = os.environ.get("DISCOURSE_URL", "http://127.0.0.1")
API_KEY = os.environ.get("DISCOURSE_API_KEY", "")
API_USERNAME = os.environ.get("DISCOURSE_API_USERNAME", "arxiv-bot")
CATEGORY_ID = os.environ.get("ARXIV_CATEGORY_ID", "")
# An address arXiv can write to about this client, sent in the User-Agent.
CONTACT = os.environ.get("ARXIV_CONTACT", "")
STATE_PATH = Path(os.environ.get("ARXIV_STATE", "/var/lib/qupa-arxiv-feed/state.json"))
POST_INTERVAL = float(os.environ.get("ARXIV_POST_INTERVAL", "2"))

# `replace` and `replace-cross` are new versions of papers already posted.
POSTED_TYPES = ("new", "cross")

# Discourse's max_topic_title_length, which this forum leaves at its default.
MAX_TITLE_LENGTH = 255

# Keeps a collaboration paper under max_post_length.
MAX_AUTHORS = 25

TEMPLATE_PATH = Path(
    os.environ.get("ARXIV_TEMPLATE")
    or Path(__file__).resolve().parent.parent / "content" / "arxiv-topic.md"
)

NS = {
    "arxiv": "http://arxiv.org/schemas/atom",
    "dc": "http://purl.org/dc/elements/1.1/",
}


# ---------------------------------------------------------------------- the feed


@dataclass(frozen=True)
class Paper:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    announce_type: str
    primary_category: str
    announced: str

    @property
    def external_id(self) -> str:
        """``arxiv-2610_00046``: Discourse allows only ``[\\w-]`` here."""
        return "arxiv-" + re.sub(r"[^\w-]", "_", self.arxiv_id)

    @property
    def abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self.arxiv_id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.arxiv_id}"


# TeX accent commands as they appear in arXiv author names, mapped to the
# combining character each one stands for. `Fern\'andez` becomes `Fernández`.
TEX_ACCENTS = {
    "'": "́", "`": "̀", "^": "̂", '"': "̈", "~": "̃",
    "=": "̄", ".": "̇", "u": "̆", "v": "̌", "H": "̋",
    "c": "̧", "k": "̨", "r": "̊",
}
TEX_LETTERS = {
    "ss": "ß", "o": "ø", "O": "Ø", "l": "ł", "L": "Ł", "ae": "æ", "AE": "Æ",
    "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å", "i": "ı", "j": "ȷ",
}
_ACCENT = re.compile(r"\{?\\([`'^\"~=.]|[uvHckr](?=[\s{]))\s*\{?([A-Za-z\\]i?)\}?\}?")
# TeX swallows one space or empty group after a control word.
_LETTER = re.compile(r"\{?\\(ss|ae|AE|oe|OE|aa|AA|o|O|l|L|i|j)(?![A-Za-z])(?:\{\}| )?\}?")


def detex(text: str) -> str:
    """Turn TeX accents in author names into letters (abstracts keep their
    TeX, which the forum renders). Accents go first, for an accented dotless i."""
    def accent(match: re.Match) -> str:
        letter = match.group(2)
        letter = {"\\i": "i", "\\j": "j"}.get(letter, letter)
        return unicodedata.normalize("NFC", letter + TEX_ACCENTS[match.group(1)])

    text = _ACCENT.sub(accent, text)
    text = _LETTER.sub(lambda m: TEX_LETTERS[m.group(1)], text)
    return re.sub(r"[{}]", "", text)


def squash(text: str | None) -> str:
    """Collapse whitespace; a newline would render as a line break on the forum."""
    return " ".join((text or "").split())


def parse_feed(xml_text: str) -> tuple[str, list[Paper]]:
    """The feed's build stamp, and every posted-type paper in it, in feed order."""
    channel = ET.fromstring(xml_text).find("channel")
    if channel is None:
        raise ValueError("not an RSS feed: no <channel>")

    build = squash(channel.findtext("lastBuildDate"))
    papers: list[Paper] = []

    for item in channel.findall("item"):
        announce_type = squash(item.findtext("arxiv:announce_type", namespaces=NS))
        if announce_type not in POSTED_TYPES:
            continue

        link = squash(item.findtext("link"))
        match = re.search(r"/abs/(.+?)(?:v\d+)?$", link)
        if not match:
            LOG.warning("skipping an item with no arXiv id in its link: %r", link)
            continue

        description = item.findtext("description") or ""
        _, found, abstract = description.partition("Abstract:")
        categories = [squash(c.text) for c in item.findall("category") if squash(c.text)]
        pub_date = squash(item.findtext("pubDate"))
        try:
            day = parsedate_to_datetime(pub_date)
            announced = f"{day.day} {day:%B %Y}"
        except (TypeError, ValueError):
            announced = pub_date

        authors = [
            name for name in (squash(detex(n)) for n in re.split(r",\s*", item.findtext("dc:creator", namespaces=NS) or ""))
            if name
        ]

        papers.append(
            Paper(
                arxiv_id=match.group(1),
                title=squash(item.findtext("title")),
                authors=authors,
                abstract=squash(abstract if found else description),
                announce_type=announce_type,
                primary_category=categories[0] if categories else "quant-ph",
                announced=announced,
            )
        )

    return build, papers


# ---------------------------------------------------------------------- a topic


def topic_title(paper: Paper) -> str:
    """The title with its arXiv id, so it is never too short or a duplicate."""
    suffix = f" (arXiv:{paper.arxiv_id})"
    room = MAX_TITLE_LENGTH - len(suffix)
    title = paper.title if len(paper.title) <= room else paper.title[: room - 1].rstrip() + "…"
    return title + suffix


# The id topic_title puts at the end, which marks a topic as one of the feed's.
TITLE_ID = re.compile(r"\(arXiv:([^()\s]+)\)$")


def topic_body(paper: Paper, template: str) -> str:
    """Fill the template in one pass, so a placeholder-like abstract is copied."""
    authors = paper.authors[:MAX_AUTHORS]
    names = ", ".join(authors)
    if len(paper.authors) > MAX_AUTHORS:
        names += f", and {len(paper.authors) - MAX_AUTHORS} more"

    if paper.announce_type == "cross":
        announcement = f"Cross-listed to quant-ph from {paper.primary_category} on {paper.announced}"
    else:
        announcement = f"Announced in {paper.primary_category} on {paper.announced}"

    values = {
        "AUTHORS": names or "Authors not listed",
        "ABSTRACT": paper.abstract,
        "ANNOUNCEMENT": announcement,
        "ARXIV_ID": paper.arxiv_id,
        "ABS_URL": paper.abs_url,
        "PDF_URL": paper.pdf_url,
    }
    return re.sub(r"%([A-Z_]+)%", lambda m: values.get(m.group(1), m.group(0)), template).strip()


# ---------------------------------------------------------------------- the forum


class Transient(RuntimeError):
    """The forum could not take the post now; the next run resumes."""


class Forum:
    def __init__(self, base_url: str, api_key: str, username: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        req.add_header("Api-Key", self.api_key)
        req.add_header("Api-Username", self.username)
        req.add_header("Accept", "application/json")
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

        # 429 is a queue: the API limiter is one bucket the agents share.
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 429 and attempt < 4:
                    wait = int(exc.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
                    LOG.info("rate limited on %s; waiting %ss", path, wait)
                    time.sleep(wait)
                    continue
                if exc.code == 429 or exc.code >= 500:
                    raise Transient(f"{method} {path} -> {exc.code}: {detail}") from exc
                raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                raise Transient(f"{method} {path} failed: {exc.reason}") from exc
        raise Transient(f"{method} {path} -> gave up after five attempts")

    def topic_exists(self, external_id: str) -> bool:
        try:
            self._request("GET", f"/t/external_id/{urllib.parse.quote(external_id)}.json")
            return True
        except RuntimeError:
            return False

    def category_topics(self, category: str) -> list[dict]:
        """Every topic the category's page lists, read in full before any change."""
        topics: list[dict] = []
        for page in range(1000):
            listed = self._request("GET", f"/c/{category}/l/latest.json?page={page}").get("topic_list", {})
            topics.extend(listed.get("topics", []))
            if not listed.get("more_topics_url") or not listed.get("topics"):
                return topics
        raise RuntimeError(f"category {category} lists more than 1000 pages")

    def delete_topic(self, topic_id: int) -> None:
        self._request("DELETE", f"/t/{topic_id}.json")

    def create_topic(self, title: str, raw: str, category: str, external_id: str) -> str:
        """Open one topic. Returns "created" or "exists"; raises on anything else."""
        try:
            self._request(
                "POST",
                "/posts.json",
                {"title": title, "raw": raw, "category": category, "external_id": external_id},
            )
            return "created"
        except Transient:
            raise
        except RuntimeError:
            # A refusal is a duplicate if the topic is there.
            if self.topic_exists(external_id):
                return "exists"
            raise


# ---------------------------------------------------------------------- one run


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOG.warning("ignoring unreadable state %s: %s", path, exc)
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def fetch_feed(url: str, state: dict) -> tuple[str | None, dict]:
    """The feed's text, or None if a conditional request says it is unchanged."""
    req = urllib.request.Request(url)
    contact = f"; mailto:{CONTACT}" if CONTACT else ""
    req.add_header("User-Agent", f"qupa-cafe-arxiv-feed (+{FORUM_URL}{contact})")
    if state.get("etag"):
        req.add_header("If-None-Match", state["etag"])
    if state.get("last_modified"):
        req.add_header("If-Modified-Since", state["last_modified"])

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            validators = {
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
            }
            return resp.read().decode("utf-8"), {k: v for k, v in validators.items() if v}
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, {}
        raise


def unreplied_preprints(topics: list[dict], keep: set[str]) -> list[dict]:
    """The feed's topics that are only their first post, minus the ids in
    ``keep``. A pinned topic, or one without the id in its title, is not the
    feed's."""
    return [
        topic for topic in topics
        if (match := TITLE_ID.search(topic.get("title", "")))
        and match.group(1) not in keep
        and topic.get("posts_count") == 1
        and not topic.get("pinned")
    ]


def prune(forum: Forum, category: str, papers: list[Paper], dry_run: bool) -> tuple[int, int]:
    """Delete the category's unreplied preprints, except this build's own, so
    a resumed build keeps what it already posted. Returns (deleted, refused)."""
    candidates = unreplied_preprints(forum.category_topics(category), {p.arxiv_id for p in papers})
    deleted = refused = 0

    for topic in candidates:
        if dry_run:
            print(f"--- would delete topic {topic['id']}: {topic['title']}")
            deleted += 1
            continue
        try:
            forum.delete_topic(topic["id"])
        except Transient:
            raise
        except RuntimeError as exc:
            LOG.error("could not delete topic %s: %s", topic["id"], exc)
            refused += 1
            continue
        deleted += 1
        LOG.info("deleted unreplied topic %s: %s", topic["id"], topic["title"])
        time.sleep(POST_INTERVAL)

    return deleted, refused


def post_papers(forum: Forum | None, papers: list[Paper], template: str, category: str) -> dict[str, int]:
    """Open a topic per paper, or print them with ``forum=None``. A refused
    paper is logged and passed over; a transient failure stops the run."""
    counts = {"created": 0, "exists": 0, "refused": 0}

    for paper in papers:
        title, raw = topic_title(paper), topic_body(paper, template)
        if forum is None:
            print(f"--- {paper.external_id}\n{title}\n\n{raw}\n")
            counts["created"] += 1
            continue

        try:
            outcome = forum.create_topic(title, raw, category, paper.external_id)
        except Transient:
            raise
        except RuntimeError as exc:
            LOG.error("refused %s: %s", paper.arxiv_id, exc)
            counts["refused"] += 1
            continue

        counts[outcome] += 1
        if outcome == "created":
            LOG.info("posted %s", paper.arxiv_id)
            time.sleep(POST_INTERVAL)

    return counts


def run(feed_file: str | None, dry_run: bool) -> int:
    state = {} if dry_run else load_state(STATE_PATH)

    if feed_file:
        xml_text, validators = Path(feed_file).read_text(encoding="utf-8"), {}
    else:
        xml_text, validators = fetch_feed(FEED_URL, state)
        if xml_text is None:
            LOG.info("feed unchanged since the last fetch")
            return 0

    build, papers = parse_feed(xml_text)
    if build and build == state.get("posted_build"):
        LOG.info("build %s was already posted in full", build)
        if validators and not dry_run:
            save_state(STATE_PATH, {**state, **validators})
        return 0

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    forum = None if dry_run else Forum(FORUM_URL, API_KEY, API_USERNAME)

    # A dry run with a key reads the category to show what would go.
    pruner = forum or (Forum(FORUM_URL, API_KEY, API_USERNAME) if API_KEY and CATEGORY_ID else None)
    deleted = prune_refused = 0
    try:
        if pruner and papers:
            try:
                deleted, prune_refused = prune(pruner, CATEGORY_ID, papers, dry_run)
            except Transient:
                raise
            except RuntimeError as exc:
                # A key without the scopes to list or delete must not stop the feed.
                LOG.error("could not read the category to delete unreplied topics: %s", exc)
                prune_refused = 1
        counts = post_papers(forum, papers, template, CATEGORY_ID)
    except Transient as exc:
        LOG.error("stopping: %s. The next run resumes from here.", exc)
        return 1

    LOG.info(
        "build %s: %d unreplied topics deleted, %d papers, %d posted, %d already on the forum, %d refused",
        build, deleted, len(papers), counts["created"], counts["exists"], counts["refused"],
    )
    if not dry_run:
        save_state(STATE_PATH, {**state, **validators, "posted_build": build})

    # Refusals are not retried, so they fail the unit to be seen.
    return 1 if counts["refused"] or prune_refused else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print the topics instead of posting them")
    parser.add_argument("--feed", metavar="FILE", help="read the feed from a file rather than from arXiv")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.dry_run:
        missing = [name for name, value in (
            ("DISCOURSE_API_KEY", API_KEY), ("ARXIV_CATEGORY_ID", CATEGORY_ID),
        ) if not value]
        if missing:
            LOG.error("not set: %s. See server/README.md.", ", ".join(missing))
            return 2

    return run(args.feed, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
