"""The wake browser: every wake, newest first, and one wake's view.

In the list ``↑``/``↓``, PgUp and PgDn choose, Enter reads, ``+`` and ``-``
raise and lower the agent's relevance threshold, ``a`` steps what it does
with a preprint (``qupa_cafe.preprints``), and Esc leaves; the tray
icon's menu updates the kit and opens the daemon's log. In a wake ``↑``/``↓``
scroll, Enter opens its session in Claude Code (attached to, while the wake
still runs), and Esc goes back. In either, ``r`` has the daemon wake the
agent again for the chosen wake's post, as ``QUPA.md`` says now, and ``t``
then ``r``, ``i`` or ``h``
asks the agent, in Claude Code, to change its interests post or
``QUPA.md`` so that it replies to wakes like the chosen one, ignores them,
or replies to them differently. ``n`` swaps the wakes for the near misses,
the posts that came close to waking the agent, matched its exclusions or
sat in a category it skips, where ``t`` then ``w`` asks it to change what
wakes it. Standard library only, drawn on the alternate screen.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import local, models, preprints, wakes
from .daemon import (
    CLAUDE_ALLOWED_TOOLS, INTERESTS_TOPIC_TITLE, PARENT_NAME, TUNE_NAME, Ranking, RankingError, Rewake, Settings,
    ask_rewake, background_sessions, claim_lock,
)

# -------------------------------------------------------------------- keys


# Key -> (action, hint on the bottom line), offered only once the threshold is
# known; "=" and "_" are the unshifted keys.
FLOOR_ACTIONS = {"+": "raise", "=": "raise", "-": "lower", "_": "lower"}

# How far one press moves the threshold.
FLOOR_STEP = 0.01

# In the list or in a wake: tune the agent on posts like the chosen one, then
# what to tune. On a post that woke it, whether and how it replies; on one
# that did not, whether it is woken.
TUNE_KEY = "t"
TUNE_CHOICES = {"r": "reply", "i": "ignore", "h": "how"}
# The question, as (what it is about, the keys that answer it); the first is
# drawn to stand out. Any other key cancels, without saying so.
TUNE_QUESTION = (
    "Tune your agent on wakes like this one:",
    "r to reply to them · i to ignore them · h to change how it replies",
)
WAKE_CHOICES = {"w": "wake"}
WAKE_QUESTION = ("Tune your agent on posts like this one, which do not wake it:", "w to be woken for them")

# In the list: the posts that came close to waking the agent, in place of its
# wakes, and back.
MISSED_KEY = "n"

# In the list of wakes or in a wake: wake the agent again for its post.
REWAKE_KEY = "r"

# In either browser's list: step the model its wakes run on, and the effort.
MODEL_KEYS = {"m": "model", "f": "effort"}

# In the list: step what the agent does with a preprint (qupa_cafe.preprints).
ARXIV_KEY = "a"


def arxiv_line() -> str:
    """What the agent does with a preprint, for the box above the list."""
    mode = preprints.read()
    following = preprints.MODES[(preprints.MODES.index(mode) + 1) % len(preprints.MODES)]
    mark = wakes.paint("○ ", "meta") if mode == preprints.OFF else wakes.paint("● ", "good")
    return mark + f"Preprints are {mode}: your agent {preprints.DOES[mode]}" + wakes.paint(
        f"  (a to make them {following})", "meta"
    )


def change_arxiv() -> str:
    """Step what the agent does with a preprint, and say what it is now."""
    mode = preprints.step()
    return f"Preprints are {mode}: your agent {preprints.DOES[mode]}, from the daemon's next poll."


def model_line(kind: str) -> str:
    """The model and effort ``kind`` of wake runs on, for the box above a list."""
    return wakes.paint("◆ ", "meta") + f"Runs on {models.read(kind).describe()}" + wakes.paint(
        "  (m steps the model, f the effort)", "meta"
    )


def change_model(kind: str, action: str) -> str:
    """Step ``kind``'s model or effort, and say what it is now."""
    chosen = models.next_model(kind) if action == "model" else models.next_effort(kind)
    which = "Forum wakes" if kind == models.WAKE else "Diary wakes"
    return f"{which} now run on {chosen.describe()}, from the next one."

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


# How often an open browser rereads the journal while no key is pressed.
REFRESH_SECONDS = 5


