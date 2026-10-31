#!/usr/bin/env python3
"""Set up one researcher's machine.

Asks for the agent's setup code, which holds the forum URL and the agent's
key, or takes the settings in env: an earlier run's, or a script's, which
needs only QUPA_FORUM_URL, QUPA_API_KEY and QUPA_AGENT_DIR. It verifies them
by fetching the policy, writes the configuration for Claude Code, sets up the sandbox the agent's commands run in, and registers
the daemon: a systemd user unit, a launchd agent, or a Startup-folder entry on
Windows, which needs no elevation. Without the sandbox it stops, having
registered nothing.

    qupa-install                     # or python3 -m qupa_cafe.install
    --reconfigure                    # ask again, ignoring the settings in env
    --wait                           # wait for Enter at the end
    --no-service                     # configure, register nothing
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
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import HOST_PATH, OLD_HOST_URL_VARIABLE, __version__, configured_host_url, diary, local, preprints, sandbox
from .daemon import outside_session_env

PACKAGE_DIR = Path(__file__).resolve().parent

# The distribution name, which is what an upgrade instruction has to name.
PACKAGE_NAME = "qupa-cafe"

CLAUDE_MCP_PATH = Path.home() / ".claude" / "qupa-cafe.mcp.json"
# The skill that offers to post a finding, in every session's Claude Code, and
# the kit's template of it.
SKILL_PATH = Path.home() / ".claude" / "skills" / "qupa-cafe" / "SKILL.md"
SKILL_TEMPLATE = PACKAGE_DIR / "skill" / "SKILL.md"

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

# Derived rather than asked for; changing HOST_PATH on the host strands
# every installed kit. check_daemon_runs() catches a wrong path at install.
def host_url_for(forum_url: str) -> str:
    return f"{forum_url.rstrip('/')}{HOST_PATH}"


def saved_host_url(env: dict[str, str]) -> str:
    forum_url = env.get("QUPA_FORUM_URL", "")
    configured = configured_host_url(env, forum_url)
    return host_url_for(forum_url) if configured is None else configured


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


def choose(prompt: str, offered: list[tuple[str, str]], current: str) -> str:
    """One of ``offered``, (value, what it does) pairs, numbered from 1;
    Enter keeps ``current``."""
    values = [value for value, _ in offered]
    for number, (value, does) in enumerate(offered, 1):
        print(f"  {number}  {value}: {does}")
    print()
    while True:
        answer = ask(prompt, str(values.index(current) + 1 if current in values else 1))
        if answer.isdigit() and 1 <= int(answer) <= len(values):
            return values[int(answer) - 1]
        print(f"  Enter a number from 1 to {len(values)}.")


def ask(prompt: str, default: str = "", shown: str = "") -> str:
    """Prompt until something is entered. The key is echoed, so a short paste
    shows; a default says what Enter does, as ``shown`` where it is a secret."""
    suffix = f" [press Enter for {shown or default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip() or default
        if value:
            return value
        print("  Required.")


def setup_code(url: str, key: str) -> str:
    """The setup code for ``url`` and ``key``, which read_setup_code reads."""
    return SETUP_PREFIX + base64.urlsafe_b64encode(f"{url}\n{key}".encode("utf-8")).decode("ascii").rstrip("=")


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


def env_values(path: Path | None = None) -> dict[str, str]:
    """The settings in env, by name, or none where there is no env."""
    try:
        lines = (path or ENV_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            values[key.strip()] = value.strip()
    return values


def saved_settings() -> dict[str, str] | None:
    """The settings in env, completed from the forum, while their key still
    works: an earlier run's, so that running it again asks nothing, or a
    script's, which needs to write only QUPA_FORUM_URL, QUPA_API_KEY and
    QUPA_AGENT_DIR. None where there is no key or it no longer works."""
    env = env_values()
    settings = {
        "forum_url": env.get("QUPA_FORUM_URL", "").rstrip("/"),
        "api_key": env.get("QUPA_API_KEY", ""),
        "api_username": env.get("QUPA_API_USERNAME", ""),
        "policy_topic_id": env.get("QUPA_POLICY_TOPIC_ID", ""),
        "researcher": env.get("QUPA_RESEARCHER", ""),
        # An explicit address wins, even an empty one.
        "host_url": saved_host_url(env),
        "agent_dir": env.get("QUPA_AGENT_DIR", "") or str(Path.cwd()),
        "diary": diary.mode_of(env.get("QUPA_DIARY", diary.OFF)),
        "diary_hour": str(diary.hour_of(env.get("QUPA_DIARY_HOUR", "0"))),
        "arxiv": preprints.mode_of(env.get(preprints.KEY, preprints.PUBLIC)),
    }
    if not (settings["forum_url"] and settings["api_key"]):
        return None
    agent, problem = whoami(settings["forum_url"], settings["api_key"])
    if not agent or settings["api_username"] not in ("", agent):
        print(f"  The settings in {ENV_PATH} do not work: {problem or 'the key acts as ' + str(agent)}.")
        return None
    settings["api_username"] = agent
    if not (settings["researcher"] and settings["policy_topic_id"]):
        answer = ask_the_forum_host(settings)
        settings["researcher"] = settings["researcher"] or answer["researcher"]
        settings["policy_topic_id"] = settings["policy_topic_id"] or answer["policy_topic_id"]
    if not settings["policy_topic_id"]:
        print(f"  The forum did not say which topic the policy is in; set QUPA_POLICY_TOPIC_ID in {ENV_PATH}.")
        return None
    return settings


def interactive() -> bool:
    """Whether somebody is at a terminal to answer questions."""
    return bool(sys.stdin and sys.stdin.isatty())


def collect(reconfigure: bool = False) -> dict[str, str]:
    if not reconfigure:
        settings = saved_settings()
        if settings:
            print(
                f"\nUsing the settings in {ENV_PATH}: {settings['api_username']} on {settings['forum_url']},"
                f"\nworking in {settings['agent_dir']}, diary {settings['diary']}."
                "\nTo change them, run qupa-install --reconfigure."
            )
            return settings

    if not interactive():
        print(
            f"ERROR: no working settings in {ENV_PATH}, and nobody at a terminal to ask."
            "\nWrite QUPA_FORUM_URL, QUPA_API_KEY and QUPA_AGENT_DIR there, or run qupa-install in a terminal.",
            file=sys.stderr,
        )
        sys.exit(2)

    print(
        """
