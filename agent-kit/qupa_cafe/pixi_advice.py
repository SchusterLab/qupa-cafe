"""The pixi command for a pip or conda command, for an agent in the sandbox.

The sandbox's Python is a pixi workspace, so a package installed with pip or
conda would bypass its manifest and lockfile and vanish when the environment
is next rebuilt. ``qupa_cafe.sandbox`` refuses such a command before it runs
and returns ``advise``'s text to the agent as the reason. The same file is
copied into the workspace beside shims named ``pip``, ``conda`` and the rest,
which run it for a command that reaches one of them some other way, such as
from a script::

    python pixi_advice.py pip install numpy

Standard library only, and no imports from the package, since the copy runs
under the sandbox's Python with nothing else beside it.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

NL = chr(10)

# How a pip command can begin: pip, pip3, pip3.13, python -m pip, uv pip.
PIP = re.compile(r"pip(\d+(\.\d+)?)?(\.exe)?$", re.IGNORECASE)
PYTHON = re.compile(r"(python(\d+(\.\d+)?)?|py)(\.exe)?$", re.IGNORECASE)
CONDA = re.compile(r"(conda|mamba|micromamba)(\.exe|\.bat)?$", re.IGNORECASE)

# Words that may precede the program without changing which one it is.
PREFIXES = {"sudo", "command", "exec", "nohup", "time", "env"}

# pip options that take a value, so that the value is not read as a package.
PIP_VALUED = {
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
    "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root",
    "--platform", "--python-version", "--implementation", "--abi", "--src",
    "--upgrade-strategy", "--progress-bar", "--log", "--cache-dir", "--trusted-host",
    "--python", "--report", "--config-settings", "-C", "--global-option", "--no-binary",
    "--only-binary", "--exists-action", "--proxy", "--retries", "--timeout", "--cert",
    "--client-cert", "--keyring-provider",
}

CONDA_VALUED = {
    "-c", "--channel", "-n", "--name", "-p", "--prefix", "-f", "--file", "--override-channels-file",
    "--repodata-fn", "--solver", "--platform", "--subdir",
}

# Channels a conda command may name that the workspace already uses.
OWN_CHANNELS = {"conda-forge", "defaults", "anaconda", "main"}


def _segments(command: str) -> list[list[str]]:
    """The simple commands in ``command``, each as words. A naive split on
    the shell's separators, which is enough to find a pip or conda command."""
    out = []
    for part in re.split(r"&&|\|\||[;|&\n]", command):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in PREFIXES):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _program(words: list[str]) -> tuple[str, list[str]] | None:
    """``("pip", rest)`` or ``("conda", rest)`` for a pip or conda command."""
    name = os.path.basename(words[0])
    if PIP.match(name):
        return "pip", words[1:]
    if name == "uv" and len(words) > 1 and words[1] == "pip":
        return "pip", words[2:]
    if PYTHON.match(name):
        for i in range(1, len(words) - 1):
            if words[i] == "-m" and words[i + 1] == "pip":
                return "pip", words[i + 2 :]
            if words[i] == "-mpip":
                return "pip", words[i + 1 :]
        return None
    if CONDA.match(name):
        return "conda", words[1:]
    return None