def read_key(timeout: float | None = None) -> str:
    """The next key, by name, or "" when none comes in ``timeout`` seconds."""
    if os.name == "nt":
        import msvcrt
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        while deadline is not None and not msvcrt.kbhit():
            if time.monotonic() >= deadline:
                return ""
            time.sleep(0.05)
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return WINDOWS_KEYS.get(msvcrt.getwch(), "")
        return name_of(ch)

    import select

    # From the descriptor, not sys.stdin: its buffer would take the whole of
    # an arrow key's sequence on the first read, leaving select nothing to
    # see, and the key would read as Escape.
    fd = sys.stdin.fileno()
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        return ""
    ch = read_char(fd)
    if ch != "\x1b":
        return name_of(ch)
    # Nothing following at once means the Escape key itself.
    sequence = ""
    while select.select([fd], [], [], 0.03)[0]:
        sequence += read_char(fd)
        if sequence in ESCAPES or len(sequence) > 3:
            break
    return ESCAPES.get(sequence, "esc" if not sequence else "")


def read_char(fd: int) -> str:
    """One character from ``fd``, as many bytes of UTF-8 as it takes."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        byte = os.read(fd, 1)
        if not byte:
            raise EOFError
        ch = decoder.decode(byte)
        if ch:
            return ch


class Terminal:
    """The alternate screen, with no cursor and keys read one at a time, in a
    window titled ``title``."""

    def __init__(self, title: str = "") -> None:
        self.title = title or TITLE

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
        set_title(self.title)
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
class Subject:
    """What a tuning session is about: a wake, or a post that did not wake
    the agent."""

    topic_id: int
    post_number: int
    title: str
    # Whether posts like it wake the agent. Tuning one that does is about
    # replying to it; one that does not, about waking.
    woke: bool
    # Why it woke the agent or did not, as a sentence for the agent.
    why: str
    agent_dir: str
    # What the wake browser shows the wake as, for a wake.
    did: str = ""
    # The agent's similarity floor, where the post was compared with it.
    floor: float | None = None
    # The wake's session, to tune in, once the wake has ended.
    session: str = ""


@dataclass
class Browser:
    """What is on the screen and what a key does to it. No terminal in here,
    so that it can be driven, and tested, a key at a time."""

    items: list[Wake]
    state: wakes.Status
    agent: str = ""
    # Where a tuning session runs when the post was not a wake.
    agent_dir: str = ""
    missed: list[wakes.NearMiss] = field(default_factory=list)
    # The list shows the posts that did not wake the agent rather than its wakes.
    showing_missed: bool = False
    index: int = 0
    missed_index: int = 0
    viewing: bool = False
    scroll: int = 0
    message: str = ""
    # What TUNE_KEY offered to tune, the keys that answer, and the question
    # they answer, drawn in place of the message while it is open.
    subject: Subject | None = None
    choices: dict[str, str] = field(default_factory=dict)
    question: tuple[str, str] = ("", "")
    _detail: list[str] = field(default_factory=list)
    # Rows the list last had room for, which PgUp and PgDn move by.
    _room: int = 10
    # The wake's lines as last drawn, folded to the width, and the page they
    # were drawn in, which scrolling moves through.
    _folded: tuple = ()
    _page: int = 0

    @property
    def selected(self) -> Wake | None:
        return self.items[self.index] if self.items else None

    @property
    def selected_miss(self) -> wakes.NearMiss | None:
        return self.missed[self.missed_index] if self.missed else None

    @property
    def tuning(self) -> bool:
        return bool(self.choices)

    @property
    def actions(self) -> dict[str, str]:
        """The list's keys besides moving and reading, as they stand now."""
        return FLOOR_ACTIONS if self.state.floor is not None else {}

    def nudged(self, action: str) -> float | None:
        """The threshold one press of ``raise`` or ``lower`` asks for, kept in
        range, or None when it is already at that end."""
        state = self.state
        step = FLOOR_STEP if action == "raise" else -FLOOR_STEP
        target = round(min(max(state.floor + step, state.floor_min), state.floor_max), 2)
        return None if target == round(state.floor, 2) else target

    def offer(self, subject: Subject) -> None:
        """Ask what to tune on ``subject``; the next key answers."""
        self.subject = subject
        self.choices = TUNE_CHOICES if subject.woke else WAKE_CHOICES
        self.question = TUNE_QUESTION if subject.woke else WAKE_QUESTION

    def handle(self, key: str, height: int = 24) -> str | None:
        """Apply a key. Returns the action ``run`` carries out -- ``"quit"``,
        ``"ask"``, ``"rewake"``, ``"raise"``, ``"lower"``, or ``"tune-"`` and
        what to tune -- or nothing."""
        self.message = ""
        if self.choices:
            choices, self.choices = self.choices, {}
            return f"tune-{choices[key]}" if key in choices else None
        if key == "esc" and not self.viewing:
            return "quit"
        if key == TUNE_KEY:
            self._tune()
            return None
        if key == REWAKE_KEY and self.selected and (self.viewing or not self.showing_missed):
            return "rewake"
        if key == MISSED_KEY and not self.viewing:
            self.showing_missed = not self.showing_missed
            return None
        if key in self.actions and not self.viewing:
            return self.actions[key]
        if key in MODEL_KEYS and not self.viewing:
            return MODEL_KEYS[key]
        if key == ARXIV_KEY and not self.viewing:
            return "arxiv"
        # Enter in a wake opens it in Claude Code; in the list it reads one.
        if key == "enter" and self.viewing:
            if self.selected and self.selected.askable:
                return "ask"
            self.message = "This wake has no session to open: its transcript is gone."
            return None

        if self.viewing:
            scroll(self, key, height)
            return None

        shown = self.missed if self.showing_missed else self.items
        if not shown:
            return None
        moves = {"up": -1, "down": 1, "pgup": -self._room, "pgdn": self._room, "home": -len(shown), "end": len(shown)}
        if key in moves:
            if self.showing_missed:
                self.missed_index = min(max(self.missed_index + moves[key], 0), len(shown) - 1)
            else:
                self.index = min(max(self.index + moves[key], 0), len(shown) - 1)
        elif key in ("enter", "right"):
            if self.showing_missed:
                self.message = "A post that did not wake your agent has no wake to read: t tunes your agent on it."
            else:
                self.viewing = True
                self.scroll = 0
                self._detail = wakes.render(self.selected.row, transcript=self.selected.transcript)
        return None

    def _tune(self) -> None:
        if self.showing_missed and not self.viewing:
            if self.selected_miss:
                self.offer(subject_of_miss(self.selected_miss, self.agent_dir))
            return
        if not self.selected:
            return
        if wakes.is_parent(self.selected.row):
            self.message = "The parent session has no post to tune for: it is what every wake starts from."
            return
        if self.selected.row.topic_id is None:
            self.message = "This wake has no topic to tune for."
            return
        self.offer(subject_of_wake(self.selected, self.agent_dir))

    # ------------------------------------------------------------ frames

    def status_line(self) -> str:
        """The line above the keys: an open question, else the message, else
        nothing, for the caller to fill."""
        if self.choices:
            about, keys = self.question
            return "  " + wakes.paint(about, "accent_bold") + " " + wakes.paint(keys, "yours")
        return self.message and wakes.paint("  " + self.message, "yours")

    def frame(self, cols: int, rows: int) -> list[str]:
        if self.viewing:
            return self._detail_frame(cols, rows)
        return self._missed_frame(cols, rows) if self.showing_missed else self._list_frame(cols, rows)

    def _listing(
        self, cols: int, rows: int, what: str, count: int, line, index: int, empty: str, keys: list[str]
    ) -> list[str]:
        """The list, of wakes or of near misses, with what is around it."""
        title = "☕ " + wakes.paint("Qupa Cafe", "selected") + wakes.paint(
            f"  {what} of {self.agent}" if self.agent else f"  {what}", "meta"
        )
        header = box(cols, title, [wakes.daemon_line(self.state), wakes.floor_line(self.state), model_line(models.WAKE),
                                   arxiv_line()])
        other = "n for wakes" if self.showing_missed else "n for near misses"
        out, self._room = listing(cols, rows, header, what, count, line, index, empty, self.status_line(),
                                  [*keys, other, "Esc to quit"])
        return out

    def _list_frame(self, cols: int, rows: int) -> list[str]:
        def line(i: int) -> str:
            return self._row(self.items[i], i == self.index, cols)

        keys = ["↑↓ to select", "Enter to read", *(["r to wake again", "t to tune replies"] if self.items else [])]
        empty = "No wakes recorded yet. The daemon records each one from kit 0.9.0 on."
        return self._listing(cols, rows, "wakes", len(self.items), line, self.index, empty, keys)

    def _missed_frame(self, cols: int, rows: int) -> list[str]:
        def line(i: int) -> str:
            return self._miss_row(self.missed[i], i == self.missed_index, cols)

        keys = ["↑↓ to select", *(["t to tune waking"] if self.missed else [])]
        empty = "No near misses: nothing has lately scored close under the threshold or sat in a skipped category."
        return self._listing(cols, rows, "near misses", len(self.missed), line, self.missed_index, empty, keys)

    def _row(self, item: Wake, chosen: bool, cols: int) -> str:
        row = item.row
        said, style = wakes.outcome(row, item.transcript)
        mark = row_mark(style)
        when = wakes.local_time(wakes.utc(row.started))[5:]  # MM-DD HH:MM
        # The relevance score, blank for a mention.
        score = f"{row.score:.2f}" if row.score is not None else "    "
        title = row.title if row.topic_id is None else f"{row.title}  (topic {row.topic_id})"
        return self._line(chosen, cols, f"{when}  {score}  {tokens_column(item)}  ", mark, f"{said:<16} ", style, title,
                          "accent" if style == "accent" else None)

    def _miss_row(self, miss: wakes.NearMiss, chosen: bool, cols: int) -> str:
        try:
            when = wakes.local_time(datetime.fromisoformat(miss.at))[5:]
        except ValueError:
            when = "?"
        score = f"{miss.similarity:.2f}" if miss.similarity is not None else "    "
        said = {"below-floor": f"under {miss.floor:.2f}" if miss.floor is not None else "under",
                "excluded": "excluded"}.get(miss.reason, "skipped")
        title = f"{miss.title}  (topic {miss.topic_id})"
        return self._line(chosen, cols, f"{when}  {score}  ", "·", f"{said:<16} ", "quiet", title)

    @staticmethod
    def _line(chosen: bool, cols: int, left: str, mark: str, middle: str, style: str, title: str,
              title_style: str | None = None) -> str:
        room = max(cols - 4 - len(left) - 2 - len(middle), 10)
        title = wakes.clip(title, room)
        pointer = wakes.paint("❯ ", "accent") if chosen else "  "
        text_style = "selected" if chosen else title_style or "prompt"
        return (
            "  " + pointer + wakes.paint(left, "meta") + wakes.paint(mark + " ", style)
            + wakes.paint(middle, style) + wakes.paint(title, text_style)
        )

    def _detail_frame(self, cols: int, rows: int) -> list[str]:
        keys = ["↑↓ to scroll"]
        if self.selected and self.selected.askable:
            keys.append("Enter to open in Claude Code")
        keys += ["r to wake again", "t to tune replies", "Esc to go back"]
        return detail_frame(self, cols, rows, keys)


