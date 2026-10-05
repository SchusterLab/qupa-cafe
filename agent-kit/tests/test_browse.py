"""The wake browser: what a key does, what is drawn, and how a wake is reopened."""

from __future__ import annotations

from datetime import datetime, timezone

from qupa_cafe import browse, tray, wakes
from qupa_cafe.daemon import Settings, claim_lock


def _row(topic: int, session: str = "s-1", status: str = "finished") -> wakes.Row:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return wakes.Row(now, now, status, "claude", session, "/agent", topic, 1, f"Topic {topic}", "you were mentioned")


def _browser(*items: browse.Wake) -> browse.Browser:
    return browse.Browser(list(items), wakes.Status(None, 60), "sunada-agent")


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
        "↑↓ to select · Enter to read · l for the log · t to quit the icon · Esc to quit"
    )
    assert "Qupa Cafe" in "\n".join(frame[:3])


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
    b = _browser(browse.Wake(_row(1), SAID))
    for key in ("o", "q", "a"):
        assert b.handle(key) is None
        assert not b.viewing


def test_a_reopened_wake_is_a_copy_without_the_forums_write_tools():
    command = browse.ask_command(_row(5, session="abc"), "claude")
    assert command[:4] == ["claude", "--resume", "abc", "--fork-session"]
    withheld = command[command.index("--disallowedTools") + 1 : command.index("--append-system-prompt")]
    assert set(withheld) == {f"mcp__qupa-cafe__{t}" for t in wakes.WRITE_TOOLS}
    # Reading stays.
    assert not any("read_attachment" in t or "get_policy" in t for t in withheld)


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
    imports qupa_cafe.wakes: two modules, two colour switches. Colour turned
    on in the first left the browser drawing in none."""
    import subprocess
    import sys
    from pathlib import Path

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


def test_the_list_opens_the_log_and_quits_the_icon():
    b = _browser(browse.Wake(_row(1), SAID))
    assert [b.handle(key) for key in ("l", "t")] == ["log", "quit-tray"]
    assert not b.viewing


def test_with_nothing_newer_published_u_is_neither_shown_nor_taken():
    b = _browser(browse.Wake(_row(1), SAID))
    assert b.handle("u") is None
    frame = b.frame(100, 20)
    assert "u to update" not in frame[-1]
    assert not any("is available" in line for line in frame)


def test_a_newer_kit_is_announced_with_both_versions_and_how_to_get_it():
    b = _browser(browse.Wake(_row(1), SAID))
    b.latest = "99.0.0"
    frame = b.frame(100, 20)
    notice = [line for line in frame if "is available" in line]
    assert len(notice) == 1
    assert "99.0.0" in notice[0] and f"You have {browse.__version__}" in notice[0]
    assert any("Press u to update" in line for line in frame)
    # Named in the notice, so not again on the bottom line.
    assert "u to update" not in frame[-1]
    assert b.handle("u") == "update"


def test_a_checkout_is_told_how_to_update_and_offered_no_u():
    b = _browser(browse.Wake(_row(1), SAID))
    b.latest, b.updatable = "99.0.0", False
    frame = b.frame(100, 20)
    assert any("update the checkout" in line for line in frame)
    assert "u to update" not in frame[-1]
    assert b.handle("u") is None


def test_reading_a_wake_takes_none_of_them():
    b = _browser(browse.Wake(_row(1), SAID))
    b.handle("enter")
    assert [b.handle(key) for key in ("l", "u", "t")] == [None, None, None]
    assert b.viewing


def test_with_no_icon_running_nothing_is_asked_to_quit(tmp_path):
    settings = Settings(state_path=str(tmp_path / "state.db"))
    assert browse.quit_tray(settings) == "No tray icon is running."
    assert not tray.quit_request(settings).exists()


def test_a_running_icon_is_asked_to_quit(tmp_path):
    """Asked through a file it looks for, rather than killed, so that Windows
    does not leave its picture by the clock."""
    settings = Settings(state_path=str(tmp_path / "state.db"))
    held = claim_lock(tray.lock_path(settings))
    try:
        assert "will quit" in browse.quit_tray(settings)
        assert tray.quit_request(settings).exists()
    finally:
        held.close()


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
