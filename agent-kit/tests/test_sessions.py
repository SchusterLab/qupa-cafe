"""Handing a post to one of the researcher's open sessions, and what happens
when the session does not answer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

import pytest
from conftest import category, serving
from test_server import _answer, _call, _run_server

from qupa_cafe import daemon, sandbox, sessions
from qupa_cafe.daemon import Forum, Settings, build_prompt, rewakes

HANDOFF = "Qupa Cafe handoff: topic 7, post 3\nalice-agent asks which flux bias the fit used: https://x/t/-/7/3"


def ended_pid() -> int:
    """The id of a process that has already ended."""
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    return int(done.stdout)


def send(to: str, message: str) -> dict:
    return {"tool_name": "SendMessage", "tool_input": {"to": to, "message": message}}


def decision(result: dict) -> tuple[str, str]:
    out = result["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def test_only_a_running_session_is_listed_and_its_name_is_its_own():
    sessions.register("trilayer [fd7ea5]", "/home/r/trilayer", "fitting the flux noise", [7], os.getpid())
    sessions.register("gone", "/home/r/old", "something", [], ended_pid())
    assert list(sessions.live()) == ["trilayer [fd7ea5]"]

    sessions.register(sessions.bare("trilayer [fd7ea5]"), "/home/r/trilayer", "fitting", [], os.getpid())
    # A later session that took the name is not forgotten by the earlier one.
    sessions.forget("trilayer", os.getpid() + 1)
    assert "trilayer" in sessions.live()
    sessions.forget("trilayer", os.getpid())
    assert "trilayer" not in sessions.live()


def test_a_session_registers_and_the_topics_it_posts_in_are_added_until_it_ends(forum):
    url, stub = forum
    stub.categories = [category(5, "Research", "Research.")]
    stub.topic_categories = {7: 5}
    register = {"name": "trilayer [fd7ea5]", "project": "/home/r/trilayer", "about": "fitting the flux noise"}

    responses = _run_server(url, [
        _call("register_session", {**register, "topics": [9]}, 1),
        _call("reply", {"topic_id": 7, "raw": "the fit converged"}, 2),
        _call("register_session", register, 3),
    ])
    said = {r["id"]: _answer(r) for r in responses}

    assert said[1]["registered"] == "trilayer"
    assert said[3]["topics"] == [7, 9]
    # The server has exited, and the registration with it.
    assert json.loads(sessions.path().read_text(encoding="utf-8"))["sessions"] == {}


def test_a_wake_hands_a_post_only_to_a_registered_session_once_in_the_handoff_s_form():
    sessions.register("trilayer", "/home/r/trilayer", "fitting the flux noise", [7], os.getpid())

    assert decision(sandbox.decide(send("fusion", HANDOFF), None))[0] == "deny"
    refused, why = decision(sandbox.decide(send("trilayer", "Please look at topic 7."), None))
    assert refused == "deny" and "Qupa Cafe handoff: topic <topic id>, post <post number>" in why

    assert decision(sandbox.decide(send("trilayer [fd7ea5]", HANDOFF), None))[0] == "allow"
    assert sessions.handed(7, 3)["session"] == "trilayer"

    again, why = decision(sandbox.decide(send("trilayer", HANDOFF), None))
    assert again == "deny" and "already" in why


def test_a_wake_does_not_read_or_write_in_slack():
    for tool in sandbox.SLACK_TOOLS:
        assert decision(sandbox.decide({"tool_name": tool, "tool_input": {}}, None))[0] == "deny"
        assert tool in sandbox.claude_settings()


def test_a_wake_does_not_register():
    event = {"tool_name": sandbox.REGISTER_TOOL, "tool_input": {"name": "fusion"}}
    assert decision(sandbox.decide(event, None))[0] == "deny"
    assert "SendMessage" in sandbox.claude_settings() and sandbox.REGISTER_TOOL in sandbox.claude_settings()


TOPIC = {"id": 7, "title": "Flux noise", "post_stream": {"posts": [
    {"post_number": 3, "username": "alice-agent", "raw": "Which flux bias did the fit use?"}]}}


def test_every_wake_is_told_the_open_sessions_and_how_to_hand_a_post_over_or_ask_for_help():
    sessions.register("trilayer", "/home/r/trilayer", "fitting the flux noise", [7], os.getpid())
    sessions.register("ats", "/home/r/ats", "writing the ATS proposal", [], os.getpid())

    for forked in (False, True):
        prompt = build_prompt(Settings(), "you were mentioned", TOPIC, "%BODY%", "the policy", forked=forked)
        assert "- `trilayer`, in /home/r/trilayer: fitting the flux noise; it has posted in this topic" in prompt
        assert "- `ats`, in /home/r/ats: writing the ATS proposal\n" in prompt
        assert "\nQupa Cafe handoff: topic 7, post 3\n" in prompt
        assert "Do not call start_reply" in prompt
        assert "send_message" in prompt and "-m qupa_cafe.sandbox read" in prompt


def test_with_no_session_open_a_wake_is_told_so():
    prompt = build_prompt(Settings(), "you were mentioned", TOPIC, "%BODY%", "the policy")
    assert "None is open now." in prompt


class Thread(BaseHTTPRequestHandler):
    """Topic 7, whose posts a test sets."""

    posts: list[dict] = []

    def _send(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        split = urllib.parse.urlsplit(self.path)
        if split.path == "/t/7.json":
            self._send({"id": 7, "post_stream": {"stream": [p["id"] for p in type(self).posts]}})
        else:
            wanted = {int(v) for v in urllib.parse.parse_qs(split.query)["post_ids[]"]}
            self._send({"post_stream": {"posts": [p for p in type(self).posts if p["id"] in wanted]}})

    def log_message(self, *args):
        pass


@pytest.fixture
def thread():
    Thread.posts = [{"id": 103, "post_number": 3, "username": "alice-agent"}]
    with serving(Thread) as url:
        yield Settings(forum_url=url, api_key="k", api_username="test-agent"), Thread


def test_a_handoff_nobody_answered_in_time_wakes_the_agent_again(thread, monkeypatch):
    settings, _ = thread
    sessions.register("trilayer", "/home/r/trilayer", "fitting", [7], os.getpid())
    sessions.hand(7, 3, "trilayer")

    assert daemon.handoff_pass(settings, Forum(settings), dry_run=False) == 0
    assert rewakes(settings.state_path) == []

    later = time.time() + sessions.WAIT_SECONDS + 1
    monkeypatch.setattr(sessions.time, "time", lambda: later)
    assert daemon.handoff_pass(settings, Forum(settings), dry_run=False) == 1
    (rewake,) = rewakes(settings.state_path)
    assert (rewake.topic_id, rewake.post_number) == (7, 3)
    assert rewake.said.startswith("you handed this post to your researcher's session `trilayer`")
    # Settled, so the next poll asks for nothing, and the post is not handed over again.
    assert daemon.handoff_pass(settings, Forum(settings), dry_run=False) == 0
    assert sessions.handed(7, 3) is not None


def test_a_handoff_the_agent_answered_or_whose_session_ended_is_settled_or_woken_for_at_once(thread):
    settings, stub = thread
    sessions.register("trilayer", "/home/r/trilayer", "fitting", [7], os.getpid())
    sessions.hand(7, 3, "trilayer")
    sessions.forget("trilayer", os.getpid())
    stub.posts.append({"id": 104, "post_number": 4, "username": "test-agent"})

    # Its session ended, so it is due now; the agent answered, so nobody is woken.
    assert daemon.handoff_pass(settings, Forum(settings), dry_run=False) == 0
    assert rewakes(settings.state_path) == [] and sessions.due() == []
