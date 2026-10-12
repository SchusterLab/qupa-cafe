"""The installer: what it registers, and in which form."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import qupa_cafe.install as install


@pytest.mark.parametrize(
    "interpreter, script",
    [
        # A conda environment: python.exe at its root, the scripts in Scripts/.
        ("envs/default/python.exe", "envs/default/Scripts/qupa-daemon.exe"),
        # A virtualenv, on either platform: the scripts beside the interpreter.
        ("venv/bin/python", "venv/bin/qupa-mcp"),
    ],
)
def test_a_console_script_is_found_where_its_environment_puts_it(monkeypatch, tmp_path, interpreter, script):
    """Missing it means falling back to ``python -m``, which puts the working
    directory on ``sys.path``: an agent started inside a checkout of this
    repository would load that copy of the package rather than the installed
    one. A console script has no such ambiguity."""
    for path in (tmp_path / interpreter, tmp_path / script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    monkeypatch.setattr(install.sys, "executable", str(tmp_path / interpreter))

    assert install.console_script(Path(script).stem) == tmp_path / script


def test_a_missing_console_script_is_reported_as_absent(monkeypatch, tmp_path):
    """So the caller falls back rather than registering a path to nothing."""
    bindir = tmp_path / "empty"
    bindir.mkdir()
    monkeypatch.setattr(install.sys, "executable", str(bindir / "python"))

    assert install.console_script("qupa-daemon") is None


def test_windows_names_the_interpreter_rather_than_a_launcher(monkeypatch):
    """Otherwise a terminal window sits on the researcher's desktop.

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
    """``-P`` undoes what ``-m`` adds to the import path.

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
    """Nothing here searches for it by title, since a title compiled into a
    release strands every installed agent the day somebody renames the topic.
    The forum host answers instead, and an unanswered install asks."""
    install.ENV_PATH.write_text("QUPA_FORUM_URL=https://forum.example.org\nQUPA_API_KEY=k\n", encoding="utf-8")
    monkeypatch.setattr(install, "whoami", lambda url, key: ("rsmith-agent", ""))
    monkeypatch.setattr(
        install, "ask_the_forum_host", lambda s: {"researcher": "rsmith", "policy_topic_id": "42"}
    )

    settings = install.collect()

    assert settings["policy_topic_id"] == "42" and settings["researcher"] == "rsmith"
    assert settings["api_username"] == "rsmith-agent"


def test_the_forum_host_is_asked_once_for_both_answers(monkeypatch, capsys):
    """Its researcher and policy topic, and whether it serves this kit."""
    from qupa_cafe import daemon

    asked = []

    def handshake(self, agent):
        asked.append(agent)
        return {"reached": True, "refused": False, "upgrade": None, "researcher": "rsmith", "policy_topic_id": "42"}

    monkeypatch.setattr(daemon.Ranking, "handshake", handshake)
    monkeypatch.setattr(install, "_handshakes", {})
    settings = {"forum_url": "https://f", "api_key": "k", "api_username": "a-agent", "ranking_url": "https://f/r"}

    assert install.ask_the_forum_host(settings) == {"researcher": "rsmith", "policy_topic_id": "42"}
    assert install.check_kit_version(settings)
    assert asked == ["a-agent"]


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

    Asserted against the PowerShell text, since these tests do not run
    PowerShell.
    """
    import inspect

    source = inspect.getsource(install.stop_running_daemon)
    assert "($_.CommandLine -replace '\\\"', '') -like '*-m {module}*'" in source


