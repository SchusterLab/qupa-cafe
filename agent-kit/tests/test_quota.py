"""The limits at which wakes stop: the cap on the share of the researcher's
Claude plan, and the budget on what the agent's own wakes spend in five hours."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from qupa_cafe import browse, daemon, install, quota
from test_browse import SAID, _browser, _row
from test_daemon import MENTION, FakeForum, FakeHost, _settings, _started, _topic, _woken, handled  # noqa: F401
from test_wakes import _response, _transcript

NOW = 1_800_000_000.0
LATER = NOW + 3600

# What qupa-install writes to env whatever it asks.
INSTALLED = {"forum_url": "http://x", "api_key": "k", "api_username": "u", "policy_topic_id": "1"}


def _status(five_hour=None, seven_day=None) -> dict:
    """A status line's input, as Claude Code gives it on a Pro or Max plan."""
    limits = {}
    if five_hour is not None:
        limits["five_hour"] = {"used_percentage": five_hour, "resets_at": LATER}
    if seven_day is not None:
        limits["seven_day"] = {"used_percentage": seven_day, "resets_at": LATER + 86400}
    return {"session_id": "s", "rate_limits": limits}


def _spent(claude_home, session, *costs, parent=0):
    """A journalled wake with one response for each ``(seconds ago, tokens)``
    in ``costs``, each costing that many uncached input tokens, after a
    fork's parent's response costing ``parent``."""

    def at(ago):
        return datetime.fromtimestamp(time.time() - ago, timezone.utc).isoformat().replace("+00:00", "Z")

    said = {"type": "text", "text": "ok"}
    records = []
    if parent:
        records += [{"type": "user", "message": {"role": "user", "content": daemon.PARENT_MARK + " The policy..."}},
                    {**_response("msg_parent", "claude-opus-5-5", parent, 0, 0, 0, said), "timestamp": at(60)}]
    records.append({"type": "user", "message": {"role": "user", "content": "You were woken by a post."}})
    for index, (ago, tokens) in enumerate(costs):
        response = {**_response(f"msg_{session}_{index}", "claude-opus-5-5", tokens, 0, 0, 0, said), "timestamp": at(ago)}
        # Claude Code writes a record for each block of a response.
        records += [response, response]
    _transcript(claude_home, session, records)
    journal = daemon.Journal(quota.state_path())
    journal.end(journal.begin(daemon.Wake(agent_dir="/agent", topic_id=10, post_number=1, title="T",
                                          reason="you were mentioned", session=session)), True)


# --------------------------------------------------------------- settings


@pytest.mark.parametrize("value, cap", [("80", "80"), (" 95% ", "95"), ("100", "100"), ("0", ""), ("101", ""),
                                        ("", ""), ("none", ""), ("8.5", "")])
def test_a_cap_is_a_whole_percentage_or_none(value, cap):
    assert quota.cap_of(value) == cap


@pytest.mark.parametrize("value, budget, tokens", [("1M", "1M", 1_000_000), ("500k", "500k", 500_000),
                                                   ("1,500,000", "1500k", 1_500_000), ("2.5m", "2500k", 2_500_000),
                                                   ("1234", "1234", 1234), ("none", "", 0), ("-5k", "", 0)])
def test_a_budget_is_a_count_of_tokens_or_none(value, budget, tokens):
    assert quota.budget_of(value) == budget and quota.tokens_of(budget) == tokens


def test_the_cap_and_the_budget_are_kept_in_env_beside_the_other_settings():
    env = install.ENV_PATH
    env.write_text("QUPA_API_KEY=k\nQUPA_QUOTA_CAP=70\n", encoding="utf-8")
    assert quota.read(env) == "70"
    quota.write("90", env)
    quota.write("2M", env, key=quota.BUDGET_KEY)
    assert env.read_text(encoding="utf-8") == "QUPA_API_KEY=k\nQUPA_QUOTA_CAP=90\nQUPA_WAKE_BUDGET=2M\n"
    assert quota.read(env, key=quota.BUDGET_KEY) == "2M"


@pytest.mark.parametrize("key, choices, step", [(quota.KEY, quota.CAPS, quota.step),
                                                (quota.BUDGET_KEY, quota.BUDGETS, quota.step_budget)])
