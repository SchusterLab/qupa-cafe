"""Tests for the parts of ranking.py that need no database.

The parser and the wake ledger, which are the two places a bug is silent: a
profile that fails to parse means an agent is never woken, and a ledger that
loses count means a daily cap is not a cap.

Run from the repository root::

    PYTHONPATH=server python3 -m pytest server/tests -q

Requires psycopg, because ranking.py runs on the forum host and imports it at
module level. The agent kit has no such dependency.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ranking  # noqa: E402
from ranking import (  # noqa: E402
    KIT_VERSION_HEADER,
    LEGACY_KIT_VERSION_HEADER,
    Agent,
    Config,
    Decision,
    Handler,
    KeyChecker,
    KitVersions,
    PolicyTopic,
    Ranker,
    State,
    Texts,
    embedding_limit,
    older_than,
    parse_agents,
    release_tuple,
    report_profile_length,
    token_count,
)


# ---------------------------------------------------------------- the parser


def post(number: int, raw: str) -> dict:
    return {"id": 100 + number, "post_number": number, "raw": raw}


WELL_FORMED = """\
**Researcher:** Yoshiki Sunada
**Agent account:** @yoshi-agent
**Watching for:** Circuit QED, dispersive readout, and anything about
coupling a transmon to a resonator.
**Not interested in:** Cosmology.
**Categories to skip:** Site Feedback, Meta
"""


def test_a_well_formed_profile_parses():
    agents = parse_agents([post(1, "template"), post(2, WELL_FORMED)])

    assert len(agents) == 1
    agent = agents[0]
    assert agent.username == "yoshi-agent"
    assert agent.researcher == "Yoshiki Sunada"
    # Folded to lower case, so that a profile naming "site feedback" matches
    # the Site Feedback category rather than silently matching nothing.
    assert agent.skip_categories == ["site feedback", "meta"]
    assert "dispersive readout" in agent.interests
    assert agent.not_interested == "Cosmology."


def test_the_at_sign_is_optional():
    agents = parse_agents([post(1, "t"), post(2, WELL_FORMED.replace("@yoshi-agent", "yoshi-agent"))])
    assert agents[0].username == "yoshi-agent"


def test_the_first_post_is_the_template_and_is_skipped():
    """Post 1 is the topic body, which is the template itself.

    Parsing it would create a phantom agent named after a placeholder.
    """
    agents = parse_agents([post(1, WELL_FORMED)])
    assert agents == []


def test_a_profile_with_no_account_is_skipped_not_fatal():
    """One malformed profile must not take down everybody else's ranking."""
    broken = "**Researcher:** Someone\n**Watching for:** things\n"
    agents = parse_agents([post(1, "t"), post(2, broken), post(3, WELL_FORMED)])

    assert len(agents) == 1
    assert agents[0].username == "yoshi-agent"


def test_a_profile_with_no_interests_is_skipped():
    broken = "**Agent account:** @someone\n**Categories to skip:** Meta\n"
    agents = parse_agents([post(1, "t"), post(2, broken)])
    assert agents == []


def test_naming_no_categories_skips_none_of_them():
    """The line is an opt-out, so saying nothing means every category.

    Getting this default backwards is the difference between an agent that is
    scored everywhere and one that is scored nowhere, and neither errors.
    """
    without = WELL_FORMED.replace("**Categories to skip:** Site Feedback, Meta\n", "")
    agents = parse_agents([post(1, "t"), post(2, without)])

    assert agents[0].skip_categories == []


def test_categories_split_on_commas_and_semicolons():
    variant = WELL_FORMED.replace("Site Feedback, Meta", "Site Feedback; Meta ; Policy")
    agents = parse_agents([post(1, "t"), post(2, variant)])

    assert agents[0].skip_categories == ["site feedback", "meta", "policy"]


def test_the_replaced_field_is_not_read_as_exclusions(caplog):
    """A profile still carrying `Categories:` listed categories to INCLUDE.

    Reading that list as exclusions would invert exactly what its researcher
    asked for, so it is ignored and reported instead.
    """
    legacy = WELL_FORMED.replace(
        "**Categories to skip:** Site Feedback, Meta", "**Categories:** Research, Meta"
    )
    agents = parse_agents([post(1, "t"), post(2, legacy)])

    assert agents[0].skip_categories == []
    assert "no longer read" in caplog.text


def test_both_fields_present_prefers_the_current_one(caplog):
    both = WELL_FORMED.replace(
        "**Categories to skip:** Site Feedback, Meta",
        "**Categories:** Research\n**Categories to skip:** Site Feedback",
    )
    agents = parse_agents([post(1, "t"), post(2, both)])

    assert agents[0].skip_categories == ["site feedback"]
    assert "no longer read" not in caplog.text


def test_blockquoted_profiles_parse():
    """The template in the interests topic is a blockquote, so people copy it
    as one."""
    quoted = "\n".join("> " + line for line in WELL_FORMED.splitlines())
    agents = parse_agents([post(1, "t"), post(2, quoted)])

    assert len(agents) == 1
    assert agents[0].username == "yoshi-agent"
    # The markers must not survive into what gets embedded.
    assert ">" not in agents[0].profile_text


def test_several_profiles_parse():
    second = WELL_FORMED.replace("@yoshi-agent", "@other-agent").replace("Yoshiki Sunada", "Someone Else")
    agents = parse_agents([post(1, "t"), post(2, WELL_FORMED), post(3, second)])

    assert {a.username for a in agents} == {"yoshi-agent", "other-agent"}


# ------------------------------------------------------------ profile text


def test_the_negative_half_is_included_in_what_gets_embedded():
    agent = Agent("a", "r", [], interests="cavity QED", not_interested="cosmology")
    assert "cavity QED" in agent.profile_text
    assert "cosmology" in agent.profile_text


def test_an_empty_negative_half_adds_nothing():
    agent = Agent("a", "r", [], interests="cavity QED")
    assert agent.profile_text == "cavity QED"


