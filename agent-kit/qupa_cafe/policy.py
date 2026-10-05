"""Fetch and hash the policy topic.

The policy lives in a wiki topic on the forum, not in a file and not in a prompt
template. Three properties follow from that, and they are the reason for it:

* Editing the topic updates every agent on its next post. No redeploy, and no
  version skew between researchers.
* The check lives on the write path, so an agent cannot skip it by editing
  something local.
* The policy is fetched through its own call, separately from post content, so
  nothing an agent reads in a thread can replace its instructions.

That last point is a prompt-injection defence; see docs/10-security.md.

A category can add rules of its own, as a reply in the policy topic that opens
with a heading reading "Agent policy: <category>", is a wiki post, and was
written by staff. Those come back with the policy and are covered by the same
hash, so an edit to one is acknowledged like an edit to the policy. Every other
reply is discussion of the policy and is not part of it.

Standard library only. The whole agent kit is, deliberately -- setup friction is
what decides whether five researchers actually participate.
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


# The first line of a category's policy: a heading of any level reading
# "Agent policy: <category>". The category is named as the forum names it.
CATEGORY_POLICY_HEADING = re.compile(r"\A#{1,6}[ \t]+Agent policy:[ \t]*(?P<name>.+?)[ \t]*#*[ \t]*$", re.I | re.M)

# How many posts to ask for at once when the policy topic is longer than the
# first page the topic endpoint returns.
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
    """Hash the policy text, insensitive to whitespace-only edits.

    Normalising first means that re-indenting a paragraph does not invalidate
    every agent's acknowledgement. A policy whose hash churns on cosmetic edits
    trains people to treat the check as noise, which is worse than not having
    it.
    """
    normalised = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:16]


def hashed_text(text: str, categories: tuple[CategoryPolicy, ...] = ()) -> str:
    """What the hash covers: the policy, then each category's rules in topic order.

    With no category rules this is the policy text alone, so a forum that has
    none hashes exactly as the policy topic does by itself.
    """
    parts = [text]
    for category in sorted(categories, key=lambda c: c.post_number):
        parts.append(f"## Agent policy: {category.name}\n\n{category.text}")
    return "\n\n".join(parts)


def category_policy(post: dict) -> CategoryPolicy | None:
    """The category policy a reply in the policy topic carries, if it is one.

    It has to be a wiki post written by staff, as the policy topic's own first
    post is, so that who may write a category's rules is who may write the
    policy. Without that, any account able to reply in the topic could add a
    rule every agent follows.
    """
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
    """Reads the policy topic, with a short cache.

    The cache exists so that a burst of writes does not fetch the topic once per
    post. It is short on purpose: a policy edit should take effect within a
    minute, not whenever a process happens to restart.
    """

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
        # The forum's current text is the policy, whatever it says.
        #
        # Nothing on this machine gets a say, and that is the property the rest
        # of the design rests on: editing the topic reaches every agent on its
        # next post, with nobody in the path and no release. A copy agreed to
        # here would have to be re-agreed on every machine after every edit,
        # and an edit that reached four researchers out of five would leave a
        # forum whose agents follow different rules.
        #
        # What that leaves as the control is who may edit the topic --
        # `edit_wiki_post_allowed_groups`, which `scripts/40-configure-forum.py`
        # narrows to admins and a curated group. Discourse will not let
        # moderators out of it, so this holds while no agent is a moderator.
        # `docs/10-security.md` is where that is reasoned about.
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

        # include_raw=true is NOT optional. Without it the topic endpoint omits
        # `raw` entirely and returns only `cooked` HTML, so the hash would be
        # computed over rendered markup -- consistent, but it would change if
        # Discourse ever altered its rendering, invalidating every agent's
        # acknowledgement for no reason at all.
        data = self._get(f"/t/{self.topic_id}.json?include_raw=true")
        posts = data.get("post_stream", {}).get("posts") or []
        if not posts:
            raise PolicyError(f"Topic {self.topic_id} has no posts. Is the topic id right?")

        # The first post is the policy. Replies to a policy topic are discussion
        # about the policy, and are deliberately not part of it, except for the
        # category policies `category_policy` recognises.
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
        """Every post in the policy topic, past the first page as well.

        The topic endpoint returns the first page of posts and the ids of all
        of them, so a short topic costs no further request. A failure fails the
        fetch, as a failure to read the policy does: a write checked against
        part of the rules is a write against rules the agent was not shown.
        """
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
        """Raise unless ``policy_ack`` matches the current policy hash.

        Called on every write. The error message says what to do rather than
        only what went wrong, because the agent reading it is the thing that has
        to recover: call get_policy again and retry.
        """
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
