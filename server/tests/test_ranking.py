"""Tests for ranking.py, with Postgres, the forum and the embedding server
replaced by stubs.

Run from the repository root::

    python3 -m pytest server/tests -q

Requires psycopg, because ranking.py runs on the forum host and imports it at
module level. The agent kit has no such dependency.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ranking  # noqa: E402
from ranking import (  # noqa: E402
    KIT_VERSION_HEADER,
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
    wake_outcome,
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


def test_a_categories_line_is_not_read_as_the_skip_line():
    both = WELL_FORMED.replace(
        "**Categories to skip:** Site Feedback, Meta",
        "**Categories:** Research\n**Categories to skip:** Site Feedback",
    )
    agents = parse_agents([post(1, "t"), post(2, both)])

    assert agents[0].skip_categories == ["site feedback"]


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


def wakes_today(state, agent, kind="forum"):
    """How many wakes of one kind the ledger holds for an agent today."""
    return state.db.execute(
        "SELECT count(*) FROM wakes WHERE agent = ? AND day = ? AND kind = ?",
        (agent, state.today(), kind),
    ).fetchone()[0]


def test_recording_a_wake_counts_it(state):
    assert wakes_today(state, "agent") == 0
    state.record_wake("agent", 1)
    assert wakes_today(state, "agent") == 1


def test_the_same_post_counts_once(state):
    """Otherwise a retry wakes an agent twice for one post."""
    state.record_wake("agent", 1)
    state.record_wake("agent", 1)
    assert wakes_today(state, "agent") == 1


def test_agents_have_separate_ledgers(state):
    state.record_wake("one", 1)
    assert wakes_today(state, "two") == 0


def test_already_woken_is_per_post(state):
    state.record_wake("agent", 1)
    assert state.already_woken("agent", 1)
    assert not state.already_woken("agent", 2)


def test_the_ledger_survives_a_restart(tmp_path):
    """Without this property, a crash loop wakes an agent again for every
    post it was already woken for."""
    path = str(tmp_path / "ranking.sqlite3")

    State(path).record_wake("agent", 1)

    assert wakes_today(State(path), "agent") == 1


def test_feed_wakes_and_forum_wakes_are_counted_apart(state):
    state.record_wake("agent", 1, "feed")
    state.record_wake("agent", 2)

    assert wakes_today(state, "agent", "feed") == 1
    assert wakes_today(state, "agent") == 1


def test_the_cursor_starts_at_zero_and_moves(state):
    assert state.get_cursor() == 0
    state.set_cursor(1)
    state.set_cursor(2)
    assert state.get_cursor() == 2


def test_the_cursor_persists(tmp_path):
    path = str(tmp_path / "ranking.sqlite3")
    State(path).set_cursor(12345)
    assert State(path).get_cursor() == 12345


# ------------------------------------------------------------------ decisions


def test_a_decision_is_serialisable():
    """Every decision goes to a JSONL log, including the rejected ones -- they
    are what lets the thresholds be re-evaluated at values nobody had data for
    up front."""
    decision = Decision(post_id=1, topic_id=2, agent="a", similarity=0.2, woke=False, reason="below-floor")

    round_tripped = json.loads(json.dumps(dataclasses.asdict(decision)))
    assert round_tripped["similarity"] == 0.2
    assert round_tripped["woke"] is False
    assert round_tripped["reason"] == "below-floor"
    assert round_tripped["at"]


class OfflineRanker(Ranker):
    """A ranker whose database reads are answered in memory: every topic
    scores 0.9 against every agent."""

    def __init__(self, config: Config, state: State, agents: list[Agent]):
        self.config = config
        self.state = state
        self.agents = agents

    def similarity(self, topic_id: int, vector: list[float]) -> float | None:
        return 0.9


def a_post(post_id: int, username: str) -> dict:
    return {
        "id": post_id, "topic_id": post_id, "post_number": 1, "username": username,
        "category_name": "arXiv quant-ph", "is_agent": False,
    }


def test_no_number_of_wakes_stops_an_agent_being_woken(state):
    """How often an agent acts on its wakes is its researcher's call, so the
    ranker keeps waking it, for feed topics and forum posts alike."""
    config = Config(feed_usernames=("arxiv-bot",))
    ranker = OfflineRanker(config, state, [Agent("a-agent", "a", [], "anything")])

    decisions = [d for n in range(1, 101)
                 for d in ranker.score_post(a_post(n, "arxiv-bot" if n % 2 else "someone"))]

    assert {d.reason for d in decisions} == {"woken"}
    assert wakes_today(state, "a-agent", "feed") == 50
    assert wakes_today(state, "a-agent") == 50


class ReplyRanker(OfflineRanker):
    """Topics score 0.9; a reply's own words score by the vector reply_vector returns."""

    def __init__(self, config, state, agents, reply):
        super().__init__(config, state, agents)
        self.reply = reply
        self.embedded: list[str] = []

    def reply_vector(self, post):
        self.embedded.append(post.get("raw"))
        return self.reply


def a_reply(post_id: int, raw: str = "Thanks, that helps.") -> dict:
    return {**a_post(post_id, "someone"), "post_number": 2, "title": "Dispersive readout", "raw": raw}


