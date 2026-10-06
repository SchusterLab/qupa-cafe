"""The daemon's state as an icon in the task tray or menu bar.

Clicking it opens the wake browser (``qupa_cafe.browse``) in a terminal. A
viewer only: it reads the daemon's state file and holds its own lock so a
second copy exits. Needs ``pip install "qupa-cafe[tray]"``. Exits 0 when quit
or already running and 2 without the extra, as the Windows Startup entry
expects.
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

# Tried in order on Linux.
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
    """The logo with a green (polling) or amber (waking) dot, or greyed when stopped."""
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
    """``python.exe`` rather than the windowless interpreter the icon runs under."""
    exe = Path(sys.executable)
    if exe.name.lower() != "python.exe" and exe.with_name("python.exe").is_file():
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
        # Opening a `.command` file needs no permission to automate Terminal.
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
        from . import browse

        # A click with one open brings it forward; being the process just
        # clicked is what lets Windows hand over the foreground.
        if browse.already_open(settings):
            if not browse.bring_to_front():
                icon.notify("The wake browser is already open.", "Qupa Cafe")
            return
        argv = [console_python(), "-m", "qupa_cafe.wakes"]
        if not open_terminal(argv, settings):
            icon.notify("No terminal to open. Run: python -m qupa_cafe.wakes", "Qupa Cafe")

    def quit_tray(icon, item) -> None:
        stop.set()
        icon.stop()

    menu = pystray.Menu(
        # What a click does on Windows and plain X11.
        pystray.MenuItem("Show wakes", show_wakes, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit the icon (the daemon keeps running)", quit_tray),
    )
    icon = pystray.Icon("qupa-cafe", images["stopped"], "Qupa Cafe", menu)

    def refresh(icon) -> None:
        # In pystray's thread, the only one it lets change the icon everywhere.
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