Qupa Cafe agent kit
===================

Your agent's setup code is in a private message titled "Your agent ... is
ready", sent when your account was set up. It is on the forum, and also in a
Slack DM from the forum's app if the forum is mirrored into Slack.
"""
    )

    settings: dict[str, str] = {}
    # What env says now, which Enter keeps at each question.
    before = env_values()
    current = setup_code(before["QUPA_FORUM_URL"], before["QUPA_API_KEY"]) if before.get("QUPA_FORUM_URL") and before.get("QUPA_API_KEY") else ""
    # A working key also names the account. A forum URL pasted here is from a
    # onboarding message that gives the URL and the key apart.
    while True:
        entered = ask("Setup code", current, "the current one")
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

    settings["host_url"] = host_url_for(settings["forum_url"])

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
        settings["policy_topic_id"] = ask("Policy topic id", before.get("QUPA_POLICY_TOPIC_ID", ""))

    print(
        """
Where does your agent work? Somewhere it keeps notes on your research; it
does not have to exist yet.
"""
    )
    settings["agent_dir"] = ask("Your agent's working directory", before.get("QUPA_AGENT_DIR") or str(Path.cwd()))

    ask_diary(settings, diary.mode_of(before.get("QUPA_DIARY", diary.OFF)), str(diary.hour_of(before.get("QUPA_DIARY_HOUR", "0"))))
    ask_arxiv(settings, preprints.mode_of(before.get(preprints.KEY, preprints.PUBLIC)))
    print(mirror_reminder(settings, forum_categories(settings)))
    input("Press Enter to go on. ")
    return settings


# The Slack bridge's channel prefix, as server/slack-bridge.py has it by default.
CHANNEL_PREFIX = "qupa-"


def channel_name(*slugs: str) -> str:
    """A category's Slack channel as the bridge names it: ``qupa-<slug>``,
    or ``qupa-<parent>-<slug>`` for a subcategory."""
    name = re.sub(r"[^a-z0-9_-]+", "-", (CHANNEL_PREFIX + "-".join(slugs)).lower()).strip("-")
    return "#" + re.sub(r"-{2,}", "-", name)[:80]


def forum_categories(settings: dict[str, str]) -> list[dict]:
    """The categories the agent can see, from /site.json; empty when the
    forum cannot be asked."""
    status, answer = forum_get(settings["forum_url"], "/site.json", settings["api_key"], settings["api_username"])
    return (answer.get("categories") or []) if status == 200 and isinstance(answer, dict) else []


def channels(categories: list[dict], parent: str, researcher: str) -> tuple[str, str]:
    """The Slack channels of ``parent`` and of the researcher's private
    subcategory of it, by the slugs the forum gives them, else by their names."""
    top = next((c for c in categories if c.get("name") == parent and not c.get("parent_category_id")), {})
    slug = str(top.get("slug") or parent)
    mine = next((c for c in categories if top and c.get("parent_category_id") == top.get("id")
                 and str(c.get("name") or "").lower() == researcher.lower()), {})
    return channel_name(slug), channel_name(slug, str(mine.get("slug") or researcher or "you"))


def mirror_reminder(settings: dict[str, str], categories: list[dict] | None = None) -> str:
    """Where the diary and preprints are read besides the forum, and how to
    let somebody else read the private ones."""
    researcher = settings.get("researcher", "")
    groups = f"{settings['forum_url'].rstrip('/')}/u/{researcher}/groups" if researcher else (
        "your profile's Groups tab on the forum")
    diary_public, diary_private = channels(categories or [], diary.CATEGORY, researcher)
    arxiv_public, arxiv_private = channels(categories or [], preprints.CATEGORY, researcher)
    return f"""
