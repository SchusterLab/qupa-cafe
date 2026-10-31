"""The sandbox a woken agent runs commands in.

Every wake gets Bash, and every command it runs goes through Anthropic's
sandbox-runtime (``sandbox_files/run.mjs``): open internet access; reads and
writes in the sandbox's workspace and in the researcher's ``QUPA_AGENT_DIR``,
except that what steers later wakes stays read-only; reads of nothing else but
the system's and its own programs; and none of the variables that hold keys. Python there is a pixi workspace with the packages of Jupyter's
scipy-notebook image (``sandbox_files/pixi.toml``), already active.

The daemon hands the wake a ``PreToolUse`` hook, which is this module run as a
script. For Bash it approves the command rewritten to run inside the sandbox,
or refuses a pip or conda install with the pixi command to run instead
(``qupa_cafe.pixi_advice``). For the file tools (``READ_TOOLS`` and
``WRITE_TOOLS``) it approves a path the sandbox could reach the same way, and
refuses any other.

On Windows the commands run in a Linux distribution in WSL 2, in the same
Linux sandbox, which this module sets up by running itself in the
distribution (``wsl_setup``).

``qupa-install`` sets it up with ``setup``, and stops when it cannot: without
node, npm and pixi, or on Windows without a Linux distribution in WSL 2, it
reports what is missing. The daemon wakes nobody without it.

The researcher can also let wakes read a project of theirs, and not change
it, which the qupa-cafe skill offers in their own sessions:

    python -m qupa_cafe.sandbox check            # run a test command in the sandbox
    python -m qupa_cafe.sandbox read <dir>       # let wakes read a project
    python -m qupa_cafe.sandbox unread <dir>     # and stop
    python -m qupa_cafe.sandbox projects         # list the projects wakes read
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
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):
    # Run as a script, which is how the hook is run: the package's parent, so
    # that this is the same qupa_cafe the daemon imported.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from qupa_cafe import pixi_advice, sessions

PACKAGE_DIR = Path(__file__).resolve().parent
FILES_DIR = PACKAGE_DIR / "sandbox_files"

NL = chr(10)
WINDOWS = os.name == "nt"
# This kit running inside WSL, where Windows hands its commands to the Linux
# sandbox, and where Windows' own PATH is appended to Linux's.
IN_WSL = sys.platform.startswith("linux") and "microsoft" in platform.release().lower()

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
    wsl = config.get("wsl")
    if wsl:
        # The Linux side is not looked at, which would start the distribution
        # on every command; the command fails there if it has gone.
        return all(wsl.get(key) for key in ("exe", "distro", "config", "node", "run")) and Path(wsl["exe"]).is_file()
    if WINDOWS:
        # On Windows commands run only in WSL.
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
    wsl = config.get("wsl")
    if wsl:
        # Git Bash would rewrite an argument that starts with / into a Windows
        # path, and these are Linux paths.
        return " ".join(
            ["MSYS_NO_PATHCONV=1", "MSYS2_ARG_CONV_EXCL='*'", shell_quote(posix(wsl["exe"])), "-d", shell_quote(wsl["distro"]),
             "-e", shell_quote(wsl["node"]), shell_quote(wsl["run"]), shell_quote(wsl["config"]), encoded]
        )
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
    """The hook's answer to one tool call, or None to leave it to Claude
    Code's own permission rules."""
    tool = event.get("tool_name", "")
    arguments = dict(event.get("tool_input") or {})
    cwd = event.get("cwd") or os.getcwd()

    def answer(decision: str, reason: str, updated: dict | None = None) -> dict:
        out = {"hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason}
        if updated is not None:
            out["updatedInput"] = updated
        return {"hookSpecificOutput": out}

    if tool == "SendMessage":
        return handoff(arguments, answer)
    if tool in SLACK_TOOLS:
        return answer("deny", "A wake does not read or write in Slack: hand the post to one of your researcher's "
                              "sessions, or tell them by private message.")
    if tool == REGISTER_TOOL:
        return answer("deny", "A wake is not a session with your researcher, so it does not register.")
    if tool in CATEGORY_TOOLS:
        return answer("deny", "A wake makes no category and adds nobody to one: tell your researcher by private "
                              "message what you would make and why.")

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
    # Elsewhere the file tools are refused, whatever a settings file in the
    # agent's directory approves, so that they reach what a command reaches
    # and no more; the reason says where to go instead.
    if tool in WRITE_TOOLS:
        path = arguments.get(WRITE_TOOLS[tool])
        if path and within(str(path), protected, cwd):
            return answer("deny", f"{path} steers your wakes, so it is read-only here. Your researcher edits it.")
        if path and config.get("wsl") and within(str(path), [config["workspace"]], cwd):
            # Claude Code asks before writing to a network path such as
            # \\wsl.localhost, whatever the hook approves, and a wake has
            # nobody to ask.
            return answer("deny", f"Write files in {config['workspace']} with a command, as "
                                  f"{config['wsl']['workspace']}; Write and Edit work in {', '.join(agent_dirs(config)) or 'no directory here'}.")
        if path and within(str(path), writable, cwd):
            return answer("allow", "Writable from the sandbox.")
        return answer("deny", f"{path} is outside what the sandbox writes: {', '.join(writable)}.")
    if tool in READ_TOOLS:
        path = arguments.get(READ_TOOLS[tool]) or cwd
        readable = [*writable, *read_dirs(config)]
        if within(str(path), readable, cwd):
            return answer("allow", "Readable from the sandbox.")
        return answer("deny", f"{path} is outside what the sandbox reads: {', '.join(readable)}.")
    return None


# The qupa-cafe tool a session registers with, which a wake may not call.
REGISTER_TOOL = "mcp__qupa-cafe__register_session"
# Nor may it make a category, or change who reads one.
CATEGORY_TOOLS = ("mcp__qupa-cafe__create_category", "mcp__qupa-cafe__add_to_category")
SLACK_TOOLS = tuple(f"mcp__qupa-cafe__slack_{name}" for name in ("channels", "read", "post", "message"))


def handoff(arguments: dict, answer) -> dict:
    """Let a wake message one of the researcher's registered sessions, and
    only to hand it a post the first line names, once for each post; record
    the handoff for the daemon. Anything else is refused, so that a post
    cannot have a wake write to any other session on the machine."""
    name = sessions.bare(arguments.get("to", ""))
    message = str(arguments.get("message") or "")
    open_now = sessions.live()
    if name not in open_now:
        listed = ", ".join(open_now) or "none is open now"
        return answer("deny", f"A wake messages only your researcher's registered sessions ({listed}), "
                              "to hand one a post.")
    where = sessions.handoff_of(message)
    if where is None:
        return answer("deny", "A handoff's first line is exactly \"" + sessions.HANDOFF.format(
            topic_id="<topic id>", post_number="<post number>") + "\".")
    earlier = sessions.handed(*where)
    if earlier is not None:
        return answer("deny", f"This post was handed to {earlier['session']} already. Answer it from what "
                              "you can read, or ask your researcher by private message for what would help.")
    sessions.hand(*where, name)
    return answer("allow", f"Hands topic {where[0]} post {where[1]} to {name}.")


def agent_dirs(config: dict) -> list[str]:
    """The researcher's directories the sandbox reads and writes."""
    return list(config.get("agent_dirs") or [])


def read_dirs(config: dict) -> list[str]:
    """The researcher's projects the sandbox reads and cannot change."""
    return list(config.get("read_dirs") or [])


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
    matcher = "|".join(["Bash", *READ_TOOLS, *WRITE_TOOLS, "SendMessage", REGISTER_TOOL, *SLACK_TOOLS, *CATEGORY_TOOLS])
    return json.dumps(
        {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [{"type": "command", "command": hook_command(), "timeout": 30}]}]}}
    )


