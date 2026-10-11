"""The sandbox a woken agent runs commands in.

Every wake gets one, whichever the runtime. Its commands get open internet
access; reads and writes in the sandbox's workspace and in the researcher's
``QUPA_AGENT_DIR``, except that what steers later wakes stays read-only; and
none of the variables that hold keys. Python there is a pixi workspace with
the packages of Jupyter's scipy-notebook image (``sandbox_files/pixi.toml``),
already active.

A Claude Code wake's Bash goes through Anthropic's sandbox-runtime
(``sandbox_files/run.mjs``), which also keeps the rest of the home directory
unreadable apart from the programs it runs. The daemon hands the wake a
``PreToolUse`` hook, which is this module run as a script. For Bash it
approves the command rewritten to run inside the sandbox, or refuses a pip or
conda install with the pixi command to run instead (``qupa_cafe.pixi_advice``).
For the file tools (``READ_TOOLS`` and ``WRITE_TOOLS``) it approves a path the
sandbox could reach the same way, and leaves any other path to the
researcher's own Claude Code settings.

A Codex wake's commands go through Codex's own sandbox, the one ``codex
sandbox`` runs a command in, under a permission profile the daemon passes to
``codex exec`` (``codex_args``). It denies the home directory's secrets by
name; on Windows the rest of the home directory stays readable, since a
profile there narrows reads only by denying them. Its hook only refuses pip
and conda installs. A protected name that does not exist yet can be created
inside Codex's sandbox, so setup creates each missing protected file empty.

``qupa-install`` sets it up with ``setup`` and stops when it cannot; the
daemon wakes nobody without it.

    python -m qupa_cafe.sandbox check     # run a test command in the sandbox
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
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


def env_python(workspace: Path) -> Path:
    return env_dir(workspace) / ("python.exe" if WINDOWS else "bin/python")


def load_config() -> dict | None:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ready(config: dict | None = None, runtime: str = "") -> bool:
    """Whether a wake can be given the sandbox: set up for ``runtime`` (the
    one it was set up for, when not named), and still there."""
    config = config if config is not None else load_config()
    if not config or (runtime and config.get("runtime", "claude") != runtime):
        return False
    needed = [config.get("pixi", ""), str(env_dir(Path(config.get("workspace", ""))))]
    if config.get("runtime", "claude") == "codex":
        needed.append(config.get("codex", ""))
    else:
        needed += [
            config.get("node", ""),
            config.get("run", ""),
            str(Path(config.get("run", "")).parent / "node_modules" / "@anthropic-ai" / "sandbox-runtime"),
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
    writable = [config["workspace"], *agent_dirs(config)]
    protected = list(config.get("protected") or [])
    # Elsewhere the researcher's own settings decide, and an unattended run
    # usually refuses; the context says where to go instead, since Claude
    # Code's refusal names no alternative.
    if tool in WRITE_TOOLS:
        path = arguments.get(WRITE_TOOLS[tool])
        if path and within(str(path), protected, cwd):
            return context(f"{path} steers your wakes, so it is read-only here; your researcher edits it.")
        if path and within(str(path), writable, cwd):
            return answer("allow", "Writable from the sandbox.")
        return context(f"{path} is outside what the sandbox writes: {', '.join(writable)}.")
    if tool in READ_TOOLS:
        path = arguments.get(READ_TOOLS[tool]) or cwd
        if within(str(path), writable, cwd):
            return answer("allow", "Readable from the sandbox.")
        return context(f"{path} is outside what the sandbox reads: {', '.join(writable)}.")
    return None


def agent_dirs(config: dict) -> list[str]:
    """The researcher's directories the sandbox reads and writes."""
    return list(config.get("agent_dirs") or [])


