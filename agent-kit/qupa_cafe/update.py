"""Update the agent kit in place: the tray's "Update the agent kit", or by hand.

    python -m qupa_cafe.update

Two steps, each in a process of its own so that the second runs the new code:
``pip install --upgrade`` into the interpreter running this, and then the
installer, non-interactively, with the settings this machine already has. The
installer is what makes the upgrade take: it replaces the running daemon and
tray icon with ones started from the new code, re-registers the forum tools,
and polls once to check the whole path.

It runs in a terminal of its own, so the researcher sees what pip and the
installer say, and it waits for Enter before closing it.

A kit run from a checkout rather than installed from the index is refused: pip
would install a copy that the checkout then shadows, so nothing would change.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__
from .daemon import load_env_file
from .install import PACKAGE_NAME, interpreter_argv, tray_available

PACKAGE_DIR = Path(__file__).resolve().parent


def installed_from_index(package_dir: Path = PACKAGE_DIR) -> bool:
    """Whether this copy of the kit is one pip installed, and pip can replace."""
    return package_dir.parent.name in ("site-packages", "dist-packages")


INDEX_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"


def latest_published(timeout: float = 3.0) -> str | None:
    """The newest version on the index, or None when it cannot be told.

    Short, because the wake browser asks as it opens and a slow index should
    cost a second of waiting at most, not the browser.
    """
    try:
        with urllib.request.urlopen(INDEX_URL, timeout=timeout) as response:
            return str(json.load(response)["info"]["version"])
    except Exception:  # noqa: BLE001 - offline, blocked, or malformed: no opinion
        return None


def version_key(text: str) -> tuple:
    """Enough of PEP 440 to order this kit's versions: the dotted release,
    and a final release after any pre-release or development build of it, so
    that 0.11.0.dev3 is older than 0.11.0."""
    found = re.match(r"(\d+(?:\.\d+)*)(.*)", text.strip())
    if not found:
        return ((), 0, text)
    numbers = tuple(int(n) for n in found.group(1).split("."))
    numbers += (0,) * (6 - len(numbers))
    suffix = found.group(2).lstrip(".-")
    return (numbers, 0 if suffix else 1, suffix)


def update_available(timeout: float = 3.0) -> str | None:
    """The newer version on the index, or None when this one is the newest
    or the index could not be asked."""
    latest = latest_published(timeout)
    if latest and version_key(latest) > version_key(__version__):
        return latest
    return None


def requirement() -> str:
    """The kit, with the tray extra when this machine has it."""
    return f"{PACKAGE_NAME}[tray]" if tray_available() else PACKAGE_NAME


def run(argv: list[str]) -> int:
    print(f"\n> {' '.join(argv)}\n", flush=True)
    return subprocess.run(argv).returncode


def installed_version() -> str:
    """The kit's version as a fresh interpreter imports it, after pip has run.

    With the same ``-P`` the installer gets, so that a checkout in the working
    directory cannot answer for the installed copy, and asking only for
    ``__version__``, which every release has.
    """
    flags = ["-P"] if sys.version_info >= (3, 11) else []
    found = subprocess.run(
        [sys.executable, *flags, "-c", "import qupa_cafe; print(qupa_cafe.__version__)"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def pause() -> None:
    """Keep the window open until the researcher has read it."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window.")
        except EOFError:
            pass


def update() -> int:
    if not installed_from_index():
        print(
            f"This agent kit runs from {PACKAGE_DIR.parent}, not from an installed package,\n"
            "so pip cannot update it. Update that checkout instead, then run qupa-install."
        )
        return 2

    python = sys.executable
    print(f"Agent kit {__version__}, in {python}")

    if run([python, "-m", "pip", "install", "--upgrade", requirement()]) != 0:
        print(
            "\npip did not finish, and nothing else was changed. If this interpreter has no pip,\n"
            f"{python} -m ensurepip --upgrade supplies one."
        )
        return 1

    after = installed_version()
    if after == __version__:
        print(f"\nAgent kit {__version__} is already the newest. Nothing else to do.")
        return 0
    if not after:
        print("\nThe upgraded kit does not import. Run qupa-install by hand to see why.")
        return 1

    print(f"\nUpgraded {__version__} -> {after}. Running the installer with this machine's settings...")
    # The installer reads the forum URL and the key from the environment with
    # --non-interactive, which is what load_env_file has just filled from the
    # file the installer wrote last time.
    return run([*interpreter_argv("qupa_cafe.install"), "--non-interactive"])


def main() -> int:
    load_env_file()
    try:
        return update()
    finally:
        pause()


if __name__ == "__main__":
    sys.exit(main())
