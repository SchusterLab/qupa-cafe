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


def test_windows_names_the_interpreter_rather_than_a_launcher(monkeypatch):
    """The bug this guards is a terminal window on the researcher's desktop.

    A console script on Windows is a launcher `.exe` that starts `python.exe`
    as a child. Creation flags do not reach a grandchild, so CREATE_NO_WINDOW
    on the launcher buys nothing: the child is a console program whose parent
    has no console, Windows gives it a new one, and on Windows 11 that console
    is a window that sits there for as long as the daemon runs.
    """
    monkeypatch.setattr(install.os, "name", "nt")
    monkeypatch.setattr(install, "running_installed", lambda: True)

    for argv in (install.daemon_argv(), install.mcp_server_argv()):
        assert argv[0] == install.sys.executable
        assert "-m" in argv
        assert not any(part.lower().endswith(".exe") for part in argv[1:])


def test_the_working_directory_is_kept_off_the_import_path(monkeypatch):
    """Which is what the console script gave and `-m` does not.

    Without it a daemon whose working directory is a checkout of this
    repository imports the checkout's copy of the package, not the installed
    one -- so an upgrade would appear to do nothing.
    """
    monkeypatch.setattr(install.sys, "version_info", (3, 12, 0))
    assert "-P" in install.interpreter_argv("qupa_cafe.daemon")

    monkeypatch.setattr(install.sys, "version_info", (3, 10, 9))
    assert "-P" not in install.interpreter_argv("qupa_cafe.daemon")


# ------------------------------------------------------------------ derivation


def test_the_agent_name_comes_from_the_key(forum):
    """Asking for it is a chance to mistype it, and a wrong one produces a
    daemon that polls the wrong account's notifications and reports nothing."""
    url, _ = forum

    assert install.whoami(url, "any-key") == ("test-agent", "")


def test_naming_an_account_is_refused(forum):
    """Which is why the header is left off: a Single-User key names its own
    account only when nothing else is named."""
    url, _ = forum

    status, _body = install.forum_get(url, "/session/current.json", "k", "somebody")

    assert status == 403


def test_a_refused_key_is_reported_as_a_refused_key(forum):
    """Not as "the forum did not say which account this is".

    Three failures reach this: a key the forum rejects, a URL that answers
    nothing, and a forum answering something unexpected. Only the first is
    fixed by typing the key again, and a message that covers all three sends
    somebody to check whichever they think of first.
    """
    url, stub = forum
    stub.refuse_keys = True

    agent, problem = install.whoami(url, "wrong-key")

    assert agent is None
    assert "refused this key" in problem


def test_an_unreachable_forum_says_so_instead(forum):
    """The key may be perfectly good, and re-typing it changes nothing."""
    agent, problem = install.whoami("http://127.0.0.1:9", "k")

    assert agent is None
    assert "did not answer" in problem
    assert "refused" not in problem


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
