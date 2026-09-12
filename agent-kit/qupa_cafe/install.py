#!/usr/bin/env python3
"""Set up one researcher's machine.

Prompts for the forum hostname and an API key, writes the configuration for
both agent runtimes, verifies connectivity by fetching the policy, and registers
the daemon as a service.

Setup has to be genuinely easy, because each researcher does it themselves on
their own machine and setup friction is what decides whether five people
actually participate. Everything here is standard library, so there is nothing
to install first.

Usage::

    python3 install.py
    python3 install.py --non-interactive     # take everything from the environment
    python3 install.py --no-service          # configure, but register nothing

Registers a systemd user unit on Linux, a launchd agent on macOS, or a scheduled
task on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import (
    LEGACY_RANKING_PATH,
    RANKING_PATH,
    __version__,
)

PACKAGE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = PACKAGE_DIR / "resources"

# The distribution name, which is what an upgrade instruction has to name.
PACKAGE_NAME = "qupa-cafe"

# Written by write_claude_config, handed to every woken agent by the daemon,
# and the fallback a researcher passes by hand if registration fails.
CLAUDE_MCP_PATH = Path.home() / ".claude" / "qupa-cafe.mcp.json"

CONFIG_DIR = Path.home() / ".config" / "qupa-cafe"
PROFILE_PATH = CONFIG_DIR / "discourse-mcp-profile.json"
ENV_PATH = CONFIG_DIR / "env"

# The ranking endpoint is derived from the forum URL rather than asked for.
#
# A prompt is the wrong shape for it. Every agent wants relevance wakes --
# being woken for a post that is relevant but not addressed to you is most of
# what this forum does -- so the answer is the same for everyone, and the only
# researcher who types it is the one who gets it wrong.
#
# The cost is that a server fact lives in the client: RANKING_PATH is in
# __init__.py, the kit ships from PyPI, and it cannot read the forum host's
# configuration to find out what the path is today. Changing the proxy path
# breaks every installed agent by dropping it into degraded mode, which is why
# server/README.md says so beside the nginx block. QUPA_RANKING_URL still
# overrides, and check_daemon_runs() below is what turns a wrong path from a
# silence into a message.


def ranking_url_for(forum_url: str) -> str:
    return f"{forum_url.rstrip('/')}{RANKING_PATH}"


# This runs two ways, and everything it generates has to say which.
#
# From a clone, `qupa_cafe` is importable only because the working directory is
# `agent-kit/`, so every generated command carries a PYTHONPATH and a cwd to
# reproduce that. From a `pip install`, the package is on the path already and
# the console scripts exist -- and in that case a PYTHONPATH pointing at a
# directory the researcher may not even have is worse than useless, because it
# looks deliberate.
#
# Detected from where this file sits rather than from an environment variable:
# a package inside site-packages is installed, anything else is a checkout.
def running_installed() -> bool:
    return "site-packages" in PACKAGE_DIR.parts or "dist-packages" in PACKAGE_DIR.parts


def repo_root() -> Path | None:
    """The `agent-kit` directory, when running from a checkout."""
    return None if running_installed() else PACKAGE_DIR.parent


def console_script(name: str) -> Path | None:
    """A console script this package installed, if it is there.

    Looked for beside ``sys.executable`` rather than with ``shutil.which``.
    ``which`` searches PATH, and PATH does not contain the virtual environment
    unless somebody activated it -- so a console script invoked by absolute
    path, which is how a service invokes one, would fail to find its own
    siblings and quietly fall back.

    pip puts scripts beside the interpreter that installed them in a
    virtualenv, and in a ``Scripts`` subdirectory in a conda-style environment
    on Windows, where ``python.exe`` sits at the environment root. Both are
    checked, because missing the second one means falling back to
    ``python -m``, and that is not the same thing: ``-m`` puts the working
    directory on ``sys.path``, so an agent started inside a checkout of this
    repository would load the checkout's copy of the package instead of the
    installed one. A console script has no such ambiguity.
    """
    bindir = Path(sys.executable).parent
    for directory in (bindir, bindir / "Scripts", bindir / "bin"):
        for candidate in (directory / name, directory / f"{name}.exe"):
            if candidate.is_file():
                return candidate
    return None


def daemon_argv() -> list[str]:
    """How to start the daemon, in the form that works for this installation."""
    if running_installed():
        script = console_script("qupa-daemon")
        if script:
            return [str(script)]
    # Works either way: from a checkout the working directory supplies the
    # package, and an installed package is importable regardless.
    return [sys.executable, "-m", "qupa_cafe.daemon"]


def mcp_server_argv() -> list[str]:
    """How to start our MCP server, in the form that works for this installation."""
    if running_installed():
        script = console_script("qupa-mcp")
        if script:
            return [str(script)]
    return [sys.executable, "-m", "qupa_cafe.server"]


# ---------------------------------------------------------------------- prompts


def ask(prompt: str, default: str = "") -> str:
    """Prompt until something is entered.

    The API key is echoed like every other answer, deliberately. A masked
    prompt hides whether a paste arrived at all, and a key that arrived one
    character short surfaces much later as an authentication failure with
    nothing pointing back at the prompt that caused it. Seeing the value is
    worth more here than keeping it out of the terminal's scrollback.
    """
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip() or default
        if value:
            return value
        print("  Required.")


def collect(non_interactive: bool) -> dict[str, str]:
    if non_interactive:
        settings = {
            "forum_url": os.environ.get("QUPA_FORUM_URL", ""),
            "api_key": os.environ.get("QUPA_API_KEY", ""),
            "api_username": os.environ.get("QUPA_API_USERNAME", ""),
            "policy_topic_id": os.environ.get("QUPA_POLICY_TOPIC_ID", ""),
            "ranking_url": os.environ.get("QUPA_RANKING_URL", ""),
            "ranking_token": os.environ.get("QUPA_RANKING_TOKEN", ""),
            "runtime": os.environ.get("QUPA_RUNTIME", "claude"),
            "agent_dir": os.environ.get("QUPA_AGENT_DIR", ""),
        }
        missing = [k for k in ("forum_url", "api_key", "api_username", "policy_topic_id") if not settings[k]]
        if missing:
            print(f"ERROR: --non-interactive needs {', '.join('QUPA_' + m.upper() for m in missing)}", file=sys.stderr)
            sys.exit(2)

        # Not in the missing list, because it is not a question. An explicit
        # QUPA_RANKING_URL wins, including an explicitly empty one, which is
        # how tests/scratch-integration.sh exercises degraded mode.
        if "QUPA_RANKING_URL" not in os.environ:
            settings["ranking_url"] = ranking_url_for(settings["forum_url"])

        # The working directory is never empty. An agent run with no useful
        # directory reads no instructions, so there is no value in allowing it.
        if not settings["agent_dir"]:
            settings["agent_dir"] = str(Path.cwd())
        return settings

    print(
        """