def row_mark(style: str) -> str:
    """The mark before a row's outcome: the parent's, or how a wake ended."""
    return {"good": "●", "quiet": "○", "bad": "✕", "busy": "◐", "accent": "◈"}.get(style, "·")


def reloaded(old: list[Wake], rows: list[wakes.Row]) -> list[Wake]:
    """The journal's rows as the browser's items, keeping each ended one's
    transcript as already read, so that a refresh rereads only what may
    have changed."""
    kept = {(w.row.session, w.row.status, w.row.finished): w for w in old if w.row.status != "running"}
    return [kept.get((row.session, row.status, row.finished)) or Wake(row) for row in rows]


def refresh(browser, items: list[Wake], state: wakes.Status) -> None:
    """Show the journal as it stands now, keeping the selected row, and in a
    wake still running, the place read to."""
    chosen = browser.selected.row.session if browser.selected else ""
    browser.items = items
    browser.state = state
    found = next((i for i, w in enumerate(items) if chosen and w.row.session == chosen), None)
    browser.index = found if found is not None else min(browser.index, max(len(items) - 1, 0))
    if browser.viewing and found is not None:
        # The place read to is kept; drawing keeps it within the lines.
        browser._detail = wakes.render(items[found].row, transcript=items[found].transcript)
    elif browser.viewing:
        browser.viewing = False


