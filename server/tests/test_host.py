"""Tests for host.py, with Postgres, the forum and the embedding server
replaced by stubs.

Run from the repository root::

    python3 -m pytest server/tests -q

Requires psycopg, because host.py runs on the forum host and imports it at
module level. The agent kit has no such dependency.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
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

import host as host_module  # noqa: E402
from host import (  # noqa: E402
    Texts,
    KIT_VERSION_HEADER,
    Agent,
    Config,
    Decision,
    Handler,
    KeyChecker,
    KitVersions,
    PolicyTopic,
    Host,
    State,
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


def blockquote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines())


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
    agents = parse_agents([post(1, "t"), post(2, blockquote(WELL_FORMED))])

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


def test_the_exclusions_are_not_part_of_the_profile():
    """Folded in, they would pull the profile towards what they name."""
    agent = Agent("a", "r", [], interests="cavity QED", not_interested="cosmology")
    assert agent.profile_text == "cavity QED"


# ----------------------------------------------------------------- the ledger


def a_state(path) -> State:
    """A State that skips the fsync at each commit. These tests never lose
    power, and a hundred fsyncs take seconds on a busy Windows disk."""
    state = State(str(path))
    state.db.execute("PRAGMA synchronous=NORMAL")
    return state


@pytest.fixture
def state(tmp_path):
    return a_state(tmp_path / "host.sqlite3")


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
    path = str(tmp_path / "host.sqlite3")

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
    path = str(tmp_path / "host.sqlite3")
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


class OfflineHost(Host):
    """A host whose database reads are answered in memory: every topic
    scores 0.9 against every agent."""

    def __init__(self, config: Config, state: State, agents: list[Agent]):
        self.config = config
        self.state = state
        self.agents = agents

    def similarity(self, topic_id: int, vector: list[float]) -> float | None:
        return 0.9


def fake_connection(asked: list | None = None, one=None, rows=()):
    """A database connection whose cursor appends each query and its
    parameters to ``asked``, and answers fetchone with ``one`` and fetchall
    with ``rows``."""

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def fetchone(self): return one
        def fetchall(self): return rows

        def execute(self, sql, parameters):
            if asked is not None:
                asked.append((sql, parameters))

    class Connection(Cursor):
        def cursor(self): return Cursor()

    return Connection()


def a_post(post_id: int, username: str) -> dict:
    return {
        "id": post_id, "topic_id": post_id, "post_number": 1, "username": username,
        "category_name": "arXiv quant-ph", "is_agent": False,
    }


def test_no_number_of_wakes_stops_an_agent_being_woken(state):
    """How often an agent acts on its wakes is its researcher's call, so the
    host keeps waking it, for feed topics and forum posts alike."""
    config = Config(feed_usernames=("arxiv-bot",))
    host = OfflineHost(config, state, [Agent("a-agent", "a", [], "anything")])

    decisions = [d for n in range(1, 101)
                 for d in host.score_post(a_post(n, "arxiv-bot" if n % 2 else "someone"))]

    assert {d.reason for d in decisions} == {"woken"}
    assert wakes_today(state, "a-agent", "feed") == 50
    assert wakes_today(state, "a-agent") == 50


def a_reply(post_id: int, raw: str = "Thanks, that helps.") -> dict:
    return {**a_post(post_id, "someone"), "post_number": 2, "title": "Dispersive readout", "raw": raw}


def test_a_reply_is_scored_by_its_thread_as_the_first_post_is(state):
    """OfflineHost scores every topic 0.9; a reply's own words are not embedded."""
    agent = Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])
    host = OfflineHost(Config(similarity_floor=0.70), state, [agent])

    [decision] = host.score_post(a_reply(5))

    assert (decision.reason, decision.similarity) == ("woken", 0.9)


def test_a_wake_tells_the_kit_how_relevant_its_post_was(state):
    host = OfflineHost(Config(), state, [Agent("a-agent", "a", [], "anything")])

    host.score_post(a_post(1, "someone"))

    [pending] = host.pending_for("a-agent")
    assert pending["post_id"] == 1 and pending["similarity"] == 0.9 and pending["at"]


def test_a_wake_just_before_midnight_is_still_pending_after_it(state):
    """The daemon polls a minute later, on the next UTC day."""
    host = OfflineHost(Config(), state, [])
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    state.db.execute(
        "INSERT INTO wakes (agent, post_id, day, at, kind) VALUES ('a-agent', 7, ?, ?, 'forum')",
        (yesterday, an_hour_ago),
    )

    assert [item["post_id"] for item in host.pending_for("a-agent")] == [7]


def test_every_wake_of_the_last_day_is_pending_however_many(state):
    """The daemon skips what it has handled, so a list cut to the newest few
    would hand it the same handled wakes at every poll and never the rest: a
    day's feed can wake one agent for eighty preprints."""
    host = OfflineHost(Config(), state, [])
    for post_id in range(1, 81):
        state.record_wake("a-agent", post_id, kind="feed")

    assert [item["post_id"] for item in host.pending_for("a-agent")] == list(range(80, 0, -1))


def test_a_wake_more_than_a_day_old_is_no_longer_pending(state):
    host = OfflineHost(Config(), state, [])
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    state.db.execute(
        "INSERT INTO wakes (agent, post_id, day, at, kind) VALUES ('a-agent', 7, '2026-01-01', ?, 'forum')", (old,)
    )

    assert host.pending_for("a-agent") == []


# ------------------------------------------------------- each agent's floor


def test_the_floor_survives_a_restart(tmp_path):
    path = str(tmp_path / "host.sqlite3")
    State(path).set_floor("a-agent", 0.68)
    assert State(path).floor("a-agent", 0.70) == 0.68


def test_the_ranker_compares_with_the_agent_s_own_floor(state):
    """OfflineHost scores everything 0.9."""
    config = Config(similarity_floor=0.70, floor_max=1.0)
    host = OfflineHost(config, state, [Agent("a-agent", "a", [], "anything"), Agent("b-agent", "b", [], "anything")])
    state.set_floor("a-agent", 0.95)

    decisions = {d.agent: d for d in host.score_post(a_post(1, "someone"))}

    assert decisions["a-agent"].reason == "below-floor"
    assert decisions["a-agent"].floor == 0.95
    assert decisions["b-agent"].reason == "woken"
    assert decisions["b-agent"].floor == 0.70


def test_every_agent_over_its_own_floor_is_woken(state):
    """Each agent is judged against its own floor alone: a preprint two
    researchers care about reaches both of them."""
    agents = [Agent("a-agent", "a", [], "anything"), Agent("b-agent", "b", [], "anything")]
    host = OfflineHost(Config(similarity_floor=0.70), state, agents)

    decisions = host.score_post(a_post(1, "arxiv-bot"))

    assert {(d.agent, d.reason) for d in decisions} == {("a-agent", "woken"), ("b-agent", "woken")}
    assert state.already_woken("a-agent", 1) and state.already_woken("b-agent", 1)


# ------------------------------------------------------------ exclusions


class VectorHost(OfflineHost):
    """A topic scores against each vector as ``scores`` says."""

    def __init__(self, config, state, agents, scores):
        super().__init__(config, state, agents)
        self.scores = scores

    def similarity(self, topic_id, vector):
        return self.scores[tuple(vector)]


INTERESTS, EXCLUSION = [1.0, 0.0], [0.0, 1.0]


def excluding_agent():
    return Agent("a-agent", "a", [], "circuits", "algorithms", profile_vector=INTERESTS, exclusion_vector=EXCLUSION)


def test_a_post_matching_the_exclusions_at_least_as_well_as_the_interests_does_not_wake(state):
    scores = {tuple(INTERESTS): 0.80, tuple(EXCLUSION): 0.82}
    host = VectorHost(Config(similarity_floor=0.70), state, [excluding_agent()], scores)

    [decision] = host.score_post({**a_post(1, "arxiv-bot"), "title": "A QAOA review"})

    assert (decision.reason, decision.woke, decision.similarity, decision.exclusion) == ("excluded", False, 0.80, 0.82)
    assert not state.already_woken("a-agent", 1)


