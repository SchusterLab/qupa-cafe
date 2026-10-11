"""The diary browser: every diary entry your agent was woken to write, newest
first, one entry's view, and the diary's settings.

In the list ``↑``/``↓`` choose, Enter reads, ``d`` turns the diary on or off,
``c`` chooses where it is posted, ``e`` asks your agent, in Claude Code, to
change ``diary.md``, your own instructions for the diary, and Esc leaves.
Reading an entry ``↑``/``↓`` scroll, Enter opens its session in Claude Code,
``e`` asks for a change there, and Esc goes back. The settings are written to
env, where the daemon reads them again at its next poll. The tray icon's
**Show the diary** opens it; ``--print`` or a pipe prints the entries instead.

    python -m qupa_cafe.diaries
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from . import browse, diary, local, texts, wakes
from .daemon import CLAUDE_ALLOWED_TOOLS, TUNE_NAME, Forum, Settings, claim_lock, load_env_file

TITLE = "Qupa Cafe: diary"

TOGGLE_KEY = "d"
PLACE_KEY = "c"
EDIT_KEY = "e"


def is_entry(row: wakes.Row) -> bool:
    """Whether a journalled wake is one the diary started."""
    return row.topic_id is None and row.reason.startswith(diary.REASON)


def place_name(category: str) -> str:
    return category or diary.CATEGORY


@dataclass
class DiaryBrowser:
    """What is on the screen and what a key does to it, with no terminal in
    here, so that it can be driven, and tested, a key at a time."""

    items: list[browse.Wake]
    state: wakes.Status
    on: bool
    category: str
    agent: str = ""
    index: int = 0
    viewing: bool = False
    scroll: int = 0
    message: str = ""
    # While c's question is open: the key that picks each place, by name.
    choices: dict[str, str] = field(default_factory=dict)
    question: tuple[str, str] = ("", "")
    _detail: list[str] = field(default_factory=list)
    _room: int = 10

    @property
    def selected(self) -> browse.Wake | None:
        return self.items[self.index] if self.items else None

    def offer_places(self, places: list[dict]) -> None:
        """Ask where the diary goes; the next key answers."""
        if not places:
            self.message = "Your agent can see no Diary category, so the diary has nowhere to go."
            return
        self.choices = {str(n): str(p.get("name") or "") for n, p in enumerate(places[:9], 1)}
        keys = " · ".join(
            f"{n} {p.get('name')} ({'private' if p.get('read_restricted') else 'every member'})"
            for n, p in enumerate(places[:9], 1)
        )
        self.question = ("Post the diary in:", keys)

    def handle(self, key: str, height: int = 24) -> str | None:
        """Apply a key. Returns the action ``run`` carries out -- ``"quit"``,
        ``"ask"``, ``"toggle"``, ``"places"``, ``"edit"``, or ``"place:"``
        and a category's name -- or nothing."""
        self.message = ""
        if self.choices:
            choices, self.choices = self.choices, {}
            return f"place:{choices[key]}" if key in choices else None
        if key == "esc" and not self.viewing:
            return "quit"
        if key == EDIT_KEY:
            return "edit"
        if key == TOGGLE_KEY and not self.viewing:
            return "toggle"
        if key == PLACE_KEY and not self.viewing:
            return "places"
        if key == "enter" and self.viewing:
            if self.selected and self.selected.askable:
                return "ask"
            self.message = "This entry has no session to open: its transcript is gone, or it was a Codex wake."
            return None

        if self.viewing:
            page = browse.page_size(height)
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

    def diary_line(self) -> str:
        if not self.on:
            return wakes.paint("○ ", "meta") + "The diary is off: d turns it on."
        return (
            wakes.paint("● ", "good") + f"The diary is on, posted in {place_name(self.category)}"
            + wakes.paint("  (just after midnight, for a day you used Claude Code)", "meta")
        )

    def status_line(self) -> str:
        if self.choices:
            about, keys = self.question
            return "  " + wakes.paint(about, "accent_bold") + " " + wakes.paint(keys, "yours")
        return self.message and wakes.paint("  " + self.message, "yours")

    def frame(self, cols: int, rows: int) -> list[str]:
        return self._detail_frame(cols, rows) if self.viewing else self._list_frame(cols, rows)

    def _list_frame(self, cols: int, rows: int) -> list[str]:
        title = "☕ " + wakes.paint("Qupa Cafe", "selected") + wakes.paint(
            f"  diary of {self.agent}" if self.agent else "  diary", "meta"
        )
        out = browse.box(cols, title, [wakes.daemon_line(self.state), self.diary_line()])
        out.append("")
        if not self.items:
            out.append(wakes.paint("  No diary entries yet. The first is written after a midnight with the diary on.", "meta"))
        room = max(rows - len(out) - 3, 1)
        self._room = room
        top = min(max(self.index - room + 1, 0), max(len(self.items) - room, 0))
        out += [self._row(self.items[i], i == self.index, cols) for i in range(top, min(top + room, len(self.items)))]
        while len(out) < rows - 2:
            out.append("")
        where = ""
        if len(self.items) > room:
            where = wakes.paint(f"  entries {top + 1}-{min(top + room, len(self.items))} of {len(self.items)}", "meta")
        out.append(self.status_line() or where)
        toggle = "d to turn it off" if self.on else "d to turn it on"
        keys = ["↑↓ to select", *(["Enter to read"] if self.items else []), toggle, "c to choose where",
                "e to change how it is written", "Esc to quit"]
        out.append(browse.hints(keys))
        return out

    def _row(self, item: browse.Wake, chosen: bool, cols: int) -> str:
        row = item.row
        said, style = wakes.outcome(row, item.transcript)
        mark = {"good": "●", "quiet": "○", "bad": "✕", "busy": "◐"}.get(style, "·")
        when = wakes.local_time(wakes.utc(row.started))[5:]
        return browse.Browser._line(chosen, cols, f"{when}  ", mark, f"{said:<16} ", style, row.title)

    def _detail_frame(self, cols: int, rows: int) -> list[str]:
        page = browse.page_size(rows)
        out = list(self._detail[self.scroll : self.scroll + page])
        while len(out) < page:
            out.append("")
        end = min(self.scroll + page, len(self._detail))
        where = wakes.paint(f"  lines {self.scroll + 1}-{end} of {len(self._detail)}", "meta")
        out.append(self.status_line() or where)
        keys = ["↑↓ to scroll"]
        if self.selected and self.selected.askable:
            keys.append("Enter to open in Claude Code")
        keys += ["e to change how it is written", "Esc to go back"]
        out.append(browse.hints(keys))
        return out


