"""The diary browser and the diary's settings in env."""

from __future__ import annotations

from qupa_cafe import browse, diaries, diary, texts, wakes
from qupa_cafe.daemon import TUNE_NAME


def _row(title="Diary of a-agent, 2026-10-10", session="", topic_id=None, reason=f"{diary.REASON} 2026-10-10"):
    return wakes.Row(started="2026-10-11 00:01:00", finished="2026-10-11 00:03:00", status="finished",
                     runtime="claude", session=session, agent_dir="/agent", topic_id=topic_id, post_number=None,
                     title=title, reason=reason)


def _browser(items=(), mode=diary.OFF, hour=0):
    return diaries.DiaryBrowser(list(items), wakes.Status(None, 60), diary.Choice(mode, hour), "a-agent")


# ------------------------------------------------------------ the settings


def test_the_settings_are_read_from_env_as_it_stands_and_written_in_place(tmp_path):
    env = tmp_path / "env"
    started = diary.Choice(diary.PUBLIC, 7)
    assert diary.choice(started, env) == started
    env.write_text("# a comment\nQUPA_API_KEY=k\nQUPA_DIARY=off\nQUPA_DIARY_HOUR=25\n", encoding="utf-8")
    # An hour out of range keeps the one started with.
    assert diary.choice(started, env) == diary.Choice(diary.OFF, 7)

    diary.set_choice(diary.Choice(diary.PRIVATE, 18), env)
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# a comment", "QUPA_API_KEY=k", "QUPA_DIARY=private", "QUPA_DIARY_HOUR=18"]
    assert diary.choice(diary.Choice(), env) == diary.Choice(diary.PRIVATE, 18)
    assert b"\r" not in env.read_bytes()


def test_a_mode_env_does_not_name_keeps_the_one_started_with(tmp_path):
    env = tmp_path / "env"
    env.write_text("QUPA_DIARY=sometimes\n", encoding="utf-8")
    assert diary.choice(diary.Choice(diary.PUBLIC), env).mode == diary.PUBLIC


# ---------------------------------------------------------------- the list


def test_only_diary_wakes_are_entries():
    assert diaries.is_entry(_row())
    assert not diaries.is_entry(_row(topic_id=5, reason="you were mentioned"))


def test_the_list_says_whether_the_diary_is_kept_where_and_when():
    wakes.set_colour(False)
    off = "\n".join(_browser().frame(160, 20))
    assert "The diary is off" in off and "d to make it public" in off and "[ ] for the hour" in off
    public = "\n".join(_browser([browse.Wake(_row(), None)], diary.PUBLIC, 18).frame(160, 20))
    assert "The diary is public, posted at 18:00 in your agent's topic in Diary" in public
    assert "Diary of a-agent, 2026-10-10" in public and "d to make it private" in public
    private = "\n".join(_browser(mode=diary.PRIVATE).frame(160, 20))
    assert "private, posted at midnight in a topic for each day in your private diary" in private
    assert "d to turn it off" in private


def test_d_steps_from_off_to_public_to_private_and_back_writing_env():
    b = _browser()
    assert b.handle("d") == "mode"
    assert "public, posted from the next midnight" in diaries.next_mode(b)
    assert diary.choice(diary.Choice()).mode == diary.PUBLIC
    # Whether the private diary is there yet or not: the forum host makes it
    # when the first private entry is due.
    assert "private" in diaries.next_mode(b) and diary.choice(diary.Choice()).mode == diary.PRIVATE
    assert "off" in diaries.next_mode(b) and diary.choice(diary.Choice(diary.PUBLIC)).mode == diary.OFF


def test_the_bracket_keys_move_the_hour_round_the_clock():
    b = _browser()
    assert b.handle("[") == "hour:-1" and b.handle("]") == "hour:1"
    assert "23:00" in diaries.move_hour(b, -1) and diary.choice(diary.Choice()).hour == 23
    assert "midnight" in diaries.move_hour(b, 1) and diary.choice(diary.Choice(hour=5)).hour == 0


def test_enter_reads_an_entry_and_e_asks_for_a_change_from_the_list_or_an_entry():
    b = _browser([browse.Wake(_row(), None)])
    assert b.handle("enter") is None and b.viewing
    assert b.handle("e") == "edit"
    assert b.handle("esc") is None and not b.viewing
    assert b.handle("e") == "edit" and b.handle("esc") == "quit"


# ---------------------------------------------------------------- the edit


def test_the_edit_prompt_carries_the_served_wording_and_diary_md(monkeypatch, tmp_path):
    config = tmp_path / "config"
    monkeypatch.setenv("QUPA_CONFIG_DIR", str(config))
    config.mkdir()
    (config / "diary.md").write_text("Keep entries to three lines.", encoding="utf-8")
    texts.save({"wake_prompt": "w", "diary_prompt": "Diary of %DAY%. %SESSIONS%"})

    prompt = diaries.edit_prompt("", diary.Choice(diary.PRIVATE), _row())
    assert "Diary of %DAY%. %SESSIONS%" in prompt
    assert "Keep entries to three lines." in prompt and str(config / "diary.md") in prompt
    assert "your private diary" in prompt and "Diary of a-agent, 2026-10-10" in prompt
    assert "Change nothing until they agree" in prompt


def test_the_edit_resumes_an_ended_entry_s_session_and_names_a_new_one_as_the_kit_s(monkeypatch):
    allowed = "--allowedTools"
    entry = browse.Wake(_row(session="2bc6e6bc-7d25-409f-8a53-442f462edd71"), wakes.Transcript("p", [], []))
    command = diaries.edit_command("claude", "P", entry)
    assert command[:4] == ["claude", "P", "--resume", entry.row.session] and allowed in command
    fresh = diaries.edit_command("claude", "P", None)
    assert fresh[:4] == ["claude", "P", "--name", f"{TUNE_NAME} diary"]