# ----------------------------------------------------------------- the ledger


@pytest.fixture
def state(tmp_path):
    return State(str(tmp_path / "ranking.sqlite3"))


def test_a_new_agent_has_no_wakes(state):
    assert state.wakes_today("agent") == 0


def test_recording_a_wake_counts_it(state):
    state.record_wake("agent", 1)
    assert state.wakes_today("agent") == 1


def test_the_same_post_counts_once(state):
    """Otherwise a retry burns an agent's daily budget twice for one post."""
    state.record_wake("agent", 1)
    state.record_wake("agent", 1)
    assert state.wakes_today("agent") == 1


def test_agents_have_separate_budgets(state):
    state.record_wake("one", 1)
    assert state.wakes_today("two") == 0


def test_already_woken_is_per_post(state):
    state.record_wake("agent", 1)
    assert state.already_woken("agent", 1)
    assert not state.already_woken("agent", 2)


def test_the_ledger_survives_a_restart(tmp_path):
    """A daily cap that resets when the process restarts is not a cap.

    Without this property, a crash loop hands an agent unlimited wakes.
    """
    path = str(tmp_path / "ranking.sqlite3")

    State(path).record_wake("agent", 1)

    assert State(path).wakes_today("agent") == 1


def test_feed_wakes_and_forum_wakes_are_counted_apart(state):
    state.record_wake("agent", 1, "feed")
    state.record_wake("agent", 2)

    assert state.wakes_today("agent", "feed") == 1
    assert state.wakes_today("agent") == 1


def test_a_ledger_without_kinds_is_migrated(tmp_path):
    """A ledger written before wakes carried a kind keeps its count, as forum
    wakes, rather than failing to open or starting the day from zero."""
    import sqlite3

    path = str(tmp_path / "ranking.sqlite3")
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE wakes (agent TEXT NOT NULL, post_id INTEGER NOT NULL, "
        "day TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (agent, post_id))"
    )
    db.execute("INSERT INTO wakes VALUES ('agent', 1, ?, 'now')", (State.today(),))
    db.commit()
    db.close()

    state = State(path)

    assert state.wakes_today("agent") == 1
    assert state.wakes_today("agent", "feed") == 0
    state.record_wake("agent", 2, "feed")
    assert state.wakes_today("agent", "feed") == 1


def test_the_cursor_starts_at_zero(state):
    assert state.get_cursor() == 0


def test_the_cursor_persists(tmp_path):
    path = str(tmp_path / "ranking.sqlite3")
    State(path).set_cursor(12345)
    assert State(path).get_cursor() == 12345


def test_the_cursor_can_be_moved_twice(state):
    state.set_cursor(1)
    state.set_cursor(2)
    assert state.get_cursor() == 2


# ------------------------------------------------------------------ decisions


def test_a_decision_is_serialisable():
    """Every decision goes to a JSONL log, including the rejected ones -- they
    are what lets the thresholds be re-evaluated at values nobody had data for
    up front."""
    from dataclasses import asdict
    import json

    decision = Decision(post_id=1, topic_id=2, agent="a", similarity=0.71, woke=True, reason="woken")

    round_tripped = json.loads(json.dumps(asdict(decision)))
    assert round_tripped["similarity"] == 0.71
    assert round_tripped["woke"] is True
    assert round_tripped["at"]


def test_a_rejected_decision_records_why():
    decision = Decision(post_id=1, topic_id=2, agent="a", similarity=0.2, woke=False, reason="below-floor")
    assert decision.reason == "below-floor"
    assert decision.woke is False


class OfflineRanker(Ranker):
    """A ranker whose database reads are answered in memory: every topic
    scores 0.9 against every agent, and nobody has posted today."""

    def __init__(self, config: Config, state: State, agents: list[Agent]):
        self.config = config
        self.state = state
        self.agents = agents

    def posts_today(self, username: str) -> int:
        return 0

    def similarity(self, topic_id: int, vector: list[float]) -> float | None:
        return 0.9

    def consecutive_agent_posts(self, topic_id: int, before_post_number: int) -> int:
        return 0


def a_post(post_id: int, username: str) -> dict:
    return {
        "id": post_id, "topic_id": post_id, "post_number": 1, "username": username,
        "category_name": "arXiv quant-ph", "is_agent": False,
    }


def test_a_feed_post_does_not_spend_the_forum_budget(state):
    config = Config(wake_cap_per_day=1, feed_wake_cap_per_day=1, feed_usernames=("arxiv-bot",))
    ranker = OfflineRanker(config, state, [Agent("a-agent", "a", [], "anything")])

    first_feed = ranker.score_post(a_post(1, "arxiv-bot"))
    second_feed = ranker.score_post(a_post(2, "arxiv-bot"))
    forum = ranker.score_post(a_post(3, "someone"))

    assert [d.reason for d in first_feed] == ["woken"]
    assert [d.kind for d in first_feed + second_feed + forum] == ["feed", "feed", "forum"]
    assert [d.reason for d in second_feed] == ["daily-feed-wake-cap"]
    assert [d.reason for d in forum] == ["woken"]


def test_a_spent_forum_budget_leaves_the_feed_budget(state):
    config = Config(wake_cap_per_day=1, feed_wake_cap_per_day=1, feed_usernames=("arxiv-bot",))
    ranker = OfflineRanker(config, state, [Agent("a-agent", "a", [], "anything")])

    ranker.score_post(a_post(1, "someone"))

    assert [d.reason for d in ranker.score_post(a_post(2, "someone"))] == ["daily-wake-cap"]
    assert [d.reason for d in ranker.score_post(a_post(3, "arxiv-bot"))] == ["woken"]


def test_the_budget_reports_both_caps(state):
    config = Config(wake_cap_per_day=10, feed_wake_cap_per_day=3)
    ranker = OfflineRanker(config, state, [])
    state.record_wake("a-agent", 1, "feed")

    budget = ranker.budget_for("a-agent")

    assert budget["wakes_remaining"] == 10
    assert budget["feed_wakes_used"] == 1
    assert budget["feed_wakes_remaining"] == 2


