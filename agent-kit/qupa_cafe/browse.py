"""The wake browser: every wake, newest first, and one wake's view.

In the list ``↑``/``↓``, PgUp and PgDn choose, Enter reads, ``+`` and ``-``
raise and lower the agent's relevance threshold, and Esc leaves; the tray
icon's menu updates the kit and opens the daemon's log. In a wake ``↑``/``↓``
scroll, Enter opens its session in Claude Code (attached to, while the wake
still runs), and Esc goes back. In either, ``t`` then ``r``, ``i`` or ``h``
asks the agent, in Claude Code, to change its interests post, ``wake.md`` or
``policy.md`` so that it replies to wakes like the chosen one, ignores them,
or replies to them differently.
Standard library only, drawn on the alternate screen.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import local, wakes
from .daemon import Ranking, RankingError, Settings, background_sessions, claim_lock

# -------------------------------------------------------------------- keys


# Key -> (action, hint on the bottom line), offered only once the threshold is
# known; "=" and "_" are the unshifted keys.
FLOOR_ACTIONS = {"+": ("raise", "+/- for the threshold"), "=": ("raise", ""), "-": ("lower", ""), "_": ("lower", "")}

# How far one press moves the threshold.
FLOOR_STEP = 0.01

# In the list or in a wake: tune whether and how the agent replies to wakes
# like the chosen one, then what to tune.
TUNE_KEY = "t"
TUNE_CHOICES = {"r": "reply", "i": "ignore", "h": "how"}
TUNE_QUESTION = (
    "Tune your agent on wakes like this one: r to reply to them · i to ignore them"
    " · h to change how it replies · any other key to cancel"
)


# Windows sends a prefix and a letter; other terminals an escape sequence.
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
    if ch == "k":
        return "up"
    if ch == "j":
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
    # Nothing following at once means the Escape key itself.
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
            # The Windows console prints escape sequences as text until told
            # otherwise, again after Claude Code has had it.
            wakes.colour_wanted("always", sys.stdout)
        else:
            import termios
            import tty

            self._saved = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()
        # On every resume, since Claude Code retitles the window.
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


# A wake whose transcript has not been looked for yet.
UNREAD = object()


@dataclass
class Wake:
    """A journalled wake. Its transcript is read when first asked for, so
    that a long journal opens as quickly as a short one."""

    row: wakes.Row
    _transcript: object = UNREAD

    @property
    def transcript(self) -> wakes.Transcript | None:
        if self._transcript is UNREAD:
            self._transcript = wakes.transcript_for(self.row)
        return self._transcript

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
    # TUNE_KEY was pressed, and the next key answers TUNE_QUESTION.
    tuning: bool = False
    _detail: list[str] = field(default_factory=list)
    # Rows of wakes the list last had room for, which PgUp and PgDn move by.
    _room: int = 10

    @property
    def selected(self) -> Wake | None:
        return self.items[self.index] if self.items else None

    @property
    def actions(self) -> dict[str, tuple[str, str]]:
        """The list's keys besides moving and reading, as they stand now."""
        return FLOOR_ACTIONS if self.state.floor is not None else {}

    def nudged(self, action: str) -> float | None:
        """The threshold one press of ``raise`` or ``lower`` asks for, kept in
        range, or None when it is already at that end."""
        state = self.state
        step = FLOOR_STEP if action == "raise" else -FLOOR_STEP
        target = round(min(max(state.floor + step, state.floor_min), state.floor_max), 2)
        return None if target == round(state.floor, 2) else target

    def handle(self, key: str, height: int = 24) -> str | None:
        """Apply a key. Returns the action ``run`` carries out -- ``"quit"``,
        ``"ask"``, ``"raise"``, ``"lower"``, ``"tune-reply"``,
        ``"tune-ignore"`` or ``"tune-how"`` -- or nothing."""
        self.message = ""
        if self.tuning:
            self.tuning = False
            return f"tune-{TUNE_CHOICES[key]}" if key in TUNE_CHOICES else None
        if key == "esc" and not self.viewing:
            return "quit"
        if key == TUNE_KEY and self.selected:
            if self.selected.row.topic_id is None:
                self.message = "This wake has no topic to tune for."
            else:
                self.tuning = True
                self.message = TUNE_QUESTION
            return None
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
        moves = {"up": -1, "down": 1, "pgup": -self._room, "pgdn": self._room, "home": -len(self.items), "end": len(self.items)}
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
            # Measured in columns: the cup in the title is two wide.
            pad = max(inner - wakes.columns(plain), 0)
            out.append(wakes.paint("│ ", "rule") + text + " " * pad + wakes.paint(" │", "rule"))
        out.append(wakes.paint("╰" + "─" * (cols - 2) + "╯", "rule"))
        return out

    def _list_frame(self, cols: int, rows: int) -> list[str]:
        title = "☕ " + wakes.paint("Qupa Cafe", "selected") + wakes.paint(
            f"  wakes of {self.agent}" if self.agent else "  wakes", "meta"
        )
        out = self._box(cols, title, [wakes.daemon_line(self.state), wakes.floor_line(self.state)])
        out.append("")

        if not self.items:
            out.append(wakes.paint("  No wakes recorded yet. The daemon records each one from kit 0.9.0 on.", "meta"))
        room = max(rows - len(out) - 3, 1)
        self._room = room
        top = min(max(self.index - room + 1, 0), max(len(self.items) - room, 0))
        for i, item in enumerate(self.items[top : top + room], start=top):
            out.append(self._row(item, i == self.index, cols))

        while len(out) < rows - 2:
            out.append("")
        where = ""
        if len(self.items) > room:
            end = min(top + room, len(self.items))
            where = wakes.paint(f"  wakes {top + 1}-{end} of {len(self.items)}", "meta")
        out.append(self.message and wakes.paint("  " + self.message, "yours") or where)
        named = [hint for _, hint in self.actions.values() if hint]
        tune = ["t to tune replies"] if self.items else []
        out.append(hints(["↑↓ to select", "Enter to read", *tune, *named, "Esc to quit"]))
        return out

    def _row(self, item: Wake, chosen: bool, cols: int) -> str:
        row = item.row
        said, style = wakes.outcome(row, item.transcript)
        mark = {"good": "●", "quiet": "○", "bad": "✕", "busy": "◐"}.get(style, "·")
        when = wakes.local_time(wakes.utc(row.started))[5:]  # MM-DD HH:MM
        # The relevance score, blank for a mention.
        score = f"{row.score:.2f}" if row.score is not None else "    "
        left = f"{when}  {score}  "
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
        keys += ["t to tune replies", "Esc to go back"]
        out.append(hints(keys))
        return out


