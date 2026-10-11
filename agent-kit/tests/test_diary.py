"""The daily diary: which sessions it reads, what it makes of them, when it
wakes the agent, and where it tells the agent to post."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import qupa_cafe.daemon as daemon
from qupa_cafe import diary, texts
from qupa_cafe.daemon import Handled, Journal, Settings

# A moment in the afternoon, local time, and the day before it.
NOW = datetime.combine(date(2026, 10, 11), datetime.min.time()).astimezone() + timedelta(hours=15)
DAY = date(2026, 10, 10)


def at(hour: int, minute: int = 0, day: date = DAY) -> str:
    """A transcript timestamp, as Claude Code writes it, for a local time."""
    local = datetime.combine(day, datetime.min.time()).astimezone() + timedelta(hours=hour, minutes=minute)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def prompt(text, when, cwd="/home/r/thesis", **extra) -> dict:
    return {"type": "user", "timestamp": when, "cwd": cwd, "message": {"role": "user", "content": text}, **extra}


def answer(text, when) -> dict:
    return {"type": "assistant", "timestamp": when, "message": {"content": [{"type": "text", "text": text}]}}


def tool_result(when) -> dict:
    return {"type": "user", "timestamp": when,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "file contents"}]}}


def write_session(projects: Path, session: str, records: list[dict], project: str = "p") -> Path:
    path = projects / project / f"{session}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def window():
    return diary.last_day(NOW)


# ----------------------------------------------------------------- the day


def test_the_day_is_the_one_that_ended_at_the_last_local_midnight():
    day, start, end = diary.last_day(NOW)
    assert day == DAY
    assert (start.hour, start.minute, end.hour) == (0, 0, 0)
    assert start.date() == DAY and end.date() == DAY + timedelta(days=1)
    # Just after midnight, it is the day that has just ended.
    assert diary.last_day(end + timedelta(minutes=1))[0] == DAY


def test_at_another_hour_the_day_is_the_one_the_middle_of_its_hours_is_in():
    """Posted at 15:00, the entry covers 15:00 the day before to 15:00 and is
    the diary of the day it is posted; posted at 4:00, as a night owl would
    have it, the diary of the day before."""
    day, start, end = diary.last_day(NOW, hour=15)
    assert day == DAY + timedelta(days=1)
    assert (start.date(), start.hour, end.date(), end.hour) == (DAY, 15, DAY + timedelta(days=1), 15)
    day, start, end = diary.last_day(NOW, hour=4)
    assert day == DAY and (start.date(), start.hour, end.date(), end.hour) == (DAY, 4, DAY + timedelta(days=1), 4)
    # Not yet 16:00, so the 16:00 entry due is the one that ended the day
    # before, whose middle, 4:00, is on that day.
    day, start, end = diary.last_day(NOW, hour=16)
    assert day == DAY and end.date() == DAY
    # Noon is the first hour that dates the entry by the day it is posted.
    assert diary.last_day(NOW, hour=11)[0] == DAY and diary.last_day(NOW, hour=12)[0] == DAY + timedelta(days=1)


# ------------------------------------------------------------ the sessions


def test_a_session_gives_each_typed_prompt_in_the_day_and_the_answer_that_ended_it(tmp_path):
    _, start, end = window()
    path = write_session(tmp_path, "s1", [
        prompt("from the day before", at(23, day=DAY - timedelta(days=1))),
        answer("old answer", at(23, 1, day=DAY - timedelta(days=1))),
        prompt("Fit the T1 data <system-reminder>secret context</system-reminder>", at(9)),
        answer("Reading the data.", at(9, 1)),
        tool_result(at(9, 2)),
        answer("T1 is 42 us.", at(9, 3)),
        prompt("<command-name>/clear</command-name>", at(10)),
        prompt("Caveat: added by the client", at(10, 1), isMeta=True),
        prompt("a subagent's prompt", at(10, 2), isSidechain=True),
        prompt("Now the T2 echo", at(11)),
    ])
    found = diary.read_session(path, start, end, daemon.KIT_SESSION_NAMES)
    assert found.cwd == "/home/r/thesis"
    assert [(t.prompt, t.answer) for t in found.turns] == [("Fit the T1 data", "T1 is 42 us."), ("Now the T2 echo", "")]


def test_the_kit_s_own_sessions_and_old_transcripts_are_left_out(tmp_path):
    _, start, end = window()
    write_session(tmp_path, "mine", [prompt("my work", at(9))])
    write_session(tmp_path, "woke", [prompt("a wake", at(9))])
    write_session(tmp_path, "tuned", [
        {"type": "custom-title", "customTitle": f"{daemon.TUNE_NAME} topic 4 post 1"}, prompt("tune", at(9))])
    write_session(tmp_path, "checked", [
        {"type": "agent-name", "agentName": daemon.CHECK_NAME}, prompt("check", at(9))])
    stale = write_session(tmp_path, "stale", [prompt("my work", at(9))])
    os.utime(stale, (start.timestamp() - 60, start.timestamp() - 60))

    found = diary.sessions(tmp_path, start, end, {"woke"}, daemon.KIT_SESSION_NAMES)
    assert [s.session for s in found] == ["mine"]


def test_a_long_day_keeps_its_first_and_last_prompts_and_says_what_it_left_out(tmp_path):
    _, start, end = window()
    records = []
    for n in range(40):
        records += [prompt(f"prompt {n} " + "x" * 300, at(8, n)), answer(f"answer {n}", at(8, n))]
    session = diary.read_session(write_session(tmp_path, "long", records), start, end, ())
    text = diary.digest([session], limit=3000)
    assert len(text) <= 3200
    assert "prompt 0 " in text and "prompt 39 " in text
    assert "in between left out" in text


def test_sessions_past_the_limit_are_named_rather_than_shown(tmp_path):
    _, start, end = window()
    found = []
    for n in range(30):
        path = write_session(tmp_path, f"s{n}", [prompt("y" * 450, at(9, n), cwd=f"/w/{n}")])
        found.append(diary.read_session(path, start, end, ()))
    text = diary.digest(found, limit=6000)
    assert len(text) <= 6600
    assert "more session(s) left out" in text and "/w/29" in text


# ---------------------------------------------------------------- the forum


SITE = [
    {"id": 1, "name": "Research"},
    {"id": 5, "name": "Diary", "color": "8C6D46"},
    {"id": 6, "name": "a", "parent_category_id": 5, "read_restricted": True},
    {"id": 7, "name": "b", "parent_category_id": 5},
    {"id": 8, "name": "a", "parent_category_id": 1, "read_restricted": True},
]


def test_a_diary_goes_in_diary_or_in_the_researcher_s_own_private_diary():
    assert diary.place(SITE, diary.PUBLIC, "a")["id"] == 5
    assert diary.place(SITE, diary.PRIVATE, "A")["id"] == 6
    # Not one anybody can read, nor one elsewhere, nor somebody else's.
    assert diary.place(SITE, diary.PRIVATE, "b") is None
    assert diary.place(SITE, diary.PRIVATE, "c") is None
    assert diary.place(SITE, diary.PRIVATE, "") is None
    assert diary.place(SITE, diary.OFF, "a") is None
    assert diary.place([{"id": 1, "name": "Research"}], diary.PUBLIC, "a") is None


def test_the_agent_is_told_who_reads_its_diary_and_how_to_post_in_it():
    public, private = SITE[1], SITE[2]
    first = diary.where_to_post("a-agent", diary.PUBLIC, public, None, DAY, "a")
    assert "create_topic" in first and "category=5" in first and '"Diary of a-agent"' in first
    assert "every member" in first
    # The topic opens with whose diary it is, and the entry is its first reply.
    opening = diary.opening_post("a-agent", "a")
    assert opening in first and "is not an entry" in first and "post this entry as a reply" in first
    assert opening.startswith("The diary of a's research, kept by a-agent.") and "@" not in opening

    later = diary.where_to_post("a-agent", diary.PUBLIC, public, {"id": 90, "title": "Diary of a-agent"}, DAY)
    assert "`reply`, topic_id=90" in later and "create_topic" not in later

    own = diary.where_to_post("a-agent", diary.PRIVATE, private, None, DAY)
    assert "create_topic" in own and "category=6" in own and '"a-agent, 2026-10-10"' in own
    assert "a topic of its own" in own and "only your researcher" in own
    assert '"diary-a-agent-2026-10-10"' in own


class SiteForum:
    def __init__(self, site, created):
        self.site, self.created = site, created

    def _get(self, path):
        if path == "/site.json":
            return {"categories": self.site}
        assert path == "/topics/created-by/a-agent.json"
        return {"topic_list": {"topics": self.created}}


def test_the_public_diary_is_the_first_open_topic_the_agent_made_there():
    forum = SiteForum(SITE, [
        {"id": 30, "category_id": 1}, {"id": 41, "category_id": 5}, {"id": 40, "category_id": 5, "archived": True},
        {"id": 44, "category_id": 5}, {"id": 50, "category_id": 6},
    ])
    category, topic = diary.forum_place(forum, "a-agent", diary.PUBLIC, "a")
    assert (category["id"], topic["id"]) == (5, 41)
    # A private diary has a topic for each day, so none is looked for.
    assert diary.forum_place(forum, "a-agent", diary.PRIVATE, "a") == (SITE[2], None)
    assert diary.forum_place(forum, "a-agent", diary.PRIVATE, "c") is None


# --------------------------------------------------------------- the prompt


def test_the_sessions_are_framed_as_data_and_substituted_last(monkeypatch):
    settings = Settings(api_username="a-agent", researcher="a", agent_dir="")
    _, start, end = window()
    filled = diary.build_prompt(settings, "%WHOAMI% %DAY% %WHERE%\n%SESSIONS%", DAY, start, end,
                                "Post here.", "I typed %WHERE% and %DAY%", "NOTICE")
    assert "working for `a`" in filled and "2026-10-10 Post here." in filled
    assert "I typed %WHERE% and %DAY%" in filled
    assert diary.DATA_NOTICE in filled and filled.endswith("NOTICE")


# ------------------------------------------------------------ when it wakes


@pytest.fixture
def handled(tmp_path):
    return Handled(str(tmp_path / "state.sqlite3"))


def test_the_first_poll_starts_the_diary_with_the_day_in_progress(handled):
    state = diary.Diary()
    assert state.due(handled, NOW) is None
    assert handled.seen(diary.HANDLED_KIND, diary.day_key(DAY))
    # The next day, the day before it is due, and once handled it is not.
    tomorrow = NOW + timedelta(days=1)
    assert state.due(handled, tomorrow)[0] == DAY + timedelta(days=1)
    handled.mark(diary.HANDLED_KIND, diary.day_key(DAY + timedelta(days=1)))
    assert state.due(handled, tomorrow) is None


def test_a_dry_run_does_not_start_the_diary(handled):
    assert diary.Diary().due(handled, NOW, mark=False) is None
    assert not handled.any(diary.HANDLED_KIND)


def test_a_day_is_written_once_whatever_the_hour_is_moved_to(handled):
    handled.mark(diary.HANDLED_KIND, 20000101)
    state = diary.Diary()
    # NOW is 15:00: at midnight the day before is due, and at 15:00 today.
    assert state.due(handled, NOW, hour=0)[0] == DAY
    handled.mark(diary.HANDLED_KIND, diary.day_key(DAY))
    assert state.due(handled, NOW, hour=15)[0] == DAY + timedelta(days=1)
    assert state.due(handled, NOW, hour=16) is None


def test_a_failed_diary_is_tried_again_after_an_hour_not_at_every_poll(handled):
    handled.mark(diary.HANDLED_KIND, 20000101)
    state = diary.Diary()
    state.failed(DAY, NOW)
    assert state.due(handled, NOW + timedelta(minutes=5)) is None
    assert state.due(handled, NOW + timedelta(minutes=61))[0] == DAY


@pytest.fixture
def projects(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home / "projects"


class FakeRanking:
    """The forum host, which makes the private diary when asked, adding it
    to what the forum shows the agent."""

    def __init__(self, site=None, refuse=""):
        self.site, self.refuse, self.asked = site, refuse, []

    def alive(self, agent):
        pass

    def private_diary(self, agent):
        self.asked.append(agent)
        if self.refuse:
            raise daemon.RankingError(self.refuse)
        self.site.append({"id": 9, "name": "a", "parent_category_id": 5, "read_restricted": True})
        return {"id": 9, "name": "a"}


def _pass(settings, handled, journal, monkeypatch, ok=True, site=SITE, created=(), ranking=None):
    woken = []
    monkeypatch.setattr(daemon, "wake_agent", lambda s, p, **k: woken.append((p, k)) or ok)
    handled.mark(diary.HANDLED_KIND, 20000101)
    woke = daemon.diary_pass(settings, SiteForum(site, list(created)), ranking or FakeRanking(site), handled,
                             diary.Diary(), False, journal, now=NOW)
    return woke, woken


def _settings(mode=diary.PUBLIC, **overrides):
    return Settings(api_username="a-agent", researcher="a", agent_dir="", diary=mode, **overrides)


def test_without_the_opt_in_nothing_is_read_or_woken(handled, projects, monkeypatch):
    write_session(projects, "mine", [prompt("work", at(9))])
    woke, woken = _pass(_settings(diary.OFF), handled, None, monkeypatch)
    assert not woke and not woken


def test_a_day_without_sessions_wakes_nothing_and_is_done(handled, projects, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%SESSIONS%"})
    woke, woken = _pass(_settings(), handled, None, monkeypatch)
    assert not woke and not woken
    assert handled.seen(diary.HANDLED_KIND, diary.day_key(DAY))


def test_a_day_with_sessions_wakes_the_agent_once_and_journals_it(handled, projects, tmp_path, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%WHERE%\n%SESSIONS%"})
    write_session(projects, "mine", [prompt("Fit the T1 data", at(9)), answer("T1 is 42 us.", at(9, 1))])
    journal = Journal(str(tmp_path / "state.sqlite3"))
    write_session(projects, "earlier-wake", [prompt("a wake's prompt", at(9))])
    journal.db.execute("INSERT INTO wakes (started, status, runtime, session, agent_dir, title, reason)"
                       " VALUES ('x', 'finished', 'claude', 'earlier-wake', '', 't', 'r')")
    journal.db.commit()

    woke, woken = _pass(_settings(), handled, journal, monkeypatch, created=[{"id": 41, "category_id": 5}])
    assert woke
    [(text, kwargs)] = woken
    assert "Fit the T1 data" in text and "T1 is 42 us." in text and "a wake's prompt" not in text
    assert "topic_id=41" in text
    assert kwargs["wake"].label == f"{daemon.WAKE_NAME} Diary of a-agent, 2026-10-10"
    assert handled.seen(diary.HANDLED_KIND, diary.day_key(DAY))
    titles = [row[0] for row in journal.db.execute("SELECT title FROM wakes WHERE session != 'earlier-wake'")]
    assert titles == ["Diary of a-agent, 2026-10-10"]


def test_a_private_diary_not_there_yet_is_made_by_the_forum_host_before_the_wake(handled, projects, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%WHERE%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    site = [c for c in SITE if c["id"] != 6]
    ranking = FakeRanking(site)
    woke, woken = _pass(_settings(diary.PRIVATE), handled, None, monkeypatch, site=site, ranking=ranking)
    assert woke and ranking.asked == ["a-agent"]
    assert "category=9" in woken[0][0] and "a-agent, 2026-10-10" in woken[0][0]


def test_one_there_already_is_not_asked_for(handled, projects, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%WHERE%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    ranking = FakeRanking(list(SITE))
    woke, woken = _pass(_settings(diary.PRIVATE), handled, None, monkeypatch, site=list(SITE), ranking=ranking)
    assert woke and not ranking.asked and "category=6" in woken[0][0]


def test_a_private_diary_the_host_will_not_make_leaves_the_day_to_do(handled, projects, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%WHERE%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    site = [c for c in SITE if c["id"] != 6]
    woke, woken = _pass(_settings(diary.PRIVATE), handled, None, monkeypatch, site=site,
                        ranking=FakeRanking(site, refuse="no Diary category"))
    assert not woke and not woken and not handled.seen(diary.HANDLED_KIND, diary.day_key(DAY))


def test_a_failed_diary_wake_leaves_the_day_to_do(handled, projects, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%SESSIONS%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    woke, _ = _pass(_settings(), handled, None, monkeypatch, ok=False)
    assert not woke and not handled.seen(diary.HANDLED_KIND, diary.day_key(DAY))


@pytest.mark.parametrize("served, site", [({"wake_prompt": "w"}, SITE), ({"wake_prompt": "w", "diary_prompt": "d"}, [])],
                         ids=["no wording", "no category"])
def test_a_diary_the_forum_cannot_take_is_deferred(handled, projects, monkeypatch, served, site):
    texts.save(served)
    write_session(projects, "mine", [prompt("work", at(9))])
    woke, woken = _pass(_settings(), handled, None, monkeypatch, site=site)
    assert not woke and not woken
    assert not handled.seen(diary.HANDLED_KIND, diary.day_key(DAY))


def test_the_diary_follows_env_as_it_stands_without_a_restart(handled, projects, monkeypatch, tmp_path):
    """The diary browser writes env; the daemon started with the diary off."""
    texts.save({"wake_prompt": "w", "diary_prompt": "%WHERE%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    diary.set_choice(diary.Choice(diary.PRIVATE))
    woke, woken = _pass(_settings(diary.OFF), handled, None, monkeypatch, site=list(SITE))
    assert woke and "category=6" in woken[0][0]


def test_the_diary_prompt_carries_qupa_md(monkeypatch, tmp_path):
    config = tmp_path / "home"
    config.mkdir()
    monkeypatch.setenv("QUPA_HOME", str(config))
    (config / "QUPA.md").write_text("Be brief.\n\n## The diary only\n\nUse a list.", encoding="utf-8")
    _, start, end = window()
    filled = diary.build_prompt(Settings(api_username="a-agent"), "%FROM_YOUR_RESEARCHER%", DAY, start, end, "", "", "")
    assert filled.index("Be brief.") < filled.index("Use a list.")


# ------------------------------------------------------- the sessions in full


def tool_call(name, arguments, when) -> dict:
    return {"type": "assistant", "timestamp": when,
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": name, "input": arguments}]}}


def tool_answer(text, when) -> dict:
    return {"type": "user", "timestamp": when,
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}}


def compacted(summary, when) -> dict:
    return {"type": "user", "timestamp": when, "isCompactSummary": True, "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content": summary}}


def test_a_session_in_full_is_its_prompts_and_final_replies_unclipped(tmp_path):
    _, start, end = window()
    long_prompt = "Fit the T1 data " + "with care " * 100
    path = write_session(tmp_path / "projects", "s1", [
        prompt("from the day before", at(23, day=DAY - timedelta(days=1))),
        prompt(long_prompt + "<system-reminder>secret context</system-reminder>", at(9)),
        answer("Reading the data first.", at(9, 1)),
        tool_call("Read", {"file_path": "/data/t1.csv"}, at(9, 1)),
        tool_answer("the file's contents", at(9, 2)),
        answer("T1 is 42 us. " * 60, at(9, 3)),
    ])
    session = diary.read_session(path, start, end, ())
    diary.write_transcripts([session], tmp_path / "out")
    text = session.written.read_text(encoding="utf-8")
    assert long_prompt.strip() in text and ("T1 is 42 us. " * 60).strip() in text
    # Nothing between the prompt and the reply that ended it, and nothing
    # from outside the window or added by the client.
    for left_out in ("Reading the data first.", "/data/t1.csv", "the file's contents", "secret context",
                     "from the day before"):
        assert left_out not in text
    # The digest clips the same turn, and names the file under its session.
    digest = diary.digest([session])
    assert f"In full: {session.written}" in digest and long_prompt.strip() not in digest


def test_a_session_s_title_pull_requests_and_edited_files_head_it(tmp_path):
    import tempfile

    _, start, end = window()
    scratch = str(Path(tempfile.gettempdir()) / "scratch.py")
    path = write_session(tmp_path / "projects", "s1", [
        {"type": "ai-title", "aiTitle": "HFSS simulation"},
        {"type": "custom-title", "customTitle": "Split mergemon coupling"},
        {"type": "pr-link", "prUrl": "https://example.org/pull/3", "timestamp": at(10)},
        {"type": "pr-link", "prUrl": "https://example.org/pull/1", "timestamp": at(10, day=DAY - timedelta(days=2))},
        {"type": "file-history-delta", "trackingPath": "scripts/fit.py", "backup": {"backupTime": at(10)}},
        {"type": "file-history-delta", "trackingPath": scratch, "backup": {"backupTime": at(10)}},
        prompt("Fit the T1 data", at(9)),
    ])
    session = diary.read_session(path, start, end, ())
    # The researcher's name for it over Claude Code's; only the window's
    # pull requests and edits; no scratch files.
    assert diary.meta_lines(session) == [
        "Title: Split mergemon coupling", "Pull requests: https://example.org/pull/3", "Files edited: scripts/fit.py"]
    diary.write_transcripts([session], tmp_path / "out")
    assert "Title: Split mergemon coupling" in session.written.read_text(encoding="utf-8")
    assert "Files edited: scripts/fit.py" in diary.digest([session])
    # The id, for the entry to name so the researcher can open the session.
    assert "Session: s1" in diary.digest([session])


def test_a_compaction_summary_is_claude_code_s_not_the_researcher_s(tmp_path):
    """Claude Code records it as the researcher's message."""
    _, start, end = window()
    path = write_session(tmp_path / "projects", "s1", [
        prompt("Fit the T1 data", at(9)),
        compacted("This session is being continued from a previous conversation. Summary: T1 fitted.", at(12)),
        prompt("Now the T2 echo", at(13)),
    ])
    session = diary.read_session(path, start, end, ())
    assert [t.prompt for t in session.turns] == ["Fit the T1 data", "Now the T2 echo"]
    diary.write_transcripts([session], tmp_path / "out")
    text = session.written.read_text(encoding="utf-8")
    assert "summary of the session so far" in text and "Summary: T1 fitted." in text
    assert text.index("Fit the T1 data") < text.index("Summary: T1 fitted.") < text.index("Now the T2 echo")