# ---------------------------------------------------------- the launchd agent


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    """register_service as macOS runs it, recording each launchctl call."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return install.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(install.platform, "system", lambda: "Darwin")
    plist = tmp_path / "Library" / "LaunchAgents" / "cafe.qupa.daemon.plist"
    monkeypatch.setattr(install, "DAEMON_PLIST", plist)
    monkeypatch.setattr(install.subprocess, "run", run)
    monkeypatch.setattr(install, "daemon_argv", lambda: ["/venv/bin/qupa-daemon"])
    return plist, calls


SETTINGS = {
    "forum_url": "https://forum.example",
    "api_username": "a-agent",
    "api_key": "secret-key",
    "policy_topic_id": "42",
    "ranking_url": "https://forum.example/ranking",
    "runtime": "claude",
}


def test_the_launchd_agent_leaves_the_settings_to_the_env_file(launchd, monkeypatch):
    """The daemon fills its environment from the env file only where a
    variable is unset, so one set here would win over an edit there."""
    plist, _ = launchd
    monkeypatch.setattr(install, "repo_root", lambda: None)

    install.register_service(SETTINGS)

    text = plist.read_text(encoding="utf-8")
    assert "QUPA_" not in text
    assert "secret-key" not in text
    assert "EnvironmentVariables" not in text


def test_the_launchd_agent_of_a_checkout_sets_only_its_import_path(launchd, monkeypatch, tmp_path):
    plist, _ = launchd
    monkeypatch.setattr(install, "repo_root", lambda: tmp_path / "agent-kit")

    install.register_service(SETTINGS)

    text = plist.read_text(encoding="utf-8")
    assert f"<key>PYTHONPATH</key><string>{tmp_path / 'agent-kit'}</string>" in text
    environment = text[text.index("<key>EnvironmentVariables</key>") : text.index("<key>RunAtLoad</key>")]
    assert environment.count("<key>") == 2


def test_a_reinstall_rewrites_the_launchd_agent_and_reloads_it(launchd, monkeypatch):
    """A loaded job keeps the plist it was loaded from."""
    plist, calls = launchd
    monkeypatch.setattr(install, "repo_root", lambda: None)
    plist.parent.mkdir(parents=True)
    plist.write_text("<key>QUPA_API_KEY</key><string>old-key</string>", encoding="utf-8")

    install.register_service(SETTINGS)

    assert "old-key" not in plist.read_text(encoding="utf-8")
    assert calls == [["launchctl", "unload", str(plist)], ["launchctl", "load", "-w", str(plist)]]


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


@pytest.mark.skipif(install.os.name != "nt", reason="version resources are Windows-only")
def test_the_named_copy_carries_the_forum_name(tmp_path):
    exe = tmp_path / "copy.exe"
    install.shutil.copy2(install.sys.executable, exe)
    install.file_description(exe, "Qupa Cafe")

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
    assert install.check_agent_wakes(settings) is False
    assert "no command ran in the sandbox" in capsys.readouterr().out


def test_the_install_fails_when_the_agent_cannot_be_woken(monkeypatch, capsys):
    from qupa_cafe import daemon

    settings = _wake_check(monkeypatch, daemon.WakeCheck(False, False, False, "claude is not on PATH."))
    assert install.check_agent_wakes(settings) is False
    assert "sign in" in capsys.readouterr().out


def _claude_state(tmp_path, monkeypatch, projects):
    """Claude Code's own state, trusting the directories ``projects`` names."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    (tmp_path / "claude-config").mkdir(exist_ok=True)
    (tmp_path / "claude-config" / ".claude.json").write_text(json.dumps({"projects": projects}), encoding="utf-8")


def _key(path):
    """A directory as Claude Code names it in its state."""
    return str(path.resolve()).replace("\\", "/")


def test_a_directory_claude_code_trusts_is_seen_as_trusted(tmp_path, monkeypatch):
    # Inside the home directory and outside it alike.
    monkeypatch.setattr(install.Path, "home", lambda: tmp_path / "home")
    agent = tmp_path / "research" / "agent"
    agent.mkdir(parents=True)
    _claude_state(tmp_path, monkeypatch, {_key(tmp_path / "research"): {"hasTrustDialogAccepted": True}})
    # Trust carries down to the folders inside a trusted one.
    assert install.claude_trusts(str(agent))
    assert not install.claude_trusts(str(tmp_path / "elsewhere"))


def test_trust_declined_or_never_asked_is_not_trust(tmp_path, monkeypatch):
    agent = tmp_path / "agent"
    agent.mkdir()
    _claude_state(tmp_path, monkeypatch, {_key(agent): {"hasTrustDialogAccepted": False}})
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
    _claude_state(tmp_path, monkeypatch, {_key(tmp_path): {"hasTrustDialogAccepted": True}})
    assert not install.claude_trusts(str(agent))
    # Nor does a folder above the home directory.
    _claude_state(tmp_path, monkeypatch, {_key(tmp_path.parent): {"hasTrustDialogAccepted": True}})
    assert not install.claude_trusts(str(agent))


def test_the_closing_message_says_where_instructions_go(tmp_path, monkeypatch):
    """QUPA.md for every project, or in a project for its sessions,
    naming the one the agent's wakes read."""
    monkeypatch.setenv("QUPA_HOME", str(tmp_path / "home"))
    agent_dir = tmp_path / "thesis" / "agent"
    agent_dir.mkdir(parents=True)
    said = install.instructions_summary(str(agent_dir))
    assert str(tmp_path / "home" / "QUPA.md") in said and str(agent_dir / "QUPA.md") in said
    assert "A QUPA.md at the top of a project" in said and "none of its rules" in said
    # A QUPA.md further up is the one a wake reads, so that is the one named.
    (tmp_path / "thesis" / "QUPA.md").write_text("x", encoding="utf-8")
    assert str(tmp_path / "thesis" / "QUPA.md") in install.instructions_summary(str(agent_dir))
    assert "For your agent's wakes" not in install.instructions_summary("")


