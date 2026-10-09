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
    readable = tmp_path / "research"
    readable.mkdir()
    config = {
        "workspace": str(workspace),
        "readable": [str(readable)],
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


def test_writes_are_approved_only_in_the_workspace(installed, tmp_path):
    inside = {"tool_name": "Write", "tool_input": {"file_path": str(Path(installed["workspace"]) / "plot.py")}}
    research = {"tool_name": "Write", "tool_input": {"file_path": str(Path(installed["readable"][0]) / "x")}}
    escape = {"tool_name": "Edit", "tool_input": {"file_path": str(Path(installed["workspace"]) / ".." / ".." / "x")}}

    assert _decision(sandbox.decide(inside, installed)) == "allow"
    # Not refused: left to the researcher's own settings, which refuse it
    # unless they say otherwise.
    assert sandbox.decide(research, installed) is None
    assert sandbox.decide(escape, installed) is None


def test_reads_are_approved_in_the_workspace_and_the_readable_directories(installed, tmp_path):
    read = lambda path: {"tool_name": "Read", "tool_input": {"file_path": str(path)}}  # noqa: E731

    assert _decision(sandbox.decide(read(Path(installed["readable"][0]) / "notes.md"), installed)) == "allow"
    assert _decision(sandbox.decide(read(Path(installed["workspace"]) / "out.png"), installed)) == "allow"
    assert sandbox.decide(read(Path.home() / ".ssh" / "id_ed25519"), installed) is None


def test_a_directory_holding_the_kits_key_is_not_made_readable(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "qupa-cafe").mkdir(parents=True)
    (tmp_path / "research").mkdir()

    assert sandbox.readable_dirs(str(tmp_path))[0] == []
    assert sandbox.readable_dirs(str(tmp_path / "research"))[0] == [str((tmp_path / "research").resolve())]


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
    command = runtime_command(Settings(runtime="claude", mcp_config=str(tmp_path / "absent.json")), "p")

    settings = json.loads(command[command.index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"].split("|")
    assert hook["hooks"][0]["command"].endswith("hook")
    # Bash runs only through the hook's approval, so a hook that does not run
    # leaves no command running unsandboxed.
    assert not any(tool.startswith("Bash") for tool in command[command.index("--allowedTools") + 1 :])


def test_a_wake_without_the_sandbox_gets_no_hook(tmp_path):
    command = runtime_command(Settings(runtime="claude", mcp_config=str(tmp_path / "absent.json")), "p")
    assert "--settings" not in command


def test_a_codex_wake_is_not_sandboxed(installed):
    assert "--settings" not in runtime_command(Settings(runtime="codex"), "p")
    assert daemon.wake_notice(Settings(runtime="codex")) == daemon.WAKE_NOTICE


def test_a_sandboxed_wake_is_told_where_it_can_work(installed):
    settings = Settings(forum_url="http://x", api_key="k", api_username="u", runtime="claude")
    topic = {"id": 10, "title": "t", "post_stream": {"posts": [{"post_number": 1, "raw": "b"}]}}

    prompt = build_prompt(settings, "you were mentioned", topic, "%BODY%")

    assert installed["workspace"] in prompt
    assert installed["readable"][0] in prompt
    assert "pixi add" in prompt
    assert "cannot draw a plot" not in prompt
    assert prompt.endswith(daemon.OLD_NOTES)
