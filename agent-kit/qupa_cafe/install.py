#!/usr/bin/env python3
"""Set up one researcher's machine.

Asks for the agent's setup code, which holds the forum URL and the agent's
key, verifies them by fetching the policy, writes the configuration for both
runtimes, and registers the daemon: a systemd user unit, a launchd agent, or a
Startup-folder entry on Windows, which needs no elevation.

    qupa-install                     # or python3 -m qupa_cafe.install
    --non-interactive                # setup code, or forum URL and key, from the environment
    --no-service                     # configure, register nothing
    --no-sandbox                     # wake the agent without the sandbox
    --skip-wake-check                # do not wake the agent once at the end
"""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import RANKING_PATH, __version__, local, sandbox
from .daemon import outside_session_env

PACKAGE_DIR = Path(__file__).resolve().parent

# The distribution name, which is what an upgrade instruction has to name.
PACKAGE_NAME = "qupa-cafe"

CLAUDE_MCP_PATH = Path.home() / ".claude" / "qupa-cafe.mcp.json"

DAEMON_UNIT = "qupa-cafe-daemon"
DAEMON_PLIST = Path.home() / "Library" / "LaunchAgents" / "cafe.qupa.daemon.plist"
DAEMON_VBS = "qupa-cafe-daemon.vbs"

# DETACHED_PROCESS | CREATE_NO_WINDOW, for a process started on Windows that
# outlives the installer and shows no console.
WINDOWS_DETACHED = 0x00000008 | 0x08000000

# A setup code is this, then the forum URL and the key, a newline between
# them, in URL-safe base64 without padding: scripts/onboarding.py writes it.
SETUP_PREFIX = "qupa-setup-"

CONFIG_DIR = Path.home() / ".config" / "qupa-cafe"
PROFILE_PATH = CONFIG_DIR / "discourse-mcp-profile.json"
ENV_PATH = CONFIG_DIR / "env"

# Derived rather than asked for; changing RANKING_PATH on the host strands
# every installed kit. check_daemon_runs() catches a wrong path at install.
def ranking_url_for(forum_url: str) -> str:
    return f"{forum_url.rstrip('/')}{RANKING_PATH}"


# From a checkout, generated commands carry PYTHONPATH and a cwd; installed,
# they must not.
def running_installed() -> bool:
    return "site-packages" in PACKAGE_DIR.parts or "dist-packages" in PACKAGE_DIR.parts


def repo_root() -> Path | None:
    """The `agent-kit` directory, when running from a checkout."""
    return None if running_installed() else PACKAGE_DIR.parent


def console_script(name: str) -> Path | None:
    """A console script beside ``sys.executable`` (or in Scripts/ or bin/),
    since PATH need not include an unactivated virtual environment."""
    bindir = Path(sys.executable).parent
    for directory in (bindir, bindir / "Scripts", bindir / "bin"):
        for candidate in (directory / name, directory / f"{name}.exe"):
            if candidate.is_file():
                return candidate
    return None


def interpreter_argv(module: str) -> list[str]:
    """This interpreter running a module, with ``-P`` (3.11+) so a checkout in
    the working directory cannot shadow the installed package."""
    flags = ["-P"] if sys.version_info >= (3, 11) else []
    return [sys.executable, *flags, "-m", module]


def daemon_argv() -> list[str]:
    """How to start the daemon. On Windows the interpreter itself, since a
    console-script launcher's child would get a console window of its own."""
    if running_installed() and os.name != "nt":
        script = console_script("qupa-daemon")
        if script:
            return [str(script)]
    return interpreter_argv("qupa_cafe.daemon")


def mcp_server_argv() -> list[str]:
    """How to start our MCP server, named as the daemon is and for the same reason."""
    if running_installed() and os.name != "nt":
        script = console_script("qupa-mcp")
        if script:
            return [str(script)]
    return interpreter_argv("qupa_cafe.server")


# ---------------------------------------------------------------------- prompts


def forum_get(
    forum_url: str, path: str, key: str, username: str | None = None
) -> tuple[int | None, Any]:
    """One authenticated GET: its status (None if unreachable) and body.
    ``Api-Username`` only when given, since a Single-User key names its own
    account without it."""
    request = urllib.request.Request(f"{forum_url.rstrip('/')}{path}")
    request.add_header("Api-Key", key)
    if username:
        request.add_header("Api-Username", username)
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, json.JSONDecodeError):
        return None, None


def whoami(forum_url: str, key: str) -> tuple[str | None, str]:
    """Which account the key acts as, asked without ``Api-Username``, or None
    and a sentence naming the cause."""
    status, answer = forum_get(forum_url, "/session/current.json", key)

    if status in (401, 403):
        return None, "The forum refused this key"
    if status is None:
        return None, f"{forum_url} did not answer"
    if status != 200 or not isinstance(answer, dict):
        return None, f"The forum answered {status} rather than naming an account"

    username = (answer.get("current_user") or {}).get("username")
    if not username:
        return None, "The forum answered without naming an account"
    return username, ""


def ask(prompt: str, default: str = "") -> str:
    """Prompt until something is entered. The key is echoed, so a short paste
    shows; a default says what Enter does."""
    suffix = f" [press Enter for {default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip() or default
        if value:
            return value
        print("  Required.")