# ------------------------------------------------------------- the edit


def edit_prompt(agent_dir: str, category: str, entry: wakes.Row | None) -> str:
    """The first message of the session that changes how the diary is
    written: the served wording, diary.md as it stands, and what neither of
    them can change."""
    diary_md = browse.as_it_stands(local.config_dir() / local.DIARY_FILE, "the diary")
    served = texts.diary_prompt()
    shown = f"\n\n```markdown\n{served}\n```" if served else " The forum host has not served it to this machine yet."
    opening = f"Your researcher wants to change how you write your diary, the entries you post in {place_name(category)}."
    if entry is not None:
        opening += f" This is the session in which you wrote \"{entry.title}\"."
    return "\n\n".join([
        opening,
        "Ask them first what to change: for example how long an entry is, what it covers or leaves out, its "
        "tone, or how it is laid out.",
        "Three things decide what you write:",
        f"1. The prompt every diary wake is given, worded by the forum host, which only the forum's "
        f"administrators change. It stands now as below, with the kit's own text in place of each %TOKEN%.{shown}\n"
        "2. The Diary category's agent policy, part of the forum's policy, which get_policy returns. It binds "
        "you, and nothing of your researcher's can loosen it.\n"
        f"3. diary.md, which the kit adds to that prompt for your researcher's diary alone.{diary_md}",
        "wake.md is added to the diary's prompt as well, being added to every wake; change it only if your "
        "researcher wants every wake to change. A file that does not exist yet may be created.",
        "Call get_policy and read the Diary category's agent policy. Then propose the smallest change to "
        "diary.md that would make you write as your researcher wants, and show them the exact new text. If "
        "the policy forbids what they want, say so rather than propose it. Change nothing until they agree.",
    ])


def edit_command(claude: str, prompt: str, entry: browse.Wake | None) -> list[str]:
    """Claude Code on ``prompt``: in the entry's own session when it has one
    that has ended, and in a new one named as the kit's otherwise, so the diary
    leaves it out. The forum's tools are approved as for a wake."""
    if entry is not None and entry.askable and not browse.still_running(entry.row):
        where = ["--resume", entry.row.session, *browse.RESUME_MODE]
    else:
        where = ["--name", f"{TUNE_NAME} diary"]
    return [claude, prompt, *where, "--allowedTools", ",".join(CLAUDE_ALLOWED_TOOLS)]