def test_a_stray_reply_in_an_on_topic_thread_is_not_scored_as_the_thread(state):
    """The thread scores 0.9; the reply's own words point elsewhere, so the
    reply scores halfway, under the floor."""
    agent = Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])
    ranker = ReplyRanker(Config(similarity_floor=0.70, reply_weight=0.5), state, [agent], reply=[0.0, 1.0])

    [decision] = ranker.score_post(a_reply(5))

    assert decision.reason == "below-floor"
    assert round(decision.similarity, 2) == 0.45
    assert ranker.embedded == ["Thanks, that helps."]


def test_a_reply_on_topic_in_an_on_topic_thread_wakes_its_agent(state):
    agent = Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])
    ranker = ReplyRanker(Config(similarity_floor=0.70, reply_weight=0.5), state, [agent], reply=[0.96, 0.28])

    [decision] = ranker.score_post(a_reply(5))

    assert decision.reason == "woken"
    assert round(decision.similarity, 2) == 0.93


def test_the_reply_weight_sets_how_much_the_thread_counts(state):
    agent = Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])
    thread_only = ReplyRanker(Config(reply_weight=0.0), state, [agent], reply=[0.0, 1.0])
    reply_only = ReplyRanker(Config(reply_weight=1.0), state, [agent], reply=[0.0, 1.0])

    assert thread_only.score_post(a_reply(5))[0].similarity == 0.9
    assert reply_only.score_post(a_reply(6))[0].similarity == 0.0


def test_a_reply_is_embedded_once_for_every_agent(state):
    agents = [Agent(f"{n}-agent", n, [], "anything", profile_vector=[1.0, 0.0]) for n in "abc"]
    ranker = ReplyRanker(Config(), state, agents, reply=[1.0, 0.0])

    ranker.score_post(a_reply(5))

    assert len(ranker.embedded) == 1


def test_a_reply_the_embedding_server_cannot_embed_waits_for_the_next_pass(state):
    """As a topic with no vector yet does, so the reply is retried, not lost."""
    agent = Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])
    ranker = ReplyRanker(Config(), state, [agent], reply=[])

    [decision] = ranker.score_post(a_reply(5))

    assert decision.reason == "no-embedding-yet"


def test_a_first_post_is_still_scored_by_its_topic(state):
    agent = Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])
    ranker = ReplyRanker(Config(), state, [agent], reply=[0.0, 1.0])

    [decision] = ranker.score_post(a_post(5, "arxiv-bot"))

    assert decision.reason == "woken" and decision.similarity == 0.9
    assert ranker.embedded == []


def test_a_wake_just_before_midnight_is_still_pending_after_it(state):
    """The daemon polls a minute later, on the next UTC day."""
    ranker = OfflineRanker(Config(), state, [])
    an_hour_ago = (datetime.now(timezone.utc) - HOUR).isoformat()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    state.db.execute(
        "INSERT INTO wakes (agent, post_id, day, at, kind) VALUES ('a-agent', 7, ?, ?, 'forum')",
        (yesterday, an_hour_ago),
    )

    assert [item["post_id"] for item in ranker.pending_for("a-agent")] == [7]


def test_every_wake_of_the_last_day_is_pending_however_many(state):
    """The daemon skips what it has handled, so a list cut to the newest few
    would hand it the same handled wakes at every poll and never the rest: a
    day's feed can wake one agent for eighty preprints."""
    ranker = OfflineRanker(Config(), state, [])
    for post_id in range(1, 81):
        state.record_wake("a-agent", post_id, kind="feed")

    assert [item["post_id"] for item in ranker.pending_for("a-agent")] == list(range(80, 0, -1))


def test_a_wake_more_than_a_day_old_is_no_longer_pending(state):
    ranker = OfflineRanker(Config(), state, [])
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    state.db.execute(
        "INSERT INTO wakes (agent, post_id, day, at, kind) VALUES ('a-agent', 7, '2026-01-01', ?, 'forum')", (old,)
    )

    assert ranker.pending_for("a-agent") == []


# ------------------------------------------------------- each agent's floor


WOKEN = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def test_a_wake_is_not_judged_before_it_settles():
    assert wake_outcome(WOKEN, WOKEN + HOUR / 2, WOKEN + HOUR, False, True, HOUR) is None


def test_a_reply_in_the_topic_counts_as_replied():
    assert wake_outcome(WOKEN, WOKEN + HOUR, None, False, True, HOUR) == "replied"


def test_no_reply_with_the_daemon_still_polling_counts_as_silent():
    assert wake_outcome(WOKEN, WOKEN + 2 * HOUR, WOKEN + HOUR, False, False, HOUR) == "silent"


def test_no_reply_from_a_machine_that_is_off_is_not_yet_silent():
    """An agent whose daemon stopped polling has not declined anything."""
    assert wake_outcome(WOKEN, WOKEN + 9 * HOUR, WOKEN + HOUR / 2, False, False, HOUR) is None


def test_a_wake_that_coincides_with_a_mention_is_ignored():
    """The kit folds the two into one run, which belongs to the mention."""
    assert wake_outcome(WOKEN, WOKEN + HOUR, WOKEN + HOUR, True, True, HOUR) == "ignored"
    assert wake_outcome(WOKEN, WOKEN + HOUR, WOKEN + HOUR, True, False, HOUR) == "ignored"


def settle_all(state, config, outcomes, agent="a-agent"):
    moves = []
    for post_id, outcome in enumerate(outcomes, start=1):
        state.record_wake(agent, post_id)
        moves.append(state.settle(agent, post_id, outcome, config))
    return moves


