"""The sandbox a woken agent runs commands in.

A Claude Code wake gets Bash, and every command it runs goes through Anthropic's
sandbox-runtime (``sandbox_files/run.mjs``): open internet access, writes to the
sandbox's workspace only, reads of the workspace and of the researcher's
``QUPA_AGENT_DIR`` and of nothing else in the home directory, and none of the
variables that hold keys. Python there is a pixi workspace with the packages of
Jupyter's scipy-notebook image (``sandbox_files/pixi.toml``), already active.

The daemon hands the wake a ``PreToolUse`` hook, which is this module run as a
script. For Bash it approves the command rewritten to run inside the sandbox,
or refuses a pip or conda install with the pixi command to run instead
(``qupa_cafe.pixi_advice``). For Read, Write, Edit, Glob and Grep it approves a
path the sandbox could reach the same way, and leaves any other path to the
researcher's own Claude Code settings. Codex wakes are not sandboxed.

``qupa-install`` sets it up with ``setup``; without node, npm and pixi, or on
Windows without the runtime's one-time elevated install, it reports what is
missing and wakes run as before, with no Bash.

    python -m qupa_cafe.sandbox check     # run a test command in the sandbox
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script, which is how the hook is run: the package's parent, so
    # that this is the same qupa_cafe the daemon imported.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from qupa_cafe import pixi_advice

PACKAGE_DIR = Path(__file__).resolve().parent
FILES_DIR = PACKAGE_DIR / "sandbox_files"

NL = chr(10)
WINDOWS = os.name == "nt"

# Where the runtime runs as on Windows; sandbox-runtime's windows-install makes it.
WINDOWS_USER = "srt-sandbox"

# The programs a shim in the workspace stands in for.
SHIMS = ("pip", "pip3", "conda", "mamba", "micromamba")

# Tools whose path the hook checks, and the input field that holds it.
READ_TOOLS = {"Read": "file_path", "Glob": "path", "Grep": "path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}


def sandbox_dir() -> Path:
    return Path(
        os.environ.get("QUPA_SANDBOX_DIR")
        or Path.home() / ".local" / "share" / "qupa-cafe" / "sandbox"
    )


def config_path() -> Path:
    return sandbox_dir() / "config.json"


def runtime_dir() -> Path:
    return sandbox_dir() / "runtime"


def workspace_dir() -> Path:
    return sandbox_dir() / "workspace"


def env_dir(workspace: Path) -> Path:
    return workspace / ".pixi" / "envs" / "default"


def load_config() -> dict | None:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ready(config: dict | None = None) -> bool:
    """Whether a wake can be given the sandbox: set up, and still there."""
    config = config if config is not None else load_config()
    if not config:
        return False
    needed = [
        config.get("node", ""),
        config.get("run", ""),
        config.get("pixi", ""),
        str(Path(config.get("run", "")).parent / "node_modules" / "@anthropic-ai" / "sandbox-runtime"),
        str(env_dir(Path(config.get("workspace", "")))),
    ]
    return all(item and Path(item).exists() for item in needed)


# ------------------------------------------------------------------- the hook


def posix(path: str) -> str:
    """A path as the shell Claude Code runs Bash in takes it, Git Bash included."""
    return str(path).replace(chr(92), "/")


def shell_quote(text: str) -> str:
    return "'" + str(text).replace("'", "'" + chr(92) + "''") + "'"


def wrapped(command: str, config: dict) -> str:
    """``command`` as a command that runs it in the sandbox. Base64, so that no
    quoting in it can reach the shell outside."""
    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    return " ".join(
        [shell_quote(posix(config["node"])), shell_quote(posix(config["run"])), shell_quote(posix(str(config_path()))), encoded]
    )


def _real(path: str, cwd: str) -> str:
    full = Path(path).expanduser()
    if not full.is_absolute():
        full = Path(cwd) / full
    return os.path.normcase(os.path.realpath(full))


def within(path: str, roots: list[str], cwd: str) -> bool:
    target = _real(path, cwd)
    for root in roots:
        top = _real(root, cwd)
        if target == top or target.startswith(top.rstrip(os.sep) + os.sep):
            return True
    return False


def decide(event: dict, config: dict | None) -> dict | None:
    """The hook's answer to one tool call, or None to leave it to the
    researcher's own settings."""
    tool = event.get("tool_name", "")
    arguments = dict(event.get("tool_input") or {})
    cwd = event.get("cwd") or os.getcwd()

    def answer(decision: str, reason: str, updated: dict | None = None) -> dict:
        out = {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}
        if updated is not None:
            out["updatedInput"] = updated
        return {"hookSpecificOutput": out}

    if tool == "Bash":
        if not ready(config):
            return answer("deny", "The sandbox is not set up on this machine, so no command can run. "
                                  "Your researcher sets it up with qupa-install.")
        command = str(arguments.get("command") or "")
        advice = pixi_advice.advise(command, config["workspace"])
        if advice:
            return answer("deny", advice)
        arguments["command"] = wrapped(command, config)
        return answer("allow", "Runs in the qupa-cafe sandbox.", arguments)

    if not config:
        return None
    workspace = [config["workspace"]]
    if tool in WRITE_TOOLS:
        path = arguments.get(WRITE_TOOLS[tool])
        if path and within(str(path), workspace, cwd):
            return answer("allow", "Inside the sandbox's workspace.")
        return None
    if tool in READ_TOOLS:
        path = arguments.get(READ_TOOLS[tool]) or cwd
        if within(str(path), workspace + list(config.get("readable") or []), cwd):
            return answer("allow", "Readable from the sandbox.")
        return None
    return None


