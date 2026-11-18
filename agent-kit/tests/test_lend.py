"""Lending an agent: the subagent a borrower's sessions see, the lender's
limits, a lent task's run and sandbox, and the borrower's tools."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from qupa_cafe import daemon, lend, quota, sandbox, server

OFFER = {"lender": "yoshi-agent", "researcher": "yoshi", "note": "numerics: \"fast\"\nand careful"}


# ------------------------------------------------------- the borrower's side


def test_each_agent_lent_to_the_researcher_becomes_a_subagent_of_their_sessions():
    assert lend.sync_agents([OFFER]) == ["yoshi-agent"]
    path = lend.agents_dir() / "qupa-lent-yoshi-agent.md"
    text = path.read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert "name: yoshi-agent\n" in front
    assert f"tools: {lend.RUN_TOOL}, {lend.RESULT_TOOL}\n" in front
    assert "model: haiku\n" in front
    # The note is one line, quoted, so it cannot end the frontmatter or add a key.
    description = next(line for line in front.splitlines() if line.startswith("description: "))
    assert json.loads(description[len("description: "):]).endswith('numerics: "fast" and careful')
    assert 'lender "yoshi-agent"' in text and "word for word" in text


def test_a_subagent_goes_when_its_offer_does_and_an_unsaid_list_changes_nothing():
    lend.sync_agents([OFFER])
    mine = lend.agents_dir() / "my-own-agent.md"
    mine.write_text("---\nname: mine\n---\n", encoding="utf-8")

    assert lend.sync_agents(None) == []
    assert (lend.agents_dir() / "qupa-lent-yoshi-agent.md").exists()

    assert lend.sync_agents([]) == []
    assert not (lend.agents_dir() / "qupa-lent-yoshi-agent.md").exists()
    # The researcher's own subagents are theirs.
    assert mine.exists()


def test_a_lender_name_that_is_not_a_plain_username_is_not_written():
    assert lend.sync_agents([{**OFFER, "lender": "../../evil"}]) == []
    assert list(lend.agents_dir().glob("*.md")) == []


# ------------------------------------------------------------ the limits


def test_no_lent_task_starts_at_the_lender_s_ceiling(monkeypatch):
    quota.record({"rate_limits": {"five_hour": {"used_percentage": 59, "resets_at": time.time() + 3600}}})
    assert lend.held_back() == ""
    quota.record({"rate_limits": {"five_hour": {"used_percentage": 61, "resets_at": time.time() + 3600}}})
    assert "up to 60% (QUPA_LEND_CEILING)" in lend.held_back()
    quota.write("70", key=lend.CEILING_KEY)
    assert lend.held_back() == ""


def test_no_lent_task_starts_once_lent_tasks_have_spent_the_budget(monkeypatch):
    quota.write("500k", key=lend.BUDGET_KEY)
    monkeypatch.setattr(lend, "spent", lambda now=None: 499_000)
    assert lend.held_back() == ""
    monkeypatch.setattr(lend, "spent", lambda now=None: 500_000)
    assert "allows them 500k (QUPA_LEND_BUDGET)" in lend.held_back()


def test_the_ledger_keeps_only_the_sessions_that_can_still_count():
    lend.record_session("old", now=1000.0)
    lend.record_session("new", now=1000.0 + quota.BUDGET_SECONDS + lend.TASK_TIMEOUT + 1)
    assert [e["session"] for e in lend.ledger()] == ["new"]


# -------------------------------------------------------- the lender's side


class Host:
    """The daemon's client for the forum host, answering a lender's requests."""

    def __init__(self, task: dict | None):
        self.lending = True
        self.task = task
        self.asked: list[dict] = []

    def _ask(self, agent: str, body: dict) -> dict:
        self.asked.append(body["lend"])
        if body["lend"].get("take"):
            task, self.task = self.task, None
            return {"task": task}
        return {}


@pytest.fixture
def lender(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox, "ready", lambda config=None: True)
    monkeypatch.setattr(lend, "trusted", lambda: True)
    monkeypatch.setattr(sandbox, "lend_config", lambda: {"workspace": str(tmp_path / "home")})
    monkeypatch.setattr(lend.shutil, "which", lambda name: "/bin/claude")
    ran = []

    def run(executable, command, cwd, timeout, on_session, alive, held, ended):
        ran.append({"command": command, "cwd": cwd, "held": held})
        on_session("s-1")
        alive()
        return "done", "s-1"

    monkeypatch.setattr(daemon, "run_in_background", run)
    monkeypatch.setattr(daemon, "last_said", lambda session: "x^3/3")
    return ran


def test_a_lent_task_runs_and_its_answer_goes_back_with_what_it_spent(lender, monkeypatch, tmp_path):
    host = Host({"id": 9, "borrower": "Wendy-agent", "researcher": "Wendy", "prompt": "Integrate x^2."})
    settings = daemon.Settings(api_username="yoshi-agent")
    from qupa_cafe import wakes
    monkeypatch.setattr(wakes, "transcript_path", lambda session: tmp_path / f"{session}.jsonl")
    monkeypatch.setattr(wakes, "spend", lambda path: [(1.0, 30_000.4), (2.0, 11_000.0)])

    assert lend.Lender(settings, host).once() is True
    assert host.asked == [{"take": True}, {"beat": 9},
                          {"finish": {"id": 9, "answer": "x^3/3", "failed": "", "tokens": 41_000}}]
    (run,) = lender
    prompt = run["command"][2]
    assert "Integrate x^2." in prompt and "Wendy" in prompt and "yoshi's Claude plan" in prompt
    # It stops on the lender's limits, not the wakes'.
    assert run["held"] is lend.held_back
    assert [e["session"] for e in lend.ledger()] == ["s-1"]


def test_nothing_is_taken_while_the_researcher_does_not_lend_or_a_limit_holds(lender, monkeypatch):
    host = Host({"id": 9, "prompt": "x"})
    settings = daemon.Settings(api_username="yoshi-agent")
    host.lending = False
    assert lend.Lender(settings, host).once() is False
    host.lending = True
    monkeypatch.setattr(lend, "held_back", lambda now=None: "the plan is full")
    assert lend.Lender(settings, host).once() is False
    monkeypatch.setattr(lend, "held_back", lambda now=None: "")
    # Claude Code starts a background session only in a folder it trusts.
    monkeypatch.setattr(lend, "trusted", lambda: False)
    assert lend.Lender(settings, host).once() is False
    assert host.asked == [] and lender == []


def test_a_lent_task_has_no_forum_tools_and_none_of_the_lender_s_settings():
    command = lend.task_command({"id": 3, "borrower": "Wendy-agent"}, "the prompt")
    assert command[:3] == ["claude", "--bg", "the prompt"]
    assert "--strict-mcp-config" in command and "--mcp-config" not in command
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--allowedTools" not in command
    given = json.loads(command[command.index("--settings") + 1])
    hook = given["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert hook.endswith(" hook lend")
    assert given["statusLine"]["command"] == quota.status_command()
    assert given["autoMemoryEnabled"] is False


def test_a_session_whose_turn_has_ended_has_ended_however_claude_code_lists_it(tmp_path, monkeypatch):
    from qupa_cafe import wakes

    transcript = tmp_path / "s.jsonl"
    monkeypatch.setattr(wakes, "transcript_path", lambda session: transcript)
    records = [{"type": "user", "message": {"content": "task"}},
               {"type": "assistant", "message": {"stop_reason": "tool_use", "content": []}},
               {"type": "user", "message": {"content": [{"type": "tool_result"}]}}]
    transcript.write_text(chr(10).join(json.dumps(r) for r in records), encoding="utf-8")
    assert daemon.turn_ended("s") is False
    records += [{"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "42"}]}},
                {"type": "system", "subtype": "turn_duration"}]
    transcript.write_text(chr(10).join(json.dumps(r) for r in records), encoding="utf-8")
    assert daemon.turn_ended("s") is True


# ---------------------------------------------------------- its sandbox


@pytest.fixture
def lent_config(tmp_path, monkeypatch):
    """The sandbox as qupa-install leaves one on Linux, and a lent task's configuration of it."""
    monkeypatch.setattr(sandbox, "WINDOWS", False)
    workspace = sandbox.sandbox_dir() / "workspace"
    (workspace / ".pixi" / "envs" / "default").mkdir(parents=True)
    run = sandbox.sandbox_dir() / "runtime" / "run.mjs"
    (run.parent / "node_modules" / "@anthropic-ai" / "sandbox-runtime").mkdir(parents=True)
    run.write_text("", encoding="utf-8")
    research = tmp_path / "research"
    research.mkdir()
    (tmp_path / "node").write_text("", encoding="utf-8")
    (tmp_path / "pixi").write_text("", encoding="utf-8")
    config = {"workspace": str(workspace), "agent_dirs": [str(research)], "read_dirs": [str(tmp_path / "project")],
              "protected": sandbox.protected_paths([str(research)]), "node": str(tmp_path / "node"),
              "run": str(run), "pixi": str(tmp_path / "pixi"), "bash": "/bin/bash"}
    sandbox.config_path().write_text(json.dumps(config), encoding="utf-8")
    lent = sandbox.lend_config()
    return {"lent": lent, "workspace": workspace, "research": research, "project": tmp_path / "project"}


