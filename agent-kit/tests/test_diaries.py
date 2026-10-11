"""The diary browser and the diary's settings in env."""

from __future__ import annotations

from qupa_cafe import browse, diaries, diary, texts, wakes
from qupa_cafe.daemon import TUNE_NAME


def _row(title="Diary of a-agent, 2026-10-10", session="", topic_id=None, reason=f"{diary.REASON} 2026-10-10"):
    return wakes.Row(started="2026-10-11 00:01:00", finished="2026-10-11 00:03:00", status="finished",
                     runtime="claude", session=session, agent_dir="/agent", topic_id=topic_id, post_number=None,
                     title=title, reason=reason)


def _browser(items=(), on=False, category=""):
    return diaries.DiaryBrowser(list(items), wakes.Status(None, 60), on, category, "a-agent")


# ------------------------------------------------------------ the settings


def test_the_settings_are_read_from_env_as_it_stands_and_written_in_place(tmp_path):
    env = tmp_path / "env"
    assert diary.choice(True, "Lab", env) == (True, "Lab")
    env.write_text("# a comment\nQUPA_API_KEY=k\nQUPA_DIARY=0\n", encoding="utf-8")
    assert diary.choice(True, "Lab", env) == (False, "Lab")

    diary.set_choice(True, "Schuster lab", env)
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# a comment", "QUPA_API_KEY=k", "QUPA_DIARY=1", "QUPA_DIARY_CATEGORY=Schuster lab"]
    assert diary.choice(False, "", env) == (True, "Schuster lab")
    assert b"\r" not in env.read_bytes()


# ---------------------------------------------------------------- the list


def test_only_diary_wakes_are_entries():
    assert diaries.is_entry(_row())
    assert not diaries.is_entry(_row(topic_id=5, reason="you were mentioned"))


def test_the_list_says_whether_the_diary_is_on_and_where():
    wakes.set_colour(False)
    off = "\n".join(_browser().frame(100, 20))
    assert "The diary is off" in off and "d to turn it on" in off
    on = "\n".join(_browser([browse.Wake(_row(), None)], on=True, category="Schuster lab").frame(100, 20))
    assert "posted in Schuster lab" in on and "Diary of a-agent, 2026-10-10" in on and "d to turn it off" in on


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


def test_toggling_and_choosing_write_env(tmp_path):
    b = _browser()
    assert "on, posted in Diary" in diaries.toggle(b)
    assert diary.choice(False, "") == (True, "")
    assert "Schuster lab" in diaries.choose(b, "Schuster lab")
    assert diary.choice(False, "") == (True, "Schuster lab")
    diaries.choose(b, "Diary")
    assert diary.choice(False, "x") == (True, "")
    assert "off" in diaries.toggle(b) and diary.choice(True, "") == (False, "")


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