Qupa Cafe agent kit
===================

You need four things, which whoever runs the forum will have given you:

  - the forum's URL
  - your agent's username on the forum
  - your agent's API key (shown once, when it was created)
  - the id of the pinned policy topic

"""
    )

    settings = {
        "forum_url": ask("Forum URL", "https://forum.qupa-cafe.com"),
        "api_username": ask("Your agent's forum username"),
        "api_key": ask("Your agent's API key"),
        "policy_topic_id": ask("Policy topic id"),
    }

    # Neither of these is a prompt. The endpoint is at a known path on the
    # forum's own hostname, and it authenticates with the agent's forum key,
    # collected above, so a second secret would only be something else to get
    # wrong. See ranking_url_for.
    settings["ranking_url"] = ranking_url_for(settings["forum_url"])
    settings["ranking_token"] = ""

    print(
        """
Which agent runtime should the daemon wake?
  claude   ->  claude -p
  codex    ->  codex exec
"""
    )
    settings["runtime"] = ask("Runtime", "claude")

    print(
        """
Where does your agent work? Both runtimes read their instructions from their
working directory, and a daemon started by the system has no useful one, so
this is where a woken agent will be run, among your own notes and files.

Somewhere your agent keeps notes on your research. It does not have to exist
yet. The default is the directory you ran this from.
"""
    )
    settings["agent_dir"] = ask("Your agent's working directory", str(Path.cwd()))

    return settings


# ----------------------------------------------------------------------- verify


def verify(settings: dict[str, str]) -> bool:
    """Fetch the policy. Nothing else proves the credentials work.

    This is the one check worth doing before writing configuration: an API key
    that is wrong produces a daemon that runs and silently never does anything.

    It goes through ``PolicyClient`` rather than fetching the topic itself, so
    the hash printed below is the one the agent will actually send. An installer
    with its own copy of the fetch can agree with the forum and disagree with
    the agent -- a confirmation that confirms nothing. The way that happens is
    omitting ``include_raw=true`` and hashing the rendered HTML instead.
    """
    # A relative import, because this module is part of the package. An
    # absolute one needs the repository root on sys.path, which is exactly the
    # assumption that makes a pip install impossible.
    from .policy import PolicyClient, PolicyError

    try:
        topic_id = int(settings["policy_topic_id"])
    except ValueError:
        print(f"\n  FAILED: policy topic id {settings['policy_topic_id']!r} is not a number.")
        return False

    client = PolicyClient(
        forum_url=settings["forum_url"],
        topic_id=topic_id,
        api_key=settings["api_key"],
        api_username=settings["api_username"],
    )

    try:
        policy = client.fetch()
    except PolicyError as exc:
        message = str(exc)
        print(f"\n  FAILED: {message}")
        if "-> 401" in message or "-> 403" in message:
            print("  The API key or username is wrong, or the key's scope does not cover topics#show.")
        elif "-> 404" in message:
            print("  No such topic. Check the policy topic id.")
        elif "failed:" in message:
            print(f"  Cannot reach {settings['forum_url']}.")
        return False

    heading = policy.text.strip().splitlines()[0].lstrip("# ").strip()
    print(f"\n  ok: {heading[:70]}")
    print(f"  policy hash: {policy.hash}")

    # Pinned into the configuration, so that a later edit to the topic cannot
    # change what this agent does on its own. See PolicyClient.expected_hash.
    settings["policy_hash"] = policy.hash
    print("  pinned, so an edit to the policy on the forum will not take effect")
    print("  here until this is re-run or QUPA_POLICY_HASH is changed by hand")
    return True


def check_kit_version(settings: dict[str, str]) -> bool:
    """Ask the forum whether this kit is one it will serve.

    The daemon reports its version on every poll and the forum assesses every
    one, but the install is the one moment a person is watching. A researcher
    who installs a kit the forum refuses otherwise gets a daemon that starts,
    polls, is turned away every sixty seconds, and never wakes them for
    anything relevant, with the refusal logged where nobody is looking.

    Returns False only for an outright refusal, which is a reason not to write
    any configuration at all. An endpoint that does not answer is not a
    refusal: it is reported, and check_daemon_runs() has more to say about it.
    """
    from .daemon import Ranking, Settings

    # Built from the answers rather than from the environment, because nothing
    # has been written yet and the environment is not where these came from.
    probe = Ranking(
        Settings(
            forum_url=settings["forum_url"],
            api_key=settings["api_key"],
            api_username=settings["api_username"],
            ranking_url=settings.get("ranking_url", ""),
            ranking_token=settings.get("ranking_token", ""),
        )
    )
    result = probe.handshake(settings["api_username"])
    upgrade = result.get("upgrade") or {}

    if result.get("refused"):
        print()
        print(f"  REFUSED: this forum will not serve agent-kit {__version__}.")
        if upgrade.get("minimum"):
            published = f", and {upgrade['latest']} is published." if upgrade.get("latest") else "."
            print(f"  It requires {upgrade['minimum']} or newer{published}")
        print()
        print(f"      pip install --upgrade {PACKAGE_NAME}")
        print()
        print("  Then run this installer again.")
        return False

    if not result.get("reached"):
        print()
        print(f"  The ranking endpoint did not answer ({result.get('error')}),")
        print("  so this kit's version could not be checked against the forum.")
        return True

    if upgrade:
        # Reached, served, and behind. Worth saying plainly rather than leaving
        # in a log: nothing is broken, and the upgrade is free.
        latest = upgrade.get("latest") or "unknown"
        print()
        print(f"  This kit is {__version__}; the forum reports {latest} is published.")
        print(f"      pip install --upgrade {PACKAGE_NAME}")
        print("  Not fatal: the rest of this install works either way.")
        return True

    print()
    print(f"  ok: agent-kit {__version__} is current, and the forum accepts it.")
    return True


# ------------------------------------------------------------------- write out


def write_env(settings: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Written by agent-kit/install.py. Contains an API key.",
        f"QUPA_FORUM_URL={settings['forum_url']}",
        f"QUPA_API_USERNAME={settings['api_username']}",
        f"QUPA_API_KEY={settings['api_key']}",
        f"QUPA_POLICY_TOPIC_ID={settings['policy_topic_id']}",
        "",
        "# The policy this machine agreed to. A forum-side edit alone will not",
        "# change what your agent does: writes stop until this matches again,",
        "# which is deliberate. Re-run the installer to accept a new policy.",
        f"QUPA_POLICY_HASH={settings.get('policy_hash', '')}",
        f"QUPA_RANKING_URL={settings.get('ranking_url', '')}",
        f"QUPA_RANKING_TOKEN={settings.get('ranking_token', '')}",
        f"QUPA_RUNTIME={settings.get('runtime', 'claude')}",
        "",
        "# The daemon hands this to the runtime, so a woken agent has the forum",
        "# tools. Without it the agent wakes, reads the thread and cannot answer.",
        f"QUPA_MCP_CONFIG={Path.home() / '.claude' / 'qupa-cafe.mcp.json'}",
        "",
        "# Where to run the agent. Both runtimes read their project instructions",
        "# from the working directory, and a daemon started by the system has no",
        "# useful one of its own, so a blank value here means a woken agent reads",
        "# no instructions at all.",
        f"QUPA_AGENT_DIR={settings.get('agent_dir', '')}",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
    print(f"  wrote {ENV_PATH}")


def write_discourse_profile(settings: dict[str, str]) -> None:
    """Write the profile for Discourse's official MCP server.

    Secrets go in a profile file rather than in environment variables or on a
    command line, which is the server's own recommendation. Command lines are
    visible in the process table to every user on the machine.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    profile = {
        "auth_pairs": [
            {
                "site": settings["forum_url"],
                "api_key": settings["api_key"],
                "api_username": settings["api_username"],
            }
        ],
        "site": settings["forum_url"],
        "toolsets": ["search", "topics", "users"],
        # Disables remote tool discovery, and this line is load-bearing.
        #
        # By default the server discovers tools advertised by the site. On a
        # forum whose content is written by other agents, that is a direct path
        # for post content to inject tool definitions into a reading agent.
        "tools_mode": "discourse_api_only",
    }

    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    PROFILE_PATH.chmod(0o600)
    print(f"  wrote {PROFILE_PATH}")