def context(text: str) -> dict:
    """No decision, and a note for the model beside whatever happens."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def hook_main(codex: bool = False) -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    result = (codex_decide if codex else decide)(event, load_config())
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


# ------------------------------------------------------------- Codex's sandbox

# The permission profile a Codex wake runs under, by the name `codex exec`
# and `codex sandbox` are given.
CODEX_PROFILE = "qupa-cafe"

# Kept out of a Codex command's environment, as run.mjs keeps them out of a
# Claude Code one: what looks like a credential, and what would make its
# Python something other than the workspace's. Codex's own default excludes
# (any name holding KEY, SECRET or TOKEN) apply as well.
CODEX_EXCLUDE = (
    "QUPA_*", "ANTHROPIC_*", "CLAUDE*", "OPENAI_*", "CODEX_*", "AWS_*", "AZURE_*", "GOOGLE_*", "GCLOUD_*",
    "GH_*", "GITHUB_*", "HF_*", "HUGGING*", "NPM_*", "PYPI_*", "TWINE_*", "SSH_AUTH*", "*PASSW*", "*CREDENTIAL*",
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "CONDA_*", "PIXI_*", "PIP_*",
    "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_*",
)


def toml(value) -> str:
    """``value`` as a TOML inline value, for a `-c` override: strings are
    JSON's, whose escapes TOML's basic strings share."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(key))}={toml(item)}" for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml(item) for item in value) + "]"
    return json.dumps(str(value))


def home_secrets() -> list[Path]:
    """What in the home directory no sandbox reads: the kit's key, the
    runtimes' own credentials, ssh keys, and the sandbox's own directory."""
    home = Path.home().resolve()
    return [home / ".config" / "qupa-cafe", home / ".claude", home / ".ssh", home / ".codex", sandbox_dir().resolve()]


def codex_profile(config: dict) -> dict:
    """The permission profile: the system's minimum and the programs read,
    the workspace and the agent's directory written, what steers wakes read
    only, the secrets denied, and the internet open."""
    filesystem: dict = {":minimal": "read"}
    for directory in config.get("programs") or []:
        filesystem[directory] = "read"
    # Not the sandbox's own directory, which holds the workspace: on Windows
    # a deny there outranks the workspace's write.
    for secret in home_secrets()[:-1]:
        filesystem[str(secret)] = "deny"
    filesystem[":workspace_roots"] = {".": "write"}
    for path in config.get("protected") or []:
        filesystem[path] = "read"
    return {
        "workspace_roots": {config["workspace"]: True},
        "filesystem": filesystem,
        "network": {"enabled": True},
    }


def pixi_environment(config: dict) -> dict[str, str]:
    """The workspace's environment activated, as pixi gives its variables."""
    workspace = config["workspace"]
    result = _run(
        [config["pixi"], "shell-hook", "--json", "--manifest-path", str(Path(workspace) / "pixi.toml")],
        env=dict(os.environ, PIXI_CACHE_DIR=str(Path(workspace) / ".cache" / "rattler")),
        timeout=120,
    )
    try:
        variables = json.loads(result.stdout)["environment_variables"]
    except (ValueError, KeyError, TypeError):
        return {}
    return {str(name): str(value) for name, value in variables.items()}


def codex_environment(config: dict) -> dict:
    """The `shell_environment_policy`: this environment without its
    credentials, and with the workspace's pixi environment active and the
    workspace as HOME."""
    workspace = Path(config["workspace"])
    scratch = str(workspace / ".tmp")
    settings = pixi_environment(config)
    settings.update({
        "HOME": str(workspace), "TMPDIR": scratch, "TMP": scratch, "TEMP": scratch,
        "PIXI_CACHE_DIR": str(workspace / ".cache" / "rattler"),
        # No display: a figure is saved, never shown.
        "MPLBACKEND": "Agg",
    })
    return {"inherit": "all", "ignore_default_excludes": False, "exclude": list(CODEX_EXCLUDE), "set": settings}


