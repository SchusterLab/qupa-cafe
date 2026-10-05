"""The wakes, with the keyboard.

A list of the recent wakes, newest first, and a view of one of them. In the
list ``↑`` and ``↓`` choose, Enter reads the wake chosen, and Esc leaves. In a
wake ``↑`` and ``↓`` scroll, Enter opens its session in Claude Code so that the
agent who was woken can be asked about it, and Esc goes back to the list.

The list also opens the daemon's log (``l``) and, when a newer kit is
published, updates it (``u``, through ``qupa_cafe.update`` in this terminal).

Asking reopens the wake's own session with ``--fork-session``, so the questions
and answers go into a copy and the wake's record stays as it was. The forum's
write tools are withheld from that copy: it is a conversation with the
researcher, and nothing said in it should reach the forum by accident. Your
own Claude Code session, started the usual way, still posts.

Standard library only, as the rest of the kit is: ``msvcrt`` reads keys on
Windows and ``termios`` elsewhere, and the screen is drawn with escape
sequences, on the alternate screen so that leaving gives the terminal back as
it was.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, update, wakes
from .daemon import Settings, claim_lock, log_path
from .install import interpreter_argv

# Withheld from a session reopened to be asked about: every tool that writes to
# the forum. Reading stays, since "what did that thread say" is a fair question.
WRITE_TOOLS = tuple(f"mcp__qupa-cafe__{name}" for name in sorted(wakes.WRITE_TOOLS))

ASKING = (
    "Your researcher has reopened this session to ask you about the wake above: "
    "why you did what you did, and what would have changed it. Answer them here. "
    "The forum's write tools are not available in this session, so nothing you "
    "say here is posted."
)


# -------------------------------------------------------------------- keys


# What else the list does, by key: the action ``run`` carries out, and how the
# key is named on the list's bottom line. `u` is taken only while a newer kit
# is published and pip can install it here, and is named in the notice that
# says so instead; see Browser.actions.
LIST_ACTIONS = {
    "l": ("log", "l for the log"),
}
UPDATE_ACTION = {"u": ("update", "")}


# What the arrow and paging keys send. Windows gives a prefix and a letter;
# a terminal elsewhere gives an escape sequence.
WINDOWS_KEYS = {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn", "G": "home", "O": "end", "K": "left", "M": "right"}
ESCAPES = {
    "[A": "up", "[B": "down", "[C": "right", "[D": "left",
    "[5~": "pgup", "[6~": "pgdn",
    "[H": "home", "[F": "end", "[1~": "home", "[4~": "end",
    "OA": "up", "OB": "down", "OH": "home", "OF": "end",
}


def name_of(ch: str) -> str:
    """A key that is a single character, by name."""
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in ("k",):
        return "up"
    if ch in ("j",):
        return "down"
    return ch


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return WINDOWS_KEYS.get(msvcrt.getwch(), "")
        return name_of(ch)

    import select

    ch = sys.stdin.read(1)
    if ch != "\x1b":
        return name_of(ch)
    # An escape alone, or the start of a sequence: whatever follows at once is
    # the sequence, and nothing following means the Escape key itself.
    sequence = ""
    while select.select([sys.stdin], [], [], 0.03)[0]:
        sequence += sys.stdin.read(1)
        if sequence in ESCAPES or len(sequence) > 3:
            break
    return ESCAPES.get(sequence, "esc" if not sequence else "")


class Terminal:
    """The alternate screen, with no cursor and keys read one at a time."""

    def __enter__(self) -> "Terminal":
        self._saved = None
        if os.name == "nt":
            # The screen is switched with escape sequences, which the Windows
            # console prints as text until it is told otherwise -- with colour
            # off as much as on, and again after Claude Code has had it.
            wakes.colour_wanted("always", sys.stdout)
        else:
            import termios
            import tty

            self._saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        # Here rather than once, because Claude Code titles the window its own
        # way while a wake is being asked about.
        set_title()
        return self

    def __exit__(self, *exc) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if self._saved is not None:
            import termios

            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved)

    def suspend(self) -> None:
        self.__exit__()

    def resume(self) -> None:
        self.__enter__()

    @staticmethod
    def draw(lines: list[str]) -> None:
        sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(lines))
        sys.stdout.flush()


# ------------------------------------------------------------------ the app


@dataclass
class Wake:
    row: wakes.Row
    transcript: wakes.Transcript | None

    @property
    def askable(self) -> bool:
        return bool(self.row.session) and self.transcript is not None


@dataclass
class Browser:
    """What is on the screen and what a key does to it. No terminal in here,
    so that it can be driven, and tested, a key at a time."""

    items: list[Wake]
    state: wakes.Status
    agent: str = ""
    index: int = 0
    viewing: bool = False
    scroll: int = 0
    message: str = ""
    # The newer kit on the index, as it was when the browser opened, or None.
    latest: str | None = None
    # Whether pip can install it: false for a kit run from a checkout.
    updatable: bool = True
    _detail: list[str] = field(default_factory=list)

    @property
    def selected(self) -> Wake | None:
        return self.items[self.index] if self.items else None

    @property
    def actions(self) -> dict[str, tuple[str, str]]:
        """The list's keys besides moving and reading, as they stand now."""
        if self.latest and self.updatable:
            return {**UPDATE_ACTION, **LIST_ACTIONS}
        return LIST_ACTIONS

    def handle(self, key: str, height: int = 24) -> str | None:
        """Apply a key. Returns the action ``run`` carries out -- ``"quit"``,
        ``"ask"``, or one of ``LIST_ACTIONS`` -- or nothing."""
        self.message = ""
        if key == "esc" and not self.viewing:
            return "quit"
        if key in self.actions and not self.viewing:
            return self.actions[key][0]
        # Enter in a wake opens it in Claude Code; in the list it reads one.
        if key == "enter" and self.viewing:
            if self.selected and self.selected.askable:
                return "ask"
            self.message = "This wake has no session to open: its transcript is gone, or it was a Codex wake."
            return None

        if self.viewing:
            page = page_size(height)
            last = max(len(self._detail) - page, 0)
            moves = {"up": -1, "down": 1, "pgup": -page, "pgdn": page, "home": -len(self._detail), "end": len(self._detail)}
            if key in moves:
                self.scroll = min(max(self.scroll + moves[key], 0), last)
            elif key in ("esc", "left", "backspace", "\x08", "\x7f"):
                self.viewing = False
            return None

        if not self.items:
            return None
        moves = {"up": -1, "down": 1, "pgup": -10, "pgdn": 10, "home": -len(self.items), "end": len(self.items)}
        if key in moves:
            self.index = min(max(self.index + moves[key], 0), len(self.items) - 1)
        elif key in ("enter", "right"):
            self.viewing = True
            self.scroll = 0
            self._detail = wakes.render(self.selected.row, transcript=self.selected.transcript)
        return None

    # ------------------------------------------------------------ frames

    def frame(self, cols: int, rows: int) -> list[str]:
        return self._detail_frame(cols, rows) if self.viewing else self._list_frame(cols, rows)

    def _box(self, cols: int, title: str, lines: list[str]) -> list[str]:
        """Claude Code's rounded box: a grey border round an orange title."""
        inner = cols - 4
        out = [wakes.paint("╭" + "─" * (cols - 2) + "╮", "rule")]
        for text, plain in [(title, visible(title))] + [(line, visible(line)) for line in lines]:
            pad = max(inner - len(plain), 0)
            out.append(wakes.paint("│ ", "rule") + text + " " * pad + wakes.paint(" │", "rule"))
        out.append(wakes.paint("╰" + "─" * (cols - 2) + "╯", "rule"))
        return out

    def _list_frame(self, cols: int, rows: int) -> list[str]:
        title = wakes.paint("✻ ", "accent") + wakes.paint("Qupa Cafe", "selected") + wakes.paint(
            f"  wakes of {self.agent}" if self.agent else "  wakes", "meta"
        )
        out = self._box(cols, title, [wakes.daemon_line(self.state)])
        out.extend(self._notice(cols))
        out.append("")

        if not self.items:
            out.append(wakes.paint("  No wakes recorded yet. The daemon records each one from kit 0.9.0 on.", "meta"))
        room = max(rows - len(out) - 3, 1)
        top = min(max(self.index - room + 1, 0), max(len(self.items) - room, 0))
        for i, item in enumerate(self.items[top : top + room], start=top):
            out.append(self._row(item, i == self.index, cols))

        while len(out) < rows - 2:
            out.append("")
        out.append(self.message and wakes.paint("  " + self.message, "yours") or "")
        # `u` is named in the notice rather than here, the one key that is.
        out.append(hints(["↑↓ to select", "Enter to read", *(hint for _, hint in LIST_ACTIONS.values()), "Esc to quit"]))
        return out

    def _notice(self, cols: int) -> list[str]:
        """A newer kit, on a block of colour under the title: both versions,
        and how to get the new one."""
        if not self.latest:
            return []
        how = (
            "Press u to update: pip installs it and the installer restarts the daemon and the icon."
            if self.updatable
            else "This kit runs from a checkout: update the checkout, then run qupa-install."
        )
        lines = [f" ⬆  Agent kit {self.latest} is available. You have {__version__}.", f"    {how}"]
        width = max(cols - 2, 4)
        clipped = (line if len(line) <= width else line[: width - 3] + "..." for line in lines)
        return ["", *(" " + wakes.paint(line.ljust(width), "notice") for line in clipped)]

    def _row(self, item: Wake, chosen: bool, cols: int) -> str:
        row = item.row
        said, style = wakes.outcome(row, item.transcript)
        mark = {"good": "●", "quiet": "○", "bad": "✕", "busy": "◐"}.get(style, "·")
        when = wakes.local_time(wakes.utc(row.started))[5:]  # MM-DD HH:MM
        left = f"{when}  "
        middle = f"{said:<16} "
        room = max(cols - 4 - len(left) - 2 - len(middle), 10)
        title = wakes.clip(f"{row.title}  (topic {row.topic_id})", room)
        pointer = wakes.paint("❯ ", "accent") if chosen else "  "
        text_style = "selected" if chosen else "prompt"
        return (
            "  " + pointer + wakes.paint(left, "meta") + wakes.paint(mark + " ", style)
            + wakes.paint(middle, style) + wakes.paint(title, text_style)
        )

    def _detail_frame(self, cols: int, rows: int) -> list[str]:
        page = page_size(rows)
        shown = self._detail[self.scroll : self.scroll + page]
        out = list(shown)
        while len(out) < page:
            out.append("")
        end = min(self.scroll + page, len(self._detail))
        where = wakes.paint(f"  lines {self.scroll + 1}-{end} of {len(self._detail)}", "meta")
        out.append(self.message and wakes.paint("  " + self.message, "yours") or where)
        keys = ["↑↓ to scroll"]
        if self.selected and self.selected.askable:
            keys.append("Enter to open in Claude Code")
        keys.append("Esc to go back")
        out.append(hints(keys))
        return out