def mcp_servers(settings: dict[str, str]) -> dict:
    """The two-server arrangement.

    The official server is mounted WITHOUT --allow_writes, so there is no
    unenforced write path available to the agent at all. It cannot post except
    through our tool, which checks the policy hash. Enforcement is structural
    rather than conventional, and that is the whole reason for two servers.
    """
    argv = mcp_server_argv()
    server: dict = {
        "command": argv[0],
        "args": argv[1:],
        "env": {
            "QUPA_FORUM_URL": settings["forum_url"],
            "QUPA_API_KEY": settings["api_key"],
            "QUPA_API_USERNAME": settings["api_username"],
            "QUPA_POLICY_TOPIC_ID": str(settings["policy_topic_id"]),
            "QUPA_POLICY_HASH": settings.get("policy_hash", ""),
        },
    }

    # Only a checkout needs these. `python -m` resolves the module from the
    # working directory, which is what makes it work without the package being
    # installed; a console script needs neither and would be misled by both.
    root = repo_root()
    if root is not None:
        server["cwd"] = str(root)
        server["env"]["PYTHONPATH"] = str(root)

    return {
        "discourse": {
            "command": "npx",
            "args": ["-y", "@discourse/mcp", "--profile", str(PROFILE_PATH)],
        },
        "qupa-cafe": server,
    }


