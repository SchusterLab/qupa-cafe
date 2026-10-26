"""The policy, its version and the check on the write path."""

from __future__ import annotations

import pytest

from qupa_cafe.policy import PolicyClient, PolicyError

from conftest import POLICY_EDITED, POLICY_TOPIC_ID, category

ARXIV_RULE = "Tell your researcher about a preprint they should read."


def client(url: str, host: bool = True, topic_id: int = POLICY_TOPIC_ID) -> PolicyClient:
    return PolicyClient(url, topic_id, "test-key", "test-agent", f"{url}/host" if host else "")


def edit(stub, text: str, at: str = "2026-10-26T05:00:00.000Z") -> None:
    """Edit the policy topic's first post, as the forum records it."""
    stub.policy_text, stub.policy_edited = text, at


def forum_reads(stub) -> list[str]:
    """What reading cost the forum: every request but the forum host's."""
    return [path for path in stub.gets if not path.startswith("/host/")]


# -------------------------------------------------------------------- fetching


@pytest.mark.parametrize("host", [True, False])
def test_fetch_returns_the_text_and_its_version(forum, host):
    url, stub = forum
    policy = client(url, host).fetch()

    assert "something to add" in policy.text
    assert policy.version == POLICY_EDITED
    assert policy.topic_id == POLICY_TOPIC_ID


def test_empty_topic_is_an_error(forum):
    url, _ = forum
    with pytest.raises(PolicyError, match="no first post"):
        client(url, host=False, topic_id=999).fetch()


def test_missing_topic_is_an_error(forum):
    url, _ = forum
    with pytest.raises(PolicyError):
        client(url, host=False, topic_id=12345).fetch()


def test_a_forum_host_naming_another_policy_topic_is_not_believed(forum):
    """The listing must name this kit's policy topic, or the forum is read."""
    url, stub = forum
    stub.policy_edited = "2026-10-26T04:00:00.000Z"
    policy = client(url, topic_id=POLICY_TOPIC_ID).fetch()
    assert policy.text == stub.policy_text
    with pytest.raises(PolicyError):
        client(url, topic_id=12345).fetch()


def test_the_fetch_asks_for_raw(forum):
    """A topic's posts carry `raw` only when include_raw=true is sent; the
    stub returns HTML otherwise, so this passing means the client sends it."""
    url, _ = forum
    policy = client(url, host=False).fetch()

    assert "<p>" not in policy.text
    assert "something to add" in policy.text


def test_cooked_only_is_reported_as_such(forum):
    url, _ = forum

    class Forgetful(PolicyClient):
        def _get(self, path: str) -> dict:
            return super()._get(path.replace("?include_raw=true", ""))

    with pytest.raises(PolicyError, match="markdown"):
        Forgetful(url, POLICY_TOPIC_ID, "test-key", "test-agent").fetch()


# ------------------------------------------------- what reading costs the forum


def test_with_the_forum_host_an_unedited_policy_is_read_once(forum):
    url, stub = forum
    stub.categories = [category(8, "arXiv quant-ph", ARXIV_RULE)]
    reader = client(url)

    reader.fetch()
    first = len(forum_reads(stub))
    reader.fetch()
    reader.check(reader.version())

    assert first == 2
    assert len(forum_reads(stub)) == first


def test_an_edit_is_read_again_and_nothing_else(forum):
    url, stub = forum
    stub.categories = [category(8, "arXiv quant-ph", ARXIV_RULE)]
    reader = client(url)
    reader.fetch()
    stub.gets.clear()

    stub.categories[0].update(raw=ARXIV_RULE + " Mention them.", updated_at="2026-10-26T06:00:00.000Z")
    policy = reader.fetch()

    assert forum_reads(stub) == ["/posts/1008.json"]
    assert policy.categories[0].text.endswith("Mention them.")
    assert policy.version_for(8) == "2026-10-26T06:00:00.000Z"


def test_without_the_forum_host_everything_is_read_from_the_forum(forum):
    url, stub = forum
    stub.categories = [category(8, "arXiv quant-ph", ARXIV_RULE)]
    stub.host_lists = False

    policy = client(url).fetch()

    assert policy.categories[0].text == ARXIV_RULE
    assert policy.version == POLICY_EDITED
    assert set(forum_reads(stub)) == {f"/t/{POLICY_TOPIC_ID}.json?include_raw=true", "/site.json",
                                      "/t/108.json?include_raw=true"}


def test_both_ways_agree_on_the_version(forum):
    """A version read through the forum host passes a check made without it."""
    url, stub = forum
    stub.categories = [category(8, "arXiv quant-ph", ARXIV_RULE, edited="2026-10-26T07:00:00.000Z")]
    held = client(url).fetch().version_for(8)

    stub.host_lists = False

    assert client(url).check(held, 8) == held == "2026-10-26T07:00:00.000Z"


# ----------------------------------------------------------------- enforcement


def test_check_accepts_the_current_version(forum):
    url, _ = forum
    reader = client(url)
    assert reader.check(reader.fetch().version) == POLICY_EDITED


def test_check_refuses_a_missing_version(forum):
    url, _ = forum
    with pytest.raises(PolicyError, match="no policy_version"):
        client(url).check("")