def page_size(rows: int) -> int:
    """How many lines of a wake fit, under which the position and the keys go.
    One number for drawing and for scrolling, or the end of a wake is never
    reached and the last page is drawn short."""
    return max(rows - 2, 1)


def visible(text: str) -> str:
    """``text`` without its escape sequences, to measure it by."""
    import re

    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def hints(keys: list[str]) -> str:
    return "  " + wakes.paint(" · ".join(keys), "meta")


# --------------------------------------------------------------- asking


def ask_command(row: wakes.Row, claude: str) -> list[str]:
    """Claude Code, reopening a wake's session as a copy, without the forum's
    write tools, and told why it has been reopened."""
    return [
        claude,
        "--resume", row.session,
        "--fork-session",
        "--disallowedTools", *WRITE_TOOLS,
        "--append-system-prompt", ASKING,
    ]


def ask(row: wakes.Row) -> str:
    """Run Claude Code on the wake's session until its user leaves it.
    Returns a message for the list, or ``""``."""
    claude = shutil.which("claude")
    if claude is None:
        return "Claude Code is not on PATH, so the session cannot be opened."
    cwd = row.agent_dir if row.agent_dir and os.path.isdir(row.agent_dir) else None
    print(wakes.paint(f"Opening the session of the wake on topic {row.topic_id} in Claude Code.", "accent"))
    print(wakes.paint("Your questions go into a copy of it, and the forum's write tools are withheld.", "meta"))
    print(wakes.paint("Leave Claude Code (/exit or Ctrl+D) to come back to the list.\n", "meta"))
    try:
        subprocess.call(ask_command(row, claude), cwd=cwd)
    except OSError as exc:
        return f"Could not start Claude Code: {exc}"
    return ""