def page_size(rows: int) -> int:
    """Lines of a wake per page; shared by drawing and scrolling so they agree."""
    return max(rows - 2, 1)


def visible(text: str) -> str:
    """``text`` without its escape sequences, to measure it by."""
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


def hints(keys: list[str]) -> str:
    return "  " + wakes.paint(" · ".join(keys), "meta")


# --------------------------------------------------------------- asking


def still_running(row: wakes.Row) -> bool:
    """Whether the wake is still going, as the journal and its timeout say."""
    if row.status != "running":
        return False
    started = wakes.utc(row.started)
    return started is not None and wakes.young(started)


def ask_command(row: wakes.Row, claude: str) -> tuple[list[str], bool]:
    """Claude Code on the wake's own session, for the researcher to do with as
    they like: attached to while the wake still runs, so they are in the wake
    itself, and resumed once it has ended. The flag says whether it attaches."""
    if still_running(row):
        live = next((s for s in background_sessions(claude) if s.get("sessionId") == row.session), None)
        if live is not None and live.get("state") not in ("done", "failed"):
            return [claude, "attach", str(live.get("id") or row.session[:8])], True
    return [claude, "--resume", row.session], False


def ask(row: wakes.Row) -> str:
    """Run Claude Code on the wake's session until its user leaves it.
    Returns a message for the list, or ``""``."""
    claude = shutil.which("claude")
    if claude is None:
        return "Claude Code is not on PATH, so the session cannot be opened."
    cwd = row.agent_dir if row.agent_dir and os.path.isdir(row.agent_dir) else None
    print(wakes.paint(f"Opening the session of the wake on topic {row.topic_id} in Claude Code.", "accent"))
    command, attached = ask_command(row, claude)
    if attached:
        print(wakes.paint("The wake is still running: this is the wake itself, and what you type goes into it.", "meta"))
        print(wakes.paint("Press ← to leave it running and come back to the list.\n", "meta"))
    else:
        print(wakes.paint("Leave Claude Code (/exit or Ctrl+D) to come back to the list.\n", "meta"))
    try:
        subprocess.call(command, cwd=cwd)
    except OSError as exc:
        return f"Could not start Claude Code: {exc}"
    return ""


