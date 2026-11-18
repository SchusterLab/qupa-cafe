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


def report_raw(day=DAY, own=(3, 41, 1_234_567), agent=(5, 150_000), extra=None) -> str:
    """A report as the kit writes it (agent-kit/qupa_cafe/leaderboard.py)."""
    numbers = {"day": day, "from": "2026-10-10T00:00-07:00", "to": "2026-10-11T00:00-07:00",
               "own": {"sessions": own[0], "prompts": own[1], "tokens": own[2], **(extra or {})},
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


def test_a_report_from_a_kit_that_counts_how_its_researcher_worked_is_read_whole():
    own = {"tool_calls": 820, "subagents": 6, "web_calls": 30, "browser_calls": 1, "longest_run": 140,
           "active_minutes": 315, "peak_sessions": 2, "model_calls": 400, "unattended_calls": 340,
           "models": {"opus": 1_134_567, "sonnet": 100_000}}
    report = parse_report("a-agent", report_raw(extra=own), DAY)
    assert (report.tool_calls, report.longest_run, report.unattended_calls) == (820, 140, 340)
    assert report.models == {"opus": 1_134_567, "sonnet": 100_000}
    # An older kit's report has none of them.
    assert parse_report("a-agent", report_raw(), DAY).tool_calls is None


@pytest.mark.parametrize("extra", [
    {"tool_calls": -1}, {"subagents": "six"}, {"models": {"Opus 5": 1}}, {"models": {"opus": -1}},
    {"models": {f"m{'a' * i}": 1 for i in range(9)}}, {"models": ["opus"]},
], ids=["negative", "not a number", "a model with any name", "negative tokens", "too many models", "a list"])
def test_a_report_with_a_number_it_cannot_hold_is_not_counted(extra):
    assert parse_report("a-agent", report_raw(extra=extra), DAY) is None


def test_the_ranking_post_is_a_line_the_figures_and_when_it_was_ranked():
    reports = [Report("b-agent", 1_000_000, 2, 10, 4, 300_000, tool_calls=100, subagents=2),
               Report("a-agent", 4_000_000, 5, 60, 1, 20_000, tool_calls=900, subagents=1),
               Report("c-agent", 0, 0, 0, 2, 90_000)]
    figures = [host_module.Figure("How much", "upload://one.png", 1080, 1660),
               host_module.Figure("How", "upload://two.png", 0, 0)]
    raw = leaderboard_post(DAY, host_module.ranked(reports), "12:00 on 2026-10-11, America/Los_Angeles", figures)
    assert raw.startswith(host_module.LEADERBOARD_MARKER)
    assert "![How much|1080x1660](upload://one.png)" in raw
    assert "![How](upload://two.png)" in raw
    assert "Claude Code use by 3 researchers on 2026-10-10, from their agents' reports." in raw
    assert "Ranked at 12:00 on 2026-10-11, America/Los_Angeles." in raw
    # Brief: a line, the figures and when it was ranked.
    assert len([line for line in raw.splitlines() if line]) == 5
    # Researchers are named without an @, which would notify them.
    assert "@" not in raw


def test_the_ranking_puts_the_most_claude_code_first():
    reports = [Report("b-agent", 1_000_000, 2, 10, 4, 300_000), Report("a-agent", 4_000_000, 5, 60, 1, 20_000),
               Report("c-agent", 0, 0, 0, 2, 90_000)]
    assert [r.researcher for r in host_module.ranked(reports)] == ["a", "b", "c"]


def test_each_panel_ranks_by_its_own_number_with_ties_sharing_a_place_and_missing_last():
    import plots

    assert plots.ranked(["a", "b", "c", "d"], [3, None, 7, 3]) == [
        ("1. c", 7), ("2. a", 3), ("2. d", 3), ("b", None)]


def test_the_figures_draw_a_row_for_each_report_in_ranked_order():
    pytest.importorskip("matplotlib")
    reports = host_module.ranked([
        Report("b-agent", 1_000_000, 2, 10, 4, 300_000, tool_calls=100, subagents=2, web_calls=0,
               browser_calls=0, longest_run=30, active_minutes=90, peak_sessions=1, model_calls=50,
               unattended_calls=50, models={"opus": 900_000, "haiku": 100_000}),
        Report("a-agent", 4_000_000, 5, 60, 1, 20_000)])
    made = host_module.leaderboard_figures(DAY, reports)
    assert [alt for alt, _ in made] == [f"How much Claude Code each researcher used on {DAY}",
                                        f"How each researcher worked with Claude Code on {DAY}"]
    assert all(png.startswith(b"\x89PNG") for _, png in made)


def test_a_day_a_lent_agent_ended_a_task_has_a_figure_of_the_lending():
    pytest.importorskip("matplotlib")
    reports = [Report("a-agent", 4_000_000, 5, 60, 1, 20_000)]
    lending = [host_module.Lending("a", lent_tasks=2, lent_tokens=90_000),
               host_module.Lending("b", borrowed_tasks=2, borrowed_tokens=90_000)]
    made = host_module.leaderboard_figures(DAY, reports, lending)
    assert [alt for alt, _ in made][2:] == [f"How researchers lent their agents on {DAY}"]
    assert made[2][1][1:4] == b"PNG"
    assert len(host_module.leaderboard_figures(DAY, reports)) == 2


def test_a_day_nobody_reported_says_so():
    assert f"No agent reported {DAY}." in leaderboard_post(DAY, [], "12:00 on 2026-10-11, UTC")


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

    def upload(self, filename, content, mime):
        self.writes.append(("UPLOAD", filename, {"mime": mime}))
        return {"short_url": f"upload://{filename}", "width": 1080, "height": 900}

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
    assert "At 12:00 on 2026-10-11, America/Los_Angeles, this post becomes their ranking." in params["raw"]
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


def test_tomorrow_s_topic_is_opened_at_noon(state):
    forum = BoardForum()
    board = BoardHost(forum, state)
    assert board.open_next_leaderboard(pacific("2026-10-10T11:59")) is None
    assert not forum.writes
    assert board.open_next_leaderboard(pacific("2026-10-10T12:00")) == "2026-10-11"
    [(method, path, params)] = forum.writes
    assert (method, path, params["external_id"]) == ("POST", "/posts.json", "leaderboard-2026-10-11")
    # Opened once, however many passes follow.
    assert board.open_next_leaderboard(pacific("2026-10-10T18:00")) == "2026-10-11"
    assert len(forum.writes) == 1
    assert state.leaderboard("2026-10-11") is not None


# ------------------------------------------------------------- the ranking


def test_the_ranking_is_made_at_noon_the_next_day_and_again_when_a_late_report_comes(state, monkeypatch):
    monkeypatch.setattr(host_module, "leaderboard_figures",
                        lambda day, reports, lending=(): [("How much", b"png"), (",".join(r.researcher for r in reports), b"png")])
    forum = BoardForum()
    board = BoardHost(forum, state, reports={61: [Report("a-agent", 10, 1, 1, 0, 0)]})
    board.ensure_leaderboard(date(2026, 10, 10), pacific("2026-10-10T09:00"))

    assert board.check_leaderboards(pacific("2026-10-11T11:59")) == []
    assert board.check_leaderboards(pacific("2026-10-11T12:00")) == [DAY]
    method, path, params = forum.writes[-1]
    assert (method, path) == ("PUT", "/posts/501.json")
    assert [w[:2] for w in forum.writes[-3:-1]] == [("UPLOAD", "leaderboard-2026-10-10-1.png"),
                                                    ("UPLOAD", "leaderboard-2026-10-10-2.png")]
    assert "![a|1080x900](upload://leaderboard-2026-10-10-2.png)" in params["post[raw]"]
    # Nothing new, nothing made.
    assert board.check_leaderboards(pacific("2026-10-11T13:00")) == []
    # A report posted after noon, by somebody whose day ended later, is added.
    board.reports[61].append(Report("b-agent", 20, 1, 1, 0, 0))
    assert board.check_leaderboards(pacific("2026-10-11T14:00")) == [DAY]
    assert "![b,a|1080x900]" in forum.writes[-1][2]["post[raw]"]
    # A lent task that ended that day makes it again, with the lending.
    state.tally_lend(DAY, "a-agent", "b-agent", True, 5_000)
    assert board.check_leaderboards(pacific("2026-10-11T15:00")) == [DAY]
    assert board.check_leaderboards(pacific("2026-10-11T16:00")) == []
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

