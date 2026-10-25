#!/usr/bin/env python3
"""Post every preprint arXiv announces in quant-ph to the forum, one topic each.

Run every ten minutes by qupa-arxiv-feed.timer. A topic names the first and
last authors, and the feed's own reply under it gives every author with their
affiliations in full, read from the paper's HTML version, and the abstract. A paper waits in the state file's queue for its HTML version for
up to an hour, and is then posted without affiliations.

Each topic carries the paper's id as its ``external_id``, which the forum holds
unique, so a repeated or interrupted run is refused on what it already posted.
A topic's title is the paper's with its arXiv id after it. When a build is
taken in, every earlier topic the feed opened that nobody else has replied to
is deleted. docs/05-forum-config.md 5.10 has the account and its key.

Usage::

    python3 arxiv-feed.py                     # post what is new
    python3 arxiv-feed.py --dry-run           # print it instead, change nothing
    python3 arxiv-feed.py --feed saved.xml    # read a saved feed rather than arXiv
"""

from __future__ import annotations

import argparse
import codecs
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
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
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
# The paper's id is appended. One request every three seconds, as arXiv asks.
HTML_URL = os.environ.get("ARXIV_HTML_URL", "https://arxiv.org/html/")
HTML_INTERVAL = float(os.environ.get("ARXIV_HTML_INTERVAL", "3"))
# How long a paper waits for its HTML version before it is posted without.
HTML_WAIT = float(os.environ.get("ARXIV_HTML_WAIT", "3600"))
# The author list is near the top; a page this long without one has none.
HTML_READ_LIMIT = 8 * 1024 * 1024

# `replace` and `replace-cross` are new versions of papers already posted.
POSTED_TYPES = ("new", "cross")

# Discourse's max_topic_title_length, which this forum leaves at its default.
MAX_TITLE_LENGTH = 255

# The forum's max_post_length (scripts/40-configure-forum.py), which the
# feed's reply fits by shortening affiliations and then cutting authors.
MAX_POST_LENGTH = 3800

CONTENT = Path(__file__).resolve().parent.parent / "content"
TEMPLATE_PATH = Path(os.environ.get("ARXIV_TEMPLATE") or CONTENT / "arxiv-topic.md")
DETAILS_TEMPLATE_PATH = Path(os.environ.get("ARXIV_DETAILS_TEMPLATE") or CONTENT / "arxiv-details.md")

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
    # What arXiv's metadata gives in parentheses after a name, or "".
    listed_affiliations: list[str] = field(default_factory=list)

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

    @property
    def html_url(self) -> str:
        return HTML_URL + self.arxiv_id


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


def split_creators(text: str) -> list[tuple[str, str]]:
    """(name, affiliation) for each comma-separated author, the affiliation
    being what follows the name in parentheses, whose commas do not split."""
    creators, depth, start = [], 0, 0
    for n, char in enumerate(text + ","):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth <= 0:
            part, start, depth = squash(text[start:n]), n + 1, 0
            if match := re.fullmatch(r"(.*?)\s*\((.*)\)", part):
                creators.append((squash(match.group(1)), squash(match.group(2))))
            elif part:
                creators.append((part, ""))
    return [(name, affiliation) for name, affiliation in creators if name]


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

        creators = split_creators(detex(item.findtext("dc:creator", namespaces=NS) or ""))

        papers.append(
            Paper(
                arxiv_id=match.group(1),
                title=squash(item.findtext("title")),
                authors=[name for name, _ in creators],
                abstract=squash(abstract if found else description),
                announce_type=announce_type,
                primary_category=categories[0] if categories else "quant-ph",
                announced=announced,
                listed_affiliations=[affiliation for _, affiliation in creators],
            )
        )

    return build, papers


# ---------------------------------------------------------------- affiliations


# Around a superscript in an author's name or affiliation, which may be the
# number that ties the two together.
MARK_OPEN, MARK_CLOSE = "\x01", "\x02"
MARK = re.compile(f"{MARK_OPEN}([^{MARK_CLOSE}]*){MARK_CLOSE}")


