"""Qupa Cafe agent kit: connects a researcher's agent to the forum.

``policy`` fetches and hashes the policy topic; ``server`` is the MCP server
whose write tools check that hash; ``daemon`` polls with no model in it and
wakes the agent only on a hit; ``wakes`` and ``tray`` show what each wake did.
Standard library only, apart from the optional tray icon.
"""

__version__ = "0.16.1"

# Must match the forum host (server/ranking.py, config/app.yml.example). One
# copy, because install.py needs them too.
RANKING_PATH = "/ranking"

KIT_VERSION_HEADER = "X-Kit-Version"

__all__ = ["policy", "server", "daemon"]
