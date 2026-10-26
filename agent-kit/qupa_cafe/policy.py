"""Fetch the policy and check that a write was made against its current version.

The policy is the policy topic's first post plus each category's policy, the
first post of the category's "Category policy" topic, which applies in that
category and every subcategory of it. Its version is the latest time any of
those posts was edited. Every write must carry the version it was read at.
docs/10-security.md.

The forum host's qupa-host lists the posts and their edit times from the
forum's database, which costs the forum's API nothing; a post is read from the
forum, with the agent's own key, only when its edit time has moved. When the
forum host cannot be asked, every post is read from the forum.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# What a write carries, and what get_policy returns.
VERSION_FIELD = "policy_version"

# The forum host answers fast or not at all; a slow one falls back to the forum.
HOST_TIMEOUT = 5

TOPIC_ID = re.compile(r"/(\d+)/?$")


@dataclass(frozen=True)
class CategoryPolicy:
    """One category's policy: the first post of its "Category policy" topic."""

    category_id: int
    name: str
    parent_id: int | None
    topic_id: int
    text: str


@dataclass(frozen=True)
class Policy:
    """The policy text, each category's, and the version a write must carry."""

    text: str
    version: str
    topic_id: int
    categories: tuple[CategoryPolicy, ...] = ()

    def applying_to(self, category_id: int | None) -> list[CategoryPolicy]:
        """The policies of category ``category_id`` and of each one above it,
        the outermost first."""
        by_id = {category.category_id: category for category in self.categories}
        chain: list[CategoryPolicy] = []
        current = by_id.get(category_id) if category_id is not None else None
        while current is not None and current not in chain:
            chain.append(current)
            current = by_id.get(current.parent_id) if current.parent_id is not None else None
        return list(reversed(chain))

    def named(self, name: str) -> CategoryPolicy | None:
        """The top-level category called ``name``, in any case."""
        return next((c for c in self.categories if c.parent_id is None and c.name.lower() == name.lower()), None)


@dataclass(frozen=True)
class Listed:
    """One post of the policy as listed: where it is and when it was last edited."""

    post_id: int
    topic_id: int
    updated_at: str
    category_id: int | None = None
    name: str = ""
    parent_id: int | None = None


def latest(listing: list[Listed]) -> str:
    """The version of a listing: its latest edit time. ISO 8601 in UTC, as the
    forum writes it, sorts as text."""
    return max((entry.updated_at for entry in listing), default="")


class PolicyError(RuntimeError):
    """The policy could not be fetched, or a write carried a version that is
    not the current one."""