def _decision(event: dict, config: dict) -> str:
    return sandbox.decide(event, config)["hookSpecificOutput"]["permissionDecision"]


def test_a_lent_task_writes_and_reads_only_its_own_emptied_home(lent_config):
    lent, home = lent_config["lent"], Path(lent_config["lent"]["workspace"])
    assert home == sandbox.lend_home() and list(home.iterdir()) == [home / ".tmp"]
    assert lent["env_workspace"] == str(lent_config["workspace"])
    assert lent["agent_dirs"] == [] and lent["read_dirs"] == [] and lent["lend"] is True
    # Its hook reads it from the file the commands are given too.
    assert sandbox.load_config(sandbox.lend_config_path()) == lent
    assert sandbox.ready(lent)

    def file(tool, path):
        return {"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(home)}

    assert _decision(file("Write", home / "fit.py"), lent) == "allow"
    assert _decision(file("Read", home / "fit.py"), lent) == "allow"
    for elsewhere in (lent_config["workspace"] / "notes.md", lent_config["research"] / "notes.md",
                      lent_config["project"] / "data.csv"):
        assert _decision(file("Read", elsewhere), lent) == "deny"
        assert _decision(file("Write", elsewhere), lent) == "deny"


@pytest.mark.parametrize("command", ["pip install tqdm", "cd x && pixi add numpy", "uv pip install a",
                                     "conda install -y b"])
def test_a_lent_task_installs_nothing(lent_config, command):
    bash = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": "."}
    out = sandbox.decide(bash, lent_config["lent"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "fixed" in out["permissionDecisionReason"]


def test_a_lent_task_s_command_runs_on_its_own_configuration(lent_config):
    bash = {"tool_name": "Bash", "tool_input": {"command": "python fit.py"}, "cwd": "."}
    out = sandbox.decide(bash, lent_config["lent"])["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert str(sandbox.lend_config_path()).replace("\\", "/") in out["updatedInput"]["command"]


def test_a_lent_task_messages_no_session_and_a_wake_gives_no_lent_agent_a_task(lent_config):
    message = {"tool_name": "SendMessage", "tool_input": {"to": "x", "message": "hi"}}
    assert _decision(message, lent_config["lent"]) == "deny"
    for tool in sandbox.LENT_TOOLS:
        assert _decision({"tool_name": tool, "tool_input": {}}, sandbox.load_config()) == "deny"


# ------------------------------------------------- the borrower's tools


class StubHost(BaseHTTPRequestHandler):
    """The forum host's /wake for lent tasks: the task is queued, then done."""

    requests: list[dict] = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["lend"]
        StubHost.requests.append(body)
        if "task" in body:
            answer = {"task": {"id": 5, "state": "queued"}}
        else:
            done = len([r for r in StubHost.requests if "result" in r]) >= 2
            answer = {"task": {"id": 5, "lender": "yoshi-agent", "state": "done" if done else "running",
                               "answer": "x^3/3" if done else "", "reason": ""}}
        payload = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def test_the_borrower_s_tool_gives_the_task_and_waits_for_the_answer(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHost)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    StubHost.requests = []
    for name, value in (("QUPA_FORUM_URL", "http://127.0.0.1:9"), ("QUPA_API_KEY", "k"),
                        ("QUPA_API_USERNAME", "Wendy-agent"), ("QUPA_POLICY_TOPIC_ID", "1"),
                        ("QUPA_HOST_URL", f"http://127.0.0.1:{httpd.server_port}")):
        monkeypatch.setenv(name, value)
    try:
        answered = server.QupaServer().call_tool("lent_agent_run", {"lender": "yoshi-agent", "task": "Integrate x^2."})
    finally:
        httpd.shutdown()
    assert json.loads(answered[0]["text"]) == {"task_id": 5, "state": "done", "answer": "x^3/3"}
    assert StubHost.requests[0] == {"task": {"lender": "yoshi-agent", "prompt": "Integrate x^2."}}
    assert StubHost.requests[1] == {"result": 5, "wait": server.LENT_REQUEST_WAIT}


# ------------------------------------------------- offering, and its window


class Asked:
    """A lend.Client whose forum host answers from a dict."""

    def __init__(self, monkeypatch, answers: dict):
        self.asked: list[dict] = []
        client = lend.Client(daemon.Settings(api_username="yoshi-agent", host_url="http://127.0.0.1:9"))

        def ask(request):
            self.asked.append(request)
            return answers[next(iter(request))]

        monkeypatch.setattr(client, "ask", ask)
        self.client = client


def test_an_offer_writes_its_limits_and_needs_the_tasks_folder_trusted(monkeypatch):
    asked = Asked(monkeypatch, {"offer": {"offer": {"to": ["Wendy"]}}})
    monkeypatch.setattr(lend, "trusted", lambda: True)
    assert asked.client.offer(["Wendy"], "note", "50", "500k") == {"to": ["Wendy"]}
    assert (lend.ceiling(), lend.budget()) == ("50", "500k")
    assert asked.asked == [{"offer": {"to": ["Wendy"], "note": "note"}}]

    for researchers, ceiling, budget in (([], None, None), (["Wendy"], "150", None), (["Wendy"], None, "lots")):
        with pytest.raises(lend.LendError):
            asked.client.offer(researchers, "", ceiling, budget)
    # An empty budget is none.
    asked.client.offer(["Wendy"], "", None, "")
    assert lend.budget() == ""

    monkeypatch.setattr(lend, "trusted", lambda: False)
    with pytest.raises(lend.LendError, match="does not trust"):
        asked.client.offer(["Wendy"])


# ------------------------------------------------------ the lending browser


def browser_with(offer=None, tasks=()):
    from qupa_cafe import lending

    browser = lending.LendBrowser("yoshi-agent")
    browser.show({"offer": offer, "tasks": list(tasks)},
                 [{"username": "Eric_Chiu", "name": "Eric Chiu"}, {"username": "Wendy_Wan", "name": "Wendy Wan"}])
    return browser


def test_the_lending_browser_ticks_whom_the_agent_is_lent_to_and_lends_to_whom_it_ticks():
    browser = browser_with({"to": ["Wendy_Wan"], "note": "numerics"})
    assert browser.ticked == {"wendy_wan"} and browser.note == "numerics"
    text = "\n".join(browser.frame(100, 30))
    assert "[x] " in text and "Lent to Wendy_Wan" in text and "Eric Chiu" in text

    browser.handle(" ")
    assert browser.chosen_names() == ["Eric_Chiu", "Wendy_Wan"]
    assert "the ticks differ" in "\n".join(browser.frame(100, 30))
    assert browser.handle("l") == "lend"
    browser.handle("down")
    browser.handle(" ")
    assert browser.chosen_names() == ["Eric_Chiu"]

    # A poll keeps ticks changed and not lent yet.
    browser.show({"offer": {"to": ["Wendy_Wan"], "note": "numerics"}, "tasks": []}, browser.researchers)
    assert browser.chosen_names() == ["Eric_Chiu"]


def test_stopping_takes_a_second_s_and_nothing_else_between():
    browser = browser_with({"to": ["Wendy_Wan"]})
    assert browser.handle("s") is None and "Press s again" in browser.message
    assert browser.handle("s") == "stop"
    browser.handle("s")
    browser.handle("down")
    assert browser.handle("s") is None
    assert browser_with(None).handle("s") is None


def test_the_other_keys_are_the_browser_s_actions():
    browser = browser_with()
    assert [browser.handle(k) for k in ("+", "-", "b", "n", "esc")] == ["ceiling:10", "ceiling:-10", "budget", "note",
                                                                       "quit"]


def test_its_tasks_are_listed_with_who_gave_each_and_why_one_failed():
    browser = browser_with(None, [
        {"id": 2, "borrower": "Wendy_Wan-agent", "state": "failed", "created_at": "2026-11-18T19:14:00",
         "reason": "it ran for 30 minutes"},
        {"id": 1, "borrower": "Eric_Chiu-agent", "state": "done", "created_at": "2026-11-18T18:00:00", "reason": ""}])
    browser.handle("t")
    text = "\n".join(browser.frame(100, 30))
    assert "from Wendy_Wan" in text and "it ran for 30 minutes" in text and "from Eric_Chiu" in text
    browser.handle("t")
    assert not browser.viewing


def test_the_ceiling_steps_by_ten_within_10_and_100_and_the_budget_through_its_steps():
    from qupa_cafe import lending

    assert lend.ceiling() == "60"
    lending.step_ceiling(10)
    assert lend.ceiling() == "70"
    for _ in range(9):
        lending.step_ceiling(-10)
    assert lend.ceiling() == "10"
    assert lend.budget() == "1M"
    lending.step_budget()
    assert lend.budget() == "2M"