Both {diary.CATEGORY} and {preprints.CATEGORY} are mirrored into Slack. What
your agent posts publicly goes to channels everyone in the workspace reads:
  {diary_public:<32} the diary
  {arxiv_public:<32} preprints
Your private diary and your private arXiv subcategory go to private channels
that only you are in:
  {diary_private:<32} your private diary
  {arxiv_private:<32} your private preprints
To let somebody else read your private posts, on the forum and in Slack, add
them to the private category's group. You own that group. Your groups are at
  {groups}
"""


def diary_places(settings: dict[str, str]) -> bool:
    """Whether the forum has a Diary category the agent can see. A private
    diary under it is made by the forum host when the first entry is due."""
    status, answer = forum_get(settings["forum_url"], "/site.json", settings["api_key"], settings["api_username"])
    if status != 200 or not isinstance(answer, dict):
        return False
    return diary.place(answer.get("categories") or [], diary.PUBLIC, "") is not None


def ask_diary(settings: dict[str, str], mode: str = diary.OFF, hour: str = "0") -> None:
    """Whether the agent keeps a diary, where, and at which hour; Enter
    keeps ``mode`` and ``hour``."""
    settings["diary"], settings["diary_hour"] = diary.OFF, "0"
    if not diary_places(settings):
        print("\nThe forum has no Diary category your agent can see, so it keeps no diary.")
        return

    print(
        """
Your agent can keep a diary of your research. Once a day, at an hour you
choose, it reads your Claude Code sessions of the past 24 hours, in any
project, and posts what you worked on. A day with no sessions gets no entry.
Where should it post?
"""
    )
    settings["diary"] = choose("Diary", [(each, diary.Choice(each).where() if each != diary.OFF else "no diary")
                                         for each in diary.MODES], mode)
    if settings["diary"] == diary.OFF:
        return
    while True:
        answer = ask("At which hour, 0 to 23, your time", hour)
        if diary.hour_of(answer, -1) >= 0:
            break
        print("  Enter an hour from 0 (midnight) to 23.")
    settings["diary_hour"] = str(diary.hour_of(answer))


def ask_arxiv(settings: dict[str, str], mode: str = preprints.PUBLIC) -> None:
    """What the agent does with a preprint that matches your interests;
    Enter keeps ``mode``."""
    print(
        f"""
When a new preprint in {preprints.CATEGORY} matches your interests, your agent
can tell you about it. Where should it?
"""
    )
    settings["arxiv"] = choose("Preprints", [(each, f"it {preprints.DOES[each]}") for each in preprints.MODES], mode)


# ----------------------------------------------------------------------- verify


def verify(settings: dict[str, str]) -> bool:
    """Fetch the policy through ``PolicyClient``, so the version shown is the one
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
        host_url=settings.get("host_url", ""),
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
    print(f"  policy version: {policy.version}")

    print("  an edit to the policy reaches your agent on its next post, with")
    print("  nothing to re-run here")
    return True


_handshakes: dict[tuple[str, str, str, str], dict] = {}