def test_ten_silent_wakes_in_a_row_raise_the_floor(state):
    config = Config(similarity_floor=0.70, floor_step=0.02, silent_wakes_to_raise=10)

    moves = settle_all(state, config, ["silent"] * 10)

    assert moves == [None] * 9 + [0.72]
    assert state.floor("a-agent", config.similarity_floor) == 0.72


def test_the_run_starts_again_after_the_floor_moves(state):
    config = Config(similarity_floor=0.70, floor_step=0.02, silent_wakes_to_raise=10)

    settle_all(state, config, ["silent"] * 19)

    assert state.floor("a-agent", config.similarity_floor) == 0.72


def test_two_replied_wakes_in_a_row_lower_the_floor(state):
    config = Config(similarity_floor=0.70, floor_step=0.02)

    assert config.replied_wakes_to_lower == 2
    assert settle_all(state, config, ["replied"] * 2) == [None, 0.68]


def test_a_reply_breaks_a_silent_run_and_silence_breaks_a_replied_one(state):
    config = Config(similarity_floor=0.70, silent_wakes_to_raise=10, replied_wakes_to_lower=3)

    settle_all(state, config, ["silent"] * 9 + ["replied"] * 2 + ["silent"] + ["replied"] * 2)

    assert state.floor("a-agent", config.similarity_floor) == 0.70


def test_an_ignored_wake_neither_extends_nor_breaks_a_run(state):
    config = Config(similarity_floor=0.70, floor_step=0.02, replied_wakes_to_lower=3)

    moves = settle_all(state, config, ["replied", "ignored", "ignored", "replied", "replied"])

    assert moves[-1] == 0.68


def test_the_floor_stays_within_its_bounds(state):
    config = Config(similarity_floor=0.61, floor_step=0.02, floor_min=0.60, replied_wakes_to_lower=1)

    settle_all(state, config, ["replied"] * 5)

    assert state.floor("a-agent", config.similarity_floor) == 0.60


def test_floors_are_per_agent(state):
    config = Config(similarity_floor=0.70, replied_wakes_to_lower=1)

    settle_all(state, config, ["replied"], agent="one")

    assert state.floor("one", config.similarity_floor) == 0.68
    assert state.floor("two", config.similarity_floor) == 0.70


def test_the_floor_and_the_outcomes_survive_a_restart(tmp_path):
    path = str(tmp_path / "ranking.sqlite3")
    config = Config(similarity_floor=0.70, replied_wakes_to_lower=1)
    settle_all(State(path), config, ["replied"])

    reopened = State(path)

    assert reopened.floor("a-agent", config.similarity_floor) == 0.68
    assert reopened.unsettled_wakes() == {}


class SettlingRanker(OfflineRanker):
    """Answers each wake's facts from a table instead of the forum."""

    def __init__(self, config, state, facts):
        super().__init__(config, state, [])
        self.facts = facts

    def wake_facts(self, agent, post_id, woken):
        return self.facts.get(post_id)


def test_settling_counts_wakes_in_order_and_waits_for_an_undecided_one(state):
    config = Config(similarity_floor=0.70, replied_wakes_to_lower=2, wake_settle_minutes=60)
    for post_id in (1, 2, 3):
        state.record_wake("a-agent", post_id)
    state.record_poll("a-agent")
    # Post 1 was answered, post 2 not yet and the daemon has not polled since,
    # post 3 was answered: 1 and 3 are not two in a row while 2 is open.
    ranker = SettlingRanker(config, state, {1: (False, True), 2: (False, False), 3: (False, True)})

    ranker.settle_wakes(now=datetime.now(timezone.utc) + 2 * HOUR)

    assert [post_id for post_id, _ in state.unsettled_wakes()["a-agent"]] == [2, 3]
    assert state.floor("a-agent", config.similarity_floor) == 0.70


def test_a_wake_on_a_vanished_post_is_ignored(state):
    config = Config(wake_settle_minutes=60)
    state.record_wake("a-agent", 1)
    ranker = SettlingRanker(config, state, {})

    ranker.settle_wakes(now=datetime.now(timezone.utc) + 2 * HOUR)

    assert state.unsettled_wakes() == {}


def test_the_ranker_compares_with_the_agent_s_own_floor(state):
    """OfflineRanker scores everything 0.9."""
    config = Config(similarity_floor=0.70, floor_step=0.25, floor_max=1.0, silent_wakes_to_raise=1)
    ranker = OfflineRanker(config, state, [Agent("a-agent", "a", [], "anything"), Agent("b-agent", "b", [], "anything")])
    state.record_wake("a-agent", 100)
    state.settle("a-agent", 100, "silent", config)

    decisions = {d.agent: d for d in ranker.score_post(a_post(1, "someone"))}

    assert decisions["a-agent"].reason == "below-floor"
    assert decisions["a-agent"].floor == 0.95
    assert decisions["b-agent"].reason == "woken"
    assert decisions["b-agent"].floor == 0.70


# -------------------------------------------------------------- the defaults


def test_the_feed_account_is_the_documented_one():
    """server/README.md gives this default, and the arXiv feed posts as it."""
    assert Config().feed_usernames == ("arxiv-bot",)


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


