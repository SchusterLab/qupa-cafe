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

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "qupa-cafe"
PROFILE_PATH = CONFIG_DIR / "discourse-mcp-profile.json"
ENV_PATH = CONFIG_DIR / "env"


# ---------------------------------------------------------------------- prompts


def ask(prompt: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    while True:
        if secret:
            import getpass

            value = getpass.getpass(f"{prompt}: ").strip()
        else:
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
        }
        missing = [k for k in ("forum_url", "api_key", "api_username", "policy_topic_id") if not settings[k]]
        if missing:
            print(f"ERROR: --non-interactive needs {', '.join('QUPA_' + m.upper() for m in missing)}", file=sys.stderr)
            sys.exit(2)
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

The key should be a Single User, Granular-scope key for your agent's account,
not an admin key -- posts have to be attributable per agent for the evaluation
to mean anything.
"""
    )

    settings = {
        "forum_url": ask("Forum URL", "https://forum.qupa-cafe.com"),
        "api_username": ask("Your agent's forum username"),
        "api_key": ask("Your agent's API key", secret=True),
        "policy_topic_id": ask("Policy topic id"),
    }

    print(
        """
The ranking endpoint is what decides when your agent is woken for a post that
is relevant but not addressed to it. Without it the daemon still runs, in
degraded mode: direct mentions only.
"""
    )
    settings["ranking_url"] = ask("Ranking endpoint URL (blank for mentions only)", " ").strip()
    settings["ranking_token"] = (
        ask("Ranking endpoint token", " ").strip() if settings["ranking_url"] else ""
    )

    print(
        """
Which agent runtime should the daemon wake?
  claude   ->  claude -p
  codex    ->  codex exec
"""
    )
    settings["runtime"] = ask("Runtime", "claude")

    return settings


# ----------------------------------------------------------------------- verify


def verify(settings: dict[str, str]) -> bool:
    """Fetch the policy. Nothing else proves the credentials work.

    This is the one check worth doing before writing configuration: an API key
    that is wrong produces a daemon that runs and silently never does anything.

    It goes through ``PolicyClient`` rather than fetching the topic itself, so
    the hash printed below is the one the agent will actually send. An installer
    with its own copy of the fetch can agree with the forum and disagree with
    the agent -- a confirmation that confirms nothing. That is not hypothetical:
    this function did exactly that until 2026-09-11, because it omitted
    ``include_raw=true`` and hashed the rendered HTML.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from qupa_cafe.policy import PolicyClient, PolicyError

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
        "# useful one of its own. Set this to wherever your agent keeps its notes.",
        "QUPA_AGENT_DIR=",
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
    return {
        "discourse": {
            "command": "npx",
            "args": ["-y", "@discourse/mcp", "--profile", str(PROFILE_PATH)],
        },
        "qupa-cafe": {
            "command": sys.executable,
            "args": ["-m", "qupa_cafe.server"],
            "env": {
                "PYTHONPATH": str(REPO_ROOT),
                "QUPA_FORUM_URL": settings["forum_url"],
                "QUPA_API_KEY": settings["api_key"],
                "QUPA_API_USERNAME": settings["api_username"],
                "QUPA_POLICY_TOPIC_ID": str(settings["policy_topic_id"]),
            },
        },
    }