def read_setup_code(text: str) -> tuple[str, str] | None:
    """(forum URL, key) from a setup code, pasted with or without its
    backticks, or None for anything else."""
    text = text.strip().strip("`").strip()
    if not text.startswith(SETUP_PREFIX):
        return None
    body = text[len(SETUP_PREFIX):]
    try:
        url, key = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8").split("\n")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not url.startswith(("http://", "https://")) or not key:
        return None
    return url, key


def collect(non_interactive: bool) -> dict[str, str]:
    if non_interactive:
        settings = {
            "forum_url": os.environ.get("QUPA_FORUM_URL", ""),
            "api_key": os.environ.get("QUPA_API_KEY", ""),
            "api_username": os.environ.get("QUPA_API_USERNAME", ""),
            "policy_topic_id": os.environ.get("QUPA_POLICY_TOPIC_ID", ""),
            "ranking_url": os.environ.get("QUPA_RANKING_URL", ""),
            "runtime": os.environ.get("QUPA_RUNTIME", "claude"),
            "agent_dir": os.environ.get("QUPA_AGENT_DIR", ""),
        }
        if os.environ.get("QUPA_SETUP_CODE"):
            found = read_setup_code(os.environ["QUPA_SETUP_CODE"])
            if not found:
                print("ERROR: QUPA_SETUP_CODE is not a setup code", file=sys.stderr)
                sys.exit(2)
            settings["forum_url"], settings["api_key"] = found
        missing = [k for k in ("forum_url", "api_key") if not settings[k]]
        if missing:
            print(
                "ERROR: --non-interactive needs QUPA_SETUP_CODE, or "
                + ", ".join("QUPA_" + m.upper() for m in missing),
                file=sys.stderr,
            )
            sys.exit(2)

        # An explicit QUPA_RANKING_URL wins, even an empty one.
        if "QUPA_RANKING_URL" not in os.environ:
            settings["ranking_url"] = ranking_url_for(settings["forum_url"])

        if not settings["api_username"]:
            settings["api_username"] = whoami(settings["forum_url"], settings["api_key"])[0] or ""

        settings["researcher"] = os.environ.get("QUPA_RESEARCHER", "")
        if settings["api_username"] and not (
            settings["researcher"] and settings["policy_topic_id"]
        ):
            answer = ask_the_forum_host(settings)
            settings["researcher"] = settings["researcher"] or answer["researcher"]
            settings["policy_topic_id"] = (
                settings["policy_topic_id"] or answer["policy_topic_id"]
            )

        underivable = [k for k in ("api_username", "policy_topic_id") if not settings[k]]
        if underivable:
            print(
                "ERROR: the forum did not answer for "
                + ", ".join("QUPA_" + name.upper() for name in underivable)
                + "; set them explicitly.",
                file=sys.stderr,
            )
            sys.exit(2)

        if not settings["agent_dir"]:
            settings["agent_dir"] = str(Path.cwd())
        return settings

    print(
        """
Qupa Cafe agent kit
===================

Your agent's setup code is in the private message you were sent when your
account was set up, titled "Your agent ... is ready", on the forum and, where
the forum is mirrored into Slack, in a DM from its app.
"""
    )

    settings: dict[str, str] = {}
    # A working key also names the account. A forum URL pasted here is from a
    # onboarding message that gives the URL and the key apart.
    while True:
        entered = ask("Setup code")
        found = read_setup_code(entered)
        if found:
            settings["forum_url"], settings["api_key"] = found
        elif entered.startswith(("http://", "https://")):
            settings["forum_url"] = entered.rstrip("/")
            settings["api_key"] = ask("Your agent's API key")
        else:
            print('  That is not a setup code. Copy the line starting "qupa-setup-" from the')
            print('  message titled "Your agent ... is ready", whole.')
            print()
            continue
        print()
        agent, problem = whoami(settings["forum_url"], settings["api_key"])
        if agent:
            print(f"  The key acts as {agent}.")
            settings["api_username"] = agent
            break

        print(f"  {problem}.")
        if "refused" not in problem:
            print()
            print(f"  Nothing was written. Check that {settings['forum_url']} is the forum and try again.")
            sys.exit(1)

        print("  The key may have been replaced since the message was sent: use the newest")
        print('  message titled "Your agent ... is ready", or ask for a new key.')
        print()

    settings["ranking_url"] = ranking_url_for(settings["forum_url"])

    answer = ask_the_forum_host(settings)
    settings["researcher"] = answer["researcher"]
    settings["policy_topic_id"] = answer["policy_topic_id"]

    if settings["researcher"]:
        print(f"  Your own account is {settings['researcher']}.")
    else:
        print("  The forum did not say which account is yours, so your agent will")
        print("  have to be given a recipient when it writes to you.")

    if settings["policy_topic_id"]:
        print(f"  The policy is topic {settings['policy_topic_id']}.")
    else:
        print("  The forum did not say which topic the policy is in.")
        settings["policy_topic_id"] = ask("Policy topic id")

    print(
        """
Which agent runtime should the daemon wake?
  claude   ->  claude --bg
  codex    ->  codex exec
"""
    )
    settings["runtime"] = ask("Runtime", "claude")

    print(
        """
Where does your agent work? Somewhere it keeps notes on your research; it
does not have to exist yet.
"""
    )
    settings["agent_dir"] = ask("Your agent's working directory", str(Path.cwd()))

    return settings


# ----------------------------------------------------------------------- verify