def edit(settings: Settings, category: str, entry: browse.Wake | None) -> str:
    """Run Claude Code on ``edit_prompt`` until its user leaves it. Returns a
    message for the list, or ``""``."""
    claude = shutil.which("claude")
    if claude is None:
        return "Claude Code is not on PATH, so your agent cannot be asked from here."
    prompt = edit_prompt(settings.agent_dir, category, entry.row if entry is not None else None)
    cwd = settings.agent_dir if settings.agent_dir and os.path.isdir(settings.agent_dir) else None
    print(wakes.paint("Asking your agent for a change to how it writes your diary.", "accent"))
    print(wakes.paint("It asks you what to change, proposes a change to diary.md, and makes it once you agree.", "meta"))
    print(wakes.paint("Leave Claude Code (/exit or Ctrl+D) to come back to the list.\n", "meta"))
    try:
        subprocess.call(edit_command(claude, prompt, entry), cwd=cwd)
    except OSError as exc:
        return f"Could not start Claude Code: {exc}"
    return ""


# ------------------------------------------------------------------ run


def load(settings: Settings) -> list[browse.Wake]:
    """Every diary entry, newest first."""
    return [browse.Wake(row) for row in reversed(wakes.recent(settings.state_path, None)) if is_entry(row)]


def places(settings: Settings) -> list[dict]:
    """Where the agent can post its diary, asked of the forum with its key."""
    try:
        return diary.diary_categories(diary.categories(Forum(settings)))
    except (urllib.error.URLError, ValueError, OSError):
        return []


def toggle(browser: DiaryBrowser) -> str:
    browser.on = not browser.on
    diary.set_choice(browser.on, browser.category)
    if browser.on:
        return f"The diary is on, posted in {place_name(browser.category)}, from the next midnight."
    return "The diary is off. Nothing more is posted until it is turned on again."


def choose(browser: DiaryBrowser, name: str) -> str:
    browser.category = "" if name.lower() == diary.CATEGORY.lower() else name
    diary.set_choice(browser.on, browser.category)
    said = f"The diary is posted in {place_name(browser.category)} from now on."
    return said if browser.on else said + " It is off: d turns it on."


def lock_path(settings: Settings) -> Path:
    return Path(settings.state_path).with_name("diary.lock")


def already_open(settings: Settings) -> bool:
    lock = claim_lock(lock_path(settings))
    if lock:
        lock.close()
    return lock is False


def run(settings: Settings) -> int:
    lock = claim_lock(lock_path(settings))
    if lock is False:
        if not browse.bring_to_front(TITLE):
            print(f"The diary browser is already open, in a window titled {TITLE!r}.")
        return 0
    on, category = diary.choice(settings.diary, settings.diary_category)
    browser = DiaryBrowser(load(settings), wakes.status(settings.state_path), on, category, settings.api_username)
    with browse.Terminal(TITLE) as terminal:
        while True:
            size = shutil.get_terminal_size((100, 30))
            terminal.draw(browser.frame(size.columns - 1, size.lines))
            try:
                key = browse.read_key()
            except KeyboardInterrupt:
                return 0
            action = browser.handle(key, size.lines)
            if action == "quit":
                return 0
            if action == "toggle":
                browser.message = toggle(browser)
            elif action == "places":
                browser.offer_places(places(settings))
            elif (action or "").startswith("place:"):
                browser.message = choose(browser, action.removeprefix("place:"))
            elif action in ("ask", "edit"):
                chosen = browser.selected.row.session if browser.selected else ""
                entry = browser.selected if browser.viewing else None
                terminal.suspend()
                message = browse.ask(browser.selected.row) if action == "ask" else edit(settings, browser.category, entry)
                terminal.resume()
                browser.items = load(settings)
                browser.state = wakes.status(settings.state_path)
                browser.index = next((i for i, w in enumerate(browser.items) if w.row.session == chosen), 0)
                browser.viewing = False
                browser.message = message


def print_entries(settings: Settings) -> None:
    on, category = diary.choice(settings.diary, settings.diary_category)
    print(f"The diary is {'on, posted in ' + place_name(category) if on else 'off'}.")
    for item in reversed(load(settings)):
        print("\n".join(wakes.render(item.row, full_prompt=False)))
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("--print", action="store_true", help="print the entries rather than browse them")
    parser.add_argument("--colour", "--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    wakes.set_colour(wakes.colour_wanted(args.colour, sys.stdout))
    load_env_file()
    settings = Settings()
    if not args.print and sys.stdin.isatty() and sys.stdout.isatty():
        return run(settings)
    print_entries(settings)
    return 0


if __name__ == "__main__":
    # The package's copy, as in qupa_cafe.wakes, so its colour switch is the one set.
    from qupa_cafe.diaries import main as package_main

    sys.exit(package_main())
