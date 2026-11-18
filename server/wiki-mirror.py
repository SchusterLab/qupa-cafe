#!/usr/bin/env python3
"""Mirror the lab's MediaWiki into a forum category, one topic per page.

Run every fifteen minutes by qupa-wiki-mirror.timer. The wiki stays where pages
are edited: each run lists the wiki's pages, and every page whose revision
changed is fetched as HTML from the wiki's REST API, turned into Markdown and
written to its topic. A page longer than one post goes on in the next posts of
the topic, each opening with which part of the page it is. Those posts are the
mirror's own, and it edits them in place; replies after them are discussion.

Each topic carries the page's id as its ``external_id``, so a renamed page
keeps its topic and a lost state file finds the topics again. A wiki link to
another page links to that page's topic. Images shown on a page are uploaded
to the forum; other files are linked on the wiki. A page deleted on the wiki
has its topic closed with a note.

With --listen, it answers the wiki's notices that a page changed, and each
starts a run within seconds; the timer's runs stay as the backstop for a lost
notice. docs/05-forum-config.md 5.11.

Usage::

    python3 wiki-mirror.py              # mirror what changed
    python3 wiki-mirror.py --dry-run    # print what would be written, change nothing
    python3 wiki-mirror.py --page "BlueFors1" --dry-run   # one page's Markdown
    python3 wiki-mirror.py --listen     # run on each of the wiki's notices
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import http.cookiejar
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:  # not on Linux, where the mirror runs; the tests run anywhere
    fcntl = None

LOG = logging.getLogger("wiki-mirror")

WIKI_URL = os.environ.get("WIKI_URL", "").rstrip("/")
# A bot password's name is `User@name`, and logs in through action=login;
# any other name logs in as a person, through clientlogin.
WIKI_USERNAME = os.environ.get("WIKI_USERNAME", "")
WIKI_PASSWORD = os.environ.get("WIKI_PASSWORD", "")
# The namespaces mirrored, by number: 0 is the wiki's main pages.
WIKI_NAMESPACES = tuple(int(n) for n in os.environ.get("WIKI_NAMESPACES", "0").split(",") if n.strip())
FORUM_URL = os.environ.get("DISCOURSE_URL", "http://127.0.0.1").rstrip("/")
API_KEY = os.environ.get("DISCOURSE_API_KEY", "")
API_USERNAME = os.environ.get("DISCOURSE_API_USERNAME", "wiki-bot")
CATEGORY_ID = os.environ.get("WIKI_CATEGORY_ID", "")
STATE_PATH = Path(os.environ.get("WIKI_STATE", "/var/lib/qupa-wiki-mirror/state.json"))
POST_INTERVAL = float(os.environ.get("WIKI_POST_INTERVAL", "1"))
# The external_id of a page's topic is this, then the page's id.
EXTERNAL_PREFIX = os.environ.get("WIKI_EXTERNAL_PREFIX", "labwiki-")
USER_AGENT = "qupa-cafe wiki mirror (+https://github.com/SchusterLab/qupa-cafe)"

# The forum's max_post_length (scripts/40-configure-forum.py). A part of a page
# leaves room for the line that opens it.
MAX_POST_LENGTH = 3800
PART_LENGTH = 3400
# A table with a row longer than this is written out row by row instead.
LONG_ROW = 600
# The forum's upload limit, max_image_size_kb.
MAX_UPLOAD_BYTES = 4096 * 1024
# An image too big to upload is fetched scaled to this width.
SCALED_WIDTH = 1600
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# The wiki's notices that a page changed (server/wiki-notify.php), which the
# forum's nginx passes on from /host/wiki-changed. Each is signed with the
# secret the wiki and the mirror share, and starts a run within seconds.
HOOK_HOST = os.environ.get("WIKI_HOOK_HOST", "172.17.0.1")
HOOK_PORT = int(os.environ.get("WIKI_HOOK_PORT", "8093"))
HOOK_SECRET = os.environ.get("WIKI_HOOK_SECRET", "")
# A notice signed further than this from now is refused, so that one recorded
# on the way cannot be sent again later.
HOOK_MAX_AGE = 300
HOOK_MAX_BODY = 4096
# One change on the wiki can fire more than one hook, such as a move and the
# redirect it leaves, so a run waits this long for the rest before it starts.
HOOK_SETTLE = float(os.environ.get("WIKI_HOOK_SETTLE", "3"))


# ------------------------------------------------------------- HTML to Markdown


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = field(default=None, repr=False)

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def has_class(self, name: str) -> bool:
        return name in (self.attrs.get("class") or "").split()


VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr", "track"}
SKIPPED = {"head", "script", "style", "noscript", "template"}


class Tree(HTMLParser):
    """The page's body as a tree of Nodes and strings."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root", {})
        self.at = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, parent=self.at)
        self.at.children.append(node)
        if tag not in VOID:
            self.at = node

    def handle_startendtag(self, tag, attrs):
        self.at.children.append(Node(tag, {k: v or "" for k, v in attrs}, parent=self.at))

    def handle_endtag(self, tag):
        node = self.at
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.at = node.parent

    def handle_data(self, data):
        self.at.children.append(data)