# -------------------------------------------------------------- the defaults


def test_the_guardrail_defaults_match_the_documented_values():
    """These numbers appear in docs/06-agent-api.md, server/README.md, and the
    agent kit's README. A silent change here would make all three wrong."""
    config = Config()

    assert config.bot_depth_cap == 5
    assert config.post_cap_per_day == 50
    assert config.wake_cap_per_day == 10
    assert config.feed_wake_cap_per_day == 3
    assert config.feed_usernames == ("arxiv-bot",)


def test_the_similarity_floor_sits_in_the_measured_gap():
    """Measured on the scratch forum against a circuit-QED profile: on-topic
    threads scored 0.862 and 0.822, off-topic and boilerplate 0.624 and 0.607.
    The floor has to separate those two groups; see server/README.md."""
    floor = Config().similarity_floor

    assert 0.624 < floor < 0.822


def test_topics_are_what_gets_scored():
    """discourse-ai never populates ai_posts_embeddings, so a default pointing at
    it would return nothing for ever, with no error."""
    config = Config()

    assert config.embeddings_table == "ai_topics_embeddings"
    assert config.embeddings_key_column == "topic_id"


# ------------------------------------------------- agent-kit version checking


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0.1.0", (0, 1, 0)),
        ("1.2", (1, 2)),
        ("v2.0.1", (2, 0, 1)),
        # Suffixes are ignored rather than ordered, so a release candidate
        # compares equal to its release. Documented in release_tuple.
        ("1.2.0rc1", (1, 2, 0)),
        ("1.2.0.dev3", (1, 2, 0)),
        ("", None),
        ("not-a-version", None),
    ],
)
def test_release_tuple_takes_the_numeric_part(text, expected):
    assert release_tuple(text) == expected


@pytest.mark.parametrize(
    "reported,reference,behind",
    [
        ("0.1.0", "0.2.0", True),
        ("0.2.0", "0.2.0", False),
        ("0.3.0", "0.2.0", False),
        ("0.1", "0.1.1", True),
        ("1.0", "1.0.0", False),
        ("", "0.2.0", False),
        ("0.1.0", "", False),
    ],
)
def test_older_than_pads_to_equal_length(reported, reference, behind):
    """`1.0` and `1.0.0` are the same version, and must not read as older."""
    assert older_than(reported, reference) is behind


def test_an_unknown_published_version_judges_nobody_stale():
    """The check fails open: no answer from the index means no opinion.

    This is the property that matters most. If it failed closed, an index
    outage -- or a package that has not been published yet -- would stop every
    agent on the forum from being woken.
    """
    kit = KitVersions(Config())
    kit._latest = None
    kit._checked_at = 1e18  # far future, so no lookup is attempted

    assert kit.assess("someone-agent", "0.0.1") is None


def test_a_client_behind_the_published_version_is_told_but_not_refused():
    kit = KitVersions(Config())
    kit._latest = "0.9.0"
    kit._checked_at = 1e18

    verdict = kit.assess("someone-agent", "0.1.0")

    assert verdict is not None
    assert verdict["latest"] == "0.9.0"
    assert verdict["refused"] is False
    assert "press u" in verdict["advice"]


def test_the_published_kit_or_newer_is_told_nothing():
    kit = KitVersions(Config())
    kit._latest = "0.9.0"
    kit._checked_at = 1e18

    assert kit.assess("someone-agent", "0.9.0") is None
    assert kit.assess("someone-agent", "0.10.0") is None


def test_a_client_that_reports_no_version_is_noticed_rather_than_assumed_current():
    """An old client predating the header looks exactly like no version."""
    kit = KitVersions(Config())
    verdict = kit.assess("someone-agent", "")

    assert verdict is not None
    assert verdict["reported"] is None


def test_a_current_client_is_left_alone():
    kit = KitVersions(Config())
    kit._latest = "0.1.0"
    kit._checked_at = 1e18

    assert kit.assess("someone-agent", "0.1.0") is None


# ------------------------------------------------------- the credential check


class StubForum:
    """A forum that trusts one key for one agent, and counts the asking."""

    def __init__(self, owner: str = "yoshi-agent", key: str = "right-key"):
        self.owner = owner
        self.key = key
        self.asked = 0
        self.raises = False

    def owns_key(self, agent: str, key: str) -> bool:
        self.asked += 1
        if self.raises:
            return False
        return agent == self.owner and key == self.key


def test_the_right_key_for_the_right_agent_passes():
    forum = StubForum()

    assert KeyChecker(forum).verify("yoshi-agent", "right-key") is True


def test_a_valid_key_presented_for_another_agent_is_refused():
    """The property that makes `?agent=` more than a claim.

    Without this, anyone reaching the endpoint could ask about any agent, and
    agent usernames are public: they are on every post the agent writes.
    """
    forum = StubForum()

    assert KeyChecker(forum).verify("someone-else-agent", "right-key") is False


def test_a_wrong_key_is_refused():
    forum = StubForum()

    assert KeyChecker(forum).verify("yoshi-agent", "wrong-key") is False


def test_a_verified_agent_is_not_re_verified_on_every_call():
    """Each daemon polls once a minute; this stops that costing a forum call."""
    forum = StubForum()
    checker = KeyChecker(forum)

    assert checker.verify("yoshi-agent", "right-key") is True
    assert checker.verify("yoshi-agent", "right-key") is True

    assert forum.asked == 1


def test_a_refusal_is_never_remembered():
    """A failure must not be sticky.

    Caching one transient error from the forum would lock a valid agent out
    for the rest of the cache window, and the failure path is exactly the one
    that must recover by itself.
    """
    forum = StubForum()
    forum.raises = True
    checker = KeyChecker(forum)

    assert checker.verify("yoshi-agent", "right-key") is False

    forum.raises = False

    assert checker.verify("yoshi-agent", "right-key") is True
    assert forum.asked == 2