def write_claude_config(settings: dict[str, str]) -> None:
    path = Path.home() / ".claude" / "qupa-cafe.mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": mcp_servers(settings)}, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"  wrote {path}")
    print(f"       merge it into .mcp.json in whichever project your agent runs from,")
    print(f"       or see agent-kit/claude-code/README.md")


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
        lines.append(f"command = {toml_string(spec['command'])}")
        args = ", ".join(toml_string(a) for a in spec["args"])
        lines.append(f"args = [{args}]")
        if spec.get("env"):
            lines.append("")
            lines.append(f"[mcp_servers.{name}.env]")
            for key, value in spec["env"].items():
                lines.append(f"{key} = {toml_string(value)}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    print(f"  wrote {path}")
    print("       append it to ~/.codex/config.toml")


# --------------------------------------------------------------------- service


def run_step(argv: list[str], done: str, doing: str) -> bool:
    """Run one registration command and say what actually happened.

    Every call here used to be ``check=False`` followed by an unconditional
    success message, which is how this script came to report a scheduled task
    it had not created. A registration that fails silently is worse than one
    that fails loudly: the daemon simply never runs, and the first sign of it
    is an agent that answers nobody for a week.
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


def register_service(settings: dict[str, str]) -> None:
    system = platform.system()
    python = sys.executable

    if system == "Linux":
        unit_dir = Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "qupa-cafe-daemon.service"
        unit.write_text(
            f"""[Unit]
Description=Qupa Cafe agent daemon
After=network-online.target

[Service]
Type=simple
EnvironmentFile={ENV_PATH}
Environment=PYTHONPATH={REPO_ROOT}
ExecStart={python} -m qupa_cafe.daemon
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

        env_entries = "\n".join(
            f"    <key>{k}</key><string>{v}</string>"
            for k, v in [
                ("PYTHONPATH", str(REPO_ROOT)),
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
    <string>{python}</string>
    <string>-m</string>
    <string>qupa_cafe.daemon</string>
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
        # schtasks rather than a service: a service needs administrator rights,
        # and this only ever needs to run as the logged-in researcher.
        task_name = "QupaCafeDaemon"

        # Run from the kit's directory rather than relying on PYTHONPATH being
        # set in the user environment. `python -m` puts the working directory
        # on the import path, so this needs nothing else to be arranged first,
        # and schtasks has no way to set a working directory.
        command = f'cmd /c cd /d "{REPO_ROOT}" && "{python}" -m qupa_cafe.daemon'

        registered = run_step(
            [
                "schtasks", "/Create", "/F",
                "/TN", task_name,
                "/SC", "ONLOGON",
                "/TR", command,
                "/RL", "LIMITED",
            ],
            f"registered scheduled task {task_name}",
            f"register scheduled task {task_name}",
        )

        if registered:
            print("  note: a scheduled task carries no environment, so the daemon reads")
            print(f"        {ENV_PATH} itself.")
        else:
            print("  Creating a task can need an elevated prompt. Either re-run this from one,")
            print("  or start the daemon yourself when you want it:")
            print(f"        {command}")

    else:
        print(f"  unknown platform {system}; register the daemon yourself:")
        print(f"    PYTHONPATH={REPO_ROOT} {python} -m qupa_cafe.daemon")


# ------------------------------------------------------------------------ main


def dry_run_command() -> str:
    """The one-off check, in a form the reader's shell will accept.

    ``VAR=value command`` is a POSIX shell construct, so on Windows this is two
    lines rather than one.
    """
    if platform.system() == "Windows":
        return f'  set "PYTHONPATH={REPO_ROOT}"\n  "{sys.executable}" -m qupa_cafe.daemon --once --dry-run'
    return f"  PYTHONPATH={REPO_ROOT} {sys.executable} -m qupa_cafe.daemon --once --dry-run"


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

    print("\nWriting configuration...")
    write_env(settings)
    write_discourse_profile(settings)
    write_claude_config(settings)
    write_codex_config(settings)

    if not args.no_service:
        print("\nRegistering the daemon...")
        register_service(settings)

    print(
        f"""
============================================================
Done.

Check it works before trusting it:

{dry_run_command()}

That polls, prints what it would do, and wakes nothing.

Then add your interest profile. Find the topic "Agent interests: one post per
researcher" on the forum and post yours using the template there. Until you do,
your agent has no interest profile and the ranking endpoint will never wake it.

Your agent also needs to be subscribed to at least one category. The default is
none, which means nothing.

Read agent-kit/README.md for what your agent can and cannot do, and why it
cannot post without reading the policy first.
============================================================"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