def parse(html: str) -> Node:
    tree = Tree()
    tree.feed(html)
    tree.close()
    body = find(tree.root, "body")
    return body or tree.root


def find(node: Node, tag: str) -> Node | None:
    for child in node.children:
        if isinstance(child, Node):
            if child.tag == tag:
                return child
            found = find(child, tag)
            if found:
                return found
    return None


BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "dl", "table", "figure", "pre", "blockquote",
         "div", "section", "hr", "center"}


@dataclass
class Block:
    text: str
    # A heading's level, 0 for anything else; a part of the page starts at one.
    level: int = 0
    # A table's header and rule, repeated when the table is split.
    table_head: str = ""


class Converter:
    """Parsoid's HTML for one page, as Markdown blocks.

    ``link(title, fragment)`` gives the address for a wiki link, and
    ``image(src, alt, width, height)`` the Markdown for an image.
    """

    def __init__(self, link: Callable[[str, str], str], image: Callable[[str, str, int, int], str]) -> None:
        self.link = link
        self.image = image

    def blocks(self, html: str) -> list[Block]:
        out: list[Block] = []
        self._blocks(parse(html), out)
        return [b for b in out if b.text.strip()]

    # -- blocks

    def _blocks(self, node: Node, out: list[Block]) -> None:
        inline: list[Node | str] = []

        def flush():
            text = self._inline_all(inline).strip()
            if text:
                out.append(Block(text))
            inline.clear()

        for child in node.children:
            if isinstance(child, str) or child.tag not in BLOCK:
                if isinstance(child, Node) and self._skip(child):
                    continue
                inline.append(child)
                continue
            flush()
            self._block(child, out)
        flush()

    def _block(self, node: Node, out: list[Block]) -> None:
        tag = node.tag
        if self._skip(node):
            return
        if tag in ("section", "div", "center"):
            self._blocks(node, out)
        elif tag == "p":
            text = self._inline_all(node.children).strip()
            if text:
                out.append(Block(text))
        elif re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            text = self._inline_all(node.children).strip()
            if text:
                out.append(Block("#" * level + " " + text, level=level))
        elif tag in ("ul", "ol"):
            text = self._list(node, 0)
            if text.strip():
                out.append(Block(text))
        elif tag == "dl":
            lines = []
            for child in node.children:
                if isinstance(child, Node) and child.tag == "dt":
                    lines.append(f"**{self._inline_all(child.children).strip()}**")
                elif isinstance(child, Node) and child.tag == "dd":
                    lines.append(self._cell(child))
            text = "  \n".join(line for line in lines if line)
            if text:
                out.append(Block(text))
        elif tag == "table":
            self._table(node, out)
        elif tag == "figure":
            out.append(Block(self._figure(node)))
        elif tag == "pre":
            code = node.text().strip("\n")
            fence = "~~~" if "```" in code else "```"
            out.append(Block(f"{fence}\n{code}\n{fence}"))
        elif tag == "blockquote":
            inner: list[Block] = []
            self._blocks(node, inner)
            text = "\n>\n".join("\n".join("> " + line for line in b.text.split("\n")) for b in inner)
            if text:
                out.append(Block(text))
        elif tag == "hr":
            out.append(Block("---"))

    def _skip(self, node: Node) -> bool:
        if node.tag in SKIPPED or node.tag in ("meta", "link"):
            return True
        typeof = node.attrs.get("typeof") or ""
        # A transclusion that renders nothing, such as DISPLAYTITLE.
        if "mw:Transclusion" in typeof and not node.text().strip() and not find(node, "img"):
            return True
        if node.has_class("toc") or node.attrs.get("id") == "toc" or node.has_class("mw-editsection"):
            return True
        return False

    def _list(self, node: Node, depth: int) -> str:
        lines = []
        number = 0
        for item in node.children:
            if not isinstance(item, Node) or item.tag != "li":
                continue
            number += 1
            marker = f"{number}. " if node.tag == "ol" else "- "
            indent = " " * len(marker)
            parts: list[str] = []
            inline: list[Node | str] = []
            nested: list[str] = []
            for child in item.children:
                if isinstance(child, Node) and child.tag in ("ul", "ol"):
                    nested.append(self._list(child, depth + 1))
                elif isinstance(child, Node) and child.tag in BLOCK:
                    inner: list[Block] = []
                    self._block(child, inner)
                    parts.extend(b.text for b in inner)
                elif not (isinstance(child, Node) and self._skip(child)):
                    inline.append(child)
            head = self._inline_all(inline).strip()
            body = "\n".join([p for p in [head, *parts] if p])
            text = marker + body.replace("\n", "\n" + indent) if body else marker.rstrip()
            for sub in nested:
                text += "\n" + "\n".join(indent + line if line else line for line in sub.split("\n"))
            lines.append(text)
        return "\n".join(lines)

    def _cell_blocks(self, node: Node) -> str:
        """A cell's content as Markdown, its blocks on lines of their own."""
        inner: list[Block] = []
        self._blocks(node, inner)
        return "\n".join(b.text for b in inner).strip()

    def _cell(self, node: Node) -> str:
        """A table cell or definition on one line: its blocks joined by <br>."""
        return self._cell_blocks(node).replace("\n", "<br>").replace("|", "\\|")

    def _table(self, node: Node, out: list[Block]) -> None:
        rows: list[list[str]] = []
        caption = ""
        for part in self._descendants(node, ("tr", "caption")):
            if part.tag == "caption":
                caption = self._inline_all(part.children).strip()
                continue
            cells = [c for c in part.children if isinstance(c, Node) and c.tag in ("td", "th")]
            if not cells:
                continue
            row = []
            for cell in cells:
                row.append(self._cell_blocks(cell))
                for _ in range(int(cell.attrs.get("colspan") or 1) - 1):
                    row.append("")
            rows.append(row)
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        if caption:
            out.append(Block(f"**{caption}**"))
        # The first row names the columns, whether the wiki marked it a header or not.
        head, body = rows[0], rows[1:]
        flat = [[c.replace("\n", "<br>").replace("|", "\\|") or " " for c in r] for r in rows]
        lines = ["| " + " | ".join(r) + " |" for r in flat]
        if max(len(line) for line in lines) > LONG_ROW or not body:
            # Too long a row for a Markdown table: each row is a heading, with
            # a line for each column under it.
            for row in body or rows:
                label = row[0].replace("\n", " ").strip() or "(row)"
                out.append(Block(f"#### {label}", level=4))
                for name, value in zip(head[1:], row[1:]):
                    if value.strip():
                        name = name.replace("\n", " ").strip()
                        out.append(Block(f"**{name}**\n\n{value}" if name else value))
            return
        head_text = lines[0] + "\n|" + "---|" * width
        out.append(Block("\n".join([head_text] + lines[1:]), table_head=head_text))

    def _descendants(self, node: Node, tags: tuple[str, ...]):
        for child in node.children:
            if isinstance(child, Node):
                if child.tag in tags:
                    yield child
                elif child.tag != "table":
                    yield from self._descendants(child, tags)

    def _figure(self, node: Node) -> str:
        img = find(node, "img")
        caption_node = find(node, "figcaption")
        caption = self._inline_all(caption_node.children).strip() if caption_node else ""
        picture = self._img(img) if img else ""
        return "\n".join(p for p in (picture, f"*{caption}*" if caption else "") if p)

    def _img(self, img: Node) -> str:
        src = img.attrs.get("src") or img.attrs.get("resource") or ""
        alt = (img.attrs.get("alt") or "").replace("[", "").replace("]", "").replace("|", " ")
        try:
            width, height = int(img.attrs.get("width") or 0), int(img.attrs.get("height") or 0)
        except ValueError:
            width = height = 0
        return self.image(src, alt, width, height) if src else ""

    # -- inline

    def _inline_all(self, nodes: list[Node | str]) -> str:
        text = "".join(self._inline(n) for n in nodes)
        text = re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", "\n", text)
        text = re.sub(r"[ \t\r\f\v]{2,}", " ", text)
        # A soft newline in HTML is a space; a hard one was written as <br>.
        text = text.replace("\n", " ").replace("\x00BR\x00", "  \n")
        return re.sub(r" {2,}(?!\n)", " ", text).strip(" ")

    def _inline(self, node: Node | str) -> str:
        if isinstance(node, str):
            return re.sub(r"\s+", " ", node)
        tag = node.tag
        if self._skip(node):
            return ""
        if tag == "br":
            return "\x00BR\x00"
        if tag == "img":
            return self._img(node)
        if node.has_class("mwe-math-element") or tag == "math":
            return self._math(node)
        inner = "".join(self._inline(c) for c in node.children)
        if tag in ("b", "strong"):
            return f"**{inner.strip()}**" if inner.strip() else ""
        if tag in ("i", "em"):
            return f"*{inner.strip()}*" if inner.strip() else ""
        if tag in ("code", "tt", "kbd", "samp"):
            code = node.text()
            return f"`{code}`" if "`" not in code else f"`` {code} ``"
        if tag in ("sup", "sub"):
            return f"<{tag}>{inner}</{tag}>"
        if tag == "a":
            return self._anchor(node, inner)
        if tag in BLOCK:
            inner_blocks: list[Block] = []
            self._block(node, inner_blocks)
            return " ".join(b.text for b in inner_blocks)
        return inner

    def _anchor(self, node: Node, inner: str) -> str:
        href = node.attrs.get("href") or ""
        rel = node.attrs.get("rel") or ""
        label = inner.strip() or href
        if find(node, "img"):
            return inner
        if "mw:WikiLink" in rel and href.startswith("./"):
            target, _, fragment = urllib.parse.unquote(href[2:]).partition("#")
            return f"[{label}]({self.link(target.replace('_', ' '), fragment)})"
        if href.startswith("#"):
            return label
        if not href:
            return inner
        return f"[{label}]({href})"

    def _math(self, node: Node) -> str:
        tex = ""
        math = node if node.tag == "math" else find(node, "math")
        if math is not None:
            tex = math.attrs.get("alttext") or ""
            if not tex:
                for annotation in self._descendants(math, ("annotation",)):
                    tex = annotation.text()
        if not tex:
            img = find(node, "img")
            tex = img.attrs.get("alt", "") if img else node.text()
        tex = tex.strip()
        tex = re.sub(r"^\{\\displaystyle\s*(.*)\}$", r"\1", tex, flags=re.S).strip()
        block = node.has_class("mwe-math-element-block") or (math is not None and math.attrs.get("display") == "block")
        return f"\n$$\n{tex}\n$$\n" if block else f"${tex}$"