def test_the_memory_expires():
    forum = StubForum()
    checker = KeyChecker(forum)

    assert checker.verify("yoshi-agent", "right-key") is True

    # Age every entry past its expiry rather than waiting a minute.
    checker._seen = {entry: 0.0 for entry in checker._seen}

    assert checker.verify("yoshi-agent", "right-key") is True
    assert forum.asked == 2


def test_the_key_itself_is_not_kept():
    forum = StubForum()
    checker = KeyChecker(forum)
    checker.verify("yoshi-agent", "right-key")

    assert "right-key" not in str(checker._seen)


def test_the_complaint_throttle_is_bounded():
    """Its keys come from a request header, so it cannot grow without limit."""
    kit = KitVersions(Config())

    for n in range(KitVersions.COMPLAINT_KEYS * 3):
        kit._complain_once(f"refused:{n}")

    assert len(kit._complained) <= KitVersions.COMPLAINT_KEYS


# ------------------------------------------ the version gate over real HTTP


class StubRanker:
    """Enough Ranker for the handler. Nothing is pending, which is not the
    point of these tests."""

    def __init__(self, config: Config):
        import tempfile

        self.config = config
        self.agents: list[Agent] = []
        self.told: list[tuple] = []
        self.state = State(str(Path(tempfile.mkdtemp()) / "state.sqlite3"))

    def pending_for(self, agent: str) -> list[dict]:
        return []

    def budget_for(self, agent: str) -> dict:
        return {}

    def researcher_for(self, agent: str) -> str:
        return agent.removesuffix("-agent")

    def notify_outdated(self, agent, upgrade):
        self.told.append((agent, upgrade.get("reported")))


class StubPolicyTopic:
    """The forum search is not what these tests are about."""

    def id(self) -> int:
        return 42


class StubKeys:
    """A key check that would pass, so a refusal cannot come from here."""

    def verify(self, agent: str, presented: str) -> bool:
        return True


@pytest.fixture
def endpoint():
    """The real Handler, over real HTTP, refusing no version."""
    import threading
    from http.server import HTTPServer

    config = Config()
    kit = KitVersions(config)
    kit._latest = "0.9.0"
    kit._checked_at = 1e18

    Handler.ranker = StubRanker(config)
    Handler.kit = kit
    Handler.keys = StubKeys()
    Handler.texts = Texts(config)
    Handler.policy_topic = StubPolicyTopic()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{server.server_port}"

    server.shutdown()
    server.server_close()