def test_a_post_matching_the_interests_better_wakes_and_logs_the_exclusion_s_score(state):
    scores = {tuple(INTERESTS): 0.85, tuple(EXCLUSION): 0.75}
    host = VectorHost(Config(similarity_floor=0.70), state, [excluding_agent()], scores)

    [decision] = host.score_post(a_post(1, "arxiv-bot"))

    assert (decision.reason, decision.exclusion) == ("woken", 0.75)
    assert state.already_woken("a-agent", 1)


def test_the_exclusions_are_checked_only_above_the_floor_and_only_when_there_are_some(state):
    below = VectorHost(Config(similarity_floor=0.90), state, [excluding_agent()],
                         {tuple(INTERESTS): 0.80, tuple(EXCLUSION): 0.99})
    assert below.score_post(a_post(1, "arxiv-bot"))[0].reason == "below-floor"

    plain = Agent("b-agent", "b", [], "circuits", profile_vector=INTERESTS)
    none = VectorHost(Config(similarity_floor=0.70), state, [plain], {tuple(INTERESTS): 0.80})
    [decision] = none.score_post(a_post(2, "arxiv-bot"))
    assert (decision.reason, decision.exclusion) == ("woken", None)


class InterestsForum:
    """The interests topic's posts, which is all refresh_agents reads."""

    def __init__(self, posts):
        self.posts = posts

    def interests_posts(self, topic_id):
        return self.posts


def test_changing_only_the_exclusions_embeds_them_afresh(state, monkeypatch):
    embedded = []
    monkeypatch.setattr(host_module, "embed", lambda text, config: embedded.append(text) or [0.5, 0.5])
    monkeypatch.setattr(host_module, "report_profile_length", lambda agent, config: None)

    def post(exclusions):
        return [{"username": "a-agent", "raw": "**Researcher:** a\n**Agent account:** @a-agent\n"
                 f"**Watching for:** circuits\n**Not interested in:** {exclusions}\n"}]

    host = OfflineHost(Config(interests_topic_id=13), state, [])
    host.agents_hash = ""
    host.forum = InterestsForum(post("algorithms"))
    host.refresh_agents()
    host.forum.posts = post("codes")
    host.refresh_agents()

    assert embedded == ["circuits", "algorithms", "circuits", "codes"]
    assert host.agents[0].exclusion_vector == [0.5, 0.5]


# ----------------------------------------- categories an agent is not woken in


class PrivateHost(OfflineHost):
    """Category 6 is read by its group's members alone."""

    def readers(self, category_id):
        return {"b-agent"} if category_id == 6 else set()


def test_a_post_in_a_private_category_wakes_only_the_agents_who_can_read_it(state):
    agents = [Agent("a-agent", "a", [], "x"), Agent("B-agent", "b", [], "x")]
    host = PrivateHost(Config(similarity_floor=0.5), state, agents)
    post = {**a_post(1, "someone"), "category_id": 6, "category_name": "Schuster lab", "read_restricted": True}
    decisions = {d.agent: d.reason for d in host.score_post(post)}
    assert decisions == {"a-agent": "cannot-see", "B-agent": "woken"}


def test_a_post_in_a_skipped_category_wakes_nobody(state):
    host = OfflineHost(Config(), state, [Agent("a-agent", "a", ["arxiv quant-ph"], "x")])
    [decision] = host.score_post(a_post(1, "arxiv-bot"))
    assert (decision.reason, decision.woke) == ("category-skipped", False)


# -------------------------------------------------------------- the defaults


def test_the_feed_account_is_the_documented_one():
    """server/README.md gives this default, and the arXiv feed posts as it."""
    assert Config().feed_usernames == ("arxiv-bot",)


def test_the_similarity_floor_sits_in_the_measured_gap():
    """Measured with bge-m3 against a circuit-QED profile: of a day's 65
    preprints, the two on its subject scored 0.616 and 0.574, and the median
    0.489. The floor has to let both through and hold back at least half of
    the day; see server/README.md."""
    floor = Config().similarity_floor

    assert 0.489 < floor <= 0.574


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


def test_the_complaint_throttle_is_bounded():
    """Its keys come from a request header, so it cannot grow without limit."""
    kit = KitVersions(Config())

    for n in range(KitVersions.COMPLAINT_KEYS * 3):
        kit._complain_once(f"refused:{n}")

    assert len(kit._complained) <= KitVersions.COMPLAINT_KEYS


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
    assert host_module.installable_version(page, now=_epoch("2026-10-09T23:58:00Z")) == "0.15.0"
    assert host_module.installable_version(page, now=_epoch("2026-10-10T00:04:22Z")) == "0.16.0"


def test_a_yanked_or_pre_release_kit_is_never_reported():
    page = _kit_page(
        ("qupa_cafe-0.10.0.tar.gz", "2026-10-01T00:00:00Z", False),
        ("qupa_cafe-0.9.0.tar.gz", "2026-10-01T00:00:00Z", False),
        ("qupa_cafe-0.16.0-py3-none-any.whl", "2026-10-01T00:00:00Z", True),
        ("qupa_cafe-0.17.0rc1-py3-none-any.whl", "2026-10-01T00:00:00Z", False),
    )
    assert host_module.installable_version(page, now=_epoch("2026-10-09T00:00:00Z")) == "0.10.0"


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


# ------------------------------------------------- /wake over real HTTP


class StubHost:
    """Enough Host for the handler. Nothing is pending, which is not the
    point of these tests."""

    def __init__(self, config: Config, state: State):
        self.config = config
        self.agents: list[Agent] = []
        self.told: list[tuple] = []
        self.state = state

    def pending_for(self, agent: str) -> list[dict]:
        return []

    floor_for = Host.floor_for
    interests_for = Host.interests_for
    to_slack = Host.to_slack

    def researcher_for(self, agent: str) -> str:
        return agent.removesuffix("-agent")

    def notify_outdated(self, agent, upgrade):
        self.told.append((agent, upgrade.get("reported")))

    def policy_posts(self, agent, policy_topic_id):
        return [{"post_id": 13, "topic_id": policy_topic_id, "updated_at": "2026-10-26T02:09:42.955Z",
                 "category_id": None, "category": None, "parent_category_id": None}]


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

    Handler.host = StubHost(config, state)
    Handler.kit = kit_at("0.9.0")
    Handler.keys = StubKeys()
    Handler.texts = Texts(config)
    Handler.policy_topic = StubPolicyTopic()

    server = serve(Handler)

    yield f"http://127.0.0.1:{server.server_port}"

    server.shutdown()
    server.server_close()


def serve(handler) -> HTTPServer:
    """An HTTP server on a free local port, answering from a thread.

    It checks for shutdown every 10 ms rather than serve_forever's default
    half second, which would otherwise be the half second each test using it
    takes to tear down.
    """
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return server


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


def post_wake(base: str, body: dict | bytes, key: str = "right-key") -> tuple[int, dict]:
    """A POST to /wake, of ``body`` as JSON, or as it is if it is bytes."""
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return _answer(
        urllib.request.Request(f"{base}/wake?agent=yoshi-agent", data=body, headers=headers, method="POST")
    )


def test_a_poll_carries_the_agent_s_floor_and_the_range_it_may_be_set_in(endpoint):
    status, payload = wake(endpoint, {KIT_VERSION_HEADER: "0.13.0", **AUTH})

    assert status == 200
    assert payload["policy"] == {"similarity_floor": 0.50, "floor_min": 0.0, "floor_max": 1.0}


def test_a_researcher_sets_their_agent_s_floor(endpoint):
    status, payload = post_wake(endpoint, {"floor": 0.75})

    assert status == 200
    assert payload["policy"]["similarity_floor"] == 0.75
    assert Handler.host.state.floor("yoshi-agent", 0.70) == 0.75
    assert wake(endpoint, AUTH)[1]["policy"]["similarity_floor"] == 0.75


@pytest.mark.parametrize("floor", [0.0, 0.3, 0.95, 1.0])
def test_a_floor_anywhere_from_0_to_1_is_taken(endpoint, floor):
    status, payload = post_wake(endpoint, {"floor": floor})

    assert status == 200
    assert payload["policy"]["similarity_floor"] == floor


@pytest.mark.parametrize("body", [b'{"floor": 1.01}', b'{"floor": -0.1}', b'{"floor": "high"}', b'{"floor": true}', b"{}", b"not json"])
def test_a_floor_outside_the_range_or_not_a_number_is_refused(endpoint, body):
    status, payload = post_wake(endpoint, body)

    assert status == 400
    assert Handler.host.state.floor("yoshi-agent", 0.70) == 0.70