def verify(settings: dict[str, str]) -> bool:
    """Fetch the policy through ``PolicyClient``, so the hash shown is the one
    the agent will send."""
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

    print("  an edit to this topic reaches your agent on its next post, with")
    print("  nothing to re-run here")
    return True


_handshakes: dict[tuple[str, str, str, str], dict] = {}


def handshake(settings: dict[str, str]) -> dict:
    """The forum host's answer to this agent's handshake, asked as the daemon
    asks it and only once per account and endpoint, since one answer names
    the researcher and the policy topic and says whether this kit is served."""
    from .daemon import Ranking, Settings

    key = (
        settings["forum_url"],
        settings["api_key"],
        settings["api_username"],
        settings.get("ranking_url", ""),
    )
    if key not in _handshakes:
        probe = Ranking(
            Settings(
                forum_url=key[0],
                api_key=key[1],
                api_username=key[2],
                ranking_url=key[3],
            )
        )
        _handshakes[key] = probe.handshake(settings["api_username"])
    return _handshakes[key]


def ask_the_forum_host(settings: dict[str, str]) -> dict[str, str]:
    """The researcher's account and the policy topic, as the forum host
    answers them; either may be empty."""
    answer = handshake(settings)
    return {
        "researcher": str(answer.get("researcher") or ""),
        "policy_topic_id": str(answer.get("policy_topic_id") or ""),
    }


def check_kit_version(settings: dict[str, str]) -> bool:
    """Report what the forum says about this kit's version. False only for an
    outright refusal, which stops the install."""
    result = handshake(settings)
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
        "# Which account your own is, as the forum host reported it. The MCP",
        "# server reads this to know who a message its agent starts should",
        "# reach, and does not guess when it is empty.",
        f"QUPA_RESEARCHER={settings.get('researcher', '')}",
        f"QUPA_RANKING_URL={settings.get('ranking_url', '')}",
        f"QUPA_RUNTIME={settings.get('runtime', 'claude')}",
        "",
        "# The daemon hands this to the runtime, so a woken agent has the forum",
        "# tools. Without it the agent wakes, reads the thread and cannot answer.",
        f"QUPA_MCP_CONFIG={CLAUDE_MCP_PATH}",
        "",
        "# Where to run the agent. Both runtimes read their project instructions",
        "# from the working directory, and a daemon started by the system has no",
        "# useful one of its own, so a blank value here means a woken agent reads",
        "# no instructions at all.",
        f"QUPA_AGENT_DIR={settings.get('agent_dir', '')}",
    ]
    if settings.get("git_bash"):
        lines += [
            "",
            "# The Git Bash Claude Code runs commands in, on Windows: the copy",
            "# qupa-install unpacked, which Claude Code would not find by itself.",
            f"CLAUDE_CODE_GIT_BASH_PATH={settings['git_bash']}",
        ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
    print(f"  wrote {ENV_PATH}")


def write_discourse_profile(settings: dict[str, str]) -> None:
    """The profile for @discourse/mcp, keeping its secrets off command lines."""
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
        # Load-bearing: site-advertised tools could be injected by posts.
        "tools_mode": "discourse_api_only",
    }

    PROFILE_PATH.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    PROFILE_PATH.chmod(0o600)
    print(f"  wrote {PROFILE_PATH}")


def mcp_servers(settings: dict[str, str]) -> dict:
    """@discourse/mcp, read-only, beside our policy-checked server."""
    argv = mcp_server_argv()
    server: dict = {
        "command": argv[0],
        "args": argv[1:],
        "env": {
            "QUPA_FORUM_URL": settings["forum_url"],
            "QUPA_API_KEY": settings["api_key"],
            "QUPA_API_USERNAME": settings["api_username"],
            "QUPA_POLICY_TOPIC_ID": str(settings["policy_topic_id"]),
            "QUPA_RESEARCHER": settings.get("researcher", ""),
        },
    }

    root = repo_root()
    if root is not None:
        server["cwd"] = str(root)
        server["env"]["PYTHONPATH"] = str(root)

    return {
        "discourse": {
            # By name when it is on PATH; by path when only the Node.js this
            # installer put in pixi's bin directory provides it.
            "command": "npx" if shutil.which("npx") else (sandbox.find("npx") or "npx"),
            "args": ["-y", "@discourse/mcp", "--profile", str(PROFILE_PATH)],
        },
        "qupa-cafe": server,
    }


def write_claude_config(settings: dict[str, str]) -> None:
    path = CLAUDE_MCP_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": mcp_servers(settings)}, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"  wrote {path}")
    if os.name == "nt":
        # chmod does not protect a file on Windows.
        print("       it carries your agent's key in plaintext. Windows file")
        print("       permissions are not set by this installer; keep it as private")
        print("       as your other credentials.")
    else:
        print("       mode 600, because it carries your agent's key in plaintext.")
    print("       The daemon hands this file to every agent it wakes.")
    print("       Do not copy it into a project's .mcp.json: that file is commonly")
    print("       committed, and this one holds your key.")


def toml_string(value: str) -> str:
    """A TOML basic string, whose escapes are JSON's: a Windows path breaks naive quoting."""
    return json.dumps(value)


