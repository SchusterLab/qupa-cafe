"""Update the kit in place: ``python -m qupa_cafe.update``, or the tray icon's Update the agent kit.

``pip install --upgrade`` into this interpreter, then the installer,
which reuses this machine's settings, in a process of its own so it
runs the new code and restarts the daemon and icon. A checkout is refused,
since it would shadow what pip installs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__
from .daemon import load_env_file
from .install import PACKAGE_NAME, interpreter_argv, pause, tray_available

PACKAGE_DIR = Path(__file__).resolve().parent


def installed_from_index(package_dir: Path = PACKAGE_DIR) -> bool:
    """Whether this copy of the kit is one pip installed, and pip can replace."""
    return package_dir.parent.name in ("site-packages", "dist-packages")


# The index page pip reads, in its JSON form (PEP 691).
INDEX_URL = f"https://pypi.org/simple/{PACKAGE_NAME}/"
INDEX_ACCEPT = "application/vnd.pypi.simple.v1+json"

# The index serves that page with max-age=600, and pip keeps its copy as long:
# a release younger than this may be missing from the copy pip reads, and an
# update offered then would find nothing newer.
SETTLE_SECONDS = 600

FILENAME = re.compile(r"^[A-Za-z0-9_.]+?-(\d[^-]*?)(?:-[^/]*\.whl|\.tar\.gz|\.zip)$")


def installable(index: dict, now: float | None = None) -> str | None:
    """The newest final release in a PEP 691 page that pip can be relied on to
    find: not yanked, and first uploaded at least SETTLE_SECONDS ago."""
    now = time.time() if now is None else now
    uploaded: dict[str, float] = {}
    for item in index.get("files") or []:
        found = FILENAME.match(str(item.get("filename", "")))
        if not found or item.get("yanked"):
            continue
        try:
            at = datetime.fromisoformat(str(item["upload-time"]).replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        version = found.group(1)
        uploaded[version] = min(at, uploaded.get(version, at))
    settled = [
        version
        for version, at in uploaded.items()
        if now - at >= SETTLE_SECONDS and version_key(version)[1] == 1
    ]
    return max(settled, key=version_key, default=None)


def latest_published(timeout: float = 3.0) -> str | None:
    """The newest version an update can install, or None when the index
    cannot be asked."""
    try:
        request = urllib.request.Request(INDEX_URL, headers={"Accept": INDEX_ACCEPT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return installable(json.load(response))
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


def requirement(version: str | None = None) -> str:
    """The kit, with the tray extra when this machine has it, at ``version``
    when the index named one."""
    name = f"{PACKAGE_NAME}[tray]" if tray_available() else PACKAGE_NAME
    return f"{name}=={version}" if version else name


def run(argv: list[str]) -> int:
    print(f"\n> {' '.join(argv)}\n", flush=True)
    return subprocess.run(argv).returncode


def installed_version() -> str:
    """The version a fresh interpreter imports; ``-P`` so a checkout in the
    working directory cannot answer for it."""
    flags = ["-P"] if sys.version_info >= (3, 11) else []
    found = subprocess.run(
        [sys.executable, *flags, "-c", "import qupa_cafe; print(qupa_cafe.__version__)"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def update() -> int:
    if not installed_from_index():
        print(
            f"This agent kit runs from {PACKAGE_DIR.parent}, not from an installed package,\n"
            "so pip cannot update it. Update that checkout instead, then run qupa-install."
        )
        return 2

    python = sys.executable
    print(f"Agent kit {__version__}, in {python}")

    # The version the index's JSON names, asked for by number and past pip's
    # cache: pip reads another page of the index, which it keeps for minutes
    # and the index's CDN can serve stale for a while after a release, so an
    # unpinned upgrade can find nothing newer than what is installed.
    latest = update_available(timeout=10)
    argv = [python, "-m", "pip", "install", "--upgrade", "--no-cache-dir", requirement(latest)]
    if run(argv) != 0:
        if latest:
            print(
                f"\npip could not find {latest} yet, and nothing else was changed. A release takes a few\n"
                "minutes to reach every copy of the index: try again shortly."
            )
            return 1
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
    # Which it reads from env, asking for them only where they no longer work.
    return run(interpreter_argv("qupa_cafe.install"))


def main() -> int:
    load_env_file()
    try:
        return update()
    finally:
        pause()


if __name__ == "__main__":
    sys.exit(main())
