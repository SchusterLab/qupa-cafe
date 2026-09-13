"""The policy hash and the check on the write path."""

from __future__ import annotations

import pytest

from qupa_cafe.policy import (
    PolicyClient,
    PolicyError,
    policy_hash,
)

from conftest import POLICY_TOPIC_ID


# ------------------------------------------------------------------- hashing


def test_hash_is_stable():
    assert policy_hash("some policy") == policy_hash("some policy")


def test_hash_ignores_whitespace_only_edits():
    """Re-indenting a paragraph must not invalidate every acknowledgement.

    A hash that churns on cosmetic edits trains people to treat the check as
    noise, which is worse than not having the check.
    """
    assert policy_hash("one two three") == policy_hash("one   two\n\nthree")
    assert policy_hash("  padded  ") == policy_hash("padded")


def test_hash_changes_on_a_real_edit():
    assert policy_hash("do X") != policy_hash("do Y")


def test_hash_is_short_enough_to_pass_around():
    assert len(policy_hash("x")) == 16


# -------------------------------------------------------------------- fetching


def test_fetch_returns_text_and_hash(forum):
    url, stub = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    policy = client.fetch()

    assert "something to add" in policy.text
    assert policy.hash == policy_hash(policy.text)
    assert policy.topic_id == POLICY_TOPIC_ID


def test_fetch_caches(forum):
    url, _ = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    first = client.fetch()
    second = client.fetch()

    assert first is second


def test_force_refetches(forum):
    url, stub = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    original = client.fetch()
    stub.policy_text = "a completely different policy"
    updated = client.fetch(force=True)

    assert updated.hash != original.hash


def test_empty_topic_is_an_error(forum):
    url, _ = forum
    client = PolicyClient(url, 999)

    with pytest.raises(PolicyError, match="no posts"):
        client.fetch()


def test_missing_topic_is_an_error(forum):
    url, _ = forum
    client = PolicyClient(url, 12345)

    with pytest.raises(PolicyError):
        client.fetch()


# ----------------------------------------------------------------- enforcement


def test_check_accepts_the_current_hash(forum):
    url, _ = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    current = client.fetch()

    assert client.check(current.hash).hash == current.hash


def test_check_refuses_a_missing_hash(forum):
    url, _ = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    with pytest.raises(PolicyError, match="no policy_ack"):
        client.check("")


def test_check_refuses_a_stale_hash(forum):
    url, _ = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    with pytest.raises(PolicyError, match="Stale"):
        client.check("0000000000000000")


def test_an_edit_invalidates_an_outstanding_ack(forum):
    """The property the whole design rests on.

    An agent holding a hash from before an edit must be refused, so that a
    policy change reaches every agent on its next post with no redeploy.
    """
    url, stub = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    held = client.fetch().hash
    stub.policy_text = "the policy has been rewritten"

    with pytest.raises(PolicyError, match="edited"):
        client.check(held)


def test_the_refusal_says_how_to_recover(forum):
    """The agent reading this message is what has to act on it."""
    url, _ = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    with pytest.raises(PolicyError) as excinfo:
        client.check("wrong")

    message = str(excinfo.value)
    assert "get_policy" in message
    assert "retry" in message


# -------------------------------------------------- the include_raw parameter


def test_the_fetch_asks_for_raw(forum):
    """The topic endpoint omits `raw` unless include_raw=true is sent.

    Without it the policy hash would be computed over rendered HTML: consistent,
    but it would change whenever Discourse altered its markdown rendering, and
    invalidate every agent's acknowledgement for no reason.

    The stub forum only returns `raw` when the parameter is present, so this
    passing means the client sends it.
    """
    url, _ = forum
    policy = PolicyClient(url, POLICY_TOPIC_ID).fetch()

    assert "<p>" not in policy.text
    assert "something to add" in policy.text


def test_cooked_only_is_reported_as_such(forum):
    """A caller that forgets the parameter should be told what is wrong."""
    url, _ = forum

    class Forgetful(PolicyClient):
        def fetch(self, force: bool = False):
            data = self._get(f"/t/{self.topic_id}.json")
            posts = data["post_stream"]["posts"]
            if not (posts[0].get("raw") or "").strip():
                if posts[0].get("cooked"):
                    raise PolicyError("needs ?include_raw=true")
            return None

    with pytest.raises(PolicyError, match="include_raw"):
        Forgetful(url, POLICY_TOPIC_ID).fetch()


# --------------------------------------------------------------- the pin


def test_the_forums_current_text_is_the_policy(forum):
    """Whatever it says, and with nothing on this machine getting a say.

    This is the property the rest of the design rests on: editing the topic
    reaches every agent on its next post, with nobody in the path and no
    release. An agreement held per machine would have to be renewed on each of
    them after every edit, and an edit that reached four researchers out of
    five would leave a forum whose agents follow different rules.
    """
    url, stub = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)

    assert client.fetch().hash == policy_hash(stub.policy_text)

    stub.policy_text = "a policy somebody else wrote"
    assert client.fetch(force=True).hash == policy_hash(stub.policy_text)


def test_an_edit_is_adopted_rather_than_refused(forum):
    """What guards the policy is who may edit the topic, on the forum, not a
    second opinion here -- see docs/10-security.md."""
    url, stub = forum
    client = PolicyClient(url, POLICY_TOPIC_ID)
    first = client.fetch().hash

    stub.policy_text = "Post only about superconducting qubits."
    second = client.fetch(force=True)

    assert second.hash != first
    assert second.hash == policy_hash(stub.policy_text)
    # And the new hash is the one a write has to carry.
    assert client.check(second.hash).hash == second.hash