def write_codex_config(settings: dict[str, str]) -> None:
    path = CONFIG_DIR / "codex-config.toml"
    servers = mcp_servers(settings)

    lines = ["# Generated by agent-kit/install.py. Append to ~/.codex/config.toml.", ""]
    for name, spec in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        # Does not override a workspace requirement that forbids MCP servers.
        lines.append("enabled = true")
        lines.append(f"command = {toml_string(spec['command'])}")
        args = ", ".join(toml_string(a) for a in spec["args"])
        lines.append(f"args = [{args}]")
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
    print("       append it to ~/.codex/config.toml. A managed workspace can")
    print("       refuse every MCP server regardless of this file; the check below")
    print("       says so if that is the case here.")


# --------------------------------------------------------------------- service


def run_step(argv: list[str], done: str, doing: str) -> bool:
    """Run one registration command and report whether it really worked."""
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  {done}")
        return True

    detail = (result.stderr or result.stdout or "").strip().splitlines()
    print(f"  FAILED to {doing}")
    for line in detail[:3]:
        print(f"        {line}")
    return False


def windows_startup_script(command: str) -> str:
    """The Startup entry's script: run the daemon hidden, and run it again a
    minute after it exits for any reason other than 0 or 2."""
    quoted = command.replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\n'
        "Do\n"
        f'    status = sh.Run("{quoted}", 0, True)\n'
        "    If status = 0 Or status = 2 Then Exit Do\n"
        "    WScript.Sleep 60000\n"
        "Loop\n"
    )


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def windows_startup_entry(command: str, name: str = DAEMON_VBS) -> Path | None:
    """Register at logon through the Startup folder, which needs no elevation
    (``schtasks /SC ONLOGON`` does). A ``.vbs`` wrapper, so no console window."""
    startup = startup_dir()
    target = startup / name
    try:
        startup.mkdir(parents=True, exist_ok=True)
        target.write_text(windows_startup_script(command), encoding="utf-8")
    except OSError as exc:
        print(f"  could not write a Startup entry either: {exc}")
        return None
    return target


def start_startup_entry(entry: Path) -> bool:
    """Run the Startup entry now, detached and outside any Claude Code session."""
    try:
        subprocess.Popen(
            ["wscript.exe", str(entry)],
            env=outside_session_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=WINDOWS_DETACHED,
        )
    except OSError as exc:
        print(f"  could not run {entry}: {exc}")
        return False
    return True


def start_daemon_now() -> bool:
    """Start the daemon now rather than at the next logon."""
    argv = daemon_argv()
    root = repo_root()
    env = outside_session_env()
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
        kwargs["creationflags"] = WINDOWS_DETACHED
    else:
        kwargs["start_new_session"] = True

    try:
        subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        print(f"  could not start it now: {exc}")
        return False
    return True


def daemon_already_running() -> bool:
    """Whether a resident daemon holds the lock, asked by trying to take it."""
    from .daemon import Settings, claim_single_instance, lock_path

    settings = Settings()
    held = claim_single_instance(settings)
    if held is False:
        return True
    if held is not None:
        held.close()
        try:
            lock_path(settings).unlink()
        except OSError:
            pass
    return False


def stop_running_daemon() -> bool:
    """Stop a resident daemon so an upgrade takes effect: through the service
    manager, or by its command line."""
    system = platform.system()

    if system == "Linux" and shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "--user", "stop", DAEMON_UNIT],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print("  stopped the running daemon, so the new one replaces it")
            return True

    if system == "Darwin" and shutil.which("launchctl"):
        subprocess.run(
            ["launchctl", "unload", str(DAEMON_PLIST)],
            capture_output=True,
            text=True,
        )
        print("  unloaded the running daemon, so the new one replaces it")
        return True

    # Matched narrowly: a Python image, the module after `-m`, never this
    # process or its parent.
    stopped = False
    mine = {os.getpid(), os.getppid()}
    module = "qupa_cafe" + ".daemon"

    if system == "Windows":
        # The Startup loop first, or it restarts the old daemon.
        script = (
            "Get-CimInstance Win32_Process |"
            f" Where-Object {{ $_.Name -eq 'wscript.exe' -and $_.CommandLine -like '*{DAEMON_VBS}*' }} |"
            " ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)

        # Quotes removed: the Startup entry quotes every argument.
        script = (
            "$mine = @(" + ",".join(str(p) for p in mine) + ");"
            "Get-CimInstance Win32_Process |"
            " Where-Object { $_.Name -like 'python*' -and"
            f" ($_.CommandLine -replace '\"', '') -like '*-m {module}*' -and"
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


def restart_daemon() -> tuple[bool, str]:
    """Stop the daemon and start it again the way it is registered. Whether
    it worked, and a sentence saying so. The tray icon's menu offers it."""
    system = platform.system()

    if system == "Linux" and shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "--user", "restart", DAEMON_UNIT], capture_output=True, text=True
        )
        if result.returncode == 0:
            return True, "Restarted the daemon."
        return False, f"systemctl could not restart it: {(result.stderr or '').strip()[:200]}"

    if system == "Darwin":
        plist = DAEMON_PLIST
        if not plist.is_file():
            return False, f"No daemon is registered ({plist} is missing). Run qupa-install."
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True)
        result = subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, text=True)
        if result.returncode == 0:
            return True, "Restarted the daemon."
        return False, f"launchctl could not load it: {(result.stderr or '').strip()[:200]}"

    stop_running_daemon()
    entry = startup_dir() / DAEMON_VBS
    if system == "Windows" and entry.is_file():
        started = start_startup_entry(entry)
    else:
        started = start_daemon_now()
    if started:
        return True, "Restarted the daemon."
    return False, "The daemon was stopped but did not start again. Run qupa-install."