class AuthorList(HTMLParser):
    """The authors LaTeXML lists in ``ltx_authors`` at the top of an arXiv HTML
    page: each one's name, and the text of each affiliation or address given
    for them, with superscripts marked. Footnotes, emails, labels and
    mathematics are left out."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}

    def __init__(self) -> None:
        super().__init__()
        # (tag, role) for every open element; role is what its text is for.
        self.open: list[tuple[str, str | None]] = []
        self.authors: list[dict] = []
        self.started = self.done = False

    def _append(self, roles, text: str) -> None:
        if "contact" in roles:
            self.authors[-1]["contacts"][-1] += text
        elif "name" in roles:
            self.authors[-1]["name"] += text

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self.done:
            return
        roles = {role for _, role in self.open}
        if tag in self.VOID:
            if tag == "br" and "skip" not in roles:
                self._append(roles, "\n")
            return

        classes = set((dict(attrs).get("class") or "").split())
        role = None
        if "ltx_authors" in classes and not self.started:
            role, self.started = "authors", True
        elif "authors" not in roles or "skip" in roles:
            role = None
        elif tag in ("math", "svg") or classes & {"ltx_note", "ltx_contact_name"}:
            role = "skip"
        elif tag == "sup" and roles & {"name", "contact"}:
            role = "mark"
            self._append(roles, MARK_OPEN)
        elif {"ltx_creator", "ltx_role_author"} <= classes:
            role = "creator"
            self.authors.append({"name": "", "contacts": []})
        elif "ltx_personname" in classes and "creator" in roles:
            role = "name"
        elif "ltx_contact" in classes and "creator" in roles:
            if classes & {"ltx_role_affiliation", "ltx_role_address"}:
                role = "contact"
                self.authors[-1]["contacts"].append("")
            else:
                role = "skip"
        self.open.append((tag, role))

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.open) - 1, -1, -1):
            if self.open[depth][0] == tag:
                closed = [role for _, role in self.open[depth:]]
                del self.open[depth:]
                if "mark" in closed and "skip" not in {role for _, role in self.open}:
                    self._append({role for _, role in self.open}, MARK_CLOSE)
                if "authors" in closed:
                    self.done = True
                return

    def handle_data(self, data: str) -> None:
        roles = {role for _, role in self.open}
        if not self.done and "authors" in roles and "skip" not in roles:
            self._append(roles, data)


def clean_affiliation(text: str) -> str:
    """One line, as the paper gives it, without a closing full stop or the
    email addresses some papers put after it."""
    text = re.sub(r"\s*,?\s*\n\s*", ", ", MARK.sub("", text).strip())
    text = re.split(r"\b(?:e-?mails?|correspond\w*)\s*:|corresponding\s+authors?\b", text, flags=re.I)[0]
    return squash(text).strip(" ,;.")


def is_email(text: str) -> bool:
    return all("@" in part for part in re.split(r"[,;\s]+", text) if part)


# A footnote a page lists among the affiliations, which names nowhere.
NOTE = re.compile(r"\bcontributed\s+equally\b|\bequal(ly)?\s+contribut|\bthese\s+authors\b", re.I)


def page_authors(html_text: str) -> list[tuple[str, list[str]]]:
    """(name, affiliations) for each author an HTML page lists, in order, the
    superscripts still marked."""
    parser = AuthorList()
    parser.feed(html_text)
    return [(author["name"], author["contacts"]) for author in parser.authors]


def numbered(name: str, contacts: list[str]) -> list[list[str]]:
    """Each author's affiliations when one name block lists every author:
    with superscript numbers, which start the affiliations they stand for, or
    with none, when every author shares every affiliation."""
    if not MARK.search(name):
        names = [n for n in re.split(r",|\band\b", squash(name)) if n.strip()]
        return [[clean_affiliation(c) for c in contacts] for _ in names]

    by_number = {}
    for contact in contacts:
        if match := re.match(f"\\s*{MARK.pattern}", contact):
            by_number[match.group(1).strip()] = clean_affiliation(contact)

    authors: list[list[str]] = []
    pending = ""
    for token in re.split(f"({MARK_OPEN}[^{MARK_CLOSE}]*{MARK_CLOSE})", name):
        if token.startswith(MARK_OPEN):
            if pending:
                authors.append([])
                pending = ""
            if authors:
                authors[-1] += [by_number[n] for n in re.split(r"[,\s]+", MARK.sub(r"\1", token))
                                if n in by_number and by_number[n] not in authors[-1]]
        else:
            pending = re.sub(r"^(\s*(,|and\b))+", "", squash(token)).strip(" ,;")
    if pending:
        authors.append([])
    return authors


def affiliations(paper: Paper, html_text: str | None) -> list[list[str]] | None:
    """Each of the paper's authors' affiliations, in the feed's order: from
    the HTML page, or, where it gives none, from arXiv's metadata. None when
    neither has any."""
    found = html_affiliations(paper, html_text) if html_text is not None else None
    if found is None and any(paper.listed_affiliations):
        found = [[a] if a else [] for a in paper.listed_affiliations]
    return found


def html_affiliations(paper: Paper, html_text: str) -> list[list[str]] | None:
    """The affiliations an HTML page gives the paper's authors, or None when
    it gives none or lists its authors in a way that leaves no telling whose
    an affiliation is."""
    listed = page_authors(html_text)
    if len(listed) == len(paper.authors):
        affs = [[clean_affiliation(c) for c in contacts] for _, contacts in listed]
    elif len(listed) == 1 and len(paper.authors) > 1:
        affs = numbered(*listed[0])
        if len(affs) != len(paper.authors):
            return None
    else:
        return None
    affs = [[a for a in own if a and not is_email(a) and not NOTE.search(a)] for own in affs]
    return affs if any(affs) else None


# ---------------------------------------------------------------------- a topic


def topic_title(paper: Paper) -> str:
    """The title with its arXiv id, so it is never too short or a duplicate."""
    suffix = f" (arXiv:{paper.arxiv_id})"
    room = MAX_TITLE_LENGTH - len(suffix)
    title = paper.title if len(paper.title) <= room else paper.title[: room - 1].rstrip() + "…"
    return title + suffix


def fill(template: str, values: dict[str, str]) -> str:
    """Fill a template in one pass, so a placeholder-like abstract is copied.
    A placeholder filled with nothing leaves no run of blank lines."""
    filled = re.sub(r"%([A-Z_]+)%", lambda m: values.get(m.group(1), m.group(0)), template)
    return re.sub(r"\n{3,}", "\n\n", filled).strip()


def topic_body(paper: Paper, template: str) -> str:
    """The topic's first post: the first and last authors and how many
    authors come between."""
    authors = paper.authors
    count = len(authors)
    if count == 0:
        names = "Authors not listed"
    elif count == 1:
        names = authors[0]
    elif count == 2:
        names = f"{authors[0]} and {authors[1]}"
    else:
        between = count - 2
        others = f"{between} other author" + ("s" if between > 1 else "")
        names = f"{authors[0]}, {others}, and {authors[-1]}"

    return fill(template, {
        "AUTHORS": names,
        "ABS_URL": paper.abs_url,
        "PDF_URL": paper.pdf_url,
    })


SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def author_lines(authors: list[str], affs: list[list[str]] | None, more: int) -> tuple[str, str]:
    """The author list and the affiliations its superscript numbers point to,
    each line opening with its own; an author's numbers stand side by side, so
    they read alike on the forum and in Slack, which has no superscript.
    Authors who share one set of affiliations have no numbers."""
    if not affs or all(a == affs[0] for a in affs):
        names = ", ".join(authors)
        listed = "; ".join(affs[0]) if affs and affs[0] else ""
    else:
        numbers: dict[str, int] = {}
        marked = []
        for name, own in zip(authors, affs):
            marks = "".join(str(numbers.setdefault(a, len(numbers) + 1)) for a in own)
            marked.append(name + marks.translate(SUPERSCRIPT))
        names = ", ".join(marked)
        listed = "\n".join(str(n).translate(SUPERSCRIPT) + a for a, n in numbers.items())
    if more:
        names += f", and {more} more"
    return names, listed


def details_body(paper: Paper, template: str, affs: list[list[str]] | None) -> str:
    """The feed's reply: every author with their affiliations as the paper
    gives them, and the abstract. Over ``MAX_POST_LENGTH``, the authors are
    cut, saying how many."""
    def body(shown: int) -> str:
        own = None if affs is None else [list(dict.fromkeys(a)) for a in affs[:shown]]
        names, listed = author_lines(paper.authors[:shown], own, len(paper.authors) - shown)
        return fill(template, {
            "AUTHORS": names or "Authors not listed",
            "AFFILIATIONS": listed,
            "ABSTRACT": paper.abstract,
        })

    count = len(paper.authors)
    shown = count
    while shown > 1 and len(body(shown)) > MAX_POST_LENGTH:
        shown -= 1
    return body(shown)


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

    def find_topic(self, external_id: str) -> dict | None:
        """The topic with this external id, with its first posts, or None."""
        try:
            return self._request("GET", f"/t/external_id/{urllib.parse.quote(external_id)}.json")
        except RuntimeError:
            return None

    def has_details(self, topic: dict) -> bool:
        """Whether the feed's reply is among the topic's posts."""
        return any(
            post.get("post_number", 1) > 1 and post.get("username") == self.username
            for post in (topic.get("post_stream") or {}).get("posts") or []
        )

    def category_topics(self, category: str) -> list[dict]:
        """Every topic the category's page lists, read in full before any change,
        each with the username of who opened it as ``original_poster``."""
        topics: list[dict] = []
        for page in range(1000):
            answer = self._request("GET", f"/c/{category}/l/latest.json?page={page}")
            names = {user.get("id"): user.get("username") for user in answer.get("users") or []}
            listed = answer.get("topic_list", {})
            for topic in listed.get("topics", []):
                opener = next((poster.get("user_id") for poster in topic.get("posters") or []
                               if "Original Poster" in (poster.get("description") or "")), None)
                topics.append({**topic, "original_poster": names.get(opener)})
            if not listed.get("more_topics_url") or not listed.get("topics"):
                return topics
        raise RuntimeError(f"category {category} lists more than 1000 pages")

    def delete_topic(self, topic_id: int) -> None:
        self._request("DELETE", f"/t/{topic_id}.json")

    def create_topic(self, title: str, raw: str, category: str, external_id: str) -> tuple[str, dict]:
        """Open one topic. Returns "created" or "exists" with the topic, which
        for "exists" has its first posts; raises on anything else."""
        try:
            post = self._request(
                "POST",
                "/posts.json",
                {"title": title, "raw": raw, "category": category, "external_id": external_id},
            )
            return "created", {"id": post.get("topic_id")}
        except Transient:
            raise
        except RuntimeError:
            # A refusal is a duplicate if the topic is there.
            if (topic := self.find_topic(external_id)) is not None:
                return "exists", topic
            raise

    def reply(self, topic_id: int, raw: str) -> None:
        self._request("POST", "/posts.json", {"topic_id": topic_id, "raw": raw})


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


def user_agent() -> str:
    contact = f"; mailto:{CONTACT}" if CONTACT else ""
    return f"qupa-cafe-arxiv-feed (+{FORUM_URL}{contact})"


def fetch_feed(url: str, state: dict) -> tuple[str | None, dict]:
    """The feed's text, or None if a conditional request says it is unchanged."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", user_agent())
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