def codex_hook_command() -> str:
    """The hook as Codex runs it. On Windows Codex runs it in PowerShell,
    which reads a quoted path followed by arguments as an error unless it is
    called with `&`; a hook that cannot start is skipped, not refused."""
    command = f'"{posix(sys.executable)}" "{posix(str(Path(__file__).resolve()))}" codex-hook'
    return f"& {command}" if WINDOWS else command


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def write_codex_profile(config: dict) -> Path:
    """The sandbox's settings as a Codex config profile, `<CODEX_HOME>/
    qupa-cafe.config.toml`, which `-p qupa-cafe` layers over the researcher's
    own configuration: the permission profile and its selection, the
    environment, and the hook. A file, since on the command line the
    environment's PATH alone can pass Windows' limit on its length. Written
    again for each session, so that the environment is the one pixi gives now."""
    hooks = [{"matcher": "Bash", "hooks": [{"type": "command", "command": codex_hook_command(), "timeout": 30}]}]
    lines = [
        "# Written by the qupa-cafe agent kit for each wake; changes here are lost.",
        f"default_permissions = {toml(CODEX_PROFILE)}",
        'approval_policy = "never"',
        f"permissions = {toml({CODEX_PROFILE: codex_profile(config)})}",
        f"shell_environment_policy = {toml(codex_environment(config))}",
        f"hooks = {toml({'PreToolUse': hooks})}",
    ]
    path = codex_home() / f"{CODEX_PROFILE}.config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(NL.join(lines) + NL, encoding="utf-8")
    return path


def codex_overrides(config: dict) -> list[str]:
    """What puts a Codex session's commands in the sandbox, for `codex exec`
    and `codex sandbox` alike: the profile file, and on Windows the sandbox
    that enforces reads."""
    write_codex_profile(config)
    overrides = ["-p", CODEX_PROFILE]
    if WINDOWS:
        overrides += ["-c", 'windows.sandbox="elevated"']
    return overrides


def codex_args(config: dict, agent_dir: str = "") -> list[str]:
    """The arguments a sandboxed `codex exec` gets before its prompt. Codex
    approves nothing unattended, so the forum's servers are approved here,
    and the hook, from the kit's profile, runs without Codex's trust."""
    cwd = (agent_dirs(config) or [config["workspace"]])[0]
    args = ["--dangerously-bypass-hook-trust", "-C", cwd, *codex_overrides(config)]
    for server in ("qupa-cafe", "discourse"):
        args += ["-c", f'mcp_servers.{server}.default_tools_approval_mode="approve"']
    return args


def codex_decide(event: dict, config: dict | None) -> dict | None:
    """The Codex hook's answer to one command: refused when the sandbox is
    gone or it installs with pip or conda, and otherwise left to Codex,
    whose sandbox it runs in."""
    if event.get("tool_name") != "Bash":
        return None

    def deny(reason: str) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    if not config or not ready(config, "codex"):
        return deny("The sandbox is not set up on this machine, so no command can run. "
                    "Your researcher sets it up with qupa-install.")
    advice = pixi_advice.advise(str((event.get("tool_input") or {}).get("command") or ""), config["workspace"])
    return deny(advice) if advice else None


def codex_placeholders(config: dict) -> list[Path]:
    """Each protected file that does not exist yet, created empty: Codex's
    sandbox keeps an existing path read-only, but lets a missing one be
    created. A protected directory is left alone, since an empty `.venv` is
    not one."""
    made = []
    for name in config.get("protected") or []:
        path = Path(name)
        if path.suffix in (".md", ".json") and path.parent.is_dir() and not path.exists():
            path.write_text("{}" + NL if path.suffix == ".json" else "", encoding="utf-8")
            made.append(path)
    return made


