"""The lending browser: whom your agent is lent to, and its lent tasks.

It lists the forum's other researchers who have agents, each with a tick,
the researchers your agent is lent to now ticked. ``↑``/``↓`` choose, Space
ticks or unticks, ``l`` lends your agent to the researchers ticked, and
``s`` twice stops lending it. ``+`` and ``-`` move the share of your plan's
usage it is lent up to, ``b`` steps what lent tasks may spend in five hours,
``n`` writes the note the borrowers' sessions see, ``t`` lists its last lent
tasks, and Esc leaves. The first ``l`` has Claude Code ask, in this
terminal, whether to trust the folder lent tasks run in. Drawn as the wake
browser is. The tray icon's **Lend your agent…** opens it; ``--print`` or a
pipe prints the offer and the tasks instead.

    python -m qupa_cafe.lending
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import browse, lend, quota, wakes
from .daemon import Settings, claim_lock, load_env_file

TITLE = "Qupa Cafe: lending"

TICK_KEYS = (" ", "space")
LEND_KEY = "l"
STOP_KEY = "s"
BUDGET_KEY = "b"
NOTE_KEY = "n"
TASKS_KEY = "t"
# The ceiling in steps of ten, as the wake browser moves the threshold; "="
# and "_" are the unshifted keys.
CEILING_KEYS = {"+": 10, "=": 10, "-": -10, "_": -10}
# How often the forum host is asked again while no key is pressed.
ASK_SECONDS = 30


@dataclass
class LendBrowser:
    """What is on the screen and what a key does to it, with no terminal or
    forum host in here, so that it can be driven, and tested, a key at a time."""

    agent: str
    researchers: list[dict] = field(default_factory=list)
    offer: dict | None = None
    tasks: list[dict] = field(default_factory=list)
    ticked: set[str] = field(default_factory=set)
    note: str = ""
    index: int = 0
    viewing: bool = False
    scroll: int = 0
    message: str = ""
    # Set by the first s, so that the second stops lending.
    stopping: bool = False
    _detail: list[str] = field(default_factory=list)
    _room: int = 10
    _top: int = 0
    _folded: tuple = ()
    _page: int = 0

    def show(self, status: dict, researchers: list[dict]) -> None:
        """What the forum host says now, keeping the ticks the researcher
        changed and has not lent to yet."""
        before = {n.lower() for n in (self.offer or {}).get("to") or []}
        self.offer = status.get("offer")
        self.tasks = list(status.get("tasks") or [])
        lent = {n.lower() for n in (self.offer or {}).get("to") or []}
        if not self.researchers or self.ticked == before:
            self.ticked = set(lent)
        if self.offer and not self.note:
            self.note = self.offer.get("note") or ""
        self.researchers = researchers
        self.index = min(self.index, max(len(researchers) - 1, 0))
        if self.viewing:
            self._detail = self.task_lines()

    @property
    def chosen(self) -> dict | None:
        return self.researchers[self.index] if self.researchers else None

    def handle(self, key: str, height: int = 24) -> str | None:
        """Apply a key. Returns the action ``run`` carries out -- ``"quit"``,
        ``"lend"``, ``"stop"``, ``"budget"``, ``"note"`` or ``"ceiling:"`` and
        how far to move it -- or nothing."""
        self.message = ""
        stopping, self.stopping = self.stopping, False
        if self.viewing:
            if key == TASKS_KEY:
                self.viewing = False
            else:
                browse.scroll(self, key, height)
            return None
        if key == "esc":
            return "quit"
        if key == STOP_KEY:
            if not self.offer:
                self.message = f"{self.agent} is not lent now."
                return None
            if stopping:
                return "stop"
            self.stopping = True
            self.message = f"Press s again to stop lending {self.agent}. Tasks not yet started are refused."
            return None
        if key == LEND_KEY:
            return "lend"
        if key == BUDGET_KEY:
            return "budget"
        if key == NOTE_KEY:
            return "note"
        if key in CEILING_KEYS:
            return f"ceiling:{CEILING_KEYS[key]}"
        if key == TASKS_KEY:
            self.viewing = True
            self.scroll = 0
            self._detail = self.task_lines()
            return None
        if not self.researchers:
            return None
        moves = {"up": -1, "down": 1, "pgup": -self._room, "pgdn": self._room,
                 "home": -len(self.researchers), "end": len(self.researchers)}
        if key in moves:
            self.index = min(max(self.index + moves[key], 0), len(self.researchers) - 1)
        elif key in (*TICK_KEYS, "enter"):
            name = self.researchers[self.index]["username"].lower()
            self.ticked ^= {name}
        return None

    def chosen_names(self) -> list[str]:
        """The researchers ticked, as the forum spells them, in the list's order."""
        return [r["username"] for r in self.researchers if r["username"].lower() in self.ticked]

    # ------------------------------------------------------------ frames

    def header_lines(self) -> list[str]:
        if self.offer:
            lent = wakes.paint("● ", "good") + f"Lent to {', '.join(self.offer['to'])}"
        else:
            lent = wakes.paint("○ ", "meta") + "Not lent" + wakes.paint("  (tick researchers, then l to lend)", "meta")
        changed = {n.lower() for n in (self.offer or {}).get("to") or []} != self.ticked
        ceiling, budget = lend.ceiling(), lend.budget()
        lines = [
            lent + (wakes.paint("  (the ticks differ: l to lend to those ticked)", "accent") if changed else ""),
            f"  Only while your plan's usage is below {ceiling + '%' if ceiling else 'any level'}"
            + wakes.paint("  (+ to raise, - to lower)", "meta"),
            f"  Lent tasks may spend {budget or 'any amount'} in five hours" + wakes.paint("  (b to step it)", "meta"),
            f"  Note: {self.note or '(none)'}" + wakes.paint("  (n to write it)", "meta"),
            wakes.paint("  A task runs on this machine and your plan, in a sandbox that sees none of your files.",
                        "meta"),
        ]
        held = lend.held_back()
        if held and self.offer:
            lines.append(wakes.paint(f"  No lent task starts now: {held}.", "bad"))
        return lines

    def status_line(self) -> str:
        return self.message and wakes.paint("  " + self.message, "yours")

    def frame(self, cols: int, rows: int) -> list[str]:
        return self._tasks_frame(cols, rows) if self.viewing else self._list_frame(cols, rows)

    def _list_frame(self, cols: int, rows: int) -> list[str]:
        title = "☕ " + wakes.paint("Qupa Cafe", "selected") + wakes.paint(f"  lending {self.agent}", "meta")
        header = browse.box(cols, title, self.header_lines())
        keys = ["↑↓ to select", "Space to tick", "l to lend to those ticked", "s to stop lending",
                "t for its tasks", "Esc to quit"]
        empty = "No other researcher on the forum has an agent yet, or the forum host could not be asked."
        out, self._room, self._top = browse.listing(
            cols, rows, header, "researchers", len(self.researchers), lambda i: self._row(i, cols),
            self.index, empty, self.status_line(), keys, self._top,
        )
        return out

    def _row(self, i: int, cols: int) -> str:
        person = self.researchers[i]
        chosen = i == self.index
        ticked = person["username"].lower() in self.ticked
        name = person.get("name") or ""
        pointer = wakes.paint("❯ ", "accent") if chosen else "  "
        box = wakes.paint("[x] " if ticked else "[ ] ", "good" if ticked else "meta")
        shown = wakes.clip(name if name and name != person["username"] else person["username"], max(cols - 40, 10))
        return ("  " + pointer + box + wakes.paint(shown, "selected" if chosen else "prompt")
                + wakes.paint(f"  {person['username']}" if shown != person["username"] else "", "meta"))

    def task_lines(self) -> list[str]:
        if not self.tasks:
            return [wakes.paint(f"  {self.agent} has had no lent task yet.", "meta")]
        out = [wakes.paint(f"  {self.agent}'s last lent tasks, newest first, in UTC:", "meta"), ""]
        for task in self.tasks:
            style = {"done": "good", "failed": "bad"}.get(task["state"], "busy")
            when = task["created_at"][:16].replace("T", " ")
            out.append(f"  {when}  " + wakes.paint(f"{task['state']:<8}", style)
                       + f"from {task['borrower'].removesuffix('-agent')}")
            if task.get("reason"):
                out.append(wakes.paint(f"      {task['reason']}", "meta"))
        return out

    def _tasks_frame(self, cols: int, rows: int) -> list[str]:
        return browse.detail_frame(self, cols, rows, ["↑↓ to scroll", "t or Esc to go back"])