def test_the_prompt_says_where_the_sessions_are_in_full_or_that_they_are_not(tmp_path):
    settings = Settings(api_username="a-agent", researcher="a", agent_dir="")
    _, start, end = window()
    there = diary.transcripts_notice(tmp_path / "t")
    filled = diary.build_prompt(settings, "%SESSIONS%", DAY, start, end, "", "", "", there)
    # Appended when the served wording has no place for it.
    assert str(tmp_path / "t") in filled and "DATA" in there and "deleted when this wake ends" in there
    assert "excerpts above alone" in diary.transcripts_notice(None)


def test_a_sandboxed_diary_wake_reads_the_sessions_in_full_which_go_when_it_ends(handled, projects, tmp_path, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%SESSIONS%\n%TRANSCRIPTS%"})
    write_session(projects, "mine", [prompt("Fit the T1 data", at(9)), answer("T1 is 42 us.", at(9, 1))])
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(daemon, "sandbox_config", lambda settings: {"workspace": str(workspace)})
    monkeypatch.setattr(daemon, "wake_notice", lambda settings, config: "NOTICE")
    root = workspace / diary.TRANSCRIPTS_DIR
    # Left by a wake that never finished, which the next one clears.
    (root / "2026-10-01").mkdir(parents=True)
    seen = []

    def wake(settings, prompt, **kwargs):
        seen.append(sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md")))
        assert str(root / "2026-10-10") in prompt and "In full:" in prompt
        return True

    monkeypatch.setattr(daemon, "wake_agent", wake)
    handled.mark(diary.HANDLED_KIND, 20000101)
    assert daemon.diary_pass(_settings(), SiteForum(SITE, []), FakeRanking(SITE), handled, diary.Diary(), False,
                             None, now=NOW)
    assert seen == [["2026-10-10/01-home-r-thesis.md"]]
    assert not root.exists()


def test_the_sessions_go_even_when_the_wake_fails_and_without_a_sandbox_no_diary_is_written(
        handled, projects, tmp_path, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%SESSIONS%\n%TRANSCRIPTS%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(daemon, "sandbox_config", lambda settings: {"workspace": str(workspace)})
    monkeypatch.setattr(daemon, "wake_notice", lambda settings, config: "NOTICE")
    woke, _ = _pass(_settings(), handled, None, monkeypatch, ok=False)
    assert not woke and not (workspace / diary.TRANSCRIPTS_DIR).exists()

    monkeypatch.setattr(daemon, "sandbox_config", lambda settings: None)
    woke, woken = _pass(_settings(), handled, None, monkeypatch)
    assert not woke and woken == []


def test_a_diary_wake_runs_on_sonnet_and_other_wakes_on_the_researcher_s_default(handled, projects, monkeypatch):
    texts.save({"wake_prompt": "w", "diary_prompt": "%SESSIONS%"})
    write_session(projects, "mine", [prompt("work", at(9))])
    woke, woken = _pass(_settings(), handled, None, monkeypatch)
    wake = woken[0][1]["wake"]
    assert wake.model == "sonnet"
    command = daemon.runtime_command(_settings(), "P", wake)
    assert command[command.index("--model") + 1] == "sonnet"
    # Before --allowedTools, which takes every word after it.
    assert command.index("--model") < command.index("--allowedTools")
    forum_wake = daemon.Wake("", 10, 1, "Topic", "you were mentioned")
    assert "--model" not in daemon.runtime_command(_settings(), "P", forum_wake)