def test_the_browser_s_keys_step_through_the_choices(key, choices, step):
    quota.write("", key=key)
    assert [step() for _ in choices] == [*choices[1:], choices[0]]


# --------------------------------------------------------------- readings


def test_no_reading_or_no_cap_holds_nothing_back():
    quota.write("80")
    assert quota.held_back(now=NOW) == ""
    quota.record(_status(five_hour=99), now=NOW)
    quota.write("")
    assert quota.held_back(now=NOW) == ""


def test_a_window_at_the_cap_holds_wakes_back_until_it_resets():
    quota.write("80")
    quota.record(_status(five_hour=40, seven_day=79), now=NOW)
    assert quota.held_back(now=NOW) == ""
    quota.record(_status(five_hour=40, seven_day=80), now=NOW)
    held = quota.held_back(now=NOW)
    assert "80% of its weekly limit" in held and "UTC" in held
    # Past its reset time the reading no longer counts.
    assert quota.held_back(now=LATER + 86401) == ""


def test_a_window_a_reading_leaves_out_keeps_its_last_reading():
    quota.record(_status(five_hour=10, seven_day=50), now=NOW)
    quota.record(_status(five_hour=12), now=NOW + 60)
    kept = quota.usage(now=NOW + 60)
    assert kept["five_hour"]["used"] == 12 and kept["seven_day"]["used"] == 50


def test_the_status_line_records_claude_code_s_reading_and_prints_a_line():
    ran = subprocess.run(
        [sys.executable, quota.__file__], input=json.dumps(_status(five_hour=6, seven_day=10)),
        capture_output=True, text=True, timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == "Qupa Cafe wake · five-hour 6%, weekly 10%"
    assert set(quota.load()) == {"five_hour", "seven_day"}


def test_a_status_line_without_rate_limits_records_nothing():
    """As on an API key, where Claude Code gives none."""
    quota.record({"session_id": "s"})
    assert not quota.readings_path().exists()


def test_every_wake_is_given_the_status_line():
    given = json.loads(daemon.wake_settings(None))
    assert given["statusLine"] == {"type": "command", "command": quota.status_command()}
    assert "quota.py" in given["statusLine"]["command"]


# ------------------------------------------------------------- the budget


def test_the_budget_counts_the_last_five_hours_of_the_agent_s_wakes(claude_home):
    # A fork's parent's cost is the parent wake's, not this one's; a
    # response six hours old is out of the window.
    _spent(claude_home, "a", (6 * 3600, 900_000), (3600, 300_000), parent=50_000)
    _spent(claude_home, "b", (60, 200_000))
    assert quota.spent() == 500_000

    quota.write("1M", key=quota.BUDGET_KEY)
    assert quota.held_back() == ""
    quota.write("500k", key=quota.BUDGET_KEY)
    held = quota.held_back()
    assert held.startswith("its wakes have spent 500k input-token equivalents") and "allow 500k (QUPA_WAKE_BUDGET)" in held
    # Under again once the hour-old 300k is five hours old.
    assert time.strftime("%H:%M UTC", time.gmtime(time.time() - 3600 + quota.BUDGET_SECONDS)) in held


def test_the_cap_and_the_budget_hold_back_apart(claude_home):
    _spent(claude_home, "a", (60, 600_000))
    quota.write("500k", key=quota.BUDGET_KEY)
    quota.write("80")
    quota.record(_status(five_hour=10))
    assert "QUPA_WAKE_BUDGET" in quota.held_back()
    quota.record(_status(five_hour=90))
    assert "QUPA_QUOTA_CAP" in quota.held_back()


# ------------------------------------------------------------- the daemon


def test_at_the_cap_nobody_is_woken_and_whoever_waits_is_told(handled, monkeypatch):
    quota.write("80")
    quota.record(_status(five_hour=85))
    woken = _woken(monkeypatch)
    forum = FakeForum({10: _topic(10)}, notes=[MENTION])
    host = FakeHost()

    assert daemon.one_pass(_settings(), forum, host, handled, dry_run=False) == 0
    assert woken == []
    (told,) = [entry for entry in host.told if entry[0] == "failed"]
    assert told[2] == 10 and "85% of its five-hour limit" in told[3]

    # Under it again, the mention is answered.
    quota.write("90")
    assert daemon.one_pass(_settings(), forum, FakeHost(), handled, dry_run=False) == 1


@pytest.mark.parametrize("limit", ["cap", "budget"])
def test_a_wake_that_reaches_a_limit_is_stopped(claude_home, monkeypatch, limit):
    ran = []

    def fake_run(argv, **kwargs):
        ran.append(argv[1:3])
        return _started("backgrounded · 2bc6e6bc · qupa wake: topic 10\n" if "--bg" in argv else "")(argv)

    def reached():
        # Its status line reads 95%, or its transcript shows 2M spent.
        if limit == "cap":
            quota.record(_status(five_hour=95))
        else:
            _spent(claude_home, "full", (5, 2_000_000))

    listed = iter(range(1000))

    def sessions(claude):
        if next(listed) == 2:
            reached()
        return [{"id": "2bc6e6bc", "sessionId": "full", "state": "working"}]

    if limit == "cap":
        quota.write("80")
    else:
        quota.write("1M", key=quota.BUDGET_KEY)
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon, "background_sessions", sessions)
    monkeypatch.setattr(daemon, "BACKGROUND_POLL_SECONDS", 0)

    state, session = daemon.run_in_background("claude", ["claude", "--bg", "p"], None)
    assert (state, session) == (daemon.CAPPED, "full")
    assert ["stop", "2bc6e6bc"] in ran
    assert daemon.failure_reason(state, session).startswith(
        "it was stopped once " + ("its researcher's Claude plan has used 95%" if limit == "cap" else "its wakes have spent 2.0M"))


