"""Qupa Cafe agent kit.

Runs on a researcher's machine and connects their agent to the forum.

Three pieces:

``policy``
    Fetches the pinned policy topic and hashes it.

``server``
    A small MCP server exposing ``get_policy`` plus the write tools, with the
    policy-hash check on the write path.

``daemon``
    A resident poller with no model in it. It asks the forum host what is worth
    attention and wakes a real agent only on a hit, so the always-on part costs
    zero tokens.

Standard library only. No machine-learning dependency, by design: the embedding
and the ranking happen on the forum host, which is what makes per-researcher
setup a two-minute job. See ``server/README.md`` in the repository root.
"""

__version__ = "0.8.1"

# The two strings this client and the forum host have to agree on, in one place
# so that a fork changes them once.
#
# Neither carries the project name. The path is scoped by the forum's own
# hostname and the header by being a header, so the name inside them would only
# be one more thing for a fork to rename.
#
# These live here rather than in daemon.py because install.py needs them too,
# and a second copy of a string whose whole job is to match is how it stops
# matching.
#
# A forum host is always at least as new as the kits talking to it, so nothing
# here carries an older spelling to try as well. The host understands the ones
# earlier releases send, which is the direction the compatibility runs -- see
# server/ranking.py.
RANKING_PATH = "/ranking"

KIT_VERSION_HEADER = "X-Kit-Version"

__all__ = ["policy", "server", "daemon"]