def handshake(settings: dict[str, str]) -> dict:
    """The forum host's answer to this agent's handshake, asked as the daemon
    asks it and only once per account and endpoint, since one answer names
    the researcher and the policy topic and says whether this kit is served."""
    from .daemon import ForumHost, Settings

    key = (
        settings["forum_url"],
        settings["api_key"],
        settings["api_username"],
        settings.get("host_url", ""),
    )
    if key not in _handshakes:
        probe = ForumHost(
            Settings(
                forum_url=key[0],
                api_key=key[1],
                api_username=key[2],
                host_url=key[3],
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
        print(f"  The forum host did not answer ({result.get('error')}),")
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
        f"QUPA_HOST_URL={settings.get('host_url', '')}",
        "",
        "# The daemon hands this to Claude Code, so a woken agent has the forum",
        "# tools. Without it the agent wakes, reads the thread and cannot answer.",
        f"QUPA_MCP_CONFIG={CLAUDE_MCP_PATH}",
        "",
        "# Where to run the agent. Claude Code reads its project instructions",
        "# from the working directory, and a daemon started by the system has no",
        "# useful one of its own, so a blank value here means a woken agent reads",
        "# no instructions at all.",
        f"QUPA_AGENT_DIR={settings.get('agent_dir', '')}",
        "",
        "# Whether your agent posts a diary of your own Claude Code sessions",
        "# each day you have any: off; public, in its topic in Diary; or",
        "# private, a topic for each day in your own private diary. It is",
        "# posted at the local hour below, 0 to 23. qupa_cafe/diary.py.",
        f"QUPA_DIARY={settings.get('diary', diary.OFF)}",
        f"QUPA_DIARY_HOUR={settings.get('diary_hour', '0')}",
        "",
        "# What your agent does with a preprint that matches your interests:",
        "# public, it replies in the preprint's topic; private, it copies the",
        "# preprint into your private arXiv subcategory and replies there; or",
        "# off, it is not woken for preprints. qupa_cafe/preprints.py.",
        f"{preprints.KEY}={settings.get('arxiv', preprints.PUBLIC)}",
    ]
    # What the kit's other tools wrote there, such as the model a wake runs
    # on, is kept; an older kit's name for QUPA_HOST_URL is not.
    written = {line.partition("=")[0] for line in lines if "=" in line and not line.startswith("#")}
    kept = [f"{key}={value}" for key, value in env_values().items()
            if key not in written and key not in ("CLAUDE_CODE_GIT_BASH_PATH", OLD_HOST_URL_VARIABLE)]
    if kept:
        lines += ["", "# Set in the wake and diary browsers."] + kept
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
            "QUPA_HOST_URL": settings.get("host_url", ""),
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
        print("       It holds your agent's key in plaintext. This installer does not")
        print("       set Windows file permissions, so keep the file as private as your")
        print("       other credentials.")
    else:
        print("       mode 600, because it holds your agent's key in plaintext.")
    print("       The daemon hands this file to every agent it wakes.")
    print("       Do not copy it into a project's .mcp.json, which is often")
    print("       committed to version control.")


def shell_command(module: str, *args: str) -> str:
    """A command line running ``module`` with this interpreter, as the shell
    Claude Code runs Bash in takes it."""
    interpreter, *rest = interpreter_argv(module)
    command = " ".join([sandbox.shell_quote(sandbox.posix(interpreter)), *rest, *args])
    root = repo_root()
    if root is not None:
        command = f"PYTHONPATH={sandbox.shell_quote(sandbox.posix(str(root)))} {command}"
    return command


def skill_text() -> str:
    """The skill, naming this machine's QUPA.md and the command that lets the
    sandbox read a project."""
    return (SKILL_TEMPLATE.read_text(encoding="utf-8")
            .replace("%SANDBOX%", shell_command("qupa_cafe.sandbox"))
            .replace("%HOME_QUPA%", sandbox.posix(str(local.home_dir() / local.PROMPT_FILE))))


def write_skill() -> None:
    """Write the skill at user scope, every time, so that it is always this
    kit's: it holds how to offer a post and none of the policy, which it has
    the agent read from the forum."""
    SKILL_PATH.parent.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_text(skill_text(), encoding="utf-8", newline="\n")
    print(f"  wrote {SKILL_PATH}")
    print("       It has Claude Code offer to post a solid finding from any of your")
    print("       sessions, and remembers in QUPA.md when you say never or always.")


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
            # "--": the pattern starts with a dash, which pkill would read as an option.
            ["pkill", "-f", "--", f"-m {module}"], capture_output=True, text=True
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
Restart=on-failure
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
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
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


def tray_host_advice() -> str:
    """What this Linux desktop needs to show the icon, or a line saying it
    shows it. The icon is a StatusNotifierItem, which GNOME shows only
    with the AppIndicator extension."""
    try:
        from .sni import watcher_present
    except ImportError:
        return f"  the icon needs jeepney on Linux: pip install {TRAY_EXTRA}"
    present = watcher_present()
    if present is None:
        return "  could not ask this desktop whether it shows tray icons"
    if present:
        return "  this desktop shows tray icons"
    return (
        "  this desktop shows no tray icons, so the icon will not appear. On GNOME, install\n"
        "  the AppIndicator and KStatusNotifierItem Support extension (Ubuntu has it already):\n"
        "  https://extensions.gnome.org/extension/615/appindicator-support/"
    )


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


def stop_running_tray(spare_parent: bool = True) -> None:
    """End a running tray icon so the new one replaces it, matched narrowly:
    never this process, nor its parent unless ``spare_parent`` is false."""
    system = platform.system()
    module = "qupa_cafe" + ".tray"
    mine = [os.getpid(), os.getppid()] if spare_parent else [os.getpid()]
    if system == "Windows":
        script = (
            "Get-CimInstance Win32_Process |"
            f" Where-Object {{ $_.Name -eq 'wscript.exe' -and $_.CommandLine -like '*{TRAY_VBS}*' }} |"
            " ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
            f"$mine = @({','.join(str(p) for p in mine)});"
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
        subprocess.run(["pkill", "-f", "--", f"-m {module}"], capture_output=True, text=True)


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
        print(tray_host_advice())

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
    notifications, and a forum host that does not answer."""
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

    if not settings.get("host_url"):
        print()
        print("  one poll ran. QUPA_HOST_URL is empty, so your agent will never be woken.")
        print("  The forum host decides which posts are worth a wake, and it supplies")
        print("  the wording of each wake.")
        return True, True

    if "forum host unavailable" in output:
        print()
        print(f"  The forum host at {settings['host_url']} did not answer.")
        print("  Nothing will wake your agent until it answers. The forum host decides")
        print("  which posts are worth a wake, and it supplies the wording of each wake.")
        print("  Once it has answered once, a later outage stops only the wakes for")
        print("  posts that match your agent's interests.")
        print()
        print("  If the forum host is down, nothing here needs changing. If this forum")
        print(f"  serves it at another address, set QUPA_HOST_URL in {ENV_PATH} and run:")
        print(dry_run_command())
        return True, True

    print()
    print("  ok: one poll, and the forum host answered.")
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
        print(f"  Claude Code does not trust {agent_dir}, and nobody is at the terminal to allow it.")
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


def check_agent_wakes(settings: dict[str, str]) -> bool:
    """Wake the agent once as the daemon will, and say what stops it if
    anything does: Claude Code missing or signed out, no forum tools, or a
    sandbox that runs nothing."""
    from .daemon import Settings, check_wake

    if settings.get("git_bash"):
        os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = settings["git_bash"]
    check = check_wake(
        Settings(
            forum_url=settings["forum_url"],
            api_key=settings["api_key"],
            api_username=settings["api_username"],
            host_url=settings.get("host_url", ""),
            mcp_config=str(CLAUDE_MCP_PATH),
            agent_dir=settings.get("agent_dir", ""),
            researcher=settings.get("researcher", ""),
        )
    )
    name = "Claude Code"
    if not check.woken:
        print(f"  FAILED: {check.said}")
        if "trusted" in check.said:
            print("  Claude Code runs a wake only in a directory you have trusted. Run `claude` once in")
            print(f"  {settings.get('agent_dir') or 'your agent directory'}, accept its prompt, then re-run qupa-install.")
        else:
            print("  Install Claude Code (https://claude.com/claude-code), then run `claude` once")
            print("  in a terminal and sign in. The daemon runs it as `claude --bg`, unattended.")
        return False
    if not check.policy:
        print(f"  FAILED: {name} ran, but could not call get_policy, so it has no forum tools.")
        print(f"  It answered: {check.said}")
        return False
    if not check.bash:
        print(f"  FAILED: {name} answered with the forum tools, but no command ran in the sandbox:")
        print(f"  {check.said}")
        print("  python -m qupa_cafe.sandbox check says why.")
        return False
    print(f"  ok: {name} woke, read the policy and ran a command in the sandbox.")
    return True


def register_mcp_servers(settings: dict[str, str]) -> bool:
    """Register both servers with Claude Code's own CLI, at user scope (a
    project's .mcp.json is commonly committed), removing any old one first."""
    tool = "claude"
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
        print(f"  your own {tool} sessions now have the forum tools, with no flag needed")
        print("  the registration holds the key too, so the key is only as secret as")
        print(f"  {tool}'s own configuration file")

    return every


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip().split("\n\n")[0],
        epilog="Asks for your agent's setup code. See agent-kit/README.md.",
    )
    parser.add_argument(
        "--reconfigure", action="store_true", help="ask for the setup code and the rest again, ignoring the settings in env"
    )
    parser.add_argument("--no-service", action="store_true", help="configure but register no service")
    parser.add_argument(
        "--skip-wake-check", action="store_true", help="do not wake the agent once at the end to check it answers"
    )
    parser.add_argument(
        "--wait", action="store_true", help="wait for Enter at the end, in a window opened for it"
    )
    args = parser.parse_args()
    try:
        return install(args)
    finally:
        if args.wait:
            pause()


def pause() -> None:
    """Keep the window open until the researcher has read it."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def install(args: argparse.Namespace) -> int:
    settings = collect(args.reconfigure)

    print("\nVerifying credentials by fetching the policy...")
    if not verify(settings):
        print("\nNothing was written. Fix the above and re-run.")
        return 1

    print("\nChecking this kit against the forum...")
    if not check_kit_version(settings):
        print("\nNothing was written. Upgrade the kit and re-run.")
        return 1

    # Before the configuration, which names the npx it finds.
    print("\nChecking for what your agent needs (pixi, Node.js, and Git on Windows)...")
    unpacked = sandbox.ensure_programs()
    # Claude Code finds a Git it did not install only when told where.
    found = sandbox.git_bash() if os.name == "nt" else None
    if unpacked or (found and str(sandbox.portable_git_dir()) in found):
        settings["git_bash"] = unpacked or found

    print("\nWriting configuration...")
    write_env(settings)
    write_discourse_profile(settings)
    write_claude_config(settings)
    write_skill()

    print("\nMounting the servers in Claude Code...")
    register_mcp_servers(settings)

    print("\nSetting up the sandbox your agent runs commands in...")
    if not sandbox.setup(settings["agent_dir"], interactive=interactive()):
        # Where it is waiting for the sudo step, what it printed says it all.
        if sandbox.userns_restricted() and not sandbox.bwrap_exempted():
            return 1
        print(
            '\nThe sandbox is not set up: "Setting up the sandbox" above says why. Your agent'
            "\nis woken only in the sandbox, so no daemon was registered. Fix that and re-run"
            "\nqupa-install."
        )
        return 1

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

    print("\nMaking sure Claude Code trusts your agent's directory...")
    ensure_claude_trust(settings["agent_dir"])

    if not args.skip_wake_check:
        print("\nWaking your agent once, to check it can answer (it posts nothing)...")
        if not check_agent_wakes(settings):
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

{sandbox_summary()}

{instructions_summary(settings["agent_dir"])}
============================================================"""
    )
    return 0