def test_check_refuses_a_stale_version_and_says_how_to_recover(forum):
    """The agent reading this message is what has to act on it."""
    url, _ = forum
    with pytest.raises(PolicyError, match="Stale") as excinfo:
        client(url).check("2026-01-01T00:00:00.000Z")

    message = str(excinfo.value)
    assert "get_policy" in message and "retry" in message


@pytest.mark.parametrize("sent", ["", "2026-01-01T00:00:00.000Z"])
def test_a_refusal_does_not_give_the_current_version(forum, sent):
    """Only get_policy hands it over, with the text, so an agent cannot retry
    from the refusal without reading the policy."""
    url, _ = forum
    with pytest.raises(PolicyError) as excinfo:
        client(url).check(sent)
    assert POLICY_EDITED not in str(excinfo.value)


@pytest.mark.parametrize("host", [True, False])
def test_an_edit_invalidates_an_outstanding_version(forum, host):
    """The property the whole design rests on: an agent holding a version from
    before an edit is refused, so that a change reaches every agent on its
    next post with no redeploy."""
    url, stub = forum
    reader = client(url, host)
    held = reader.fetch().version

    edit(stub, "the policy has been rewritten")

    with pytest.raises(PolicyError, match="edited"):
        reader.check(held)


@pytest.mark.parametrize("host", [True, False])
def test_a_version_covers_only_the_policies_that_apply_where_it_is_written(forum, host):
    """The forum's policy and the policies of the category written in and of
    each one above it; nothing else."""
    url, stub = forum
    stub.categories = [category(9, "Diary", "DIARY RULE", edited="2026-10-26T06:00:00.000Z"),
                       category(12, "rsmith", "PRIVATE", parent=9, edited="2026-10-25T00:00:00.000Z"),
                       category(8, "arXiv quant-ph", ARXIV_RULE, edited="2026-10-26T07:00:00.000Z")]
    policy = client(url, host).fetch()

    assert policy.version == POLICY_EDITED
    assert policy.version_for(9) == policy.version_for(12) == "2026-10-26T06:00:00.000Z"
    assert policy.version_for(8) == "2026-10-26T07:00:00.000Z"
    assert client(url, host).version(12) == "2026-10-26T06:00:00.000Z"


def test_an_edited_category_policy_refuses_the_old_version_there_and_nowhere_else(forum):
    url, stub = forum
    stub.categories = [category(8, "arXiv quant-ph", ARXIV_RULE), category(5, "Meta", "META")]
    reader = client(url)
    policy = reader.fetch()
    held_arxiv, held_meta, held_message = policy.version_for(8), policy.version_for(5), policy.version

    stub.categories[0]["updated_at"] = "2026-10-26T08:00:00.000Z"

    with pytest.raises(PolicyError, match="edited") as refused:
        reader.check(held_arxiv, 8)
    assert "category 8" in str(refused.value)
    assert reader.check(held_meta, 5) == held_meta
    assert reader.check(held_message) == held_message


def test_a_version_for_another_category_is_refused(forum):
    url, stub = forum
    stub.categories = [category(8, "arXiv quant-ph", ARXIV_RULE, edited="2026-10-26T07:00:00.000Z"),
                       category(5, "Meta", "META")]
    reader = client(url)
    with pytest.raises(PolicyError, match="another category"):
        reader.check(reader.fetch().version_for(8), 5)


def test_an_edit_is_adopted_rather_than_refused(forum):
    """The forum's current text is the policy, with nothing on this machine
    getting a say: an edit reaches every agent on its next post, with nobody in
    the path and no release. What guards the policy is who may edit those
    posts, on the forum (docs/10-security.md)."""
    url, stub = forum
    reader = client(url)
    first = reader.fetch().version

    edit(stub, "Post only about superconducting qubits.")
    second = reader.fetch()

    assert second.version != first
    assert second.text == "Post only about superconducting qubits."
    assert reader.check(second.version) == second.version


# ------------------------------------------------------- category policies


def test_each_category_has_its_policy(forum):
    url, stub = forum
    stub.categories = [category(5, "Meta", "About the forum itself."), category(8, "arXiv quant-ph", ARXIV_RULE)]

    policy = client(url).fetch()

    assert [(c.name, c.text) for c in policy.categories] == [("Meta", "About the forum itself."),
                                                              ("arXiv quant-ph", ARXIV_RULE)]


@pytest.mark.parametrize("host", [True, False])
def test_a_subcategory_carries_its_own_policy_and_its_parent_s(forum, host):
    url, stub = forum
    stub.categories = [category(9, "Diary", "DIARY RULE"), category(12, "rsmith", "PRIVATE DIARY", parent=9),
                       category(8, "arXiv quant-ph", ARXIV_RULE)]

    policy = client(url, host).fetch()

    assert [c.text for c in policy.applying_to(12)] == ["DIARY RULE", "PRIVATE DIARY"]
    assert [c.text for c in policy.applying_to(9)] == ["DIARY RULE"]
    assert policy.applying_to(None) == []
    assert policy.named("diary").category_id == 9