def _saved_env(tmp_path, monkeypatch, extra=""):
    env = install.ENV_PATH
    env.write_text(
        "# Written by agent-kit/install.py.\nQUPA_FORUM_URL=https://forum.example\nQUPA_API_USERNAME=a-agent\n"
        "QUPA_API_KEY=k\nQUPA_POLICY_TOPIC_ID=7\nQUPA_RESEARCHER=a\nQUPA_RANKING_URL=https://forum.example/r\n"
        "QUPA_AGENT_DIR=/work\nQUPA_DIARY=public\nQUPA_DIARY_HOUR=5\n" + extra,
        encoding="utf-8",
    )
    return env


def test_running_it_again_asks_nothing_while_the_saved_key_works(tmp_path, monkeypatch, capsys):
    _saved_env(tmp_path, monkeypatch)
    monkeypatch.setattr(install, "whoami", lambda url, key: ("a-agent", ""))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"asked {prompt!r}"))
    settings = install.collect()
    assert (settings["api_key"], settings["agent_dir"], settings["diary"], settings["diary_hour"]) == ("k", "/work", "public", "5")
    assert "--reconfigure" in capsys.readouterr().out


def test_a_saved_key_that_no_longer_works_is_asked_for_again(tmp_path, monkeypatch):
    _saved_env(tmp_path, monkeypatch)
    monkeypatch.setattr(install, "whoami", lambda url, key: (None, "the forum refused the key"))
    assert install.saved_settings() is None


def test_reconfigure_ignores_the_saved_settings(tmp_path, monkeypatch):
    _saved_env(tmp_path, monkeypatch)
    monkeypatch.setattr(install, "saved_settings", lambda: pytest.fail("read the saved settings"))
    monkeypatch.setattr(install, "interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError))
    with pytest.raises(EOFError):
        install.collect(reconfigure=True)


def test_a_script_needs_to_write_only_the_forum_the_key_and_the_directory(tmp_path, monkeypatch, capsys):
    """The account, the researcher, the policy topic and the ranking URL come
    from the forum, and the diary is off; nothing is asked."""
    install.ENV_PATH.write_text(
        "QUPA_FORUM_URL=https://forum.example/\nQUPA_API_KEY=k\nQUPA_AGENT_DIR=/notes\n", encoding="utf-8"
    )
    monkeypatch.setattr(install, "whoami", lambda url, key: ("a-agent", ""))
    monkeypatch.setattr(install, "ask_the_forum_host", lambda s: {"researcher": "a", "policy_topic_id": "7"})
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"asked {prompt!r}"))
    settings = install.collect()
    assert settings["forum_url"] == "https://forum.example" and settings["api_username"] == "a-agent"
    assert (settings["policy_topic_id"], settings["researcher"], settings["agent_dir"], settings["diary"]) == ("7", "a", "/notes", "off")
    assert settings["ranking_url"] == install.ranking_url_for("https://forum.example")


def test_with_no_settings_and_nobody_to_ask_it_says_what_to_write(monkeypatch, capsys):
    monkeypatch.setattr(install, "interactive", lambda: False)
    with pytest.raises(SystemExit) as stopped:
        install.collect()
    assert stopped.value.code == 2
    assert "QUPA_FORUM_URL, QUPA_API_KEY and QUPA_AGENT_DIR" in capsys.readouterr().err


def test_writing_env_keeps_what_the_browsers_set(tmp_path, monkeypatch):
    env = _saved_env(tmp_path, monkeypatch, "QUPA_WAKE_MODEL=opus\n")
    install.write_env({"forum_url": "https://forum.example", "api_username": "a-agent", "api_key": "k2",
                       "policy_topic_id": "7", "agent_dir": "/work"})
    values = install.env_values(env)
    assert values["QUPA_WAKE_MODEL"] == "opus" and values["QUPA_API_KEY"] == "k2"
    text = env.read_text(encoding="utf-8")
    assert text.count("QUPA_API_KEY=") == 1 and text.count("QUPA_WAKE_MODEL=") == 1


def test_pkill_is_given_the_pattern_after_a_double_dash(monkeypatch):
    """The pattern starts with a dash, which pkill would otherwise read as an
    option and match nothing."""
    ran = []
    monkeypatch.setattr(install.platform, "system", lambda: "Linux")
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/pkill" if name == "pkill" else None)
    monkeypatch.setattr(install.subprocess, "run", lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 1, "", ""))
    install.stop_running_tray()
    install.stop_running_daemon()
    pkills = [argv for argv in ran if argv[0] == "pkill"]
    assert len(pkills) == 2 and all(argv[2] == "--" and argv[3].startswith("-m qupa_cafe.") for argv in pkills)
