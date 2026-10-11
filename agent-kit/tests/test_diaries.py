"""The diary browser and the diary's settings in env."""

from __future__ import annotations

from qupa_cafe import browse, diaries, diary, texts, wakes
from qupa_cafe.daemon import TUNE_NAME


def _row(title="Diary of a-agent, 2026-10-10", session="", topic_id=None, reason=f"{diary.REASON} 2026-10-10"):
    return wakes.Row(started="2026-10-11 00:01:00", finished="2026-10-11 00:03:00", status="finished",
                     runtime="claude", session=session, agent_dir="/agent", topic_id=topic_id, post_number=None,
                     title=title, reason=reason)


def _browser(items=(), on=False, category="", hour=0):
    return diaries.DiaryBrowser(list(items), wakes.Status(None, 60), diary.Choice(on, category, hour), "a-agent")


# ------------------------------------------------------------ the settings


def test_the_settings_are_read_from_env_as_it_stands_and_written_in_place(tmp_path):
    env = tmp_path / "env"
    started = diary.Choice(True, "Lab", 7)
    assert diary.choice(started, env) == started
    env.write_text("# a comment\nQUPA_API_KEY=k\nQUPA_DIARY=0\nQUPA_DIARY_HOUR=25\n", encoding="utf-8")
    # An hour out of range keeps the one started with.
    assert diary.choice(started, env) == diary.Choice(False, "Lab", 7)

    diary.set_choice(diary.Choice(True, "Schuster lab", 18), env)
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# a comment", "QUPA_API_KEY=k", "QUPA_DIARY=1", "QUPA_DIARY_HOUR=18", "QUPA_DIARY_CATEGORY=Schuster lab"]
    assert diary.choice(diary.Choice(), env) == diary.Choice(True, "Schuster lab", 18)
    assert b"\r" not in env.read_bytes()


# ---------------------------------------------------------------- the list


def test_only_diary_wakes_are_entries():
    assert diaries.is_entry(_row())
    assert not diaries.is_entry(_row(topic_id=5, reason="you were mentioned"))


def test_the_list_says_whether_the_diary_is_on_and_where():
    wakes.set_colour(False)
    off = "\n".join(_browser().frame(140, 20))
    assert "The diary is off" in off and "d to turn it on" in off and "[ ] for the hour" in off
    on = "\n".join(_browser([browse.Wake(_row(), None)], on=True, category="Schuster lab", hour=18).frame(140, 20))
    assert "posted in Schuster lab at 18:00" in on and "Diary of a-agent, 2026-10-10" in on and "d to turn it off" in on
    assert "at midnight" in "\n".join(_browser(on=True).frame(140, 20))


def test_d_toggles_c_asks_where_and_a_number_answers(tmp_path):
    b = _browser()
    assert b.handle("d") == "toggle"
    assert b.handle("c") == "places"
    b.offer_places([{"id": 5, "name": "Diary"}, {"id": 6, "name": "Schuster lab", "read_restricted": True}])
    assert "2 Schuster lab (private)" in b.question[1] and "1 Diary (every member)" in b.question[1]
    assert b.handle("2") == "place:Schuster lab" and not b.choices
    # Any other key cancels.
    b.offer_places([{"id": 5, "name": "Diary"}])
    assert b.handle("x") is None and not b.choices


def test_no_place_to_post_says_so():
    b = _browser()
    b.offer_places([])
    assert not b.choices and "no Diary category" in b.message


def test_toggling_choosing_and_moving_the_hour_write_env(tmp_path):
    b = _browser()
    assert "on, posted in Diary, from the next midnight" in diaries.toggle(b)
    assert diary.choice(diary.Choice()) == diary.Choice(True, "", 0)
    assert "Schuster lab" in diaries.choose(b, "Schuster lab")
    assert diary.choice(diary.Choice()).category == "Schuster lab"
    diaries.choose(b, "Diary")
    assert diary.choice(diary.Choice(category="x")).category == ""
    # Round the clock either way.
    assert "23:00" in diaries.move_hour(b, -1) and diary.choice(diary.Choice()).hour == 23
    assert "midnight" in diaries.move_hour(b, 1) and diary.choice(diary.Choice(hour=5)).hour == 0
    assert "off" in diaries.toggle(b) and not diary.choice(diary.Choice(on=True)).on


def test_the_bracket_keys_move_the_hour():
    b = _browser()
    assert b.handle("[") == "hour:-1" and b.handle("]") == "hour:1"


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

    prompt = diaries.edit_prompt("", "Schuster lab", _row())
    assert "Diary of %DAY%. %SESSIONS%" in prompt
    assert "Keep entries to three lines." in prompt and str(config / "diary.md") in prompt
    assert "Schuster lab" in prompt and "Diary of a-agent, 2026-10-10" in prompt
    assert "Change nothing until they agree" in prompt


def test_the_edit_resumes_an_ended_entry_s_session_and_names_a_new_one_as_the_kit_s(monkeypatch):
    allowed = "--allowedTools"
    entry = browse.Wake(_row(session="2bc6e6bc-7d25-409f-8a53-442f462edd71"), wakes.Transcript("p", [], []))
    command = diaries.edit_command("claude", "P", entry)
    assert command[:4] == ["claude", "P", "--resume", entry.row.session] and allowed in command
    fresh = diaries.edit_command("claude", "P", None)
    assert fresh[:4] == ["claude", "P", "--name", f"{TUNE_NAME} diary"]