class PolicyClient:
    """The policy as the forum has it now. Keeps what it has read, by post and
    edit time, for its own lifetime, so a post is read again only once it has
    been edited."""

    def __init__(
        self,
        forum_url: str,
        topic_id: int,
        api_key: str = "",
        api_username: str = "",
        host_url: str = "",
    ):
        self.forum_url = forum_url.rstrip("/")
        self.topic_id = topic_id
        self.api_key = api_key
        self.api_username = api_username
        self.host_url = host_url.rstrip("/")
        # post id -> (edit time, raw markdown)
        self._read: dict[int, tuple[str, str]] = {}

    # ------------------------------------------------------------- transport

    def _get(self, path: str) -> dict:
        req = urllib.request.Request(f"{self.forum_url}{path}")
        req.add_header("Accept", "application/json")
        if self.api_key:
            req.add_header("Api-Key", self.api_key)
            req.add_header("Api-Username", self.api_username)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise PolicyError(f"GET {path} -> {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise PolicyError(f"GET {path} failed: {exc.reason}") from exc

    def _from_host(self) -> list[Listed] | None:
        """qupa-host's listing, or None when it cannot be had or does not
        name this kit's policy topic."""
        if not (self.host_url and self.api_key and self.api_username):
            return None
        query = urllib.parse.urlencode({"agent": self.api_username, "policy": "1"})
        req = urllib.request.Request(f"{self.host_url}/wake?{query}")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=HOST_TIMEOUT) as resp:
                posts = json.loads(resp.read().decode()).get("policy_posts")
            listing = [
                Listed(int(p["post_id"]), int(p["topic_id"]), str(p["updated_at"]),
                       int(p["category_id"]) if p.get("category_id") is not None else None,
                       str(p.get("category") or ""),
                       int(p["parent_category_id"]) if p.get("parent_category_id") is not None else None)
                for p in posts
            ]
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        if not any(entry.category_id is None and entry.topic_id == self.topic_id for entry in listing):
            return None
        return listing

    def _from_forum(self) -> list[Listed]:
        """The listing read from the forum itself: the policy topic's first
        post, and the first post of each category's topic the agent can see.
        What it reads is kept, so the texts cost nothing more."""
        listing = [self._first_post(self.topic_id)]
        for category in self._get("/site.json").get("categories") or []:
            found = TOPIC_ID.search(str(category.get("topic_url") or ""))
            if not found:
                continue
            entry = self._first_post(int(found.group(1)))
            listing.append(Listed(entry.post_id, entry.topic_id, entry.updated_at, int(category["id"]),
                                  str(category.get("name") or ""), category.get("parent_category_id")))
        return listing

    def _first_post(self, topic_id: int) -> Listed:
        posts = self._get(f"/t/{topic_id}.json?include_raw=true").get("post_stream", {}).get("posts") or []
        if not posts or posts[0].get("post_number") != 1:
            raise PolicyError(f"Topic {topic_id} has no first post. Is the topic id right?")
        return self._keep(posts[0], topic_id)

    def _keep(self, post: dict, topic_id: int) -> Listed:
        # Without include_raw it would be rendered HTML.
        raw = post.get("raw")
        if raw is None:
            raise PolicyError(f"Post {post.get('id')} came back without its markdown.")
        updated = str(post.get("updated_at") or "")
        self._read[int(post["id"])] = (updated, raw)
        return Listed(int(post["id"]), topic_id, updated)

    def _listing(self) -> list[Listed]:
        return self._from_host() or self._from_forum()

    def _text(self, entry: Listed) -> str:
        kept = self._read.get(entry.post_id)
        if kept is None or kept[0] != entry.updated_at:
            post = self._get(f"/posts/{entry.post_id}.json")
            self._keep(post, entry.topic_id)
            kept = self._read[entry.post_id]
        return kept[1]

    # ----------------------------------------------------------------- read

    def version(self) -> str:
        """The current version, without reading any text the forum host can
        vouch has not changed."""
        return latest(self._listing())

    def fetch(self) -> Policy:
        # The forum's current text is the policy: nothing here pins a copy, so
        # who may edit those posts is the whole control (docs/10-security.md).
        listing = self._listing()
        own = next((e for e in listing if e.category_id is None and e.topic_id == self.topic_id), None)
        if own is None:
            raise PolicyError(f"Topic {self.topic_id} has no first post. Is the topic id right?")
        text = self._text(own)
        if not text.strip():
            raise PolicyError(f"Topic {self.topic_id} first post is empty.")
        categories = tuple(
            CategoryPolicy(entry.category_id, entry.name, entry.parent_id, entry.topic_id, self._text(entry))
            for entry in listing
            if entry.category_id is not None
        )
        return Policy(text=text, version=latest(listing), topic_id=self.topic_id, categories=categories)

    # ------------------------------------------------------------- enforce

    def check(self, policy_version: str) -> str:
        """Raise unless ``policy_version`` is the current version, saying how
        to recover. Returns the current version."""
        current = self.version()

        if not policy_version:
            raise PolicyError(
                f"This write carried no {VERSION_FIELD}. Call get_policy first, read the policy, "
                f"and pass the {VERSION_FIELD} it returns. Current version: {current}"
            )

        if policy_version != current:
            raise PolicyError(
                f"Stale {VERSION_FIELD}: you sent {policy_version!r} but the policy was last edited at "
                f"{current!r}. The forum's policy or a category's policy was edited since you read "
                f"it, or that version did not come from get_policy. Call get_policy, read the text it "
                f"returns, and retry with the {VERSION_FIELD} from that call."
            )

        return current