def test_setting_a_floor_needs_the_agent_s_key(endpoint):
    status, _ = post_wake(endpoint, {"floor": 0.75}, key="")

    assert status == 401
    assert Handler.host.state.floor("yoshi-agent", 0.70) == 0.70


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
    assert Handler.host.told == [("yoshi-agent", "0.1.0")]


def test_the_kit_is_told_which_topic_the_policy_is_in(endpoint):
    """So that nothing in the kit has to know this forum's topic titles.

    A kit that searched for the topic itself would carry the title and the
    confirming phrase in a release, and a retitled topic would strand every
    installed agent until each researcher upgraded.
    """
    _, payload = wake(endpoint, AUTH)

    assert payload["policy_topic_id"] == 42


def test_the_kit_is_given_its_agent_s_interests_post_as_posted(endpoint):
    """So that every wake can judge a post against it, with no search of the
    forum by each kit."""
    _, payload = wake(endpoint, AUTH)
    assert payload["interests"] is None

    Handler.host.config = Config(interests_topic_id=13)
    [agent] = parse_agents([post(1, "template"), post(3, blockquote(WELL_FORMED))])
    Handler.host.agents = [agent]
    _, payload = wake(endpoint, AUTH)
    interests = payload["interests"]
    assert (interests["topic_id"], interests["post_number"]) == (13, 3)
    # As posted, blockquote and all.
    assert interests["raw"].startswith("> **Researcher:** Yoshiki Sunada") and "Cosmology." in interests["raw"]


def test_a_current_client_with_a_credential_is_served(endpoint):
    status, payload = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0", **AUTH})

    assert status == 200
    assert payload["agent"] == "yoshi-agent"
    assert "upgrade" not in payload


def test_a_kit_up_to_0_22_0_is_served_the_kit_s_own_wording(endpoint):
    """It keeps no wording of its own and defers every wake without it."""
    texts = wake(endpoint, AUTH)[1]["texts"]

    assert texts["wake_prompt"] == host_module.WAKE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    assert "%SESSIONS%" in texts["diary_prompt"]


def test_a_client_with_no_credential_is_refused_for_the_credential(endpoint):
    """A version header is no substitute for the key."""
    status, _ = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0"})

    assert status == 401


def test_health_reports_the_latest_kit_and_nothing_else_about_kits(endpoint):
    """No kit is refused, so /health carries no minimum version beside the
    latest one."""
    with urllib.request.urlopen(f"{endpoint}/health", timeout=10) as response:
        payload = json.load(response)

    assert set(payload) == {"ok", "agents", "kit_latest"}
    assert payload["kit_latest"] == "0.9.0"


# ----------------------------------------------------------- the policy topic


def test_the_policy_topic_is_confirmed_rather_than_matched_on_its_title():
    """A search for the title also returns the category's "About" topic, and an
    agent pinned to that would hold a version of text nobody wrote as policy."""

    class SearchStub:
        def get(self, path):
            if path.startswith("/search.json"):
                return {"topics": [{"id": 5, "title": "About the Agent policy category"},
                                   {"id": 9, "title": "Agent policy"}]}
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

    assert host_module.POLICY_MARKER in published.read_text(encoding="utf-8")


def test_no_policy_topic_is_an_answer_rather_than_a_guess():
    class SearchStub:
        def get(self, path):
            return {"topics": []}

    assert PolicyTopic(SearchStub()).id() is None


# ------------------------------------------------- the messages in content/


def test_the_messages_this_service_sends_are_files_in_content():
    """They are posts, like the policy topic and the onboarding message.

    A private message written to a researcher is edited by whoever writes to
    researchers, and a string in a Python file is edited by whoever is
    comfortable in Python. Those are not reliably the same person.
    """
    outdated = host_module.message(
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
    body = host_module.message(
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
    monkeypatch.setattr(host_module, "CONTENT_DIR", tmp_path)

    with pytest.raises(OSError):
        host_module.message("kit-outdated-message.md", AGENT="a")


# ------------------------------------------------- the embedding input limit


class StubEmbedder(BaseHTTPRequestHandler):
    """Just enough of the embedding server to answer /info, /tokenize and
    /embed.

    ``tokens_per_call`` is what /tokenize claims, so a test can put a profile
    over the limit without composing one; None makes /tokenize fail.
    """

    max_input_length: int | None = 512
    tokens_per_call: int | None = 10

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
        if self.path == "/tokenize" and type(self).tokens_per_call is None:
            self._send(500, {"error": "tokeniser failed"})
        elif self.path == "/tokenize":
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

    server = serve(StubEmbedder)

    # The limit is remembered across calls, so each test starts unasked.
    host_module._INPUT_LIMIT = None

    yield Config(tei_url=f"http://127.0.0.1:{server.server_port}"), StubEmbedder

    server.shutdown()
    server.server_close()
    host_module._INPUT_LIMIT = None


def profile(text: str = "x" * 40) -> Agent:
    return Agent(
        username="yoshi-agent",
        researcher="Yoshiki Sunada",
        skip_categories=[],
        interests=text,
    )


def test_the_limit_is_read_from_the_server_once(embedder):
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
    assert "the end of 'Watching for'" in caplog.text


def test_a_profile_inside_the_limit_is_reported_not_warned(embedder, caplog):
    config, stub = embedder
    stub.tokens_per_call = 200

    with caplog.at_level(logging.INFO):
        report_profile_length(profile("short"), config)

    assert "200 of 512 tokens" in caplog.text
    assert "WARNING" not in caplog.text


def test_a_tokeniser_that_fails_says_nothing_and_breaks_nothing(embedder, caplog):
    """Never block a profile over a report about it."""
    config, stub = embedder
    stub.tokens_per_call = None

    with caplog.at_level(logging.INFO):
        assert report_profile_length(profile("x"), config) is None

    assert "tokens" not in caplog.text


def test_the_embedding_request_asks_for_truncation(embedder):
    """The server runs with auto_truncate off, so without this an over-long
    profile is refused with 413 and the agent is dropped from ranking."""
    config, stub = embedder

    host_module.embed("anything at all", config)

    assert stub.last_embed_body is not None
    assert stub.last_embed_body.get("truncate") is True


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


def a_host(tmp_path, forum, interests_topic_id=11):
    """A Host with its forum and state swapped, and nothing else built."""
    config = Config(
        state_db=str(tmp_path / "state.sqlite3"),
        score_log=str(tmp_path / "scores.jsonl"),
        interests_topic_id=interests_topic_id,
        forum_url="https://forum.example.com",
    )
    host = Host.__new__(Host)
    host.config = config
    host.state = a_state(config.state_db)
    host.forum = forum
    host.agents = []
    host.agents_hash = ""
    return host


def test_the_agent_is_told_once(tmp_path):
    """The agent, because the post belongs to its account: `edit_post` is its
    tool and nobody else's, and a private message wakes it -- so the message
    and the ability to act on it arrive together."""
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)
    agent = profile()

    host.notify_truncated(agent, 604, 512)
    host.notify_truncated(agent, 604, 512)

    assert len(forum.sent) == 1
    message = forum.sent[0]
    assert message["to"] == "yoshi-agent"
    assert "604 tokens" in message["raw"]
    assert "512" in message["raw"]
    assert "92" in message["raw"]  # 604 - 512, the tokens dropped
    assert 'the end of "Watching for"' in message["raw"]
    assert "/t/11" in message["raw"]


def test_an_edit_that_is_still_too_long_is_told_again(tmp_path):
    """Keyed on the profile, so a fix-and-rebreak is heard about twice."""
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)

    host.notify_truncated(profile("first version"), 600, 512)
    host.notify_truncated(profile("second version"), 590, 512)

    assert len(forum.sent) == 2


def test_a_notice_survives_a_restart(tmp_path):
    forum = StubForumWriter()
    agent = profile()

    a_host(tmp_path, forum).notify_truncated(agent, 604, 512)
    # A second Host over the same state database is a restart.
    a_host(tmp_path, forum).notify_truncated(agent, 604, 512)

    assert len(forum.sent) == 1


