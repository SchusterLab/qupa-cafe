"""The sandbox's hook, and the pixi command it gives for a pip or conda one.

What runs inside the sandbox is sandbox-runtime's, and it is checked where it
runs: ``python -m qupa_cafe.sandbox check``, which qupa-install runs as its
last step of setting it up.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from qupa_cafe import daemon, pixi_advice, sandbox
from qupa_cafe.daemon import Settings, build_prompt, runtime_command


@pytest.fixture
def installed(tmp_path):
    """A sandbox set up as qupa-install leaves one, minus the programs."""
    root = sandbox.sandbox_dir()
    workspace = root / "workspace"
    (workspace / ".pixi" / "envs" / "default").mkdir(parents=True)
    run = root / "runtime" / "run.mjs"
    (run.parent / "node_modules" / "@anthropic-ai" / "sandbox-runtime").mkdir(parents=True)
    run.write_text("", encoding="utf-8")
    node = tmp_path / "node"
    pixi = tmp_path / "pixi"
    for program in (node, pixi):
        program.write_text("", encoding="utf-8")
    research = tmp_path / "research"
    research.mkdir()
    config = {
        "workspace": str(workspace),
        "agent_dirs": [str(research)],
        "protected": sandbox.protected_paths([str(research)]),
        "node": str(node),
        "run": str(run),
        "pixi": str(pixi),
        "bash": "/bin/bash",
    }
    sandbox.config_path().write_text(json.dumps(config), encoding="utf-8")
    return config


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command, "description": "d"}, "cwd": "."}


def _decision(result: dict) -> str:
    return result["hookSpecificOutput"]["permissionDecision"]


# ------------------------------------------------------------ pip and conda


@pytest.mark.parametrize(
    "command",
    [
        "pip install numpy",
        "pip3 install -U numpy",
        "python -m pip install numpy",
        "python3.13 -m pip install --user numpy",
        "uv pip install numpy",
        "sudo pip install numpy",
        "cd work && pip install numpy && python run.py",
        "PIP_NO_INPUT=1 pip install numpy",
    ],
)
def test_every_way_of_calling_pip_install_is_caught(command):
    advice = pixi_advice.advise(command, "/ws")
    assert advice is not None
    assert "pixi add numpy" in advice
    assert "pixi add --pypi numpy" in advice


def test_a_conda_install_becomes_pixi_add_with_its_versions():
    advice = pixi_advice.advise("conda install -y -c conda-forge numpy=2.0 scipy", "/ws")
    assert "pixi add numpy=2.0 scipy" in advice
    assert "channel add" not in advice


def test_a_channel_of_its_own_is_added_first():
    advice = pixi_advice.advise("mamba install -c bioconda samtools", "/ws")
    assert advice.index("pixi workspace channel add bioconda") < advice.index("pixi add samtools")


def test_a_requirement_conda_forge_cannot_read_is_offered_from_pypi_only():
    advice = pixi_advice.advise("pip install 'qiskit[visualization]>=1.0'", "/ws")
    assert "pixi add --pypi 'qiskit[visualization]>=1.0'" in advice
    assert "pixi add 'qiskit" not in advice


def test_a_requirements_file_is_added_from_pypi():
    advice = pixi_advice.advise("pip install -r requirements.txt", "/ws")
    assert "pixi add --pypi" in advice
    assert "requirements.txt" in advice


def test_removing_a_package_becomes_pixi_remove():
    assert "pixi remove numpy" in pixi_advice.advise("pip uninstall -y numpy", "/ws")
    assert "pixi remove numpy" in pixi_advice.advise("conda remove numpy", "/ws")


def test_creating_an_environment_is_answered_with_the_one_already_active():
    advice = pixi_advice.advise("conda create -n work python=3.12", "/ws")
    assert "pixi add" in advice
    assert "/ws" in advice


@pytest.mark.parametrize(
    "command",
    [
        "pip list",
        "pip show numpy",
        "python -m pip --version",
        "python script.py --install numpy",
        "pixi add numpy",
        "echo pip install numpy",
        "grep 'conda install' notes.md",
    ],
)
def test_what_installs_nothing_is_let_through(command):
    assert pixi_advice.advise(command, "/ws") is None


def test_the_shim_refuses_an_install_with_the_pixi_command(tmp_path):
    """The copy in the workspace runs on its own, under the sandbox's Python."""
    result = subprocess.run(
        [sys.executable, "-I", str(Path(pixi_advice.__file__)), "pip", "install", "tqdm"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.returncode == 1
    assert "pixi add tqdm" in result.stderr


# ------------------------------------------------------------------ the hook


def test_a_command_is_approved_rewritten_to_run_in_the_sandbox(installed):
    result = sandbox.decide(_bash("python -c 'print(1)'"), installed)

    assert _decision(result) == "allow"
    updated = result["hookSpecificOutput"]["updatedInput"]
    assert updated["description"] == "d"
    *_, encoded = updated["command"].split(" ")
    assert base64.b64decode(encoded).decode() == "python -c 'print(1)'"
    assert sandbox.posix(installed["run"]) in updated["command"]


def test_no_quoting_in_a_command_reaches_the_shell_outside(installed):
    nasty = "echo '$(rm -rf ~)' \"`id`\"; exit 3"
    updated = sandbox.decide(_bash(nasty), installed)["hookSpecificOutput"]["updatedInput"]
    *_, encoded = updated["command"].split(" ")
    assert base64.b64decode(encoded).decode() == nasty
    assert "rm -rf" not in updated["command"]


def test_a_pip_install_is_refused_with_the_pixi_command(installed):
    result = sandbox.decide(_bash("pip install tqdm"), installed)

    assert _decision(result) == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "pixi add tqdm" in reason
    assert installed["workspace"] in reason


def test_without_the_sandbox_no_command_runs():
    result = sandbox.decide(_bash("ls"), None)
    assert _decision(result) == "deny"
    assert "qupa-install" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_writes_are_approved_in_the_workspace_and_the_agents_directory(installed, tmp_path):
    write = lambda path: {"tool_name": "Write", "tool_input": {"file_path": str(path)}}  # noqa: E731
    workspace, research = Path(installed["workspace"]), Path(installed["agent_dirs"][0])

    assert _decision(sandbox.decide(write(workspace / "plot.py"), installed)) == "allow"
    assert _decision(sandbox.decide(write(research / "notes" / "run.md"), installed)) == "allow"
    # Not refused: left to the researcher's own settings, which refuse it
    # unless they say otherwise, with a note of where to write instead.
    for elsewhere in (tmp_path / "x", workspace / ".." / ".." / "x"):
        out = sandbox.decide(write(elsewhere), installed)["hookSpecificOutput"]
        assert "permissionDecision" not in out
        assert installed["workspace"] in out["additionalContext"]


@pytest.mark.parametrize("name", ["QUPA.md", "CLAUDE.md", "AGENTS.md", ".claude/settings.json", ".mcp.json", ".venv/x.py"])
def test_what_steers_later_wakes_is_never_approved_for_writing(installed, name):
    """A misled agent would otherwise rewrite its own instructions, or the
    settings and hooks Claude Code runs outside the sandbox."""
    path = Path(installed["agent_dirs"][0]) / name
    out = sandbox.decide({"tool_name": "Edit", "tool_input": {"file_path": str(path)}}, installed)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert "read-only" in out["additionalContext"]


def test_the_kits_own_environment_is_protected_where_the_agents_directory_holds_it(monkeypatch, tmp_path):
    """The MCP server that enforces the policy runs from it."""
    agent = tmp_path / "agent"
    (agent / ".env-kit").mkdir(parents=True)
    monkeypatch.setattr(sandbox.sys, "prefix", str(agent / ".env-kit"))
    assert str((agent / ".env-kit").resolve()) in sandbox.protected_paths([str(agent)])
    assert str((tmp_path / "elsewhere").resolve()) not in sandbox.protected_paths([str(tmp_path / "elsewhere")])


def test_the_hook_approves_reads_only_where_the_sandbox_writes(installed, tmp_path):
    """Which is all run.mjs opens to it, so a key the configuration does not
    define opens nothing more."""
    other = tmp_path / "other"
    other.mkdir()
    config = dict(installed, readable=[str(other)])

    out = sandbox.decide({"tool_name": "Read", "tool_input": {"file_path": str(other / "x")}}, config)
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert str(other) not in sandbox.wake_notice(config)


def test_reads_are_approved_in_the_workspace_and_the_agents_directories(installed, tmp_path):
    read = lambda path: {"tool_name": "Read", "tool_input": {"file_path": str(path)}}  # noqa: E731

    assert _decision(sandbox.decide(read(Path(installed["agent_dirs"][0]) / "notes.md"), installed)) == "allow"
    assert _decision(sandbox.decide(read(Path(installed["workspace"]) / "out.png"), installed)) == "allow"
    out = sandbox.decide(read(Path.home() / ".ssh" / "id_ed25519"), installed)["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert installed["agent_dirs"][0] in out["additionalContext"]


def test_a_directory_holding_the_kits_key_is_not_opened_to_the_sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "qupa-cafe").mkdir(parents=True)
    (tmp_path / "research").mkdir()

    assert sandbox.writable_agent_dirs(str(tmp_path))[0] == []
    assert sandbox.writable_agent_dirs(str(tmp_path / "research"))[0] == [str((tmp_path / "research").resolve())]


def test_the_hook_runs_as_a_script(installed):
    """Which is how Claude Code runs it, with no package on the path."""
    result = subprocess.run(
        [sys.executable, str(Path(sandbox.__file__)), "hook"],
        input=json.dumps(_bash("pip install tqdm")),
        capture_output=True,
        text=True,
        cwd=Path(installed["workspace"]),
    )
    assert result.returncode == 0, result.stderr
    assert _decision(json.loads(result.stdout)) == "deny"


# ------------------------------------------------------------ in the daemon


def test_a_claude_wake_with_the_sandbox_gets_its_hook(installed, tmp_path):
    command = runtime_command(Settings(mcp_config=str(tmp_path / "absent.json")), "p")

    settings = json.loads(command[command.index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"].split("|")
    assert hook["hooks"][0]["command"].endswith("hook")
    # Bash runs only through the hook's approval, so a hook that does not run
    # leaves no command running unsandboxed.
    assert not any(tool.startswith("Bash") for tool in command[command.index("--allowedTools") + 1 :])


def test_a_wake_without_the_sandbox_gets_no_hook(tmp_path):
    command = runtime_command(Settings(mcp_config=str(tmp_path / "absent.json")), "p")
    chosen = json.loads(command[command.index("--settings") + 1])
    assert "hooks" not in chosen


def test_a_wake_keeps_claude_code_s_tool_search(installed, tmp_path):
    """Loading every tool with the session made each request reread their
    definitions; a wake that reads only its post needs none of them."""
    for config in (None, installed):
        assert "ENABLE_TOOL_SEARCH" not in json.dumps(json.loads(daemon.wake_settings(config)))
    assert "hooks" in json.loads(daemon.wake_settings(installed))


def test_a_sandboxed_wake_is_told_where_it_can_work(installed):
    settings = Settings(
        forum_url="http://x", api_key="k", api_username="u", agent_dir=installed["agent_dirs"][0]
    )
    topic = {"id": 10, "title": "t", "post_stream": {"posts": [{"post_number": 1, "raw": "b"}]}}

    prompt = build_prompt(settings, "you were mentioned", topic, "%BODY%")

    assert installed["workspace"] in prompt
    assert installed["agent_dirs"][0] in prompt
    assert "absolute paths" in prompt
    assert "CLAUDE.md" in prompt and "read-only" in prompt
    assert "pixi add" in prompt
    assert "cannot draw a plot" not in prompt
    assert prompt.endswith(daemon.OLD_NOTES)


# ------------------------------------------------------- the install banner


def test_the_install_banner_names_where_the_agent_runs_code_and_the_risk(installed):
    from qupa_cafe import install

    text = install.sandbox_summary()
    assert installed["agent_dirs"][0] in text
    assert "misleads" in text
    # Short enough, with the rest of the closing message, to be read whole.
    assert len(text.splitlines()) <= 6


# --------------------------------------------- what qupa-install installs


def test_nothing_is_installed_that_is_already_there(monkeypatch):
    """pixi, Node.js and the rest come from wherever this machine has them."""
    installed_now = []
    monkeypatch.setattr(sandbox, "find", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox, "git_bash", lambda: r"C:\Program Files\Git\bin\bash.exe")
    monkeypatch.setattr(sandbox, "install_pixi", lambda: installed_now.append("pixi"))
    monkeypatch.setattr(sandbox, "install_portable_git", lambda: installed_now.append("git"))
    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: installed_now.append(argv))
    monkeypatch.setattr(sandbox, "WINDOWS", True)

    assert sandbox.ensure_programs() is None
    assert installed_now == []


def test_only_what_is_missing_is_installed(monkeypatch):
    ran = []
    have = {"pixi", "npm"}
    monkeypatch.setattr(sandbox, "find", lambda name: f"/opt/{name}" if name in have else None)
    monkeypatch.setattr(sandbox, "wanted_programs", lambda: {"node": "nodejs", "npm": "nodejs", "rg": "ripgrep"})
    monkeypatch.setattr(sandbox, "install_pixi", lambda: pytest.fail("pixi is already there"))
    monkeypatch.setattr(sandbox, "WINDOWS", False)

    class Done:
        returncode = 0

    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: ran.append(argv) or Done())
    sandbox.ensure_programs()
    assert ran == [["/opt/pixi", "global", "install", "nodejs", "ripgrep"]]


def test_a_git_download_that_does_not_match_its_checksum_is_not_used(monkeypatch, tmp_path):
    import io

    release = {"assets": [{"name": "PortableGit-9.9-64-bit.7z.exe", "size": 3, "digest": "sha256:" + "0" * 64,
                           "browser_download_url": "https://example.invalid/pg.exe"}]}
    bodies = {sandbox.GIT_RELEASE_API: json.dumps(release).encode(), "https://example.invalid/pg.exe": b"exe"}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(sandbox.urllib.request, "urlopen", lambda url, timeout=0: Response(bodies[url]))
    monkeypatch.setattr(sandbox.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(sandbox, "_run", lambda *a, **k: pytest.fail("nothing unverified may run"))
    assert sandbox.install_portable_git() is None
    assert not sandbox.portable_git_dir().exists()


def test_a_setup_that_fails_leaves_no_sandbox_behind(installed, monkeypatch):
    """Not the configuration of a sandbox it has half replaced."""
    monkeypatch.setattr(sandbox, "missing_programs", lambda: ["pixi: https://pixi.sh"])
    assert sandbox.setup(installed["agent_dirs"][0], interactive=False) is False
    assert not sandbox.config_path().exists()
    assert sandbox.load_config() is None


def test_an_update_shows_the_uac_prompt_rather_than_skipping_it(monkeypatch, tmp_path, capsys):
    ran = []

    class Done:
        returncode = 0

    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: ran.append(argv) or Done())
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("an update asks nothing"))
    sandbox.runtime_dir().mkdir(parents=True, exist_ok=True)
    assert sandbox.windows_install("node", sandbox.runtime_dir(), interactive=False) is True
    assert "windows-install" in ran[0] and "--proxy-port-range" in ran[0]
    assert "UAC prompt" in capsys.readouterr().out


def test_a_declined_uac_prompt_is_said_to_be_declined(monkeypatch, capsys):
    class Declined:
        returncode = 2
        stdout = stderr = ""

    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: Declined())
    assert sandbox.windows_install("node", sandbox.runtime_dir(), interactive=False) is False
    assert "declined" in capsys.readouterr().out