# ------------------------------------------------------------ one window


# The browser's window title, which is how another process finds the window to
# bring it forward: the tray icon when it is clicked again, or a second browser.
TITLE = "Qupa Cafe: wakes"


def lock_path(settings: Settings) -> Path:
    return Path(settings.state_path).with_name("browser.lock")


def already_open(settings: Settings) -> bool:
    """Whether a browser holds the lock. False when there is nowhere to keep
    one, since then no browser could have taken it either."""
    lock = claim_lock(lock_path(settings))
    if lock:
        lock.close()
    return lock is False


def set_title() -> None:
    sys.stdout.write(f"\x1b]0;{TITLE}\x07")
    sys.stdout.flush()
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(TITLE)
        except Exception:  # noqa: BLE001 - a title is a convenience
            pass


def bring_to_front() -> bool:
    """Raise the open browser's window. False when it cannot be found, which
    includes macOS, where nothing here can raise another app's window."""
    if os.name == "nt":
        return _raise_on_windows()
    if sys.platform != "darwin" and shutil.which("wmctrl"):
        return subprocess.run(["wmctrl", "-a", TITLE], capture_output=True).returncode == 0
    return False


def _raise_on_windows() -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            text = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, text, length + 1)
            # Contained rather than equal: a terminal may add to its tab's title.
            if TITLE in text.value:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(each, 0)
    if not found:
        return False
    if user32.IsIconic(found[0]):
        user32.ShowWindow(found[0], 9)  # SW_RESTORE
    user32.SetForegroundWindow(found[0])
    return True