def kit_at(latest: str | None) -> KitVersions:
    """A KitVersions that knows ``latest`` and asks PyPI nothing."""
    kit = KitVersions(Config())
    kit._latest = latest
    kit._checked_at = 1e18  # far future, so no lookup is attempted
    return kit


def test_an_unknown_published_version_judges_nobody_stale():
    """The check fails open: no answer from the index means no opinion.

    This is the property that matters most. If it failed closed, an index
    outage -- or a package that has not been published yet -- would stop every
    agent on the forum from being woken.
    """
    assert kit_at(None).assess("someone-agent", "0.0.1") is None


def test_a_client_behind_the_published_version_is_told_but_not_refused():
    verdict = kit_at("0.9.0").assess("someone-agent", "0.1.0")

    assert verdict is not None
    assert verdict["latest"] == "0.9.0"
    assert verdict["refused"] is False
    assert "Update the agent kit" in verdict["advice"] and "python -m qupa_cafe.update" in verdict["advice"]


def test_the_published_kit_or_newer_is_told_nothing():
    kit = kit_at("0.9.0")

    assert kit.assess("someone-agent", "0.9.0") is None
    assert kit.assess("someone-agent", "0.10.0") is None


def test_a_client_that_reports_no_version_is_noticed_rather_than_assumed_current():
    """A client that sends no version header could be any version."""
    verdict = KitVersions(Config()).assess("someone-agent", "")

    assert verdict is not None
    assert verdict["reported"] is None


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


# ------------------------------------------------- /wake over real HTTP


class StubRanker:
    """Enough Ranker for the handler. Nothing is pending, which is not the
    point of these tests."""

    def __init__(self, config: Config, state: State):
        self.config = config
        self.agents: list[Agent] = []
        self.told: list[tuple] = []
        self.state = state

    def pending_for(self, agent: str) -> list[dict]:
        return []

    floor_for = Ranker.floor_for

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
def endpoint(state):
    """The real Handler, over real HTTP, with 0.9.0 the published kit."""
    config = Config()

    Handler.ranker = StubRanker(config, state)
    Handler.kit = kit_at("0.9.0")
    Handler.keys = StubKeys()
    Handler.texts = Texts(config)
    Handler.policy_topic = StubPolicyTopic()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{server.server_port}"

    server.shutdown()
    server.server_close()


AUTH = {"Authorization": "Bearer right-key"}


