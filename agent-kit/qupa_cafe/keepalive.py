"""Run a module of the kit, and run it again a minute after it exits for any
reason other than 0 or 2.

On Windows the daemon and the tray icon start at logon as ``pythonw.exe -P -m
qupa_cafe.keepalive <module>``, from the current user's Run key. Exit 0 is a
clean stop, as when the icon is quit, and 2 is a stop that running again would
not mend, such as another daemon holding the lock or configuration only the
installer can fix.

Only the standard library, so that a checkout can run this file as a script:
``keepalive.py --cwd <checkout> <module>`` starts the module from the checkout,
without ``-P``, so ``-m`` finds the package there.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

# Exits that end the loop.
FINAL_EXITS = (0, 2)
RESTART_SECONDS = 60
# CREATE_NO_WINDOW: pythonw.exe has no console, so a console program it starts
# would get a window of its own.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def child_argv(module: str, from_checkout: bool) -> list[str]:
    flags = ["-P"] if not from_checkout and sys.version_info >= (3, 11) else []
    return [sys.executable, *flags, "-m", module]


def keep_alive(module: str, cwd: str | None = None, sleep=time.sleep, run=subprocess.call) -> int:
    """Run ``module`` until it exits with one of ``FINAL_EXITS``; that exit."""
    argv = child_argv(module, cwd is not None)
    while True:
        try:
            status = run(argv, cwd=cwd, creationflags=NO_WINDOW)
        except OSError:
            status = 1
        if status in FINAL_EXITS:
            return status
        sleep(RESTART_SECONDS)


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cwd", help="a checkout to run the module from")
    parser.add_argument("module", choices=["qupa_cafe.daemon", "qupa_cafe.tray"])
    parsed = parser.parse_args(args)
    return keep_alive(parsed.module, parsed.cwd)


if __name__ == "__main__":
    sys.exit(main())
