"""The wake browser: what a key does, what is drawn, and how a wake is reopened."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qupa_cafe import browse, daemon, wakes
from qupa_cafe.daemon import Settings, claim_lock


def _row(topic: int, session: str = "s-1", status: str = "finished", score: float | None = None) -> wakes.Row:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return wakes.Row(now, now, status, session, "/agent", topic, 1, f"Topic {topic}", "you were mentioned", score)


def _browser(*items: browse.Wake, floor: float | None = None) -> browse.Browser:
    status = wakes.Status(None, 60, floor, 0.60, 0.90) if floor is not None else wakes.Status(None, 60)
    return browse.Browser(list(items), status, "sunada-agent")


SAID = wakes.Transcript("You were woken.", ["⏺ get_policy()"], [])


def test_arrows_move_the_choice_and_stop_at_the_ends():
    b = _browser(browse.Wake(_row(1), SAID), browse.Wake(_row(2), SAID), browse.Wake(_row(3), SAID))
    b.handle("down"); b.handle("down"); b.handle("down")
    assert b.index == 2
    b.handle("up")
    assert b.index == 1
    b.handle("home")
    assert b.index == 0
    b.handle("up")
    assert b.index == 0


def test_enter_reads_the_wake_and_escape_goes_back():
    b = _browser(browse.Wake(_row(7), SAID))
    b.handle("enter")
    assert b.viewing
    assert any("You were woken." in line for line in b.frame(100, 30))
    assert b.frame(100, 30)[-1].strip() == "↑↓ to scroll · Enter to open in Claude Code · t to tune replies · Esc to go back"
    # Enter in a wake opens it in Claude Code.
    assert b.handle("enter") == "ask"
    b.handle("esc")
    assert not b.viewing
    # Escape from the list itself is leaving.
    assert b.handle("esc") == "quit"


def test_the_chosen_wake_is_marked_and_the_keys_are_shown():
    b = _browser(browse.Wake(_row(1), SAID), browse.Wake(_row(2), SAID))
    b.handle("down")
    frame = b.frame(100, 20)
    chosen = [line for line in frame if "❯" in line]
    assert len(chosen) == 1 and "Topic 2" in chosen[0]
    assert frame[-1].strip() == (
        "↑↓ to select · Enter to read · t to tune replies · n for near misses · Esc to quit"
    )
    assert "Qupa Cafe" in "\n".join(frame[:3])


def test_the_title_is_a_cup_and_the_box_stays_square():
    frame = _browser(browse.Wake(_row(1), SAID), floor=0.72).frame(100, 20)
    box = [browse.visible(line) for line in frame[:5]]
    assert box[1].startswith("│ ☕ Qupa Cafe")
    assert "✻" not in "".join(box)
    # The cup is two columns wide, and every line of the box ends in the same column.
    assert len({wakes.columns(line) for line in box}) == 1


def test_the_list_shows_each_wake_s_score_and_the_threshold():
    b = _browser(browse.Wake(_row(1, score=0.83), SAID), browse.Wake(_row(2), SAID), floor=0.72)
    frame = b.frame(100, 20)
    rows = [line for line in frame if "Topic" in line]
    assert "0.83" in rows[0] and "0.8" not in rows[1].split("Topic")[0]
    assert any("Relevance threshold 0.72" in line for line in frame)
    assert "+/- for the threshold" in frame[-1]


def test_plus_and_minus_ask_for_a_threshold_one_step_away_within_range():
    b = _browser(browse.Wake(_row(1), SAID), floor=0.72)
    assert b.handle("+") == "raise" and b.handle("=") == "raise"
    assert b.handle("-") == "lower" and b.handle("_") == "lower"
    assert b.nudged("raise") == 0.73
    assert b.nudged("lower") == 0.71
    b.state.floor = 0.90
    assert b.nudged("raise") is None
    b.state.floor = 0.60
    assert b.nudged("lower") is None


def test_with_no_threshold_known_plus_and_minus_do_nothing():
    b = _browser(browse.Wake(_row(1), SAID))
    assert b.handle("+") is None and b.handle("-") is None
    frame = b.frame(100, 20)
    assert "threshold" not in frame[-1]
    assert any("not known yet" in line for line in frame)


def test_a_threshold_the_host_accepts_is_shown(monkeypatch):
    b = _browser(browse.Wake(_row(1), SAID), floor=0.72)
    asked = []

    def accept(self, agent, floor):
        asked.append(floor)
        return {"similarity_floor": floor, "floor_min": 0.6, "floor_max": 0.9}

    monkeypatch.setattr(browse.Ranking, "set_floor", accept)
    message = browse.set_floor(Settings(api_username="sunada-agent"), b, "raise")

    assert asked == [0.73]
    assert b.state.floor == 0.73 and "0.73" in message


def test_a_threshold_the_host_refuses_leaves_it_as_it_was(monkeypatch):
    b = _browser(browse.Wake(_row(1), SAID), floor=0.72)

    def refuse(self, agent, floor):
        raise browse.RankingError("HTTP 403")

    monkeypatch.setattr(browse.Ranking, "set_floor", refuse)
    message = browse.set_floor(Settings(api_username="sunada-agent"), b, "lower")

    assert b.state.floor == 0.72 and "not changed" in message and "403" in message


def test_a_wake_without_a_transcript_cannot_be_asked():
    b = _browser(browse.Wake(_row(1, session=""), None))
    b.handle("enter")
    assert b.handle("enter") is None
    assert "no session" in b.message
    assert "Enter to open" not in b.frame(100, 30)[-1]
    b2 = _browser(browse.Wake(_row(2), SAID))
    b2.handle("enter")
    assert b2.handle("enter") == "ask"


def test_only_the_keys_shown_do_anything():
    """Opening the log and updating the kit are the tray icon's menu's, and the
    forum host tells the researcher of a newer kit, so the list takes no key
    for either and shows no notice."""
    b = _browser(browse.Wake(_row(1), SAID))
    for key in ("o", "q", "a", "l", "u"):
        assert b.handle(key) is None
        assert not b.viewing
    assert not any("is available" in line for line in b.frame(100, 20))


def test_a_finished_wake_opens_its_own_session_asking_before_each_tool():
    """Nothing added and nothing withheld: the researcher decides what to do.
    The wake ran in dontAsk, which a resumed session keeps and which would
    refuse every tool not approved beforehand, so Claude Code is told to ask."""
    command = browse.ask_command(_row(5, session="abc"), "claude")
    assert command == (["claude", "--resume", "abc", "--permission-mode", "manual"], False)


def test_a_background_wake_still_running_is_attached_to(monkeypatch):
    """The wake itself, its one writer, rather than a copy."""
    row = _row(5, session="2bc6e6bc-7d25-409f-8a53-442f462edd71")
    row.status = "running"
    row.started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    live = [{"id": "2bc6e6bc", "sessionId": row.session, "state": "working"}]
    monkeypatch.setattr(browse, "background_sessions", lambda claude: live)
    assert browse.ask_command(row, "claude") == (["claude", "attach", "2bc6e6bc"], True)
    live[0]["state"] = "done"
    assert browse.ask_command(row, "claude") == (["claude", "--resume", row.session, *browse.RESUME_MODE], False)
    # One the journal left running past the timeout ended with the daemon.
    row.started = "2026-01-01 00:00:00"
    live[0]["state"] = "working"
    assert browse.ask_command(row, "claude") == (["claude", "--resume", row.session, *browse.RESUME_MODE], False)


def test_t_then_r_or_i_tunes_from_the_list_or_a_wake_and_anything_else_cancels():
    b = _browser(browse.Wake(_row(7), SAID))
    assert b.handle("t") is None
    asked = b.frame(160, 20)[-2]
    about, keys = browse.TUNE_QUESTION
    assert b.tuning and browse.visible(asked).strip() == f"{about} {keys}"
    # What it is about stands out from the keys that answer it.
    assert wakes.paint(about, "accent_bold") in asked and "cancel" not in asked
    assert b.handle("r") == "tune-reply"
    assert not b.tuning

    b.handle("enter")
    b.handle("t")
    assert b.handle("i") == "tune-ignore"
    b.handle("t")
    assert b.handle("h") == "tune-how"

    b.handle("t")
    assert b.handle("esc") is None
    # The cancelling key does nothing else: Esc did not leave the wake.
    assert b.viewing and not b.tuning


def test_a_wake_without_a_topic_cannot_be_tuned():
    b = _browser(browse.Wake(_row(None), SAID))
    assert b.handle("t") is None
    assert not b.tuning and "no topic" in b.message


def test_tuning_resumes_an_ended_wake_s_session_and_starts_a_new_one_otherwise(monkeypatch):
    """The wake's own session holds what the agent made of the post."""
    monkeypatch.setattr(browse, "background_sessions", lambda claude: [])
    allowed = ["--allowedTools", ",".join(daemon.CLAUDE_ALLOWED_TOOLS)]
    row = _row(5, session="2bc6e6bc-7d25-409f-8a53-442f462edd71")
    ended = browse.subject_of_wake(browse.Wake(row, SAID), "/agent")
    assert browse.tune_command(ended, "claude", "P") == ["claude", "P", "--resume", row.session, *browse.RESUME_MODE, *allowed]
    # No transcript, nothing to resume; the new session is named as the kit's.
    named = ["--name", f"{daemon.TUNE_NAME} topic 5 post 1"]
    assert browse.tune_command(browse.subject_of_wake(browse.Wake(row, None), "/agent"), "claude", "P") == ["claude", "P", *named, *allowed]
    # Resuming a running wake would write into it.
    row.status = "running"
    assert browse.tune_command(browse.subject_of_wake(browse.Wake(row, SAID), "/agent"), "claude", "P") == ["claude", "P", *named, *allowed]