# ------------------------------------------- Ubuntu's user-namespace restriction


def test_a_restricted_linux_is_given_a_script_to_run_with_sudo(installed, monkeypatch, capsys):
    """qupa-install runs no sudo itself: it stops, names the script, and the
    next qupa-install, after the script has run, goes on."""
    monkeypatch.setattr(sandbox, "missing_programs", lambda: [])
    monkeypatch.setattr(sandbox, "userns_restricted", lambda: True)
    monkeypatch.setattr(sandbox, "bwrap_exempted", lambda: False)
    monkeypatch.setattr(sandbox.subprocess, "call", lambda *a, **k: pytest.fail("ran something"))
    monkeypatch.setattr(sandbox, "_run", lambda argv, **k: pytest.fail(f"ran {argv}"))
    assert sandbox.setup(installed["agent_dirs"][0], interactive=True) is False
    script = sandbox.bwrap_script_path()
    assert f"sudo sh {script}" in capsys.readouterr().out
    text = script.read_text(encoding="utf-8")
    assert f"install -D -m 755 /usr/bin/bwrap {sandbox.LINUX_BWRAP}" in text
    assert f"\nprofile qupa-cafe-bwrap {sandbox.LINUX_BWRAP} flags=(unconfined) {{\n" in text
    assert "\nEOF\n" in text and "\r" not in text


