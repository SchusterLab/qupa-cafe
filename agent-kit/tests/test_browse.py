"""The wake browser: what a key does, what is drawn, and how a wake is reopened."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
    assert b.frame(100, 30)[-1].strip() == "↑↓ to scroll · Enter to open in Claude Code · r to wake again · t to tune replies · Esc to go back"
    # Enter in a wake opens it in Claude Code.
    assert b.handle("enter") == "ask"
    b.handle("esc")
    assert not b.viewing
    # Escape from the list itself is leaving.
    assert b.handle("esc") == "quit"


def test_the_chosen_wake_is_marked_and_the_keys_are_shown():
    b = _browser(browse.Wake(_row(1), SAID), browse.Wake(_row(2), SAID))
    b.handle("down")
    frame = b.frame(120, 20)
    chosen = [line for line in frame if "❯" in line]
    assert len(chosen) == 1 and "Topic 2" in chosen[0]
    assert frame[-1].strip() == (
        "↑↓ to select · Enter to read · r to wake again · t to tune replies · Esc to quit"
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
    # As wide as the tray icon opens it, where the threshold's line fits.
    frame = b.frame(120, 20)
    rows = [line for line in frame if "Topic" in line]
    assert "0.83" in rows[0] and "0.8" not in rows[1].split("Topic")[0]
    assert any("Relevance threshold 0.72" in line for line in frame)
    assert any("(+ raises, - lowers)" in line for line in frame) and "+" not in frame[-1]


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
    for key in ("o", "q", "l", "u"):
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
    command = browse.tune_command(browse.subject_of_wake(browse.Wake(_row(7), None), "/agent"), "claude", "the prompt")
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


def test_the_hint_follows_whether_the_session_is_attached(monkeypatch, capsys):
    """Attached, what the researcher types goes into the running wake, so they
    are told; resumed, they are told how to come back to the list."""
    monkeypatch.setattr(browse.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(browse.subprocess, "call", lambda command, cwd=None: 0)
    monkeypatch.setattr(browse, "read_key", lambda timeout=None: "enter")

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
    frame = b.frame(120, 20)
    assert "wakes 1-" in frame[-2] and "of 100" in frame[-2]
    b.handle("end")
    frame = b.frame(120, 20)
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
    frame = _browser(browse.Wake(_row(1), SAID)).frame(120, 20)
    assert "wakes 1-" not in frame[-2]


def test_transcripts_are_read_only_for_the_wakes_drawn(monkeypatch):
    read = []
    monkeypatch.setattr(wakes, "transcript_for", lambda row: read.append(row.topic_id) or SAID)
    b = _browser(*(browse.Wake(_row(i)) for i in range(1, 101)))
    b.frame(100, 20)
    assert 0 < len(read) < 100 and read[0] == 1
    b.handle("end")
    b.frame(100, 20)
    assert 100 in read and 50 not in read


def test_m_and_f_step_the_model_and_effort_wakes_run_on(tmp_path):
    """Written to env, where the daemon reads them again at the next wake."""
    from qupa_cafe import models

    b = _browser(browse.Wake(_row(1), SAID))
    assert any("Runs on your Claude Code default model, its default effort" in line for line in b.frame(160, 20))
    assert b.handle("m") == "model" and b.handle("f") == "effort"
    assert browse.change_model(models.WAKE, "model") == "Forum wakes now run on fable, its default effort, from the next one."
    browse.change_model(models.WAKE, "effort")
    assert models.read(models.WAKE) == models.Choice("fable", "low")
    assert any("Runs on fable, effort low" in line for line in b.frame(160, 20))
    # Reading a wake, they are not the browser's to take.
    b.handle("enter")
    assert b.handle("m") is None


def test_a_steps_what_the_agent_does_with_a_preprint():
    """Written to env, where the daemon reads it again at its next poll."""
    from qupa_cafe import preprints

    b = _browser(browse.Wake(_row(1), SAID))
    assert any("Preprints are public: your agent replies in the preprint's topic" in line for line in b.frame(200, 20))
    assert b.handle("a") == "arxiv"
    said = browse.change_arxiv()
    assert said.startswith("Preprints are private: your agent replies in your private arXiv subcategory")
    assert preprints.read() == preprints.PRIVATE
    # Each fits a line of a 120-column window, the box's borders and all.
    assert len(said) <= 116
    framed = [line for line in b.frame(120, 20) if "Preprints are private" in line]
    assert len(framed) == 1 and "(a to make them off)" in framed[0]
    assert browse.change_arxiv().startswith("Preprints are off") and browse.change_arxiv().startswith("Preprints are public")
    b.handle("enter")
    assert b.handle("a") is None


@pytest.mark.skipif(sys.platform == "win32", reason="the console is read through msvcrt there")
@pytest.mark.parametrize("typed, key", [(b"\x1b[A", "up"), (b"\x1b[B", "down"), (b"\x1b[6~", "pgdn"), (b"\x1b", "esc"), (b"\xc3\xa9", "\u00e9")])
def test_a_key_is_read_whole_from_the_terminal(monkeypatch, typed, key):
    """An arrow key arrives as several bytes at once, which must not read as
    Escape followed by nothing."""
    import os

    reading, writing = os.pipe()
    os.write(writing, typed)
    with os.fdopen(reading, "r") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        assert browse.read_key() == key
    os.close(writing)


def _parent_row(**overrides):
    fields = dict(started="2026-10-12 06:15:30", finished="2026-10-12 06:15:39", status="finished", session="p-1",
                  agent_dir="/a", topic_id=None, post_number=None, title="parent session", reason=daemon.PARENT_REASON)
    fields.update(overrides)
    return wakes.Row(**fields)


def test_the_parent_stands_out_and_opens_as_a_copy():
    """A turn typed into the parent itself would reach every later wake."""
    row = _parent_row()
    assert wakes.outcome(row, None) == ("parent", "accent") and browse.row_mark("accent") == "◈"
    command, attached = browse.ask_command(row, "claude")
    assert "--fork-session" in command and not attached
    # Named so that the diary leaves it out, as it does the parent.
    assert command[command.index("--name") + 1].startswith(daemon.PARENT_NAME)
    wake = _parent_row(reason="you were mentioned", topic_id=5)
    assert "--fork-session" not in browse.ask_command(wake, "claude")[0]


def test_the_parent_is_in_the_wake_browser_alone(tmp_path):
    """Diary wakes run afresh, so the parent is the forum wakes'."""
    state = str(tmp_path / "state.sqlite3")
    journal = daemon.Journal(state)
    journal.end(journal.begin(daemon.Wake("/a", None, None, "parent session", daemon.PARENT_REASON, "p-1")), True)
    assert [r.session for r in wakes.recent(state, None, wakes.FORUM)] == ["p-1"]
    assert wakes.recent(state, None, wakes.DIARY) == []


