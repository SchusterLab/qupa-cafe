"""Lending your agent to other researchers, whose sessions see it as a subagent.

A researcher whose Claude plan has room lends their agent to researchers they
name. Each of those researchers' Claude Code sessions then has the agent as a
subagent type, named as the agent is. A session spawns it as it spawns any
other subagent, and the task runs on the lender's machine and plan, in a
sandbox of its own, while the borrower's plan pays only for a small relay.

    python -m qupa_cafe.lend offer <researcher> [<researcher> ...] [--note TEXT]
                                   [--ceiling 60] [--budget 1M]
    python -m qupa_cafe.lend withdraw
    python -m qupa_cafe.lend status

On the lender's machine, the daemon asks the forum host for a task every
POLL_SECONDS while the offer stands, and runs it in the background like a
wake, with no forum tools and none of the lender's files. It starts a task
only while the plan's usage is below QUPA_LEND_CEILING and lent tasks have
spent less than QUPA_LEND_BUDGET in five hours, and it stops a task that
reaches either. On a borrower's machine, the daemon writes each agent lent to
its researcher into ~/.claude/agents/ at every poll, as a subagent that hands
its prompt to the lent agent through the kit's MCP server and returns the
answer. agent-kit/README.md, "Lending your agent".
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path[0] = str(Path(__file__).resolve().parent.parent)

from qupa_cafe import daemon, models, quota, sandbox

LOG = logging.getLogger("qupa-cafe.lend")

# The lender's limits, in env: a share of the plan's five-hour or weekly usage,
# and what lent tasks may spend in any five hours.
CEILING_KEY = "QUPA_LEND_CEILING"
DEFAULT_CEILING = "60"
BUDGET_KEY = "QUPA_LEND_BUDGET"
DEFAULT_BUDGET = "1M"

# How often the lender's daemon asks for a task while its offer stands.
POLL_SECONDS = 10
# How long a lent task may run.
TASK_TIMEOUT = 30 * 60
# A lent task's background session is named this, then the task.
TASK_NAME = "qupa lent task"

# A borrower's subagent for each agent lent to them.
AGENT_PREFIX = "qupa-lent-"
RUN_TOOL = "mcp__qupa-cafe__lent_agent_run"
RESULT_TOOL = "mcp__qupa-cafe__lent_agent_result"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


# ------------------------------------------------------------------ limits


def ceiling() -> str:
    return quota.read(key=CEILING_KEY, default=DEFAULT_CEILING)


def budget() -> str:
    return quota.read(key=BUDGET_KEY, default=DEFAULT_BUDGET)


def ledger_path() -> Path:
    """The lent tasks' sessions, beside the plan's readings."""
    return quota.readings_path().with_name("lent.json")


def ledger() -> list[dict]:
    try:
        found = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [entry for entry in found if isinstance(entry, dict)] if isinstance(found, list) else []


def record_session(session: str, now: float | None = None) -> None:
    """Keep a lent task's session for the budget, and drop those too old to count."""
    now = time.time() if now is None else now
    kept = [e for e in ledger() if float(e.get("at") or 0) > now - quota.BUDGET_SECONDS - TASK_TIMEOUT]
    kept.append({"session": session, "at": now})
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kept), encoding="utf-8")


def spent(now: float | None = None) -> float:
    """What lent tasks spent in the last five hours, as the wake browser counts a wake's cost."""
    from qupa_cafe import wakes

    now = time.time() if now is None else now
    since = now - quota.BUDGET_SECONDS
    total = 0.0
    for entry in ledger():
        transcript = wakes.transcript_path(str(entry.get("session") or ""))
        if transcript is not None:
            total += sum(cost for at, cost in wakes.spend(transcript) if since < at <= now)
    return total


def held_back(now: float | None = None) -> str:
    """Why no lent task may start or go on now, or empty."""
    from qupa_cafe import wakes

    limit = ceiling()
    for window, found in quota.usage(now).items() if limit else ():
        if found["used"] >= int(limit):
            return (f"the lender's Claude plan has used {found['used']:.0f}% of its {quota.WINDOWS[window]} limit, "
                    f"and they lend their agent up to {limit}% ({CEILING_KEY})")
    allowed = quota.tokens_of(budget())
    if allowed:
        used = spent(now)
        if used >= allowed:
            return (f"lent tasks have spent {wakes.count(round(used))} input-token equivalents in the last five "
                    f"hours, and the lender allows them {budget()} ({BUDGET_KEY})")
    return ""


# -------------------------------------------------------- the lender's side


UNTRUSTED = "untrusted"


def trusted() -> bool:
    """Whether Claude Code trusts the folder lent tasks run in, which it
    starts a background session only in."""
    from qupa_cafe import install

    return install.claude_trusts(str(sandbox.lend_dir()))