def parts(blocks: list[Block], limit: int = PART_LENGTH) -> list[str]:
    """The page cut into posts of at most ``limit`` characters, each part
    starting at a heading where it can."""
    pieces: list[Block] = []
    for block in blocks:
        # Cut a little short, so that the heading above can go with the first piece.
        pieces.extend(_split(block, limit - 200))
    out: list[str] = []
    current: list[Block] = []

    def size(blocks: list[Block]) -> int:
        return sum(len(b.text) for b in blocks) + 2 * max(len(blocks) - 1, 0)

    for piece in pieces:
        starts_section = piece.level in (1, 2) and size(current) > limit // 3
        if current and (size(current + [piece]) > limit or starts_section):
            # Headings at the end of a part go on with what they head.
            carried: list[Block] = []
            while current and current[-1].level and len(current) > 1:
                carried.insert(0, current.pop())
            if size(carried + [piece]) > limit:
                current, carried = current + carried, []
            out.append("\n\n".join(b.text for b in current))
            current = carried
        current.append(piece)
    if current:
        out.append("\n\n".join(b.text for b in current))
    return out


def _split(block: Block, limit: int) -> list[Block]:
    if len(block.text) <= limit:
        return [block]
    lines = block.text.split("\n")
    out: list[Block] = []
    current: list[str] = []
    size = 0
    head = block.table_head
    for line in lines:
        # A list is cut before one of its own items, never before a nested
        # line, which would open the next post indented, as code. The margin
        # parts() leaves lets a cut wait for the next such item.
        nested = line[:1].isspace()
        over = size + len(line) + 1 > limit
        if current and over and (not nested or size + len(line) + 1 > limit + 180):
            out.append(Block("\n".join(current)))
            current = head.split("\n") if head and not line.startswith("|---") else []
            size = sum(len(c) + 1 for c in current)
            if nested:
                line = line.lstrip()
        # A single line longer than a post is cut where it must be.
        while len(line) > limit:
            out.append(Block(line[:limit]))
            line = line[limit:]
        current.append(line)
        size += len(line) + 1
    if current:
        out.append(Block("\n".join(current)))
    return out


