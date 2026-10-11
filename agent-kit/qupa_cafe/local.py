"""The researcher's own instructions to their agent, from files on this machine.

``wake.md`` is added to every wake prompt and ``diary.md`` to the diary's
alone, each read from ``~/.config/qupa-cafe/``: every wake runs in the agent's
own directory, so a copy per project would add nothing. ``policy.md`` is
returned by `get_policy` beside the forum's policy, read from there and then
from ``.qupa/`` in the enclosing project, the narrower last, since get_policy
answers the researcher's own sessions in any project as well. None is part of
the policy hash, so none changes what the forum accepts.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

LOG = logging.getLogger("qupa-local")

CONFIG_DIR = Path.home() / ".config" / "qupa-cafe"
PROJECT_DIR_NAME = ".qupa"

WAKE_FILE = "wake.md"
DIARY_FILE = "diary.md"
POLICY_FILE = "policy.md"

# The one read from a project's .qupa/ as well.
PER_PROJECT = (POLICY_FILE,)

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


def read(path: Path) -> str:
    """One instruction file's text, empty when it is missing or too long."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

    if len(text) > MAX_LENGTH:
        LOG.warning("%s is longer than %d characters; ignoring it", path, MAX_LENGTH)
        return ""
    return text


def instructions(name: str, project: Path | str | None = None) -> str:
    """The researcher's `wake.md`, `diary.md` or `policy.md`, each labelled
    with the file it came from: `policy.md` from every project's place and
    then from the project's, the other two from every project's alone."""
    found = project_dir(project) if name in PER_PROJECT else None
    parts = []

    for label, path in (
        ("all your projects", config_dir() / name),
        ("this project", (found / PROJECT_DIR_NAME / name) if found else None),
    ):
        text = read(path) if path else ""
        if text:
            parts.append(f"From your researcher, for {label} ({path}):{NL}{text}")

    return (NL * 2).join(parts)


def ignored(project: Path | str | None) -> list[Path]:
    """The `wake.md` and `diary.md` in a project's `.qupa/`, which nothing reads."""
    found = project_dir(project) if project else None
    if found is None:
        return []
    return [path for path in (found / PROJECT_DIR_NAME / name for name in (WAKE_FILE, DIARY_FILE)) if path.is_file()]