def hook_main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    result = decide(event, load_config())
    if result is not None:
        sys.stdout.write(json.dumps(result))
    return 0


def hook_command() -> str:
    """The hook as Claude Code runs it: this file under this interpreter."""
    return f'"{posix(sys.executable)}" "{posix(str(Path(__file__).resolve()))}" hook'


def claude_settings() -> str:
    """The ``--settings`` a sandboxed wake gets, as JSON."""
    matcher = "|".join(["Bash", *READ_TOOLS, *WRITE_TOOLS])
    return json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]}]}}
    )


def wake_notice(config: dict) -> str:
    """What a sandboxed wake can do, for the end of its prompt."""
    readable = list(config.get("readable") or [])
    reads = f" and can read {', '.join(readable)}" if readable else ""
    return (
        "Bash runs each command in a sandbox with open internet access. It starts in "
        f"{config['workspace']}, the only place you can write, which keeps what you leave there "
        f"for later wakes{reads}; nothing else on this machine is reachable. Python there is a "
        "pixi environment with the scipy-notebook packages, already active, so run python "
        "directly. To add a package run `pixi add <name>` (conda-forge) or `pixi add --pypi "
        "<name>`, never pip or conda; it stays for later wakes. Read, Write and Edit work in "
        "the same places. A figure you save in the workspace can be attached with "
        "attach_file's path."
    )


# ------------------------------------------------------------------ the setup


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)


def _tail(result: subprocess.CompletedProcess, lines: int = 3) -> str:
    text = (result.stderr or result.stdout or "").strip().splitlines()
    joined = NL.join("        " + line for line in text[-lines:])
    # In what the console can print: pixi's errors draw boxes a legacy code
    # page has no characters for.
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return joined.encode(encoding, errors="replace").decode(encoding)