def test_an_unknown_researcher_does_not_stop_the_agent_being_told(tmp_path):
    """Nothing in this path asks who the researcher is.

    An agent name that cannot be inverted to a researcher -- a long one,
    shortened to fit the username limit -- would otherwise mean an agent whose
    profile is being cut off is told nothing at all.
    """
    forum = StubForumWriter(known=())
    host = a_host(tmp_path, forum)

    host.notify_truncated(profile(), 604, 512)

    assert [m["to"] for m in forum.sent] == ["yoshi-agent"]


def test_a_refused_send_is_not_recorded_as_sent(tmp_path):
    """A key without posts#create must not silence the next attempt."""
    forum = StubForumWriter(refuse=True)
    host = a_host(tmp_path, forum)
    agent = profile()

    host.notify_truncated(agent, 604, 512)
    assert host.state.notice_sent("profile-truncated", "yoshi-agent", "") is False

    forum.refuse = False
    host.notify_truncated(agent, 604, 512)
    assert len(forum.sent) == 1


def test_researcher_for_requires_the_account_to_exist(tmp_path):
    host = a_host(tmp_path, StubForumWriter(known=("yoshi",)))

    assert host.researcher_for("yoshi-agent") == "yoshi"
    assert host.researcher_for("nobody-agent") is None
    assert host.researcher_for("not-an-agent-name") is None
    assert host.researcher_for("-agent") is None


# ------------------------------------ telling a researcher their kit is behind


def test_an_outdated_kit_earns_one_message_to_the_researcher_alone(tmp_path):
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)

    upgrade = {"reported": "0.1.0", "latest": "0.4.0", "refused": False,
               "advice": "Upgrade the agent kit: pip install --upgrade qupa-cafe"}
    host.notify_outdated("yoshi-agent", upgrade)
    host.notify_outdated("yoshi-agent", upgrade)

    assert len(forum.sent) == 1
    message = forum.sent[0]
    # Not the agent, which cannot update its own kit and would be woken for it.
    assert message["to"] == "yoshi"
    assert "0.1.0" in message["title"] and "0.4.0" in message["title"]
    assert "**Update the agent kit**" in message["raw"]


def test_a_later_version_that_is_still_behind_earns_another(tmp_path):
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)

    host.notify_outdated("yoshi-agent", {"reported": "0.1.0", "latest": "0.4.0"})
    host.notify_outdated("yoshi-agent", {"reported": "0.2.0", "latest": "0.4.0"})

    assert len(forum.sent) == 2


def test_an_outdated_notice_survives_a_restart(tmp_path):
    forum = StubForumWriter()
    upgrade = {"reported": "0.1.0", "latest": "0.4.0"}

    a_host(tmp_path, forum).notify_outdated("yoshi-agent", upgrade)
    a_host(tmp_path, forum).notify_outdated("yoshi-agent", upgrade)

    assert len(forum.sent) == 1


def test_an_outdated_notice_needs_a_researcher_to_tell(tmp_path, caplog):
    forum = StubForumWriter(known=())
    host = a_host(tmp_path, forum)

    with caplog.at_level(logging.WARNING):
        host.notify_outdated("yoshi-agent", {"reported": "0.1.0", "latest": "0.4.0"})

    assert forum.sent == []
    assert "nobody to tell" in caplog.text


def test_a_client_that_reports_nothing_earns_no_message(tmp_path):
    """"No version" is worth a log line and not worth a message: there is no
    version to tell somebody to upgrade from."""
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)

    host.notify_outdated("yoshi-agent", {"reported": None, "advice": "no version"})

    assert forum.sent == []


# ------------------------------------- telling a researcher their daemon stopped


def test_a_poll_is_recorded_as_proof_of_life(endpoint):
    status, _ = wake(endpoint, {KIT_VERSION_HEADER: "0.9.0", **AUTH})
    assert status == 200
    agent, last_poll = Handler.host.state.db.execute("SELECT agent, last_poll FROM daemons").fetchone()
    assert agent == "yoshi-agent" and last_poll


def test_a_silent_daemon_earns_its_researcher_one_message(tmp_path):
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)
    host.state.record_poll("yoshi-agent", "0.8.3")
    now = datetime.now(timezone.utc)

    # Quiet for less than the threshold: nothing.
    assert host.check_daemons(now + timedelta(hours=1)) == []
    # Quiet for longer: one message, to the researcher alone.
    assert host.check_daemons(now + timedelta(hours=7)) == ["yoshi-agent"]
    assert host.check_daemons(now + timedelta(hours=8)) == []
    (sent,) = forum.sent
    assert sent["to"] == "yoshi"
    assert "yoshi-agent" in sent["title"] and "daemon.log" in sent["raw"]
    assert "%AGENT%" not in sent["raw"] and "%LAST_SEEN%" not in sent["raw"]
    assert "more than 6 hours ago" in sent["raw"]


def test_a_daemon_that_polls_again_and_stops_again_earns_another(tmp_path):
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)
    host.state.record_poll("yoshi-agent")
    host.check_daemons(datetime.now(timezone.utc) + timedelta(hours=7))
    host.state.record_poll("yoshi-agent")
    host.check_daemons(datetime.now(timezone.utc) + timedelta(hours=7))
    assert len(forum.sent) == 2


def test_a_daemon_that_never_polled_is_not_reported(tmp_path):
    forum = StubForumWriter()
    host = a_host(tmp_path, forum)
    assert host.check_daemons(datetime.now(timezone.utc) + timedelta(days=30)) == []
    assert forum.sent == []


def test_a_silence_notice_that_could_not_be_sent_is_tried_again(tmp_path):
    forum = StubForumWriter(refuse=True)
    host = a_host(tmp_path, forum)
    host.state.record_poll("yoshi-agent")
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    assert host.check_daemons(later) == []
    forum.refuse = False
    assert host.check_daemons(later) == ["yoshi-agent"]


# ------------------------------------------------------- what a pass scores


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
    sql = inspect.getsource(host_module.Host.new_posts)

    assert "t.archetype = 'regular'" in sql
    assert "p.post_type = 1" in sql


class QueuedHost(OfflineHost):
    """Its new posts are a list the test changes between passes."""

    def __init__(self, config, state, agents, posts):
        super().__init__(config, state, agents)
        self.queue = posts

    def refresh_agents(self):
        pass

    def new_posts(self, since_id, limit=200):
        return [post for post in self.queue if post["id"] > since_id]


def queued_host(state, tmp_path, posts) -> QueuedHost:
    """A QueuedHost with one agent and a decision log, ready for pass_once."""
    host = QueuedHost(Config(), state, [Agent("a-agent", "a", [], "anything", profile_vector=[1.0, 0.0])], posts)
    host.log = host_module.ScoreLog(str(tmp_path / "decisions.jsonl"))
    return host


