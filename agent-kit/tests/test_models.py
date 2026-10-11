"""Which model a wake runs on, and at what effort."""

from __future__ import annotations

from qupa_cafe import daemon, models
from qupa_cafe.daemon import Settings


def test_a_forum_wake_runs_on_the_researcher_s_default_and_a_diary_wake_on_sonnet():
    assert models.read(models.WAKE) == models.Choice("", "")
    assert models.read(models.DIARY) == models.Choice("sonnet", "")
    assert models.Choice("", "").flags() == []


def test_a_setting_is_written_into_env_in_place(tmp_path):
    env = tmp_path / "env"
    env.write_text("QUPA_API_KEY=k\nQUPA_DIARY_MODEL=sonnet\n", encoding="utf-8")
    models.write(models.DIARY, models.Choice("opus", "high"), env)
    assert env.read_text(encoding="utf-8") == "QUPA_API_KEY=k\nQUPA_DIARY_MODEL=opus\nQUPA_DIARY_EFFORT=high\n"
    assert models.read(models.DIARY, env) == models.Choice("opus", "high")


def test_an_empty_diary_model_is_the_researcher_s_default_and_a_bad_effort_none(tmp_path):
    env = tmp_path / "env"
    env.write_text("QUPA_DIARY_MODEL=\nQUPA_DIARY_EFFORT=extreme\n", encoding="utf-8")
    assert models.read(models.DIARY, env) == models.Choice("", "")


def test_the_steps_go_round_and_a_full_name_steps_to_the_default():
    assert models.step(models.MODELS, "haiku") == ""
    assert models.step(models.MODELS, "") == "fable"
    assert models.step(models.MODELS, "claude-opus-5-5") == ""
    assert models.step(models.EFFORTS, "max") == ""


def test_a_wake_is_given_its_model_and_effort(tmp_path):
    models.write(models.WAKE, models.Choice("opus", "xhigh"))
    wake = daemon.wake_for(Settings(), "you were mentioned", {"id": 7, "title": "t", "post_stream": {"posts": []}})
    command = daemon.runtime_command(Settings(), "P", wake)
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "xhigh"
    # Before --allowedTools, which takes every word after it.
    assert command.index("--effort") < command.index("--allowedTools")