def fetch_html(url: str) -> str | None:
    """A paper's HTML version as far as the end of its author list, or None
    while arXiv has none to serve."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", user_agent())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            parser, chunks, read = AuthorList(), [], 0
            while not parser.done and read < HTML_READ_LIMIT:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                read += len(chunk)
                chunks.append(decoder.decode(chunk))
                parser.feed(chunks[-1])
            return "".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            LOG.warning("%s -> %s; trying again at the next run", url, exc.code)
        return None
    except (urllib.error.URLError, OSError) as exc:
        LOG.warning("could not fetch %s: %s; trying again at the next run", url, exc)
        return None


def unreplied_preprints(topics: list[dict], keep: set[str], feed: str) -> list[dict]:
    """The topics ``feed`` opened that nobody else has posted in, minus the
    titles in ``keep``. A pinned topic is not the feed's."""
    return [
        topic for topic in topics
        if topic.get("original_poster") == feed
        and topic.get("title") not in keep
        and (topic.get("posts_count") == 1
             or (topic.get("posts_count") == 2 and topic.get("last_poster_username") == feed))
        and not topic.get("pinned")
    ]


def prune(forum: Forum, category: str, papers: list[Paper], dry_run: bool) -> tuple[int, int]:
    """Delete the category's unreplied preprints, except this build's own, so
    a resumed build keeps what it already posted. Returns (deleted, refused)."""
    keep = {topic_title(p) for p in papers}
    candidates = unreplied_preprints(forum.category_topics(category), keep, forum.username)
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


