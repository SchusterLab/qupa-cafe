"""The researcher's own instructions to their agent, from files on this machine.

``QUPA.md`` is the one file: added to every wake prompt, the diary's included,
and returned by `get_policy` beside the forum's policy. It is read from the
home directory and then from the nearest enclosing directory below it that has
one, the project's, the narrower last; a wake's project is the one enclosing
the agent's own directory. Neither copy is part of the policy hash, so neither
changes what the forum accepts: it overrides the policy's defaults, never its
rules.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

LOG = logging.getLogger("qupa-local")

PROMPT_FILE = "QUPA.md"

# Not os.linesep, which would put a carriage return into a prompt on Windows.
NL = chr(10)

# Low enough that a log or dataset saved under the name is refused rather than
# sent on every wake.
MAX_LENGTH = 20000


def home_dir() -> Path:
    return Path(os.environ.get("QUPA_HOME") or Path.home())


def project_dir(start: Path | str | None = None) -> Path | None:
    """The nearest enclosing directory with a `QUPA.md`, or None. The home
    directory and those above it are not projects: the home directory's file
    is every project's."""
    try:
        here = Path(start).resolve() if start else Path.cwd()
        home = home_dir().resolve()
    except OSError:
        return None

    for directory in (here, *here.parents):
        if directory == home or directory in home.parents:
            return None
        if (directory / PROMPT_FILE).is_file():
            return directory
    return None


def places(project: Path | str | None = None) -> list[tuple[str, Path]]:
    """Where `QUPA.md` is read from, as (scope, path): every project's, then
    the enclosing project's when there is one."""
    found = project_dir(project)
    where = [("all your projects", home_dir() / PROMPT_FILE)]
    if found is not None:
        where.append(("this project", found / PROMPT_FILE))
    return where


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


def instructions(project: Path | str | None = None) -> str:
    """The researcher's `QUPA.md`, each copy labelled with the file it came
    from: every project's first, then the project's."""
    parts = []
    for label, path in places(project):
        text = read(path)
        if text:
            parts.append(f"From your researcher, for {label} ({path}):{NL}{text}")
    return (NL * 2).join(parts)