def _split(rest: list[str], valued: set[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Positional words, and the values of the valued options by option."""
    positional: list[str] = []
    options: dict[str, list[str]] = {}
    i = 0
    while i < len(rest):
        word = rest[i]
        if word.startswith("-"):
            name, eq, value = word.partition("=")
            if name in valued:
                if not eq and i + 1 < len(rest):
                    value = rest[i + 1]
                    i += 1
                options.setdefault(name, []).append(value)
        else:
            positional.append(word)
        i += 1
    return positional, options


def _quoted(specs: list[str]) -> str:
    return " ".join(shlex.quote(spec) for spec in specs)


def _conda_forge_can_take(spec: str) -> bool:
    """Whether a pip requirement reads as a conda-forge spec too: a name and a
    version constraint, with no extras, URL or path."""
    return re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*([<>=!~]=?[^\s\[\]@;]*)?$", spec) is not None


def _pip(rest: list[str], workspace: str) -> str | None:
    positional, options = _split(rest, PIP_VALUED)
    if not positional:
        return None
    sub, specs = positional[0], positional[1:]
    lines = [f"pip is not used here. Python packages come from the pixi workspace at {workspace}, "
             "which every command here already runs in."]

    if sub == "install":
        editable = options.get("-e", []) + options.get("--editable", [])
        files = options.get("-r", []) + options.get("--requirement", [])
        if not (specs or editable or files):
            return None
        lines.append("Instead of that pip install, run:")
        if specs and all(_conda_forge_can_take(spec) for spec in specs):
            lines.append(f"  pixi add {_quoted(specs)}")
            lines.append("which takes them from conda-forge, where the rest of the environment comes from, or, "
                         "for a package conda-forge does not have:")
        if specs:
            lines.append(f"  pixi add --pypi {_quoted(specs)}")
        for path in editable:
            lines.append(f"  pixi add --pypi --editable '<name> @ file://{os.path.abspath(path)}'")
        for path in files:
            lines.append(f"  pixi add --pypi $(grep -v -e '^#' -e '^-' {shlex.quote(path)})")
        if options.get("-i") or options.get("--index-url") or options.get("--extra-index-url"):
            lines.append("For another package index, set index-url or extra-index-urls under "
                         "[pypi-options] in pixi.toml first.")
        lines.append("What pixi adds stays installed for later wakes.")
        return NL.join(lines)

    if sub == "uninstall":
        if not specs:
            return None
        lines.append("Instead of that pip uninstall, run:")
        lines.append(f"  pixi remove {_quoted(specs)}")
        lines.append(f"or, for a package added with --pypi, pixi remove --pypi {_quoted(specs)}")
        return NL.join(lines)

    return None


def _conda(rest: list[str], workspace: str) -> str | None:
    positional, options = _split(rest, CONDA_VALUED)
    if not positional:
        return None
    if positional[0] == "env" and len(positional) > 1:
        sub, specs = "env " + positional[1], positional[2:]
    else:
        sub, specs = positional[0], positional[1:]
    lines = [f"conda is not used here. Packages come from the pixi workspace at {workspace}, "
             "which every command here already runs in, so there is no environment to create "
             "or activate."]

    channels = [c for c in options.get("-c", []) + options.get("--channel", []) if c not in OWN_CHANNELS]

    if sub == "install" and (specs or options.get("-f") or options.get("--file")):
        lines.append(f"Instead of that conda {sub}, run:")
        for channel in channels:
            lines.append(f"  pixi workspace channel add {shlex.quote(channel)}")
        if specs:
            lines.append(f"  pixi add {_quoted(specs)}")
        for path in options.get("-f", []) + options.get("--file", []):
            lines.append(f"  pixi add $(grep -v '^#' {shlex.quote(path)})")
        lines.append("What pixi adds stays installed for later wakes.")
        return NL.join(lines)

    if sub in ("remove", "uninstall") and specs:
        lines.append(f"Instead of that conda {sub}, run:")
        lines.append(f"  pixi remove {_quoted(specs)}")
        return NL.join(lines)

    if sub in ("update", "upgrade"):
        lines.append(f"Instead of that conda {sub}, run:")
        lines.append(f"  pixi update {_quoted(specs)}".rstrip())
        return NL.join(lines)

    if sub in ("list",):
        lines.append("To see what is installed, run:")
        lines.append("  pixi list")
        return NL.join(lines)

    if sub in ("create", "activate", "env create", "env update"):
        lines.append("Add what you need to this one with pixi add <package>, or pixi add --pypi <package> "
                     "for a package only PyPI has.")
        return NL.join(lines)

    return None


def advise(command: str, workspace: str = "the sandbox's workspace") -> str | None:
    """What to run instead of the pip or conda installs in ``command``, or None
    when it has none."""
    advice = []
    for words in _segments(command):
        found = _program(words)
        if found is None:
            continue
        kind, rest = found
        text = _pip(rest, workspace) if kind == "pip" else _conda(rest, workspace)
        if text and text not in advice:
            advice.append(text)
    return (NL * 2).join(advice) or None


def main(argv: list[str]) -> int:
    """A shim's entry: ``argv`` is the program it stands for, then its arguments.
    Anything pip does that installs nothing is passed on to the real pip."""
    if not argv:
        print("usage: pixi_advice.py <pip|conda|...> [arguments]", file=sys.stderr)
        return 2
    workspace = os.environ.get("PIXI_PROJECT_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = advise(" ".join(shlex.quote(word) for word in argv), workspace)
    if text:
        print(text, file=sys.stderr)
        return 1
    if PIP.match(os.path.basename(argv[0])):
        return subprocess.call([sys.executable, "-m", "pip", *argv[1:]])
    print(
        f"{argv[0]} is not used here. Packages come from the pixi workspace at {workspace}: "
        "pixi add <package> adds one, pixi list shows them.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
