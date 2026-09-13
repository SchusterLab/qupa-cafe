"""The installer: what it registers, and in which form."""

from __future__ import annotations

from pathlib import Path

import pytest

import qupa_cafe.install as install

from conftest import POLICY_TOPIC_ID


def test_a_console_script_is_found_in_a_conda_layout(monkeypatch, tmp_path):
    """``python.exe`` sits at the environment root there, not beside the scripts.

    Missing it means falling back to ``python -m``, which is not the same
    thing: ``-m`` puts the working directory on ``sys.path``, so an agent
    started inside a checkout of this repository would load that copy of the
    package rather than the installed one. A console script has no such
    ambiguity, which is the reason to prefer it.
    """
    env = tmp_path / "envs" / "default"
    (env / "Scripts").mkdir(parents=True)
    (env / "python.exe").write_text("")
    script = env / "Scripts" / "qupa-daemon.exe"
    script.write_text("")

    monkeypatch.setattr(install.sys, "executable", str(env / "python.exe"))

    assert install.console_script("qupa-daemon") == script


def test_a_console_script_is_found_beside_the_interpreter(monkeypatch, tmp_path):
    """Which is where a virtualenv puts it, on either platform."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").write_text("")
    script = bindir / "qupa-mcp"
    script.write_text("")

    monkeypatch.setattr(install.sys, "executable", str(bindir / "python"))

    assert install.console_script("qupa-mcp") == script


def test_a_missing_console_script_is_reported_as_absent(monkeypatch, tmp_path):
    """So the caller falls back rather than registering a path to nothing."""
    bindir = tmp_path / "empty"
    bindir.mkdir()
    monkeypatch.setattr(install.sys, "executable", str(bindir / "python"))

    assert install.console_script("qupa-daemon") is None


# ------------------------------------------------------------------ derivation


def test_the_agent_name_comes_from_the_key(forum):
    """Asking for it is a chance to mistype it, and a wrong one produces a
    daemon that polls the wrong account's notifications and reports nothing."""
    url, _ = forum

    assert install.whoami(url, "any-key") == "test-agent"


def test_naming_an_account_is_refused_and_reported_as_unknown(forum):
    """Which is why the header is left off: a Single-User key names its own
    account only when nothing else is named."""
    url, _ = forum

    assert install.forum_get(url, "/session/current.json", "k", "somebody") is None


def test_a_forum_that_does_not_answer_derives_nothing(forum):
    """So the installer asks instead of proceeding on a guess."""
    assert install.whoami("http://127.0.0.1:9", "k") is None


def test_the_forum_host_is_asked_which_topic_the_policy_is_in(monkeypatch):
    """Nothing here searches for it.

    A search needs the topic's title, and a title compiled into a release
    strands every installed agent the day somebody renames the topic. The
    forum host answers instead, and an unanswered install asks.
    """
    monkeypatch.setattr(
        install, "ask_the_forum_host", lambda s: {"researcher": "rsmith", "policy_topic_id": "42"}
    )

    assert "find_policy_topic" not in dir(install)
