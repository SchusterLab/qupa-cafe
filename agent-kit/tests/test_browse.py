"""The wake browser: what a key does, what is drawn, and how a wake is reopened."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from qupa_cafe import browse, wakes
from qupa_cafe.daemon import Settings, claim_lock


def _row(topic: int, session: str = "s-1", status: str = "finished", score: float | None = None) -> wakes.Row:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return wakes.Row(now, now, status, "claude", session, "/agent", topic, 1, f"Topic {topic}", "you were mentioned", score)


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
    assert b.frame(100, 30)[-1].strip() == "↑↓ to scroll · Enter to open in Claude Code · Esc to go back"
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
        "↑↓ to select · Enter to read · Esc to quit"
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


def test_a_finished_wake_opens_its_own_session_as_it_was():
    """Nothing added and nothing withheld: the researcher decides what to do."""
    command = browse.ask_command(_row(5, session="abc"), "claude")
    assert command == ["claude", "--resume", "abc"]


def test_a_background_wake_still_running_is_attached_to(monkeypatch):
    """The wake itself, its one writer, rather than a copy."""
    row = _row(5, session="2bc6e6bc-7d25-409f-8a53-442f462edd71")
    row.status = "running"
    row.started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    live = [{"id": "2bc6e6bc", "sessionId": row.session, "state": "working"}]
    monkeypatch.setattr(browse, "background_sessions", lambda claude: live)
    assert browse.ask_command(row, "claude") == ["claude", "attach", "2bc6e6bc"]
    live[0]["state"] = "done"
    assert browse.ask_command(row, "claude") == ["claude", "--resume", row.session]
    # One the journal left running past the timeout ended with the daemon.
    row.started = "2026-01-01 00:00:00"
    live[0]["state"] = "working"
    assert browse.ask_command(row, "claude") == ["claude", "--resume", row.session]


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


def test_transcripts_are_read_only_for_the_wakes_drawn(monkeypatch):
    read = []
    monkeypatch.setattr(wakes, "transcript_for", lambda row: read.append(row.topic_id) or SAID)
    b = _browser(*(browse.Wake(_row(i)) for i in range(1, 101)))
    b.frame(100, 20)
    assert 0 < len(read) < 100 and read[0] == 1
    b.handle("end")
    b.frame(100, 20)
    assert 100 in read and 50 not in read