def wake_notice(config: dict, agent_dir: str = "") -> str:
    """What a sandboxed wake can do and how, for the end of its prompt."""
    workspace = config["workspace"]
    dirs = agent_dirs(config)
    if agent_dir and Path(agent_dir).is_dir():
        agent_dir = str(Path(agent_dir).resolve())
    if agent_dir and agent_dir in dirs:
        where = (
            f"Every command starts in your working directory, {agent_dir}, and you can write there "
            f"and in {workspace}, which is HOME there and keeps scratch files; put them in $TMPDIR, "
            "which is in it, since /tmp may not be writable."
        )
        names = sorted({Path(path).name for path in config.get("protected") or []})
        if names:
            where += (
                " What steers your wakes is read-only in your working directory: "
                + ", ".join(names)
                + "; your researcher edits those."
            )
        if WINDOWS and config.get("runtime", "claude") == "claude":
            where += (
                f" A command cannot create a file or folder directly in {agent_dir}, only in a "
                "folder already there; Write can."
            )
    else:
        where = (
            f"Every command starts in {workspace}, which is also HOME there; it is the only place you "
            "can write, and what you leave there is kept for later wakes; put scratch files in "
            "$TMPDIR, which is there too, since /tmp may not be writable."
        )
        if agent_dir:
            where += f" Your working directory, {agent_dir}, is outside the sandbox."
    python = (
        "Python there is a pixi environment with the scipy-notebook packages (numpy, scipy, pandas, "
        "matplotlib, sympy, scikit-learn, statsmodels and others), already active, so run python "
        "directly. To add a package run `pixi add <name>` from conda-forge or `pixi add --pypi "
        "<name>` from PyPI, never pip or conda; it stays for later wakes."
    )
    if config.get("runtime", "claude") == "codex":
        windows = (
            " Windows' own TLS does not work in the sandbox, so curl and PowerShell cannot fetch an https "
            "URL; fetch it with Python."
        ) if WINDOWS else ""
        return (
            f"Each command you run goes through Codex's sandbox, with open internet access. {where} "
            f"{python}{windows} matplotlib draws without a display: save a figure with savefig, look at it "
            "with view_image, and attach it to a post with attach_file's path. The whole wake is stopped "
            "after fifteen minutes."
        )
    return (
        "Bash runs each command in a sandbox with open internet access, though not to this "
        f"machine's or its network's own addresses. {where} Give Write and Edit absolute paths. "
        f"{python} matplotlib draws without "
        "a display: save a figure with savefig, look at it with Read, and attach it to a post with "
        "attach_file's path. A command is stopped after two minutes unless you give Bash a longer "
        "timeout, up to ten, and the whole wake after fifteen."
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
    # Git's own installer, for every user and for one, and the copy
    # install_portable_git unpacks.
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
        if base:
            candidates.append(Path(base) / "Git" / "bin" / "bash.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Programs" / "Git" / "bin" / "bash.exe")
    candidates.append(portable_git_dir() / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def portable_git_dir() -> Path:
    return sandbox_dir().parent / "git"


GIT_RELEASE_API = "https://api.github.com/repos/git-for-windows/git/releases/latest"


def install_portable_git() -> str | None:
    """Git for Windows' portable build from its latest release, checked
    against the SHA-256 GitHub publishes for it, unpacked into the kit's own
    directory. No installer runs, nothing is elevated, and nothing outside
    that directory changes. Returns its bash."""
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "64-bit"
    try:
        with urllib.request.urlopen(GIT_RELEASE_API, timeout=30) as response:
            release = json.load(response)
        asset = next(
            item for item in release.get("assets", [])
            if item.get("name", "").startswith("PortableGit-") and item["name"].endswith(f"-{arch}.7z.exe")
        )
        print(f"  downloading {asset['name']} ({asset.get('size', 0) // 2**20} MB)...")
        with urllib.request.urlopen(asset["browser_download_url"], timeout=600) as response:
            data = response.read()
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"  FAILED to download Git for Windows: {exc}")
        return None
    if asset.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest():
        print("  FAILED: the Git for Windows download does not match its published checksum, so it was not used.")
        return None

    target = portable_git_dir()
    archive = target.parent / asset["name"]
    target.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    try:
        # A 7-Zip self-extractor: -y answers its prompts, -o names where.
        result = _run([str(archive), "-y", f"-o{target}"], timeout=900)
    finally:
        archive.unlink(missing_ok=True)
    bash = target / "bin" / "bash.exe"
    if result.returncode != 0 or not bash.is_file():
        print("  FAILED to unpack Git for Windows")
        print(_tail(result))
        return None
    # Its first login shell finishes the setup a portable copy defers.
    _run([str(bash), "-lc", "true"], timeout=300)
    print(f"  installed Git for Windows in {target}")
    return str(bash)


def pixi_bin() -> Path:
    """Where pixi puts itself and what `pixi global install` exposes."""
    return Path(os.environ.get("PIXI_HOME") or Path.home() / ".pixi") / "bin"


def find(name: str) -> str | None:
    """A program on PATH, or one this kit installed into pixi's bin directory,
    which need not be on PATH."""
    found = shutil.which(name)
    if found:
        return found
    for candidate in (pixi_bin() / name, pixi_bin() / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


# Node.js, which the Discourse MCP server needs whether or not there is a
# sandbox, by command and the conda-forge package that provides it.
NODE = {"node": "nodejs", "npm": "nodejs"}


def wanted_programs(runtime: str = "claude") -> dict[str, str]:
    """The commands the sandbox needs on this system beside pixi, and the
    conda-forge package that provides each. Codex brings its own sandbox."""
    wanted = dict(NODE)
    if runtime == "codex":
        return wanted
    if sys.platform.startswith("linux"):
        wanted.update({"bwrap": "bubblewrap", "socat": "socat", "rg": "ripgrep"})
    elif sys.platform == "darwin":
        wanted["rg"] = "ripgrep"
    return wanted


PIXI_RELEASES = "https://github.com/prefix-dev/pixi/releases/latest/download/"


def pixi_archive() -> str | None:
    """The name of pixi's release archive for this machine, or None."""
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    if arch is None:
        return None
    if WINDOWS:
        return f"pixi-{arch}-pc-windows-msvc.zip"
    if sys.platform == "darwin":
        return f"pixi-{arch}-apple-darwin.tar.gz"
    if sys.platform.startswith("linux"):
        return f"pixi-{arch}-unknown-linux-musl.tar.gz"
    return None


def install_pixi() -> str | None:
    """pixi's own release, checked against its published SHA-256, unpacked
    into pixi's bin directory. Nothing else is changed: no shell profile and
    no PATH, since this kit finds it there itself."""
    name = pixi_archive()
    if name is None:
        print(f"  pixi publishes no build for this machine ({platform.system()} {platform.machine()}).")
        return None
    try:
        with urllib.request.urlopen(PIXI_RELEASES + name, timeout=120) as response:
            data = response.read()
        with urllib.request.urlopen(PIXI_RELEASES + name + ".sha256", timeout=30) as response:
            expected = response.read().decode("ascii", "replace").split()[0].lower()
    except (OSError, IndexError) as exc:
        print(f"  FAILED to download pixi: {exc}")
        return None
    if hashlib.sha256(data).hexdigest() != expected:
        print("  FAILED: the pixi download does not match its published checksum, so it was not used.")
        return None

    target = pixi_bin()
    target.mkdir(parents=True, exist_ok=True)
    binary = "pixi.exe" if WINDOWS else "pixi"
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                content = archive.read(binary)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                member = next(m for m in archive.getmembers() if Path(m.name).name == binary and m.isfile())
                content = archive.extractfile(member).read()
    except (KeyError, StopIteration, OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print(f"  FAILED to unpack pixi: {exc}")
        return None
    path = target / binary
    path.write_bytes(content)
    path.chmod(0o755)
    print(f"  installed pixi in {target}")
    return str(path)


def ensure_programs(runtime: str = "claude") -> str | None:
    """Install what is missing and can be had without elevation, and only
    that: on Windows, for Claude Code, Git for Windows' bash; pixi; and
    through `pixi global install` Node.js, which the Discourse server needs,
    and, for Claude Code's sandbox, ripgrep, socat and bubblewrap. All of it
    for this user alone. Returns the bash this kit unpacked, which Claude Code
    is then told of, or None."""
    unpacked = None
    if WINDOWS and runtime == "claude" and not git_bash():
        unpacked = install_portable_git()
    packages = sorted({package for name, package in wanted_programs(runtime).items() if not find(name)})
    if not packages and find("pixi"):
        return unpacked
    pixi = find("pixi") or install_pixi()
    if pixi is None or not packages:
        return unpacked
    print(f"  installing {', '.join(packages)} with pixi, into {pixi_bin()}...")
    result = _run([pixi, "global", "install", *packages])
    if result.returncode != 0:
        print(f"  FAILED to install {', '.join(packages)}")
        print(_tail(result))
    return unpacked


def missing_programs(runtime: str = "claude") -> list[str]:
    """What the sandbox needs that this machine still lacks, each with how to get it."""
    missing = []
    if not find("pixi"):
        missing.append("pixi: https://pixi.sh")
    for name, package in wanted_programs(runtime).items():
        if not find(name):
            missing.append(f"{name}: the {package} package, from conda-forge or your system's package manager")
    if runtime == "codex" and not shutil.which("codex"):
        missing.append("codex: Codex, https://developers.openai.com/codex")
    if runtime == "claude" and WINDOWS and not git_bash():
        missing.append("bash: Git for Windows, https://git-scm.com")
    return missing


def secret_within(directory: Path) -> Path | None:
    """The first of the home directory's secrets that ``directory`` holds, or
    None. A directory holding one is never made readable to the sandbox,
    since a narrower deny inside it does not hold on every system."""
    directory = directory.resolve()
    for secret in home_secrets():
        if directory == secret or directory in secret.parents:
            return secret
    return None


def program_dirs(runtime: str = "claude") -> list[str]:
    """Where the sandbox's programs come from, for it to read: each one's own
    directory, and pixi's install, whose exposed commands lead into the
    environments beside its bin directory. None that holds a secret."""
    dirs = {Path(found).parent for name in [*wanted_programs(runtime), "pixi"] if (found := find(name))}
    if pixi_bin().is_dir():
        dirs.add(pixi_bin().parent)
    return sorted(str(d) for d in dirs if secret_within(d) is None)


def writable_agent_dirs(agent_dir: str) -> tuple[list[str], str]:
    """The researcher's directories the sandbox may read and write, and why
    one is not."""
    if not agent_dir or not Path(agent_dir).is_dir():
        return [], ""
    agent = Path(agent_dir).resolve()
    secret = secret_within(agent)
    if secret is not None:
        return [], (
            f"{agent} holds {secret}, so the sandbox is not given it. "
            "Point QUPA_AGENT_DIR at a directory of its own to let your agent work in your files."
        )
    return [str(agent)], ""


# In the agent's directory, what steers later wakes or runs outside the
# sandbox, kept read-only to it: the researcher's instructions, Claude Code's
# and Codex's own settings and hooks, and the git hooks the next commit runs.
PROTECTED_NAMES = (
    "QUPA.md", ".claude", ".codex", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md",
    ".venv", "venv", ".git/hooks", ".git/config",
)


def protected_paths(directories: list[str]) -> list[str]:
    """PROTECTED_NAMES in each directory, and this kit's own interpreter and
    code wherever one of them holds it, since the MCP server that enforces the
    policy runs from there."""
    paths = set()
    own = [Path(sys.prefix).resolve(), PACKAGE_DIR.parent]
    for directory in directories:
        top = Path(directory).resolve()
        paths.update(str(top / name) for name in PROTECTED_NAMES)
        paths.update(str(path) for path in own if path == top or top in path.parents)
    return sorted(paths)


def _has_grant(path: Path, rights: str, kind: str = "(OI)(CI)", deny: bool = False) -> bool:
    result = _run(["icacls", str(path)])
    ace = f"{WINDOWS_USER}:{kind}{'(DENY)' if deny else ''}({rights})".lower()
    return result.returncode == 0 and ace in result.stdout.lower()


def windows_deny(path: Path, rights: str, kind: str) -> bool:
    """A lasting DENY ACE for the sandbox user, which outranks its grants."""
    if _has_grant(path, rights, kind, deny=True):
        return True
    result = _run(["icacls", str(path), "/deny", f"{WINDOWS_USER}:{kind}({rights})", "/Q"])
    if result.returncode != 0:
        print(f"  FAILED to keep the sandbox from changing {path}")
        print(_tail(result))
        return False
    return True


def windows_protect(directory: Path, protected: list[str]) -> bool:
    """Keep the sandbox from changing what steers wakes. What exists gets a
    DENY of writing and deleting; what does not yet exist cannot carry an ACE,
    so the directory itself refuses new entries at its top level (a folder
    already there takes them), which is where those names are read."""
    ok = windows_deny(directory, "WD,AD", "")
    for name in protected:
        path = Path(name)
        if path.is_dir():
            ok = windows_deny(path, "W,D", "(OI)(CI)") and ok
        elif path.is_file():
            ok = windows_deny(path, "W,D", "") and ok
    return ok


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


# The loopback ports the runtime's proxies listen on, which its firewall rule
# lets the sandbox user reach. Below Windows' dynamic range (49152-65535),
# since a port in it can be held by any program's outgoing connection, and
# the runtime's own default range is in it.
WINDOWS_PROXY_PORTS = (47080, 47089)


def windows_marker() -> Path:
    """What this kit last installed the Windows sandbox with."""
    return runtime_dir() / "windows-install.json"


def windows_provisioned(node: str, run: Path) -> bool:
    """Whether the sandbox user exists and its firewall rule admits our ports,
    which only an install made by this kit records."""
    result = _run([node, str(run), "-", "--windows-status"], cwd=str(run.parent))
    try:
        user = json.loads(result.stdout)["user"]
        marker = json.loads(windows_marker().read_text(encoding="utf-8"))
    except (ValueError, KeyError, OSError):
        return False
    ports = list(WINDOWS_PROXY_PORTS)
    return bool(user.get("provisioned") and user.get("credPresent")) and marker.get("ports") == ports


def windows_install(node: str, runtime: Path, interactive: bool) -> bool:
    """sandbox-runtime's one-time elevated step: a hidden local user the
    commands run as, and firewall rules that apply only to it. Its UAC prompt
    is the consent, so a run that asks nothing else (an update) shows it too;
    an interactive one asks first, to say what it is for."""
    cli = runtime / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js"
    command = f'"{node}" "{cli}" windows-install --proxy-port-range {WINDOWS_PROXY_PORTS[0]}-{WINDOWS_PROXY_PORTS[1]} --force'
    print("  On Windows the sandbox runs commands as a hidden local user, srt-sandbox, whose")
    print("  network goes only through the sandbox's proxy. Creating it needs one UAC prompt.")
    if not interactive:
        print("  Approve the UAC prompt to go on; declining it leaves your agent without the sandbox.")
    elif input("  Create it now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        print(f"  Not done. To do it later, run this and then qupa-install again:{NL}    {command}")
        return False
    ports = "-".join(str(port) for port in WINDOWS_PROXY_PORTS)
    # --force: an install made without our ports is replaced, not kept.
    result = _run([node, str(cli), "windows-install", "--proxy-port-range", ports, "--force"], cwd=str(runtime))
    if result.returncode == 2:
        print(f"  Not done: the UAC prompt was declined. To do it later, run this and then qupa-install again:{NL}    {command}")
        return False
    if result.returncode != 0:
        print("  FAILED to create the sandbox user")
        print(_tail(result))
        return False
    windows_marker().write_text(json.dumps({"ports": list(WINDOWS_PROXY_PORTS)}) + NL, encoding="utf-8")
    print(f'  Created. `"{node}" "{cli}" windows-uninstall` removes it.')
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


def setup(agent_dir: str, interactive: bool, runtime: str = "claude") -> bool:
    """Install the sandbox for ``runtime`` and the environment, and check a
    command runs. False, having said why, when the sandbox cannot be had here."""
    # Written again only once everything below has worked, so a setup that
    # fails part-way leaves no sandbox half replaced.
    config_path().unlink(missing_ok=True)
    missing = missing_programs(runtime)
    if missing:
        print("  Not set up. Missing:")
        for line in missing:
            print(f"    {line}")
        return False

    node, npm, pixi = (find(name) for name in ("node", "npm", "pixi"))
    workspace = workspace_dir()
    writable, refused = writable_agent_dirs(agent_dir)
    if refused:
        print(f"  {refused}")
    protected = protected_paths(writable)
    workspace.mkdir(parents=True, exist_ok=True)

    if runtime == "claude" and not install_runtime(node, npm, workspace, writable, protected, interactive):
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
        "runtime": runtime,
        "workspace": str(workspace),
        "agent_dirs": writable,
        "protected": protected,
        "pixi": pixi,
        "programs": program_dirs(runtime),
    }
    if runtime == "codex":
        config["codex"] = shutil.which("codex")
        for path in codex_placeholders(config):
            print(f"  Created {path}, empty, so that the sandbox keeps it read-only.")
    else:
        config.update({
            "node": node,
            "run": str(runtime_dir() / "run.mjs"),
            "bash": git_bash() if WINDOWS else "/bin/bash",
            # Where the runtime finds bwrap, socat and rg, which may be only in
            # pixi's bin directory and not on the PATH a wake inherits.
            "path": sorted({str(Path(find(name)).parent) for name in wanted_programs() if find(name)}),
            "windows_ports": list(WINDOWS_PROXY_PORTS),
        })
    config_path().write_text(json.dumps(config, indent=2) + NL, encoding="utf-8")
    return check(config)


def install_runtime(node: str, npm: str, workspace: Path, writable: list[str], protected: list[str],
                    interactive: bool) -> bool:
    """Anthropic's sandbox-runtime, for Claude Code, and on Windows its
    sandbox user's one-time install and grants."""
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json", "run.mjs"):
        shutil.copyfile(FILES_DIR / name, runtime / name)
    print("  Installing Anthropic's sandbox-runtime...")
    result = _run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=str(runtime))
    if result.returncode != 0:
        print("  FAILED to install sandbox-runtime")
        print(_tail(result))
        return False
    if not WINDOWS:
        return True

    if not windows_provisioned(node, runtime / "run.mjs") and not windows_install(node, runtime, interactive):
        return False
    # Before the environment exists, so that its files inherit the grant.
    granted = windows_grant(runtime, "RX") and windows_grant(workspace, "M")
    granted = granted and windows_grant(Path(find("pixi")), "RX", inherit=False)
    # The bash every command runs in. A Git under the user's profile, the
    # copy this kit unpacked or a per-user install, is not readable to the
    # sandbox user until granted; one in Program Files already is.
    bash = git_bash()
    if bash and Path.home().resolve() in Path(bash).resolve().parents:
        granted = granted and windows_grant(Path(bash).resolve().parent.parent, "RX")
    for directory in writable:
        print(f"  Letting the sandbox work in {directory}...")
        granted = granted and windows_grant(Path(directory), "M") and windows_protect(Path(directory), protected)
    return granted


# What the check runs in a Codex sandbox: the environment's scipy, the
# internet, no key in the environment, and the kit's key unreadable.
CODEX_CHECK = NL.join([
    "import os, pathlib, scipy, urllib.request",
    'urllib.request.urlopen("https://pypi.org/simple/", timeout=30).read(1)',
    'assert not [n for n in os.environ if n.startswith("QUPA_") or n.endswith("API_KEY")], "a key reached the sandbox"',
    "try:",
    "    os.listdir(pathlib.Path(KEY_DIR))",
    "except OSError:",
    "    pass",
    "else:",
    '    raise SystemExit("the kit\'s key directory is readable from the sandbox")',
    'print(f"scipy {scipy.__version__}, internet reachable, the key unreadable")',
])


def check(config: dict | None = None) -> bool:
    """Run the test command in the sandbox: the environment's scipy, the
    internet, and no keys in the environment."""
    config = config if config is not None else load_config()
    if not ready(config):
        print("  The sandbox is not set up. Run qupa-install.")
        return False
    if config.get("runtime", "claude") == "codex":
        cwd = (agent_dirs(config) or [config["workspace"]])[0]
        argv = [config["codex"], "sandbox", "-P", CODEX_PROFILE, "-C", cwd, *codex_overrides(config)]
        # A file, since Windows cuts an argument at its first line break, run
        # by the environment's own Python, since the command is looked up on
        # this process's PATH rather than the sandbox's.
        script = Path(config["workspace"]) / ".tmp" / "qupa-check.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        key_dir = str(Path.home() / ".config" / "qupa-cafe")
        script.write_text(f"KEY_DIR = {key_dir!r}{NL}{CODEX_CHECK}{NL}", encoding="utf-8")
        argv += ["--", str(env_python(Path(config["workspace"]))), str(script)]
        result = _run(argv, cwd=cwd, timeout=300)
    else:
        result = _run([config["node"], config["run"], str(config_path()), "--check"], cwd=config["workspace"], timeout=300)
    if result.returncode != 0:
        print("  FAILED: a command in the sandbox did not run")
        print(_tail(result, 6))
        return False
    print(f"  A command ran in the sandbox: {result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ''}")
    return True


def main(argv: list[str]) -> int:
    if argv[:1] in (["hook"], ["codex-hook"]):
        return hook_main(codex=argv[0] == "codex-hook")
    if argv[:1] == ["check"]:
        return 0 if check() else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
