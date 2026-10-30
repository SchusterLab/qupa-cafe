"""A cap on the share of the researcher's Claude plan that wakes may reach.

``QUPA_QUOTA_CAP`` in env, which ``qupa-install`` asks for, the wake
browser's ``c`` steps, and the daemon reads again before every wake, so a
change needs no restart: a percentage from 1 to 100, or empty for none. No
wake starts while the plan's five-hour or weekly usage is at or above it,
and a wake running when a reading shows it reached is stopped.

The usage is the plan's, the researcher's own sessions and the agent's
wakes together, as Claude Code shows it in ``/usage``. Claude Code gives it
only to a status line command, and only on a Pro or Max plan, so every wake
is given this module as its status line, which records each reading in
``quota.json`` beside the daemon's state. A reading is as recent as the last
wake's; a window whose reset time has passed counts as unused until the next
reading. With no reading, as on an API key, the cap holds nothing back.

``QUPA_WAKE_BUDGET``, which ``qupa-install`` asks for too and the browser's
``b`` steps, is the agent's alone: what its wakes may spend in any five
hours, counted from their own transcripts as the wake browser counts a
wake's cost. It holds wakes back and stops a running one the same way,
until enough of that spending is five hours old.

    python -m qupa_cafe.quota       # a status line: reads Claude Code's JSON on stdin
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    # Run as a script, which is how Claude Code runs a status line: the
    # package's parent, so that this is the same qupa_cafe the daemon imported.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from qupa_cafe import diary

KEY = "QUPA_QUOTA_CAP"

# What the wake browser's key steps through, in order; empty is no cap.
CAPS = ("", "50", "70", "80", "90", "95")
# What qupa-install offers first.
SUGGESTED = "80"

# The windows Claude Code reports, and their names for a person.
WINDOWS = {"five_hour": "five-hour", "seven_day": "weekly"}

# The budget: what the agent's wakes may spend in any five hours, in
# input-token equivalents as the wake browser counts a wake's cost
# (qupa_cafe.wakes.equivalent), such as 500k or 1M; empty for none.
BUDGET_KEY = "QUPA_WAKE_BUDGET"
BUDGETS = ("", "250k", "500k", "1M", "2M", "5M")
SUGGESTED_BUDGET = "1M"
BUDGET_SECONDS = 5 * 3600
UNITS = {"": 1, "k": 1_000, "m": 1_000_000}


def cap_of(value: str) -> str:
    """A cap as env holds it: a whole percentage from 1 to 100, or empty."""
    value = (value or "").strip().rstrip("%")
    try:
        number = int(value)
    except ValueError:
        return ""
    return str(number) if 1 <= number <= 100 else ""


def tokens_of(value: str) -> int:
    """A budget's number of input-token equivalents, as 500k, 1M or 2000000
    give it; 0 for none or for anything else."""
    value = (value or "").strip().replace(",", "").replace("_", "")
    unit = value[-1:].lower() if value[-1:].lower() in ("k", "m") else ""
    try:
        number = float(value[:-1] if unit else value)
    except ValueError:
        return 0
    return int(number * UNITS[unit]) if number > 0 else 0


def budget_of(value: str) -> str:
    """A budget as env holds it: 500k, 1M or a plain count, or empty."""
    tokens = tokens_of(value)
    if not tokens:
        return ""
    for unit, size in (("M", 1_000_000), ("k", 1_000)):
        if tokens % size == 0:
            return f"{tokens // size}{unit}"
    return str(tokens)


def read(path: Path | None = None, key: str = KEY) -> str:
    """The cap, or with ``key`` BUDGET_KEY the budget, as env says now, else
    as this process's environment does, else none."""
    value = os.environ.get(key, "")
    try:
        lines = (path or diary.env_file()).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        name, sep, found = line.strip().partition("=")
        if sep and name.strip() == key:
            value = found
    return budget_of(value) if key == BUDGET_KEY else cap_of(value)


def write(cap: str, path: Path | None = None, key: str = KEY) -> None:
    """Write the cap, or with ``key`` BUDGET_KEY the budget, into env, in
    place, leaving every other line be."""
    path = path or diary.env_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept, done = [], False
    for line in lines:
        if line.partition("=")[0].strip() == key:
            kept.append(f"{key}={cap}")
            done = True
        else:
            kept.append(line)
    if not done:
        kept.append(f"{key}={cap}")
    # newline="" so Windows does not put carriage returns into the file.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(kept) + "\n")


def step() -> str:
    """Step the cap to the next of CAPS, and return it; one not among them
    steps to the first."""
    cap = read()
    cap = CAPS[(CAPS.index(cap) + 1) % len(CAPS)] if cap in CAPS else CAPS[0]
    write(cap)
    return cap


def describe(cap: str) -> str:
    return f"wakes stop at {cap}% of your Claude plan's five-hour or weekly usage" if cap else "no cap on wakes' Claude usage"


def step_budget() -> str:
    """Step the budget to the next of BUDGETS, and return it."""
    budget = read(key=BUDGET_KEY)
    budget = BUDGETS[(BUDGETS.index(budget) + 1) % len(BUDGETS)] if budget in BUDGETS else BUDGETS[0]
    write(budget, key=BUDGET_KEY)
    return budget


def describe_budget(budget: str) -> str:
    return (f"wakes may spend {budget} input-token equivalents in any five hours" if budget
            else "no budget on what wakes spend")


