"""Fetch and hash the policy topic.

The policy is the topic's first post plus each category's agent policy (a staff
wiki reply opening "Agent policy: <category>"), fetched through its own call so
nothing read in a thread can replace it. Every write must carry the hash of
that text. docs/10-security.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


CATEGORY_POLICY_HEADING = re.compile(r"\A#{1,6}[ \t]+Agent policy:[ \t]*(?P<name>.+?)[ \t]*#*[ \t]*$", re.I | re.M)

POSTS_PER_REQUEST = 20


@dataclass(frozen=True)
class CategoryPolicy:
    """The rules one category adds, from a reply in the policy topic."""

    name: str
    post_number: int
    text: str


@dataclass(frozen=True)
class Policy:
    """The policy text, with the hash that writes must carry."""

    text: str
    hash: str
    topic_id: int
    fetched_at: float
    categories: tuple[CategoryPolicy, ...] = ()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


def policy_hash(text: str) -> str:
    """Hash the policy text, insensitive to whitespace-only edits."""
    normalised = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def hashed_text(text: str, categories: tuple[CategoryPolicy, ...] = ()) -> str:
    """What the hash covers: the policy, then each category's rules in topic order."""
    parts = [text]
    for category in sorted(categories, key=lambda c: c.post_number):
        parts.append(f"## Agent policy: {category.name}\n\n{category.text}")
    return "\n\n".join(parts)


def category_policy(post: dict) -> CategoryPolicy | None:
    """The category policy a reply carries, if it is one. Only a staff wiki
    post counts, so that who may write one is who may write the policy."""
    if (post.get("post_number") or 0) < 2 or not post.get("wiki") or not post.get("staff"):
        return None
    raw = (post.get("raw") or "").strip()
    found = CATEGORY_POLICY_HEADING.match(raw)
    if not found:
        return None
    return CategoryPolicy(found.group("name"), int(post["post_number"]), raw[found.end():].strip())


class PolicyError(RuntimeError):
    """The policy could not be fetched, or a write carried a stale hash."""


class PolicyClient:
    """Reads the policy topic, cached for a minute so a burst of writes does
    not refetch it."""

    CACHE_SECONDS = 60

    def __init__(
        self,
        forum_url: str,
        topic_id: int,
        api_key: str = "",
        api_username: str = "",
    ):
        self.forum_url = forum_url.rstrip("/")
        self.topic_id = topic_id
        self.api_key = api_key
        self.api_username = api_username
        # The forum's current text is the policy: nothing here pins a copy, so
        # who may edit the topic is the whole control (docs/10-security.md).
        self._cached: Policy | None = None

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

    # ----------------------------------------------------------------- read

    def fetch(self, force: bool = False) -> Policy:
        if self._cached and not force and self._cached.age_seconds < self.CACHE_SECONDS:
            return self._cached

        # Without include_raw the hash would be over rendered HTML.
        data = self._get(f"/t/{self.topic_id}.json?include_raw=true")
        posts = data.get("post_stream", {}).get("posts") or []
        if not posts:
            raise PolicyError(f"Topic {self.topic_id} has no posts. Is the topic id right?")

        # Other replies are discussion, not policy.
        text = posts[0].get("raw") or ""
        if not text.strip():
            if posts[0].get("cooked"):
                raise PolicyError(
                    f"Topic {self.topic_id} returned cooked HTML but no raw markdown. "
                    "The request needs ?include_raw=true."
                )
            raise PolicyError(f"Topic {self.topic_id} first post is empty.")

        categories = tuple(
            found for post in self._all_posts(data, posts) if (found := category_policy(post))
        )
        self._cached = Policy(
            text=text,
            hash=policy_hash(hashed_text(text, categories)),
            topic_id=self.topic_id,
            fetched_at=time.time(),
            categories=categories,
        )
        return self._cached

    def _all_posts(self, data: dict, posts: list[dict]) -> list[dict]:
        """Every post in the topic, past the first page too. A failure fails the
        fetch rather than checking writes against part of the rules."""
        seen = {post.get("id") for post in posts}
        missing = [i for i in data.get("post_stream", {}).get("stream") or [] if i not in seen]
        posts = list(posts)
        for start in range(0, len(missing), POSTS_PER_REQUEST):
            query = urllib.parse.urlencode(
                [("include_raw", "true")] + [("post_ids[]", i) for i in missing[start:start + POSTS_PER_REQUEST]]
            )
            page = self._get(f"/t/{self.topic_id}/posts.json?{query}")
            posts.extend(page.get("post_stream", {}).get("posts") or [])
        return posts

    # ------------------------------------------------------------- enforce

    def check(self, policy_ack: str) -> Policy:
        """Raise unless ``policy_ack`` matches the current hash, saying how to recover."""
        current = self.fetch(force=True)

        if not policy_ack:
            raise PolicyError(
                "This write carried no policy_ack. Call get_policy first, read the "
                f"policy, and pass its hash. Current hash: {current.hash}"
            )

        if policy_ack != current.hash:
            raise PolicyError(
                f"Stale policy_ack: you sent {policy_ack!r} but the policy hashes to "
                f"{current.hash!r}. Either the policy or a category's agent policy "
                "was edited since you read it, or that hash did not come from get_policy. Call get_policy, read "
                "the text it returns, and retry with the hash from that call."
            )

        return current