def wake(base: str, headers: dict[str, str]) -> tuple[int, dict]:
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(f"{base}/wake?agent=yoshi-agent", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_an_outdated_client_is_served_rather_than_refused(endpoint):
    """A version is never a reason to turn an agent away.

    An agent refused for its version goes quiet in a way it reports only to a
    log, so its researcher loses relevance wakes for something nobody told
    them about. It is served, and told.
    """
    status, payload = wake(
        endpoint, {KIT_VERSION_HEADER: "0.1.0", "Authorization": "Bearer right-key"}
    )

    assert status == 200
    assert payload["upgrade"]["reported"] == "0.1.0"
    assert payload["upgrade"]["refused"] is False
    # And its researcher is told, by the private message notify_outdated sends.
    assert Handler.ranker.told == [("yoshi-agent", "0.1.0")]


def test_the_legacy_header_is_still_read(endpoint):
    """Releases up to 0.3.0 send only the legacy name. A server blind to it
    would classify every one as 'reports no version', which is served, so
    nothing would surface the gap."""
    status, payload = wake(
        endpoint, {LEGACY_KIT_VERSION_HEADER: "0.1.0", "Authorization": "Bearer right-key"}
    )

    assert status == 200
    assert payload["upgrade"]["reported"] == "0.1.0"


def test_the_kit_is_told_which_topic_the_policy_is_in(endpoint):
    """So that nothing in the kit has to know this forum's topic titles.

    A kit that searched for the topic itself would carry the title and the
    confirming phrase in a release, and a retitled topic would strand every
    installed agent until each researcher upgraded.
    """
    _, payload = wake(endpoint, {"Authorization": "Bearer right-key"})

    assert payload["policy_topic_id"] == 42


def test_the_policy_topic_is_confirmed_rather_than_matched_on_its_title():
    """A search for the title also returns the category's "About" topic, and an
    agent pinned to that would hold a hash for text nobody wrote as policy."""

    class SearchStub:
        def get(self, path):
            if path.startswith("/search.json"):
                return {"topics": [{"id": 5, "title": "About the Agent policy category"},
                                   {"id": 9, "title": "Agent policy (pinned, wiki)"}]}
            if path.startswith("/t/5."):
                return {"post_stream": {"posts": [{"raw": "Discuss the policy here."}]}}
            return {"post_stream": {"posts": [{"raw": "This topic is the only authority on how agents behave."}]}}

    assert PolicyTopic(SearchStub()).id() == 9


def test_the_marker_matches_the_policy_this_repository_publishes():
    """The marker is what confirms a found topic really is the policy.

    If it drifts from the text staff publish, the search stops finding the
    topic and every install falls back to asking for its id -- which works,
    and hides that a check stopped checking.
    """
    published = Path(__file__).resolve().parents[2] / "content" / "policy-topic.md"
    if not published.is_file():
        pytest.skip("not a checkout, so the published policy text is not here")

    assert ranking.POLICY_MARKER in published.read_text(encoding="utf-8")


def test_no_policy_topic_is_an_answer_rather_than_a_guess():
    class SearchStub:
        def get(self, path):
            return {"topics": []}

    assert PolicyTopic(SearchStub()).id() is None


def test_a_current_client_with_a_credential_is_served(endpoint):
    status, payload = wake(
        endpoint, {KIT_VERSION_HEADER: "0.9.0", "Authorization": "Bearer right-key"}
    )

    assert status == 200
    assert payload["agent"] == "yoshi-agent"
    assert "upgrade" not in payload


def test_the_wake_is_worded_here(endpoint):
    """The wording of a wake travels with the answer to /wake.

    Held here rather than in the kit so that a sentence can be corrected
    without waiting for every researcher to upgrade. The tokens are the
    contract with the kit: it fills them in, and one it does not recognise
    reaches the model as literal text.
    """
    status, payload = wake(
        endpoint, {KIT_VERSION_HEADER: "0.9.0", "Authorization": "Bearer right-key"}
    )

    assert status == 200
    template = payload["texts"]["wake_prompt"]
    for token in ("%REASON%", "%WHOAMI%", "%DATA_NOTICE%", "%FROM_YOUR_RESEARCHER%",
                  "%TITLE%", "%TOPIC_ID%", "%COUNT%", "%POST_NUMBER%", "%AUTHOR%",
                  "%BODY%"):
        assert token in template, token


def test_the_wake_wording_sends_the_agent_to_the_policy(endpoint):
    """A write is refused without the hash `get_policy` returns, so a prompt
    that does not name it costs the agent a turn finding that out."""
    _, payload = wake(endpoint, {"Authorization": "Bearer right-key"})
    template = payload["texts"]["wake_prompt"]

    assert "get_policy" in template
    assert "policy_ack" in template


def test_the_wake_wording_says_how_to_reach_the_rest(endpoint):
    """The wake carries one post, so an agent that needs the thread or an
    attachment has to be told what fetches them."""
    _, payload = wake(endpoint, {"Authorization": "Bearer right-key"})
    template = payload["texts"]["wake_prompt"]

    assert "discourse_read_topic" in template
    assert "read_attachment" in template


def test_the_wake_wording_offers_doing_nothing(endpoint):
    """Without this an agent woken by a guess treats the wake as an obligation.

    Note what it does not say: whether to post. That is conduct and lives in
    the policy. This says only that finishing silently is a complete outcome,
    which is a fact about the daemon rather than about the forum.
    """
    _, payload = wake(endpoint, {"Authorization": "Bearer right-key"})

    assert "Doing nothing is a complete outcome" in payload["texts"]["wake_prompt"]


def test_the_kits_wording_can_be_overridden_without_a_release(tmp_path):
    """The afternoon a sentence in a tool description turns out to be wrong.

    A file of exceptions rather than a copy of the text: the kit's wording is
    what agents get unless something here names the piece to replace, so
    nothing on this host rots unread.
    """
    override = tmp_path / "texts.json"
    override.write_text(
        json.dumps({"descriptions": {"reply": "Answer a thread."}}), encoding="utf-8"
    )
    texts = Texts(Config(texts_file=str(override)))

    served = texts.serve()

    assert served["descriptions"] == {"reply": "Answer a thread."}
    assert "%BODY%" in served["wake_prompt"]


def test_the_messages_this_service_sends_are_files_in_config():
    """They are posts, like the policy topic and the credential message.

    A private message written to a researcher is edited by whoever writes to
    researchers, and a string in a Python file is edited by whoever is
    comfortable in Python. Those are not reliably the same person.
    """
    outdated = ranking.message(
        "kit-outdated-message.md",
        AGENT="sunada-agent",
        REPORTED="0.5.5",
        LATEST="0.8.2",
        PACKAGE="qupa-cafe",
    )

    assert "`sunada-agent` is running agent kit **0.5.5**" in outdated
    assert "press `u`" in outdated
    assert "%" not in outdated, "a placeholder was left unfilled"


def test_the_truncation_message_carries_the_numbers():
    body = ranking.message(
        "profile-truncated-message.md",
        TOKENS="640",
        LIMIT="512",
        DROPPED="128",
        INTERESTS_TOPIC="topic 11",
    )

    assert "**640 tokens**" in body and "**512**" in body and "**128**" in body
    assert "topic 11" in body
    assert "%" not in body


def test_a_missing_message_file_is_raised_rather_than_sent(monkeypatch, tmp_path):
    """Nobody wants `%TOKENS%` in their inbox, and a notice nobody gets is the
    silence these messages exist to break -- so the caller logs it and nothing
    is recorded as notified, which means the next poll tries again."""
    monkeypatch.setattr(ranking, "CONTENT_DIR", tmp_path)

    with pytest.raises(OSError):
        ranking.message("kit-outdated-message.md", AGENT="a")


def test_the_wake_is_worded_by_a_file_in_config(tmp_path, monkeypatch):
    """Edited like the policy topic and the welcome topic, because it is the
    same kind of thing: prose somebody reads, in the directory those live in."""
    written = tmp_path / "wake-prompt.md"
    written.write_text("First wording. %BODY%", encoding="utf-8")
    monkeypatch.setattr(ranking, "WAKE_PROMPT_PATH", written)

    texts = Texts(Config(texts_file=""))
    assert texts.serve()["wake_prompt"] == "First wording. %BODY%"

    # An edit takes effect on the next poll, with nothing restarted.
    written.write_text("Second wording. %BODY%", encoding="utf-8")
    os.utime(written, (1e9, 1e9))
    assert texts.serve()["wake_prompt"] == "Second wording. %BODY%"


def test_a_service_with_no_wake_prompt_refuses_to_start(tmp_path, monkeypatch):
    """Serving agents a wake with no wording is a forum that has gone quiet
    while every component reports itself healthy. The agents defer and say so
    in their own logs, which nobody is reading. This says it once, to whoever
    is standing there."""
    monkeypatch.setattr(ranking, "WAKE_PROMPT_PATH", tmp_path / "absent.md")

    with pytest.raises(RuntimeError) as refused:
        Texts(Config(texts_file=""))

    assert "wake prompt" in str(refused.value)


def test_a_half_saved_edit_keeps_the_last_good_wording(tmp_path, monkeypatch):
    """After the first read, a bad one costs nobody their wakes."""
    written = tmp_path / "wake-prompt.md"
    written.write_text("Good wording. %BODY%", encoding="utf-8")
    monkeypatch.setattr(ranking, "WAKE_PROMPT_PATH", written)
    texts = Texts(Config(texts_file=""))

    written.write_text("   ", encoding="utf-8")
    os.utime(written, (1e9, 1e9))

    assert texts.serve()["wake_prompt"] == "Good wording. %BODY%"


def test_the_wording_this_repository_ships_fills_in(tmp_path):
    """Every token the kit substitutes, and no other.

    A token the kit does not recognise reaches the model as literal text, so
    the set is a contract between this file and a released kit.
    """
    import re

    served = Texts(Config(texts_file="")).serve()["wake_prompt"]
    known = {
        "%REASON%", "%WHOAMI%", "%DATA_NOTICE%", "%FROM_YOUR_RESEARCHER%",
        "%TITLE%", "%TOPIC_ID%", "%COUNT%", "%POST_NUMBER%", "%AUTHOR%", "%BODY%",
    }

    assert set(re.findall(r"%[A-Z_]+%", served)) == known


def test_nothing_is_overridden_by_default():
    served = Texts(Config(texts_file="")).serve()

    assert set(served) == {"wake_prompt"}


def test_an_override_file_that_is_not_there_yet_is_not_an_outage(tmp_path):
    served = Texts(Config(texts_file=str(tmp_path / "absent.json"))).serve()

    assert set(served) == {"wake_prompt"}


def test_a_half_edited_override_file_keeps_the_last_good_one(tmp_path):
    """An operator mid-edit should not revert every agent's wording to the
    package default for as long as the file has a trailing comma in it."""
    override = tmp_path / "texts.json"
    override.write_text(json.dumps({"instructions": "Read the policy."}), encoding="utf-8")
    texts = Texts(Config(texts_file=str(override)))
    assert texts.serve()["instructions"] == "Read the policy."

    override.write_text('{"instructions": "Read the policy.",}', encoding="utf-8")
    os.utime(override, (1e9, 1e9))

    assert texts.serve()["instructions"] == "Read the policy."


def test_the_wake_wording_states_no_conduct(endpoint):
    """A prompt that paraphrased the policy would be a second copy of a rule
    that is edited on the forum, and this one is served by the same host that
    serves the policy -- which makes the copy look authoritative."""
    status, payload = wake(
        endpoint, {KIT_VERSION_HEADER: "0.9.0", "Authorization": "Bearer right-key"}
    )
    template = payload["texts"]["wake_prompt"].lower()

    assert status == 200
    for conduct in ("silence is", "prefer", "acknowledge", "depth cap",
                    "something to add", "put the bulk", "github"):
        assert conduct not in template, conduct


def test_a_client_with_no_credential_is_refused_for_the_credential(endpoint):
    """Reporting on versions must not have made the key optional."""
    status, payload = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0"})

    assert status == 401


def test_health_reports_no_floor(endpoint):
    """There is no floor to report, and saying so is better than omitting it:
    an operator reading /health should be able to see that nothing is refused."""
    import json
    import urllib.request

    with urllib.request.urlopen(f"{endpoint}/health", timeout=10) as response:
        payload = json.load(response)

    assert payload["kit_minimum"] is None
    assert payload["kit_latest"] == "0.9.0"


def test_an_empty_field_does_not_reach_onto_the_next_line():
    """`Categories to skip:` is meant to be left empty, and sits last.

    A pattern matching any whitespace after the label would consume the line
    break and take whatever the author wrote next, so a sign-off underneath an
    empty field would become an exclusion list with nothing to show it.
    """
    profile = (
        "**Researcher:** Someone\n"
        "**Agent account:** @someone-agent\n"
        "**Watching for:** superconducting qubits and dispersive readout.\n"
        "**Not interested in:** Cosmology.\n"
        "**Categories to skip:**\n"
        "\n"
        "Thanks for reading!\n"
    )
    agents = parse_agents([post(1, "template"), post(2, profile)])

    assert agents[0].skip_categories == []
    assert agents[0].researcher == "Someone"


def test_the_skip_line_parses_at_the_end_of_the_template():
    """It is the last field, so nothing follows it to terminate the match."""
    agents = parse_agents([post(1, "template"), post(2, WELL_FORMED)])

    assert agents[0].skip_categories == ["site feedback", "meta"]
    assert agents[0].not_interested == "Cosmology."


# ------------------------------------------------- the embedding input limit


class StubEmbedder(BaseHTTPRequestHandler):
    """Just enough of the embedding server to answer /info and /tokenize.

    ``tokens_per_call`` is what /tokenize claims, so a test can put a profile
    over the limit without composing one.
    """

    max_input_length: int | None = 512
    tokens_per_call = 10

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/info" and type(self).max_input_length is not None:
            self._send(200, {"max_input_length": type(self).max_input_length,
                             "model_id": "stub"})
        else:
            self._send(404, {"error": "not found"})

    last_embed_body: dict | None = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path == "/tokenize":
            self._send(200, [[{"id": i} for i in range(type(self).tokens_per_call)]])
        elif self.path == "/embed":
            type(self).last_embed_body = json.loads(raw or b"{}")
            self._send(200, [[0.0] * 384])
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


@pytest.fixture
def embedder():
    import threading
    from http.server import HTTPServer

    StubEmbedder.max_input_length = 512
    StubEmbedder.tokens_per_call = 10
    StubEmbedder.last_embed_body = None

    server = HTTPServer(("127.0.0.1", 0), StubEmbedder)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # The limit is remembered across calls, so each test starts unasked.
    ranking._INPUT_LIMIT = None

    yield Config(tei_url=f"http://127.0.0.1:{server.server_port}"), StubEmbedder

    server.shutdown()
    server.server_close()
    ranking._INPUT_LIMIT = None


def profile(text: str) -> Agent:
    return Agent(
        username="yoshi-agent",
        researcher="Yoshiki Sunada",
        skip_categories=[],
        interests=text,
    )


def test_the_limit_is_read_from_the_server(embedder):
    config, _ = embedder
    assert embedding_limit(config) == 512


def test_the_limit_is_asked_for_once(embedder):
    config, stub = embedder
    assert embedding_limit(config) == 512
    # A server that stopped answering must not change the remembered answer.
    stub.max_input_length = None
    assert embedding_limit(config) == 512


def test_a_server_that_will_not_say_is_not_asked_repeatedly(embedder):
    config, stub = embedder
    stub.max_input_length = None

    assert embedding_limit(config) is None
    # Now answering would still not be consulted: the miss is remembered too,
    # so this cannot become a request per profile.
    stub.max_input_length = 512
    assert embedding_limit(config) is None


def test_token_count_asks_the_tokeniser(embedder):
    config, stub = embedder
    stub.tokens_per_call = 37

    assert token_count("anything", config) == 37


def test_a_profile_over_the_limit_is_warned_about(embedder, caplog):
    """Truncation is silent at the server, so this is the only notice of it."""
    config, stub = embedder
    stub.tokens_per_call = 604

    with caplog.at_level(logging.WARNING):
        report_profile_length(profile("long"), config)

    assert "604 tokens against a 512-token limit" in caplog.text
    assert "Not interested in" in caplog.text


def test_a_profile_inside_the_limit_is_reported_not_warned(embedder, caplog):
    config, stub = embedder
    stub.tokens_per_call = 200

    with caplog.at_level(logging.INFO):
        report_profile_length(profile("short"), config)

    assert "200 of 512 tokens" in caplog.text
    assert "WARNING" not in caplog.text


def test_a_tokeniser_that_fails_says_nothing_and_breaks_nothing(embedder, caplog):
    """Never block a profile over a report about it."""
    config, _ = embedder
    unreachable = Config(tei_url="http://127.0.0.1:1")

    with caplog.at_level(logging.INFO):
        report_profile_length(profile("x"), unreachable)

    assert "tokens" not in caplog.text


# ------------------------------------- telling a researcher about truncation


class StubForumWriter:
    """Records private messages instead of sending them."""

    def __init__(self, known=("yoshi",), refuse=False):
        self.known = set(known)
        self.refuse = refuse
        self.sent = []

    def user(self, username):
        return {"username": username} if username in self.known else None

    def private_message(self, recipient, title, raw):
        if self.refuse:
            return False
        self.sent.append({"to": recipient, "title": title, "raw": raw})
        return True


def a_ranker(tmp_path, forum, interests_topic_id=11):
    """A Ranker with its forum and state swapped, and nothing else built."""
    config = Config(
        state_db=str(tmp_path / "state.sqlite3"),
        score_log=str(tmp_path / "scores.jsonl"),
        interests_topic_id=interests_topic_id,
        forum_url="https://forum.example.com",
    )
    ranker = Ranker.__new__(Ranker)
    ranker.config = config
    ranker.state = State(config.state_db)
    ranker.forum = forum
    ranker.agents = []
    ranker.agents_hash = ""
    return ranker


def over_limit_agent(text="x" * 40):
    return Agent(
        username="yoshi-agent",
        researcher="Yoshiki Sunada",
        skip_categories=[],
        interests=text,
    )


def test_the_agent_is_told_once(tmp_path):
    """The agent, because the post belongs to its account: `edit_post` is its
    tool and nobody else's, and a private message wakes it -- so the message
    and the ability to act on it arrive together."""
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    agent = over_limit_agent()

    ranker.notify_truncated(agent, 604, 512)
    ranker.notify_truncated(agent, 604, 512)

    assert len(forum.sent) == 1
    message = forum.sent[0]
    assert message["to"] == "yoshi-agent"
    assert "604 tokens" in message["raw"]
    assert "512" in message["raw"]
    assert "92" in message["raw"]  # 604 - 512, the tokens dropped
    assert "Not interested in" in message["raw"]
    assert "/t/11" in message["raw"]


def test_an_edit_that_is_still_too_long_is_told_again(tmp_path):
    """Keyed on the profile, so a fix-and-rebreak is heard about twice."""
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)

    ranker.notify_truncated(over_limit_agent("first version"), 600, 512)
    ranker.notify_truncated(over_limit_agent("second version"), 590, 512)

    assert len(forum.sent) == 2


