"""The installer: what it registers, and in which form."""

from __future__ import annotations

from pathlib import Path

import pytest

import qupa_cafe.install as install

from conftest import POLICY_TEXT, POLICY_TOPIC_ID


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


def test_the_policy_topic_is_found_by_title_and_confirmed_by_its_text(forum):
    """Confirmed, because a search also returns the category's About topic, and
    a hash pinned to that would be a hash of text nobody wrote as policy."""
    url, _ = forum

    assert install.find_policy_topic(url, "any-key") == str(POLICY_TOPIC_ID)


def test_a_forum_that_does_not_answer_derives_nothing(forum):
    """So the installer asks instead of proceeding on a guess."""
    assert install.whoami("http://127.0.0.1:9", "k") is None
    assert install.find_policy_topic("http://127.0.0.1:9", "k") is None


def test_the_marker_matches_the_policy_this_repository_publishes():
    """The marker is what confirms a found topic really is the policy.

    If it drifts from the text staff publish, derivation stops finding the
    topic and every install quietly falls back to asking for its id -- which
    works, and hides that a check stopped checking.
    """
    published = Path(__file__).resolve().parents[2] / "config" / "policy-topic.md"
    if not published.is_file():
        pytest.skip("not a checkout, so the published policy text is not here")

    assert install.POLICY_MARKER in published.read_text(encoding="utf-8")
    assert install.POLICY_MARKER in POLICY_TEXT