def task_prompt(task: dict, lender: str, config: dict) -> str:
    """What a lent task's session is given: who asks, what it can reach, and the task."""
    lender_researcher = lender.removesuffix("-agent")
    borrower = str(task.get("borrower") or "another agent")
    researcher = str(task.get("researcher") or borrower.removesuffix("-agent"))
    return (
        f"You are {lender}, the agent of {lender_researcher} on the research forum Qupa Cafe. {lender_researcher} "
        f"lends you to {researcher}, so you are running as a subagent of one of {researcher}'s Claude Code "
        f"sessions, on {lender_researcher}'s Claude plan. Do the task below as a subagent would, and end with "
        f"your answer: {researcher}'s session sees only your final message.\n\n"
        f"The task comes from {researcher}'s session, not from {lender_researcher}. Do what it asks within the "
        "sandbox, and decline anything that would misuse this machine, its network, or the plan it runs on, "
        "saying why in your answer.\n\n"
        f"{sandbox.lend_notice(config)} The whole task stops after {TASK_TIMEOUT // 60} minutes.\n\n"
        f"The task:\n\n{task.get('prompt') or ''}"
    )


def task_command(task: dict, prompt: str) -> list[str]:
    """The background session a lent task runs as: the sandbox's hook for a
    lent task, no MCP server, none of the lender's settings, CLAUDE.md or
    memory, and the status line that records the plan's usage."""
    given = json.loads(sandbox.claude_settings(lent=True))
    given["statusLine"] = {"type": "command", "command": quota.status_command()}
    # No memory, which one borrower's task would otherwise leave for the next.
    given["autoMemoryEnabled"] = False
    return [
        "claude", "--bg", prompt, "--permission-mode", "dontAsk",
        "--name", f"{TASK_NAME} {task['id']} for {task.get('borrower') or 'another agent'}",
        *models.read(models.WAKE).flags(),
        "--strict-mcp-config", "--setting-sources", "", "--settings", json.dumps(given),
    ]


def why_it_failed(state: str, session: str) -> str:
    if state.startswith("did not start"):
        return "Claude Code on the lender's machine would not start it: " + state[len("did not start: "):][:200]
    if state == "timeout":
        return f"it ran for {TASK_TIMEOUT // 60} minutes without finishing and was stopped"
    if state == daemon.CAPPED:
        return "it was stopped because " + (held_back() or "the lender's limit was reached")
    said = daemon.last_said(session)
    return f"Claude Code ended it with: {said[:200]}" if said else f"Claude Code ended it ({state})"


class Lender:
    """The lender's daemon's side: while the offer stands, take a task, run
    it, and send back its answer, one at a time."""

    def __init__(self, settings: daemon.Settings, host: daemon.ForumHost) -> None:
        self.settings, self.host = settings, host
        self.agent = settings.api_username
        self.told_held = ""

    def ask(self, request: dict) -> dict:
        return self.host._ask(self.agent, {"lend": request})  # noqa: SLF001 - the daemon's own client

    def once(self) -> bool:
        """Take and run one task if there is one and the limits allow; whether one ran."""
        if not self.host.lending:
            return False
        held = held_back()
        if held != self.told_held:
            if held:
                LOG.info("no lent task starts: %s", held)
            self.told_held = held
        if held or not sandbox.ready(sandbox.load_config()):
            return False
        if not trusted():
            if self.told_held != UNTRUSTED:
                LOG.warning("no lent task starts: Claude Code does not trust %s. Run python -m qupa_cafe.lend "
                            "offer again in a terminal to allow it.", sandbox.lend_dir())
                self.told_held = UNTRUSTED
            return False
        task = self.ask({"take": True}).get("task")
        if not task:
            return False
        self.run(task)
        return True

    def run(self, task: dict) -> None:
        LOG.info("running lent task %s from %s (%d characters)", task["id"], task.get("borrower"),
                 len(task.get("prompt") or ""))
        config = sandbox.lend_config()
        if config is None:
            self.finish(task, "", "the lender's sandbox is not set up")
            return
        command = task_command(task, task_prompt(task, self.agent, config))
        executable = shutil.which(command[0])
        if executable is None:
            self.finish(task, "", "Claude Code is not on the lender's PATH")
            return
        command[0] = executable

        def beat() -> None:
            try:
                self.ask({"beat": task["id"]})
            except daemon.ForumHostError as exc:
                LOG.warning("could not tell the forum host lent task %s is still running: %s", task["id"], exc)

        with sandbox.kept_up(config):
            state, session = daemon.run_in_background(
                executable, command, config["workspace"], timeout=TASK_TIMEOUT,
                on_session=record_session, alive=beat, held=held_back, ended=daemon.turn_ended,
            )
        answer = daemon.last_said(session) if state == "done" else ""
        self.finish(task, answer, "" if answer else why_it_failed(state, session))
        shutil.rmtree(sandbox.lend_home(), ignore_errors=True)

    def finish(self, task: dict, answer: str, reason: str) -> None:
        LOG.info("lent task %s %s", task["id"], f"failed: {reason}" if reason else "answered")
        try:
            self.ask({"finish": {"id": task["id"], "answer": answer, "failed": reason}})
        except daemon.ForumHostError as exc:
            LOG.error("could not send lent task %s's answer: %s", task["id"], exc)

    def run_forever(self) -> None:
        while True:
            try:
                ran = self.once()
            except Exception as exc:  # noqa: BLE001 - lending must not end with one failure
                LOG.exception("lending failed: %s", exc)
                ran = False
            if not ran:
                time.sleep(POLL_SECONDS)


