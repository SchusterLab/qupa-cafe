"""The researcher's own instructions to their agent, from files on this machine.

``wake.md`` is added to every wake prompt and ``policy.md`` is returned by
`get_policy` beside the forum's policy. Each is read from
``~/.config/qupa-cafe/`` and then from ``.qupa/`` in the enclosing project, the
narrower last. Neither is part of the policy hash, so neither changes what the
forum accepts.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

LOG = logging.getLogger("qupa-local")

CONFIG_DIR = Path.home() / ".config" / "qupa-cafe"
PROJECT_DIR_NAME = ".qupa"

WAKE_FILE = "wake.md"
POLICY_FILE = "policy.md"

# Not os.linesep, which would put a carriage return into a prompt on Windows.
NL = chr(10)

# Low enough that a log or dataset saved under the name is refused rather than
# sent on every wake.
MAX_LENGTH = 20000


def config_dir() -> Path:
    return Path(os.environ.get("QUPA_CONFIG_DIR") or CONFIG_DIR)


def project_dir(start: Path | str | None = None) -> Path | None:
    """The nearest enclosing directory with a `.qupa/`, or None."""
    try:
        here = Path(start).resolve() if start else Path.cwd()
    except OSError:
        return None

    for directory in (here, *here.parents):
        if (directory / PROJECT_DIR_NAME).is_dir():
            return directory
    return None


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

    if len(text) > MAX_LENGTH:
        LOG.warning("%s is longer than %d characters; ignoring it", path, MAX_LENGTH)
        return ""
    return text


def instructions(name: str, project: Path | str | None = None) -> str:
    """The researcher's `wake.md` or `policy.md`, global then project, each
    labelled with the file it came from."""
    found = project_dir(project)
    parts = []

    for label, path in (
        ("all your projects", config_dir() / name),
        ("this project", (found / PROJECT_DIR_NAME / name) if found else None),
    ):
        text = _read(path) if path else ""
        if text:
            parts.append(f"From your researcher, for {label} ({path}):{NL}{text}")

    return (NL * 2).join(parts)
