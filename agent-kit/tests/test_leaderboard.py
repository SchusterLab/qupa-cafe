"""The daily leaderboard report: which day it is of, what it counts, and how
the daemon posts it."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import qupa_cafe.daemon as daemon
from qupa_cafe import diary, leaderboard
from qupa_cafe.daemon import ForumHostError, Handled, Journal, Settings

DAY = date(2026, 10, 10)


def local(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.min.time()).astimezone() + timedelta(hours=hour, minutes=minute)


def at(hour: int, minute: int = 0, day: date = DAY) -> str:
    """A transcript timestamp, as Claude Code writes it, for a local time."""
    return local(hour, minute, day).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def typed(text: str, when: str) -> dict:
    return {"type": "user", "timestamp": when, "message": {"role": "user", "content": text}}


def response(message_id: str, when: str, tokens: int = 1000, model: str = "claude-sonnet-5-5") -> dict:
    return {"type": "assistant", "timestamp": when,
            "message": {"id": message_id, "model": model, "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": tokens, "output_tokens": 0}}}


def write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def projects(claude_home):
    return claude_home / "projects"


@pytest.fixture
def handled(tmp_path):
    return Handled(str(tmp_path / "daemon.sqlite3"))


# ----------------------------------------------------------------- the day


def test_a_report_posted_at_nine_is_of_that_day_and_one_at_eight_of_the_day_before(handled):
    """A researcher whose diary hour is 9:00 reports the 24 hours to 9:00 as
    the day they end on; one whose hour is 8:00, as the day before."""
    board = leaderboard.Leaderboard()
    day, start, end = board.due(handled, local(9, 0, DAY + timedelta(days=1)), hour=9)
    assert day == DAY + timedelta(days=1)
    assert (start, end) == (local(9), local(9, 0, DAY + timedelta(days=1)))
    day, start, end = board.due(handled, local(8, 0, DAY + timedelta(days=1)), hour=8)
    assert day == DAY
    assert (start, end) == (local(8), local(8, 0, DAY + timedelta(days=1)))
    # At noon and at 11:00 it is the same.
    assert board.due(handled, local(12, 0, DAY + timedelta(days=1)), hour=12)[0] == DAY + timedelta(days=1)
    assert board.due(handled, local(11, 0, DAY + timedelta(days=1)), hour=11)[0] == DAY + timedelta(days=1)
    # A minute before 9:00, the 9:00 report due is still the one of the day before.
    assert board.due(handled, local(8, 59, DAY + timedelta(days=1)), hour=9)[0] == DAY


def test_a_day_is_reported_once_and_a_failure_is_tried_again_after_an_hour(handled):
    board = leaderboard.Leaderboard()
    now = local(15, 0, DAY + timedelta(days=1))
    board.failed(DAY, now)
    assert board.due(handled, now + timedelta(minutes=30)) is None
    assert board.due(handled, now + timedelta(minutes=61))[0] == DAY
    handled.mark(leaderboard.HANDLED_KIND, diary.day_key(DAY))
    assert board.due(handled, now + timedelta(hours=2)) is None


# ---------------------------------------------------------------- counting


def test_the_researcher_s_own_use_counts_sessions_prompts_and_tokens_in_the_hours(projects):
    start, end = local(0), local(0, 0, DAY + timedelta(days=1))
    write(projects / "p" / "mine.jsonl", [
        typed("before the day", at(-2)), response("m0", at(-2)),
        typed("Fit the T1 data", at(9)), response("m1", at(9, 1)),
        {"type": "user", "timestamp": at(9, 2), "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        response("m2", at(9, 3), tokens=500),
    ])
    # A fork of it repeats its responses, which are counted once.
    write(projects / "p" / "fork.jsonl", [response("m1", at(9, 1)), typed("and the T2", at(10)),
                                          response("m3", at(10, 1))])
    # A subagent's calls are the session's.
    write(projects / "p" / "mine" / "subagents" / "agent-1.jsonl", [response("s1", at(9, 5), tokens=200)])
    # Neither a wake the journal records nor a session the kit named is the researcher's.
    write(projects / "p" / "wake.jsonl", [typed("a wake's prompt", at(9)), response("w1", at(9, 1))])
    write(projects / "p" / "named.jsonl", [{"type": "custom-title", "customTitle": "qupa-cafe wake"},
                                           typed("x", at(9)), response("n1", at(9, 1))])

    own = leaderboard.own_usage(projects, start, end, {"wake"}, ("qupa-cafe",))
    assert (own.sessions, own.prompts, own.tokens) == (2, 2, 1000 + 500 + 1000 + 200)


def test_the_agent_s_use_counts_its_wakes_but_not_its_parents_and_their_calls_in_the_hours(projects, tmp_path):
    start, end = local(0), local(0, 0, DAY + timedelta(days=1))
    state = str(tmp_path / "daemon.sqlite3")
    journal = Journal(state)

    def started(moment: datetime) -> str:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for session, when, reason in (("parent", local(8), daemon.PARENT_REASON), ("w1", local(9), "mention"),
                                  ("w2", local(23, 50), "mention"), ("late", local(1, 0, DAY + timedelta(days=1)), "x"),
                                  ("early", local(-1), "mention")):
        journal.db.execute("INSERT INTO wakes (started, status, runtime, session, agent_dir, title, reason)"
                           " VALUES (?, 'finished', 'claude', ?, '', 't', ?)", (started(when), session, reason))
    journal.db.commit()
    write(projects / "a" / "parent.jsonl", [response("p1", at(8), tokens=3000)])
    write(projects / "a" / "w1.jsonl", [response("a1", at(9), tokens=100)])
    # A wake that runs past the end counts only its calls before it.
    write(projects / "a" / "w2.jsonl", [response("a2", at(23, 55), tokens=10),
                                        response("a3", at(0, 5, DAY + timedelta(days=1)), tokens=99)])
    write(projects / "a" / "early.jsonl", [response("e1", at(0, 10), tokens=7)])

    agent = leaderboard.agent_usage(state, start, end, daemon.PARENT_REASON)
    assert agent.wakes == 2
    assert agent.tokens == 3000 + 100 + 10 + 7


# ------------------------------------------------------------- the report


def test_the_report_holds_its_numbers_for_the_host_and_says_them_for_a_person():
    report = leaderboard.Report(DAY, local(12, 0, DAY - timedelta(days=1)), local(12),
                                own=leaderboard.Own(3, 41, 1_234_567), agent=leaderboard.Agent(5, 150_000))
    raw = leaderboard.raw(report, "sunada-agent", "sunada")
    first, rest = raw.split("\n", 1)
    assert first.startswith("<!-- qupa-cafe:leaderboard {") and first.endswith("} -->")
    numbers = json.loads(first[len("<!-- qupa-cafe:leaderboard "):-len(" -->")])
    assert numbers["day"] == "2026-10-10"
    assert numbers["own"] == {"sessions": 3, "prompts": 41, "tokens": 1_234_567}
    assert numbers["agent"] == {"wakes": 5, "tokens": 150_000}
    assert "sunada's Claude Code: 1.2M, in 3 sessions with 41 prompts typed." in rest
    assert "sunada-agent: 150k, in 5 wakes." in rest
    assert "@" not in raw


def test_the_setting_is_on_unless_env_turns_it_off(tmp_path):
    env = tmp_path / "env"
    assert leaderboard.enabled(env)
    env.write_text("QUPA_LEADERBOARD=off\n", encoding="utf-8")
    assert not leaderboard.enabled(env)
    env.write_text("QUPA_LEADERBOARD=on\n", encoding="utf-8")
    assert leaderboard.enabled(env)


# ---------------------------------------------------------------- the pass


class BoardForum:
    def __init__(self):
        self.replies, self.followed = [], []

    def reply(self, topic_id, raw):
        self.replies.append((topic_id, raw))
        return {"id": 1}

    def follow(self, topic_id, level):
        self.followed.append((topic_id, level))


class BoardHost:
    def __init__(self, fail=False):
        self.asked, self.fail = [], fail

    def leaderboard_topic(self, agent, day):
        self.asked.append((agent, day))
        if self.fail:
            raise ForumHostError("the forum has no Leaderboard category")
        return {"topic_id": 77}


def _settings(**overrides):
    return Settings(**{"api_username": "a-agent", "researcher": "a", "agent_dir": "", "host_url": "http://h",
                       "diary_hour": 0, **overrides})


NOW = local(15, 0, DAY + timedelta(days=1))


def test_a_day_with_use_is_reported_once_in_the_day_s_topic(handled, projects, tmp_path):
    write(projects / "p" / "mine.jsonl", [typed("work", at(9)), response("m1", at(9, 1))])
    forum, host = BoardForum(), BoardHost()
    board = leaderboard.Leaderboard()
    assert daemon.leaderboard_pass(_settings(), forum, host, handled, board, False, None, now=NOW)
    assert host.asked == [("a-agent", DAY)]
    [(topic_id, raw)] = forum.replies
    assert topic_id == 77 and '"day":"2026-10-10"' in raw
    # Told only of replies to it and mentions, so other reports wake nobody.
    assert forum.followed == [(77, daemon.REGULAR)]
    assert handled.seen(leaderboard.HANDLED_KIND, diary.day_key(DAY))
    assert not daemon.leaderboard_pass(_settings(), forum, host, handled, board, False, None, now=NOW)
    assert len(forum.replies) == 1


def test_it_is_reported_whether_or_not_the_diary_is_on_but_not_when_turned_off(handled, projects, tmp_path):
    write(projects / "p" / "mine.jsonl", [typed("work", at(9)), response("m1", at(9, 1))])
    forum = BoardForum()
    (tmp_path / "env").write_text("QUPA_DIARY=off\nQUPA_LEADERBOARD=off\n", encoding="utf-8")
    assert not daemon.leaderboard_pass(_settings(), forum, BoardHost(), handled, leaderboard.Leaderboard(), False,
                                       None, now=NOW)
    (tmp_path / "env").write_text("QUPA_DIARY=off\n", encoding="utf-8")
    assert daemon.leaderboard_pass(_settings(), forum, BoardHost(), handled, leaderboard.Leaderboard(), False,
                                   None, now=NOW)


def test_a_day_without_use_posts_nothing_and_is_done(handled, projects):
    forum, host = BoardForum(), BoardHost()
    assert not daemon.leaderboard_pass(_settings(), forum, host, handled, leaderboard.Leaderboard(), False, None,
                                       now=NOW)
    assert not forum.replies and not host.asked
    assert handled.seen(leaderboard.HANDLED_KIND, diary.day_key(DAY))


def test_a_host_that_refuses_leaves_the_day_to_report_in_an_hour(handled, projects):
    write(projects / "p" / "mine.jsonl", [typed("work", at(9)), response("m1", at(9, 1))])
    forum, board = BoardForum(), leaderboard.Leaderboard()
    assert not daemon.leaderboard_pass(_settings(), forum, BoardHost(fail=True), handled, board, False, None, now=NOW)
    assert not handled.seen(leaderboard.HANDLED_KIND, diary.day_key(DAY))
    host = BoardHost()
    assert not daemon.leaderboard_pass(_settings(), forum, host, handled, board, False, None,
                                       now=NOW + timedelta(minutes=10))
    assert not host.asked
    assert daemon.leaderboard_pass(_settings(), forum, host, handled, board, False, None,
                                   now=NOW + timedelta(minutes=61))


def test_a_dry_run_posts_nothing_and_leaves_the_day(handled, projects):
    write(projects / "p" / "mine.jsonl", [typed("work", at(9)), response("m1", at(9, 1))])
    forum, host = BoardForum(), BoardHost()
    daemon.leaderboard_pass(_settings(), forum, host, handled, leaderboard.Leaderboard(), True, None, now=NOW)
    assert not forum.replies and not host.asked
    assert not handled.seen(leaderboard.HANDLED_KIND, diary.day_key(DAY))