# ---------------------------------------------- qupa-install and the browser


@pytest.mark.parametrize("ask, setting, answers, value", [
    (install.ask_quota, "quota_cap", [""], "80"),
    (install.ask_quota, "quota_cap", ["none"], ""),
    (install.ask_quota, "quota_cap", ["150", "75"], "75"),
    (install.ask_budget, "wake_budget", [""], "1M"),
    (install.ask_budget, "wake_budget", ["none"], ""),
    (install.ask_budget, "wake_budget", ["lots", "300k"], "300k"),
])
def test_qupa_install_asks_for_the_cap_and_the_budget(monkeypatch, ask, setting, answers, value):
    given = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(given))
    settings = {}
    ask(settings)
    assert settings[setting] == value


def test_a_scripted_install_writes_neither_so_a_later_one_in_a_terminal_asks():
    install.write_env(INSTALLED)
    written = install.ENV_PATH.read_text(encoding="utf-8")
    assert quota.KEY not in written and quota.BUDGET_KEY not in written
    install.write_env({**INSTALLED, "quota_cap": "70", "wake_budget": "500k"})
    written = install.ENV_PATH.read_text(encoding="utf-8")
    assert f"{quota.KEY}=70" in written and f"{quota.BUDGET_KEY}=500k" in written


def test_the_wake_browser_shows_the_cap_and_the_last_reading():
    assert "No cap" in browse.visible(browse.quota_line())
    quota.write("80")
    quota.record(_status(five_hour=85, seven_day=10), now=time.time())
    line = browse.visible(browse.quota_line())
    assert "Wakes stop at 80% of your plan, and are stopped now" in line and "five-hour 85%, weekly 10%" in line
    assert browse.change_quota() == "Wakes stop at 90% of your Claude plan's five-hour or weekly usage, from the next wake."


def test_the_wake_browser_shows_the_budget_and_what_wakes_spent(claude_home):
    assert "No budget" in browse.visible(browse.budget_line())
    _spent(claude_home, "a", (60, 600_000))
    quota.write("500k", key=quota.BUDGET_KEY)
    line = browse.visible(browse.budget_line())
    assert "Wakes may spend 500k in any five hours, and are stopped now" in line and "600k in the last five hours" in line
    assert browse.change_budget() == "Wakes may spend 1M input-token equivalents in any five hours, from now."


def test_c_and_b_in_the_list_step_the_limits_and_q_does_not():
    """q is what people press to quit, and would change the cap unseen."""
    b = _browser(browse.Wake(_row(1), SAID))
    assert b.handle("c") == "quota"
    assert b.handle("b") == "budget"
    assert b.handle("q") is None