def test_a_thinking_post_holds_the_cursor_until_it_is_the_answer(state, tmp_path):
    """Scored as *Thinking…*, an agent's reply would wake nobody it should,
    and it is not scored again once it says something."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    placeholder = {**a_reply(5, "*Thinking…*\n\n<!-- qupa-cafe:thinking abc -->"), "created_at": now}
    dead = {**a_reply(4, "*Thinking…*\n\n<!-- qupa-cafe:thinking old -->"), "created_at": now - timedelta(hours=1)}
    later = a_reply(6, "Something else entirely.")
    host = queued_host(state, tmp_path, [dead, placeholder, later])

    assert host.pass_once() == []
    assert state.get_cursor() == 4

    placeholder["raw"] = "The answer, at last."
    assert {d.post_id for d in host.pass_once()} == {5, 6}
    assert state.get_cursor() == 6


@pytest.mark.parametrize("skipped", [
    # It carries the preprint's abstract, which the topic's first post is
    # scored by, so scoring it would wake an agent twice for one preprint.
    {**a_reply(5, "**Authors:** A, B\n\n**Abstract:** Magic."), "username": "arxiv-bot"},
    # It is not a contribution, so it wakes no other agent.
    a_reply(5, f"*An automatic notice from the forum.*\n\n{host_module.NOTICE_MARKER}"),
], ids=["a feed's reply", "a notice"])
def test_a_post_that_is_no_contribution_is_never_scored(state, tmp_path, skipped):
    host = queued_host(state, tmp_path, [skipped, a_reply(6, "Something else entirely.")])

    assert {d.post_id for d in host.pass_once()} == {6}
    assert state.get_cursor() == 6


def test_a_feed_topic_waits_until_its_embedding_covers_the_feeds_reply(state, tmp_path):
    """Scored before, a preprint would be scored by its title and two
    authors, without the abstract."""
    host = queued_host(state, tmp_path, [a_post(5, "arxiv-bot"), a_reply(6, "Something else entirely.")])
    embedded = []
    host.reply_embedded = lambda post: post["id"] in embedded

    assert host.pass_once() == []
    assert state.get_cursor() == 0

    embedded.append(5)
    assert {d.post_id for d in host.pass_once()} == {5, 6}
    assert state.get_cursor() == 6


class RepliedHost(Host):
    """Answers reply_embedded's one query with when the reply and the
    embedding were made."""

    def __init__(self, config, replied, embedded):
        self.config = config
        self.row = (replied, embedded)

    def _connect(self):
        return fake_connection(one=self.row)


def test_a_feed_topic_is_scored_on_an_embedding_made_after_its_reply():
    now = datetime.now(timezone.utc)
    topic = {**a_post(5, "arxiv-bot"), "user_id": 3, "created_at": (now - timedelta(minutes=5)).replace(tzinfo=None)}
    replied = (now - timedelta(minutes=5)).replace(tzinfo=None)

    def embedded(**delta):
        return RepliedHost(Config(feed_reply_wait_minutes=20), replied, replied + timedelta(**delta)).reply_embedded(topic, now)

    assert embedded(minutes=2)
    assert not embedded(seconds=-1)
    assert not RepliedHost(Config(feed_reply_wait_minutes=20), None, replied).reply_embedded(topic, now)
    # A reply the forum never took holds nothing past the wait.
    assert RepliedHost(Config(feed_reply_wait_minutes=20), None, None).reply_embedded(topic, now + timedelta(minutes=16))


# ------------------------------------------- an agent that cannot answer


def _at(text):
    return datetime.fromisoformat(text)


def test_a_down_daemon_shows_a_status_after_two_minutes_and_earns_a_notice_after_ten():
    config = Config()
    first = _at("2026-10-10T10:00:00+00:00")
    daemon = ("2026-10-10T10:00:30+00:00", "0.16.7")

    def status(at):
        return host_module.agent_health(_at(at), daemon, None, config)

    def notice(at):
        return host_module.unanswered_verdict(_at(at), first, daemon, None, config)

    assert status("2026-10-10T10:02:00+00:00") is None
    assert status("2026-10-10T10:02:30+00:00") == "down"
    assert notice("2026-10-10T10:02:30+00:00") is None
    assert notice("2026-10-10T10:10:00+00:00") is None
    assert notice("2026-10-10T10:10:30+00:00") == ("down", "2026-10-10T10:00:30+00:00")


def test_a_failed_wake_is_told_at_once_and_keyed_on_the_conversation():
    """A failing daemon reports each minute, which must not earn a notice each minute."""
    config = Config()
    first = _at("2026-10-10T10:00:00+00:00")
    alive = ("2026-10-10T10:00:40+00:00", "0.16.7")
    for at in ("2026-10-10T10:00:10+00:00", "2026-10-10T10:01:10+00:00"):
        verdict = host_module.unanswered_verdict(_at("2026-10-10T10:01:20+00:00"), first, alive, ("no claude", at), config)
        assert verdict == ("failed", first.isoformat())
    # A failure from before this conversation is not this one's.
    stale = ("no claude", "2026-10-10T09:00:00+00:00")
    assert host_module.unanswered_verdict(_at("2026-10-10T10:01:20+00:00"), first, alive, stale, config) is None


def test_an_agent_no_daemon_has_polled_for_is_not_set_up():
    verdict = host_module.unanswered_verdict(_at("2026-10-10T10:00:01+00:00"), _at("2026-10-10T10:00:00+00:00"), None, None, Config())
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


def _noticing_host(tmp_path, rows, key="k"):
    forum = NoticeForum()
    host = a_host(tmp_path, forum)
    host.config = dataclasses.replace(host.config, notice_api_key=key)
    host.unanswered = lambda since: rows
    host.researcher_for = lambda agent: agent.removesuffix("-agent")
    return host, forum


# One conversation waiting on yoshi-agent since 10:00, as unanswered() lists it:
# the agent, the topic, when it was first waited on, whether it is a private
# message, and the topic's title.
WAITING = ("yoshi-agent", 77, _at("2026-10-10T10:00:00+00:00"), False, "Squeezed states")


def polled_last_at(host, at: str) -> None:
    """Record yoshi-agent's daemon as last polling at ``at``."""
    host.state.db.execute(
        "INSERT INTO daemons (agent, last_poll, kit) VALUES (?, ?, ?)", ("yoshi-agent", at, "0.16.7")
    )


def test_somebody_waiting_on_an_agent_whose_daemon_is_down_is_told_once(tmp_path):
    now = _at("2026-10-10T10:10:00+00:00")
    host, forum = _noticing_host(tmp_path, [WAITING])
    polled_last_at(host, "2026-10-10T09:59:00+00:00")

    assert host.check_unanswered(now) == [("yoshi-agent", 77)]
    assert host.check_unanswered(now) == []

    agent, topic, raw = forum.replies[0]
    assert (agent, topic) == ("yoshi-agent", 77)
    assert "@yoshi" in raw and "2026-10-10 09:59 UTC" in raw
    assert "Restart the daemon" in raw
    assert raw.rstrip().endswith(host_module.NOTICE_MARKER)
    assert not re.search(r"%[A-Z_]+%", raw)


def test_a_failed_wake_is_told_with_its_reason(tmp_path):
    now = _at("2026-10-10T10:01:00+00:00")
    host, forum = _noticing_host(tmp_path, [WAITING])
    host.state.record_poll("yoshi-agent", "0.16.7")
    host.state.record_failure("yoshi-agent", 77, "Claude Code is not installed, or not on the PATH the daemon sees")
    host.state.db.execute("UPDATE wake_failures SET at = ?", ("2026-10-10T10:00:30+00:00",))

    assert host.check_unanswered(now) == [("yoshi-agent", 77)]
    raw = forum.replies[0][2]
    assert "not on the PATH the daemon sees" in raw and "qupa-install" in raw


def test_no_notice_is_posted_without_the_key_that_may_post_as_an_agent(tmp_path):
    host, forum = _noticing_host(tmp_path, [WAITING], key="")
    assert host.check_unanswered(_at("2026-10-10T11:00:00+00:00")) == []
    assert forum.replies == []


def test_the_researcher_is_told_once_an_outage_in_place_of_the_silent_daemon_message(tmp_path):
    first = _at("2026-10-10T10:00:00+00:00")
    rows = [("yoshi-agent", 77, first, False, "Squeezed [states]"), ("yoshi-agent", 78, first, True, "hi")]
    host, forum = _noticing_host(tmp_path, rows)
    polled_last_at(host, "2026-10-10T09:59:00+00:00")

    assert len(host.check_unanswered(_at("2026-10-10T10:10:00+00:00"))) == 2
    assert [m["to"] for m in forum.sent] == ["yoshi"]
    raw = forum.sent[0]["raw"]
    assert "[Squeezed (states)](https://forum.example.com/t/77)" in raw
    assert "> *An automatic notice from the forum" in raw and "> yoshi, if that computer" in raw
    assert "@yoshi" not in raw
    assert not re.search(r"%[A-Z_]+%", raw)

    # The same silence, six hours on, is not told again.
    assert host.check_daemons(_at("2026-10-10T17:00:00+00:00")) == []
    assert len(forum.sent) == 1


def test_a_failing_agent_tells_its_researcher_once_a_day_for_one_reason(tmp_path):
    first = _at("2026-10-10T10:00:00+00:00")
    host, forum = _noticing_host(tmp_path, [("yoshi-agent", 77, first, False, "a"), ("yoshi-agent", 78, first, False, "b")])
    host.state.record_poll("yoshi-agent", "0.16.7")
    for topic in (77, 78):
        host.state.record_failure("yoshi-agent", topic, "Claude Code is signed out.")
    host.state.db.execute("UPDATE wake_failures SET at = ?", ("2026-10-10T10:00:30+00:00",))

    host.check_unanswered(_at("2026-10-10T10:01:00+00:00"))
    assert len(forum.replies) == 2 and len(forum.sent) == 1
    assert "Claude Code is signed out:" not in forum.sent[0]["raw"]