def test_tuning_approves_the_forum_tools_and_nothing_else():
    """Reading the forum and editing the agent's post ask nothing; a file edit
    still asks, and the prompt is never read as a tool's name."""
    command = browse.tune_command(browse.subject_of_miss(_miss(7), "/agent"), "claude", "the prompt")
    tools = command[command.index("--allowedTools") + 1].split(",")
    assert "mcp__discourse" in tools and "mcp__qupa-cafe__edit_post" in tools
    assert not [tool for tool in tools if not tool.startswith("mcp__")]
    assert command[1] == "the prompt" and command[-2] == "--allowedTools"


def test_the_tuning_prompt_names_the_wake_the_wish_and_all_three_places(monkeypatch, tmp_path):
    monkeypatch.setenv("QUPA_HOME", str(tmp_path / "home"))
    agent_dir = tmp_path / "project" / "agent"
    agent_dir.mkdir(parents=True)
    (tmp_path / "project" / "QUPA.md").write_text("Stay out of Sam's threads.", encoding="utf-8")
    row = _row(42, score=0.81)
    row.agent_dir = str(agent_dir)
    subject = browse.subject_of_wake(browse.Wake(row, SAID), "/elsewhere")

    prompt = browse.tune_prompt(subject, "ignore")

    assert "leave alone posts like the one on topic 42" in prompt
    assert "You were woken for it because you were mentioned, with a relevance score of 0.81." in prompt
    assert 'shows that wake as "wrote nothing"' in prompt
    assert browse.INTERESTS_TOPIC_TITLE in prompt and "edit_post" in prompt
    # Every project's file, then the one of the project the wake runs in.
    everywhere = str(tmp_path / "home" / "QUPA.md")
    here = str(tmp_path / "project" / "QUPA.md")
    assert everywhere in prompt and here in prompt and prompt.index(everywhere) < prompt.index(here)
    assert "override the policy's defaults, never its rules" in prompt
    assert "Change nothing until they agree." in prompt
    assert "reply to posts like" in browse.tune_prompt(subject, "reply")


