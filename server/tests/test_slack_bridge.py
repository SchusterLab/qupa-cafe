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


def test_to_slack_turns_images_and_attachments_into_their_names():
    assert bridge_mod.to_slack("see ![plot|690x388](upload://abc.png)", "u") == "see [image: plot]"
    assert bridge_mod.to_slack("data: [run.csv|attachment](upload://x.csv) (2 KB)", "u") == "data: run.csv (2 KB)"


def test_to_slack_cuts_long_posts_with_a_link():
    text = bridge_mod.to_slack("word " * 2000, "https://f/t/x/1/3")
    assert len(text) < bridge_mod.MAX_SLACK_TEXT + 100
    assert text.endswith("<https://f/t/x/1/3|continued on the forum>")


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
            "arxiv-bot": {"id": 5, "username": "arxiv-bot", "email": "arxiv-bot@forum", "active": True},
        }
        self.groups = {"agents": ["sunada-agent"], "feeds": ["arxiv-bot"]}
        self.topics: dict[int, dict] = {}
        self.posts: list[dict] = []
        self.invites: list[str] = []
        self.created_as: list[tuple[str, dict]] = []
        self.next_post = 100

    def topic(self, topic_id, title, category_id, opener, archetype="regular"):
        self.topics[topic_id] = {
            "id": topic_id, "title": title, "slug": title.lower().replace(" ", "-"),
            "category_id": category_id, "archetype": archetype, "opener": opener, "posts": [],
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

            if path == "/posts.json":
                # Public posts only, newest first, 50 a page, as posts#latest.
                public = [
                    p for p in stub.posts
                    if stub.topics[p["topic_id"]]["archetype"] == "regular"
                    and ("before" not in q or p["id"] < int(q["before"]))
                ]
                return self._json(200, {"latest_posts": sorted(public, key=lambda p: -p["id"])[:50]})
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
                    "details": {"created_by": {"username": topic["opener"]}},
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
                post = stub.post(int(form["topic_id"]), as_user, form["raw"])
                return self._json(200, post)
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

    def conversations_open(self, users):
        return {"channel": {"id": f"D{users}"}}

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
    bridge = bridge_mod.Bridge(forum, slack, bridge_mod.State(tmp_path / "state.db"))
    bridge.bot_user_id = "UBOT"
    stub.topic(10, "Surface code thresholds", 1, "sunada")
    stub.post(10, "sunada", "Opening the discussion.")
    bridge.poll()  # the first run only places the cursor
    yield stub, slack, bridge
    httpd.shutdown()


def replies(slack):
    return [m for m in slack.messages if m.get("thread_ts")]


# ---------------------------------------------------------------- forum to Slack


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
    first, second = replies(slack)
    assert first["username"] == "arxiv-bot" and "Abstract: magic." in first["text"]
    assert second["username"] == "sunada-agent"
    assert first["channel"] == slack.channels["qupa-arxiv"]


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
