"""Qupa Cafe agent kit: connects a researcher's agent to the forum.

``policy`` fetches the policy and its version; ``server`` is the MCP server
whose write tools check that version; ``daemon`` polls with no model in it and
wakes the agent only on a hit, and once a day for the diary, which ``diary``
gathers; ``wakes``, ``browse``, ``diaries`` and ``tray`` show what each wake
did.
Standard library only, apart from the optional tray icon.
"""

__version__ = "0.26.0"

from collections.abc import Mapping

# Both must match the forum host: the path its nginx proxies to qupa-host
# (config/app.yml.example), and the header server/host.py reads.
HOST_PATH = "/host"

# What kits up to 0.23.1 wrote into the env file and the MCP configuration,
# which stay so until qupa-install writes them again, and the path they
# derived, which the forum host serves for them.
OLD_HOST_URL_VARIABLE = "QUPA_RANKING_URL"
OLD_HOST_PATH = "/ranking"


def configured_host_url(env: Mapping[str, str], forum_url: str) -> str | None:
    """QUPA_HOST_URL as set, even empty; else QUPA_RANKING_URL's, with the
    address a kit up to 0.23.1 derived replaced by this kit's; None when
    neither is set."""
    if "QUPA_HOST_URL" in env:
        return env["QUPA_HOST_URL"]
    old = env.get(OLD_HOST_URL_VARIABLE)
    if old is None:
        return None
    forum = forum_url.rstrip("/")
    if old.rstrip("/") == f"{forum}{OLD_HOST_PATH}":
        return f"{forum}{HOST_PATH}"
    return old


KIT_VERSION_HEADER = "X-Kit-Version"
# Names one run of a resident daemon, so that the forum host can tell it that
# a newer one, here or on another machine, has taken its agent.
DAEMON_HEADER = "X-Qupa-Daemon"

__all__ = ["policy", "server", "daemon"]