def test_the_restriction_is_read_from_the_sysctl_or_ubuntu_s_profile(monkeypatch, tmp_path):
    sysctl, profile = tmp_path / "sysctl", tmp_path / "bwrap-userns-restrict"
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "USERNS_SYSCTL", sysctl)
    monkeypatch.setattr(sandbox, "UBUNTU_BWRAP_PROFILE", profile)
    assert not sandbox.userns_restricted()
    sysctl.write_text("1\n", encoding="ascii")
    assert sandbox.userns_restricted()
    # Off, Ubuntu's profile still confines /usr/bin/bwrap.
    sysctl.write_text("0\n", encoding="ascii")
    profile.write_text("", encoding="ascii")
    assert sandbox.userns_restricted()
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    assert not sandbox.userns_restricted()


# ------------------------------------------------- the environment, kept up to date


KIT_V1 = """[dependencies]
python = "3.13.15.*"
pip = "==26.2.1"
blas = { version = "*", build = "*openblas" }
pandas = "==3.0.6"
xlrd = "==2.0.2"
ffmpeg = "*"

[target.linux.dependencies]
c-compiler = "*"
"""


def test_a_spec_is_given_to_pixi_add_as_it_takes_one():
    found = sandbox.dependencies(KIT_V1)
    assert "c-compiler" not in found
    assert [sandbox.matchspec(name, spec) for name, spec in found.items()] == [
        "python=3.13.15.*", "pip==26.2.1", "blas=*=*openblas", "pandas==3.0.6", "xlrd==2.0.2", "ffmpeg",
    ]