# ----------------------------------------------------- the list's actions


def open_file(path) -> None:
    """Open a file with whatever this desktop opens it with."""
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - the researcher's own log
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], start_new_session=True)


def show_log(settings: Settings) -> str:
    path = log_path(settings)
    if not path.is_file():
        return f"No daemon log yet at {path}."
    try:
        open_file(path)
    except OSError as exc:
        return f"Could not open {path}: {exc}"
    return f"Opened {path}."


def update_kit() -> tuple[bool, str]:
    """Run the update in this terminal, which pip and the installer print to.
    Whether it finished, and a message for the list."""
    print(wakes.paint("Updating the agent kit.\n", "accent"))
    code = subprocess.call(interpreter_argv("qupa_cafe.update"))
    if code != 0:
        return False, "The update did not finish; what it printed says why."
    return True, "Done. This window still runs the kit it was opened with: Esc, and open it again for the new one."


# ------------------------------------------------------------------ run


def load(settings: Settings, limit: int) -> list[Wake]:
    rows = list(reversed(wakes.recent(settings.state_path, limit)))
    return [Wake(row, wakes.transcript_for(row)) for row in rows]


def run(settings: Settings, limit: int) -> int:
    lock = claim_lock(lock_path(settings))
    if lock is False:
        if not bring_to_front():
            print(f"The wake browser is already open, in a window titled {TITLE!r}.")
        return 0
    browser = Browser(
        load(settings, limit),
        wakes.status(settings.state_path),
        settings.api_username,
        # Asked once, as it opens, with a short timeout: an index that cannot
        # be reached shows no notice and no `u`, and costs seconds at most.
        latest=update.update_available(),
        updatable=update.installed_from_index(),
    )
    with Terminal() as terminal:
        while True:
            size = shutil.get_terminal_size((100, 30))
            terminal.draw(browser.frame(size.columns - 1, size.lines))
            try:
                key = read_key()
            except KeyboardInterrupt:
                return 0
            action = browser.handle(key, size.lines)
            if action == "quit":
                return 0
            if action == "log":
                browser.message = show_log(settings)
            if action == "update":
                terminal.suspend()
                done, browser.message = update_kit()
                terminal.resume()
                if done:
                    # Installed; what is left is to reopen, which the message says.
                    browser.latest = None
            if action == "ask":
                row = browser.selected.row
                terminal.suspend()
                message = ask(row)
                terminal.resume()
                # Reread: the agent may have been woken again meanwhile.
                chosen = row.session
                browser.items = load(settings, limit)
                browser.state = wakes.status(settings.state_path)
                browser.index = next((i for i, w in enumerate(browser.items) if w.row.session == chosen), 0)
                browser.viewing = False
                browser.message = message