def wake_notice(config: dict, agent_dir: str = "") -> str:
    """What a sandboxed wake can do and how, for the end of its prompt."""
    workspace = config["workspace"]
    dirs = agent_dirs(config)
    if agent_dir and Path(agent_dir).is_dir():
        agent_dir = str(Path(agent_dir).resolve())
    if agent_dir and agent_dir in dirs:
        where = (
            f"Commands start in your working directory, {agent_dir}. You can write there and in "
            f"{workspace}, which is HOME in the sandbox and keeps what you leave for later wakes. Put scratch "
            "files in $TMPDIR, since /tmp may not be writable."
        )
        if config.get("protected"):
            where += " QUPA.md, CLAUDE.md and the other files that steer your wakes are read-only there."
    else:
        where = (
            f"Commands start in {workspace}, which is HOME in the sandbox and the only place you can write. "
            "It keeps what you leave for later wakes. Put scratch files in $TMPDIR, since /tmp may not be "
            "writable."
        )
        if agent_dir:
            where += f" Your working directory, {agent_dir}, is outside the sandbox."
    projects = read_dirs(config)
    if projects:
        where += (
            f" You can read, and not change, your researcher's {'project' if len(projects) == 1 else 'projects'} "
            f"{', '.join(projects)}, to answer a post from what is there."
        )
    wsl = config.get("wsl")
    if wsl:
        where += (
            f" Commands run in Linux, under WSL, where a Windows path is under {wsl['mount']}: C:\\Users is "
            f"{wsl['mount']}c/Users there, and {workspace} is {wsl['workspace']}. Give Read, Write and Edit "
            f"the Windows path. Write and Edit cannot change {workspace}; write there with a command."
        )
    return (
        "Bash runs each command in a sandbox with internet access, though not to this machine's or its "
        f"network's own addresses. {where} Give Write and Edit absolute paths. Python is a pixi environment "
        "with the scipy-notebook packages, already active. Add a package with `pixi add <name>` from "
        "conda-forge or `pixi add --pypi <name>`, never with pip or conda. A package you add stays for later "
        "wakes. Save a figure with savefig, look at it with Read, and attach it with attach_file's path. A "
        "command stops after two minutes unless you give Bash a longer timeout, of up to ten minutes. The "
        "whole wake stops after fifteen minutes."
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
    search = None
    if IN_WSL:
        # Not a Windows program WSL appended the directory of: Windows' npm
        # is a shell script that would run Windows' node.
        search = ":".join(d for d in os.environ.get("PATH", "").split(":") if not d.startswith("/mnt/"))
    found = shutil.which(name, path=search)
    if found:
        return found
    for candidate in (pixi_bin() / name, pixi_bin() / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    return None


# Node.js, which the Discourse MCP server needs as well as the sandbox, by
# command and the conda-forge package that provides it.
NODE = {"node": "nodejs", "npm": "nodejs"}


def wanted_programs() -> dict[str, str]:
    """The commands the sandbox needs on this system beside pixi, and the
    conda-forge package that provides each."""
    wanted = dict(NODE)
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


def ensure_programs() -> str | None:
    """Install what is missing and can be had without elevation, and only
    that: on Windows, Git for Windows' bash; pixi; and through `pixi global
    install` Node.js, ripgrep, socat and bubblewrap. All of it for this user
    alone. Returns the bash this kit unpacked, which Claude Code is then told
    of, or None."""
    unpacked = None
    if WINDOWS and not git_bash():
        unpacked = install_portable_git()
    packages = sorted({package for name, package in wanted_programs().items() if not find(name)})
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


# Ubuntu 23.10 and later restrict unprivileged user namespaces through
# AppArmor, and confine /usr/bin/bwrap so that what it starts has no
# capabilities: the runtime's seccomp step, run inside bubblewrap, then fails
# to make its own namespace, whatever the sysctl says. A root-owned copy of
# that bubblewrap at a path of its own, with a profile allowing it user
# namespaces, works with the restriction left on, and is the only exemption.
# Linux paths, named as POSIX paths so that they read the same wherever this runs.
USERNS_SYSCTL = PurePosixPath("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
APPARMOR_ENABLED = PurePosixPath("/sys/module/apparmor/parameters/enabled")
UBUNTU_BWRAP_PROFILE = PurePosixPath("/etc/apparmor.d/bwrap-userns-restrict")
SYSTEM_BWRAP = PurePosixPath("/usr/bin/bwrap")
LINUX_BWRAP = PurePosixPath("/usr/local/libexec/qupa-cafe/bwrap")
LINUX_BWRAP_PROFILE = PurePosixPath("/etc/apparmor.d/qupa-cafe-bwrap")
LINUX_BWRAP_PROFILE_TEXT = NL.join([
    "abi <abi/4.0>,",
    "include <tunables/global>",
    "",
    "# A root-owned copy of bubblewrap for the qupa-cafe sandbox, allowed user namespaces.",
    f"profile qupa-cafe-bwrap {LINUX_BWRAP} flags=(unconfined) {{",
    "  userns,",
    "  include if exists <local/qupa-cafe-bwrap>",
    "}",
])


def userns_restricted() -> bool:
    """Whether this Linux confines user namespaces so that the system's
    bubblewrap cannot run the sandbox."""
    if not sys.platform.startswith("linux"):
        return False
    # A kernel without AppArmor, such as WSL's, enforces no profile that
    # Ubuntu installed.
    try:
        enabled = Path(APPARMOR_ENABLED).read_text(encoding="ascii").strip().upper().startswith("Y")
    except OSError:
        enabled = False
    try:
        restricted = Path(USERNS_SYSCTL).read_text(encoding="ascii").strip() == "1"
    except OSError:
        restricted = False
    return enabled and (restricted or Path(UBUNTU_BWRAP_PROFILE).exists())


def bwrap_exempted() -> bool:
    return Path(LINUX_BWRAP).is_file() and Path(LINUX_BWRAP_PROFILE).is_file()


def bwrap_script_path() -> Path:
    return runtime_dir().parent / "qupa-cafe-bwrap.sh"


def bwrap_script() -> str:
    """The script the researcher runs with sudo, once, to give the sandbox
    its bubblewrap. qupa-install never runs sudo itself, so the password goes
    only to a sudo the researcher started."""
    return NL.join([
        "#!/bin/sh",
        "# Gives the qupa-cafe sandbox a bubblewrap that AppArmor allows user namespaces:",
        f"# a root-owned copy of {SYSTEM_BWRAP} at",
        f"#   {LINUX_BWRAP}",
        "# and a profile for that copy alone at",
        f"#   {LINUX_BWRAP_PROFILE}",
        "# The restriction stays on for everything else. Run it once, with sudo. To undo it:",
        f"#   sudo apparmor_parser -R {LINUX_BWRAP_PROFILE}",
        f"#   sudo rm -r {LINUX_BWRAP.parent} {LINUX_BWRAP_PROFILE}",
        "set -e",
        "umask 022",
        f"[ -x {SYSTEM_BWRAP} ] || apt-get install -y bubblewrap",
        f"install -D -m 755 {SYSTEM_BWRAP} {LINUX_BWRAP}",
        f"cat > {LINUX_BWRAP_PROFILE} <<'EOF'",
        LINUX_BWRAP_PROFILE_TEXT,
        "EOF",
        f"apparmor_parser -r {LINUX_BWRAP_PROFILE}",
        "echo 'Done. Now run qupa-install again.'",
        "",
    ])


def ask_for_bwrap() -> bool:
    """Write the script, say how to run it, and return False: the sandbox
    is set up by the next qupa-install, once the script has run."""
    script = bwrap_script_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(bwrap_script(), encoding="utf-8", newline=NL)
    print("  One step needs sudo, once. Run this, then qupa-install again:")
    print()
    if os.environ.get("QUPA_WSL_FOR_WINDOWS"):
        # Set up from Windows, whose terminal the researcher is in.
        print(f"    wsl -d {os.environ.get('WSL_DISTRO_NAME', '')} -- sudo sh {script}")
    else:
        print(f"    sudo sh {script}")
    print()
    print("  It lets the sandbox use its own copy of bubblewrap under Ubuntu's AppArmor")
    print("  rules. The script is short, and its first lines say what it does.")
    return False


def missing_programs() -> list[str]:
    """What the sandbox needs that this machine still lacks, each with how to get it."""
    missing = []
    if not find("pixi"):
        missing.append("pixi: https://pixi.sh")
    for name, package in wanted_programs().items():
        if not find(name):
            missing.append(f"{name}: the {package} package, from conda-forge or your system's package manager")
    if WINDOWS and not git_bash():
        missing.append("bash: Git for Windows, https://git-scm.com")
    return missing


def secret_within(directory: Path) -> Path | None:
    """The first of the home directory's secrets that ``directory`` holds, or
    None. A directory holding one is never made readable to the sandbox,
    since a narrower deny inside it does not hold on every system."""
    directory = directory.resolve()
    home = Path.home().resolve()
    secrets = [home / ".config" / "qupa-cafe", home / ".claude", home / ".ssh", home / ".codex", sandbox_dir().resolve()]
    for secret in secrets:
        if directory == secret or directory in secret.parents:
            return secret
    return None


def program_dirs() -> list[str]:
    """Where the sandbox's programs come from, for it to read: each one's own
    directory, and pixi's install, whose exposed commands lead into the
    environments beside its bin directory. None that holds a secret."""
    dirs = {Path(found).parent for name in wanted_programs() if (found := find(name))}
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


# On Windows commands run in a Linux distribution in WSL 2, in the Linux
# sandbox, which reads only what it is given. The daemon, Claude Code and the
# file tools stay on Windows: the hook hands each command to wsl.exe. The Linux side is this
# module, run by the distribution's python3 from where Windows has the kit, and
# set up by its Linux code in a directory of its own, WSL_SANDBOX under the
# distribution's home.
WSL_SANDBOX = ".local/share/qupa-cafe/windows-sandbox"


def wsl_exe() -> str | None:
    found = shutil.which("wsl.exe")
    if found:
        return found
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
    return str(system) if system.is_file() else None


def _wsl(distro: str, *argv: str, **kwargs) -> subprocess.CompletedProcess:
    return _run([wsl_exe() or "wsl.exe", "-d", distro, "-e", *argv], **kwargs)


def wsl_distro() -> str | None:
    """The distribution commands run in: QUPA_WSL_DISTRO, else the default
    one, else the first, of those on WSL 2, and not Docker Desktop's, which
    has no shell. None without one."""
    exe = wsl_exe()
    if exe is None:
        return None
    try:
        # WSL_UTF8, or wsl.exe writes UTF-16.
        result = subprocess.run([exe, "-l", "-v"], capture_output=True, env=dict(os.environ, WSL_UTF8="1"), timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace").replace("\x00", "").replace("\ufeff", "")
    found, default = [], None
    for line in text.splitlines()[1:]:
        words = line.replace("*", " * ").split()
        if not words:
            continue
        marked = words[0] == "*"
        words = words[1:] if marked else words
        if len(words) < 3 or words[-1] != "2" or words[0].startswith("docker-desktop"):
            continue
        found.append(words[0])
        if marked:
            default = words[0]
    named = os.environ.get("QUPA_WSL_DISTRO")
    if named:
        return named if named in found else None
    return default or (found[0] if found else None)


def wsl_mount(distro: str) -> str | None:
    """Where the distribution mounts Windows' drives, ending in a slash:
    /mnt/ unless its wsl.conf says otherwise."""
    result = _wsl(distro, "wslpath", "-a", "-u", "C:\\", timeout=120)
    path = result.stdout.strip().rstrip("/")
    return path[:-1] if result.returncode == 0 and path.endswith("/c") else None


def linux_path(path: str, mount: str) -> str | None:
    """A Windows path on a drive as the distribution has it, or None for one
    that is not, such as a network share's."""
    text = str(path)
    if len(text) < 2 or text[1] != ":" or not text[0].isalpha():
        return None
    rest = text[2:].replace("\\", "/").strip("/")
    return f"{mount}{text[0].lower()}" + (f"/{rest}" if rest else "")


def wsl_setup(distro: str, writable: list[str], protected: list[str], projects: list[str]) -> bool:
    """The Linux sandbox in ``distro``, set up by this module there, and the
    configuration that hands it each command."""
    print(f"  Commands will run in Linux, in WSL's {distro}, in the Linux sandbox.")
    exe = wsl_exe()
    home = _wsl(distro, "sh", "-c", 'printf %s "$HOME"', timeout=300).stdout.strip()
    mount = wsl_mount(distro)
    if not home.startswith("/") or mount is None:
        print(f"  FAILED to start {distro}, or to find where it mounts Windows' drives.")
        return False
    given = {"agent_dirs": writable, "protected": protected, "read_dirs": [d for d in projects if d not in writable]}
    linux = {key: [linux_path(path, mount) for path in paths] for key, paths in given.items()}
    script = linux_path(str(Path(__file__).resolve()), mount)
    if script is None or None in linux["agent_dirs"] or None in linux["read_dirs"]:
        print("  FAILED: the kit and the directories it works in must be on a drive, not a network share.")
        return False
    linux["protected"] = [path for path in linux["protected"] if path]

    directory = f"{home}/{WSL_SANDBOX}"
    payload = base64.b64encode(json.dumps(linux).encode("utf-8")).decode("ascii")
    # Not captured: its progress and any question it asks are the researcher's.
    sys.stdout.flush()
    ran = subprocess.run([exe, "-d", distro, "-e", "env", f"QUPA_SANDBOX_DIR={directory}", "QUPA_WSL_FOR_WINDOWS=1",
                          "python3", script, "wsl-setup", payload])
    if ran.returncode != 0:
        if ran.returncode == 127:
            print(f"  {distro} has no python3. Install it there, then run qupa-install again:")
            print(f"    wsl -d {distro} -- sudo apt install python3")
        return False
    try:
        inside = json.loads(_wsl(distro, "cat", f"{directory}/config.json", timeout=120).stdout)
    except ValueError:
        print(f"  FAILED to read the sandbox's configuration in {distro}.")
        return False
    shared = _wsl(distro, "wslpath", "-w", inside["workspace"], timeout=120).stdout.strip()
    config = {
        "wsl": {
            "exe": exe, "distro": distro, "mount": mount, "dir": directory, "config": f"{directory}/config.json",
            "node": inside["node"], "run": inside["run"], "script": script, "workspace": inside["workspace"],
        },
        # The Windows paths, which the file tools are given.
        "workspace": shared or inside["workspace"],
        "agent_dirs": given["agent_dirs"],
        "read_dirs": given["read_dirs"],
        "protected": protected,
    }
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(config, indent=2) + NL, encoding="utf-8")
    cli = runtime_dir() / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js"
    if (runtime_dir() / "windows-install.json").is_file() and cli.is_file():
        # The account kits up to 0.26.0 ran commands as, which nothing uses now.
        print("  The Windows account srt-sandbox and its firewall rules are still on this machine,")
        print("  and this kit does not use them. To remove them, with one UAC prompt, run:")
        print(f'    "{find("node")}" "{cli}" windows-uninstall')
    return check(config)


def wsl_command(config: dict, *argv: str) -> list[str]:
    """This module's ``argv`` run in the distribution, on its sandbox."""
    wsl = config["wsl"]
    return [wsl["exe"], "-d", wsl["distro"], "-e", "env", f"QUPA_SANDBOX_DIR={wsl['dir']}", "python3", wsl["script"], *argv]


class kept_up:
    """The distribution kept running while a wake does, when commands run in
    WSL: WSL stops it seconds after its last process ends, and the next command
    would wait for it to start again."""

    def __init__(self, config: dict | None):
        self.wsl = (config or {}).get("wsl")
        self.keeper = None

    def __enter__(self):
        if self.wsl:
            try:
                self.keeper = subprocess.Popen(
                    [self.wsl["exe"], "-d", self.wsl["distro"], "-e", "sleep", "infinity"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError:
                self.keeper = None
        return self

    def __exit__(self, *exc):
        if self.keeper is not None:
            self.keeper.terminate()
        return False


def write_workspace(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / "pixi.toml").exists():
        for name in ("pixi.toml", "pixi.lock"):
            shutil.copyfile(FILES_DIR / name, workspace / name)
        shutil.copyfile(FILES_DIR / "pixi.toml", workspace / KIT_MANIFEST_COPY)
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


# The kit's manifest as a workspace was last brought up to it, beside the
# workspace's own, which the agent's `pixi add` and `pixi remove` change.
KIT_MANIFEST_COPY = ".qupa-kit-pixi.toml"


def dependencies(text: str) -> dict[str, str]:
    """A pixi manifest's [dependencies], each spec as written, from the line
    form this kit's manifest has and pixi writes."""
    found: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("["):
            inside = line == "[dependencies]"
        elif inside and "=" in line:
            name, spec = (part.strip() for part in line.split("=", 1))
            found[name] = spec
    return found


def matchspec(name: str, spec: str) -> str:
    """A spec as the manifest writes it, as `pixi add` takes it."""
    if spec.startswith("{"):
        fields = dict(
            (key.strip(), value.strip().strip('"')) for key, value in
            (part.split("=", 1) for part in spec.strip("{} ").split(",") if "=" in part)
        )
        return f"{name}={fields.get('version', '*')}" + (f"={fields['build']}" if "build" in fields else "")
    spec = spec.strip('"')
    if spec == "*":
        return name
    return name + spec if spec[:1] in "=<>!~" else f"{name}={spec}"


def manifest_changes(kit: str, previous: str | None, workspace: str) -> tuple[list[str], list[str]]:
    """What brings a workspace's manifest up to the kit's: each of the kit's
    dependencies it holds otherwise or not at all, to add, and each one an
    earlier kit had and this one has dropped that it still holds, to remove.
    A package the agent added itself is neither."""
    new, old, held = dependencies(kit), dependencies(previous or ""), dependencies(workspace)
    add = [matchspec(name, spec) for name, spec in new.items() if held.get(name) != spec]
    remove = [name for name in old if name not in new and name in held]
    return add, remove


def update_workspace(workspace: Path, pixi: str, env: dict) -> bool:
    """Bring a workspace an earlier kit made up to this kit's manifest, so that
    its packages follow the scipy-notebook build the kit is made from; nothing
    when it already is. False, having said why, when pixi refuses."""
    kit = (FILES_DIR / "pixi.toml").read_text(encoding="utf-8")
    copy = workspace / KIT_MANIFEST_COPY
    previous = copy.read_text(encoding="utf-8") if copy.exists() else None
    if previous == kit:
        return True
    manifest = workspace / "pixi.toml"
    add, remove = manifest_changes(kit, previous, manifest.read_text(encoding="utf-8"))
    for verb, specs in (("remove", remove), ("add", add)):
        if not specs:
            continue
        print(f"  Bringing the Python environment up to this kit: pixi {verb} {len(specs)} package(s)...")
        result = _run([pixi, verb, "--manifest-path", str(manifest), *specs], env=env)
        if result.returncode != 0:
            print(f"  FAILED to {verb} the packages this kit {'dropped' if verb == 'remove' else 'pins'}")
            print(_tail(result))
            return False
    copy.write_text(kit, encoding="utf-8")
    return True


def setup(agent_dir: str, interactive: bool = False) -> bool:
    """Install the runtime and the environment, and check a command runs.
    False, having said why, when the sandbox cannot be had here. On Windows
    without a Linux distribution in WSL 2, ``interactive`` offers to install
    one."""
    # The projects the researcher let it read stay readable.
    projects = read_dirs(load_config() or {})
    # Written again only once everything below has worked, so a setup that
    # fails part-way leaves no sandbox half replaced.
    config_path().unlink(missing_ok=True)
    writable, refused = writable_agent_dirs(agent_dir)
    if refused:
        print(f"  {refused}")
    protected = protected_paths(writable)
    if WINDOWS:
        distro = wsl_distro()
        if not distro:
            print("  On Windows, commands run in Linux, under WSL 2, and this machine has no Linux")
            print("  distribution there.")
            distro = wsl_install() if interactive else None
        if distro:
            return wsl_setup(distro, writable, protected, projects)
        print("  To install one, run the command below and restart if it asks. Then open Ubuntu")
        print("  from the Start menu once to choose its username, and run qupa-install again:")
        print()
        print(f"    {WSL_INSTALL}")
        return False
    return install(writable, protected, projects)


WSL_INSTALL = "wsl --install -d Ubuntu"


def wsl_install() -> str | None:
    """Ask, then install WSL and Ubuntu with WSL's own installer, which shows
    a UAC prompt to turn WSL on and asks for Ubuntu's username where it can
    start Ubuntu at once. The distribution, or None where a restart or the
    first start of Ubuntu is still to come."""
    exe = wsl_exe()
    if exe is None:
        return None
    print("  WSL's installer can install Ubuntu. It shows one UAC prompt and may need a")
    print("  restart. It also asks you to choose a username and password for Ubuntu.")
    if input("  Install it now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        return None
    # Not captured: its progress, the UAC prompt's outcome and Ubuntu's
    # questions are the researcher's.
    sys.stdout.flush()
    ran = subprocess.run([exe, "--install", "-d", "Ubuntu"])
    if ran.returncode != 0:
        print(f"  WSL's installer stopped (exit {ran.returncode}).")
        return None
    return wsl_distro()


def install(writable: list[str], protected: list[str], projects: list[str]) -> bool:
    """The runtime, the environment and the configuration, for these
    directories of the researcher's, then the test command."""
    missing = missing_programs()
    if missing:
        print("  Not set up. Missing:")
        for line in missing:
            print(f"    {line}")
        return False

    if userns_restricted() and not bwrap_exempted():
        return ask_for_bwrap()

    node, npm, pixi = (find(name) for name in ("node", "npm", "pixi"))
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

    write_workspace(workspace)
    env = dict(os.environ, PIXI_CACHE_DIR=str(workspace / ".cache" / "rattler"))
    if not update_workspace(workspace, pixi, env):
        return False
    print(f"  Installing the Python environment in {workspace}")
    print("  (scipy-notebook's packages: about 2 GB, a few minutes the first time)...")
    result = _run([pixi, "install", "--manifest-path", str(workspace / "pixi.toml")], env=env)
    if result.returncode != 0:
        print("  FAILED to install the environment")
        print(_tail(result))
        return False

    config = {
        "workspace": str(workspace),
        "agent_dirs": writable,
        "read_dirs": [d for d in projects if d not in writable],
        "protected": protected,
        "node": node,
        "run": str(runtime / "run.mjs"),
        "pixi": pixi,
        "bash": "/bin/bash",
        # Where the runtime finds bwrap, socat and rg, which may be only in
        # pixi's bin directory and not on the PATH a wake inherits; the
        # exempted bubblewrap first, where AppArmor needs it.
        "path": ([str(LINUX_BWRAP.parent)] if userns_restricted() else [])
        + sorted({str(Path(find(name)).parent) for name in wanted_programs() if find(name)}),
        "programs": program_dirs(),
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
    wsl = config.get("wsl")
    if wsl:
        result = _run([wsl["exe"], "-d", wsl["distro"], "-e", wsl["node"], wsl["run"], wsl["config"], "--check"], timeout=300)
    else:
        result = _run([config["node"], config["run"], str(config_path()), "--check"], cwd=config["workspace"], timeout=300)
    if result.returncode != 0:
        print("  FAILED: a command in the sandbox did not run")
        print(_tail(result, 6))
        return False
    print(f"  A command ran in the sandbox: {result.stdout.strip()}")
    return True


def readable_project(directory: str) -> tuple[str, str]:
    """``directory`` resolved, and why the sandbox may not read it, empty
    when it may."""
    path = Path(directory).expanduser()
    if not path.is_dir():
        return str(path), f"{path} is not a directory."
    path = path.resolve()
    if path == Path.home().resolve() or path in Path.home().resolve().parents:
        return str(path), f"{path} is your home directory or above it, which holds your keys."
    secret = secret_within(path)
    if secret is not None:
        return str(path), f"{path} holds {secret}, so the sandbox is not given it."
    return str(path), ""


def grant_read(directory: str) -> bool:
    """Let wakes read ``directory``, a project of the researcher's, and not
    change it, from the next command on. False, having said why, when it
    cannot be."""
    config = load_config()
    if not ready(config):
        print("The sandbox is not set up. Run qupa-install first.")
        return False
    path, refused = readable_project(directory)
    if refused:
        print(refused)
        return False
    if within(path, [config["workspace"], *agent_dirs(config)], path):
        print(f"Your agent's wakes already read and write {path}.")
        return True
    if path in read_dirs(config):
        print(f"Your agent's wakes already read {path}.")
        return True
    wsl = config.get("wsl")
    if wsl:
        inside = linux_path(path, wsl["mount"])
        if inside is None:
            print(f"{path} is not on a drive, so the sandbox in WSL cannot read it.")
            return False
        result = _run(wsl_command(config, "read", inside), timeout=300)
        if result.returncode != 0:
            print(f"FAILED to let the sandbox in WSL read {path}")
            print(_tail(result))
            return False
    config["read_dirs"] = [*read_dirs(config), path]
    config_path().write_text(json.dumps(config, indent=2) + NL, encoding="utf-8")
    print(f"Your agent's wakes can now read {path}, and cannot change it.")
    return True


def forget_read(directory: str) -> bool:
    """Stop wakes reading ``directory``."""
    config = load_config()
    if not config:
        print("The sandbox is not set up.")
        return False
    path = str(Path(directory).expanduser().resolve())
    if path not in read_dirs(config):
        print(f"{path} is not among the projects your agent's wakes read.")
        return False
    wsl = config.get("wsl")
    if wsl:
        result = _run(wsl_command(config, "unread", linux_path(path, wsl["mount"]) or path), timeout=300)
        if result.returncode != 0:
            print(f"FAILED to take back the sandbox's reading of {path} in WSL")
            print(_tail(result))
            return False
    config["read_dirs"] = [d for d in read_dirs(config) if d != path]
    config_path().write_text(json.dumps(config, indent=2) + NL, encoding="utf-8")
    print(f"Your agent's wakes no longer read {path}.")
    return True


def main(argv: list[str]) -> int:
    if argv[:1] == ["hook"]:
        return hook_main()
    if argv[:1] == ["check"]:
        return 0 if check() else 1
    if argv[:1] == ["wsl-setup"] and len(argv) == 2:
        # In WSL, from Windows' wsl_setup: what Windows chose, as Linux paths.
        given = json.loads(base64.b64decode(argv[1]).decode("utf-8"))
        ensure_programs()
        return 0 if install(given["agent_dirs"], given["protected"], given["read_dirs"]) else 1
    if argv[:1] == ["read"] and len(argv) == 2:
        return 0 if grant_read(argv[1]) else 1
    if argv[:1] == ["unread"] and len(argv) == 2:
        return 0 if forget_read(argv[1]) else 1
    if argv[:1] == ["projects"]:
        for directory in read_dirs(load_config() or {}):
            print(directory)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