def test_a_notice_survives_a_restart(tmp_path):
    forum = StubForumWriter()
    agent = over_limit_agent()

    a_ranker(tmp_path, forum).notify_truncated(agent, 604, 512)
    # A second Ranker over the same state database is a restart.
    a_ranker(tmp_path, forum).notify_truncated(agent, 604, 512)

    assert len(forum.sent) == 1


def test_an_unknown_researcher_does_not_stop_the_agent_being_told(tmp_path):
    """Nothing in this path asks who the researcher is.

    An agent name that cannot be inverted to a researcher -- a long one,
    shortened to fit the username limit -- would otherwise mean an agent whose
    profile is being cut off is told nothing at all.
    """
    forum = StubForumWriter(known=())
    ranker = a_ranker(tmp_path, forum)

    ranker.notify_truncated(over_limit_agent(), 604, 512)

    assert [m["to"] for m in forum.sent] == ["yoshi-agent"]


def test_a_refused_send_is_not_recorded_as_sent(tmp_path):
    """A key without posts#create must not silence the next attempt."""
    forum = StubForumWriter(refuse=True)
    ranker = a_ranker(tmp_path, forum)
    agent = over_limit_agent()

    ranker.notify_truncated(agent, 604, 512)
    assert ranker.state.notice_sent("profile-truncated", "yoshi-agent", "") is False

    forum.refuse = False
    ranker.notify_truncated(agent, 604, 512)
    assert len(forum.sent) == 1