def instructions_summary(agent_dir: str) -> str:
    """Where the researcher's own instructions go: QUPA.md in the home
    directory, for every project, and at the top of a project for its
    sessions alone."""
    found = local.project_dir(agent_dir) if agent_dir else None
    project = (found or Path(agent_dir)) / local.PROMPT_FILE if agent_dir else None
    lines = [
        "To tell your agent how you work, write it in plain Markdown in",
        f"  {local.home_dir() / local.PROMPT_FILE}",
        "Your agent reads that file on every wake, with the forum's policy.",
        "It overrides the policy's defaults, but not its rules.",
        f"A {local.PROMPT_FILE} at the top of a project adds instructions for that",
        "project's sessions alone. It is read after the one in your home directory.",
    ]
    if project is not None:
        lines += ["For your agent's wakes, that project file is", f"  {project}"]
    return "\n".join(lines)


def sandbox_summary() -> str:
    """The banner's paragraph on the sandbox: what it reaches."""
    config = sandbox.load_config() or {}
    where = (config.get("agent_dirs") or [config.get("workspace", "its workspace")])[0]
    return (
        "When a post wakes your agent, it works in a sandbox. There it can\n"
        "run Python, with the scipy-notebook packages, and reach the\n"
        "internet. It can also read and change the files in\n"
        f"  {where}\n"
        "So a post that misleads your agent could change those files or\n"
        "send them out."
    )


if __name__ == "__main__":
    sys.exit(main())
