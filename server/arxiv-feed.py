#!/usr/bin/env python3
"""Post every preprint arXiv announces in quant-ph to the forum, one topic each.

Reads arXiv's quant-ph RSS feed, keeps the new submissions and the cross-lists,
and opens a topic for each in the arXiv category: the title, the authors, the
abstract, and links to the abstract page and the PDF. A revised version of a
paper the forum already has is not posted again.

Run by a systemd timer every ten minutes. arXiv announces once a day, Sunday to
Thursday, and rebuilds the feed a few hours later, so almost every run asks
whether the feed has changed, hears that it has not, and exits. The run after a
rebuild posts the whole batch.

Three properties the rest of the forum relies on.

**Idempotent without local state.** Every topic carries the paper's id as its
``external_id``, which Discourse holds unique, so posting a paper twice is
refused by the forum rather than prevented by a file here. A run killed halfway
through is repeated by the next one, which posts what is missing and is refused
on the rest. The state file only saves the forum those refusals: it remembers
which build of the feed was posted in full.

**One account, and a key that can do nothing else.** ``arxiv-bot`` is a
moderator, because staff are exempt from ``max_topics_per_day`` and a day's
batch is larger than that limit. Its key is granular, scoped to creating posts
and reading topics, so the moderator status is unreachable through it.
``scripts/40-configure-forum.py`` creates both; docs/05-forum-config.md explains.

**Polite to arXiv and to the forum.** One conditional request to arXiv per run,
with a User-Agent that says who is asking. Topics are opened one at a time, a
couple of seconds apart, because the API limiter is one bucket for the whole
site and the agents share it.

Usage::

    python3 arxiv-feed.py                     # post what is new
    python3 arxiv-feed.py --dry-run           # print it instead, change nothing
    python3 arxiv-feed.py --feed saved.xml    # read a saved feed rather than arXiv

Configuration is environment variables, read from
``/etc/qupa-cafe/arxiv-feed.env`` by the service; see server/README.md.
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
# Who arXiv should write to about this client. arXiv asks automated clients to
# identify themselves, and an address is what makes that useful to them.
CONTACT = os.environ.get("ARXIV_CONTACT", "")
STATE_PATH = Path(os.environ.get("ARXIV_STATE", "/var/lib/qupa-arxiv-feed/state.json"))
POST_INTERVAL = float(os.environ.get("ARXIV_POST_INTERVAL", "2"))

# The announcement types that are posted. `replace` and `replace-cross` are new
# versions of papers already announced, and the forum has those topics.
POSTED_TYPES = ("new", "cross")

# Discourse's max_topic_title_length, which this forum leaves at its default.
MAX_TITLE_LENGTH = 255

# A collaboration paper can list hundreds of authors, and the post has to stay
# under max_post_length. The abstract page has the rest.
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
# A control word ends at the first non-letter, and TeX swallows one space or an
# empty group after it: `Stra\ss e` is Straße and `\o{}stergaard` is østergaard.
_LETTER = re.compile(r"\{?\\(ss|ae|AE|oe|OE|aa|AA|o|O|l|L|i|j)(?![A-Za-z])(?:\{\}| )?\}?")


def detex(text: str) -> str:
    """Turn the TeX accents arXiv keeps in author names into the letters they are.

    Names only. An abstract's TeX is left alone, because the forum renders it.
    """
    # Accents first: `\'\i` is an accented dotless i, which is a plain í, and
    # replacing `\i` on its own first would leave the accent with no letter.
    def accent(match: re.Match) -> str:
        letter = match.group(2)
        letter = {"\\i": "i", "\\j": "j"}.get(letter, letter)
        return unicodedata.normalize("NFC", letter + TEX_ACCENTS[match.group(1)])

    text = _ACCENT.sub(accent, text)
    text = _LETTER.sub(lambda m: TEX_LETTERS[m.group(1)], text)
    return re.sub(r"[{}]", "", text)


def squash(text: str | None) -> str:
    """Collapse every run of whitespace, newlines included, to one space.

    A newline inside a paragraph would render as a line break on the forum.
    """
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
    """The paper's title with its arXiv id after it.

    The id is not decoration. Discourse refuses a title shorter than fifteen
    characters and a title another topic already has, and arXiv has short
    titles and repeated ones; with the id appended, neither can happen.
    """
    suffix = f" (arXiv:{paper.arxiv_id})"
    room = MAX_TITLE_LENGTH - len(suffix)
    title = paper.title if len(paper.title) <= room else paper.title[: room - 1].rstrip() + "…"
    return title + suffix


def topic_body(paper: Paper, template: str) -> str:
    """Fill the template in one pass, so that text from arXiv which happens to
    look like a placeholder is copied rather than substituted."""
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
    """The forum could not take the post right now. The run stops and the next
    one picks up where this one left off."""


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

        # 429 is a queue, not a failure. The API limiter is one bucket for the
        # whole site, so the traffic it is counting may be an agent's.
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
            # A refusal is a duplicate if the topic is there. Asking is more
            # reliable than reading the error message, whose wording is the
            # forum's to change.
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
    """The feed's text, or None if it has not changed since it was last read.

    Conditional on the ETag and Last-Modified the previous fetch returned, so
    an unchanged feed costs arXiv a 304.
    """
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


def post_papers(forum: Forum | None, papers: list[Paper], template: str, category: str) -> dict[str, int]:
    """Open a topic per paper. ``forum=None`` prints them instead.

    A paper the forum refuses for a reason other than already having it is
    logged and passed over, so that one malformed entry does not hold back the
    rest of the day. A transient failure stops the run, and the next one
    resumes.
    """
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

    try:
        counts = post_papers(forum, papers, template, CATEGORY_ID)
    except Transient as exc:
        LOG.error("stopping: %s. The next run resumes from here.", exc)
        return 1

    LOG.info(
        "build %s: %d papers, %d posted, %d already on the forum, %d refused",
        build, len(papers), counts["created"], counts["exists"], counts["refused"],
    )
    if not dry_run:
        save_state(STATE_PATH, {**state, **validators, "posted_build": build})

    # Refusals are not retried, so they have to be visible: a failed unit is
    # what `systemctl --failed` and the ops checks look for.
    return 1 if counts["refused"] else 0


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