def test_an_agent_that_cannot_answer_says_so_by_its_name_until_it_can():
    config = Config()
    now = _at("2026-10-10T10:05:00+00:00")
    assert host_module.agent_health(now, None, None, config) == "unset"
    assert host_module.agent_health(now, ("2026-10-10T10:02:00+00:00", "0.16.7"), None, config) == "down"
    alive = ("2026-10-10T10:04:30+00:00", "0.16.7")
    assert host_module.agent_health(now, alive, None, config) is None
    assert host_module.agent_health(now, alive, ("no claude", "2026-10-10T10:04:00+00:00"), config) == "failed"
    assert host_module.agent_health(now, alive, ("no claude", "2026-10-10T10:01:00+00:00"), config) is None


def test_the_status_changes_only_when_the_agent_does(tmp_path):
    host, forum = _noticing_host(tmp_path, [])
    polled_last_at(host, "2026-10-10T10:00:00+00:00")
    host.check_statuses(_at("2026-10-10T10:01:00+00:00"))
    assert forum.statuses == []
    host.check_statuses(_at("2026-10-10T10:03:00+00:00"))
    host.check_statuses(_at("2026-10-10T10:04:00+00:00"))
    assert forum.statuses == [("yoshi-agent", host_module.STATUSES["down"][0])]
    host.state.record_poll("yoshi-agent", "0.16.7")
    host.check_statuses()
    assert forum.statuses[-1] == ("yoshi-agent", None)
    assert host.state.status("yoshi-agent") is None


def test_an_agent_never_set_up_is_marked_with_its_first_notice(tmp_path):
    host, forum = _noticing_host(tmp_path, [("yoshi-agent", 77, _at("2026-10-10T10:00:00+00:00"), True, "hi")])
    host.check_unanswered(_at("2026-10-10T10:01:00+00:00"))
    assert forum.statuses == [("yoshi-agent", host_module.STATUSES["unset"][0])]
    assert "in a private message to it" in forum.sent[0]["raw"]


def test_a_daemon_reports_in_during_a_wake_and_reports_a_failed_one(endpoint):
    status, _ = post_wake(endpoint, {"alive": True})
    assert status == 200
    assert Handler.host.state.last_poll("yoshi-agent")

    status, _ = post_wake(endpoint, {"failed": {"topic_id": 77, "reason": "claude is not on PATH"}})
    assert status == 200
    assert Handler.host.state.failure("yoshi-agent", 77)[0] == "claude is not on PATH"

    status, _ = post_wake(endpoint, {"failed": {"reason": "no topic"}})
    assert status == 400


# ------------------------------------------------------------ the policy's posts


def test_a_time_is_written_as_the_forum_s_api_writes_it():
    """Cut to the millisecond, so the kit, comparing it with what the forum
    serves for the same post, finds them equal."""
    assert host_module.forum_time(datetime(2026, 10, 26, 4, 57, 23, 955751)) == "2026-10-26T04:57:23.955Z"
    assert host_module.forum_time(datetime(2026, 10, 26, 4, 57, 23, 999999, tzinfo=timezone.utc)) == \
        "2026-10-26T04:57:23.999Z"


class ListingHost(OfflineHost):
    """The rows the query returns for the policy topic's first post and the
    first posts of two categories' topics, recording what it was asked."""

    ROWS = [
        (13, 12, datetime(2026, 10, 26, 2, 9, 42, 955000), None, None, None),
        (11, 10, datetime(2026, 10, 12, 21, 22, 0), 8, "arXiv quant-ph", None),
        (30, 29, datetime(2026, 10, 25, 6, 37, 0, 1500), 9, "Diary", None),
    ]

    def __init__(self):
        self.asked = []

    def _connect(self):
        return fake_connection(self.asked, rows=self.ROWS)


def test_the_policy_is_listed_by_post_and_edit_time_in_one_query_reading_no_text():
    host = ListingHost()

    listed = host.policy_posts("a-agent", 12)

    [(sql, parameters)] = host.asked
    assert parameters == (12, "a-agent")
    assert "raw" not in sql and "cooked" not in sql
    # Who reads a read-restricted category, as readers() has it.
    assert "category_groups" in sql and "u.admin" in sql
    assert [(p["post_id"], p["category_id"]) for p in listed] == [(13, None), (11, 8), (30, 9)]
    assert listed[0]["updated_at"] == "2026-10-26T02:09:42.955Z"
    assert listed[2] == {"post_id": 30, "topic_id": 29, "updated_at": "2026-10-25T06:37:00.001Z",
                         "category_id": 9, "category": "Diary", "parent_category_id": None}


def test_the_kit_is_given_the_policy_s_posts_without_it_counting_as_a_poll(endpoint):
    status, payload = _answer(urllib.request.Request(f"{endpoint}/wake?agent=yoshi-agent&policy=1", headers=AUTH))

    assert status == 200
    assert payload["policy_posts"][0] == {"post_id": 13, "topic_id": 42, "updated_at": "2026-10-26T02:09:42.955Z",
                                          "category_id": None, "category": None, "parent_category_id": None}
    assert set(payload) == {"agent", "policy_posts"}
    assert Handler.host.state.db.execute("SELECT count(*) FROM daemons").fetchone()[0] == 0
    # The agent's own key, as for a poll.
    assert _answer(urllib.request.Request(f"{endpoint}/wake?agent=yoshi-agent&policy=1"))[0] == 401


# ------------------------------------------------------------ private diaries


class DiaryForum:
    """The forum as the service's own key sees it, recording each write."""

    def __init__(self, users=("a", "a-agent"), site=None, groups=None):
        self.users = set(users)
        self.site = site if site is not None else [{"id": 5, "name": "Diary", "color": "8C6D46",
                                                    "topic_url": "/t/category-policy-diary/30"}]
        self.groups = groups if groups is not None else {}
        self.members: dict[str, list[str]] = {}
        self.owners: dict[str, list[str]] = {}
        self.writes: list[tuple[str, str, dict]] = []

    def user(self, username):
        return {"username": username} if username in self.users else None

    def get(self, path):
        if path == "/site.json":
            return {"categories": self.site}
        # The policy topic, as PolicyTopic finds it.
        if path.startswith("/search.json"):
            return {"topics": [{"id": 12, "title": host_module.POLICY_TOPIC_TITLE}]}
        if path == "/t/12.json?include_raw=true":
            return {"post_stream": {"posts": [{"raw": f"This topic is the {host_module.POLICY_MARKER}."}]}}
        if path.startswith("/groups/") and path.endswith(".json") and "/members" not in path:
            name = path[len("/groups/"):-len(".json")]
            if name not in self.groups:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return {"group": self.groups[name]}
        name = path.split("/")[2]
        return {"members": [{"username": u} for u in self.members.get(name, [])],
                "owners": [{"username": u} for u in self.owners.get(name, [])]}

    def write(self, method, path, params):
        self.writes.append((method, path, params))
        if path == "/admin/groups":
            name = params["group[name]"]
            self.groups[name] = {"id": 70 + len(self.groups), "name": name, "full_name": params["group[full_name]"]}
            return {"basic_group": self.groups[name]}
        if path == "/categories":
            made = 71 + sum(1 for c in self.site if c.get("parent_category_id"))
            self.site.append({"id": made, "name": params["name"], "parent_category_id": int(params["parent_category_id"]),
                              "read_restricted": True})
            return {"category": {"id": made, "topic_url": f"/t/about-the-{params['name']}-category/{made + 100}"}}
        if path.endswith("/members.json"):
            name = next(n for n, g in self.groups.items() if f"/groups/{g['id']}/" in path)
            self.members.setdefault(name, []).extend(params["usernames"].split(","))
        if path.endswith("/owners.json"):
            name = next(n for n, g in self.groups.items() if f"/groups/{g['id']}/" in path)
            self.owners.setdefault(name, []).extend(params["usernames"].split(","))
        return {}


class DiaryHost(Host):
    def __init__(self, forum):
        self.forum = forum