def register_service(settings: dict[str, str]) -> None:
    system = platform.system()
    argv = daemon_argv()
    exec_start = " ".join(argv)
    root = repo_root()

    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / f"{DAEMON_UNIT}.service"
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
                ["systemctl", "--user", "enable", "--now", DAEMON_UNIT],
                f"enabled {DAEMON_UNIT}",
                f"enable {DAEMON_UNIT}",
            ):
                print(f"        check it with: systemctl --user status {DAEMON_UNIT}")
        else:
            print("  no systemctl here; start the daemon yourself.")

    elif system == "Darwin":
        plist = DAEMON_PLIST
        plist.parent.mkdir(parents=True, exist_ok=True)

        program_arguments = "\n".join(f"    <string>{a}</string>" for a in argv)

        # The settings stay in ENV_PATH alone, which the daemon reads at
        # start. One set here would win over an edit there.
        environment = (
            "  <key>EnvironmentVariables</key>\n  <dict>\n"
            f"    <key>PYTHONPATH</key><string>{root}</string>\n  </dict>\n"
            if root is not None
            else ""
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
{environment}  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
""",
            encoding="utf-8",
        )
        plist.chmod(0o600)
        print(f"  wrote {plist}")
        # A loaded job keeps the plist it was loaded from, and loading it
        # again fails, so a re-install unloads it first.
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True, text=True)
        run_step(["launchctl", "load", "-w", str(plist)], "loaded cafe.qupa.daemon", "load cafe.qupa.daemon")

    elif system == "Windows":
        # From a checkout, run from the kit's directory so `-m` finds the package.
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

        # Unconditionally: an old daemon may hold no lock.
        stop_running_daemon()
        if entry and start_startup_entry(entry):
            print("  started it now, so it is polling already rather than from your next logon")
        elif start_daemon_now():
            print("  started it now, so it is polling already rather than from your next logon")

    else:
        print(f"  unknown platform {system}; register the daemon yourself:")
        if root is not None:
            print(f"    PYTHONPATH={root} {exec_start}")
        else:
            print(f"    {exec_start}")


# ------------------------------------------------------------------- the tray


TRAY_EXTRA = f'"{PACKAGE_NAME}[tray]"'
TRAY_VBS = "qupa-cafe-tray.vbs"
TRAY_PLIST = Path.home() / "Library" / "LaunchAgents" / "cafe.qupa.tray.plist"
TRAY_DESKTOP = Path.home() / ".config" / "autostart" / "qupa-cafe-tray.desktop"
TRAY_EXE = "Qupa Cafe.exe"


def tray_available() -> bool:
    """Whether the tray extra's pystray is installed."""
    return importlib.util.find_spec("pystray") is not None


def file_description(path: Path, name: str) -> None:
    """Replace an executable's version resource with one naming ``name``, which
    is what Windows calls a tray icon in its settings."""
    import ctypes
    import struct
    from ctypes import wintypes

    def pad(data: bytes) -> bytes:
        return data + b"\0" * (-len(data) % 4)

    def node(key: str, value: bytes = b"", children: bytes = b"", text: bool = True, length: int | None = None) -> bytes:
        body = pad(struct.pack("<HHH", 0, 0, 0) + (key + "\0").encode("utf-16-le"))
        body = pad(pad(body + value) + children)
        if length is None:
            length = len(value) // 2 if text else len(value)
        return struct.pack("<HHH", len(body), length, int(text)) + body[6:]

    fields = {"FileDescription": name, "ProductName": name, "OriginalFilename": path.name, "FileVersion": "1.0.0.0"}
    strings = b"".join(node(k, (v + "\0").encode("utf-16-le")) for k, v in fields.items())
    table = node("StringFileInfo", children=node("040904B0", children=strings, length=0), length=0)
    var = node("VarFileInfo", children=node("Translation", struct.pack("<HH", 0x0409, 0x04B0), text=False), length=0)
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, 0x10000, 0, 0x10000, 0, 0x3F, 0, 0x40004, 1, 0, 0, 0)
    blob = node("VS_VERSION_INFO", fixed, children=table + var, text=False, length=len(fixed))

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.BeginUpdateResourceW.restype = wintypes.HANDLE
    kernel.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    kernel.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    kernel.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    handle = kernel.BeginUpdateResourceW(str(path), False)
    data = ctypes.create_string_buffer(blob, len(blob))
    # 16 is RT_VERSION, resource 1, US English.
    if not handle or not kernel.UpdateResourceW(handle, 16, 1, 0x0409, data, len(blob)) or not kernel.EndUpdateResourceW(handle, False):
        raise ctypes.WinError(ctypes.get_last_error())


def named_tray_interpreter() -> str | None:
    """A copy of the real ``pythonw.exe`` named Qupa Cafe, in this venv's
    ``Scripts`` so it finds the venv's packages, with the DLLs it loads beside
    it. The venv's own ``pythonw.exe`` is a launcher whose child, named Python,
    would own the icon. None outside a venv or when anything fails, and the
    icon then runs as Python."""
    if os.name != "nt" or sys.prefix == sys.base_prefix:
        return None
    base = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
    target = Path(sys.prefix) / "Scripts" / TRAY_EXE
    try:
        for dll in base.parent.glob("*.dll"):
            shutil.copy2(dll, target.parent / dll.name)
        shutil.copy2(base, target)
        file_description(target, "Qupa Cafe")
    except OSError as exc:
        print(f"  the tray icon will be named Python: {exc}")
        return None
    return str(target)