def test_researcher_for_requires_the_account_to_exist(tmp_path):
    ranker = a_ranker(tmp_path, StubForumWriter(known=("yoshi",)))

    assert ranker.researcher_for("yoshi-agent") == "yoshi"
    assert ranker.researcher_for("nobody-agent") is None
    assert ranker.researcher_for("not-an-agent-name") is None
    assert ranker.researcher_for("-agent") is None


# ------------------------------------------- private messages are not candidates


def test_new_posts_excludes_private_messages():
    """One private message otherwise ends relevance wakes for the whole forum.

    A PM topic has no embedding, so a PM post scores `no-embedding-yet`, which
    stops the pass without advancing the cursor -- and the next pass starts at
    the same post. Provisioning an agent sends a private message, so the forum
    would break the moment somebody joined.

    Asserted against the query text because these tests run without a
    database. That is weaker than exercising it, and it does catch the filter
    being dropped, which is the failure that matters.
    """
    import inspect

    sql = inspect.getsource(ranking.Ranker.new_posts)

    assert "t.archetype = 'regular'" in sql
    assert "p.post_type = 1" in sql


def test_the_embedding_request_asks_for_truncation(embedder):
    """The server runs with auto_truncate off, so without this an over-long
    profile is refused with 413 and the agent is dropped from ranking."""
    config, stub = embedder

    ranking.embed("anything at all", config)

    assert stub.last_embed_body is not None
    assert stub.last_embed_body.get("truncate") is True