def post_paper(forum: Forum | None, paper: Paper, affs: list[list[str]] | None,
               templates: tuple[str, str], category: str) -> str:
    """Open the paper's topic and post the feed's reply in it at once, so the
    topic is embedded with both. Prints them with ``forum=None``. Returns
    "created", "exists" or "refused"; a transient failure is raised, and the
    next run posts what is missing."""
    title = topic_title(paper)
    raw, details = topic_body(paper, templates[0]), details_body(paper, templates[1], affs)
    if forum is None:
        print(f"--- {paper.external_id}\n{title}\n\n{raw}\n\n--- reply\n{details}\n")
        return "created"

    try:
        outcome, topic = forum.create_topic(title, raw, category, paper.external_id)
        if outcome == "created" or not forum.has_details(topic):
            forum.reply(topic["id"], details)
    except Transient:
        raise
    except RuntimeError as exc:
        LOG.error("refused %s: %s", paper.arxiv_id, exc)
        return "refused"
    return outcome


def post_queue(forum: Forum | None, queue: list[dict], templates: tuple[str, str], category: str,
               save, dry_run: bool) -> dict[str, int]:
    """Post each queued paper whose HTML version is up, or that has waited
    ``HTML_WAIT`` for it, taking it off the queue and saving. A dry run
    waits for nothing. A refused paper is logged and passed over; a
    transient failure stops the run."""
    counts = {"created": 0, "exists": 0, "refused": 0, "waiting": 0}

    for entry in list(queue):
        paper = Paper(**entry["paper"])
        html = fetch_html(paper.html_url)
        time.sleep(HTML_INTERVAL)
        if html is None and time.time() - entry["since"] < HTML_WAIT and not dry_run:
            counts["waiting"] += 1
            continue

        outcome = post_paper(forum, paper, affiliations(paper, html), templates, category)
        queue.remove(entry)
        save()
        counts[outcome] += 1
        if outcome == "created" and forum is not None:
            LOG.info("posted %s%s", paper.arxiv_id, "" if html else ", with no HTML version")
            time.sleep(POST_INTERVAL)

    return counts


