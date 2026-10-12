"""Remove the agent kit from this machine: ``qupa-uninstall``, or the tray icon's Uninstall the agent kit.

Stops and unregisters the daemon and the tray icon, removes the forum's
servers from Claude Code, and deletes the kit's settings and data, the
sandbox among them. What needs sudo, the package itself, and the
researcher's own files are named and left alone.

    qupa-uninstall                   # or python3 -m qupa_cafe.uninstall
    --yes                            # do not ask first
    --wait                           # wait for Enter at the end
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import install, sandbox
from .install import CLAUDE_MCP_PATH, CONFIG_DIR, DAEMON_PLIST, DAEMON_UNIT, DAEMON_VBS, TRAY_DESKTOP, TRAY_PLIST, TRAY_VBS

DATA_DIR = Path.home() / ".local" / "share" / "qupa-cafe"
SERVERS = ("discourse", "qupa-cafe")


def quietly(function, *args) -> None:
    """Run one of the installer's helpers without its own messages, which
    are worded for replacing what it stops."""
    with contextlib.redirect_stdout(io.StringIO()):
        function(*args)


def remove(path: Path) -> bool:
    """Delete a file or a directory tree, read-only files included. False,
    having said so, when something could not be deleted."""
    if not path.exists() and not path.is_symlink():
        return True

    def writable_again(function, name, _):
        os.chmod(name, stat.S_IWRITE)
        function(name)

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onerror=writable_again)
        else:
            path.unlink()
    except OSError as exc:
        print(f"  could not delete {path}: {exc}")
        return False
    print(f"  deleted {path}")
    return True


def unregister_daemon() -> None:
    system = platform.system()
    if system == "Linux" and shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", DAEMON_UNIT], capture_output=True, text=True)
        remove(Path.home() / ".config" / "systemd" / "user" / f"{DAEMON_UNIT}.service")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
    elif system == "Darwin" and shutil.which("launchctl"):
        subprocess.run(["launchctl", "unload", str(DAEMON_PLIST)], capture_output=True, text=True)
        remove(DAEMON_PLIST)
    elif system == "Windows":
        remove(install.startup_dir() / DAEMON_VBS)
    # Whatever is left running, however it was started.
    quietly(install.stop_running_daemon)
    print("  stopped the daemon")


def unregister_tray() -> None:
    system = platform.system()
    # The icon may be what opened this window, so it is stopped too.
    quietly(install.stop_running_tray, False)
    if system == "Darwin":
        remove(TRAY_PLIST)
    elif system == "Windows":
        remove(install.startup_dir() / TRAY_VBS)
    else:
        remove(TRAY_DESKTOP)
    print("  stopped the tray icon")


def unregister_servers() -> None:
    claude = shutil.which("claude")
    if claude is None:
        print(f"  claude is not on PATH, so remove the servers {', '.join(SERVERS)} from it yourself")
        return
    for name in SERVERS:
        result = subprocess.run([claude, "mcp", "remove", "--scope", "user", name], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  removed {name} from claude")


def uninstall_windows_sandbox() -> bool:
    """Remove the Windows sandbox's account and firewall rules, which its UAC
    prompt consents to. False when they are still there."""
    if platform.system() != "Windows" or not sandbox.windows_marker().is_file():
        return True
    node = sandbox.find("node")
    cli = sandbox.runtime_dir() / "node_modules" / "@anthropic-ai" / "sandbox-runtime" / "dist" / "cli.js"
    print("  Removing the sandbox's Windows account, srt-sandbox, and its firewall rules needs one UAC prompt.")
    if node and cli.is_file():
        if subprocess.run([node, str(cli), "windows-uninstall"], cwd=str(cli.parent)).returncode == 0:
            print("  removed them")
            return True
    print("  Not removed. To remove them later: npx @anthropic-ai/sandbox-runtime windows-uninstall")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="do not ask first")
    parser.add_argument("--wait", action="store_true", help="wait for Enter at the end, in a window opened for it")
    args = parser.parse_args()
    try:
        return uninstall(args.yes)
    finally:
        if args.wait:
            install.pause()


def uninstall(yes: bool) -> int:
    agent_dir = install.env_values().get("QUPA_AGENT_DIR", "")
    print(
        "This removes the Qupa Cafe agent kit from this machine. It stops your agent's\n"
        "daemon and the tray icon, removes the forum's tools from Claude Code, and deletes\n"
        "the kit's settings, its record of wakes and the sandbox. Your agent's working\n"
        "directory, your QUPA.md files and everything on the forum stay."
    )
    if not yes:
        if not install.interactive():
            print("Nothing was removed: run it in a terminal, or with --yes.", file=sys.stderr)
            return 2
        if input("Remove it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was removed.")
            return 1

    print("\nStopping and unregistering...")
    unregister_tray()
    unregister_daemon()
    unregister_servers()

    print("\nDeleting the kit's files...")
    whole = uninstall_windows_sandbox()
    for path in (CLAUDE_MCP_PATH, CONFIG_DIR, DATA_DIR):
        whole = remove(path) and whole

    print("\nLeft for you:")
    if sandbox.bwrap_exempted():
        print("  The sandbox's copy of bubblewrap and its AppArmor profile, which need sudo:")
        print(f"    sudo apparmor_parser -R {sandbox.LINUX_BWRAP_PROFILE}")
        print(f"    sudo rm -r {sandbox.LINUX_BWRAP.parent} {sandbox.LINUX_BWRAP_PROFILE}")
    if agent_dir:
        print(f"  Your agent's working directory, {agent_dir}")
    print(f"  pixi and what it installed, in {Path.home() / '.pixi'}, if qupa-install put it there")
    print(f'  The kit itself: "{sys.executable}" -m pip uninstall {install.PACKAGE_NAME}')
    print("  Your agent's account and posts on the forum")
    return 0 if whole else 1


if __name__ == "__main__":
    sys.exit(main())