def test_a_refresh_keeps_the_selected_wake_and_rereads_only_what_may_have_changed():
    ended = browse.Wake(_parent_row(session="a", reason="x", topic_id=1))
    running = browse.Wake(_parent_row(session="b", reason="x", topic_id=2, status="running", finished=None))
    browser = browse.Browser([running, ended], wakes.Status(None, 60), index=1)
    rows = [_parent_row(session="c", reason="x", topic_id=3), running.row, ended.row]
    items = browse.reloaded(browser.items, rows)
    assert items[2] is ended and items[1] is not running
    browse.refresh(browser, items, wakes.Status(None, 60))
    assert browser.selected.row.session == "a"


def test_no_key_within_the_timeout_reads_as_none(monkeypatch):
    if sys.platform == "win32":
        import msvcrt

        monkeypatch.setattr(msvcrt, "kbhit", lambda: False)
        assert browse.read_key(0.1) == ""
    else:
        import os

        reading, writing = os.pipe()
        with os.fdopen(reading, "r") as stdin:
            monkeypatch.setattr(sys, "stdin", stdin)
            assert browse.read_key(0.1) == ""
        os.close(writing)



# ------------------------------------------------------------- waking again


def test_r_asks_for_the_chosen_wake_s_post_once(tmp_path):
    settings = Settings(state_path=str(tmp_path / "state.sqlite3"))
    b = _browser(browse.Wake(_row(7, score=0.81), SAID))
    assert b.handle("r") == "rewake"
    assert "woken again for post 1 of topic 7 at the daemon's next poll" in browse.rewake(settings, b.selected)
    assert "already waiting" in browse.rewake(settings, b.selected)
    assert daemon.rewakes(settings.state_path) == [daemon.Rewake(7, 1, "you were mentioned", 0.81)]
    # And from the wake itself.
    b.handle("enter")
    assert b.handle("r") == "rewake"


def test_r_has_no_post_to_wake_for_in_the_parent_or_an_empty_list(tmp_path):
    settings = Settings(state_path=str(tmp_path / "state.sqlite3"))
    parent = wakes.Row("2026-10-11 00:01:00", None, "finished", "p", "/agent", None, None, "parent",
                       daemon.PARENT_REASON)
    assert "parent session" in browse.rewake(settings, browse.Wake(parent, SAID))
    assert daemon.rewakes(settings.state_path) == []
    assert _browser().handle("r") is None


def test_a_wake_asked_for_again_names_the_reason_it_first_woke_once():
    first = daemon.Rewake(7, 1, "you were mentioned")
    assert first.said == f"{daemon.REWOKEN}, which first woke you because you were mentioned"
    assert daemon.Rewake(7, 1, first.said).said == first.said


# ---------------------------------------------------------- fitting the screen