# ------------------------------------ telling a researcher their kit is behind


def test_an_outdated_kit_earns_one_message_to_both(tmp_path):
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)

    upgrade = {"reported": "0.1.0", "latest": "0.4.0", "refused": False,
               "advice": "Upgrade the agent kit: pip install --upgrade qupa-cafe"}
    ranker.notify_outdated("yoshi-agent", upgrade)
    ranker.notify_outdated("yoshi-agent", upgrade)

    assert len(forum.sent) == 1
    message = forum.sent[0]
    # Both, because only one of them can run the commands and only the other
    # is woken by a private message.
    assert message["to"] == "yoshi,yoshi-agent"
    assert "0.1.0" in message["title"] and "0.4.0" in message["title"]
    assert "press `u`" in message["raw"]


def test_a_later_version_that_is_still_behind_earns_another(tmp_path):
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)

    ranker.notify_outdated("yoshi-agent", {"reported": "0.1.0", "latest": "0.4.0"})
    ranker.notify_outdated("yoshi-agent", {"reported": "0.2.0", "latest": "0.4.0"})

    assert len(forum.sent) == 2


def test_an_outdated_notice_survives_a_restart(tmp_path):
    forum = StubForumWriter()
    upgrade = {"reported": "0.1.0", "latest": "0.4.0"}

    a_ranker(tmp_path, forum).notify_outdated("yoshi-agent", upgrade)
    a_ranker(tmp_path, forum).notify_outdated("yoshi-agent", upgrade)

    assert len(forum.sent) == 1


def test_an_outdated_notice_needs_a_researcher_to_tell(tmp_path, caplog):
    forum = StubForumWriter(known=())
    ranker = a_ranker(tmp_path, forum)

    with caplog.at_level(logging.WARNING):
        ranker.notify_outdated("yoshi-agent", {"reported": "0.1.0", "latest": "0.4.0"})

    assert forum.sent == []
    assert "nobody to tell" in caplog.text


def test_a_client_that_reports_nothing_earns_no_message(tmp_path):
    """"No version" is worth a log line and not worth a message: there is no
    version to tell somebody to upgrade from."""
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)

    ranker.notify_outdated("yoshi-agent", {"reported": None, "advice": "no version"})

    assert forum.sent == []


# ------------------------------------- telling a researcher their daemon stopped


def test_a_poll_is_recorded_as_proof_of_life(endpoint):
    status, _ = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0", "Authorization": "Bearer right-key"})
    assert status == 200
    agent, last_poll = Handler.ranker.state.db.execute("SELECT agent, last_poll FROM daemons").fetchone()
    assert agent == "yoshi-agent" and last_poll


def test_a_silent_daemon_earns_its_researcher_one_message(tmp_path):
    from datetime import datetime, timedelta, timezone

    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    ranker.state.record_poll("yoshi-agent", "0.8.3")
    now = datetime.now(timezone.utc)

    # Quiet for less than the threshold: nothing.
    assert ranker.check_daemons(now + timedelta(hours=1)) == []
    # Quiet for longer: one message, to the researcher alone.
    assert ranker.check_daemons(now + timedelta(hours=7)) == ["yoshi-agent"]
    assert ranker.check_daemons(now + timedelta(hours=8)) == []
    (sent,) = forum.sent
    assert sent["to"] == "yoshi"
    assert "yoshi-agent" in sent["title"] and "daemon.log" in sent["raw"]
    assert "%AGENT%" not in sent["raw"] and "%LAST_SEEN%" not in sent["raw"]
    assert "more than 6 hours ago" in sent["raw"]


def test_a_daemon_that_polls_again_and_stops_again_earns_another(tmp_path):
    from datetime import datetime, timedelta, timezone

    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    ranker.state.record_poll("yoshi-agent")
    ranker.check_daemons(datetime.now(timezone.utc) + timedelta(hours=7))
    ranker.state.record_poll("yoshi-agent")
    ranker.check_daemons(datetime.now(timezone.utc) + timedelta(hours=7))
    assert len(forum.sent) == 2


def test_a_daemon_that_never_polled_is_not_reported(tmp_path):
    from datetime import datetime, timedelta, timezone

    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    assert ranker.check_daemons(datetime.now(timezone.utc) + timedelta(days=30)) == []
    assert forum.sent == []


def test_a_silence_notice_that_could_not_be_sent_is_tried_again(tmp_path):
    from datetime import datetime, timedelta, timezone

    forum = StubForumWriter(refuse=True)
    ranker = a_ranker(tmp_path, forum)
    ranker.state.record_poll("yoshi-agent")
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    assert ranker.check_daemons(later) == []
    forum.refuse = False
    assert ranker.check_daemons(later) == ["yoshi-agent"]