def test_a_kit_s_new_pins_reach_the_workspace_and_the_agent_s_own_packages_stay():
    kit_v2 = KIT_V1.replace('pandas = "==3.0.6"', 'pandas = "==3.1.0"').replace('xlrd = "==2.0.2"\n', "")
    workspace = KIT_V1.replace('ffmpeg = "*"', 'ffmpeg = "*"\nqutip = ">=5.2.1,<6"')

    add, remove = sandbox.manifest_changes(kit_v2, KIT_V1, workspace)

    assert add == ["pandas==3.1.0"]
    assert remove == ["xlrd"]
    # A workspace an earlier kit made before the kit kept its copy: every pin
    # that differs, and nothing removed.
    assert sandbox.manifest_changes(kit_v2, None, workspace) == (["pandas==3.1.0"], [])


def test_a_workspace_is_updated_once_for_each_new_kit_manifest(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(sandbox, "_run", lambda argv, **kwargs: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    kit = (sandbox.FILES_DIR / "pixi.toml").read_text(encoding="utf-8")
    (tmp_path / "pixi.toml").write_text(kit.replace('pandas = "==', 'pandas = "==0.'), encoding="utf-8")

    assert sandbox.update_workspace(tmp_path, "pixi", {})
    ((_, verb, _, _, *specs),) = ran
    assert verb == "add" and len(specs) == 1 and specs[0].startswith("pandas==")
    assert (tmp_path / sandbox.KIT_MANIFEST_COPY).read_text(encoding="utf-8") == kit

    assert sandbox.update_workspace(tmp_path, "pixi", {})
    assert len(ran) == 1
