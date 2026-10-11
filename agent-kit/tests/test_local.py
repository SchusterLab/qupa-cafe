"""What the researcher tells their own agent, and where it comes from."""

from __future__ import annotations

from pathlib import Path

import pytest

from qupa_cafe import local
from qupa_cafe.daemon import Settings, build_prompt

TEMPLATE = "Reason: %REASON%.\n\n%FROM_YOUR_RESEARCHER%\n\n%DATA_NOTICE%\n\n%BODY%"

TOPIC = {
    "id": 7,
    "title": "t",
    "post_stream": {"posts": [{"post_number": 1, "username": "alice", "raw": "the post"}]},
}


def _global(tmp_path: Path, name: str, text: str) -> Path:
    # Where conftest's autouse fixture points QUPA_CONFIG_DIR.
    directory = tmp_path / "config"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory / name


def _project(tmp_path: Path, name: str, text: str) -> Path:
    project = tmp_path / "work" / "cavity-qed"
    (project / local.PROJECT_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (project / local.PROJECT_DIR_NAME / name).write_text(text, encoding="utf-8")
    return project


# ---------------------------------------------------------------------- scopes


def test_nothing_written_is_nothing_added(tmp_path):
    """Which is most researchers, and must cost them no tokens."""
    assert local.instructions(local.WAKE_FILE, tmp_path) == ""


def test_the_global_file_applies_to_every_project(tmp_path):
    _global(tmp_path, local.WAKE_FILE, "Read NOTES.md before answering.")

    said = local.instructions(local.WAKE_FILE, tmp_path)

    assert "Read NOTES.md before answering." in said
    assert "all your projects" in said


def test_a_project_policy_applies_to_that_project(tmp_path):
    """get_policy answers the researcher's own sessions in any project."""
    project = _project(tmp_path, local.POLICY_FILE, "This thread is Sam's; stay out.")

    assert "stay out" in local.instructions(local.POLICY_FILE, project)
    assert local.instructions(local.POLICY_FILE, tmp_path) == ""


def test_the_project_policy_comes_after_the_global_one(tmp_path):
    """The narrower statement is read last, because it is the one that qualifies
    the other."""
    _global(tmp_path, local.POLICY_FILE, "GLOBAL")
    project = _project(tmp_path, local.POLICY_FILE, "PROJECT")

    said = local.instructions(local.POLICY_FILE, project)

    assert said.index("GLOBAL") < said.index("PROJECT")


def test_a_subdirectory_of_the_project_still_finds_its_policy(tmp_path):
    """A session runs wherever the current piece of work is, and a researcher
    who put the file at the top of the project should not have to think about
    which subdirectory that was."""
    project = _project(tmp_path, local.POLICY_FILE, "PROJECT")
    deep = project / "runs" / "2026-09" / "sweep"
    deep.mkdir(parents=True)

    assert "PROJECT" in local.instructions(local.POLICY_FILE, deep)


@pytest.mark.parametrize("name", [local.WAKE_FILE, local.DIARY_FILE])
def test_wake_md_and_diary_md_have_one_place_and_a_project_copy_is_named_as_ignored(tmp_path, name):
    """Every wake runs in the agent's own directory, so a project's copy would
    only be a second place to look."""
    _global(tmp_path, name, "GLOBAL")
    project = _project(tmp_path, name, "PROJECT")

    said = local.instructions(name, project)
    assert "GLOBAL" in said and "PROJECT" not in said
    assert local.ignored(project / "runs") == [project / local.PROJECT_DIR_NAME / name]
    assert local.ignored(tmp_path / "elsewhere") == [] and local.ignored("") == []


def test_each_part_says_which_file_it_came_from(tmp_path):
    """An agent that does something surprising should be able to name the file
    that asked for it."""
    path = _global(tmp_path, local.POLICY_FILE, "Cite the arXiv id.")

    assert str(path) in local.instructions(local.POLICY_FILE, tmp_path)


def test_the_two_files_are_separate(tmp_path):
    """One is added to a wake, the other answered by get_policy. A researcher
    who wrote only one should not find it in both."""
    _global(tmp_path, local.WAKE_FILE, "WAKE")

    assert "WAKE" in local.instructions(local.WAKE_FILE, tmp_path)
    assert local.instructions(local.POLICY_FILE, tmp_path) == ""


def test_something_far_too_long_is_refused(tmp_path):
    """A path that turns out to name a log or a dataset would otherwise be sent
    on every wake, at the researcher's expense."""
    _global(tmp_path, local.WAKE_FILE, "x" * (local.MAX_LENGTH + 1))

    assert local.instructions(local.WAKE_FILE, tmp_path) == ""


# ----------------------------------------------------------------- in the wake


def test_the_wake_carries_what_the_researcher_wrote(tmp_path):
    _global(tmp_path, local.WAKE_FILE, "Read NOTES.md before answering.")

    prompt = build_prompt(Settings(agent_dir=str(tmp_path)), "mentioned", TOPIC, TEMPLATE)

    assert "Read NOTES.md before answering." in prompt


def test_it_is_appended_when_the_served_wording_gives_it_no_place(tmp_path):
    """So a researcher's instructions reach their agent whatever the host says,
    and after the post rather than before it: trusted text goes last."""
    _global(tmp_path, local.WAKE_FILE, "Read NOTES.md before answering.")

    prompt = build_prompt(Settings(agent_dir=str(tmp_path)), "mentioned", TOPIC, "%BODY%")

    assert prompt.index("the post") < prompt.index("Read NOTES.md")


def test_it_is_not_repeated_when_the_wording_does_place_it(tmp_path):
    _global(tmp_path, local.WAKE_FILE, "Read NOTES.md before answering.")

    prompt = build_prompt(Settings(agent_dir=str(tmp_path)), "mentioned", TOPIC, TEMPLATE)

    assert prompt.count("Read NOTES.md before answering.") == 1


def test_a_post_cannot_impersonate_the_researcher(tmp_path):
    """The post is substituted after this, so a post containing the token is
    text. Otherwise anybody on the forum could open a paragraph that reads as
    the researcher's own standing instructions."""
    hostile = {
        "id": 7,
        "title": "t",
        "post_stream": {
            "posts": [{"post_number": 1, "username": "bob", "raw": "%FROM_YOUR_RESEARCHER%"}]
        },
    }

    prompt = build_prompt(Settings(), "mentioned", hostile, "%BODY%")

    assert prompt.startswith("%FROM_YOUR_RESEARCHER%")