# ------------------------------------------------------------------ the wiki


class Wiki:
    def __init__(self, base: str, username: str, password: str) -> None:
        self.base = base
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        self.username, self.password = username, password

    def api(self, params: dict, post: bool = False) -> dict:
        params = {**params, "format": "json", "formatversion": "2"}
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.base}/api.php"
        resp = self.opener.open(url, data=data, timeout=60) if post else self.opener.open(
            f"{url}?{data.decode()}", timeout=60)
        answer = json.loads(resp.read().decode())
        if "error" in answer:
            raise RuntimeError(f"the wiki refused {params.get('action')}: {answer['error'].get('info')}")
        return answer

    def login(self) -> None:
        token = self.api({"action": "query", "meta": "tokens", "type": "login"})["query"]["tokens"]["logintoken"]
        if "@" in self.username:
            result = self.api({"action": "login", "lgname": self.username, "lgpassword": self.password,
                               "lgtoken": token}, post=True)["login"]
            if result.get("result") != "Success":
                raise RuntimeError(f"the wiki refused the login: {result.get('reason') or result.get('result')}")
        else:
            result = self.api({"action": "clientlogin", "username": self.username, "password": self.password,
                               "logintoken": token, "loginreturnurl": self.base + "/"}, post=True)["clientlogin"]
            if result.get("status") != "PASS":
                raise RuntimeError(f"the wiki refused the login: {result.get('message') or result.get('status')}")

    def pages(self, namespaces: tuple[int, ...]) -> dict[int, dict]:
        """Every page that is not a redirect: id -> title and latest revision."""
        found: dict[int, dict] = {}
        for ns in namespaces:
            cont: dict = {}
            while True:
                r = self.api({"action": "query", "generator": "allpages", "gapnamespace": ns, "gaplimit": "max",
                              "gapfilterredir": "nonredirects", "prop": "info", **cont})
                for p in r.get("query", {}).get("pages", []):
                    found[p["pageid"]] = {"title": p["title"], "revid": p["lastrevid"]}
                if "continue" not in r:
                    break
                cont = r["continue"]
        return found

    def redirects(self, namespaces: tuple[int, ...]) -> dict[str, str]:
        """Redirect title -> the title it leads to."""
        titles: list[str] = []
        for ns in namespaces:
            cont: dict = {}
            while True:
                r = self.api({"action": "query", "list": "allpages", "apnamespace": ns, "aplimit": "max",
                              "apfilterredir": "redirects", **cont})
                titles += [p["title"] for p in r.get("query", {}).get("allpages", [])]
                if "continue" not in r:
                    break
                cont = r["continue"]
        out: dict[str, str] = {}
        # The API resolves redirects for up to 50 titles a request.
        for start in range(0, len(titles), 50):
            r = self.api({"action": "query", "titles": "|".join(titles[start:start + 50]), "redirects": "1"})
            for hop in r.get("query", {}).get("redirects", []):
                out[hop["from"]] = hop["to"]
        return out

    def html(self, title: str) -> str:
        key = urllib.parse.quote(title.replace(" ", "_"), safe="")
        return self.opener.open(f"{self.base}/rest.php/v1/page/{key}/html", timeout=60).read().decode()

    def fetch(self, url: str) -> tuple[bytes, str]:
        with self.opener.open(url, timeout=60) as resp:
            return resp.read(MAX_UPLOAD_BYTES + 1), (resp.headers.get_content_type() or "")

    def page_url(self, title: str, fragment: str = "") -> str:
        key = urllib.parse.quote(title.replace(" ", "_"), safe="/:")
        return f"{self.base}/index.php/{key}" + (f"#{urllib.parse.quote(fragment)}" if fragment else "")


