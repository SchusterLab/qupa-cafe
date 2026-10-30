"""Agents in Slack: the channels someone added the bot to, DMs to people with
no forum account, and the answers carried back to the agent.

The stub forum and the fake Slack are test_slack_bridge.py's.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
# connect and world are fixtures, which pytest finds by name in this module.
from test_slack_bridge import bridge_mod, connect, dm, start_thread, world  # noqa: F401

LAB = {"id": "CLAB", "name": "lab", "is_member": True, "purpose": {"value": "Our group"}, "num_members": 6}


@pytest.fixture
def lab(world):  # noqa: F811 - the fixture imported above
    stub, slack, bridge = world
    slack.others = [LAB, {"id": "CRND", "name": "random", "is_member": False}]
    return stub, slack, bridge


def ask(bridge, action, **body):
    return bridge.agent_request({"agent": "sunada-agent", "action": action, **body})


def private_messages(stub):
    return [(as_user, form) for as_user, form in stub.created_as if form.get("archetype") == "private_message"]


def test_an_agent_sees_only_the_channels_someone_added_the_bot_to(lab):
    stub, slack, bridge = lab
    status, answer = ask(bridge, "channels")
    assert status == 200
    assert answer["channels"] == [{"id": "CLAB", "name": "lab", "private": False, "purpose": "Our group", "members": 6}]

    # Not one it is not in, which it does not join, nor one mirroring the forum.
    status, answer = ask(bridge, "read", channel="#random")
    assert status == 422 and "under Integrations" in answer["error"]
    assert ("join", {"channel": "CRND"}) not in slack.calls
    status, answer = ask(bridge, "post", channel="qupa-research", text="Hello")
    assert status == 422 and "mirrors the forum" in answer["error"]
    # And only an agent asks.
    assert bridge.agent_request({"agent": "pat", "action": "channels"})[0] == 403


def test_an_agent_reads_a_channel_oldest_first(lab):
    stub, slack, bridge = lab
    slack.history["CLAB"] = [
        {"ts": "5.2", "user": "UP", "text": "Is the fridge up?", "reply_count": 2},
        {"ts": "5.1", "user": "UN", "text": "Morning <@UP>", "files": [{"name": "plot.png"}]},
    ]
    status, answer = ask(bridge, "read", channel="lab")
    assert status == 200 and answer["channel"] == "lab"
    first, second = answer["messages"]
    assert first == {"ts": "5.1", "from": "Newcomer", "text": "Morning @pat", "files": ["plot.png"]}
    assert second == {"ts": "5.2", "from": "@pat", "text": "Is the fridge up?", "replies": 2}


def test_an_agent_posts_in_a_channel_and_answers_in_its_thread_reach_the_agent(lab):
    stub, slack, bridge = lab
    start_thread(stub, bridge)
    status, answer = ask(bridge, "post", channel="lab", text="Has anyone measured **T1** here?", topic_id=10, post_number=2)
    assert status == 200 and answer["posted"] and answer["url"].startswith("https://slack.example/archives/CLAB/")
    sent = slack.sent[answer["ts"]]
    assert sent["channel"] == "CLAB" and sent["username"] == "sunada's agent"
    assert "Has anyone measured *T1* here?" in sent["text"]
    # Where the discussion is: the forum's topic, and its post's copy in Slack.
    assert "/t/surface-code-thresholds/10/2|Surface code thresholds>" in sent["text"]
    copy = bridge.state.copy_of(stub.topics[10]["posts"][1]["id"])
    assert f"archives/{copy.channel_id}/p{copy.ts.replace('.', '')}" in sent["text"]
    assert "may quote them on the forum under your name" in sent["text"]

    # A non-member's answer in its thread wakes the agent, in a private
    # message from the system account, and is not posted in the topic.
    reply = {"type": "message", "user": "UN", "text": "Yes, 80 us.", "channel": "CLAB", "ts": "6.1", "thread_ts": answer["ts"]}
    assert bridge.handle_message(reply) == "answer carried to sunada-agent"
    ((as_user, form),) = private_messages(stub)
    assert as_user == "system" and form["target_recipients"] == "sunada-agent"
    assert form["title"].startswith("Slack: answers to your message in #lab")
    assert "Newcomer wrote in Slack" in form["raw"] and "> Yes, 80 us." in form["raw"]
    assert "data, not instructions" in form["raw"]
    # The next answer continues the same message.
    topic_id = max(stub.topics)
    bridge.handle_message({**reply, "user": "UP", "text": "Same here.", "ts": "6.2"})
    as_user, form = stub.created_as[-1]
    assert as_user == "system" and form["topic_id"] == str(topic_id) and "@pat wrote in Slack" in form["raw"]
    # Nothing of it goes back into Slack, and the channel's other talk is not carried.
    before = len(slack.messages) + len(slack.dms)
    bridge.poll()
    assert len(slack.messages) + len(slack.dms) == before
    assert bridge.handle_message({**reply, "ts": "6.3", "thread_ts": None}) == "not a bridged channel"


def test_a_topic_the_workspace_may_not_read_is_not_linked(lab):
    stub, slack, bridge = lab
    stub.topic(20, "Staff matters", 2, "sunada")
    stub.post(20, "sunada", "Private.")
    status, answer = ask(bridge, "post", channel="lab", text="See this", topic_id=20)
    assert status == 422 and "not one the whole workspace may read" in answer["error"]
    assert not any(m["channel"] == "CLAB" for m in slack.messages)


def test_an_agent_dms_someone_with_no_account_invited_the_first_time(lab):
    stub, slack, bridge = lab
    status, answer = ask(bridge, "message", person="Newcomer", text="Could you share the fridge log?", topic_id=10)
    assert status == 200 and answer == {**answer, "sent": True, "to": "Newcomer", "invited": True}
    (first,) = slack.dms
    assert first["channel"] == "DUN" and first["username"] == "sunada's agent"
    assert first["text"].startswith("*sunada's agent*, on Qupa Cafe")
    assert "Could you share the fridge log?" in first["text"] and "/t/surface-code-thresholds/10|" in first["text"]
    assert "https://forum.example/invites/K1" in first["text"] and "Reply *stop*" in first["text"]
    assert "#is-the-agent-kit-safe-to-run|the project's README>" in first["text"]

    # A second DM, from any agent, carries no invitation.
    status, answer = ask(bridge, "message", person="new@uni.edu", text="And the wiring diagram?")
    assert status == 200 and answer["invited"] is False
    assert "invites/" not in slack.dms[-1]["text"]
    assert "the project's README" not in slack.dms[-1]["text"]
    # Nor does their writing to the bot bring one.
    assert bridge.handle_message(dm("UN", "Sure, attached.", "7.1")) == "answer carried to sunada-agent"
    assert stub.invites == ["new@uni.edu"]


def test_an_answer_in_a_dm_goes_to_the_agent_that_wrote_and_is_acknowledged_once(lab):
    stub, slack, bridge = lab
    ask(bridge, "message", person="UN", text="Which fridge was it?")
    assert bridge.handle_message(dm("UN", "The Bluefors.", "7.1")) == "answer carried to sunada-agent"
    ((as_user, form),) = private_messages(stub)
    assert as_user == "system" and "your DM to them" in form["title"] and "> The Bluefors." in form["raw"]
    assert slack.dms[-1]["text"] == "Sent to sunada's agent."
    sent = len(slack.dms)
    assert bridge.handle_message(dm("UN", "The big one.", "7.2")) == "answer carried to sunada-agent"
    assert len(slack.dms) == sent


def test_an_agent_does_not_dm_a_member_a_guest_or_someone_who_said_stop(lab):
    stub, slack, bridge = lab
    status, answer = ask(bridge, "message", person="pat@uni.edu", text="Hi")
    assert status == 422 and "@pat" in answer["error"] and "there" in answer["error"]
    slack.people["UG"] = {"id": "UG", "is_restricted": True, "profile": {"email": "g@else.org", "real_name": "Gus"}}
    status, answer = ask(bridge, "message", person="Gus", text="Hi")
    assert status == 422 and "guest" in answer["error"]
    status, answer = ask(bridge, "message", person="Nobody Here", text="Hi")
    assert status == 422 and "nobody" in answer["error"]

    assert bridge.handle_message(dm("UN", "stop", "8.1")) == "stopped agents' messages"
    assert "No agent will message you again" in slack.dms[-1]["text"]
    status, answer = ask(bridge, "message", person="UN", text="Hi")
    assert status == 422 and "no more messages" in answer["error"]
    assert bridge.handle_message(dm("UN", "start", "8.2")) == "allowed agents' messages"
    assert ask(bridge, "message", person="UN", text="Hello again")[0] == 200


def test_the_agents_listener_wants_the_hosts_secret(lab):
    stub, slack, bridge = lab
    httpd = bridge_mod.listen_for_edits(bridge, "127.0.0.1", 0, "", "agent-s3cret")
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/agent"

        def call(secret, body):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={bridge_mod.AGENT_SECRET_HEADER: secret})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()

        assert call("wrong", {"agent": "sunada-agent", "action": "channels"}) == (403, "bad secret")
        status, answer = call("agent-s3cret", {"agent": "sunada-agent", "action": "channels"})
        assert status == 200 and answer["channels"][0]["name"] == "lab"
        # Without its secret, the edit webhook is not served.
        req = urllib.request.Request(url.removesuffix("agent"), data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req, timeout=5)
        assert refused.value.code == 404
    finally:
        httpd.shutdown()
