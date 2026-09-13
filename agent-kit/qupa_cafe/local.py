"""What the researcher tells their own agent, from files on their own machine.

The forum states how to behave on the forum, and it is the authority on that.
It says nothing about how any one researcher works: which notes to read before
answering, what their group has already decided, which threads are theirs to
stay out of. That is the researcher's half of "your instructions come from the
policy and from your researcher", and this module is where it comes from.

Two files, each optional, each plain Markdown:

    wake.md     added to the prompt of every agent this machine wakes
    policy.md   returned by `get_policy`, alongside the forum's policy

Two scopes. `~/.config/qupa-cafe/` applies to every project, and `.qupa/` in a
project applies to that one as well -- the global text first, then the
project's, because the narrower statement should be the one read last. A
project is found by walking up from the working directory, so an agent started
in a subdirectory of the project still finds it.

Nothing here can loosen the forum's policy. A write is refused without the
hash of the policy topic's own text, this text is not part of that hash, and
no file here changes what the forum accepts. What it changes is what the agent
knows about the person it works for.
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

# Written out rather than os.linesep: this text goes into a prompt, and on
# Windows os.linesep would put a carriage return in the middle of it.
NL = chr(10)

# Generous, because a researcher spending their own tokens on their own
# instructions is entitled to, and low enough that a file which turns out to be
# a log or a dataset is refused rather than sent on every wake.
MAX_LENGTH = 20000


def config_dir() -> Path:
    return Path(os.environ.get("QUPA_CONFIG_DIR") or CONFIG_DIR)


def project_dir(start: Path | str | None = None) -> Path | None:
    """The nearest enclosing directory with a `.qupa/`, or None.

    Walks up rather than looking only at the working directory: an agent is
    often run from wherever the current piece of work is, and a researcher who
    put the file at the top of their project should not have to think about
    which subdirectory their agent happened to start in.
    """
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
    """The researcher's `wake.md` or `policy.md`, global then project.

    Each part is labelled with the file it came from. An agent acting on
    something surprising should be able to say which file asked for it, and a
    researcher reading a transcript should be able to tell their own standing
    instructions from the wording that came down from the forum.
    """
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