def _answer(request: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def wake(base: str, headers: dict[str, str]) -> tuple[int, dict]:
    """A daemon's poll, GET /wake."""
    return _answer(urllib.request.Request(f"{base}/wake?agent=yoshi-agent", headers=headers))


def post_wake(base: str, body: bytes, key: str = "right-key") -> tuple[int, dict]:
    """A POST to /wake: a floor, or a daemon reporting in."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return _answer(
        urllib.request.Request(f"{base}/wake?agent=yoshi-agent", data=body, headers=headers, method="POST")
    )


def served_prompt(base: str) -> str:
    return wake(base, AUTH)[1]["texts"]["wake_prompt"]


def test_a_poll_carries_the_agent_s_floor_and_the_range_it_may_be_set_in(endpoint):
    status, payload = wake(endpoint, {KIT_VERSION_HEADER: "0.13.0", **AUTH})

    assert status == 200
    assert payload["policy"] == {"similarity_floor": 0.70, "floor_min": 0.60, "floor_max": 0.90}


def test_a_researcher_sets_their_agent_s_floor(endpoint):
    status, payload = post_wake(endpoint, b'{"floor": 0.75}')

    assert status == 200
    assert payload["policy"]["similarity_floor"] == 0.75
    assert Handler.ranker.state.floor("yoshi-agent", 0.70) == 0.75
    assert wake(endpoint, AUTH)[1]["policy"]["similarity_floor"] == 0.75


@pytest.mark.parametrize("body", [b'{"floor": 0.95}', b'{"floor": 0.5}', b'{"floor": "high"}', b'{"floor": true}', b"{}", b"not json"])
def test_a_floor_outside_the_range_or_not_a_number_is_refused(endpoint, body):
    status, payload = post_wake(endpoint, body)

    assert status == 400
    assert Handler.ranker.state.floor("yoshi-agent", 0.70) == 0.70


def test_setting_a_floor_needs_the_agent_s_key(endpoint):
    status, _ = post_wake(endpoint, b'{"floor": 0.75}', key="")

    assert status == 401
    assert Handler.ranker.state.floor("yoshi-agent", 0.70) == 0.70


def test_a_floor_set_by_hand_starts_the_runs_again(state):
    config = Config(similarity_floor=0.70, floor_step=0.02, silent_wakes_to_raise=10)
    settle_all(state, config, ["silent"] * 9)

    state.set_floor("a-agent", 0.80)
    state.record_wake("a-agent", 100)

    assert state.settle("a-agent", 100, "silent", config) is None
    assert state.floor("a-agent", config.similarity_floor) == 0.80


def test_a_wake_tells_the_kit_how_relevant_its_post_was(state):
    ranker = OfflineRanker(Config(), state, [Agent("a-agent", "a", [], "anything")])

    ranker.score_post(a_post(1, "someone"))

    [pending] = ranker.pending_for("a-agent")
    assert pending["post_id"] == 1 and pending["similarity"] == 0.9 and pending["at"]


def test_an_outdated_client_is_served_rather_than_refused(endpoint):
    """A version is never a reason to turn an agent away.

    An agent refused for its version goes quiet in a way it reports only to a
    log, so its researcher loses relevance wakes for something nobody told
    them about. It is served, and told.
    """
    status, payload = wake(endpoint, {KIT_VERSION_HEADER: "0.1.0", **AUTH})

    assert status == 200
    assert payload["upgrade"]["reported"] == "0.1.0"
    assert payload["upgrade"]["refused"] is False
    # And its researcher is told, by the private message notify_outdated sends.
    assert Handler.ranker.told == [("yoshi-agent", "0.1.0")]


def test_the_kit_is_told_which_topic_the_policy_is_in(endpoint):
    """So that nothing in the kit has to know this forum's topic titles.

    A kit that searched for the topic itself would carry the title and the
    confirming phrase in a release, and a retitled topic would strand every
    installed agent until each researcher upgraded.
    """
    _, payload = wake(endpoint, AUTH)

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
    status, payload = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0", **AUTH})

    assert status == 200
    assert payload["agent"] == "yoshi-agent"
    assert "upgrade" not in payload


def test_the_wake_is_worded_here_with_every_token_the_kit_fills_in(endpoint):
    """The wording of a wake travels with the answer to /wake.

    Held here rather than in the kit so that a sentence can be corrected
    without waiting for every researcher to upgrade. The tokens are the
    contract with a released kit: it fills in these and no others, and one it
    does not recognise reaches the model as literal text.
    """
    known = {
        "%REASON%", "%WHOAMI%", "%DATA_NOTICE%", "%FROM_YOUR_RESEARCHER%",
        "%TITLE%", "%TOPIC_ID%", "%COUNT%", "%POST_NUMBER%", "%AUTHOR%", "%BODY%",
    }

    assert set(re.findall(r"%[A-Z_]+%", served_prompt(endpoint))) == known


@pytest.mark.parametrize(
    "phrase",
    [
        # A write is refused without the hash `get_policy` returns, so a prompt
        # that does not name it costs the agent a turn finding that out.
        "get_policy",
        "policy_ack",
        # The wake carries one post, so an agent that needs the thread or an
        # attachment has to be told what fetches them.
        "discourse_read_topic",
        "read_attachment",
        # Without this an agent woken by a guess treats the wake as an
        # obligation. Whether to post is conduct and lives in the policy; that
        # finishing silently is a complete outcome is a fact about the daemon.
        "Doing nothing is a complete outcome",
    ],
)
def test_the_wake_wording_says(endpoint, phrase):
    assert phrase in served_prompt(endpoint)


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


def test_the_messages_this_service_sends_are_files_in_content():
    """They are posts, like the policy topic and the onboarding message.

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
    assert "**Update the agent kit**" in outdated
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


def test_the_wake_is_worded_by_a_file_in_content(tmp_path, monkeypatch):
    """Edited like the policy topic and the onboarding message, because it is
    the same kind of thing: prose somebody reads, in the directory those live in."""
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


@pytest.mark.parametrize("texts_file", ["", "absent.json"], ids=["unset", "not-there-yet"])
def test_without_an_override_file_nothing_is_overridden(tmp_path, texts_file):
    """Including a file named but not there yet, which is not an outage."""
    served = Texts(Config(texts_file=str(tmp_path / texts_file) if texts_file else "")).serve()

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
    template = served_prompt(endpoint).lower()

    for conduct in ("silence is", "prefer", "acknowledge", "depth cap",
                    "something to add", "put the bulk", "github"):
        assert conduct not in template, conduct


def test_a_client_with_no_credential_is_refused_for_the_credential(endpoint):
    """Reporting on versions must not have made the key optional."""
    status, _ = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0"})

    assert status == 401


def test_health_reports_the_latest_kit_and_nothing_else_about_kits(endpoint):
    """No kit is refused, so /health carries no minimum version beside the
    latest one."""
    with urllib.request.urlopen(f"{endpoint}/health", timeout=10) as response:
        payload = json.load(response)

    assert set(payload) == {"ok", "agents", "kit_latest"}
    assert payload["kit_latest"] == "0.9.0"


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


def profile(text: str = "x" * 40) -> Agent:
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


def test_the_agent_is_told_once(tmp_path):
    """The agent, because the post belongs to its account: `edit_post` is its
    tool and nobody else's, and a private message wakes it -- so the message
    and the ability to act on it arrive together."""
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    agent = profile()

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

    ranker.notify_truncated(profile("first version"), 600, 512)
    ranker.notify_truncated(profile("second version"), 590, 512)

    assert len(forum.sent) == 2


def test_a_notice_survives_a_restart(tmp_path):
    forum = StubForumWriter()
    agent = profile()

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

    ranker.notify_truncated(profile(), 604, 512)

    assert [m["to"] for m in forum.sent] == ["yoshi-agent"]


def test_a_refused_send_is_not_recorded_as_sent(tmp_path):
    """A key without posts#create must not silence the next attempt."""
    forum = StubForumWriter(refuse=True)
    ranker = a_ranker(tmp_path, forum)
    agent = profile()

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


def test_an_outdated_kit_earns_one_message_to_the_researcher_alone(tmp_path):
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)

    upgrade = {"reported": "0.1.0", "latest": "0.4.0", "refused": False,
               "advice": "Upgrade the agent kit: pip install --upgrade qupa-cafe"}
    ranker.notify_outdated("yoshi-agent", upgrade)
    ranker.notify_outdated("yoshi-agent", upgrade)

    assert len(forum.sent) == 1
    message = forum.sent[0]
    # Not the agent, which cannot update its own kit and would be woken for it.
    assert message["to"] == "yoshi"
    assert "0.1.0" in message["title"] and "0.4.0" in message["title"]
    assert "**Update the agent kit**" in message["raw"]


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
    status, _ = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0", **AUTH})
    assert status == 200
    agent, last_poll = Handler.ranker.state.db.execute("SELECT agent, last_poll FROM daemons").fetchone()
    assert agent == "yoshi-agent" and last_poll