def tokens_column(item: Wake) -> str:
    """A wake's cost for its row in a list, in input-token equivalents, as
    wide whether known or not."""
    transcript = item.transcript
    cost = round(transcript.usage.equivalent) if transcript is not None else 0
    return f"{wakes.count(cost) + ' tok' if cost else '':>9}"


def box(cols: int, title: str, lines: list[str]) -> list[str]:
    """Claude Code's rounded box: a grey border round an orange title."""
    inner = cols - 4
    out = [wakes.paint("╭" + "─" * (cols - 2) + "╮", "rule")]
    # A line too long for the box goes on inside it, on the next.
    for text in [piece for line in [title, *lines] for piece in fold(line, inner)]:
        # Measured in columns: the cup in the title is two wide.
        pad = max(inner - wakes.columns(visible(text)), 0)
        out.append(wakes.paint("│ ", "rule") + text + " " * pad + wakes.paint(" │", "rule"))
    out.append(wakes.paint("╰" + "─" * (cols - 2) + "╯", "rule"))
    return out


def page_size(rows: int) -> int:
    """Lines of a wake per page before the first drawing has measured it."""
    return max(rows - 2, 1)


ESCAPE = r"\x1b\[[0-9;?]*[A-Za-z]"
# An escape sequence or one character, in the order they are drawn.
DRAWN = re.compile(ESCAPE + "|.", re.S)
RESET = "\x1b[0m"


def visible(text: str) -> str:
    """``text`` without its escape sequences, to measure it by."""
    return re.sub(ESCAPE, "", text)