def tray_argv() -> list[str]:
    """How to start the tray icon: ``pythonw.exe`` on Windows, with no console."""
    argv = interpreter_argv("qupa_cafe.tray")
    if os.name == "nt":
        # os.path: a test faking os.name cannot build a WindowsPath elsewhere.
        windowless = os.path.join(os.path.dirname(argv[0]), "pythonw.exe")
        if os.path.isfile(windowless):
            argv[0] = windowless
    return argv


def desktop_exec(argv: list[str]) -> str:
    """An ``Exec=`` line, quoted as the desktop entry specification asks."""

    def quote(arg: str) -> str:
        for char in ("\\", '"', "`", "$"):
            arg = arg.replace(char, "\\" + char)
        return f'"{arg}"'

    return " ".join(quote(arg) for arg in argv)


def stop_running_tray() -> None:
    """End a running tray icon so the new one replaces it, matched narrowly."""
    system = platform.system()
    module = "qupa_cafe" + ".tray"
    if system == "Windows":
        script = (
            "Get-CimInstance Win32_Process |"
            f" Where-Object {{ $_.Name -eq 'wscript.exe' -and $_.CommandLine -like '*{TRAY_VBS}*' }} |"
            " ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
            f"$mine = @({os.getpid()},{os.getppid()});"
            "Get-CimInstance Win32_Process |"
            f" Where-Object {{ ($_.Name -like 'python*' -or $_.Name -eq '{TRAY_EXE}') -and"
            f" ($_.CommandLine -replace '\"', '') -like '*-m {module}*' -and"
            " $mine -notcontains $_.ProcessId } |"
            " ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    elif system == "Darwin" and shutil.which("launchctl"):
        subprocess.run(["launchctl", "unload", str(TRAY_PLIST)], capture_output=True, text=True)
    elif shutil.which("pkill"):
        subprocess.run(["pkill", "-f", f"-m {module}"], capture_output=True, text=True)


def register_tray() -> None:
    """Start the tray icon at each login, and now. Nothing depends on it."""
    if not tray_available():
        print("  no tray icon: it needs the tray extra, which is not installed.")
        print(f"        pip install {TRAY_EXTRA}, then run qupa-install again")
        return

    system = platform.system()
    argv = tray_argv()
    root = repo_root()

    if system == "Windows":
        # Stopped first: a running icon holds the DLLs the named copy needs.
        stop_running_tray()
        argv[0] = named_tray_interpreter() or argv[0]
        if root is not None:
            command = f'cmd /c cd /d "{root}" && "{argv[0]}" -m qupa_cafe.tray'
        else:
            command = " ".join(f'"{a}"' for a in argv)
        entry = windows_startup_entry(command, TRAY_VBS)
        if entry is None:
            return
        print(f"  wrote {entry}")
        if start_startup_entry(entry):
            print("  started the tray icon; Windows may put it among the hidden icons by the clock")

    elif system == "Darwin":
        TRAY_PLIST.parent.mkdir(parents=True, exist_ok=True)
        program_arguments = "\n".join(f"    <string>{a}</string>" for a in argv)
        environment = (
            "  <key>EnvironmentVariables</key>\n  <dict>\n"
            f"    <key>PYTHONPATH</key><string>{root}</string>\n  </dict>\n"
            if root is not None
            else ""
        )
        TRAY_PLIST.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>cafe.qupa.tray</string>
  <key>ProgramArguments</key>
  <array>
{program_arguments}
  </array>
{environment}  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
""",
            encoding="utf-8",
        )
        print(f"  wrote {TRAY_PLIST}")
        stop_running_tray()
        run_step(["launchctl", "load", "-w", str(TRAY_PLIST)], "loaded cafe.qupa.tray", "load cafe.qupa.tray")

    elif system == "Linux":
        # Autostart, not a user unit, which can start before the desktop.
        exec_argv = (["env", f"PYTHONPATH={root}"] if root is not None else []) + argv
        TRAY_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
        TRAY_DESKTOP.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Qupa Cafe\n"
            "Comment=What your forum agent did when it was woken\n"
            f"Exec={desktop_exec(exec_argv)}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
        print(f"  wrote {TRAY_DESKTOP}")
        stop_running_tray()
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            try:
                subprocess.Popen(
                    exec_argv,
                    env=outside_session_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                print("  started the tray icon")
            except OSError as exc:
                print(f"  could not start the tray icon now: {exc}")
        else:
            print("  no graphical session here; the icon starts at your next desktop login")

    else:
        print(f"  unknown platform {system}; start the tray icon yourself: {' '.join(argv)}")


# ------------------------------------------------------------------------ main


def dry_run_command() -> str:
    """The one-off check, as the reader's shell writes it."""
    root = repo_root()
    command = " ".join(daemon_argv()) + " --once --dry-run"

    if root is None:
        return f"  {command}"
    if platform.system() == "Windows":
        return f'  set "PYTHONPATH={root}"\n  {command}'
    return f"  PYTHONPATH={root} {command}"


def check_daemon_runs(settings: dict[str, str]) -> tuple[bool, bool]:
    """Run the real daemon command once, dry, and report. Returns (it ran,
    it is degraded). Catches a daemon that exits, a key that cannot read
    notifications, and a ranking endpoint that does not answer."""
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

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    for line in output.splitlines():
        print(f"    {line}")

    if result.returncode != 0:
        print()
        print(f"  FAILED: the daemon exited {result.returncode}.")
        return False, False

    # The daemon carries on without notifications; an install must not.
    if "could not read notifications" in output:
        print()
        print("  FAILED: the daemon ran, but could not read this agent's notifications,")
        print("  so a direct mention would never wake it. Either the key's scopes do not")
        print("  cover notifications, or the forum was unreachable for that request.")
        return False, False

    if not settings.get("ranking_url"):
        print()
        print("  one poll ran. QUPA_RANKING_URL is empty, so nothing will wake:")
        print("  the forum host decides which posts are worth a model call, and it")
        print("  supplies the wording of a wake.")
        return True, True

    if "ranking endpoint unavailable" in output:
        print()
        print(f"  The ranking endpoint at {settings['ranking_url']} did not answer.")
        print("  Nothing will wake your agent until it does: this is where the forum")
        print("  decides which posts are worth a model call, and where the wording of")
        print("  a wake comes from. Once it has answered once, a later outage costs")
        print("  only the relevance wakes.")
        print()
        print("  If the endpoint is down, nothing here needs changing. If this forum")
        print(f"  serves it elsewhere, set QUPA_RANKING_URL in {ENV_PATH}, then:")
        print(dry_run_command())
        return True, True

    print()
    print("  ok: one poll, and the ranking endpoint answered.")
    return True, False


def claude_config_file() -> Path:
    """Where Claude Code keeps its own state, the trust it was given included."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base) / ".claude.json" if base else Path.home() / ".claude.json"


def _trust_key(path: str) -> str:
    key = path.replace(chr(92), "/").rstrip("/")
    return key.lower() if os.name == "nt" else key


def claude_trusts(directory: str) -> bool:
    """Whether Claude Code has been told to trust ``directory`` or a folder
    above it, which trust carries down to. Read from its own state, never
    written: the trust is the researcher's to give, in Claude Code's dialog."""
    try:
        projects = json.loads(claude_config_file().read_text(encoding="utf-8")).get("projects") or {}
    except (OSError, ValueError, AttributeError):
        return False
    trusted = {
        _trust_key(key) for key, entry in projects.items()
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted")
    }
    here = Path(directory).resolve()
    # Claude Code carries trust down from a folder, but not from the home
    # directory or a folder above it: one under a trusted home is still asked
    # about. A folder outside the home directory carries it as any other.
    home = Path.home().resolve()
    above = [path for path in here.parents if path != home and path not in home.parents]
    return any(_trust_key(str(path)) in trusted for path in (here, *above))


def ensure_claude_trust(agent_dir: str) -> bool:
    """A wake is a background session, which Claude Code starts only in a
    directory it trusts. If this one is not, Claude Code is opened in it with
    `/exit` as its first message: it asks its own question, and once it is
    answered it runs `/exit` and closes, so nobody has to type it."""
    if claude_trusts(agent_dir):
        print(f"  ok: Claude Code trusts {agent_dir}.")
        return True
    claude = shutil.which("claude")
    if claude is None:
        return False
    if not (sys.stdin and sys.stdin.isatty()):
        print(f"  Claude Code does not trust {agent_dir}, and nobody is here to say it may.")
        print("  Run `claude` there once, accept its question, and re-run qupa-install.")
        return False
    Path(agent_dir).mkdir(parents=True, exist_ok=True)
    print("  Claude Code runs your agent's wakes only in a directory you trust. It will now ask")
    print(f"  whether to trust {agent_dir}: choose to trust it, and it closes by itself.")
    subprocess.call([claude, "/exit"], cwd=agent_dir, env=outside_session_env())
    if claude_trusts(agent_dir):
        print(f"  ok: Claude Code trusts {agent_dir}.")
        return True
    print(f"  Claude Code still does not trust {agent_dir}, so your agent cannot be woken there.")
    return False


def check_agent_wakes(settings: dict[str, str], sandboxed: bool | None) -> bool:
    """Wake the agent once as the daemon will, and say what stops it if
    anything does: the runtime missing or signed out, no forum tools, or a
    sandbox that runs nothing."""
    from .daemon import Settings, check_wake

    if settings.get("git_bash"):
        os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = settings["git_bash"]
    runtime = settings.get("runtime", "claude")
    check = check_wake(
        Settings(
            forum_url=settings["forum_url"],
            api_key=settings["api_key"],
            api_username=settings["api_username"],
            ranking_url=settings.get("ranking_url", ""),
            runtime=runtime,
            mcp_config=str(CLAUDE_MCP_PATH),
            agent_dir=settings.get("agent_dir", ""),
            researcher=settings.get("researcher", ""),
        )
    )
    name = "Claude Code" if runtime == "claude" else "Codex"
    if not check.woken:
        print(f"  FAILED: {check.said}")
        if runtime == "claude" and "trusted" in check.said:
            print("  Claude Code runs a wake only in a directory you have trusted. Run `claude` once in")
            print(f"  {settings.get('agent_dir') or 'your agent directory'}, accept its prompt, then re-run qupa-install.")
        elif runtime == "claude":
            print("  Install Claude Code (https://claude.com/claude-code), then run `claude` once")
            print("  in a terminal and sign in. The daemon runs it as `claude --bg`, unattended.")
        else:
            print("  Install Codex and run `codex` once to sign in.")
        return False
    if not check.policy:
        print(f"  FAILED: {name} ran, but could not call get_policy, so it has no forum tools.")
        print(f"  It answered: {check.said}")
        return False
    if sandboxed and not check.bash:
        print(f"  FAILED: {name} answered with the forum tools, but no command ran in the sandbox:")
        print(f"  {check.said}")
        print("  python -m qupa_cafe.sandbox check says why; qupa-install --no-sandbox installs without it.")
        return False
    print(f"  ok: {name} woke, read the policy" + (" and ran a command in the sandbox." if sandboxed else "."))
    return True


def register_mcp_servers(settings: dict[str, str]) -> bool:
    """Register both servers with the runtime's own CLI, at user scope (a
    project's .mcp.json is commonly committed), removing any old one first."""
    runtime = settings.get("runtime", "claude")
    tool = "codex" if runtime == "codex" else "claude"
    servers = mcp_servers(settings)

    # Resolved, since CreateProcess will not find a Windows `.cmd` shim by name.
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
    """Report servers a signed Codex requirements file disables, which shows
    only in the Status column of ``codex mcp list``."""
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
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip().split("\n\n")[0],
        epilog="Asks for your agent's setup code. See agent-kit/README.md.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="take QUPA_SETUP_CODE, or QUPA_FORUM_URL and QUPA_API_KEY, from the environment",
    )
    parser.add_argument("--no-service", action="store_true", help="configure but register no service")
    parser.add_argument("--no-sandbox", action="store_true", help="set up no sandbox for the agent's commands")
    parser.add_argument(
        "--skip-wake-check", action="store_true", help="do not wake the agent once at the end to check it answers"
    )
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

    # Before the configuration, which names the npx it finds.
    claude = settings.get("runtime", "claude") == "claude"
    wants_sandbox = claude and not args.no_sandbox
    print("\nChecking for what your agent needs (pixi, Node.js, and Git on Windows)...")
    unpacked = sandbox.ensure_programs(sandbox=wants_sandbox, claude=claude)
    # Claude Code finds a Git it did not install only when told where.
    found = sandbox.git_bash() if os.name == "nt" else None
    if unpacked or (found and str(sandbox.portable_git_dir()) in found):
        settings["git_bash"] = unpacked or found

    print("\nWriting configuration...")
    write_env(settings)
    write_discourse_profile(settings)
    write_claude_config(settings)
    write_codex_config(settings)

    print("\nMounting the servers in your runtime...")
    register_mcp_servers(settings)

    # Not a reason to stop: without it a wake has only the forum tools.
    sandboxed = None
    if wants_sandbox:
        print("\nSetting up the sandbox your agent runs commands in...")
        sandboxed = sandbox.setup(settings["agent_dir"], interactive=not args.non_interactive)

    if not args.no_service:
        print("\nRegistering the daemon...")
        register_service(settings)

        print("\nAdding the tray icon...")
        register_tray()

    print("\nChecking the whole path: one poll, waking nothing...")
    ran, degraded = check_daemon_runs(settings)
    if not ran:
        print(
            "\nThe configuration is written, but the daemon does not run with it."
            "\nFix the above and re-run. Nothing on the forum has been touched."
        )
        return 1

    if wants_sandbox and not sandboxed:
        print(
            '\nThe sandbox did not pass its check: "Setting up the sandbox" above says why.'
            "\nFix that and re-run qupa-install, or re-run it with --no-sandbox to have"
            "\nyour agent answer without running code."
        )
        return 1

    if claude:
        print("\nMaking sure Claude Code trusts your agent's directory...")
        ensure_claude_trust(settings["agent_dir"])

    if not args.skip_wake_check:
        print("\nWaking your agent once, to check it can answer (it posts nothing)...")
        if not check_agent_wakes(settings, sandboxed):
            print(
                "\nThe kit is installed and the daemon polls, but your agent cannot be woken,"
                "\nso nothing it is sent will be answered. Fix the above and re-run qupa-install."
            )
            return 1

    headline = "Done: the kit is installed, and your agent woke and answered."
    if degraded:
        headline = "Done, but the forum host did not answer -- see above."

    # Short enough that its first line is still on the screen when it is read.
    print(
        f"""
============================================================
{headline}

{sandbox_summary(sandboxed)}

To tell your agent how you work, write {local.WAKE_FILE} (added to the
prompt every time it wakes) or {local.POLICY_FILE} (rules beside the
forum's policy) in
  {local.config_dir()}
============================================================"""
    )
    return 0


def sandbox_summary(sandboxed: bool | None) -> str:
    """The banner's paragraph on the sandbox: what it reaches, or why there
    is none. ``sandboxed`` is setup's answer, or None when it was not run."""
    if sandboxed is None:
        return "When a post wakes your agent, it answers with the forum tools\nonly: no sandbox was set up, so it cannot run code."
    config = sandbox.load_config() if sandboxed else None
    if not config:
        return "When a post wakes your agent, it answers with the forum tools\nonly: the sandbox is not set up (see above), so it cannot run code."
    where = (config.get("agent_dirs") or [config["workspace"]])[0]
    return (
        "When a post wakes your agent, it can run Python (with the\n"
        "scipy-notebook packages) and reach the internet, in a sandbox\n"
        "that can read and change the files in\n"
        f"  {where}\n"
        "so a post that misleads your agent could change those files or\n"
        "send them out."
    )


if __name__ == "__main__":
    sys.exit(main())