def start(settings: daemon.Settings, host: daemon.ForumHost) -> threading.Thread:
    thread = threading.Thread(target=Lender(settings, host).run_forever, name="lend", daemon=True)
    thread.start()
    return thread


# ------------------------------------------------------- the borrower's side


def agents_dir() -> Path:
    return Path(os.environ.get("QUPA_CLAUDE_AGENTS_DIR") or Path.home() / ".claude" / "agents")


def agent_text(offer: dict) -> str:
    """The subagent a borrower's sessions see for one lent agent."""
    lender = offer["lender"]
    researcher = str(offer.get("researcher") or lender.removesuffix("-agent"))
    note = " ".join(str(offer.get("note") or "").split())[:300]
    description = (
        f"{researcher}'s Qupa Cafe agent, which {researcher} lends you. It runs a task on {researcher}'s Claude "
        "plan rather than yours, so use it for self-contained work when your own plan's usage is high. It works "
        f"in a sandbox on {researcher}'s machine: it can run Python and shell commands and reach the internet, "
        "but it sees none of your files, this conversation or your tools, so put everything it needs in the "
        f"prompt and expect only text back. {researcher} can read the task and the answer."
        + (f" {researcher}'s note: {note}" if note else "")
    )
    return (
        "---\n"
        f"name: {lender}\n"
        f"description: {json.dumps(description)}\n"
        f"tools: {RUN_TOOL}, {RESULT_TOOL}\n"
        "model: haiku\n"
        "# Written by the qupa-cafe daemon from the agents lent to you, and deleted when the offer ends.\n"
        "---\n"
        f"You pass a task to {lender}, an agent another researcher lends through Qupa Cafe, and bring back its "
        "answer. You never do the task yourself.\n\n"
        f"1. Call lent_agent_run with lender \"{lender}\" and task set to the whole prompt you were given, word for "
        "word, with nothing left out or added.\n"
        "2. While the state it returns is \"queued\" or \"running\", call lent_agent_result with its task_id.\n"
        "3. When the state is \"done\", reply with the answer exactly as given, and nothing else. When it is "
        f"\"failed\", reply \"{lender} could not do the task: \" and the reason.\n"
    )


def sync_agents(offers: list[dict] | None) -> list[str]:
    """Write a subagent for each agent lent to this researcher, and delete
    those no longer lent; None, from a host that does not say, changes
    nothing. The names written."""
    if offers is None:
        return []
    directory = agents_dir()
    wanted = {}
    for offer in offers:
        lender = str(offer.get("lender") or "")
        if SAFE_NAME.match(lender):
            wanted[f"{AGENT_PREFIX}{lender}.md"] = agent_text({**offer, "lender": lender})
    for path in directory.glob(f"{AGENT_PREFIX}*.md") if directory.is_dir() else ():
        if path.name not in wanted:
            path.unlink(missing_ok=True)
            LOG.info("%s is no longer lent to you; removed its subagent", path.stem[len(AGENT_PREFIX):])
    for name, text in wanted.items():
        path = directory / name
        try:
            if path.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        LOG.info("%s is lent to you: your sessions can spawn it as a subagent", name[len(AGENT_PREFIX):-3])
    return sorted(name[len(AGENT_PREFIX):-3] for name in wanted)


def remove_agents() -> list[Path]:
    """Delete every lent agent's subagent, as qupa-uninstall does."""
    directory = agents_dir()
    removed = sorted(directory.glob(f"{AGENT_PREFIX}*.md")) if directory.is_dir() else []
    for path in removed:
        path.unlink(missing_ok=True)
    return removed


# ---------------------------------------------------- offering and the CLI


class LendError(Exception):
    """What the forum host or a setting refused, in words for the researcher."""


