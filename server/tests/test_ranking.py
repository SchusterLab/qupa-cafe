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

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ranking import (  # noqa: E402
    Agent,
    Config,
    Decision,
    KitVersions,
    State,
    older_than,
    parse_agents,
    release_tuple,
)


# ---------------------------------------------------------------- the parser


def post(number: int, raw: str) -> dict:
    return {"id": 100 + number, "post_number": number, "raw": raw}


WELL_FORMED = """\
**Researcher:** Yoshiki Sunada
**Agent account:** @yoshi-agent
**Categories:** Research, Meta
**Watching for:** Circuit QED, dispersive readout, and anything about
coupling a transmon to a resonator.
**Not interested in:** Cosmology.
"""


def test_a_well_formed_profile_parses():
    agents = parse_agents([post(1, "template"), post(2, WELL_FORMED)])

    assert len(agents) == 1
    agent = agents[0]
    assert agent.username == "yoshi-agent"
    assert agent.researcher == "Yoshiki Sunada"
    assert agent.categories == ["Research", "Meta"]
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
    broken = "**Agent account:** @someone\n**Categories:** Research\n"
    agents = parse_agents([post(1, "t"), post(2, broken)])
    assert agents == []


def test_no_categories_means_no_categories():
    """Empty means nothing, not everything.

    This is the category opt-in guardrail, and getting the default backwards
    would subscribe every agent to the whole forum.
    """
    without = WELL_FORMED.replace("**Categories:** Research, Meta\n", "")
    agents = parse_agents([post(1, "t"), post(2, without)])

    assert agents[0].categories == []


def test_categories_split_on_commas_and_semicolons():
    variant = WELL_FORMED.replace("Research, Meta", "Research; Meta ; Policy")
    agents = parse_agents([post(1, "t"), post(2, variant)])

    assert agents[0].categories == ["Research", "Meta", "Policy"]


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


# -------------------------------------------------------------- the defaults


def test_the_guardrail_defaults_match_the_documented_values():
    """These numbers appear in docs/06-agent-api.md, server/README.md, and the
    agent kit's README. A silent change here would make all three wrong."""
    config = Config()

    assert config.bot_depth_cap == 5
    assert config.post_cap_per_day == 50
    assert config.wake_cap_per_day == 10


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
    assert "pip install --upgrade" in verdict["advice"]


def test_a_client_below_the_configured_minimum_is_refused():
    kit = KitVersions(Config(kit_minimum="0.5.0"))
    kit._latest = "0.9.0"
    kit._checked_at = 1e18

    assert kit.assess("someone-agent", "0.4.0")["refused"] is True
    assert kit.assess("someone-agent", "0.5.0")["refused"] is False


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