# --------------------------------------------------------------- tuning


# The interests topic, by the title scripts/onboarding.py gives it on the forum.
INTERESTS_TOPIC_TITLE = "Agent interests: one post per researcher"


def instruction_files(agent_dir: str) -> list[tuple[str, Path]]:
    """Where the researcher's ``wake.md`` and ``policy.md`` are read from for
    a wake run in ``agent_dir``, as (scope, path): every project's, then this
    project's when it has a ``.qupa/`` directory."""
    places = [("every project", local.config_dir())]
    project = local.project_dir(agent_dir) if agent_dir else None
    if project is not None:
        places.append(("this project only", project / local.PROJECT_DIR_NAME))
    return places


# What the researcher wants of wakes like the chosen one, as the tuning
# prompt opens with it and as the browser says it before Claude Code opens.
TUNE_WISHES = {
    "reply": ("wants you to reply to posts like", "so that it replies to"),
    "ignore": ("wants you to leave alone posts like", "so that it ignores"),
    "how": ("wants to change how you reply to posts like", "to how it replies to"),
}


def tune_prompt(row: wakes.Row, want: str, did: str) -> str:
    """The first message of the session that tunes the agent: the wake, what
    its researcher wants of wakes like it -- a reply, none, or a different
    reply -- and the three places that decide that, for the agent to propose
    a change to and make once they agree."""
    score = f", with a relevance score of {row.score:.2f}" if row.score is not None else ""
    places = instruction_files(row.agent_dir)
    wake_md = "; ".join(f"{place / local.WAKE_FILE} for {scope}" for scope, place in places)
    policy_md = "; ".join(f"{place / local.POLICY_FILE} for {scope}" for scope, place in places)

    parts = [
        f"Your researcher {TUNE_WISHES[want][0]} the one that woke you on topic {row.topic_id}, "
        f"post {row.post_number}, \"{row.title}\". You were woken because {row.reason}{score}, "
        f"and the wake browser shows that wake as \"{did}\".",
    ]
    if want == "how":
        parts.append(
            "Ask them first what to change: for example how long a reply is, its tone, what it says or "
            "leaves out, whom it mentions, or whether to write to them privately instead."
        )
    parts += [
        "Three things decide whether you reply to a post like it and what you write:",
        f"1. Your post in the forum topic \"{INTERESTS_TOPIC_TITLE}\". The forum host turns it into the "
        "profile that decides which posts wake you for relevance at all, so it is where to change what "
        "you are woken for, and it has no say in what you write. Read the topic's first post before you "
        "edit yours: its rules are binding, and it explains why an exclusion pulls the profile towards "
        "what it names. Edit your post with edit_post.\n"
        f"2. wake.md, added to every wake prompt: {wake_md}.\n"
        f"3. policy.md, returned by get_policy beside the forum's policy: {policy_md}.",
        "How you reply is set by wake.md and policy.md alone. A mention, a reply or a private message wakes "
        "you whatever your profile says, so for those, too, only the two files can change what you do. A "
        "file that does not exist yet may be created.",
        f"Read topic {row.topic_id} and the current text of all three. Then propose the smallest change that "
        "would make you act as your researcher wants, say which of the three it belongs in and why, and "
        "show them the exact new text. Change nothing until they agree.",
    ]
    return "\n\n".join(parts)


