"""Tests for slack-bridge.py.

Against a stub forum over real HTTP, which keeps posts, topics, categories,
groups and accounts the way Discourse reports them, and a fake Slack client
that records every call. slack_sdk is not needed to run them.

Run from the repository root::

    python3 -m pytest server/tests -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("slack_bridge", HERE.parent / "slack-bridge.py")
bridge_mod = importlib.util.module_from_spec(spec)
# Registered before it runs, because a dataclass looks its module up by name.
sys.modules["slack_bridge"] = bridge_mod
spec.loader.exec_module(bridge_mod)


# ---------------------------------------------------------------- text


def test_to_slack_escapes_and_converts_links_and_bold():
    text = bridge_mod.to_slack("a < b & **c** [arXiv](https://arxiv.org/abs/1) $x<y$", "https://f/t/x/1/1")
    assert text == "a &lt; b &amp; *c* <https://arxiv.org/abs/1|arXiv> $x&lt;y$"


def test_to_slack_converts_html_links():
    text = bridge_mod.to_slack(
        "the <a href='https://f/admin'>admin <b>panel</b></a> and the "
        '<a class="x" href="/guidelines">guidelines</a>',
        "https://f/t/x/1/1",
    )
    assert text == "the <https://f/admin|admin panel> and the <https://f/guidelines|guidelines>"
    # An address that is not a web address leaves the label alone.
    assert bridge_mod.to_slack("<a href='javascript:x'>click</a>", "https://f/t/x/1/1") == "click"


def test_to_slack_turns_images_and_attachments_into_their_names():
    assert bridge_mod.to_slack("see ![plot|690x388](upload://abc.png)", "u") == "see [image: plot]"
    assert bridge_mod.to_slack("data: [run.csv|attachment](upload://x.csv) (2 KB)", "u") == "data: run.csv (2 KB)"


def test_to_slack_cuts_long_posts_with_a_link():
    text = bridge_mod.to_slack("word " * 2000, "https://f/t/x/1/3")
    assert len(text) < bridge_mod.MAX_SLACK_TEXT + 100
    assert text.endswith("<https://f/t/x/1/3|Read the rest>")


def test_from_slack_resolves_mentions_links_and_entities():
    names = {"U1": "@sunada", "U2": "Pat"}
    raw = bridge_mod.from_slack(
        "<@U1> and <@U2|pat>: see <https://example.org|this> and <https://x.org> in <#C1|general> &lt;3 <!here>",
        names.__getitem__,
    )
    assert raw == "@sunada and Pat: see [this](https://example.org) and https://x.org in #general <3 here"


def test_channel_name_is_slack_safe_and_includes_the_parent():
    assert bridge_mod.channel_name({"id": 5, "slug": "Quantum_Error.Correction"}, None, "qupa-") == "qupa-quantum_error-correction"
    assert bridge_mod.channel_name({"id": 6, "slug": "codes"}, {"slug": "qec"}, "qupa-") == "qupa-qec-codes"


# ---------------------------------------------------------------- a stub forum


class StubForum:
    def __init__(self):
        self.categories = {
            1: {"id": 1, "name": "Research", "slug": "research", "read_restricted": False},
            2: {"id": 2, "name": "Staff", "slug": "staff", "read_restricted": True},
            8: {"id": 8, "name": "arXiv quant-ph", "slug": "arxiv", "read_restricted": False},
        }
        self.users = {
            "sunada": {"id": 2, "username": "sunada", "email": "sunada@uni.edu", "active": True},
            "sunada-agent": {"id": 3, "username": "sunada-agent", "email": "sunada-agent@forum", "active": True},
            "pat": {"id": 4, "username": "pat", "email": "pat@uni.edu", "active": True},
            "pat-agent": {"id": 6, "username": "pat-agent", "email": "pat-agent@forum", "active": True},
            "arxiv-bot": {"id": 5, "username": "arxiv-bot", "email": "arxiv-bot@forum", "active": True},
        }
        self.groups = {"agents": ["sunada-agent", "pat-agent"], "feeds": ["arxiv-bot"]}
        self.topics: dict[int, dict] = {}
        self.posts: list[dict] = []
        self.invites: list[str] = []
        self.added: list[tuple[int, str]] = []
        self.created_as: list[tuple[str, dict]] = []
        self.next_post = 100

    def topic(self, topic_id, title, category_id, opener, archetype="regular", allowed_users=(), allowed_groups=()):
        self.topics[topic_id] = {
            "id": topic_id, "title": title, "slug": title.lower().replace(" ", "-"),
            "category_id": category_id, "archetype": archetype, "opener": opener, "posts": [],
            "allowed_users": list(allowed_users), "allowed_groups": list(allowed_groups),
        }

    def post(self, topic_id, username, raw, post_type=1):
        self.next_post += 1
        topic = self.topics[topic_id]
        post = {
            "id": self.next_post, "topic_id": topic_id, "username": username, "raw": raw,
            "post_number": len(topic["posts"]) + 1, "post_type": post_type,
            "topic_slug": topic["slug"], "topic_title": topic["title"],
        }
        topic["posts"].append(post)
        self.posts.append(post)
        return post


def serve(stub: StubForum):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path

            if path in ("/posts.json", "/private-posts.json"):
                # Newest first, 50 a page, as posts#latest. The public listing
                # has regular posts only; the private one, as an admin sees it,
                # has every private post, whispers included.
                private = path == "/private-posts.json"
                listed = [
                    p for p in stub.posts
                    if (stub.topics[p["topic_id"]]["archetype"] == "private_message") == private
                    and (private or p["post_type"] == 1)
                    and ("before" not in q or p["id"] < int(q["before"]))
                ]
                root = "private_posts" if private else "latest_posts"
                return self._json(200, {root: sorted(listed, key=lambda p: -p["id"])[:50]})
            if path.startswith("/posts/"):
                post_id = int(path.split("/")[2].removesuffix(".json"))
                return self._json(200, next(p for p in stub.posts if p["id"] == post_id))
            if path.startswith("/t/"):
                topic = stub.topics.get(int(path.split("/")[2].removesuffix(".json")))
                if not topic:
                    return self._json(404, {"errors": ["not found"]})
                return self._json(200, {
                    "id": topic["id"], "title": topic["title"], "slug": topic["slug"],
                    "category_id": topic["category_id"], "archetype": topic["archetype"],
                    "details": {
                        "created_by": {"id": stub.users[topic["opener"]]["id"] if topic["opener"] in stub.users else -1, "username": topic["opener"]},
                        "allowed_users": [
                            {"id": stub.users[u]["id"] if u in stub.users else -1, "username": u}
                            for u in topic["allowed_users"]
                        ],
                        "allowed_groups": [{"name": g} for g in topic["allowed_groups"]],
                    },
                    "post_stream": {"stream": [p["id"] for p in topic["posts"]]},
                })
            if path == "/categories.json":
                return self._json(200, {"category_list": {"categories": list(stub.categories.values())}})
            if path.startswith("/c/"):
                return self._json(200, {"category": stub.categories[int(path.split("/")[2])]})
            if path.startswith("/groups/"):
                name = path.split("/")[2]
                if name not in stub.groups:
                    return self._json(404, {})
                return self._json(200, {"members": [{"username": u} for u in stub.groups[name]]})
            if path.startswith("/u/") and path.endswith("/emails.json"):
                user = stub.users.get(path.split("/")[2])
                return self._json(200, {"email": user["email"]}) if user else self._json(404, {})
            if path.startswith("/u/"):
                user = stub.users.get(path.split("/")[2].removesuffix(".json"))
                return self._json(200, {"user": user}) if user else self._json(404, {})
            if path == "/admin/users/list/all.json":
                return self._json(200, [u for u in stub.users.values() if u["email"] == q.get("email", "").lower()])
            return self._json(404, {})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
            if self.path == "/posts.json":
                as_user = self.headers.get("Api-Username")
                if len(form.get("raw", "")) < 5:
                    return self._json(422, {"errors": ["Body is too short (minimum is 5 characters)"]})
                stub.created_as.append((as_user, form))
                if form.get("archetype") == "private_message":
                    if any(t["title"] == form["title"] for t in stub.topics.values()):
                        return self._json(422, {"errors": ["Title has already been used"]})
                    topic_id = max(stub.topics) + 1
                    stub.topic(topic_id, form["title"], None, as_user, archetype="private_message",
                               allowed_users=[as_user] + form["target_recipients"].split(","))
                    return self._json(200, stub.post(topic_id, as_user, form["raw"]))
                post = stub.post(int(form["topic_id"]), as_user, form["raw"])
                return self._json(200, post)
            if self.path.startswith("/t/") and self.path.endswith("/invite.json"):
                topic = stub.topics[int(self.path.split("/")[2])]
                topic["allowed_users"].append(form["user"])
                stub.added.append((topic["id"], form["user"]))
                return self._json(200, {"success": "OK"})
            if self.path == "/invites.json":
                stub.invites.append(form["email"])
                return self._json(200, {"id": len(stub.invites), "email": form["email"]})
            return self._json(404, {})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ---------------------------------------------------------------- a fake Slack


class SlackError(Exception):
    pass


class FakeSlack:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.channels: dict[str, str] = {}
        self.messages: list[dict] = []
        self.ephemerals: list[dict] = []
        self.dms: list[dict] = []
        self.people = {
            "US": {"id": "US", "profile": {"email": "sunada@uni.edu", "display_name": "Sunada"}},
            "UP": {"id": "UP", "profile": {"email": "pat@uni.edu", "display_name": "Pat"}},
            "UN": {"id": "UN", "profile": {"email": "new@uni.edu", "display_name": "Newcomer"}},
            "UA": {"id": "UA", "profile": {"email": "sunada-agent@forum"}},
        }
        self.group_members: dict[str, list[str]] = {}
        self._ts = 1000

    def _next_ts(self):
        self._ts += 1
        return f"{self._ts}.000100"

    def conversations_create(self, name, is_private):
        if name in self.channels:
            raise SlackError("The server responded with: {'ok': False, 'error': 'name_taken'}")
        self.channels[name] = f"C{len(self.channels) + 1}"
        return {"channel": {"id": self.channels[name]}}

    def conversations_list(self, **kwargs):
        return {"channels": [{"name": n, "id": i} for n, i in self.channels.items()], "response_metadata": {}}

    def conversations_info(self, channel):
        if channel.startswith("D"):
            # The app has no im:read, so Slack refuses this for a DM.
            raise SlackError("The server responded with: {'ok': False, 'error': 'missing_scope', 'needed': 'im:read'}")
        name = next(n for n, i in self.channels.items() if i == channel)
        return {"channel": {"id": channel, "name": name}}

    def conversations_rename(self, channel, name):
        if name in self.channels:
            raise SlackError("The server responded with: {'ok': False, 'error': 'name_taken'}")
        old = next(n for n, i in self.channels.items() if i == channel)
        self.channels[name] = self.channels.pop(old)
        self.calls.append(("rename", {"channel": channel, "name": name}))

    def conversations_join(self, channel):
        self.calls.append(("join", {"channel": channel}))

    def conversations_setPurpose(self, channel, purpose):  # noqa: N802 - slack_sdk's name
        self.calls.append(("purpose", {"channel": channel, "purpose": purpose}))

    def chat_postMessage(self, **kwargs):  # noqa: N802
        if kwargs["channel"].startswith("D"):
            self.dms.append(kwargs)
        else:
            self.messages.append(kwargs)
        return {"ts": self._next_ts()}

    def chat_postEphemeral(self, **kwargs):  # noqa: N802
        self.ephemerals.append(kwargs)

    def conversations_members(self, channel, **kwargs):
        return {"members": self.group_members[channel], "response_metadata": {}}

    def conversations_open(self, users):
        # One user is their DM with the bot; several are a group DM with it.
        ids = users.split(",")
        if len(ids) == 1:
            return {"channel": {"id": f"D{users}"}}
        channel = "G" + "".join(sorted(ids))
        self.group_members[channel] = sorted(ids) + ["UBOT"]
        return {"channel": {"id": channel}}

    def users_info(self, user):
        return {"user": self.people[user]}

    def users_lookupByEmail(self, email):  # noqa: N802
        for person in self.people.values():
            if person["profile"].get("email") == email:
                return {"user": person}
        raise SlackError("The server responded with: {'ok': False, 'error': 'users_not_found'}")


@pytest.fixture
def world(tmp_path):
    stub = StubForum()
    httpd = serve(stub)
    forum = bridge_mod.Forum(f"http://127.0.0.1:{httpd.server_address[1]}", "key", "system")
    slack = FakeSlack()
    # `newest`, so that the history set up here is the forum's past and every
    # test below starts from a bridge that has relayed none of it.
    bridge = bridge_mod.Bridge(forum, slack, bridge_mod.State(tmp_path / "state.db"), first_run="newest")
    bridge.bot_user_id = "UBOT"
    stub.topic(10, "Surface code thresholds", 1, "sunada")
    stub.post(10, "sunada", "Opening the discussion.")
    stub.topic(40, "Your agent sunada-agent is ready", None, "system", archetype="private_message",
               allowed_users=["system", "sunada"])
    stub.post(40, "system", "Here is the key.")
    bridge.poll()  # with `newest`, the first run only places the cursors
    yield stub, slack, bridge
    httpd.shutdown()


def replies(slack):
    return [m for m in slack.messages if m.get("thread_ts")]


# ---------------------------------------------------------------- forum to Slack


def test_a_new_forums_first_posts_reach_slack(tmp_path):
    """By default the first run starts from the forum's first post, so the
    topics the configuration made before the bridge -- the welcome topic among
    them -- and the credential messages are mirrored like any later post."""
    stub = StubForum()
    httpd = serve(stub)
    try:
        forum = bridge_mod.Forum(f"http://127.0.0.1:{httpd.server_address[1]}", "key", "system")
        slack = FakeSlack()
        bridge = bridge_mod.Bridge(forum, slack, bridge_mod.State(tmp_path / "state.db"))
        bridge.bot_user_id = "UBOT"
        stub.topic(10, "Welcome to Qupa Cafe", 1, "system")
        stub.post(10, "system", "Start here.")
        stub.topic(40, "Your agent sunada-agent is ready", None, "system", archetype="private_message",
                   allowed_users=["system", "sunada"])
        stub.post(40, "system", "Here is the key.")

        bridge.poll()

        parents = [m for m in slack.messages if not m.get("thread_ts")]
        assert any("Welcome to Qupa Cafe" in m["text"] for m in parents)
        # The credential message is considered too, rather than skipped: it
        # reaches Slack for whoever in it has an account there.
        assert bridge.state.cursor("pm_cursor") == stub.topics[40]["posts"][-1]["id"]
        # And once only: the cursor now stands after them.
        before = len(slack.messages)
        bridge.poll()
        assert len(slack.messages) == before
    finally:
        httpd.shutdown()


def test_first_run_relays_no_history_but_makes_channels(world):
    stub, slack, bridge = world
    assert replies(slack) == []
    # A channel for every category everybody can read, and none for staff.
    assert set(slack.channels) == {"qupa-research", "qupa-arxiv"}


def test_a_post_becomes_a_reply_in_its_topics_thread(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "I get **0.57%** for this [code](https://arxiv.org/abs/2)")
    assert bridge.poll() == 1

    parent = [m for m in slack.messages if not m.get("thread_ts")]
    assert len(parent) == 1 and "Surface code thresholds" in parent[0]["text"]
    (reply,) = replies(slack)
    assert reply["channel"] == slack.channels["qupa-research"]
    assert reply["thread_ts"] == bridge.state.thread_for(10)[1]
    assert reply["username"] == "pat"
    assert "*0.57%*" in reply["text"] and "<https://arxiv.org/abs/2|code>" in reply["text"]

    # A second post goes in the same thread, and nothing is relayed twice.
    stub.post(10, "sunada", "Agreed.")
    bridge.poll()
    assert bridge.poll() == 0
    assert len(replies(slack)) == 2
    assert len([m for m in slack.messages if not m.get("thread_ts")]) == 1


def test_a_topics_first_post_is_its_threads_parent(world):
    """The channel shows what a topic says without opening the thread: the
    parent is the title over the first post, under its author, and the first
    post is not repeated as a reply."""
    stub, slack, bridge = world
    stub.topic(11, "Leakage in the cat qubit", 1, "pat")
    stub.post(11, "pat", "Does **leakage** matter here?")
    assert bridge.poll() == 1

    (parent,) = [m for m in slack.messages if not m.get("thread_ts")]
    assert parent["username"] == "pat"
    assert "|Leakage in the cat qubit>*\nDoes *leakage* matter here?" in parent["text"]
    # The linked title is the link to the post, so no second link follows it.
    assert "View this post" not in parent["text"]
    assert parent["text"].endswith("\n_Reply in this thread to post to the topic._")
    assert replies(slack) == []
    assert bridge.state.thread_for(11)[0] == parent["channel"]

    stub.post(11, "sunada", "Only at large photon numbers.")
    bridge.poll()
    (reply,) = replies(slack)
    assert reply["thread_ts"] == bridge.state.thread_for(11)[1]


def test_an_agents_topic_mentions_its_researcher_above_the_title(world):
    stub, slack, bridge = world
    stub.topic(12, "A counterexample at distance 5", 1, "sunada-agent")
    stub.post(12, "sunada-agent", "Here it is.")
    bridge.poll()
    (parent,) = [m for m in slack.messages if not m.get("thread_ts")]
    assert parent["username"] == "sunada-agent"
    assert parent["text"].startswith("<@US>, your agent posted:\n*<")


def test_an_agents_post_mentions_its_researcher(world):
    stub, slack, bridge = world
    stub.post(10, "sunada-agent", "A counterexample at distance 5.")
    bridge.poll()
    (reply,) = replies(slack)
    assert reply["text"].startswith("<@US>, your agent posted")
    assert reply["username"] == "sunada-agent"

    # A person's post mentions nobody.
    stub.post(10, "pat", "Nice catch.")
    bridge.poll()
    assert "<@" not in replies(slack)[1]["text"]


def test_restricted_categories_private_messages_and_whispers_stay_out(world):
    stub, slack, bridge = world
    stub.topic(20, "Moderation notes", 2, "sunada")
    stub.post(20, "sunada", "Staff only.")
    stub.topic(21, "A private message", None, "sunada", archetype="private_message")
    stub.post(21, "sunada", "Just between us.")
    stub.post(10, "sunada", "A whisper.", post_type=4)
    assert bridge.poll() == 0
    assert slack.messages == []


def test_a_preprint_waits_for_its_first_reply(world):
    stub, slack, bridge = world
    stub.topic(30, "Magic states (arXiv:2610.00001)", 8, "arxiv-bot")
    stub.post(30, "arxiv-bot", "Abstract: magic.")
    assert bridge.poll() == 0
    assert slack.messages == []

    stub.post(30, "sunada-agent", "Relevant to our threshold work.")
    bridge.poll()
    (parent,) = [m for m in slack.messages if not m.get("thread_ts")]
    assert parent["username"] == "arxiv-bot" and "Abstract: magic." in parent["text"]
    assert parent["channel"] == slack.channels["qupa-arxiv"]
    (reply,) = replies(slack)
    assert reply["username"] == "sunada-agent"


def test_an_existing_channel_is_adopted(tmp_path, world):
    stub, slack, bridge = world
    # A lost state file: the channel exists in Slack and not in the bridge's memory.
    other = bridge_mod.Bridge(bridge.forum, slack, bridge_mod.State(tmp_path / "lost.db"))
    assert other.ensure_channel(stub.categories[1]) == slack.channels["qupa-research"]
    assert ("join", {"channel": slack.channels["qupa-research"]}) in slack.calls


def test_channels_are_renamed_when_the_prefix_changes(world):
    stub, slack, bridge = world
    research = slack.channels["qupa-research"]
    bridge.prefix = "lab-"
    bridge.sync_categories(force=True)
    # Renamed in place: the same channel, so its threads still map to topics.
    assert slack.channels["lab-research"] == research
    assert set(slack.channels) == {"lab-research", "lab-arxiv"}
    assert bridge.state.channel_for(1) == research

    # A name somebody already took by hand leaves the channel as it is.
    slack.channels["taken-research"] = "CX"
    bridge.prefix = "taken-"
    bridge.sync_categories(force=True)
    assert slack.channels["lab-research"] == research


# ---------------------------------------------------------------- conversations with the bot


def dm(user, text, ts, thread_ts=None, channel=None):
    event = {"type": "message", "channel_type": "im", "channel": channel or f"D{user}", "user": user,
             "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def in_thread(slack, channel, thread_ts):
    return [m for m in slack.dms + slack.messages if m["channel"] == channel and m.get("thread_ts") == thread_ts]


def test_a_dm_with_the_bot_is_a_private_message_with_your_agent(world):
    stub, slack, bridge = world
    outcome = bridge.handle_message(dm("US", "Can you rerun the threshold sweep at d=7?", "7000.1"))
    assert outcome.startswith("opened message")
    as_user, form = stub.created_as[-1]
    assert as_user == "sunada" and form["archetype"] == "private_message"
    assert form["target_recipients"] == "sunada-agent"
    assert form["title"] == "Can you rerun the threshold sweep at d=7?"
    topic_id = max(stub.topics)
    assert bridge.state.thread_for(topic_id) == ("DUS", "7000.1")
    # The bot says where it went, in the thread.
    (note,) = in_thread(slack, "DUS", "7000.1")
    assert "sunada-agent" in note["text"]

    # The agent answers on the forum, and the answer arrives in the thread
    # under the agent's name, with no mention: it is the reader's own DM.
    stub.post(topic_id, "sunada-agent", "Running it now; about an hour.")
    assert bridge.poll() == 1
    reply = in_thread(slack, "DUS", "7000.1")[-1]
    assert reply["username"] == "sunada-agent" and reply["text"].startswith("Running it now")
    assert "<@" not in reply["text"]

    # A reply in the thread continues the same message, and is not echoed.
    assert bridge.handle_message(dm("US", "Thanks, post the plot here.", "7000.5", thread_ts="7000.1")).startswith("posted as sunada")
    assert stub.created_as[-1] == ("sunada", {"topic_id": str(topic_id), "raw": "Thanks, post the plot here."})
    before = len(in_thread(slack, "DUS", "7000.1"))
    bridge.poll()
    assert len(in_thread(slack, "DUS", "7000.1")) == before


def test_a_group_dm_includes_everybody_and_all_their_agents(world):
    stub, slack, bridge = world
    slack.group_members["GSP"] = ["US", "UP", "UBOT", "UN"]
    event = {"type": "message", "channel_type": "mpim", "channel": "GSP", "user": "US",
             "text": "Shall we write the comparison up?", "ts": "7100.1"}
    assert bridge.handle_message(event).startswith("opened message")
    _, form = stub.created_as[-1]
    assert sorted(form["target_recipients"].split(",")) == ["pat", "pat-agent", "sunada-agent"]
    # The newcomer has no forum account, and the note says they are not in it.
    note = slack.messages[-1]
    assert note["thread_ts"] == "7100.1" and "Newcomer" in note["text"]

    # In a group, an agent's reply says whose agent it is.
    topic_id = max(stub.topics)
    stub.post(topic_id, "pat-agent", "Draft outline attached on the forum.")
    bridge.poll()
    assert slack.messages[-1]["text"].startswith("<@UP>, your agent posted")


def test_someone_added_on_the_forum_brings_their_agent(world):
    stub, slack, bridge = world
    bridge.handle_message(dm("US", "Can you rerun the threshold sweep at d=7?", "7000.1"))
    topic_id = max(stub.topics)
    stub.topics[topic_id]["allowed_users"].append("pat")
    stub.post(topic_id, "sunada-agent", "Adding pat, who ran the d=5 case.")
    bridge.poll()
    assert stub.added == [(topic_id, "pat-agent")]


def test_a_message_from_the_agent_reaches_its_researchers_dm(world):
    stub, slack, bridge = world
    stub.topic(41, "The d=7 sweep finished", None, "sunada-agent", archetype="private_message",
               allowed_users=["sunada-agent", "sunada"])
    stub.post(41, "sunada-agent", "Threshold holds at 0.57%.")
    assert bridge.poll() == 1
    # The parent is the message itself, under its author, with no mention in a DM.
    (parent,) = [m for m in slack.dms if m["channel"] == "DUS"]
    assert not parent.get("thread_ts") and parent["username"] == "sunada-agent"
    assert "|The d=7 sweep finished>*\nThreshold holds at 0.57%." in parent["text"]
    assert "<@" not in parent["text"]
    assert parent["text"].endswith("\n_A private message among sunada and their agents. Reply in this thread to answer it there._")
    assert "View this post" not in parent["text"]
    thread_ts = bridge.state.thread_for(41)[1]

    # Answered from the DM, the reply goes to the message as sunada.
    event = dm("US", "Great, write it up.", "8000.2", thread_ts=thread_ts)
    assert bridge.handle_message(event).startswith("posted as sunada")
    assert stub.created_as[-1] == ("sunada", {"topic_id": "41", "raw": "Great, write it up."})


def test_a_message_between_people_opens_a_group_dm_and_brings_their_agents(world):
    stub, slack, bridge = world
    stub.topic(41, "About your threshold result", None, "pat", archetype="private_message",
               allowed_users=["pat", "sunada"])
    stub.post(41, "pat", "Can we compare notes?")
    assert bridge.poll() == 1
    # Both agents are added on the forum.
    assert sorted(a for _, a in stub.added) == ["pat-agent", "sunada-agent"]
    # One thread, in a group DM of the two of them and the bot.
    channel, thread_ts = bridge.state.thread_for(41)
    assert slack.group_members[channel] == ["UP", "US", "UBOT"]
    (parent,) = [m for m in slack.messages if m["channel"] == channel]
    assert parent["username"] == "pat" and "Can we compare notes?" in parent["text"]
    assert "A private message among pat, sunada and their agents." in parent["text"]

    # A reply goes under it.
    stub.post(41, "sunada", "Yes, tomorrow.")
    bridge.poll()
    assert [m["username"] for m in slack.messages if m.get("thread_ts") == thread_ts] == ["sunada"]


def test_somebody_added_later_moves_the_thread_to_a_conversation_with_them(world):
    stub, slack, bridge = world
    stub.topic(41, "The d=7 sweep finished", None, "sunada-agent", archetype="private_message",
               allowed_users=["sunada-agent", "sunada"])
    stub.post(41, "sunada-agent", "Threshold holds at 0.57%.")
    bridge.poll()
    old_channel, old_ts = bridge.state.thread_for(41)
    assert old_channel == "DUS"

    stub.topics[41]["allowed_users"].append("pat")
    stub.post(41, "sunada", "Adding pat.")
    bridge.poll()
    channel, thread_ts = bridge.state.thread_for(41)
    assert channel == "GUPUS"
    # The new conversation opens with the first post too; outside a DM, the
    # agent's post mentions its researcher.
    parent, reply = [m for m in slack.messages if m["channel"] == channel]
    assert parent["username"] == "sunada-agent" and parent["text"].startswith("<@US>, your agent posted:\n*<")
    assert "Threshold holds at 0.57%." in parent["text"]
    assert reply["thread_ts"] == thread_ts and reply["username"] == "sunada"
    note = [m for m in slack.dms if m.get("thread_ts") == old_ts][-1]
    assert "continues in a conversation that includes them" in note["text"]
    assert stub.added == [(41, "pat-agent")]


def test_the_forums_own_messages_reach_slack_without_agents_or_keys(world):
    stub, slack, bridge = world
    key = "0123456789abcdef" * 4
    stub.post(40, "system", f"Your key is `{key}`.")
    assert bridge.poll() == 1
    assert stub.added == []
    reply = [m for m in slack.dms if m["channel"] == "DUS" and m.get("thread_ts")][-1]
    assert key not in reply["text"] and "possibly a credential" in reply["text"]


def test_a_message_with_nobody_on_slack_stays_on_the_forum(world):
    stub, slack, bridge = world
    stub.users["solo"] = {"id": 8, "username": "solo", "email": "solo@elsewhere.org", "active": True}
    stub.topic(41, "Just me", None, "solo", archetype="private_message", allowed_users=["solo"])
    stub.post(41, "solo", "A note to myself.")
    assert bridge.poll() == 0
    assert slack.dms == [] and slack.messages == []


def test_no_api_key_reaches_slack(world):
    stub, slack, bridge = world
    key = "0123456789abcdef" * 4
    bridge.handle_message(dm("US", "Where did my key go?", "7000.1"))
    stub.post(max(stub.topics), "sunada-agent", f"It was `{key}`, and you should rotate it.")
    bridge.poll()
    reply = in_thread(slack, "DUS", "7000.1")[-1]
    assert key not in reply["text"] and "possibly a credential" in reply["text"]
    # A 40-character commit hash is left alone.
    assert "possibly" not in bridge_mod.redact("a" * 40)


def test_whispers_in_a_conversation_stay_out(world):
    stub, slack, bridge = world
    bridge.handle_message(dm("US", "Can you rerun the threshold sweep at d=7?", "7000.1"))
    before = len(slack.dms)
    stub.post(max(stub.topics), "sunada-agent", "A whisper.", post_type=4)
    assert bridge.poll() == 0 and len(slack.dms) == before


def test_message_titles_read_as_subject_lines():
    assert bridge_mod.message_title("**Rerun** the d=7 sweep\nwith more shots") == "Rerun the d=7 sweep"
    assert bridge_mod.message_title("**Rerun** it\nwith more shots") == "From Slack: Rerun it"
    assert bridge_mod.message_title("hi") == "From Slack: hi"
    long = bridge_mod.message_title("word " * 40)
    assert len(long) <= 81 and long.endswith("…")


def test_a_repeated_title_gets_the_time(world):
    stub, slack, bridge = world
    bridge.handle_message(dm("US", "Can you rerun the threshold sweep at d=7?", "7000.1"))
    assert bridge.handle_message(dm("US", "Can you rerun the threshold sweep at d=7?", "7200.1")).startswith("opened message")
    assert stub.created_as[-1][1]["title"].startswith("Can you rerun the threshold sweep at d=7? (")


def test_somebody_with_no_agent_and_nobody_else_is_told(world):
    stub, slack, bridge = world
    stub.groups["agents"] = ["pat-agent"]
    assert bridge.handle_message(dm("US", "Anyone there?", "7300.1")) == "nobody to send to"
    assert stub.created_as == []


# ---------------------------------------------------------------- Slack to the forum


def reply_event(slack, bridge, user, text, topic_id=10, **extra):
    channel, thread_ts = bridge.state.thread_for(topic_id)
    return {"type": "message", "user": user, "text": text, "channel": channel,
            "ts": "2000.000001", "thread_ts": thread_ts, **extra}


def test_a_thread_reply_is_posted_as_the_slack_users_forum_account(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    before = len(slack.messages)

    outcome = bridge.handle_message(reply_event(slack, bridge, "US", "Thanks <@UP>, see <https://x.org|this>"))
    assert outcome.startswith("posted as sunada")
    as_user, form = stub.created_as[-1]
    assert as_user == "sunada"
    assert form["topic_id"] == "10" and form["raw"] == "Thanks @pat, see [this](https://x.org)"

    # The forum now has the post, and the poller does not put it back in Slack.
    assert bridge.poll() == 0
    assert len(slack.messages) == before


def test_someone_with_no_account_is_invited_once_and_told_by_dm(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()

    assert bridge.handle_message(reply_event(slack, bridge, "UN", "May I join?")) == "no account, invited"
    assert stub.invites == ["new@uni.edu"]
    (dm,) = slack.dms
    assert dm["channel"] == "DUN" and "new@uni.edu" in dm["text"] and "inbox" in dm["text"]

    # Posting again before redeeming does not send a second invitation.
    assert bridge.handle_message(reply_event(slack, bridge, "UN", "Hello?")) == "no account, invited already"
    assert stub.invites == ["new@uni.edu"]
    assert len(slack.dms) == 2

    # Once they have joined, the next reply is theirs.
    stub.users["newcomer"] = {"id": 9, "username": "newcomer", "email": "new@uni.edu", "active": True}
    assert bridge.handle_message(reply_event(slack, bridge, "UN", "Now I am here.")).startswith("posted as newcomer")


def test_a_slack_guest_is_not_invited(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    slack.people["UG"] = {"id": "UG", "is_restricted": True, "profile": {"email": "guest@else.org"}}
    assert bridge.handle_message(reply_event(slack, bridge, "UG", "Let me in.")) == "no account, a guest"
    assert stub.invites == []
    assert "admin" in slack.dms[-1]["text"]


def test_nobody_posts_as_an_agent(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    outcome = bridge.handle_message(reply_event(slack, bridge, "UA", "Impersonation attempt."))
    assert outcome.startswith("refused")
    assert stub.created_as == [] and stub.invites == []


def test_a_top_level_message_is_not_relayed(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    channel = slack.channels["qupa-research"]
    event = {"type": "message", "user": "US", "text": "Anyone around?", "channel": channel, "ts": "3000.1"}
    assert bridge.handle_message(event) == "top-level message, not relayed"
    assert stub.created_as == []
    assert slack.ephemerals[-1]["user"] == "US"


def test_bots_edits_and_unbridged_channels_are_ignored(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    base = reply_event(slack, bridge, "US", "text")
    assert bridge.handle_message({**base, "bot_id": "B1"}) == "not from a person"
    assert bridge.handle_message({**base, "user": None, "subtype": "message_changed"}) == "not from a person"
    assert bridge.handle_message({**base, "channel": "CELSEWHERE"}) == "not a bridged channel"
    assert stub.created_as == []


def test_a_refusal_is_told_to_the_person_who_wrote(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    outcome = bridge.handle_message(reply_event(slack, bridge, "US", "ok"))
    assert outcome == "refused: Body is too short (minimum is 5 characters)"
    assert "too short" in slack.ephemerals[-1]["text"]


def test_threads_and_posts_carry_their_links(world):
    """A thread opens with the topic's title, and every post ends with a link
    to it, unless it was cut and already ends with a link to the rest."""
    stub, slack, bridge = world
    stub.post(10, "pat", "I get 0.57% for this.")
    bridge.poll()

    (parent,) = [m for m in slack.messages if not m.get("thread_ts")]
    assert parent["text"].startswith("*<")
    assert "|Surface code thresholds>*\n" in parent["text"]
    (reply,) = replies(slack)
    assert reply["text"].endswith("|View this post>")

    # An agent's post carries the same link, after the mention of its researcher.
    stub.post(10, "sunada-agent", "A counterexample at distance 5.")
    bridge.poll()
    agent = replies(slack)[1]["text"]
    assert agent.startswith("<@US>, your agent posted:\n")
    assert agent.endswith("|View this post>")

    stub.post(10, "pat", "word " * 2000)
    bridge.poll()
    cut = replies(slack)[2]["text"]
    assert cut.endswith("|Read the rest>")
    assert "View this post" not in cut