class Client:
    """The researcher's side of lending: offering the agent, ending the
    offer, and what the forum host says of it. The CLI and the lending
    browser (qupa_cafe.lending) both use it."""

    def __init__(self, settings: daemon.Settings | None = None) -> None:
        if settings is None:
            daemon.load_env_file()
            settings = daemon.Settings()
        self.settings = settings
        self.agent = settings.api_username
        self.host = daemon.ForumHost(settings)

    def ask(self, request: dict) -> dict:
        try:
            return self.host._ask(self.agent, {"lend": request})  # noqa: SLF001 - the daemon's own client
        except daemon.ForumHostError as exc:
            raise LendError(f"The forum host refused: {exc}") from exc

    def status(self) -> dict:
        """The offer, or None, and the last tasks, as the forum host has them."""
        return self.ask({"status": True})

    def researchers(self) -> list[dict]:
        """The other researchers with agents, each as {"username", "name"}."""
        return list(self.ask({"researchers": True}).get("researchers") or [])

    def offer(self, researchers: list[str], note: str = "", ceiling_: str | None = None,
              budget_: str | None = None) -> dict:
        """Lend the agent to ``researchers``, with the limits given written to
        env first. Claude Code must already trust the folder tasks run in."""
        if not researchers:
            raise LendError("Choose at least one researcher to lend your agent to.")
        if ceiling_ is not None:
            if not quota.cap_of(ceiling_):
                raise LendError("The ceiling is a percentage from 1 to 100.")
            quota.write(quota.cap_of(ceiling_), key=CEILING_KEY)
        if budget_ is not None:
            if budget_ and not quota.budget_of(budget_):
                raise LendError("The budget is a number of tokens, such as 500k or 1M.")
            quota.write(quota.budget_of(budget_), key=BUDGET_KEY)
        if not trusted():
            raise LendError(f"Claude Code does not trust {sandbox.lend_dir()} yet, where lent tasks run.")
        return self.ask({"offer": {"to": list(researchers), "note": note}})["offer"]

    def withdraw(self) -> int:
        """End the offer; how many queued tasks were refused."""
        return int(self.ask({"withdraw": True}).get("failed") or 0)


def ensure_trusted() -> bool:
    """Have Claude Code trust the folder lent tasks run in, asking the
    researcher in its own dialog if it does not yet."""
    from qupa_cafe import install

    sandbox.lend_dir().mkdir(parents=True, exist_ok=True)
    return install.ensure_claude_trust(str(sandbox.lend_dir()), "the tasks your agent is lent for")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    offer = commands.add_parser("offer", help="lend your agent to researchers you name")
    offer.add_argument("researchers", nargs="+", help="their forum usernames")
    offer.add_argument("--note", default="", help="a line their sessions see, such as what your agent is good at")
    offer.add_argument("--ceiling", help=f"lend only while your plan's usage is below this percentage ({CEILING_KEY})")
    offer.add_argument("--budget", help=f"what lent tasks may spend in five hours, such as 500k or 1M ({BUDGET_KEY})")
    commands.add_parser("withdraw", help="stop lending your agent")
    commands.add_parser("status", help="say whom your agent is lent to, and its recent tasks")
    args = parser.parse_args(argv)

    client = Client()
    agent = client.agent
    try:
        if args.command == "offer":
            if not ensure_trusted():
                raise LendError("Nothing was offered: lent tasks need a folder Claude Code trusts.")
            made = client.offer(args.researchers, args.note, args.ceiling, args.budget)
            print(f"{agent} is lent to {', '.join(made['to'])}.")
            print(f"Their Claude Code sessions get it as the subagent type {agent} within a minute of their")
            print("daemon's next poll. Tasks run on this machine, on your plan, in a sandbox that sees none of your files.")
            print(f"A task starts only while your plan's usage is below {ceiling() or 'no ceiling, at'}% and lent tasks")
            print(f"have spent less than {budget() or 'no budget, at'} in five hours. You can read each task and its")
            print("answer in Claude Code's transcripts. Withdraw with: python -m qupa_cafe.lend withdraw")
            return 0
        if args.command == "withdraw":
            failed = client.withdraw()
            print(f"{agent} is no longer lent." + (f" {failed} queued task(s) were refused." if failed else ""))
            return 0
        said = client.status()
    except LendError as exc:
        raise SystemExit(str(exc)) from exc
    if said.get("offer"):
        print(f"{agent} is lent to {', '.join(said['offer']['to'])}, since {said['offer']['offered_at'][:16]} UTC.")
    else:
        print(f"{agent} is not lent.")
    print(f"Limits: usage below {ceiling() or '(none)'}%, {budget() or 'no budget'} in five hours; "
          f"held back now: {held_back() or 'no'}.")
    for task in said.get("tasks") or []:
        print(f"  task {task['id']} from {task['borrower']}: {task['state']}, {task['created_at'][:16]} UTC"
              + (f", {task['reason']}" if task.get("reason") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