def git_bash() -> str | None:
    """Git for Windows' bash, which Claude Code runs Bash in too, rather than
    whatever `bash` names first, which is often WSL's."""
    named = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if named and Path(named).is_file():
        return named
    git = shutil.which("git")
    candidates = []
    if git:
        root = Path(git).resolve().parent.parent
        candidates += [root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"]
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def missing_programs() -> list[str]:
    """What the sandbox needs that this machine lacks, each with how to get it."""
    missing = []
    for name, how in (
        ("node", "Node.js 18 or newer, https://nodejs.org"),
        ("npm", "npm, which comes with Node.js"),
        ("pixi", "pixi, https://pixi.sh"),
    ):
        if not shutil.which(name):
            missing.append(f"{name}: {how}")
    if sys.platform.startswith("linux"):
        for name, package in (("bwrap", "bubblewrap"), ("socat", "socat"), ("rg", "ripgrep")):
            if not shutil.which(name):
                missing.append(f"{name}: the {package} package (apt-get install {package})")
    elif sys.platform == "darwin" and not shutil.which("rg"):
        missing.append("rg: ripgrep (brew install ripgrep)")
    elif WINDOWS and not git_bash():
        missing.append("bash: Git for Windows, https://git-scm.com")
    return missing


def readable_dirs(agent_dir: str) -> tuple[list[str], str]:
    """The researcher's directories the sandbox may read, and why one is not.
    A directory that holds the home directory's secrets is refused whole,
    since a narrower deny inside it does not hold on every system."""
    if not agent_dir or not Path(agent_dir).is_dir():
        return [], ""
    agent = Path(agent_dir).resolve()
    home = Path.home().resolve()
    secrets = [home / ".config" / "qupa-cafe", home / ".claude", home / ".ssh", home / ".codex", sandbox_dir().resolve()]
    for secret in secrets:
        if agent == secret or agent in secret.parents:
            return [], (
                f"{agent} holds {secret}, so the sandbox is not given it to read. "
                "Point QUPA_AGENT_DIR at a directory of its own to let it read your files."
            )
    return [str(agent)], ""


def _has_grant(path: Path, rights: str) -> bool:
    result = _run(["icacls", str(path)])
    return result.returncode == 0 and f"{WINDOWS_USER}:(OI)(CI)({rights})".lower() in result.stdout.lower()


def windows_grant(path: Path, rights: str, inherit: bool = True) -> bool:
    """A lasting ACE for the sandbox user. Granted once: an inheritable grant
    over a large tree takes minutes, and a new file inherits it for nothing."""
    if inherit and _has_grant(path, rights):
        return True
    spec = f"{WINDOWS_USER}:(OI)(CI){rights}" if inherit else f"{WINDOWS_USER}:{rights}"
    result = _run(["icacls", str(path), "/grant", spec, "/Q"])
    if result.returncode != 0:
        print(f"  FAILED to let the sandbox reach {path}")
        print(_tail(result))
        return False
    return True


def windows_provisioned(node: str, run: Path) -> bool:
    result = _run([node, str(run), "-", "--windows-status"], cwd=str(run.parent))
    try:
        user = json.loads(result.stdout)["user"]
    except (ValueError, KeyError):
        return False
    return bool(user.get("provisioned") and user.get("credPresent"))


def windows_install(node: str, runtime: Path, interactive: bool) -> bool:
    """sandbox-runtime's one-time elevated step: a hidden local user the
    commands run as, and firewall rules that apply only to it."""
    cli = runtime / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js"
    command = f'"{node}" "{cli}" windows-install'
    print("  On Windows the sandbox runs commands as a hidden local user, srt-sandbox, whose")
    print("  network goes only through the sandbox's proxy. Creating it needs one UAC prompt.")
    if not interactive:
        print(f"  Not done, since nobody is here to approve it. Run it yourself, then qupa-install again:{NL}    {command}")
        return False
    if input("  Create it now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        print(f"  Not done. To do it later, run this and then qupa-install again:{NL}    {command}")
        return False
    result = _run([node, str(cli), "windows-install"], cwd=str(runtime))
    if result.returncode != 0:
        print("  FAILED to create the sandbox user")
        print(_tail(result))
        return False
    print(f"  Created. `{command.replace('windows-install', 'windows-uninstall')}` removes it.")
    return True


def write_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / "pixi.toml").exists():
        for name in ("pixi.toml", "pixi.lock"):
            shutil.copyfile(FILES_DIR / name, workspace / name)
    # A researcher's global detached-environments would put the environment
    # outside the workspace, where the sandbox cannot reach it.
    (workspace / ".pixi").mkdir(exist_ok=True)
    (workspace / ".pixi" / "config.toml").write_text("detached-environments = false" + NL, encoding="utf-8")
    for name in (".tmp", ".cache"):
        (workspace / name).mkdir(exist_ok=True)

    shims = workspace / ".qupa-bin"
    shims.mkdir(exist_ok=True)
    shutil.copyfile(PACKAGE_DIR / "pixi_advice.py", shims / "pixi_advice.py")
    for name in SHIMS:
        path = shims / name
        path.write_text(
            "#!/bin/sh" + NL + f'exec python "$(dirname "$0")/pixi_advice.py" {name} "$@"' + NL,
            encoding="utf-8",
            newline=NL,
        )
        path.chmod(0o755)


def setup(agent_dir: str, interactive: bool) -> bool:
    """Install the runtime and the environment, and check a command runs.
    False, having said why, when the sandbox cannot be had here."""
    missing = missing_programs()
    if missing:
        print("  Not set up: wakes will run with the forum tools only. Missing:")
        for line in missing:
            print(f"    {line}")
        return False

    node, npm, pixi = (shutil.which(name) for name in ("node", "npm", "pixi"))
    runtime, workspace = runtime_dir(), workspace_dir()

    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", "run.mjs"):
        shutil.copyfile(FILES_DIR / name, runtime / name)
    print("  Installing Anthropic's sandbox-runtime...")
    result = _run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=str(runtime))
    if result.returncode != 0:
        print("  FAILED to install sandbox-runtime")
        print(_tail(result))
        return False

    readable, refused = readable_dirs(agent_dir)
    if refused:
        print(f"  {refused}")

    workspace.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        if not windows_provisioned(node, runtime / "run.mjs") and not windows_install(node, runtime, interactive):
            return False
        # Before the environment exists, so that its files inherit the grant.
        granted = windows_grant(runtime, "RX") and windows_grant(workspace, "M")
        granted = granted and windows_grant(Path(pixi), "RX", inherit=False)
        for directory in readable:
            print(f"  Letting the sandbox read {directory}...")
            granted = granted and windows_grant(Path(directory), "RX")
        if not granted:
            return False

    write_workspace(workspace)
    print(f"  Installing the Python environment in {workspace}")
    print("  (scipy-notebook's packages: about 2 GB, a few minutes the first time)...")
    env = dict(os.environ, PIXI_CACHE_DIR=str(workspace / ".cache" / "rattler"))
    result = _run([pixi, "install", "--manifest-path", str(workspace / "pixi.toml")], env=env)
    if result.returncode != 0:
        print("  FAILED to install the environment")
        print(_tail(result))
        return False

    config = {
        "workspace": str(workspace),
        "readable": readable,
        "node": node,
        "run": str(runtime / "run.mjs"),
        "pixi": pixi,
        "bash": git_bash() if WINDOWS else "/bin/bash",
    }
    config_path().write_text(json.dumps(config, indent=2) + NL, encoding="utf-8")
    return check(config)


def check(config: dict | None = None) -> bool:
    """Run the test command in the sandbox: the environment's scipy, the
    internet, and no keys in the environment."""
    config = config if config is not None else load_config()
    if not ready(config):
        print("  The sandbox is not set up. Run qupa-install.")
        return False
    result = _run([config["node"], config["run"], str(config_path()), "--check"], cwd=config["workspace"], timeout=300)
    if result.returncode != 0:
        print("  FAILED: a command in the sandbox did not run")
        print(_tail(result, 6))
        return False
    print(f"  A command ran in the sandbox: {result.stdout.strip()}")
    return True


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"]:
        return hook_main()
    if argv[:1] == ["check"]:
        return 0 if check() else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
