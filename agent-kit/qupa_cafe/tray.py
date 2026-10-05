"""The daemon, a click away: an icon in the task tray or the menu bar.

The icon says whether the daemon is polling, waking the agent, or has stopped,
and its tooltip says when the agent was last woken and for what. Clicking it
opens a terminal with the wake browser (``qupa_cafe.browse``): the recent wakes
to choose from with the arrow keys, each to read, and each to reopen in Claude
Code to ask the agent about; it also opens the daemon's log and updates the
kit. The icon's own menu has Show wakes, since on macOS a click opens the menu
rather than doing anything itself, and Quit the icon.

It is a viewer and nothing else. The daemon runs without it, under whatever
restarts it, and quitting this or a desktop with no tray to show it in costs no
wake. It reads the daemon's state file, read-only, and holds a lock of its own
so that a second copy exits rather than adding a second icon.

Needs the ``tray`` extra, which the rest of the kit does not::

    pip install "qupa-cafe[tray]"
    python -m qupa_cafe.tray

Exits 0 when quit or when another copy is running, 2 when the extra is not
installed, and anything else is an error worth starting again for -- the same
meaning the Windows Startup entry gives the daemon's exits.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import wakes
from .daemon import Settings, claim_lock, load_env_file, outside_session_env

REFRESH_SECONDS = 20

COLOURS = {
    "polling": (46, 160, 67),
    "waking": (219, 154, 4),
    "stopped": (150, 150, 150),
}

# Windows truncates a tray tooltip at 127 characters.
TOOLTIP_CHARS = 120

# Tried in order on Linux, where there is no one way to open a terminal.
# xdg-terminal-exec is the freedesktop proposal for exactly this, and
# x-terminal-emulator is Debian's alternative for the same.
TERMINALS = (
    ("xdg-terminal-exec", []),
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("xterm", ["-e"]),
)


def describe(settings: Settings) -> tuple[str, str]:
    """Which icon to show, and the tooltip to go with it."""
    state = wakes.status(settings.state_path)
    rows = wakes.recent(settings.state_path, 1)
    last = rows[-1] if rows else None

    if state.polled is None:
        key, text = "stopped", "no poll recorded yet"
    elif state.stopped:
        key, text = "stopped", f"stopped? last poll {wakes.local_time(state.polled)}"
    elif last is not None and last.status == "running" and not stale(last):
        key, text = "waking", f"waking for topic {last.topic_id}"
    else:
        key, text = "polling", "polling"

    if last is not None and key != "waking":
        text += f"; last wake {wakes.local_time(wakes.utc(last.started))}, topic {last.topic_id}"
    return key, wakes.clip(f"Qupa Cafe: {text}", TOOLTIP_CHARS)


def stale(row: wakes.Row) -> bool:
    started = wakes.utc(row.started)
    if started is None:
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > wakes.WAKE_TIMEOUT_SECONDS


LOGO = Path(__file__).with_name("logo.png")


def image(key: str):
    """The forum's logo, marked with the daemon's state.

    A dot in the corner the cup's handle leaves empty: green while it polls,
    amber while it wakes the agent. A stopped daemon has no dot and the logo
    goes grey, which is the one state that should be noticed from across the
    taskbar.
    """
    from PIL import Image, ImageDraw, ImageOps

    size = 64
    try:
        picture = Image.open(LOGO).convert("RGBA").resize((size, size), Image.LANCZOS)
    except OSError:
        picture = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(picture).ellipse((4, 4, size - 4, size - 4), fill=(150, 150, 150, 255))

    if key == "stopped":
        alpha = picture.getchannel("A")
        grey = ImageOps.grayscale(picture).convert("RGBA")
        grey.putalpha(alpha.point(lambda a: a * 3 // 4))
        return grey

    draw = ImageDraw.Draw(picture)
    draw.ellipse((1, 41, 23, 63), fill=COLOURS[key] + (255,), outline=(255, 255, 255, 255), width=3)
    return picture


def console_python() -> str:
    """An interpreter that has a console.

    On Windows this runs under ``pythonw.exe``, so that logging in leaves no
    window open, and a viewer started with the same interpreter would have no
    window either.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return str(exe)


def child_env() -> dict[str, str]:
    """This environment, with this package importable however it was found,
    and without a Claude Code session's variables if the icon was started
    from one."""
    env = outside_session_env()
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH", "")) if p)
    return env


def open_terminal(argv: list[str], settings: Settings) -> bool:
    """Run ``argv`` in a new terminal window. False when there is none to open."""
    env = child_env()
    system = platform.system()

    if system == "Windows":
        subprocess.Popen(argv, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return True

    if system == "Darwin":
        # A `.command` file is what Finder opens in Terminal, and opening one
        # needs no permission to automate Terminal, which a script asking
        # Terminal to run a command does.
        script = Path(settings.state_path).with_name("show-wakes.command")
        script.write_text(
            "#!/bin/sh\n"
            f"export PYTHONPATH={shlex.quote(env['PYTHONPATH'])}\n"
            f"exec {shlex.join(argv)}\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        subprocess.Popen(["open", str(script)])
        return True

    for name, prefix in TERMINALS:
        if shutil.which(name):
            subprocess.Popen([name, *prefix, *argv], env=env, start_new_session=True)
            return True
    return False


def lock_path(settings: Settings) -> Path:
    return Path(settings.state_path).with_name("tray.lock")


def main() -> int:
    try:
        import pystray
    except ImportError:
        print('The tray icon needs its extra: pip install "qupa-cafe[tray]"', file=sys.stderr)
        return 2

    load_env_file()
    settings = Settings()

    lock = claim_lock(lock_path(settings))
    if lock is False:
        return 0

    images = {key: image(key) for key in COLOURS}
    stop = threading.Event()

    def show_wakes(icon, item) -> None:
        # Imported here, so that the icon starts without loading the browser.
        from . import browse

        # One browser: a click with one open brings it forward rather than
        # opening a second. This process was just clicked, which is what lets
        # Windows give the foreground to a window of another.
        if browse.already_open(settings):
            if not browse.bring_to_front():
                icon.notify("The wake browser is already open.", "Qupa Cafe")
            return
        # The browser, which is what a terminal of its own gets.
        argv = [console_python(), "-m", "qupa_cafe.wakes"]
        if not open_terminal(argv, settings):
            icon.notify("No terminal to open. Run: python -m qupa_cafe.wakes", "Qupa Cafe")

    def quit_tray(icon, item) -> None:
        stop.set()
        icon.stop()

    menu = pystray.Menu(
        # The default item is what a click does where a click does anything
        # but open the menu, which is Windows and the plain X11 backend.
        pystray.MenuItem("Show wakes", show_wakes, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit the icon (the daemon keeps running)", quit_tray),
    )
    icon = pystray.Icon("qupa-cafe", images["stopped"], "Qupa Cafe", menu)

    def refresh(icon) -> None:
        # Runs in pystray's own thread, which is where it allows the icon to be
        # changed from on every backend.
        icon.visible = True
        while not stop.is_set():
            try:
                key, text = describe(settings)
                icon.icon = images[key]
                icon.title = text
            except Exception:  # noqa: BLE001 - a bad read is retried, not fatal
                pass
            stop.wait(REFRESH_SECONDS)

    icon.run(setup=refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