def test_the_tuning_prompt_carries_the_file_as_it_stands(monkeypatch, tmp_path):
    """Read by the browser, so the agent need not go looking for it."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "QUPA.md").write_text("Leave arXiv preprints on finance alone.\n", encoding="utf-8")
    monkeypatch.setenv("QUPA_HOME", str(home))

    prompt = browse.tune_prompt(browse.subject_of_wake(browse.Wake(_row(42), SAID), str(tmp_path)), "how")

    assert "Leave arXiv preprints on finance alone." in prompt
    assert "no need to read it" in prompt


def test_the_tuning_prompt_says_a_missing_file_may_be_created(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("QUPA_HOME", str(home))

    prompt = browse.tune_prompt(browse.subject_of_wake(browse.Wake(_row(42), SAID), str(tmp_path)), "how")

    assert f"{home / 'QUPA.md'}, for all your projects: does not exist yet" in prompt
    assert "may be created" in prompt


def test_tuning_how_the_agent_replies_asks_what_to_change_and_points_at_the_file():
    """The interests post decides only what wakes the agent, not what it writes."""
    prompt = browse.tune_prompt(browse.subject_of_wake(browse.Wake(_row(42), SAID), "/agent"), "how")
    assert "wants to change how you reply to posts like the one on topic 42" in prompt
    assert "Ask them first what to change" in prompt
    assert "how you reply is set by QUPA.md alone" in prompt
    assert "Change nothing until they agree." in prompt


def _miss(topic: int, reason: str = "below-floor", similarity: float | None = 0.61, category: str = "Research"):
    return wakes.NearMiss(topic + 100, topic, 1, f"Missed {topic}", category, "2026-10-10T10:00:00+00:00",
                          similarity, 0.70, reason)


def test_a_post_its_exclusions_held_back_is_marked_and_explained_as_such():
    b = _browser(browse.Wake(_row(1), SAID))
    b.missed = [_miss(9, "excluded", 0.80)]
    b.handle("n")
    assert any("excluded" in line and "Missed 9" in line for line in b.frame(120, 20))

    prompt = browse.tune_prompt(browse.subject_of_miss(b.missed[0], "/agent"), "wake")
    assert "it cleared your similarity floor (it scored 0.80)" in prompt
    assert 'matched the "Not interested in:" line of your interests post at least as well as your interests' in prompt


def test_n_shows_the_posts_that_did_not_wake_the_agent_and_t_offers_to_wake_it_for_them():
    b = _browser(browse.Wake(_row(1), SAID))
    b.missed = [_miss(7), _miss(8, "category-skipped", None, "Meta")]
    b.agent_dir = "/agent"

    b.handle("n")
    frame = b.frame(120, 20)
    assert any("under 0.70" in line and "Missed 7" in line for line in frame)
    assert any("skipped" in line and "Missed 8" in line for line in frame)
    assert frame[-1].strip() == "↑↓ to select · t to tune waking · n for wakes · Esc to quit"

    b.handle("down")
    b.handle("t")
    assert b.question == browse.WAKE_QUESTION and browse.WAKE_QUESTION[0] in browse.visible(b.frame(160, 20)[-2])
    # Reply, ignore and how are about wakes, which this was not.
    assert b.handle("r") is None
    b.handle("t")
    assert b.handle("w") == "tune-wake"
    assert (b.subject.topic_id, b.subject.woke) == (8, False)
    assert "Meta" in b.subject.why and "Categories to skip" in b.subject.why

    b.handle("n")
    assert not b.showing_missed and b.selected.row.topic_id == 1


def test_waking_for_posts_like_one_points_at_the_interests_post_and_the_floor_only():
    subject = browse.subject_of_miss(_miss(7), "/agent")
    prompt = browse.tune_prompt(subject, "wake")
    assert "wants you woken for posts like the one on topic 7" in prompt
    assert "It did not wake you: it scored 0.61 against your similarity floor of 0.70." in prompt
    assert browse.INTERESTS_TOPIC_TITLE in prompt and "similarity floor, 0.70," in prompt
    assert "Only your researcher sets it, with + and - in the wake browser" in prompt
    assert "QUPA.md cannot make a post wake you" in prompt
    assert "Change nothing until they agree." in prompt


def test_the_hint_follows_whether_the_session_is_attached(monkeypatch, capsys):
    """Attached, what the researcher types goes into the running wake, so they
    are told; resumed, they are told how to come back to the list."""
    monkeypatch.setattr(browse.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(browse.subprocess, "call", lambda command, cwd=None: 0)

    monkeypatch.setattr(browse, "ask_command", lambda row, claude: (["claude", "attach", "2bc6e6bc"], True))
    assert browse.ask(_row(5)) == ""
    assert "still running" in capsys.readouterr().out

    monkeypatch.setattr(browse, "ask_command", lambda row, claude: (["claude", "--resume", "s-1"], False))
    assert browse.ask(_row(5)) == ""
    out = capsys.readouterr().out
    assert "still running" not in out and "/exit" in out


def test_escape_sequences_are_named():
    assert browse.ESCAPES["[A"] == "up" and browse.ESCAPES["[B"] == "down"
    assert browse.WINDOWS_KEYS["H"] == "up" and browse.WINDOWS_KEYS["P"] == "down"
    assert browse.name_of("\r") == "enter" and browse.name_of("j") == "down"


def test_long_views_scroll_within_their_length():
    long = wakes.Transcript("p", [f"line {i}" for i in range(200)], [])
    b = _browser(browse.Wake(_row(1), long))
    b.handle("enter", height=24)
    b.handle("end", height=24)
    last = b.scroll
    b.handle("down", height=24)
    assert b.scroll == last > 0
    assert b.frame(100, 24)[-3].endswith("line 199")  # the last line of the wake, above the position and the keys


def test_run_as_a_module_the_browser_shares_the_colour_switch():
    """`python -m qupa_cafe.wakes` runs the file as __main__, and the browser
    imports qupa_cafe.wakes: two modules, and the browser has to read the
    colour switch the command line set, or it draws in none."""
    probe = (
        "import runpy, sys\n"
        "from qupa_cafe import browse\n"
        "browse.run = lambda settings, limit: print('colour' if browse.wakes._colour else 'none') or 0\n"
        "sys.stdin.isatty = sys.stdout.isatty = lambda: True\n"
        "sys.argv = ['wakes', '--colour', 'always']\n"
        "runpy.run_module('qupa_cafe.wakes', run_name='__main__')\n"
    )
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=root)
    assert result.stdout.strip() == "colour", result.stderr


def test_reading_a_wake_takes_none_of_them():
    b = _browser(browse.Wake(_row(1), SAID))
    b.handle("enter")
    assert [b.handle(key) for key in ("l", "u", "t")] == [None, None, None]
    assert b.viewing


def test_a_second_browser_is_noticed_and_not_opened(tmp_path, monkeypatch, capsys):
    """The tray asks already_open before opening a window; a browser started
    by hand meanwhile asks for itself, and brings the open one forward."""
    settings = Settings(state_path=str(tmp_path / "state.db"))
    assert browse.already_open(settings) is False

    held = claim_lock(browse.lock_path(settings))
    try:
        assert browse.already_open(settings) is True
        raised = []
        monkeypatch.setattr(browse, "bring_to_front", lambda: raised.append(1) or False)
        monkeypatch.setattr(browse, "Terminal", lambda: (_ for _ in ()).throw(AssertionError("drew a second browser")))
        assert browse.run(settings, 10) == 0
        assert raised == [1]
        assert browse.TITLE in capsys.readouterr().out
    finally:
        held.close()


def test_the_list_scrolls_past_a_screenful_and_says_where_it_is():
    b = _browser(*(browse.Wake(_row(i), SAID) for i in range(1, 101)))
    frame = b.frame(100, 20)
    assert "wakes 1-" in frame[-2] and "of 100" in frame[-2]
    b.handle("end")
    frame = b.frame(100, 20)
    chosen = [line for line in frame if "❯" in line]
    assert len(chosen) == 1 and "Topic 100" in chosen[0]
    assert frame[-2].strip().endswith("-100 of 100")


def test_page_keys_move_by_what_the_list_has_room_for():
    b = _browser(*(browse.Wake(_row(i), SAID) for i in range(1, 101)))
    frame = b.frame(100, 20)
    shown = sum(1 for line in frame if "Topic" in line)
    b.handle("pgdn")
    assert b.index == shown
    b.handle("pgup")
    assert b.index == 0


def test_a_list_that_fits_shows_no_position():
    frame = _browser(browse.Wake(_row(1), SAID)).frame(100, 20)
    assert "wakes 1-" not in frame[-2]


def _old_row(topic: int) -> wakes.Row:
    then = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    return wakes.Row(then, then, "finished", "s-1", "/agent", topic, 1, f"Topic {topic}", "you were mentioned")


def test_transcripts_are_read_only_for_the_wakes_drawn(monkeypatch):
    """And for those of the last 24 hours, which the box above the list adds up."""
    read = []
    monkeypatch.setattr(wakes, "transcript_for", lambda row: read.append(row.topic_id) or SAID)
    b = _browser(*(browse.Wake(_old_row(i)) for i in range(1, 101)))
    b.frame(100, 20)
    assert 0 < len(read) < 100 and read[0] == 1
    b.handle("end")
    b.frame(100, 20)
    assert 100 in read and 50 not in read


def test_the_box_adds_up_the_last_24_hours():
    usage = wakes.Usage(fresh=10, cache_written=1000, cache_read=20_000, output=500, equivalent=6_000.4)
    recent = [browse.Wake(_row(n), wakes.Transcript("p", [], [], usage)) for n in (1, 2)]
    old = browse.Wake(_old_row(3), wakes.Transcript("p", [], [], usage))
    line = browse.day_line([old, *recent])
    assert "12.0k input-token equivalents, 43.0k tokens, 2 wakes" in line
    assert any("last 24 hours" in row for row in _browser(*recent).frame(120, 20))