# ------------------------------------------------------------- actions


def step_ceiling(by: int) -> str:
    """Move the ceiling by ``by`` points, within 10% to 100%; none counts as 100%."""
    now = round(int(lend.ceiling() or 100) / 10) * 10
    moved = min(max(now + by, 10), 100)
    quota.write(str(moved), key=lend.CEILING_KEY)
    return f"Lent tasks start only while your plan's usage is below {moved}%."


def step_budget() -> str:
    budget = lend.budget()
    steps = quota.BUDGETS
    budget = steps[(steps.index(budget) + 1) % len(steps)] if budget in steps else steps[0]
    quota.write(budget, key=lend.BUDGET_KEY)
    return (f"Lent tasks may spend {budget} input-token equivalents in five hours." if budget
            else "Lent tasks may spend any amount: only the ceiling limits them.")


def ask_host(browser: LendBrowser, client: lend.Client) -> None:
    try:
        browser.show(client.status(), client.researchers())
    except lend.LendError as exc:
        browser.message = str(exc)


def lock_path(settings: Settings) -> Path:
    return Path(settings.state_path).with_name("lending.lock")


def already_open(settings: Settings) -> bool:
    lock = claim_lock(lock_path(settings))
    if lock:
        lock.close()
    return lock is False


def run(settings: Settings) -> int:
    lock = claim_lock(lock_path(settings))
    if lock is False:
        if not browse.bring_to_front(TITLE):
            print(f"The lending browser is already open, in a window titled {TITLE!r}.")
        return 0
    client = lend.Client(settings)
    browser = LendBrowser(client.agent)
    ask_host(browser, client)
    asked = time.monotonic()
    with browse.Terminal(TITLE) as terminal:
        while True:
            size = shutil.get_terminal_size((100, 30))
            terminal.draw(browser.frame(size.columns - 1, size.lines))
            try:
                key = browse.read_key(browse.REFRESH_SECONDS)
            except KeyboardInterrupt:
                return 0
            if not key:
                if time.monotonic() - asked > ASK_SECONDS:
                    ask_host(browser, client)
                    asked = time.monotonic()
                continue
            action = browser.handle(key, size.lines)
            if action == "quit":
                return 0
            if (action or "").startswith("ceiling:"):
                browser.message = step_ceiling(int(action.removeprefix("ceiling:")))
            elif action == "budget":
                browser.message = step_budget()
            elif action == "note":
                terminal.suspend()
                try:
                    said = input(f"The note borrowers' sessions see, such as what {client.agent} is good at,\n"
                                 f"or Enter to keep \"{browser.note}\": ").strip()
                except EOFError:
                    said = ""
                terminal.resume()
                if said:
                    browser.note = said[:300]
                    browser.message = "Press l to lend with the new note." if browser.offer else "The note is kept for when you lend."
            elif action == "lend":
                chosen = browser.chosen_names()
                if not chosen:
                    browser.message = "Tick at least one researcher with Space first."
                    continue
                if not lend.trusted():
                    terminal.suspend()
                    trusted = lend.ensure_trusted()
                    terminal.resume()
                    if not trusted:
                        browser.message = "Nothing was lent: lent tasks need a folder Claude Code trusts."
                        continue
                try:
                    made = client.offer(chosen, browser.note)
                except lend.LendError as exc:
                    browser.message = str(exc)
                    continue
                ask_host(browser, client)
                browser.message = (f"Lent to {', '.join(made['to'])}. Their sessions get {client.agent} as a "
                                   "subagent within a minute or two.")
            elif action == "stop":
                try:
                    failed = client.withdraw()
                except lend.LendError as exc:
                    browser.message = str(exc)
                    continue
                browser.ticked = set()
                ask_host(browser, client)
                browser.message = f"{client.agent} is no longer lent." + (
                    f" {failed} queued task(s) were refused." if failed else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n")[0])
    parser.add_argument("--print", action="store_true", help="print the offer and the tasks rather than browse them")
    parser.add_argument("--colour", "--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--columns", type=int, help="make the window this many columns wide before browsing")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    wakes.set_colour(wakes.colour_wanted(args.colour, sys.stdout))
    load_env_file()
    settings = Settings()
    if not args.print and sys.stdin.isatty() and sys.stdout.isatty():
        if args.columns:
            browse.set_width(args.columns)
        return run(settings)
    return lend.main(["status"])


if __name__ == "__main__":
    # The package's copy, as in qupa_cafe.wakes, so its colour switch is the one set.
    from qupa_cafe.lending import main as package_main

    sys.exit(package_main())
