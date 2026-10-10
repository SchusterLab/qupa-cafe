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


def test_the_windows_startup_entry_restarts_the_daemon():
    script = install.windows_startup_script(r'cmd /c "C:\py\python.exe" -m qupa_cafe.daemon')
    # Run hidden, wait for it, and loop.
    assert r'sh.Run("cmd /c ""C:\py\python.exe"" -m qupa_cafe.daemon", 0, True)' in script
    assert script.startswith('Set sh = CreateObject("WScript.Shell")\nDo\n')
    assert script.rstrip().endswith("Loop")
    # Another daemon holding the lock, and configuration only the installer can
    # fix, end the loop; anything else is restarted a minute later.
    assert "If status = 0 Or status = 2 Then Exit Do" in script
    assert "WScript.Sleep 60000" in script


def test_the_windows_daemon_search_ignores_quotes():
    """The Startup entry quotes every argument, so the daemon it starts has
    `"-m" "qupa_cafe.daemon"` on its command line. A search for the unquoted
    form misses it, and an upgrade leaves the old daemon running.

    Asserted against the PowerShell text because these tests do not run
    PowerShell; the search itself was run against a real Startup-entry daemon.
    """
    import inspect

    source = inspect.getsource(install.stop_running_daemon)
    assert "($_.CommandLine -replace '\\\"', '') -like '*-m {module}*'" in source


# -------------------------------------------------------------------- the tray


def test_the_tray_is_skipped_without_its_extra(monkeypatch, capsys):
    monkeypatch.setattr(install, "tray_available", lambda: False)
    install.register_tray()
    assert 'pip install "qupa-cafe[tray]"' in capsys.readouterr().out


def test_the_tray_runs_windowless_on_windows(monkeypatch, tmp_path):
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(install.os, "name", "nt")
    monkeypatch.setattr(install, "interpreter_argv", lambda module: [str(tmp_path / "python.exe"), "-m", module])
    assert install.tray_argv()[0] == str(tmp_path / "pythonw.exe")


def test_the_autostart_entry_quotes_as_the_specification_asks():
    line = install.desktop_exec(["env", "PYTHONPATH=/a b", '/x/"py"$1', "-m", "qupa_cafe.tray"])
    assert line == '"env" "PYTHONPATH=/a b" "/x/\\"py\\"\\$1" "-m" "qupa_cafe.tray"'


def test_the_tray_has_a_startup_entry_of_its_own(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    entry = install.windows_startup_entry('"pythonw.exe" -m qupa_cafe.tray', install.TRAY_VBS)
    assert entry.name == "qupa-cafe-tray.vbs"
    assert not (entry.parent / "qupa-cafe-daemon.vbs").exists()


def test_a_startup_entry_started_now_leaves_the_session_behind(monkeypatch, tmp_path):
    """The daemon and the icon outlive the session the installer ran in, and
    should not carry its id, its socket or its NO_COLOR for the whole login."""
    seen = {}

    def fake_popen(argv, **kwargs):
        seen["env"] = kwargs.get("env")

    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "secret")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(install.subprocess, "Popen", fake_popen)

    assert install.start_startup_entry(tmp_path / "qupa-cafe-daemon.vbs") is True
    assert not {"CLAUDECODE", "CLAUDE_CODE_MESSAGING_TOKEN", "NO_COLOR"} & set(seen["env"])


def test_the_tray_keeps_pythonw_outside_a_venv(monkeypatch):
    monkeypatch.setattr(install.os, "name", "nt")
    monkeypatch.setattr(install.sys, "base_prefix", install.sys.prefix)
    assert install.named_tray_interpreter() is None


def test_the_named_copy_carries_the_forum_name(tmp_path):
    import pytest

    if install.os.name != "nt":
        pytest.skip("version resources are Windows-only")
    exe = tmp_path / "copy.exe"
    install.shutil.copy2(install.sys.executable, exe)
    install.file_description(exe, "Qupa Cafe")
    import subprocess

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"(Get-Item '{exe}').VersionInfo.FileDescription"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out == "Qupa Cafe"


def _wake_check(monkeypatch, check):
    from qupa_cafe import daemon

    monkeypatch.setattr(daemon, "check_wake", lambda settings: check)
    settings = {"forum_url": "http://x", "api_key": "k", "api_username": "u", "runtime": "claude"}
    return settings


def test_the_install_fails_when_the_sandbox_ran_no_command(monkeypatch, capsys):
    from qupa_cafe import daemon

    settings = _wake_check(monkeypatch, daemon.WakeCheck(True, True, False, "WOKEN 5aa6f598 NOBASH"))
    assert install.check_agent_wakes(settings, sandboxed=True) is False
    assert "no command ran in the sandbox" in capsys.readouterr().out
    # Without a sandbox, no command is expected.
    assert install.check_agent_wakes(settings, sandboxed=None) is True


def test_the_install_fails_when_the_agent_cannot_be_woken(monkeypatch, capsys):
    from qupa_cafe import daemon

    settings = _wake_check(monkeypatch, daemon.WakeCheck(False, False, False, "claude is not on PATH."))
    assert install.check_agent_wakes(settings, sandboxed=None) is False
    assert "sign in" in capsys.readouterr().out


def _claude_state(tmp_path, monkeypatch, projects):
    import json

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    (tmp_path / "claude-config").mkdir()
    (tmp_path / "claude-config" / ".claude.json").write_text(json.dumps({"projects": projects}), encoding="utf-8")


def test_a_directory_claude_code_trusts_is_seen_as_trusted(tmp_path, monkeypatch):
    agent = tmp_path / "research" / "agent"
    agent.mkdir(parents=True)
    key = str((tmp_path / "research").resolve()).replace("\\", "/")
    _claude_state(tmp_path, monkeypatch, {key: {"hasTrustDialogAccepted": True}})
    # Trust carries down to the folders inside a trusted one.
    assert install.claude_trusts(str(agent))
    assert not install.claude_trusts(str(tmp_path / "elsewhere"))


def test_trust_declined_or_never_asked_is_not_trust(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    agent.mkdir()
    key = str(agent.resolve()).replace("\\", "/")
    _claude_state(tmp_path, monkeypatch, {key: {"hasTrustDialogAccepted": False}})
    assert not install.claude_trusts(str(agent))


def test_trust_is_asked_for_in_claude_codes_own_dialog_and_never_written(tmp_path, monkeypatch, capsys):
    """/exit as the first message closes Claude Code once its question is answered."""
    agent = tmp_path / "agent"
    _claude_state(tmp_path, monkeypatch, {})
    before = (tmp_path / "claude-config" / ".claude.json").read_text(encoding="utf-8")
    ran = []

    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(install.sys, "stdin", Terminal())
    monkeypatch.setattr(install.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(install.subprocess, "call", lambda argv, **kw: ran.append((argv, kw["cwd"])) or 0)

    assert install.ensure_claude_trust(str(agent)) is False
    assert ran == [(["/bin/claude", "/exit"], str(agent))]
    assert (tmp_path / "claude-config" / ".claude.json").read_text(encoding="utf-8") == before
    assert "choose to trust it" in capsys.readouterr().out


def test_a_trusted_home_directory_does_not_make_a_folder_in_it_trusted(tmp_path, monkeypatch):
    """Claude Code still asks about a folder under a home it trusts."""
    monkeypatch.setattr(install.Path, "home", lambda: tmp_path)
    agent = tmp_path / "agent"
    agent.mkdir()
    _claude_state(tmp_path, monkeypatch, {str(tmp_path.resolve()).replace("\\", "/"): {"hasTrustDialogAccepted": True}})
    assert not install.claude_trusts(str(agent))