def test_a_line_too_wide_is_folded_at_a_space_in_its_colours_and_indent():
    wakes.set_colour(True)
    try:
        line = "    " + wakes.paint("alpha beta gamma delta epsilon", "meta") + " zeta"
        folded = browse.fold(line, 20)
        assert [browse.visible(piece) for piece in folded] == ["    alpha beta gamma", "    delta epsilon", "    zeta"]
        # Each piece ends in no colour, and one broken inside a colour takes it up again.
        assert all(piece.endswith(browse.RESET) for piece in folded[:2])
        assert folded[1].lstrip(" ").startswith("\x1b[")
        # A wide character counts two, and a word longer than the line is cut.
        assert all(wakes.columns(browse.visible(p)) <= 6 for p in browse.fold("日本語のテキスト", 6))
        assert browse.fold("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
        # A line that begins with a mark goes on under its text.
        assert [browse.visible(p) for p in browse.fold("● The diary is public", 12)] == ["● The diary", "  is public"]
    finally:
        wakes.set_colour(False)


def test_no_frame_is_wider_or_taller_than_the_screen():
    """A line the terminal wraps, or one line too many, scrolls the top of the
    screen out of view."""
    long = "woken because " + "a reason that goes on and on " * 8
    transcript = wakes.Transcript("p", [long] * 50, [])
    for cols, rows in ((61, 12), (80, 24), (100, 30)):
        b = _browser(*(browse.Wake(_row(i), transcript) for i in range(1, 40)), floor=0.72)
        b.message = "A message for the list, long enough to need a second line on any screen narrower than this one."
        frames = [b.frame(cols, rows)]
        b.handle("enter", rows)
        frames.append(b.frame(cols, rows))
        b.handle("end", rows)
        frames.append(b.frame(cols, rows))
        for frame in frames:
            assert len(frame) == rows
            assert max(wakes.columns(browse.visible(line)) for line in frame) <= cols


def test_the_end_of_a_wake_is_reached_in_the_lines_it_is_drawn_in():
    transcript = wakes.Transcript("p", ["word " * 60] * 10 + ["the last line"], [])
    b = _browser(browse.Wake(_row(1), transcript))
    b.handle("enter")
    b.frame(80, 20)
    b.handle("end", 20)
    assert any("the last line" in line for line in b.frame(80, 20))


def test_elsewhere_than_windows_the_width_is_the_xterm_window_operation(monkeypatch, capsys):
    monkeypatch.setattr(browse.os, "name", "posix")
    browse.set_width(120)
    assert capsys.readouterr().out == "\x1b[8;;120t"


def test_a_session_started_on_a_prompt_waits_for_the_forum_s_tools(monkeypatch):
    """Claude Code would send the prompt before its MCP servers are up."""
    monkeypatch.setenv("MCP_CONNECTION_NONBLOCKING", "1")
    env = browse.prompted_env()
    assert env["MCP_CONNECTION_NONBLOCKING"] == "0" and int(env["MCP_CONNECT_TIMEOUT_MS"]) >= 30000
    assert env["PATH"] == os.environ["PATH"]


def test_claude_code_opens_only_after_a_key_and_esc_goes_back(monkeypatch, capsys):
    """Claude Code clears the screen as it starts, so what is printed before it
    waits for a key."""
    monkeypatch.setattr(browse.shutil, "which", lambda name: "claude")
    monkeypatch.setattr(browse, "ask_command", lambda row, claude: (["claude", "--resume", "s-1"], False))
    ran = []
    monkeypatch.setattr(browse.subprocess, "call", lambda command, **kw: ran.append(command) or 0)
    if os.name != "nt":
        monkeypatch.setattr("termios.tcgetattr", lambda fd: None)
        monkeypatch.setattr("termios.tcsetattr", lambda fd, when, mode: None)
        monkeypatch.setattr("tty.setcbreak", lambda fd: None)

    monkeypatch.setattr(browse, "read_key", lambda timeout=None: "esc")
    assert browse.ask(_row(5)) == "" and ran == []
    assert "Press any key to open Claude Code" in capsys.readouterr().out

    monkeypatch.setattr(browse, "read_key", lambda timeout=None: "x")
    browse.ask(_row(5))
    assert ran == [["claude", "--resume", "s-1"]]


def test_the_list_stays_put_while_the_chosen_wake_is_on_screen():
    """It moves only as far as it takes to show the chosen wake."""
    b = _browser(*[browse.Wake(_row(i), SAID) for i in range(1, 41)])

    def shown() -> list[int]:
        return [int(n) for line in b.frame(120, 24) for n in re.findall(r"\(topic (\d+)\)", browse.visible(line))]

    first = shown()
    for _ in range(len(first) + 4):
        b.handle("down")
    scrolled = shown()
    assert scrolled[-1] == b.items[b.index].row.topic_id and scrolled[0] != first[0]

    # Up within the screen leaves every row where it was.
    for _ in range(len(scrolled) - 1):
        b.handle("up")
        assert shown() == scrolled
    # Up past the top moves the list by one row.
    b.handle("up")
    assert shown()[0] == b.items[b.index].row.topic_id and shown()[1:] == scrolled[:-1]
