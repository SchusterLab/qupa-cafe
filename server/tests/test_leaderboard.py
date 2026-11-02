"""The daily leaderboard on the forum host: opening a day's topic for the
kits' reports, reading the reports, and ranking them at noon the next day.

Run from the repository root::

    python3 -m pytest server/tests -q
"""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import host as host_module  # noqa: E402
from host import Config, Handler, Host, Report, leaderboard_post, parse_report  # noqa: E402
from test_host import a_state, endpoint, post_wake, state  # noqa: E402,F401 - fixtures

DAY = "2026-10-10"
ZONE = "America/Los_Angeles"


def report_raw(day=DAY, own=(3, 41, 1_234_567), agent=(5, 150_000)) -> str:
    """A report as the kit writes it (agent-kit/qupa_cafe/leaderboard.py)."""
    numbers = {"day": day, "from": "2026-10-10T00:00-07:00", "to": "2026-10-11T00:00-07:00",
               "own": {"sessions": own[0], "prompts": own[1], "tokens": own[2]},
               "agent": {"wakes": agent[0], "tokens": agent[1]}}
    return f"<!-- qupa-cafe:leaderboard {json.dumps(numbers, separators=(',', ':'))} -->\nUsage from ..."


# ----------------------------------------------------------------- reports


def test_a_report_s_numbers_are_read_from_its_marker():
    assert parse_report("a-agent", report_raw(), DAY) == Report("a-agent", 1_234_567, 3, 41, 5, 150_000)
    assert parse_report("a-agent", report_raw(), DAY).researcher == "a"


@pytest.mark.parametrize("raw", [
    "no marker at all",
    report_raw(day="2026-10-09"),
    report_raw(own=(3, 41, -1)),
    report_raw(own=(3, 41, "many")),
    report_raw(agent=(True, 0)),
    "<!-- qupa-cafe:leaderboard {not json} -->",
    "<!-- qupa-cafe:leaderboard -->",
], ids=["no marker", "another day", "negative", "not a number", "a boolean", "not JSON", "the ranking's own"])
def test_anything_but_a_report_of_the_day_is_not_counted(raw):
    assert parse_report("a-agent", raw, DAY) is None


def test_the_ranking_puts_the_most_claude_code_first_with_a_bar_and_the_totals():
    reports = [Report("b-agent", 1_000_000, 2, 10, 4, 300_000), Report("a-agent", 4_000_000, 5, 60, 1, 20_000),
               Report("c-agent", 0, 0, 0, 2, 90_000)]
    raw = leaderboard_post(DAY, host_module.ranked(reports), "12:00 on 2026-10-11, America/Los_Angeles")
    assert raw.startswith(host_module.LEADERBOARD_MARKER)
    rows = [line for line in raw.splitlines() if line.startswith("| ") and line[2].isdigit()]
    assert [row.split(" | ")[1] for row in rows] == ["a", "b", "c"]
    # The longest bar is the first's, a quarter of it the second's, none the third's.
    assert "`" + "█" * host_module.BAR_WIDTH + "`" in rows[0]
    assert "`" + "█" * (host_module.BAR_WIDTH // 4) + "`" in rows[1]
    assert "`" not in rows[2]
    assert "| 4.0M | 5 | 60 | 1 | 20.0k |" in rows[0]
    assert "3 researchers reported. Their Claude Code used 5.0M in all, and 1.0M at the median" in raw
    assert "in 7 sessions with 70 prompts. Their agents used 410k in 7 wakes." in raw
    assert "Ranked at 12:00 on 2026-10-11, America/Los_Angeles." in raw
    # Researchers are named without an @, which would notify them.
    assert "@" not in raw


def test_a_day_nobody_reported_says_so():
    assert "No agent reported this day." in leaderboard_post(DAY, [], "12:00 on 2026-10-11, UTC")


# -------------------------------------------------------------- the topic


class BoardForum:
    """The forum as the service's own key sees it."""

    def __init__(self, site=None, existing=None):
        self.site = site if site is not None else [{"id": 9, "name": "Leaderboard"}]
        self.existing = existing or {}
        self.writes: list[tuple[str, str, dict]] = []

    def get(self, path):
        if path == "/site.json":
            return {"categories": self.site}
        if path.startswith("/t/external_id/"):
            external = path[len("/t/external_id/"):-len(".json")]
            if external not in self.existing:
                raise urllib.error.HTTPError(path, 404, "not found", {}, None)
            return self.existing[external]
        raise AssertionError(path)

    def write(self, method, path, params):
        self.writes.append((method, path, params))
        if path == "/posts.json":
            return {"id": 500 + len(self.writes), "topic_id": 60 + len(self.writes)}
        return {}


class BoardHost(Host):
    def __init__(self, forum, state, reports=None):
        self.forum = forum
        self.state = state
        self.config = Config(leaderboard_timezone=ZONE)
        import threading
        self.leaderboard_lock = threading.Lock()
        self.reports = reports or {}

    def leaderboard_reports(self, topic_id, day):
        return list(self.reports.get(topic_id, []))


def pacific(text: str) -> datetime:
    """A moment given in the rankings' own time zone."""
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(ZONE))