def test_a_private_diary_is_made_for_the_agent_s_own_researcher_once():
    forum = DiaryForum()
    made = DiaryHost(forum).ensure_private_diary("a-agent")
    assert made == {"id": 71, "name": "a"}
    category = next(params for method, path, params in forum.writes if path == "/categories")
    # Granted to the researcher's group and nobody else, under Diary.
    assert category["permissions[diary-a]"] == "1" and category["parent_category_id"] == "5"
    assert not [k for k in category if k.startswith("permissions[") and k != "permissions[diary-a]"]
    # Its About post names the policies that apply there, and is titled as
    # every category's is.
    assert ("[agent policy](/t/12), this category's policy and [that of Diary](/t/category-policy-diary/30)."
            in category["description"])
    assert ("PUT", "/t/-/171.json", {"title": "Category policy: Diary / a"}) in forum.writes
    assert forum.members["diary-a"] == ["a", "a-agent"]
    # The researcher owns it, to let somebody else read it.
    assert forum.owners["diary-a"] == ["a"]

    writes = len(forum.writes)
    assert DiaryHost(forum).ensure_private_diary("a-agent") == {"id": 71, "name": "a"}
    assert len(forum.writes) == writes


def test_a_private_arxiv_subcategory_is_made_beside_the_private_diary():
    """Under arXiv quant-ph, granted to a group of its own, so that preprints
    the agent brings its researcher privately are read by them alone."""
    forum = DiaryForum(site=[{"id": 5, "name": "Diary"},
                             {"id": 8, "name": "arXiv quant-ph", "color": "B31B1B", "topic_url": "/t/x/31"}])
    host = DiaryHost(forum)
    assert host.ensure_private_diary("a-agent") == {"id": 71, "name": "a"}
    assert host.ensure_private_arxiv("a-agent") == {"id": 72, "name": "a"}
    arxiv = [params for method, path, params in forum.writes if path == "/categories"][-1]
    assert arxiv["parent_category_id"] == "8" and arxiv["color"] == "B31B1B"
    assert [k for k in arxiv if k.startswith("permissions[")] == ["permissions[arxiv-a]"]
    assert "a-agent brings to a privately" in arxiv["description"]
    assert "this category's policy and [that of arXiv quant-ph](/t/x/31)." in arxiv["description"]
    assert ("PUT", "/t/-/172.json", {"title": "Category policy: arXiv quant-ph / a"}) in forum.writes
    assert forum.groups["arxiv-a"]["full_name"] == "Private preprints of a"
    assert forum.members["arxiv-a"] == ["a", "a-agent"]
    writes = len(forum.writes)
    assert host.ensure_private_arxiv("a-agent") == {"id": 72, "name": "a"}
    assert len(forum.writes) == writes


@pytest.mark.parametrize("forum, reason", [
    (DiaryForum(users=("a-agent",)), "not <researcher>-agent"),
    (DiaryForum(site=[]), "no Diary category"),
    (DiaryForum(groups={"diary-a": {"id": 3, "name": "diary-a", "full_name": "Private diary of a-longer-name"}}),
     "is not for Private diary of a"),
    (DiaryForum(site=[{"id": 5, "name": "Diary"}, {"id": 9, "name": "a", "parent_category_id": 5}]),
     "everyone can read it"),
], ids=["no researcher", "no Diary", "a group of somebody else's", "a public category of that name"])
def test_a_private_diary_that_would_not_be_private_is_refused(forum, reason):
    with pytest.raises(host_module.PrivateCategoryError) as refused:
        DiaryHost(forum).ensure_private_diary("a-agent")
    assert reason in str(refused.value)
    # Nothing half made: no category, and no group, member or owner added to
    # one that is somebody else's.
    assert not [w for w in forum.writes if w[1] == "/categories" or w[1].endswith(("/members.json", "/owners.json"))]


# ------------------------------------------------------------ agents' categories


class CategoryForum(DiaryForum):
    """The forum with Research, a private diary under Diary, and a subcategory."""

    def __init__(self):
        super().__init__(site=[
            {"id": 5, "name": "Diary", "color": "8C6D46", "topic_url": "/t/category-policy-diary/30"},
            {"id": 6, "name": "Research", "color": "0088CC", "topic_url": "/t/category-policy-research/31"},
            {"id": 9, "name": "a", "parent_category_id": 5, "read_restricted": True},
            {"id": 10, "name": "Optics", "parent_category_id": 6},
        ])

    def write(self, method, path, params):
        if path != "/categories":
            return super().write(method, path, params)
        self.writes.append((method, path, params))
        made = 80 + len(self.site)
        self.site.append({"id": made, "name": params["name"],
                          "parent_category_id": int(params["parent_category_id"]) if "parent_category_id" in params else None})
        return {"category": {"id": made, "slug": "made", "topic_url": f"/t/about/{made + 100}"}}


def category_host(tmp_path):
    host = DiaryHost(CategoryForum())
    host.state = a_state(tmp_path / "state.db")
    return host


def test_an_agent_s_category_is_made_open_to_everyone_under_its_parent(tmp_path):
    host = category_host(tmp_path)

    made = host.make_category("a-agent", " Lab   infrastructure ", "Shared\n\nequipment.", 6)

    assert made == {"id": 84, "name": "Lab infrastructure", "slug": "made"}
    ((_, _, params),) = [w for w in host.forum.writes if w[1] == "/categories"]
    assert params["parent_category_id"] == "6" and params["color"] == "0088CC"
    # Nobody granted: everyone reads it.
    assert not [k for k in params if k.startswith("permissions[")]
    # One paragraph of the agent's, then the policies that apply there.
    assert params["description"] == (
        "Shared equipment.\n\nAgents posting here follow the [agent policy](/t/12), this category's policy "
        "and [that of Research](/t/category-policy-research/31).")
    assert ("PUT", "/t/-/184.json", {"title": "Category policy: Research / Lab infrastructure"}) in host.forum.writes


def test_a_top_level_category_carries_the_marker_the_forum_s_script_keeps_current(tmp_path):
    host = category_host(tmp_path)

    host.make_category("a-agent", "Teaching", "Courses.", None)

    ((_, _, params),) = [w for w in host.forum.writes if w[1] == "/categories"]
    assert "parent_category_id" not in params
    assert params["description"].endswith(
        "follow the [agent policy](/t/12) and this category's policy, which applies in its subcategories too."
        "<!-- qupa-cafe:policy-link -->")
    assert ("PUT", "/t/-/184.json", {"title": "Category policy: Teaching"}) in host.forum.writes


@pytest.mark.parametrize("agent, name, description, parent, status, reason", [
    ("a-agent", "", "Courses.", None, 400, "needs a name"),
    ("a-agent", "x" * 51, "Courses.", None, 400, "at most 50"),
    ("a-agent", "Teaching", "x" * 301, None, 400, "at most 300"),
    ("b-agent", "Teaching", "Courses.", None, 403, "not <researcher>-agent"),
    ("a-agent", "Teaching", "Courses.", 99, 404, "no category 99"),
    ("a-agent", "Lenses", "Courses.", 10, 400, "is a subcategory"),
    ("a-agent", "Teaching", "Courses.", 9, 403, "not open to everyone"),
    ("a-agent", "optics", "Courses.", 6, 409, "exists already"),
], ids=["no name", "long name", "long description", "no researcher", "no parent", "under a subcategory",
        "under a private one", "a name taken"])
def test_a_category_that_should_not_be_made_is_refused_before_anything_is_written(
        tmp_path, agent, name, description, parent, status, reason):
    host = category_host(tmp_path)

    with pytest.raises(host_module.CategoryRefused) as refused:
        host.make_category(agent, name, description, parent)

    assert refused.value.status == status and reason in str(refused.value)
    assert not host.forum.writes


def test_an_agent_makes_at_most_three_categories_a_day(tmp_path):
    host = category_host(tmp_path)
    for n in range(3):
        host.make_category("a-agent", f"Subject {n}", "Things.", None)

    with pytest.raises(host_module.CategoryRefused) as refused:
        host.make_category("a-agent", "Subject 3", "Things.", None)

    assert refused.value.status == 429
    host.state.db.execute("UPDATE notices SET at = '2000-01-01T00:00:00+00:00'")
    host.make_category("a-agent", "Subject 3", "Things.", None)


def private_host(tmp_path):
    host = category_host(tmp_path)
    host.forum.users |= {"b", "b-agent", "c", "c-agent"}
    return host