def run(feed_file: str | None, dry_run: bool) -> int:
    state = {} if dry_run else load_state(STATE_PATH)

    if feed_file:
        xml_text, validators = Path(feed_file).read_text(encoding="utf-8"), {}
    else:
        xml_text, validators = fetch_feed(FEED_URL, state)
    if xml_text is None and not state.get("queue"):
        LOG.info("feed unchanged since the last fetch")
        return 0

    def save() -> None:
        if not dry_run:
            save_state(STATE_PATH, state)

    templates = (TEMPLATE_PATH.read_text(encoding="utf-8"), DETAILS_TEMPLATE_PATH.read_text(encoding="utf-8"))
    forum = None if dry_run else Forum(FORUM_URL, API_KEY, API_USERNAME)

    # A dry run with a key reads the category to show what would go.
    pruner = forum or (Forum(FORUM_URL, API_KEY, API_USERNAME) if API_KEY and CATEGORY_ID else None)
    prune_refused = 0
    try:
        if xml_text is not None:
            build, papers = parse_feed(xml_text)
            # `posted_build` names the build in a state file without a queue.
            if build and build == state.get("build", state.get("posted_build")):
                LOG.info("build %s was already taken in", build)
            else:
                deleted = 0
                if pruner and papers:
                    try:
                        deleted, prune_refused = prune(pruner, CATEGORY_ID, papers, dry_run)
                    except Transient:
                        raise
                    except RuntimeError as exc:
                        # A key without the scopes to list or delete must not stop the feed.
                        LOG.error("could not read the category to delete unreplied topics: %s", exc)
                        prune_refused = 1
                if state.get("queue"):
                    LOG.warning("build %s replaces %d papers never posted", build, len(state["queue"]))
                state.pop("posted_build", None)
                state.update(build=build, queue=[{"paper": asdict(p), "since": time.time()} for p in papers])
                LOG.info("build %s: %d unreplied topics deleted, %d papers", build, deleted, len(papers))
            # Only once the build is taken in, so a failed run fetches it again.
            state.update(validators)
            save()

        counts = post_queue(forum, state.setdefault("queue", []), templates, CATEGORY_ID, save, dry_run)
    except Transient as exc:
        LOG.error("stopping: %s. The next run resumes from here.", exc)
        return 1

    LOG.info(
        "build %s: %d posted, %d already on the forum, %d refused, %d waiting for an HTML version",
        state.get("build"), counts["created"], counts["exists"], counts["refused"], counts["waiting"],
    )

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