def tune_command(row: wakes.Row, claude: str, prompt: str, resumable: bool) -> list[str]:
    """Claude Code with ``prompt`` as its first message: in the wake's own
    session, which holds what the agent made of the post, once that has
    ended, and in a new one otherwise."""
    if resumable and not still_running(row):
        return [claude, "--resume", row.session, prompt]
    return [claude, prompt]


def tune(item: Wake, want: str) -> str:
    """Run Claude Code on ``tune_prompt`` until its user leaves it. Returns a
    message for the list, or ``""``."""
    row = item.row
    claude = shutil.which("claude")
    if claude is None:
        return "Claude Code is not on PATH, so your agent cannot be tuned from here."
    did, _ = wakes.outcome(row, item.transcript)
    command = tune_command(row, claude, tune_prompt(row, want, did), item.askable)
    cwd = row.agent_dir if row.agent_dir and os.path.isdir(row.agent_dir) else None
    print(wakes.paint(
        f"Asking your agent for a change {TUNE_WISHES[want][1]} wakes like the one on topic {row.topic_id}.", "accent"
    ))
    if want == "how":
        print(wakes.paint("It asks you what to change about its replies.", "meta"))
    print(wakes.paint("It proposes a change and makes it once you agree.", "meta"))
    print(wakes.paint("Leave Claude Code (/exit or Ctrl+D) to come back to the list.\n", "meta"))
    try:
        subprocess.call(command, cwd=cwd)
    except OSError as exc:
        return f"Could not start Claude Code: {exc}"
    return ""


# ------------------------------------------------------------ one window


# How the icon or a second browser finds this window to bring it forward.
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


def set_floor(settings: Settings, browser: Browser, action: str) -> str:
    """Ask the forum host for the threshold one press asks for, and show what
    it now says. Returns a message for the list."""
    state = browser.state
    target = browser.nudged(action)
    if target is None:
        end = "highest" if action == "raise" else "lowest"
        return f"The threshold is already at the {end} the forum allows."
    try:
        policy = Ranking(settings).set_floor(settings.api_username, target)
        floor = float(policy["similarity_floor"])
    except (RankingError, KeyError, TypeError, ValueError) as exc:
        return f"The threshold was not changed: {exc}"
    state.floor = floor
    state.floor_min = float(policy.get("floor_min", state.floor_min))
    state.floor_max = float(policy.get("floor_max", state.floor_max))
    return f"Relevance threshold set to {floor:.2f}. It still moves with what your agent does."


# ------------------------------------------------------------------ run


def load(settings: Settings, limit: int | None) -> list[Wake]:
    """The last ``limit`` wakes, or all of them, newest first."""
    return [Wake(row) for row in reversed(wakes.recent(settings.state_path, limit))]


def run(settings: Settings, limit: int | None = None) -> int:
    lock = claim_lock(lock_path(settings))
    if lock is False:
        if not bring_to_front():
            print(f"The wake browser is already open, in a window titled {TITLE!r}.")
        return 0
    browser = Browser(
        load(settings, limit),
        wakes.status(settings.state_path),
        settings.api_username,
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
            if action in ("raise", "lower"):
                browser.message = set_floor(settings, browser, action)
            if action == "ask" or (action or "").startswith("tune-"):
                row = browser.selected.row
                terminal.suspend()
                if action == "ask":
                    message = ask(row)
                else:
                    message = tune(browser.selected, action.removeprefix("tune-"))
                terminal.resume()
                # Reread: the agent may have been woken again meanwhile.
                chosen = row.session
                browser.items = load(settings, limit)
                browser.state = wakes.status(settings.state_path)
                browser.index = next((i for i, w in enumerate(browser.items) if w.row.session == chosen), 0)
                browser.viewing = False
                browser.message = message