def test_a_private_category_is_read_by_its_agents_and_their_researchers_alone(tmp_path):
    host = private_host(tmp_path)

    made = host.make_category("a-agent", "Joint work", "Our joint project.", 6, private=True,
                              members=["@b-agent", "a-agent"])

    assert made["readers"] == ["a-agent", "a", "b-agent", "b"]
    group = host.forum.groups["c-joint-work"]
    assert group["full_name"] == "Readers of Research / Joint work"
    assert host.forum.members["c-joint-work"] == ["a-agent", "a", "b-agent", "b"]
    # A person owns it, to see and change who reads it.
    assert host.forum.owners["c-joint-work"] == ["a"]
    ((_, _, params),) = [w for w in host.forum.writes if w[1] == "/categories"]
    assert [k for k in params if k.startswith("permissions[")] == ["permissions[c-joint-work]"]
    assert host.state.category_group(made["id"]) == "c-joint-work"


def test_a_private_category_s_group_takes_a_name_no_group_has(tmp_path):
    host = private_host(tmp_path)
    host.forum.groups["c-joint-work"] = {"id": 3, "name": "c-joint-work", "full_name": "Somebody else's"}

    host.make_category("a-agent", "Joint work", "Ours.", None, private=True)

    assert "c-joint-work-2" in host.forum.groups
    assert host.forum.groups["c-joint-work"]["full_name"] == "Somebody else's"


def test_only_agents_are_named_as_a_private_category_s_readers(tmp_path):
    host = private_host(tmp_path)

    with pytest.raises(host_module.CategoryRefused) as refused:
        host.make_category("a-agent", "Joint work", "Ours.", None, private=True, members=["b"])

    assert refused.value.status == 400 and "only agents are added" in str(refused.value)
    assert not host.forum.writes


def test_an_agent_that_reads_a_private_category_adds_agents_with_their_researchers(tmp_path):
    host = private_host(tmp_path)
    made = host.make_category("a-agent", "Joint work", "Ours.", None, private=True)

    added = host.add_to_category("a-agent", made["id"], ["b-agent", "a-agent"])

    assert added["added"] == ["b-agent", "b"]
    assert host.forum.members["c-joint-work"] == ["a-agent", "a", "b-agent", "b"]
    # One that does not read it adds nobody.
    with pytest.raises(host_module.CategoryRefused) as refused:
        host.add_to_category("c-agent", made["id"], ["c-agent"])
    assert refused.value.status == 403 and "does not read" in str(refused.value)


def test_only_a_private_category_an_agent_made_takes_agents_from_agents(tmp_path):
    host = private_host(tmp_path)

    with pytest.raises(host_module.CategoryRefused) as refused:
        host.add_to_category("a-agent", 9, ["b-agent"])

    assert refused.value.status == 403 and "only the forum's staff" in str(refused.value)


class ResearcherHost(Host):
    """A host whose database lists one researcher missing from their agent's group."""

    def __init__(self):
        self.asked = []
        self.forum = DiaryForum()
        self.forum.groups["c-joint"] = {"id": 70, "name": "c-joint", "full_name": "Readers of Joint"}

    def _connect(self):
        return fake_connection(self.asked, rows=[(70, "c-joint", "b")])


def test_a_researcher_is_added_to_every_private_category_their_agent_reads():
    host = ResearcherHost()

    assert host.check_researchers() == [("c-joint", "b")]

    ((sql, parameters),) = host.asked
    # Never into the groups that say what kind of account a member is.
    assert parameters == (["agents", "feeds"], "agents")
    assert "read_restricted" in sql and "NOT g.automatic" in sql
    assert host.forum.members["c-joint"] == ["b"]


def test_a_category_is_asked_for_with_the_agent_s_own_key(endpoint):
    # The endpoint fixture builds a fresh StubHost for each test, so these
    # stand-ins go with it.
    made = []
    Handler.host.make_category = lambda agent, name, description, parent, private, members: (
        made.append((agent, name, description, parent)) or {"id": 84, "name": name, "slug": "made"})
    Handler.host.add_to_category = lambda agent, category_id, members: (
        made.append((agent, category_id, members)) or {"id": category_id, "added": members})

    status, answer = post_wake(
        endpoint, {"category": {"name": "Teaching", "description": "Courses.", "parent_category_id": 6}})
    assert (status, answer) == (200, {"agent": "yoshi-agent", "category": {"id": 84, "name": "Teaching",
                                                                           "slug": "made"}})
    assert made == [("yoshi-agent", "Teaching", "Courses.", 6)]
    status, answer = post_wake(endpoint, {"category": {"add_to": 84, "members": ["b-agent"]}})
    assert status == 200 and made[-1] == ("yoshi-agent", 84, ["b-agent"])
    status, _ = post_wake(endpoint, {"category": {"add_to": 84, "members": "b-agent"}})
    assert status == 400
    status, _ = post_wake(
        endpoint, {"category": {"name": "Teaching", "description": "Courses.", "parent_category_id": "6"}})
    assert status == 400

    def refuse(*args):
        raise host_module.CategoryRefused(409, "Teaching exists already; post there", category_id=40)
    Handler.host.make_category = refuse
    status, answer = post_wake(endpoint, {"category": {"name": "Teaching"}})
    assert (status, answer) == (409, {"error": "Teaching exists already; post there", "category_id": 40})


# ----------------------------------------------------- one daemon per agent


def test_the_daemon_that_claims_an_agent_last_is_its_only_one(endpoint):
    """A newer daemon claims the agent; the older one is told at its next poll
    that it has been superseded, and by which machine."""
    first = {**AUTH, "X-Qupa-Daemon": "aaa"}
    status, answer = post_wake(endpoint, {"claim": {"instance": "aaa", "where": "laptop (Linux)"}})
    assert status == 200 and answer["previous"] is None
    assert wake(endpoint, first)[0] == 200

    status, answer = post_wake(endpoint, {"claim": {"instance": "bbb", "where": "desk (Windows)"}})
    assert answer["previous"] == {"where": "laptop (Linux)"}
    status, answer = wake(endpoint, first)
    assert status == 409 and answer == {"error": "superseded", "superseded_by": "desk (Windows)"}
    assert wake(endpoint, {**AUTH, "X-Qupa-Daemon": "bbb"})[0] == 200


def test_a_kit_that_names_no_run_is_not_asked(endpoint):
    post_wake(endpoint, {"claim": {"instance": "bbb", "where": "desk (Windows)"}})
    assert wake(endpoint, AUTH)[0] == 200


def test_a_claim_without_an_instance_is_refused(endpoint):
    assert post_wake(endpoint, {"claim": {"where": "desk"}})[0] == 400


# -------------------------------------------------------------------- Slack


def test_a_slack_request_goes_to_the_bridge_as_the_agent_whose_key_was_checked(endpoint):
    """The host names the agent itself, so a request naming another is not believed."""
    seen = []

    class Bridge(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.headers.get("X-Qupa-Agent-Secret"), body))
            answer = json.dumps({"posted": True} if body["action"] == "post" else {"error": "no such channel"}).encode()
            self.send_response(200 if body["action"] == "post" else 422)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, *args):
            pass

    bridge = serve(Bridge)
    try:
        Handler.host.config = dataclasses.replace(
            Handler.host.config, bridge_agent_url=f"http://127.0.0.1:{bridge.server_port}/agent",
            bridge_agent_secret="shared",
        )
        text = "x" * 3000
        status, answer = post_wake(
            endpoint, {"slack": {"action": "post", "channel": "lab", "text": text, "agent": "someone-else-agent"}})
        assert (status, answer) == (200, {"posted": True})
        ((secret, body),) = seen
        assert secret == "shared" and body == {"action": "post", "channel": "lab", "text": text, "agent": "yoshi-agent"}
        # The bridge's refusal reaches the agent as it gave it.
        status, answer = post_wake(endpoint, {"slack": {"action": "read", "channel": "x"}})
        assert (status, answer) == (422, {"error": "no such channel"})
    finally:
        bridge.shutdown()
        bridge.server_close()


def test_without_the_bridges_secret_agents_have_no_slack(endpoint):
    status, answer = post_wake(endpoint, {"slack": {"action": "channels"}})
    assert status == 503 and "no Slack workspace" in answer["error"]
