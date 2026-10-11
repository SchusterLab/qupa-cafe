"""Which Claude Code model a wake runs on, and at what effort.

Two settings each for forum wakes and for diary wakes, kept in env, where
the wake and diary browsers change them and the daemon reads them again at
every wake, so a change needs no restart:

    QUPA_WAKE_MODEL, QUPA_WAKE_EFFORT      forum wakes
    QUPA_DIARY_MODEL, QUPA_DIARY_EFFORT    diary wakes

A model is anything ``claude --model`` takes, an alias such as ``sonnet`` or
a full name; empty is the researcher's own Claude Code default. An effort is
one of ``claude --effort``'s levels; empty is the model's default. A diary
wake runs on Sonnet unless the researcher says otherwise: an entry is a
summary of the day, which needs less than a reply on the forum does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import diary

WAKE, DIARY = "wake", "diary"

KEYS = {
    WAKE: ("QUPA_WAKE_MODEL", "QUPA_WAKE_EFFORT"),
    DIARY: ("QUPA_DIARY_MODEL", "QUPA_DIARY_EFFORT"),
}
DEFAULT_MODEL = {WAKE: "", DIARY: "sonnet"}

# What the browsers' keys step through, in order; empty is the default.
MODELS = ("", "fable", "opus", "sonnet", "haiku")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")


@dataclass
class Choice:
    model: str = ""
    effort: str = ""

    def flags(self) -> list[str]:
        """What ``claude`` is given for it."""
        return (["--model", self.model] if self.model else []) + (["--effort", self.effort] if self.effort else [])

    def describe(self) -> str:
        model = self.model or "your Claude Code default model"
        effort = f"effort {self.effort}" if self.effort else "its default effort"
        return f"{model}, {effort}"


def effort_of(value: str) -> str:
    value = value.strip().lower()
    return value if value in EFFORTS else ""


def read(kind: str, path: Path | None = None) -> Choice:
    """The setting for ``kind`` as env says now, else as this process's
    environment does, else the default."""
    model_key, effort_key = KEYS[kind]
    values = {model_key: os.environ.get(model_key), effort_key: os.environ.get(effort_key)}
    try:
        lines = (path or diary.env_file()).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() in values:
            values[key.strip()] = value
    model = values[model_key]
    return Choice(
        DEFAULT_MODEL[kind] if model is None else model.strip(),
        effort_of(values[effort_key] or ""),
    )


def write(kind: str, chosen: Choice, path: Path | None = None) -> None:
    """Write the setting for ``kind`` into env, in place, leaving every other
    line be."""
    model_key, effort_key = KEYS[kind]
    path = path or diary.env_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    wanted = {model_key: chosen.model, effort_key: chosen.effort}
    kept = []
    for line in lines:
        key = line.partition("=")[0].strip()
        kept.append(f"{key}={wanted.pop(key)}" if key in wanted else line)
    kept += [f"{key}={value}" for key, value in wanted.items()]
    # newline="" so Windows does not put carriage returns into the file.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(kept) + "\n")


def step(values: tuple[str, ...], current: str) -> str:
    """The value after ``current``, round to the first; one not in the list,
    such as a full model name, steps to the first."""
    return values[(values.index(current) + 1) % len(values)] if current in values else values[0]


def next_model(kind: str) -> Choice:
    chosen = read(kind)
    chosen.model = step(MODELS, chosen.model)
    write(kind, chosen)
    return chosen


def next_effort(kind: str) -> Choice:
    chosen = read(kind)
    chosen.effort = step(EFFORTS, chosen.effort)
    write(kind, chosen)
    return chosen