# ----------------------------------------------------------------- the forum


class Forum:
    def __init__(self, base: str, api_key: str, username: str, dry_run: bool = False) -> None:
        self.base, self.api_key, self.username, self.dry_run = base, api_key, username, dry_run

    def request(self, method: str, path: str, data: dict | None = None, body: bytes | None = None,
                content_type: str | None = None) -> dict:
        if self.dry_run and method != "GET":
            print(f"--- {method} {path}")
            if data:
                for key, value in data.items():
                    print(f"{key}: {value}" if key != "raw" else f"raw:\n{value}\n")
            return {"id": 0, "topic_id": 0, "short_url": "upload://dry-run.png"}
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            content_type = "application/x-www-form-urlencoded"
        req = urllib.request.Request(f"{self.base}{path}", data=body, method=method)
        req.add_header("Api-Key", self.api_key)
        req.add_header("Api-Username", self.username)
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 3:
                    time.sleep(int(exc.headers.get("Retry-After") or 0) or 2 ** (attempt + 2))
                    continue
                detail = exc.read().decode(errors="replace")[:400]
                raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc
        raise RuntimeError(f"{method} {path}: gave up after four tries")

    def topic_by_external_id(self, external_id: str) -> dict | None:
        try:
            return self.request("GET", f"/t/external_id/{external_id}.json?print=true")
        except RuntimeError as exc:
            if "-> 404" in str(exc):
                return None
            raise

    def upload(self, filename: str, content: bytes, mime: str) -> dict:
        if self.dry_run:
            print(f"--- upload {filename} ({len(content)} bytes)")
            return {"short_url": f"upload://{filename}"}
        boundary = "qupa" + uuid.uuid4().hex
        parts = []
        for name, value in (("type", "composer"), ("synchronous", "true")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                     f"Content-Type: {mime}\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", "/uploads.json", body=b"".join(parts),
                            content_type=f"multipart/form-data; boundary={boundary}")


# ----------------------------------------------------------------- mirroring