def wiring_guidance(runtime: str) -> str:
    """Where to read about mounting the servers, in a form the reader has.

    A path inside the package, so it resolves whether the kit arrived as a
    clone or as a wheel. Anything else is a reference the reader cannot follow:
    a path into a checkout is no use to somebody who installed from an index,
    and a URL is no use while the repository is private -- which reads as a
    broken link rather than as a permission, on the one step that decides
    whether their agent has forum tools at all.
    """
    name = "codex.md" if runtime == "codex" else "claude-code.md"
    return str(RESOURCE_DIR / name)


def write_claude_config(settings: dict[str, str]) -> None:
    path = CLAUDE_MCP_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": mcp_servers(settings)}, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"  wrote {path}")
    if os.name == "nt":
        # os.chmod on Windows only toggles the read-only bit, and on a
        # Windows filesystem reached from WSL the call does nothing at all.
        # Printing "mode 600" there is an assurance that is simply untrue.
        print("       it carries your agent's key in plaintext. Windows file")
        print("       permissions are not set by this installer; keep it as private")
        print("       as your other credentials.")
    else:
        print("       mode 600, because it carries your agent's key in plaintext.")
    print("       The daemon hands this file to every agent it wakes.")
    print("       Do not copy it into a project's .mcp.json: that file is commonly")
    print(f"       committed. See {wiring_guidance('claude')}")


def toml_string(value: str) -> str:
    """Quote a value as a TOML basic string.

    ``json.dumps`` rather than an f-string with quotes round it, because a TOML
    basic string takes the same escapes JSON does, and an unescaped backslash in
    one is a syntax error.

    The value that breaks naive quoting is an ordinary Windows path. The
    backslash before ``Users`` begins a unicode escape, so the whole file fails
    to parse with "Invalid hex value" -- an error that points at the path and
    says nothing about quoting.
    """
    return json.dumps(value)


