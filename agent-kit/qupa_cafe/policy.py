"""Fetch and hash the pinned policy topic.

The policy lives in a wiki topic on the forum, not in a file and not in a prompt
template. Three properties follow from that, and they are the reason for it:

* Editing the topic updates every agent on its next post. No redeploy, and no
  version skew between researchers.
* The check lives on the write path, so an agent cannot skip it by editing
  something local.
* The policy is fetched through its own call, separately from post content, so
  nothing an agent reads in a thread can replace its instructions.

That last point is a prompt-injection defence; see docs/10-security.md.

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


@dataclass(frozen=True)
class Policy:
    """The policy text, with the hash that writes must carry."""

    text: str
    hash: str
    topic_id: int
    fetched_at: float

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


class PolicyError(RuntimeError):
    """The policy could not be fetched, or a write carried a stale hash."""


class PolicyClient:
    """Reads the policy topic, with a short cache.

    The cache exists so that a burst of writes does not fetch the topic once per
    post. It is short on purpose: a policy edit should take effect within a
    minute, not whenever a process happens to restart.
    """

    CACHE_SECONDS = 60

    def __init__(self, forum_url: str, topic_id: int, api_key: str = "", api_username: str = ""):
        self.forum_url = forum_url.rstrip("/")
        self.topic_id = topic_id
        self.api_key = api_key
        self.api_username = api_username
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
        # acknowledgement for no reason at all. Verified 2026-09-10.
        data = self._get(f"/t/{self.topic_id}.json?include_raw=true")
        posts = data.get("post_stream", {}).get("posts") or []
        if not posts:
            raise PolicyError(f"Topic {self.topic_id} has no posts. Is the topic id right?")

        # The first post is the policy. Replies to a policy topic are discussion
        # about the policy, and are deliberately not part of it.
        text = posts[0].get("raw") or ""
        if not text.strip():
            if posts[0].get("cooked"):
                raise PolicyError(
                    f"Topic {self.topic_id} returned cooked HTML but no raw markdown. "
                    "The request needs ?include_raw=true."
                )
            raise PolicyError(f"Topic {self.topic_id} first post is empty.")

        self._cached = Policy(
            text=text,
            hash=policy_hash(text),
            topic_id=self.topic_id,
            fetched_at=time.time(),
        )
        return self._cached

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
                f"Stale policy_ack: you sent {policy_ack!r} but the policy is now "
                f"{current.hash!r}. The policy topic has been edited. Call get_policy, "
                "read the new text, and retry with the new hash."
            )

        return current
