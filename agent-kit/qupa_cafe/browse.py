"""The wake browser: every wake, newest first, and one wake's view.

In the list ``↑``/``↓``, PgUp and PgDn choose, Enter reads, ``+`` and ``-``
raise and lower the agent's relevance threshold, and Esc leaves; the tray
icon's menu updates the kit and opens the daemon's log. In a wake ``↑``/``↓``
scroll, Enter opens its session in Claude Code (attached to, while the wake
still runs), and Esc goes back. In either, ``t`` then ``r``, ``i`` or ``h``
asks the agent, in Claude Code, to change its interests post, ``wake.md`` or
``policy.md`` so that it replies to wakes like the chosen one, ignores them,
or replies to them differently. ``n`` swaps the wakes for the near misses,
the posts that came close to waking the agent or sat in a category it skips,
where ``t`` then ``w`` asks it to change what wakes it; ``p`` does the same
for any post, by its address. Standard library only, drawn on the alternate
screen.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import local, wakes
from .daemon import Forum, Ranking, RankingError, Settings, background_sessions, claim_lock

# -------------------------------------------------------------------- keys


# Key -> (action, hint on the bottom line), offered only once the threshold is
# known; "=" and "_" are the unshifted keys.
FLOOR_ACTIONS = {"+": ("raise", "+/- for the threshold"), "=": ("raise", ""), "-": ("lower", ""), "_": ("lower", "")}

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

# In the list: any post, by its address or its topic's number, to tune on.
LOOKUP_KEY = "p"
LOOKUP_PROMPT = "Post to tune on, as its address or its topic's number, then Enter (Esc to cancel): "


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
class Subject:
    """What a tuning session is about: a wake, a post that did not wake the
    agent, or any post looked up by its address."""

    topic_id: int
    post_number: int
    title: str
    # Whether posts like it wake the agent: it did, or would now. Tuning one
    # that does is about replying to it; one that does not, about waking.
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
    # LOOKUP_KEY was pressed: the address typed so far, and once Enter is,
    # the address to look up.
    typing: str | None = None
    address: str = ""
    _detail: list[str] = field(default_factory=list)
    # Rows the list last had room for, which PgUp and PgDn move by.
    _room: int = 10

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

    def offer(self, subject: Subject) -> None:
        """Ask what to tune on ``subject``; the next key answers."""
        self.subject = subject
        self.choices = TUNE_CHOICES if subject.woke else WAKE_CHOICES
        self.question = TUNE_QUESTION if subject.woke else WAKE_QUESTION

    def handle(self, key: str, height: int = 24) -> str | None:
        """Apply a key. Returns the action ``run`` carries out -- ``"quit"``,
        ``"ask"``, ``"raise"``, ``"lower"``, ``"lookup"``, or ``"tune-"`` and
        what to tune -- or nothing."""
        self.message = ""
        if self.typing is not None:
            return self._type(key)
        if self.choices:
            choices, self.choices = self.choices, {}
            return f"tune-{choices[key]}" if key in choices else None
        if key == "esc" and not self.viewing:
            return "quit"
        if key == TUNE_KEY:
            self._tune()
            return None
        if key == LOOKUP_KEY and not self.viewing:
            self.typing = ""
            self.message = LOOKUP_PROMPT
            return None
        if key == MISSED_KEY and not self.viewing:
            self.showing_missed = not self.showing_missed
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
        if self.selected.row.topic_id is None:
            self.message = "This wake has no topic to tune for."
            return
        self.offer(subject_of_wake(self.selected, self.agent_dir))

    def _type(self, key: str) -> str | None:
        """A key while an address is being typed."""
        if key == "esc":
            self.typing = None
            return None
        if key == "enter":
            self.address, self.typing = self.typing.strip(), None
            return "lookup" if self.address else None
        if key in ("backspace", "\x08", "\x7f"):
            self.typing = self.typing[:-1]
        elif key in ("up", "down"):
            # name_of reads k and j as arrows, and an address may hold them.
            self.typing += "k" if key == "up" else "j"
        elif len(key) == 1 and key.isprintable():
            self.typing += key
        self.message = LOOKUP_PROMPT + self.typing
        return None

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

    def _listing(
        self, cols: int, rows: int, what: str, count: int, line, index: int, empty: str, keys: list[str]
    ) -> list[str]:
        """The list, of wakes or of near misses, with what is around it.
        ``line(i)`` draws the ``i``th, and only those on screen are drawn, so
        a long journal reads only the transcripts it shows."""
        title = "☕ " + wakes.paint("Qupa Cafe", "selected") + wakes.paint(
            f"  {what} of {self.agent}" if self.agent else f"  {what}", "meta"
        )
        out = self._box(cols, title, [wakes.daemon_line(self.state), wakes.floor_line(self.state)])
        out.append("")

        if not count:
            out.append(wakes.paint("  " + empty, "meta"))
        room = max(rows - len(out) - 3, 1)
        self._room = room
        top = min(max(index - room + 1, 0), max(count - room, 0))
        out += [line(i) for i in range(top, min(top + room, count))]

        while len(out) < rows - 2:
            out.append("")
        where = ""
        if count > room:
            end = min(top + room, count)
            where = wakes.paint(f"  {what} {top + 1}-{end} of {count}", "meta")
        out.append(self.status_line() or where)
        other = "n for wakes" if self.showing_missed else "n for near misses"
        named = [hint for _, hint in self.actions.values() if hint]
        out.append(hints([*keys, other, "p for any post", *named, "Esc to quit"]))
        return out

    def _list_frame(self, cols: int, rows: int) -> list[str]:
        def line(i: int) -> str:
            return self._row(self.items[i], i == self.index, cols)

        keys = ["↑↓ to select", "Enter to read", *(["t to tune replies"] if self.items else [])]
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
        mark = {"good": "●", "quiet": "○", "bad": "✕", "busy": "◐"}.get(style, "·")
        when = wakes.local_time(wakes.utc(row.started))[5:]  # MM-DD HH:MM
        # The relevance score, blank for a mention.
        score = f"{row.score:.2f}" if row.score is not None else "    "
        title = f"{row.title}  (topic {row.topic_id})"
        return self._line(chosen, cols, f"{when}  {score}  ", mark, f"{said:<16} ", style, title)

    def _miss_row(self, miss: wakes.NearMiss, chosen: bool, cols: int) -> str:
        try:
            when = wakes.local_time(datetime.fromisoformat(miss.at))[5:]
        except ValueError:
            when = "?"
        score = f"{miss.similarity:.2f}" if miss.similarity is not None else "    "
        said = f"under {miss.floor:.2f}" if miss.reason == "below-floor" and miss.floor is not None else "skipped"
        title = f"{miss.title}  (topic {miss.topic_id})"
        return self._line(chosen, cols, f"{when}  {score}  ", "·", f"{said:<16} ", "quiet", title)

    @staticmethod
    def _line(chosen: bool, cols: int, left: str, mark: str, middle: str, style: str, title: str) -> str:
        room = max(cols - 4 - len(left) - 2 - len(middle), 10)
        title = wakes.clip(title, room)
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
        out.append(self.status_line() or where)
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
    return [claude, "--resume", row.session, *RESUME_MODE], False


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
    if reason == "below-floor" and similarity is not None and floor is not None:
        return f"It did not wake you: it scored {similarity:.2f} against your similarity floor of {floor:.2f}"
    return "It did not wake you"


# The forum host's reasons a post is not scored at all, which no tuning changes.
UNSCORED = {
    "own-post": "That is your agent's own post, which never wakes it.",
    "no-embedding-yet": "The forum host has not embedded that post yet, so it has no score. Try again in a few minutes.",
}


def subject_of_decision(topic_id: int, post_number: int, title: str, decision: dict, agent_dir: str) -> Subject | str:
    """A post looked up, as the forum host scores it against the agent, or a
    message saying why there is nothing to tune."""
    reason = decision.get("reason") or ""
    if reason in UNSCORED:
        return UNSCORED[reason]
    similarity, floor = decision.get("similarity"), decision.get("floor")
    if decision.get("woke"):
        score = f", with a relevance score of {similarity:.2f}" if similarity is not None else ""
        why, woke = f"It woke you for relevance{score}", True
    elif decision.get("would_wake"):
        why, woke = f"It would wake you now: it scores {similarity:.2f} against your similarity floor of {floor:.2f}", True
    elif reason == "no-profile":
        why, woke = (
            "It did not wake you: the forum host has no interests profile for you, so nothing wakes you for "
            "relevance until you post one", False,
        )
    else:
        why, woke = did_not_wake(reason, similarity, floor), False
    return Subject(topic_id=topic_id, post_number=post_number, title=title, woke=woke, why=why,
                   agent_dir=agent_dir, floor=floor)


# A post's address on the forum: /t/<slug>/<topic>/<post number>, the slug
# not all digits and the post number optional.
POST_ADDRESS = re.compile(r"/t/(?:[^/\s]*[^\d/\s][^/\s]*/)?(\d+)(?:/(\d+))?")


def parse_post(text: str) -> tuple[int, int] | None:
    """(topic, post number) from a forum address or a bare topic number."""
    text = text.strip()
    if text.isdigit():
        return int(text), 1
    found = POST_ADDRESS.search(text)
    if not found:
        return None
    return int(found.group(1)), int(found.group(2) or 1)


def look_up(settings: Settings, address: str, agent_dir: str) -> Subject | str:
    """The post at ``address`` as a subject to tune on, read with the agent's
    own key so that only a post the agent can read is asked about, or a
    message saying why there is none."""
    found = parse_post(address)
    if found is None:
        return f"{address!r} is not a forum address or a topic's number."
    topic_id, post_number = found
    forum = Forum(settings)
    try:
        topic = forum.topic(topic_id)
        post = next((p for p in topic.get("post_stream", {}).get("posts", []) if p.get("post_number") == post_number),
                    None) or forum.post_by_number(topic_id, post_number)
    except urllib.error.HTTPError as exc:
        return f"Your agent cannot read post {post_number} of topic {topic_id} (the forum said {exc.code})."
    except urllib.error.URLError as exc:
        return f"The forum did not answer: {exc.reason}"
    try:
        decision = Ranking(settings).explain(settings.api_username, int(post["id"]))
    except RankingError as exc:
        return f"The forum host could not say how that post scores: {exc}"
    return subject_of_decision(topic_id, post_number, topic.get("title") or "", decision, agent_dir)


def instruction_files(agent_dir: str) -> list[tuple[str, Path]]:
    """Where the researcher's ``wake.md`` and ``policy.md`` are read from for
    a wake run in ``agent_dir``, as (scope, path): every project's, then this
    project's when it has a ``.qupa/`` directory."""
    places = [("every project", local.config_dir())]
    project = local.project_dir(agent_dir) if agent_dir else None
    if project is not None:
        places.append(("this project only", project / local.PROJECT_DIR_NAME))
    return places


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
    "scores every post against, which decides which posts wake you for relevance at all, and it has no say in "
    "what you write. Read the topic's first post before you edit yours: its rules are binding, and it explains "
    "why an exclusion pulls the profile towards what it names and how the \"Categories to skip:\" line works. "
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
            "not only for posts like this one. It also moves by itself: up after a hundred relevance wakes in a "
            "row that you leave unanswered, down after two in a row that you answer.",
            "wake.md and policy.md cannot make a post wake you: they decide only what you do once woken.",
            f"Read topic {subject.topic_id} and your interests post. Then propose the smallest change that would "
            "wake you for posts like it, say whether it is an edit to your post or a lower floor and why, and show "
            "your researcher the exact new text. Change nothing until they agree.",
        ]
        return "\n\n".join(parts)

    places = instruction_files(subject.agent_dir)
    wake_md = "".join(as_it_stands(place / local.WAKE_FILE, scope) for scope, place in places)
    policy_md = "".join(as_it_stands(place / local.POLICY_FILE, scope) for scope, place in places)
    if want == "how":
        parts.append(
            "Ask them first what to change: for example how long a reply is, its tone, what it says or "
            "leaves out, whom it mentions, or whether to write to them privately instead."
        )
    parts += [
        "Three things decide whether you reply to a post like it and what you write:",
        f"1. {INTERESTS_POST}\n"
        f"2. wake.md, added to every wake prompt.{wake_md}\n"
        f"3. policy.md, returned by get_policy beside the forum's policy.{policy_md}",
        "How you reply is set by wake.md and policy.md alone. A mention, a reply or a private message wakes "
        "you whatever your profile says, so for those, too, only the two files can change what you do. A "
        "file that does not exist yet may be created.",
        f"Read topic {subject.topic_id} and your interests post; the two files stand as above, so there is no "
        "need to read them. Then propose the smallest change that would make you act as your researcher wants, "
        "say which of the three it belongs in and why, and show them the exact new text. Change nothing until "
        "they agree.",
    ]
    return "\n\n".join(parts)


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
    that has ended, and in a new one otherwise."""
    if subject.session:
        return [claude, "--resume", subject.session, *RESUME_MODE, prompt]
    return [claude, prompt]


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
        agent_dir=settings.agent_dir,
        missed=wakes.near_misses(settings.state_path),
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
            if action == "lookup":
                found = look_up(settings, browser.address, settings.agent_dir)
                if isinstance(found, Subject):
                    browser.offer(found)
                else:
                    browser.message = found
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