def test_a_day_s_topic_is_opened_once_in_the_leaderboard_category(state):
    forum = BoardForum()
    board = BoardHost(forum, state)
    now = pacific("2026-10-10T23:00")
    assert board.ensure_leaderboard(date(2026, 10, 10), now) == {"topic_id": 61, "title": "Leaderboard for 2026-10-10"}
    [(method, path, params)] = forum.writes
    assert (method, path) == ("POST", "/posts.json")
    assert params["category"] == "9" and params["external_id"] == "leaderboard-2026-10-10"
    assert params["raw"].startswith(host_module.LEADERBOARD_MARKER)
    assert "At 12:00 on 2026-10-11, America/Los_Angeles, this post becomes the ranking" in params["raw"]
    assert board.ensure_leaderboard(date(2026, 10, 10), now)["topic_id"] == 61
    assert len(forum.writes) == 1


def test_a_topic_the_forum_has_already_is_found_by_its_external_id(state):
    forum = BoardForum(existing={"leaderboard-2026-10-10": {"id": 33, "post_stream": {"posts": [{"id": 330}]}}})
    board = BoardHost(forum, state)
    assert board.ensure_leaderboard(date(2026, 10, 10), pacific("2026-10-10T23:00"))["topic_id"] == 33
    assert not forum.writes
    assert state.leaderboard(DAY) == (33, 330)


@pytest.mark.parametrize("day, refused", [
    (date(2026, 10, 7), "not a day this forum ranks now"),
    (date(2026, 10, 12), "not a day this forum ranks now"),
])
def test_a_day_far_from_today_is_refused(state, day, refused):
    board = BoardHost(BoardForum(), state)
    with pytest.raises(host_module.LeaderboardRefused) as exc:
        board.ensure_leaderboard(day, pacific("2026-10-10T23:00"))
    assert refused in str(exc.value)


def test_a_kit_s_own_day_ahead_or_two_days_behind_is_opened(state):
    """A kit's day is its own time zone's, and a report may come in late."""
    board = BoardHost(BoardForum(), state)
    for day in (date(2026, 10, 8), date(2026, 10, 11)):
        board.ensure_leaderboard(day, pacific("2026-10-10T23:00"))


def test_a_forum_without_the_category_is_refused(state):
    with pytest.raises(host_module.LeaderboardRefused) as exc:
        BoardHost(BoardForum(site=[]), state).ensure_leaderboard(date(2026, 10, 10), pacific("2026-10-10T23:00"))
    assert "no Leaderboard category" in str(exc.value)


# ------------------------------------------------------------- the ranking


def test_the_ranking_is_made_at_noon_the_next_day_and_again_when_a_late_report_comes(state):
    forum = BoardForum()
    board = BoardHost(forum, state, reports={61: [Report("a-agent", 10, 1, 1, 0, 0)]})
    board.ensure_leaderboard(date(2026, 10, 10), pacific("2026-10-10T09:00"))

    assert board.check_leaderboards(pacific("2026-10-11T11:59")) == []
    assert board.check_leaderboards(pacific("2026-10-11T12:00")) == [DAY]
    method, path, params = forum.writes[-1]
    assert (method, path) == ("PUT", "/posts/501.json")
    assert "| 1 | a |" in params["post[raw]"]
    # Nothing new, nothing made.
    assert board.check_leaderboards(pacific("2026-10-11T13:00")) == []
    # A report posted after noon, by somebody whose day ended later, is added.
    board.reports[61].append(Report("b-agent", 20, 1, 1, 0, 0))
    assert board.check_leaderboards(pacific("2026-10-11T14:00")) == [DAY]
    assert "| 1 | b |" in forum.writes[-1][2]["post[raw]"]
    # A week on, it is left as it is.
    board.reports[61].append(Report("c-agent", 30, 1, 1, 0, 0))
    assert board.check_leaderboards(pacific("2026-10-19T12:00")) == []


def test_the_hour_follows_the_time_zone_s_clock_change(state):
    """On 1 November 2026 Pacific time goes back an hour; noon is still noon."""
    board = BoardHost(BoardForum(), state)
    assert board.ranked_at(date(2026, 11, 1)).utcoffset().total_seconds() == -8 * 3600
    assert board.ranked_at(date(2026, 10, 30)).utcoffset().total_seconds() == -7 * 3600
    assert board.ranked_at(date(2026, 11, 1)).astimezone(timezone.utc).hour == 20


# ------------------------------------------------------------- the endpoint


def test_a_kit_asks_for_the_day_s_topic(endpoint, monkeypatch):
    asked = []
    monkeypatch.setattr(Handler.host, "ensure_leaderboard",
                        lambda day: asked.append(day) or {"topic_id": 61, "title": "Leaderboard for 2026-10-10"},
                        raising=False)
    status, payload = post_wake(endpoint, {"leaderboard": "2026-10-10"})
    assert status == 200 and payload["leaderboard"]["topic_id"] == 61
    assert asked == [date(2026, 10, 10)]
    assert post_wake(endpoint, {"leaderboard": "yesterday"})[0] == 400


def test_a_refused_day_is_a_409_with_the_reason(endpoint, monkeypatch):
    def refuse(day):
        raise host_module.LeaderboardRefused("the forum has no Leaderboard category")
    monkeypatch.setattr(Handler.host, "ensure_leaderboard", refuse, raising=False)
    status, payload = post_wake(endpoint, {"leaderboard": "2026-10-10"})
    assert status == 409 and "no Leaderboard category" in payload["error"]