def topic_title(title: str) -> str:
    """The topic's title: the page's, marked as the wiki's. The mark also makes
    a short title long enough for the forum, and one the forum would judge
    unclear, such as "Waterjet Cutter", acceptable."""
    return f"{title} (lab wiki page)"


def opening(title: str, url: str, number: int, count: int) -> str:
    if number == 1:
        whole = "" if count == 1 else f" It is in {count} posts, of which this is the first."
        return (f"*This is the lab wiki's page [{title}]({url}), copied here. Edit it on the wiki, and this copy "
                f"follows within 15 minutes.{whole}*")
    return f"*Part {number} of {count} of the lab wiki's page [{title}]({url}).*"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Mirror:
    def __init__(self, wiki: Wiki, forum: Forum, state_path: Path, category_id: str) -> None:
        self.wiki, self.forum, self.state_path, self.category_id = wiki, forum, state_path, category_id
        try:
            self.state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("pages", {})
        self.state.setdefault("uploads", {})
        self.titles: dict[str, int] = {}
        self.redirects: dict[str, str] = {}

    def save(self) -> None:
        if self.forum.dry_run:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True))
        tmp.replace(self.state_path)

    # -- what a link and an image become

    def link(self, title: str, fragment: str) -> str:
        title = title[:1].upper() + title[1:]
        target = self.redirects.get(title, title)
        page_id = self.titles.get(target)
        if page_id is not None:
            return f"/t/external_id/{EXTERNAL_PREFIX}{page_id}"
        return self.wiki.page_url(title, fragment)

    def image(self, src: str, alt: str, width: int, height: int) -> str:
        url = urllib.parse.urljoin(self.wiki.base + "/index.php/x", src)
        size = f"|{width}x{height}" if width and height else ""
        held = self.state["uploads"].get(url)
        if held:
            return f"![{alt}{size}]({held})"
        try:
            content, mime = self.wiki.fetch(url)
        except (urllib.error.URLError, OSError) as exc:
            LOG.warning("cannot fetch the image %s: %s", url, exc)
            return f"[{alt or 'image'}]({url})"
        mime = mime if mime in IMAGE_TYPES else (mimetypes.guess_type(url)[0] or "")
        filename = urllib.parse.unquote(url.rsplit("/", 1)[-1]) or "image"
        if len(content) > MAX_UPLOAD_BYTES and mime in IMAGE_TYPES:
            # The wiki scales an image on request, to a size the forum takes.
            scaled = (f"{self.wiki.base}/index.php?title=Special:FilePath/{urllib.parse.quote(filename)}"
                      f"&width={SCALED_WIDTH}")
            try:
                content, scaled_mime = self.wiki.fetch(scaled)
                mime = scaled_mime if scaled_mime in IMAGE_TYPES else mime
            except (urllib.error.URLError, OSError, KeyError) as exc:
                LOG.warning("cannot fetch a scaled %s: %s", filename, exc)
        if len(content) > MAX_UPLOAD_BYTES or mime not in IMAGE_TYPES:
            return f"[{alt or 'image'}]({url})"
        try:
            short = self.forum.upload(filename, content, mime).get("short_url")
        except RuntimeError as exc:
            LOG.warning("the forum would not take %s: %s", filename, exc)
            return f"[{alt or 'image'}]({url})"
        if not short:
            return f"[{alt or 'image'}]({url})"
        self.state["uploads"][url] = short
        return f"![{alt}{size}]({short})"

    # -- one page

    def render(self, title: str) -> list[str]:
        blocks = Converter(self.link, self.image).blocks(self.wiki.html(title))
        return parts(blocks) or ["*This page is empty on the wiki.*"]

    def posts_for(self, title: str, chunks: list[str]) -> list[str]:
        url = self.wiki.page_url(title)
        return [f"{opening(title, url, i, len(chunks))}\n\n{chunk}" for i, chunk in enumerate(chunks, 1)]

    def mirror_page(self, page_id: int, page: dict) -> str:
        key = str(page_id)
        held = self.state["pages"].get(key)
        if held and held.get("revid") == page["revid"] and held.get("title") == page["title"] and not held.get("deleted"):
            return "unchanged"
        posts = self.posts_for(page["title"], self.render(page["title"]))
        if not held:
            held = self.adopt(page_id)
        if not held:
            return self.create(page_id, page, posts)
        return self.update(key, held, page, posts)

    def adopt(self, page_id: int) -> dict | None:
        """A topic the forum has for the page that the state file lost."""
        topic = self.forum.topic_by_external_id(f"{EXTERNAL_PREFIX}{page_id}")
        if not topic:
            return None
        ours = [p for p in (topic.get("post_stream") or {}).get("posts", []) if p.get("username") == self.forum.username]
        ours.sort(key=lambda p: p["post_number"])
        # The mirror's posts are the first ones in the topic, before anybody replied.
        run = []
        for p in ours:
            if p["post_number"] != len(run) + 1:
                break
            run.append(p["id"])
        held = {"topic_id": topic["id"], "posts": run, "digests": [], "title": topic.get("title"), "revid": None}
        self.state["pages"][str(page_id)] = held
        return held

    def create(self, page_id: int, page: dict, posts: list[str]) -> str:
        made = self.forum.request("POST", "/posts.json", {
            "title": topic_title(page["title"]), "raw": posts[0], "category": self.category_id,
            "external_id": f"{EXTERNAL_PREFIX}{page_id}",
        })
        topic_id = made.get("topic_id")
        ids = [made.get("id")]
        for raw in posts[1:]:
            time.sleep(POST_INTERVAL)
            ids.append(self.forum.request("POST", "/posts.json", {"topic_id": topic_id, "raw": raw}).get("id"))
        self.state["pages"][str(page_id)] = {"topic_id": topic_id, "posts": ids, "digests": [digest(p) for p in posts],
                                             "title": page["title"], "revid": page["revid"]}
        self.save()
        return f"created topic {topic_id} in {len(posts)} post(s)"

    def update(self, key: str, held: dict, page: dict, posts: list[str]) -> str:
        changed = 0
        topic_id = held["topic_id"]
        if held.get("deleted"):
            self.forum.request("PUT", f"/t/{topic_id}/status.json", {"status": "closed", "enabled": "false"})
            held["deleted"] = False
        if held.get("title") != page["title"]:
            self.forum.request("PUT", f"/t/-/{topic_id}.json", {"title": topic_title(page["title"])})
            changed += 1
        ids, digests = held["posts"], held.get("digests") or []
        for i, raw in enumerate(posts):
            if i < len(ids):
                if i < len(digests) and digests[i] == digest(raw):
                    continue
                self.forum.request("PUT", f"/posts/{ids[i]}.json",
                                   {"post[raw]": raw, "post[edit_reason]": "updated from the wiki"})
            else:
                # A page that grew past its posts goes on after any replies.
                ids.append(self.forum.request("POST", "/posts.json", {"topic_id": topic_id, "raw": raw}).get("id"))
            changed += 1
            time.sleep(POST_INTERVAL)
        for post_id in ids[len(posts):]:
            self.forum.request("PUT", f"/posts/{post_id}.json", {
                "post[raw]": f"*This part of the lab wiki's page [{page['title']}]({self.wiki.page_url(page['title'])}) "
                             "is gone: the page is shorter now.*",
                "post[edit_reason]": "updated from the wiki"})
            changed += 1
        held.update(posts=ids, digests=[digest(p) for p in posts] + ["gone"] * (len(ids) - len(posts)),
                    title=page["title"], revid=page["revid"])
        self.save()
        return f"updated topic {topic_id}: {changed} change(s)"

    def mark_deleted(self, key: str, held: dict) -> str:
        title = held.get("title") or "this page"
        first = held["posts"][0] if held.get("posts") else None
        if first:
            self.forum.request("PUT", f"/posts/{first}.json", {
                "post[raw]": f"*The lab wiki's page {title} has been deleted, so this copy is no longer kept up to "
                             "date. The replies below stay.*",
                "post[edit_reason]": "deleted on the wiki"})
        self.forum.request("PUT", f"/t/{held['topic_id']}/status.json", {"status": "closed", "enabled": "true"})
        held["deleted"] = True
        held["revid"] = None
        # The first post now holds the note, so a page restored rewrites it.
        if held.get("digests"):
            held["digests"][0] = "deleted"
        self.save()
        return f"closed topic {held['topic_id']}"

    # -- a run

    def run(self, only: str | None = None) -> list[str]:
        pages = self.wiki.pages(WIKI_NAMESPACES)
        self.titles = {p["title"]: page_id for page_id, p in pages.items()}
        self.redirects = self.wiki.redirects(WIKI_NAMESPACES)
        done = []
        for page_id, page in sorted(pages.items(), key=lambda item: item[1]["title"]):
            if only and page["title"] != only:
                continue
            try:
                outcome = self.mirror_page(page_id, page)
            except Exception as exc:  # noqa: BLE001 - one page must not stop the rest
                LOG.error("cannot mirror %s: %s", page["title"], exc, exc_info=LOG.isEnabledFor(logging.DEBUG))
                continue
            if outcome != "unchanged":
                LOG.info("%s: %s", page["title"], outcome)
                done.append(page["title"])
        if not only:
            for key, held in list(self.state["pages"].items()):
                if int(key) not in pages and not held.get("deleted"):
                    try:
                        LOG.info("%s: %s", held.get("title"), self.mark_deleted(key, held))
                    except Exception as exc:  # noqa: BLE001 - tried again at the next run
                        LOG.error("cannot close the topic of %s: %s", held.get("title"), exc)
                        continue
                    done.append(held.get("title") or key)
        return done