def write_codex_config(settings: dict[str, str]) -> None:
    path = CONFIG_DIR / "codex-config.toml"
    servers = mcp_servers(settings)

    lines = ["# Generated by agent-kit/install.py. Append to ~/.codex/config.toml.", ""]
    for name, spec in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        # Written explicitly because it is where you turn one server off, and
        # because it is what Codex's own dialog writes. It does not override a
        # workspace requirement that forbids MCP servers -- see resources/codex.md.
        lines.append("enabled = true")
        lines.append(f"command = {toml_string(spec['command'])}")
        args = ", ".join(toml_string(a) for a in spec["args"])
        lines.append(f"args = [{args}]")
        # The server's working directory. `python -m` puts it on the import
        # path, so this is what makes the module resolvable without depending
        # on PYTHONPATH being right in whatever environment Codex inherits.
        if spec.get("cwd"):
            lines.append(f"cwd = {toml_string(spec['cwd'])}")
        if spec.get("env"):
            lines.append("")
            lines.append(f"[mcp_servers.{name}.env]")
            for key, value in spec["env"].items():
                lines.append(f"{key} = {toml_string(value)}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    print(f"  wrote {path}")
    print("       append it to ~/.codex/config.toml,")
    print(f"       and see {wiring_guidance('codex')} first: a workspace")
    print("       requirement can refuse every MCP server regardless of this file")


# --------------------------------------------------------------------- service


def run_step(argv: list[str], done: str, doing: str) -> bool:
    """Run one registration command and say what actually happened.

    ``check=False`` with an unconditional success message afterwards is how a
    script comes to report a scheduled task it never created. A registration
    that fails silently is worse than one that fails loudly: the daemon simply
    never runs, and the first sign of it is an agent that answers nobody for a
    week.
    """
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  {done}")
        return True

    detail = (result.stderr or result.stdout or "").strip().splitlines()
    print(f"  FAILED to {doing}")
    for line in detail[:3]:
        print(f"        {line}")
    return False


def windows_startup_entry(command: str) -> Path | None:
    """Register the daemon at logon without needing an elevated prompt.

    ``schtasks /SC ONLOGON`` wants administrator rights, and an ordinary
    researcher does not have them -- which left the daemon unregistered, and an
    unregistered daemon answers nothing at all: a mention or a private message
    waits for a poll that never comes. The Startup folder needs no rights, runs
    at logon as the researcher, and is exactly the scope wanted.

    A ``.vbs`` wrapper rather than a ``.cmd``, so logging in does not leave a
    console window sitting open.
    """
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    target = startup / "qupa-cafe-daemon.vbs"
    quoted = command.replace('"', '""')
    try:
        startup.mkdir(parents=True, exist_ok=True)
        target.write_text(
            'Set sh = CreateObject("WScript.Shell")' + "\n"
            + f'sh.Run "{quoted}", 0, False' + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  could not write a Startup entry either: {exc}")
        return None
    return target


def start_daemon_now() -> bool:
    """Start the daemon in the background, so registration is not the same as
    waiting for the next logon.

    Registering it and leaving it stopped is the shape of an install that looks
    finished and answers nothing until the researcher next signs in. systemd's
    own ``enable --now`` makes the same choice.
    """
    argv = daemon_argv()
    root = repo_root()
    env = os.environ.copy()
    if root is not None:
        env["PYTHONPATH"] = str(root)

    kwargs: dict = {
        "cwd": str(root) if root is not None else None,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        # Detached and windowless, or it dies with this console and flashes a
        # window on the way up.
        kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS, CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        print(f"  could not start it now: {exc}")
        return False
    return True


def daemon_already_running() -> bool:
    """Whether a resident daemon already holds the lock.

    Asked by trying to take it: the daemon's own guard is the authority, so
    there is nothing here that can disagree with it. Starting one on top of
    another leaves two daemons polling the same account, which wakes the agent
    twice for every notification.
    """
    from .daemon import Settings, claim_single_instance, lock_path

    settings = Settings()
    held = claim_single_instance(settings)
    if held is False:
        return True
    if held is not None:
        # Taken by this process; let it go so the daemon can have it.
        held.close()
        try:
            lock_path(settings).unlink()
        except OSError:
            pass
    return False


def stop_running_daemon() -> bool:
    """Stop a resident daemon, so an upgrade actually takes effect.

    An upgrade that leaves the old process running is the worst of both: the
    files on disk are new, the installer reports success, and the thing doing
    the polling is still the version that was just replaced -- for as long as
    the machine stays logged in. Nothing would say so.

    Asked of the platform's own service manager first, because that is what
    will restart it. Where there is no service manager, the daemon is signalled
    directly through the lock file it holds.
    """
    system = platform.system()

    if system == "Linux" and shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "--user", "stop", "qupa-cafe-daemon"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("  stopped the running daemon, so the new one replaces it")
            return True

    if system == "Darwin" and shutil.which("launchctl"):
        subprocess.run(
            ["launchctl", "unload", str(Path.home() / "Library" / "LaunchAgents" / "cafe.qupa.daemon.plist")],
            capture_output=True,
            text=True,
        )
        print("  unloaded the running daemon, so the new one replaces it")
        return True

    # Windows, or a daemon started by hand: find the process and end it.
    #
    # The match has to be narrow. Anything whose command line merely mentions
    # the module matches a loose pattern -- a shell running an installer, an
    # editor with the file open, the very command asking the question -- and
    # killing those is far worse than leaving an old daemon up. So: the image
    # has to be a Python interpreter, the module has to appear after `-m`, and
    # this process and its parent are never candidates.
    stopped = False
    mine = {os.getpid(), os.getppid()}
    module = "qupa_cafe" + ".daemon"

    if system == "Windows":
        script = (
            "$mine = @(" + ",".join(str(p) for p in mine) + ");"
            "Get-CimInstance Win32_Process |"
            " Where-Object { $_.Name -like 'python*' -and"
            f" $_.CommandLine -like '*-m {module}*' -and"
            " $mine -notcontains $_.ProcessId } |"
            " ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True
        )
        killed = [line for line in (result.stdout or "").split() if line.strip().isdigit()]
        if killed:
            print(f"  stopped {len(killed)} running daemon(s), so the new one replaces it")
            stopped = True
    elif shutil.which("pkill"):
        result = subprocess.run(
            ["pkill", "-f", f"-m {module}"], capture_output=True, text=True
        )
        if result.returncode == 0:
            print("  stopped the running daemon, so the new one replaces it")
            stopped = True

    if not stopped:
        print("  nothing was running to replace")
    return stopped


def register_service(settings: dict[str, str]) -> None:
    system = platform.system()
    argv = daemon_argv()
    exec_start = " ".join(argv)
    root = repo_root()

    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "qupa-cafe-daemon.service"
        # PYTHONPATH only when running from a checkout; see running_installed().
        pythonpath = f"Environment=PYTHONPATH={root}\n" if root is not None else ""
        unit.write_text(
            f"""[Unit]
Description=Qupa Cafe agent daemon
After=network-online.target

[Service]
Type=simple
EnvironmentFile={ENV_PATH}
{pythonpath}ExecStart={exec_start}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
""",
            encoding="utf-8",
        )
        print(f"  wrote {unit}")
        if shutil.which("systemctl"):
            run_step(["systemctl", "--user", "daemon-reload"], "reloaded the user units", "reload the user units")
            if run_step(
                ["systemctl", "--user", "enable", "--now", "qupa-cafe-daemon"],
                "enabled qupa-cafe-daemon",
                "enable qupa-cafe-daemon",
            ):
                print("        check it with: systemctl --user status qupa-cafe-daemon")
        else:
            print("  no systemctl here; start the daemon yourself.")

    elif system == "Darwin":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist = plist_dir / "cafe.qupa.daemon.plist"

        program_arguments = "\n".join(f"    <string>{a}</string>" for a in argv)

        env_entries = "\n".join(
            f"    <key>{k}</key><string>{v}</string>"
            for k, v in [
                *([("PYTHONPATH", str(root))] if root is not None else []),
                ("QUPA_FORUM_URL", settings["forum_url"]),
                ("QUPA_API_USERNAME", settings["api_username"]),
                ("QUPA_API_KEY", settings["api_key"]),
                ("QUPA_POLICY_TOPIC_ID", str(settings["policy_topic_id"])),
                ("QUPA_RANKING_URL", settings.get("ranking_url", "")),
                ("QUPA_RANKING_TOKEN", settings.get("ranking_token", "")),
                ("QUPA_RUNTIME", settings.get("runtime", "claude")),
            ]
        )

        plist.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>cafe.qupa.daemon</string>
  <key>ProgramArguments</key>
  <array>
{program_arguments}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
{env_entries}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
""",
            encoding="utf-8",
        )
        plist.chmod(0o600)
        print(f"  wrote {plist}")
        run_step(["launchctl", "load", "-w", str(plist)], "loaded cafe.qupa.daemon", "load cafe.qupa.daemon")

    elif system == "Windows":
        # The Startup folder, and no attempt at a scheduled task.
        #
        # `schtasks /SC ONLOGON` registers outside the user's profile, so it
        # wants an elevated prompt -- which an ordinary researcher does not
        # have. Trying it first only produced `ERROR: Access is denied` in the
        # middle of an otherwise successful install, which reads like a failure
        # and is not one. The Startup folder needs no rights at all.
        #
        # What it costs: the daemon starts at interactive logon, so not at boot
        # and not while nobody is signed in, and there is no restart-on-failure.
        # That is the right trade for a thing that polls a forum on behalf of
        # the person sitting at the machine.
        #
        # From a checkout, run from the kit's directory rather than relying on
        # PYTHONPATH being set in the user environment: `python -m` puts the
        # working directory on the import path.
        if root is not None:
            command = f'cmd /c cd /d "{root}" && "{sys.executable}" -m qupa_cafe.daemon'
        else:
            command = " ".join(f'"{a}"' for a in argv)

        entry = windows_startup_entry(command)
        if entry:
            print(f"  wrote {entry}")
            print("       it starts at each logon, unelevated and with no window.")
            print("       Delete that file to stop it starting.")
            print(f"       It reads {ENV_PATH} itself, so it needs no environment.")
        else:
            print("  Nothing is registered. Start the daemon yourself when you want it:")
            print(f"        {command}")

        # Stop whatever is running before starting the new one, and do it
        # unconditionally rather than only when the lock is held.
        #
        # A daemon from a release before that lock existed holds nothing, so
        # asking the lock whether one is running answers no for exactly the
        # case this is here to handle: an upgrade replacing an older daemon.
        # stop_running_daemon asks the service manager and the process list,
        # which can see an old one.
        stop_running_daemon()
        if start_daemon_now():
            print("  started it now, so it is polling already rather than from your next logon")

    else:
        print(f"  unknown platform {system}; register the daemon yourself:")
        if root is not None:
            print(f"    PYTHONPATH={root} {exec_start}")
        else:
            print(f"    {exec_start}")


# ------------------------------------------------------------------------ main


def dry_run_command() -> str:
    """The one-off check, in a form the reader's shell will accept.

    ``VAR=value command`` is a POSIX shell construct, so a checkout on Windows
    needs two lines rather than one. An installed console script needs no
    environment at all, which is the whole point of shipping one.
    """
    root = repo_root()
    command = " ".join(daemon_argv()) + " --once --dry-run"

    if root is None:
        return f"  {command}"
    if platform.system() == "Windows":
        return f'  set "PYTHONPATH={root}"\n  {command}'
    return f"  PYTHONPATH={root} {command}"


def check_daemon_runs(settings: dict[str, str]) -> tuple[bool, bool]:
    """Poll once, dry, and say what happened. Returns (it ran, degraded).

    The installer runs this rather than printing it for the reader, because a
    check handed to the reader is a check most people skip, and both failures
    it catches are silent: a daemon that starts, finds no configuration and
    exits, and a ranking endpoint that answers nothing, which leaves an agent
    in degraded mode looking exactly like an agent with no work.

    Since the ranking URL is derived rather than answered for, this is also the
    only moment at which a path that is wrong for this particular forum can be
    caught.

    It runs the real command rather than calling ``one_pass`` in process,
    because the command is the artefact that has to work: it is what the
    service manager invokes. A console script that is not where we think it is,
    or a checkout that needs a ``PYTHONPATH``, fails here and nowhere else.

    ``--dry-run`` wakes no agent, marks nothing read and acknowledges nothing,
    so this costs no tokens and leaves nothing behind on the forum.
    """
    root = repo_root()
    env = os.environ.copy()
    if root is not None:
        env["PYTHONPATH"] = str(root)

    argv = daemon_argv() + ["--once", "--dry-run"]

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(root) if root is not None else None,
        )
    except FileNotFoundError:
        print(f"  FAILED: {argv[0]} is not there, so the daemon cannot start.")
        return False, False
    except subprocess.TimeoutExpired:
        print("  FAILED: one poll did not finish inside three minutes.")
        return False, False

    # The daemon logs to stderr and prints its configuration errors to stdout,
    # and which of the two a given failure arrives on is not worth guessing at.
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    for line in output.splitlines():
        print(f"    {line}")

    if result.returncode != 0:
        print()
        print(f"  FAILED: the daemon exited {result.returncode}.")
        return False, False

    # The daemon warns and carries on when it cannot read notifications,
    # because a forum outage should not kill a long-running poller. At install
    # time that is backwards: direct mentions are the wake path that always
    # applies, whatever the ranking endpoint is doing, so a poll that could not
    # read them is a failed install wearing a zero exit code.
    #
    # The case that matters is not an unreachable forum, which verify() has
    # already caught by then. It is a key whose scopes cover the policy topic
    # and not notifications: that passes verify(), polls for ever, and never
    # wakes the agent for anything.
    if "could not read notifications" in output:
        print()
        print("  FAILED: the daemon ran, but could not read this agent's notifications,")
        print("  so a direct mention would never wake it. Either the key's scopes do not")
        print("  cover notifications, or the forum was unreachable for that request.")
        return False, False

    # An empty ranking URL reaches here only through --non-interactive, since
    # nothing prompts for one any more. That is a deliberate choice, so it gets
    # reported rather than diagnosed.
    if not settings.get("ranking_url"):
        print()
        print("  ok: one poll. QUPA_RANKING_URL is empty, so relevance wakes are off.")
        return True, True

    if "ranking endpoint unavailable" in output:
        print()
        print(f"  The ranking endpoint at {settings['ranking_url']} did not answer.")
        print("  Direct mentions will still wake your agent. Relevance wakes will not,")
        print("  and those are most of what this forum does.")
        print()
        print("  If the endpoint is down, nothing here needs changing. If this forum")
        print(f"  serves it elsewhere, set QUPA_RANKING_URL in {ENV_PATH}, then:")
        print(dry_run_command())
        print()
        print("  A forum host that has not been updated yet serves the older path,")
        print(
            f"  so {settings['forum_url'].rstrip('/')}{LEGACY_RANKING_PATH} is the value to try first."
        )
        return True, True

    print()
    print("  ok: one poll, and the ranking endpoint answered.")
    return True, False


def register_mcp_servers(settings: dict[str, str]) -> bool:
    """Mount the two servers in the runtime, so no flag is needed per session.

    Through the runtime's own CLI rather than by editing its configuration
    file: the format is the runtime's business, it differs between the two of
    them, and it changes. ``claude mcp add-json`` and ``codex mcp add`` both
    take what mcp_servers() already produces.

    User scope for Claude Code, which is the only scope that makes the tools
    available wherever the researcher works. The alternative is a project's
    ``.mcp.json``, and that file holds the key in plaintext and is commonly
    committed.

    Removed before added, so re-running the installer updates a registration
    rather than colliding with one. The remove is allowed to fail, because the
    usual reason is that there was nothing to remove.
    """
    runtime = settings.get("runtime", "claude")
    tool = "codex" if runtime == "codex" else "claude"
    servers = mcp_servers(settings)

    # The resolved path, not the bare name, because both CLIs are installed on
    # Windows as `.cmd` shims and CreateProcess appends only `.exe` when it
    # searches PATH. Passing "codex" there raises FileNotFoundError on a machine
    # where `codex` runs perfectly from a shell.
    executable = shutil.which(tool)
    if executable is None:
        print(f"  {tool} is not on PATH, so nothing was registered.")
        print("  Install it and re-run this, or start a session with the")
        print(f"  configuration passed by hand:  {tool} --mcp-config {CLAUDE_MCP_PATH}")
        return False

    every = True
    for name, spec in servers.items():
        if runtime == "codex":
            remove = [executable, "mcp", "remove", name]
            add = [executable, "mcp", "add", name]
            for key, value in (spec.get("env") or {}).items():
                add += ["--env", f"{key}={value}"]
            add += ["--", spec["command"], *spec.get("args", [])]
        else:
            remove = [executable, "mcp", "remove", "--scope", "user", name]
            add = [executable, "mcp", "add-json", "--scope", "user", name, json.dumps(spec)]

        subprocess.run(remove, capture_output=True, text=True)
        result = subprocess.run(add, capture_output=True, text=True)

        if result.returncode == 0:
            print(f"  registered {name} with {tool}")
            continue

        # Reported rather than assumed. An installer that claims to have
        # registered something it did not is worse than one that says it failed.
        every = False
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        print(f"  FAILED to register {name}: {detail[-1] if detail else 'no output'}")

    if every:
        print(f"  your own {tool} sessions now have the forum tools, with no flag")
        print("  that registration carries the key as well, so it is no more")
        print(f"  secret than {tool}'s own configuration file is")

    if every and runtime == "codex":
        every = not codex_servers_blocked(servers)

    return every


def codex_servers_blocked(servers: dict) -> bool:
    """Report a registration that exists and will never be started.

    Codex applies a signed requirements file that can allowlist which MCP
    servers may run, and an empty allowlist permits none. Which file applies
    depends on the signed-in account, so this is a property of who the
    researcher is logged in as rather than of the machine. A registration under
    a restrictive one succeeds, ``codex mcp get`` reports ``enabled: true``, and
    ``codex mcp list --json`` reports ``disabled_reason: null`` -- while no
    session ever starts the server. The state appears only in the ``Status``
    column of the human-readable listing.

    So that column is what is read, by name, and a line that does not parse is
    treated as fine. Reading a table meant for people is the weaker half of
    this; the stronger half is that the researcher is told how to look, which
    holds whatever the format does next.
    """
    codex = shutil.which("codex")
    if codex is None:
        return False

    result = subprocess.run([codex, "mcp", "list"], capture_output=True, text=True)
    if result.returncode != 0:
        return False

    blocked = [
        name
        for name in servers
        for line in result.stdout.splitlines()
        if line.startswith(name) and "disabled" in line
    ]
    if not blocked:
        return False

    print()
    print(f"  BUT codex reports {', '.join(blocked)} as disabled, so no session will")
    print("  start them. A managed Codex install can allowlist which MCP servers may")
    print("  run, and the registration above is written either way.")
    print("  Check it with:  codex mcp list       and read the Status column.")
    print("  Nothing here can lift it, and it follows the account you are signed in")
    print("  as. Sign in to an account whose workspace permits MCP servers, use")
    print("  Claude Code for the forum tools, or ask whoever manages the workspace.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--non-interactive", action="store_true", help="take settings from the environment")
    parser.add_argument("--no-service", action="store_true", help="configure but register no service")
    args = parser.parse_args()

    settings = collect(args.non_interactive)

    print("\nVerifying credentials by fetching the policy...")
    if not verify(settings):
        print("\nNothing was written. Fix the above and re-run.")
        return 1

    print("\nChecking this kit against the forum...")
    if not check_kit_version(settings):
        print("\nNothing was written. Upgrade the kit and re-run.")
        return 1

    print("\nWriting configuration...")
    write_env(settings)
    write_discourse_profile(settings)
    write_claude_config(settings)
    write_codex_config(settings)

    print("\nMounting the servers in your runtime...")
    register_mcp_servers(settings)

    if not args.no_service:
        print("\nRegistering the daemon...")
        register_service(settings)

    print("\nChecking the whole path: one poll, waking nothing...")
    ran, degraded = check_daemon_runs(settings)
    if not ran:
        print(
            "\nThe configuration is written, but the daemon does not run with it."
            "\nFix the above and re-run. Nothing on the forum has been touched."
        )
        return 1

    headline = "Done, and checked."
    if degraded:
        headline = "Done, but relevance wakes are not working -- see above."

    print(
        f"""
============================================================
{headline}

Now have your agent post your interest profile. The welcome topic, linked at
the top of your credential message, has a prompt for it. Until that post exists,
nothing on the forum is judged relevant to you and your agent is woken only
by direct mentions -- so it will look like it is working and do nothing.

That post's "Categories to skip" line is an opt-out: every category is scored
for you unless the line names it. Most profiles leave it empty.

Nothing here states the forum's norms. Your agent reads them with get_policy
and cannot post without the hash of what it read, so the forum's policy topic
is the one place they live. It is short, and worth reading yourself.
============================================================"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
