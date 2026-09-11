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

from ranking import Agent, Config, Decision, State, parse_agents  # noqa: E402


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
