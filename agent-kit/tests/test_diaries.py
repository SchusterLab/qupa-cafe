"""The diary browser and the diary's settings in env."""

from __future__ import annotations

from datetime import date

from conftest import wording
from qupa_cafe import browse, diaries, diary, models, wakes
from qupa_cafe.daemon import TUNE_NAME, Journal, Settings, Wake


def _row(title="Diary of a-agent, 2026-10-10", session="", topic_id=None, reason=f"{diary.REASON} 2026-10-10"):
    return wakes.Row(started="2026-10-11 00:01:00", finished="2026-10-11 00:03:00", status="finished",
                     session=session, agent_dir="/agent", topic_id=topic_id, post_number=None,
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


def test_the_diary_s_wakes_are_the_diary_browser_s_and_no_others_are(tmp_path):
    """And the wake browser shows the rest, counting only those against -n."""
    path = str(tmp_path / "state.sqlite3")
    journal = Journal(path)
    for n in range(3):
        journal.begin(Wake("/agent", 10 + n, 1, f"Topic {n}", "you were mentioned"))
        journal.begin(Wake("/agent", None, None, f"Diary of a-agent, 2026-10-1{n}",
                           f"{diary.REASON} 2026-10-1{n}, from 2 session(s)"))
    assert wakes.is_diary(_row()) and not wakes.is_diary(_row(topic_id=5, reason="you were mentioned"))
    assert [r.title for r in wakes.recent(path, 2, wakes.FORUM)] == ["Topic 1", "Topic 2"]
    assert [r.title for r in wakes.recent(path, None, wakes.DIARY)] == [f"Diary of a-agent, 2026-10-1{n}" for n in range(3)]
    assert len(wakes.recent(path, None)) == 6
    settings = Settings(state_path=path)
    assert [w.row.title for w in browse.load(settings, None)] == ["Topic 2", "Topic 1", "Topic 0"]
    assert [w.row.title for w in diaries.load(settings)][0] == "Diary of a-agent, 2026-10-12"


def test_the_list_says_whether_the_diary_is_kept_where_and_when():
    wakes.set_colour(False)
    lines = lambda b: [line.strip("│ ") for line in b.frame(160, 20)]
    off = lines(_browser())
    assert "○ The diary is off  (d to turn it on)" in off and "Posted at midnight  (+ to make it later, - earlier)" in off
    public = lines(_browser([browse.Wake(_row(), None)], diary.PUBLIC, 18))
    # The mode is followed by the key that changes it, and the hour by the
    # keys that move it, as the threshold is.
    assert "● The diary is public, in your agent's topic in Diary  (d to make it private)" in public
    assert "Posted at 18:00  (+ to make it later, - earlier)" in public
    assert any("Diary of a-agent, 2026-10-10" in line for line in public)
    assert not any("d to make it private" in line for line in public[-3:])
    private = lines(_browser(mode=diary.PRIVATE))
    assert "● The diary is private, a topic a day in your private diary  (d to turn it off)" in private
    assert "Posted at midnight  (+ to make it later, - earlier)" in private


def test_d_steps_from_off_to_public_to_private_and_back_writing_env():
    b = _browser()
    assert b.handle("d") == "mode"
    assert diaries.next_mode(b) == "The diary is public, posted in the Diary category, which every member reads."
    assert diary.choice(diary.Choice()).mode == diary.PUBLIC
    # Whether the private diary is there yet or not: the forum host makes it
    # when the first private entry is due.
    assert diaries.next_mode(b) == (
        "The diary is private, posted in your private diary category, which only you and your agent read.")
    assert diary.choice(diary.Choice()).mode == diary.PRIVATE
    assert "off" in diaries.next_mode(b) and diary.choice(diary.Choice(diary.PUBLIC)).mode == diary.OFF


def test_the_bracket_keys_move_the_hour_round_the_clock():
    b = _browser()
    assert b.handle("-") == "hour:-1" and b.handle("+") == "hour:1"
    assert b.handle("_") == "hour:-1" and b.handle("=") == "hour:1"
    assert "23:00" in diaries.move_hour(b, -1) and diary.choice(diary.Choice()).hour == 23
    assert "midnight" in diaries.move_hour(b, 1) and diary.choice(diary.Choice(hour=5)).hour == 0


def test_enter_reads_an_entry_and_e_asks_for_a_change_from_the_list_or_an_entry():
    b = _browser([browse.Wake(_row(), None)])
    # → reads too, and ← goes back.
    assert b.handle("right") is None and b.viewing
    assert b.handle("left") is None and not b.viewing
    assert b.handle("enter") is None and b.viewing
    assert b.handle("e") == "edit"
    assert b.handle("esc") is None and not b.viewing
    assert b.handle("e") == "edit" and b.handle("esc") == "quit"


# ---------------------------------------------------------------- the edit


def test_the_edit_prompt_carries_the_served_wording_and_qupa_md(monkeypatch, tmp_path):
    config = tmp_path / "home"
    monkeypatch.setenv("QUPA_HOME", str(config))
    config.mkdir()
    (config / "QUPA.md").write_text("Keep entries to three lines.", encoding="utf-8")
    wording(wake="w", diary="Diary of %DAY%. %SESSIONS%")

    prompt = diaries.edit_prompt("", diary.Choice(diary.PRIVATE), _row())
    assert "Diary of %DAY%. %SESSIONS%" in prompt
    assert "Keep entries to three lines." in prompt and str(config / "QUPA.md") in prompt
    assert "your private diary" in prompt and "Diary of a-agent, 2026-10-10" in prompt
    assert "Change nothing until they agree" in prompt


def test_the_edit_resumes_an_ended_entry_s_session_and_names_a_new_one_as_the_kit_s(monkeypatch):
    allowed = "--allowedTools"
    entry = browse.Wake(_row(session="2bc6e6bc-7d25-409f-8a53-442f462edd71"), wakes.Transcript("p", [], []))
    command = diaries.edit_command("claude", "P", entry)
    assert command[:4] == ["claude", "P", "--resume", entry.row.session] and allowed in command
    fresh = diaries.edit_command("claude", "P", None)
    assert fresh[:4] == ["claude", "P", "--name", f"{TUNE_NAME} diary"]


def test_each_entry_shows_what_its_wake_cost():
    usage = wakes.Usage(fresh=12, cache_written=50_400, cache_read=357_000, output=2_200, equivalent=147_512)
    written = browse.Wake(_row(session="s-1"), wakes.Transcript("p", [], ["reply"], usage))
    frame = _browser([written], diary.PUBLIC).frame(160, 20)
    assert any("147k tok" in line and "Diary of a-agent" in line for line in frame)


def test_m_and_f_step_the_diary_s_model_and_effort():
    b =_browser([browse.Wake(_row(), None)], diary.PUBLIC)
    assert any("Runs on your Claude Code default model, its default effort" in line for line in b.frame(160, 20))
    assert b.handle("m") == "model" and b.handle("f") == "effort"
    assert browse.change_model(models.DIARY, "model") == "Diary wakes now run on fable, its default effort, from the next one."
    # The forum's wakes are left as they were.
    assert models.read(models.WAKE) == models.Choice("", "")


# ------------------------------------------------------------ writing again


def test_r_asks_for_the_chosen_entry_s_day_once_and_only_with_the_diary_on(tmp_path):
    settings = Settings(state_path=str(tmp_path / "state.sqlite3"))
    b = _browser([browse.Wake(_row(), None)], mode=diary.PUBLIC)
    assert b.handle("r") == "rewrite"
    assert "again at the daemon's next poll" in diaries.rewrite(settings, b.chosen, b.selected)
    assert "already waiting" in diaries.rewrite(settings, b.chosen, b.selected)
    assert diary.rewrites(settings.state_path) == [date(2026, 10, 10)]

    off = diary.Choice(diary.OFF)
    assert "The diary is off" in diaries.rewrite(settings, off, browse.Wake(_row(reason=f"{diary.REASON} 2026-10-09"), None))
    assert diary.rewrites(settings.state_path) == [date(2026, 10, 10)]
    # Nothing to write again in an empty list.
    assert _browser().handle("r") is None


def test_the_diary_browser_fits_the_screen_whatever_its_lines():
    """A line the terminal wraps scrolls the top of the screen out of view."""
    long = "woken because it was time for your diary, " + "written again " * 20
    transcript = wakes.Transcript("p", [long] * 40, [])
    b = _browser([browse.Wake(_row(), transcript)] * 10, mode=diary.PUBLIC)
    b.message = "Your agent writes the diary of 2026-10-10 again at the daemon's next poll, as QUPA.md says now."
    for cols, rows in ((61, 12), (80, 24), (120, 40)):
        b.viewing = False
        frames = [b.frame(cols, rows)]
        b.handle("enter", rows)
        frames.append(b.frame(cols, rows))
        for frame in frames:
            assert len(frame) == rows
            assert max(wakes.columns(browse.visible(line)) for line in frame) <= cols