@contextlib.contextmanager
def one_run_at_a_time(state_path: Path) -> Iterator[None]:
    """Hold the lock beside the state file, so that the timer's run and the
    listener's never write the same topics at once. A run that finds the lock
    held waits for it."""
    if fcntl is None:
        yield
        return
    with open(state_path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def sync(dry_run: bool = False, only: str | None = None) -> list[str]:
    """One run: log in to the wiki, and write what changed."""
    with one_run_at_a_time(STATE_PATH):
        wiki = Wiki(WIKI_URL, WIKI_USERNAME, WIKI_PASSWORD)
        wiki.login()
        mirror = Mirror(wiki, Forum(FORUM_URL, API_KEY, API_USERNAME, dry_run=dry_run), STATE_PATH, CATEGORY_ID)
        done = mirror.run(only=only)
    LOG.info("%d page(s) written", len(done))
    return done


# ------------------------------------------------------- the wiki's notices


class Runs:
    """Runs started by notices, one at a time. Notices that arrive during a run
    start one more run after it, which sees what they announced."""

    def __init__(self, run: Callable[[], object], settle: float = HOOK_SETTLE) -> None:
        self.run, self.settle = run, settle
        self.pending = threading.Event()

    def notify(self) -> None:
        self.pending.set()

    def loop(self) -> None:
        while True:
            self.pending.wait()
            time.sleep(self.settle)
            self.pending.clear()
            try:
                self.run()
            except Exception as exc:  # noqa: BLE001 - the timer's next run is the backstop
                LOG.error("the run a notice started failed: %s", exc, exc_info=LOG.isEnabledFor(logging.DEBUG))


def verify_notice(raw: bytes, signature: str, secret: str, now: float) -> str:
    """Why the notice is refused, or "" if it is the wiki's and recent."""
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return "signature did not verify"
    try:
        signed_at = float(json.loads(raw)["at"])
    except (ValueError, KeyError, TypeError):
        return "no time in the notice"
    if abs(now - signed_at) > HOOK_MAX_AGE:
        return f"signed {now - signed_at:.0f} s from now"
    return ""


def hook_handler(secret: str, notify: Callable[[], None],
                 clock: Callable[[], float] = time.time) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str) -> None:
            payload = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
            length = int(self.headers.get("Content-Length") or 0)
            # Read before answering, up to a limit, so that the sender sees the
            # answer rather than a reset connection.
            raw = self.rfile.read(min(length, 16 * HOOK_MAX_BODY)) if length else b""
            if length > HOOK_MAX_BODY:
                self._send(413, "too long")
                return
            refused = verify_notice(raw, self.headers.get("X-Qupa-Signature", ""), secret, clock())
            if refused:
                LOG.warning("notice refused: %s", refused)
                self._send(403, "refused")
                return
            try:
                event = str(json.loads(raw).get("event") or "a change")
            except (ValueError, AttributeError):
                event = "a change"
            LOG.info("the wiki reported %s", event[:40])
            notify()
            self._send(202, "accepted")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's interface
            self._send(200, "ok") if self.path == "/health" else self._send(404, "not found")

        def log_message(self, fmt: str, *args) -> None:
            LOG.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def listen() -> None:
    """Answer the wiki's notices until stopped, each starting a run."""
    runs = Runs(sync)
    threading.Thread(target=runs.loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOOK_HOST, HOOK_PORT), hook_handler(HOOK_SECRET, runs.notify))
    LOG.info("listening for the wiki's notices on %s:%d", HOOK_HOST, HOOK_PORT)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="print what would be written, change nothing")
    parser.add_argument("--page", help="mirror only the page with this title")
    parser.add_argument("--listen", action="store_true", help="answer the wiki's notices, each starting a run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if os.environ.get("WIKI_DEBUG") else logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    missing = [name for name, value in (("WIKI_URL", WIKI_URL), ("WIKI_USERNAME", WIKI_USERNAME),
                                         ("WIKI_PASSWORD", WIKI_PASSWORD), ("WIKI_CATEGORY_ID", CATEGORY_ID))
               if not value] + ([] if args.dry_run or API_KEY else ["DISCOURSE_API_KEY"])
    if args.listen and len(HOOK_SECRET) < 16:
        missing.append("WIKI_HOOK_SECRET (16 characters or more)")
    if missing:
        print(f"ERROR: set {', '.join(missing)}.", file=sys.stderr)
        return 2
    if args.listen:
        listen()
        return 0
    sync(dry_run=args.dry_run, only=args.page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