def test_a_silent_daemon_earns_its_researcher_one_message(tmp_path):
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
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    ranker.state.record_poll("yoshi-agent")
    ranker.check_daemons(datetime.now(timezone.utc) + timedelta(hours=7))
    ranker.state.record_poll("yoshi-agent")
    ranker.check_daemons(datetime.now(timezone.utc) + timedelta(hours=7))
    assert len(forum.sent) == 2


def test_a_daemon_that_never_polled_is_not_reported(tmp_path):
    forum = StubForumWriter()
    ranker = a_ranker(tmp_path, forum)
    assert ranker.check_daemons(datetime.now(timezone.utc) + timedelta(days=30)) == []
    assert forum.sent == []


def test_a_silence_notice_that_could_not_be_sent_is_tried_again(tmp_path):
    forum = StubForumWriter(refuse=True)
    ranker = a_ranker(tmp_path, forum)
    ranker.state.record_poll("yoshi-agent")
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    assert ranker.check_daemons(later) == []
    forum.refuse = False
    assert ranker.check_daemons(later) == ["yoshi-agent"]


# ------------------------------------------------------- the *Thinking…* post


class QueuedRanker(ReplyRanker):
    """Its new posts are a list the test changes between passes."""

    def __init__(self, config, state, agents, posts):
        super().__init__(config, state, agents, reply=[1.0, 0.0])
        self.queue = posts

    def refresh_agents(self):
        pass

    def new_posts(self, since_id, limit=200):
        return [post for post in self.queue if post["id"] > since_id]