def fold(text: str, cols: int) -> list[str]:
    """``text`` as lines at most ``cols`` columns wide, broken at a space
    when one comes late enough on the line and anywhere otherwise, the lines
    after the first indented as the first is, past any mark it begins with,
    and each in the colours it had. A line wider than the terminal would wrap there, and the screen
    would scroll its top line out of view."""
    plain = visible(text)
    if wakes.columns(plain) <= cols:
        return [text]
    cols = max(cols, 2)
    # Each character drawn, with the colour it is drawn in.
    cells: list[tuple[str, str]] = []
    colour = ""
    for token in DRAWN.findall(text):
        if token.startswith("\x1b["):
            if token.endswith("m"):
                colour = "" if token in (RESET, "\x1b[m") else colour + token
            continue
        cells.append((colour, token))
    lead = min(wakes.columns(re.match(r" *(?:[^\w\s] )?", plain).group()), cols // 2)
    lines: list[str] = []
    start = 0
    while start < len(cells):
        room = cols if not lines else cols - lead
        end, used, space = start, 0, None
        while end < len(cells) and used + wakes.columns(cells[end][1]) <= room:
            if cells[end][1] == " ":
                space = end
            used += wakes.columns(cells[end][1])
            end += 1
        if end < len(cells):
            if cells[end][1] == " ":
                space = end
            if space is not None and space - start > room // 2:
                end = space
        end = max(end, start + 1)
        lines.append(("" if not lines else " " * lead) + painted(cells[start:end]))
        start = end + 1 if end < len(cells) and cells[end][1] == " " else end
    return lines


def painted(cells: list[tuple[str, str]]) -> str:
    """Characters in their colours, as one line that ends with none."""
    out, current = "", ""
    for colour, ch in cells:
        if colour != current:
            out += (RESET if current else "") + colour
            current = colour
        out += ch
    return out + (RESET if current else "")


def hints(keys: list[str]) -> str:
    return "  " + wakes.paint(" · ".join(keys), "meta")


def hint_lines(keys: list[str], cols: int) -> list[str]:
    """The keys on as many lines as the width needs, broken between keys."""
    lines: list[list[str]] = [[]]
    for key in keys:
        if lines[-1] and wakes.columns(visible(hints([*lines[-1], key]))) > cols:
            lines.append([])
        lines[-1].append(key)
    return [piece for line in lines for piece in fold(hints(line), cols)]


def listing(cols: int, rows: int, header: list[str], what: str, count: int, line, index: int, empty: str,
            status: str, keys: list[str]) -> tuple[list[str], int]:
    """A list under ``header``, with the status line, or where the list is
    scrolled to, and the keys below it, every line within the width so that
    the screen never scrolls. ``line(i)`` draws the ``i``th, and only those
    on screen are drawn, so a long journal reads only the transcripts it
    shows. Returns the lines and the rows the list had room for."""
    above = fold(status, cols) if status else [""]
    below = hint_lines(keys, cols)
    gone = fold(wakes.paint("  " + empty, "meta"), cols) if not count else []
    # On a screen too short for it all, the box gives up its lines from the
    # bottom up, keeping its title and its border, before the list goes.
    spare = rows - len(above) - len(below) - len(gone) - 3
    if spare < len(header):
        header = header[: max(spare - 1, 0)] + header[-1:] if spare >= 2 else []
    out = [*header, "", *gone]
    room = max(rows - len(out) - len(above) - len(below) - 1, 1)
    top = min(max(index - room + 1, 0), max(count - room, 0))
    out += [fold(line(i), cols)[0] for i in range(top, min(top + room, count))]
    while len(out) < rows - len(above) - len(below):
        out.append("")
    if not status and count > room:
        above = [wakes.paint(f"  {what} {top + 1}-{min(top + room, count)} of {count}", "meta")]
    return (out + above + below)[:rows], room


def folded_detail(browser, cols: int) -> list[str]:
    """The lines of the wake ``browser`` is reading, folded to ``cols``,
    folded again only when the lines or the width have changed."""
    kept = browser._folded
    if not kept or kept[0] is not browser._detail or kept[1] != cols:
        browser._folded = (browser._detail, cols, [piece for line in browser._detail for piece in fold(line, cols)])
    return browser._folded[2]


def detail_frame(browser, cols: int, rows: int, keys: list[str]) -> list[str]:
    """A page of the wake ``browser`` is reading, with the status line, or
    where the page is, and the keys below it, every line within the width."""
    lines = folded_detail(browser, cols)
    status = browser.status_line()
    above = fold(status, cols) if status else [""]
    below = hint_lines(keys, cols)
    page = max(rows - len(above) - len(below), 1)
    browser._page = page
    browser.scroll = min(browser.scroll, max(len(lines) - page, 0))
    out = lines[browser.scroll : browser.scroll + page]
    out += [""] * (page - len(out))
    if not status:
        end = min(browser.scroll + page, len(lines))
        above = [wakes.paint(f"  lines {browser.scroll + 1}-{end} of {len(lines)}", "meta")]
    return (out + above + below)[:rows]


def scroll(browser, key: str, height: int) -> None:
    """Apply a key to the wake ``browser`` is reading: move through it by the
    lines and the page it was last drawn with, or go back to the list."""
    kept = browser._folded
    lines = kept[2] if kept and kept[0] is browser._detail else browser._detail
    page = browser._page or page_size(height)
    last = max(len(lines) - page, 0)
    moves = {"up": -1, "down": 1, "pgup": -page, "pgdn": page, "home": -len(lines), "end": len(lines)}
    if key in moves:
        browser.scroll = min(max(browser.scroll + moves[key], 0), last)
    elif key in ("esc", "left", "backspace", "\x08", "\x7f"):
        browser.viewing = False


# --------------------------------------------------------------- asking


def still_running(row: wakes.Row) -> bool:
    """Whether the wake is still going, as the journal and its timeout say."""
    if row.status != "running":
        return False
    started = wakes.utc(row.started)
    return started is not None and wakes.young(started)


# A wake runs in dontAsk, which refuses every tool not approved beforehand,
# and a resumed session keeps its mode. With the researcher there, Claude
# Code asks them instead.
RESUME_MODE = ["--permission-mode", "manual"]


def ask_command(row: wakes.Row, claude: str) -> tuple[list[str], bool]:
    """Claude Code on the wake's own session, for the researcher to do with as
    they like: attached to while the wake still runs, so they are in the wake
    itself, and resumed once it has ended. The flag says whether it attaches."""
    if still_running(row):
        live = next((s for s in background_sessions(claude) if s.get("sessionId") == row.session), None)
        if live is not None and live.get("state") not in ("done", "failed"):
            return [claude, "attach", str(live.get("id") or row.session[:8])], True
    # The parent opens as a fork of its own: a turn typed into the parent
    # itself would reach every later wake. Named as the parent is, so that
    # the diary does not take it for its researcher's own work.
    fork = ["--fork-session", "--name", f"{PARENT_NAME} copy"] if wakes.is_parent(row) else []
    return [claude, "--resume", row.session, *fork, *RESUME_MODE], False


def ask(row: wakes.Row) -> str:
    """Run Claude Code on the wake's session until its user leaves it.
    Returns a message for the list, or ``""``."""
    claude = shutil.which("claude")
    if claude is None:
        return "Claude Code is not on PATH, so the session cannot be opened."
    cwd = row.agent_dir if row.agent_dir and os.path.isdir(row.agent_dir) else None
    what = f"the wake on topic {row.topic_id}" if row.topic_id is not None else row.title
    print(wakes.paint(f"Opening the session of {what} in Claude Code.", "accent"))
    if wakes.is_parent(row):
        print(wakes.paint("It opens as a copy: wakes go on forking the parent as it is.", "meta"))
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


def rewake(settings: Settings, item: Wake) -> str:
    """Ask the daemon to wake the agent again for the wake's post. Returns a
    message for the list."""
    row = item.row
    if wakes.is_parent(row):
        return "The parent session has no post to wake your agent for again: it is what every wake starts from."
    if row.topic_id is None or not row.post_number:
        return "This wake has no post to wake your agent for again."
    where = f"post {row.post_number} of topic {row.topic_id}"
    if not ask_rewake(settings.state_path, Rewake(row.topic_id, row.post_number, row.reason, row.score)):
        return f"Your agent is already waiting to be woken again for {where}."
    return f"Your agent is woken again for {where} at the daemon's next poll, as QUPA.md says now."


# --------------------------------------------------------------- tuning


def subject_of_wake(item: Wake, agent_dir: str) -> Subject:
    row = item.row
    score = f", with a relevance score of {row.score:.2f}" if row.score is not None else ""
    did, _ = wakes.outcome(row, item.transcript)
    resumable = item.askable and not still_running(row)
    return Subject(
        topic_id=row.topic_id, post_number=row.post_number or 1, title=row.title, woke=True,
        why=f"You were woken for it because {row.reason}{score}", agent_dir=row.agent_dir or agent_dir,
        did=did, session=row.session if resumable else "",
    )


def subject_of_miss(miss: wakes.NearMiss, agent_dir: str) -> Subject:
    return Subject(
        topic_id=miss.topic_id, post_number=miss.post_number, title=miss.title, woke=False,
        why=did_not_wake(miss.reason, miss.similarity, miss.floor, miss.category), agent_dir=agent_dir,
        floor=miss.floor,
    )


def did_not_wake(reason: str, similarity: float | None, floor: float | None, category: str = "") -> str:
    """Why a post did not wake the agent, as the forum host's reason says."""
    if reason == "category-skipped":
        where = f"it is in {category}, a category" if category else "it is in a category"
        return f"It did not wake you: {where} the \"Categories to skip:\" line of your interests post names"
    if reason == "excluded":
        score = f" (it scored {similarity:.2f})" if similarity is not None else ""
        return (f"It did not wake you: it cleared your similarity floor{score}, but it matched the \"Not interested "
                "in:\" line of your interests post at least as well as your interests")
    if reason == "below-floor" and similarity is not None and floor is not None:
        return f"It did not wake you: it scored {similarity:.2f} against your similarity floor of {floor:.2f}"
    return "It did not wake you"


# What the researcher wants of posts like the chosen one, as the tuning
# prompt opens with it and as the browser says it before Claude Code opens.
TUNE_WISHES = {
    "reply": ("wants you to reply to posts like", "so that it replies to"),
    "ignore": ("wants you to leave alone posts like", "so that it ignores"),
    "how": ("wants to change how you reply to posts like", "to how it replies to"),
    "wake": ("wants you woken for posts like", "so that it is woken for"),
}

INTERESTS_POST = (
    f"Your post in the forum topic \"{INTERESTS_TOPIC_TITLE}\". The forum host turns it into the profile it "
    "scores every post against, which decides which posts wake you for relevance at all, and every wake reads it "
    "too, to judge what in a post bears on your researcher's work and what to say about it. Read the topic's "
    "first post before you edit yours: its rules are binding, and it explains how the \"Not interested in:\" and \"Categories to skip:\" lines hold posts back. "
    "Edit your post with edit_post."
)


def tune_prompt(subject: Subject, want: str) -> str:
    """The first message of the session that tunes the agent: the post, what
    its researcher wants of posts like it -- to be woken for them, a reply,
    none, or a different reply -- and the places that decide that, for the
    agent to propose a change to and make once they agree."""
    opening = (
        f"Your researcher {TUNE_WISHES[want][0]} the one on topic {subject.topic_id}, post {subject.post_number}, "
        f"\"{subject.title}\". {subject.why}."
    )
    if subject.did:
        opening += f" The wake browser shows that wake as \"{subject.did}\"."
    parts = [opening]

    if want == "wake":
        floor = f", {subject.floor:.2f}," if subject.floor is not None else ""
        parts += [
            "Two things decide whether a post wakes you for relevance:",
            f"1. {INTERESTS_POST}\n"
            f"2. Your similarity floor{floor} the score a post must reach to wake you. Only your researcher sets "
            "it, with + and - in the wake browser, and lowering it wakes you for everything that scores as close, "
            "not only for posts like this one.",
            "QUPA.md cannot make a post wake you: it decides only what you do once woken.",
            f"Read topic {subject.topic_id} and your interests post. Then propose the smallest change that would "
            "wake you for posts like it, say whether it is an edit to your post or a lower floor and why, and show "
            "your researcher the exact new text. Change nothing until they agree.",
        ]
        return "\n\n".join(parts)

    prompt_md = instructions_as_they_stand(subject.agent_dir)
    if want == "how":
        parts.append(
            "Ask them first what to change: for example how long a reply is, its tone, what it says or "
            "leaves out, whom it mentions, or whether to write to them privately instead."
        )
    parts += [
        "Two things decide whether you reply to a post like it and what you write:",
        f"1. {INTERESTS_POST}\n"
        "2. QUPA.md, your researcher's own instructions, added to every wake prompt and returned by "
        "get_policy beside the forum's policy. They override the policy's defaults, never its rules."
        f"{prompt_md}",
        "Beyond the policy's defaults, how you reply is set by QUPA.md alone. A mention, a reply or a "
        "private message wakes you whatever your profile says, so for those, too, only QUPA.md can change "
        "what you do. A file that does not exist yet may be created.",
        f"Read topic {subject.topic_id} and your interests post, and call get_policy to see which of its parts "
        "are defaults; QUPA.md stands as above, so there is no need to read it. Then propose the smallest "
        "change that would make you act as your researcher wants, say whether it belongs in your post or in "
        "QUPA.md and why, and show them the exact new text. If a rule of the policy forbids what they "
        "want, say so rather than propose it. Change nothing until they agree.",
    ]
    return "\n\n".join(parts)


def instructions_as_they_stand(agent_dir: str) -> str:
    """Every ``QUPA.md`` a wake run in ``agent_dir`` reads, as
    ``as_it_stands`` gives each: every project's, then the project's."""
    return "".join(as_it_stands(path, scope) for scope, path in local.places(agent_dir or None))


def as_it_stands(path: Path, scope: str) -> str:
    """One instruction file for the tuning prompt: where it is and what it
    says, read here so that the agent need not read where the kit keeps its
    key."""
    text = local.read(path)
    if not text:
        return f"\n   {path}, for {scope}: does not exist yet, or is empty."
    return f"\n   {path}, for {scope}, which reads:\n\n```markdown\n{text}\n```\n"


def tune_command(subject: Subject, claude: str, prompt: str) -> list[str]:
    """Claude Code with ``prompt`` as its first message: in the wake's own
    session, which holds what the agent made of the post, when it has one
    that has ended, and in a new one otherwise, named as the kit's so that
    the diary leaves it out. The forum's tools, the agent kit's and the
    read-only discourse server's, are approved as for a wake, so reading the
    forum and editing the agent's post ask nothing; a file edit still asks.
    The prompt goes first, since --allowedTools takes every word after it."""
    if subject.session:
        where = ["--resume", subject.session, *RESUME_MODE]
    else:
        where = ["--name", f"{TUNE_NAME} topic {subject.topic_id} post {subject.post_number}"]
    return [claude, prompt, *where, "--allowedTools", ",".join(CLAUDE_ALLOWED_TOOLS)]


def tune(subject: Subject, want: str) -> str:
    """Run Claude Code on ``tune_prompt`` until its user leaves it. Returns a
    message for the list, or ``""``."""
    claude = shutil.which("claude")
    if claude is None:
        return "Claude Code is not on PATH, so your agent cannot be tuned from here."
    command = tune_command(subject, claude, tune_prompt(subject, want))
    cwd = subject.agent_dir if subject.agent_dir and os.path.isdir(subject.agent_dir) else None
    print(wakes.paint(
        f"Asking your agent for a change {TUNE_WISHES[want][1]} posts like the one on topic {subject.topic_id}.",
        "accent",
    ))
    if want == "how":
        print(wakes.paint("It asks you what to change about its replies.", "meta"))
    print(wakes.paint("It proposes a change and makes it once you agree.", "meta"))
    print(wakes.paint("Leave Claude Code (/exit or Ctrl+D) to come back to the list.\n", "meta"))
    try:
        subprocess.call(command, cwd=cwd)
    except OSError as exc:
        return f"Could not start Claude Code: {exc}"
    return "" if want == "wake" else "r on a wake wakes your agent again for its post, as QUPA.md says now."


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


def set_title(title: str = TITLE) -> None:
    sys.stdout.write(f"\x1b]0;{title}\x07")
    sys.stdout.flush()
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:  # noqa: BLE001 - a title is a convenience
            pass


def bring_to_front(title: str = TITLE) -> bool:
    """Raise the open browser's window. False when it cannot be found, which
    includes macOS, where nothing here can raise another app's window."""
    if os.name == "nt":
        return _raise_on_windows(title)
    if sys.platform != "darwin" and shutil.which("wmctrl"):
        return subprocess.run(["wmctrl", "-a", title], capture_output=True).returncode == 0
    return False


def _raise_on_windows(title: str) -> bool:
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
            if title in text.value:
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
    return f"Relevance threshold set to {floor:.2f}."


# ------------------------------------------------------------------ run


def load_rows(settings: Settings, limit: int | None) -> list[wakes.Row]:
    """The last ``limit`` wakes' rows, or all of them, newest first."""
    # The diary's wakes are the diary browser's.
    return list(reversed(wakes.recent(settings.state_path, limit, wakes.FORUM)))


def load(settings: Settings, limit: int | None) -> list[Wake]:
    return [Wake(row) for row in load_rows(settings, limit)]


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
        agent_dir=settings.agent_dir,
        missed=wakes.near_misses(settings.state_path),
    )
    with Terminal() as terminal:
        while True:
            size = shutil.get_terminal_size((100, 30))
            terminal.draw(browser.frame(size.columns - 1, size.lines))
            try:
                key = read_key(REFRESH_SECONDS)
            except KeyboardInterrupt:
                return 0
            if not key:
                # Nothing pressed: the journal as it stands, unless a question is open.
                if not browser.tuning:
                    refresh(browser, reloaded(browser.items, load_rows(settings, limit)), wakes.status(settings.state_path))
                    browser.missed = wakes.near_misses(settings.state_path)
                    browser.missed_index = min(browser.missed_index, max(len(browser.missed) - 1, 0))
                continue
            action = browser.handle(key, size.lines)
            if action == "quit":
                return 0
            if action in ("raise", "lower"):
                browser.message = set_floor(settings, browser, action)
            if action in MODEL_KEYS.values():
                browser.message = change_model(models.WAKE, action)
            if action == "arxiv":
                browser.message = change_arxiv()
            if action == "rewake":
                browser.message = rewake(settings, browser.selected)
            if action == "ask" or (action or "").startswith("tune-"):
                chosen = browser.selected.row.session if browser.selected else ""
                terminal.suspend()
                if action == "ask":
                    message = ask(browser.selected.row)
                else:
                    message = tune(browser.subject, action.removeprefix("tune-"))
                terminal.resume()
                # Reread: the agent may have been woken again meanwhile.
                browser.items = load(settings, limit)
                browser.missed = wakes.near_misses(settings.state_path)
                browser.state = wakes.status(settings.state_path)
                browser.index = next((i for i, w in enumerate(browser.items) if w.row.session == chosen), 0)
                browser.missed_index = min(browser.missed_index, max(len(browser.missed) - 1, 0))
                browser.viewing = False
                browser.message = message