# ------------------------------------------------------------- the budget


def state_path() -> str:
    """The daemon's journal, as qupa_cafe.daemon.Settings finds it."""
    return os.environ.get("QUPA_STATE") or str(Path.home() / ".local" / "share" / "qupa-cafe" / "daemon.sqlite3")


def spending(now: float | None = None, path: str | None = None) -> list[tuple[float, float]]:
    """When each model response of the agent's wakes in the last five hours
    came, and what it cost, oldest first: the wakes' own responses and their
    subagents', from Claude Code's transcripts of the sessions the journal
    records, a wake still running among them."""
    from qupa_cafe import wakes  # wakes imports the daemon, which imports this module

    now = time.time() if now is None else now
    since = now - BUDGET_SECONDS
    # A response in the window can belong to a wake that started up to the
    # longest a wake may run before it.
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since - wakes.WAKE_TIMEOUT_SECONDS))
    found: list[tuple[float, float]] = []
    for session in wakes.sessions_since(path or state_path(), started):
        transcript = wakes.transcript_path(session)
        if transcript is not None:
            found += [(at, cost) for at, cost in wakes.spend(transcript) if since < at <= now]
    return sorted(found)


def spent(now: float | None = None) -> float:
    """What the agent's wakes spent in the last five hours."""
    return sum(cost for _, cost in spending(now))


def over_budget(budget: str | None = None, now: float | None = None) -> str:
    """Why the budget holds wakes back now, or empty."""
    budget = read(key=BUDGET_KEY) if budget is None else budget
    allowed = tokens_of(budget)
    if not allowed:
        return ""
    now = time.time() if now is None else now
    found = spending(now)
    total = sum(cost for _, cost in found)
    if total < allowed:
        return ""
    # It is under again once enough of the oldest responses are five hours old.
    left = total
    for at, cost in found:
        left -= cost
        if left < allowed:
            break
    until = time.strftime("%H:%M UTC on %d %B", time.gmtime(at + BUDGET_SECONDS))
    from qupa_cafe import wakes

    return (f"its wakes have spent {wakes.count(round(total))} input-token equivalents of its researcher's Claude "
            f"usage in the last five hours, and they allow {budget} ({BUDGET_KEY}), so it waits until {until}")


# ------------------------------------------------------------------ readings


def readings_path() -> Path:
    return Path(os.environ.get("QUPA_QUOTA") or Path.home() / ".local" / "share" / "qupa-cafe" / "quota.json")


def record(status: dict, now: float | None = None) -> dict:
    """Keep the windows of one status line's input, each with when it was
    read; a window it leaves out keeps its last reading."""
    now = time.time() if now is None else now
    limits = status.get("rate_limits") if isinstance(status.get("rate_limits"), dict) else {}
    kept = load()
    for window in WINDOWS:
        found = limits.get(window)
        if isinstance(found, dict) and isinstance(found.get("used_percentage"), (int, float)):
            kept[window] = {"used": float(found["used_percentage"]), "resets_at": float(found.get("resets_at") or 0),
                            "read_at": now}
    if kept:
        path = readings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_text(json.dumps(kept), encoding="utf-8")
        part.replace(path)
    return kept


def load() -> dict:
    try:
        found = json.loads(readings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def usage(now: float | None = None) -> dict[str, dict]:
    """Each window's last reading whose reset time has not passed."""
    now = time.time() if now is None else now
    return {window: found for window, found in load().items()
            if window in WINDOWS and isinstance(found, dict) and float(found.get("resets_at") or 0) > now}


def held_back(cap: str | None = None, now: float | None = None) -> str:
    """Why no wake may start now, by the cap or the budget, or empty. In
    words for the forum's notice to somebody waiting, so the time it waits
    until is in UTC."""
    cap = read() if cap is None else cap
    for window, found in usage(now).items() if cap else ():
        if found["used"] >= int(cap):
            until = time.strftime("%H:%M UTC on %d %B", time.gmtime(found["resets_at"]))
            return (f"its researcher's Claude plan has used {found['used']:.0f}% of its {WINDOWS[window]} limit, "
                    f"and they stop its wakes at {cap}% ({KEY}), so it waits until the limit resets at {until}")
    return over_budget(now=now)


def summary(now: float | None = None) -> str:
    """The last readings in a few words, for the wake browser: "five-hour 6%,
    weekly 10%, read 17:39"; empty with none."""
    found = usage(now)
    if not found:
        return ""
    parts = [f"{WINDOWS[window]} {reading['used']:.0f}%" for window, reading in found.items()]
    latest = max(reading.get("read_at", 0) for reading in found.values())
    return ", ".join(parts) + f", read {time.strftime('%H:%M', time.localtime(latest))}"


def status_command() -> str:
    """This module as Claude Code runs a wake's status line."""
    posix = lambda path: str(path).replace(chr(92), "/")  # noqa: E731
    return f'"{posix(sys.executable)}" "{posix(Path(__file__).resolve())}"'


def main() -> int:
    """Record the status line's input, and print the line itself."""
    try:
        status = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        status = {}
    kept = record(status if isinstance(status, dict) else {})
    line = "Qupa Cafe wake"
    if kept:
        line += " · " + ", ".join(f"{WINDOWS[w]} {r['used']:.0f}%" for w, r in kept.items() if w in WINDOWS)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