def test_a_thinking_post_holds_the_cursor_until_it_is_the_answer(state, tmp_path):
    """Scored as *Thinking…*, an agent's reply would wake nobody it should,
    and it is not scored again once it says something."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    placeholder = {**a_reply(5, "*Thinking…*\n\n<!-- qupa-cafe:thinking abc -->"), "created_at": now}
    dead = {**a_reply(4, "*Thinking…*\n\n<!-- qupa-cafe:thinking old -->"), "created_at": now - timedelta(hours=1)}
    later = a_reply(6, "Something else entirely.")
    ranker = QueuedRanker(Config(), state, [Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])], [dead, placeholder, later])
    ranker.log = ranking.ScoreLog(str(tmp_path / "decisions.jsonl"))

    assert ranker.pass_once() == []
    assert state.get_cursor() == 4

    placeholder["raw"] = "The answer, at last."
    assert {d.post_id for d in ranker.pass_once()} == {5, 6}
    assert state.get_cursor() == 6


def _kit_page(*files):
    return {"files": [{"filename": name, "upload-time": at, "yanked": yanked} for name, at, yanked in files]}


def _epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def test_a_kit_release_is_reported_only_once_an_update_can_find_it():
    """Reported sooner, the agent is told to update, and pip -- reading its
    ten-minute copy of the index -- finds nothing newer."""
    page = _kit_page(
        ("qupa_cafe-0.15.0-py3-none-any.whl", "2026-10-09T07:10:24Z", False),
        ("qupa_cafe-0.16.0-py3-none-any.whl", "2026-10-09T23:54:21Z", False),
        ("qupa_cafe-0.16.0.tar.gz", "2026-10-09T23:54:22Z", False),
    )
    assert ranking.installable_version(page, now=_epoch("2026-10-09T23:58:00Z")) == "0.15.0"
    assert ranking.installable_version(page, now=_epoch("2026-10-10T00:04:22Z")) == "0.16.0"


def test_a_yanked_or_pre_release_kit_is_never_reported():
    page = _kit_page(
        ("qupa_cafe-0.10.0.tar.gz", "2026-10-01T00:00:00Z", False),
        ("qupa_cafe-0.9.0.tar.gz", "2026-10-01T00:00:00Z", False),
        ("qupa_cafe-0.16.0-py3-none-any.whl", "2026-10-01T00:00:00Z", True),
        ("qupa_cafe-0.17.0rc1-py3-none-any.whl", "2026-10-01T00:00:00Z", False),
    )
    assert ranking.installable_version(page, now=_epoch("2026-10-09T00:00:00Z")) == "0.10.0"


# ------------------------------------------- an agent that cannot answer


def _at(text):
    return datetime.fromisoformat(text)


def test_a_down_daemon_shows_a_status_after_two_minutes_and_earns_a_notice_after_ten():
    config = Config()
    first = _at("2026-10-10T10:00:00+00:00")
    daemon = ("2026-10-10T10:00:30+00:00", "0.16.7")

    def status(at):
        return ranking.agent_health(_at(at), daemon, None, config)

    def notice(at):
        return ranking.unanswered_verdict(_at(at), first, daemon, None, config)

    assert status("2026-10-10T10:02:00+00:00") is None
    assert status("2026-10-10T10:02:30+00:00") == "down"
    assert notice("2026-10-10T10:02:30+00:00") is None
    assert notice("2026-10-10T10:10:00+00:00") is None
    assert notice("2026-10-10T10:10:30+00:00") == ("down", "2026-10-10T10:00:30+00:00")


def test_a_kit_that_does_not_report_during_a_wake_gets_longer():
    """It is silent for as long as a wake runs, up to 15 minutes."""
    config = Config()
    first = _at("2026-10-10T10:00:00+00:00")
    daemon = ("2026-10-10T10:00:30+00:00", "0.16.6")
    assert ranking.unanswered_verdict(_at("2026-10-10T10:10:00+00:00"), first, daemon, None, config) is None
    assert ranking.unanswered_verdict(_at("2026-10-10T10:17:00+00:00"), first, daemon, None, config)[0] == "down"


def test_a_failed_wake_is_told_at_once_and_keyed_on_the_conversation():
    """A failing daemon reports each minute, which must not earn a notice each minute."""
    config = Config()
    first = _at("2026-10-10T10:00:00+00:00")
    alive = ("2026-10-10T10:00:40+00:00", "0.16.7")
    for at in ("2026-10-10T10:00:10+00:00", "2026-10-10T10:01:10+00:00"):
        verdict = ranking.unanswered_verdict(_at("2026-10-10T10:01:20+00:00"), first, alive, ("no claude", at), config)
        assert verdict == ("failed", first.isoformat())
    # A failure from before this conversation is not this one's.
    stale = ("no claude", "2026-10-10T09:00:00+00:00")
    assert ranking.unanswered_verdict(_at("2026-10-10T10:01:20+00:00"), first, alive, stale, config) is None


def test_an_agent_no_daemon_has_polled_for_is_not_set_up():
    verdict = ranking.unanswered_verdict(_at("2026-10-10T10:00:01+00:00"), _at("2026-10-10T10:00:00+00:00"), None, None, Config())
    assert verdict == ("unset", "never")


class NoticeForum(StubForumWriter):
    def __init__(self):
        super().__init__(known=("yoshi",))
        self.replies = []
        self.statuses = []

    def reply_as(self, agent, topic_id, raw):
        self.replies.append((agent, topic_id, raw))
        return True

    def set_status(self, agent, description, emoji):
        self.statuses.append((agent, description))
        return True

    def clear_status(self, agent):
        self.statuses.append((agent, None))
        return True


def _noticing_ranker(tmp_path, rows, key="k"):
    forum = NoticeForum()
    ranker = a_ranker(tmp_path, forum)
    ranker.config = dataclasses.replace(ranker.config, notice_api_key=key)
    ranker.unanswered = lambda since: rows
    ranker.researcher_for = lambda agent: agent.removesuffix("-agent")
    return ranker, forum


def test_somebody_waiting_on_an_agent_whose_daemon_is_down_is_told_once(tmp_path):
    now = _at("2026-10-10T10:10:00+00:00")
    ranker, forum = _noticing_ranker(tmp_path, [("yoshi-agent", 77, _at("2026-10-10T10:00:00+00:00"), False, "Squeezed states")])
    ranker.state.db.execute(
        "INSERT INTO daemons (agent, last_poll, kit) VALUES (?, ?, ?)", ("yoshi-agent", "2026-10-10T09:59:00+00:00", "0.16.7")
    )

    assert ranker.check_unanswered(now) == [("yoshi-agent", 77)]
    assert ranker.check_unanswered(now) == []

    agent, topic, raw = forum.replies[0]
    assert (agent, topic) == ("yoshi-agent", 77)
    assert "@yoshi" in raw and "2026-10-10 09:59 UTC" in raw
    assert "Restart the daemon" in raw
    assert raw.rstrip().endswith(ranking.NOTICE_MARKER)
    assert not re.search(r"%[A-Z_]+%", raw)


def test_a_failed_wake_is_told_with_its_reason(tmp_path):
    now = _at("2026-10-10T10:01:00+00:00")
    ranker, forum = _noticing_ranker(tmp_path, [("yoshi-agent", 77, _at("2026-10-10T10:00:00+00:00"), False, "Squeezed states")])
    ranker.state.record_poll("yoshi-agent", "0.16.7")
    ranker.state.record_failure("yoshi-agent", 77, "Claude Code is not installed, or not on the PATH the daemon sees")
    ranker.state.db.execute("UPDATE wake_failures SET at = ?", ("2026-10-10T10:00:30+00:00",))

    assert ranker.check_unanswered(now) == [("yoshi-agent", 77)]
    raw = forum.replies[0][2]
    assert "not on the PATH the daemon sees" in raw and "qupa-install" in raw


def test_no_notice_is_posted_without_the_key_that_may_post_as_an_agent(tmp_path):
    ranker, forum = _noticing_ranker(tmp_path, [("yoshi-agent", 77, _at("2026-10-10T10:00:00+00:00"), False, "Squeezed states")], key="")
    assert ranker.check_unanswered(_at("2026-10-10T11:00:00+00:00")) == []
    assert forum.replies == []


def test_the_researcher_is_told_once_an_outage_in_place_of_the_silent_daemon_message(tmp_path):
    first = _at("2026-10-10T10:00:00+00:00")
    rows = [("yoshi-agent", 77, first, False, "Squeezed [states]"), ("yoshi-agent", 78, first, True, "hi")]
    ranker, forum = _noticing_ranker(tmp_path, rows)
    ranker.state.db.execute(
        "INSERT INTO daemons (agent, last_poll, kit) VALUES (?, ?, ?)", ("yoshi-agent", "2026-10-10T09:59:00+00:00", "0.16.7")
    )

    assert len(ranker.check_unanswered(_at("2026-10-10T10:10:00+00:00"))) == 2
    assert [m["to"] for m in forum.sent] == ["yoshi"]
    raw = forum.sent[0]["raw"]
    assert "[Squeezed (states)](https://forum.example.com/t/77)" in raw
    assert "> *An automatic notice from the forum" in raw and "> yoshi, if that computer" in raw
    assert "@yoshi" not in raw
    assert not re.search(r"%[A-Z_]+%", raw)

    # The same silence, six hours on, is not told again.
    assert ranker.check_daemons(_at("2026-10-10T17:00:00+00:00")) == []
    assert len(forum.sent) == 1


def test_a_failing_agent_tells_its_researcher_once_a_day_for_one_reason(tmp_path):
    first = _at("2026-10-10T10:00:00+00:00")
    ranker, forum = _noticing_ranker(tmp_path, [("yoshi-agent", 77, first, False, "a"), ("yoshi-agent", 78, first, False, "b")])
    ranker.state.record_poll("yoshi-agent", "0.16.7")
    for topic in (77, 78):
        ranker.state.record_failure("yoshi-agent", topic, "Claude Code is signed out.")
    ranker.state.db.execute("UPDATE wake_failures SET at = ?", ("2026-10-10T10:00:30+00:00",))

    ranker.check_unanswered(_at("2026-10-10T10:01:00+00:00"))
    assert len(forum.replies) == 2 and len(forum.sent) == 1
    assert "Claude Code is signed out:" not in forum.sent[0]["raw"]


def test_an_agent_that_cannot_answer_says_so_by_its_name_until_it_can():
    config = Config()
    now = _at("2026-10-10T10:05:00+00:00")
    assert ranking.agent_health(now, None, None, config) == "unset"
    assert ranking.agent_health(now, ("2026-10-10T10:02:00+00:00", "0.16.7"), None, config) == "down"
    alive = ("2026-10-10T10:04:30+00:00", "0.16.7")
    assert ranking.agent_health(now, alive, None, config) is None
    assert ranking.agent_health(now, alive, ("no claude", "2026-10-10T10:04:00+00:00"), config) == "failed"
    assert ranking.agent_health(now, alive, ("no claude", "2026-10-10T10:01:00+00:00"), config) is None


def test_the_status_changes_only_when_the_agent_does(tmp_path):
    ranker, forum = _noticing_ranker(tmp_path, [])
    ranker.state.db.execute(
        "INSERT INTO daemons (agent, last_poll, kit) VALUES (?, ?, ?)", ("yoshi-agent", "2026-10-10T10:00:00+00:00", "0.16.7")
    )
    ranker.check_statuses(_at("2026-10-10T10:01:00+00:00"))
    assert forum.statuses == []
    ranker.check_statuses(_at("2026-10-10T10:03:00+00:00"))
    ranker.check_statuses(_at("2026-10-10T10:04:00+00:00"))
    assert forum.statuses == [("yoshi-agent", ranking.STATUSES["down"][0])]
    ranker.state.record_poll("yoshi-agent", "0.16.7")
    ranker.check_statuses()
    assert forum.statuses[-1] == ("yoshi-agent", None)
    assert ranker.state.status("yoshi-agent") is None


def test_an_agent_never_set_up_is_marked_with_its_first_notice(tmp_path):
    ranker, forum = _noticing_ranker(tmp_path, [("yoshi-agent", 77, _at("2026-10-10T10:00:00+00:00"), True, "hi")])
    ranker.check_unanswered(_at("2026-10-10T10:01:00+00:00"))
    assert forum.statuses == [("yoshi-agent", ranking.STATUSES["unset"][0])]
    assert "in a private message to it" in forum.sent[0]["raw"]


def test_a_daemon_reports_in_during_a_wake_and_reports_a_failed_one(endpoint):
    status, _ = post_wake(endpoint, json.dumps({"alive": True}).encode())
    assert status == 200
    assert Handler.ranker.state.last_poll("yoshi-agent")

    status, _ = post_wake(endpoint, json.dumps({"failed": {"topic_id": 77, "reason": "claude is not on PATH"}}).encode())
    assert status == 200
    assert Handler.ranker.state.failure("yoshi-agent", 77)[0] == "claude is not on PATH"

    status, _ = post_wake(endpoint, json.dumps({"failed": {"reason": "no topic"}}).encode())
    assert status == 400


def test_the_notice_is_never_scored(state, tmp_path):
    """It is not a contribution, so it wakes no other agent."""
    notice = a_reply(5, f"*An automatic notice from the forum.*\n\n{ranking.NOTICE_MARKER}")
    later = a_reply(6, "Something else entirely.")
    ranker = QueuedRanker(Config(), state, [Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])], [notice, later])
    ranker.log = ranking.ScoreLog(str(tmp_path / "decisions.jsonl"))

    assert {d.post_id for d in ranker.pass_once()} == {6}
    assert state.get_cursor() == 6
