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


def test_to_slack_drops_a_code_fences_language():
    """Slack has no language on a code block and would show it as its first line."""
    raw = "Run:\n\n```text\nqupa-install\n```\n\n```\nplain\n```\n\nand `inline` stays."
    assert bridge_mod.to_slack(raw, "https://f/t/x/1/1") == "Run:\n\n```\nqupa-install\n```\n\n```\nplain\n```\n\nand `inline` stays."


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
    # One elsewhere is a link, since the bridge does not fetch it.
    assert bridge_mod.to_slack("![Fig. 2](https://arxiv.org/html/x/Fig2.png)", "u") == (
        "<https://arxiv.org/html/x/Fig2.png|image: Fig. 2>"
    )
    assert bridge_mod.to_slack("data: [run.csv|attachment](upload://x.csv) (2 KB)", "u") == "data: run.csv (2 KB)"


def test_to_slack_keeps_a_post_of_the_longest_length_whole():
    raw = ("word " * 799) + "last"  # 3999 characters, under max_post_length
    assert bridge_mod.to_slack(raw, "https://f/t/x/1/3") == raw


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
        self.edited_as: list[tuple[str, int, str]] = []
        self.next_post = 100
        # upload:// reference -> bytes; an image whose bytes are None is known
        # to the forum but fails to download.
        self.uploads: dict[str, bytes | None] = {}
        self.downloaded_as: list[str | None] = []
        # Set to a host to serve uploads as secure_uploads does: a redirect to
        # an address signed for the object store.
        self.object_store: str | None = None

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
            if path.startswith("/secure-uploads/original/"):
                self.send_response(302)
                self.send_header("Location", f"{stub.object_store}/bucket/{path.rsplit('/', 1)[-1]}?X-Amz-Signature=s")
                self.end_headers()
                return None
            if path.startswith("/uploads/default/original/"):
                stub.downloaded_as.append(self.headers.get("Api-Key"))
                content = stub.uploads.get("upload://" + path.rsplit("/", 1)[-1])
                if content is None:
                    return self._json(404, {})
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return None
            if path == "/admin/users/list/all.json":
                return self._json(200, [u for u in stub.users.values() if u["email"] == q.get("email", "").lower()])
            return self._json(404, {})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            parsed = urllib.parse.parse_qs(self.rfile.read(length).decode())
            form = {k: v[0] for k, v in parsed.items()}
            if self.path == "/uploads/lookup-urls.json":
                # As uploads#lookup_urls: the known ones, with the host the
                # forum names itself by, which is not the one the bridge uses.
                prefix = "/secure-uploads" if stub.object_store else "/uploads/default"
                return self._json(200, [
                    {"short_url": short, "url": f"//forum.example{prefix}/original/1X/{short[len('upload://'):]}"}
                    for short in parsed.get("short_urls[]", []) if short in stub.uploads
                ])
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

        def do_PUT(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
            if self.path.startswith("/posts/"):
                post_id = int(self.path.split("/")[2].removesuffix(".json"))
                post = next(p for p in stub.posts if p["id"] == post_id)
                if post["username"] != self.headers.get("Api-Username"):
                    return self._json(403, {"errors": ["You are not permitted to edit this post."]})
                if post.get("past_edit_limit"):
                    return self._json(422, {"errors": ["You can no longer edit this post."]})
                post["raw"] = form["post[raw]"]
                stub.edited_as.append((self.headers.get("Api-Username"), post_id, form["post[raw]"]))
                return self._json(200, {"post": post})
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
        # Every message the bot has in Slack, by its ts.
        self.sent: dict[str, dict] = {}
        self.files: list[dict] = []
        # Set to an error to refuse uploads, as a token without files:write does.
        self.refuse_files: str | None = None
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
        ts = self._next_ts()
        self.sent[ts] = kwargs
        if kwargs["channel"].startswith("D"):
            self.dms.append(kwargs)
        else:
            self.messages.append(kwargs)
        return {"ts": ts}

    def files_upload_v2(self, **kwargs):
        if self.refuse_files:
            raise SlackError(f"The server responded with: {{'ok': False, 'error': '{self.refuse_files}'}}")
        self.files.append(kwargs)
        return {"files": [{"id": f"F{len(self.files)}"}]}

    def chat_update(self, channel, ts, text):  # noqa: N802
        self.sent[ts]["text"] = text
        self.calls.append(("update", {"channel": channel, "ts": ts, "text": text}))

    def chat_delete(self, channel, ts):  # noqa: N802
        self.sent.pop(ts)
        self.calls.append(("delete", {"channel": channel, "ts": ts}))

    def conversations_replies(self, channel, ts, cursor=None, limit=200):
        thread = [
            {"ts": t, "text": m["text"], "bot_id": "B1"}
            for t, m in self.sent.items()
            if m["channel"] == channel and (t == ts or m.get("thread_ts") == ts)
        ]
        return {"messages": thread, "response_metadata": {}}

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
    topics the configuration made before the bridge and the credential messages
    are mirrored like any later post."""
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
    assert parent["text"].endswith("\n\n_Reply in this thread to post to the topic._")
    assert replies(slack) == []
    assert bridge.state.thread_for(11)[0] == parent["channel"]

    stub.post(11, "sunada", "Only at large photon numbers.")
    bridge.poll()
    (reply,) = replies(slack)
    assert reply["thread_ts"] == bridge.state.thread_for(11)[1]


PNG = b"\x89PNG\r\n\x1a\nfigure two"


def test_a_posts_uploaded_image_follows_it_into_the_thread(world):
    """Slack cannot fetch an image from a forum that needs a login, so the
    bridge carries the bytes, read with its own key from the host it uses."""
    stub, slack, bridge = world
    stub.uploads["upload://aBc.png"] = PNG
    stub.post(10, "sunada-agent", "Figure 2: ![Fig2.png|690x388](upload://aBc.png) and again ![Fig2.png](upload://aBc.png)")
    assert bridge.poll() == 1

    (reply,) = replies(slack)
    assert "[image: Fig2.png]" in reply["text"]
    (upload,) = slack.files
    assert upload["channel"] == reply["channel"] and upload["thread_ts"] == reply["thread_ts"]
    assert upload["file_uploads"] == [{"content": PNG, "filename": "Fig2.png", "title": "Fig2.png"}]
    assert stub.downloaded_as == ["key"]


def test_the_key_stays_behind_at_the_object_stores_redirect(world):
    """With secure_uploads the forum redirects to a signed address in the
    object store, and the bridge's key is the most powerful on the host."""
    stub, slack, bridge = world
    seen = []

    class Store(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)

        def log_message(self, *args):
            pass

    store = ThreadingHTTPServer(("127.0.0.1", 0), Store)
    threading.Thread(target=store.serve_forever, daemon=True).start()
    try:
        stub.object_store = f"http://127.0.0.1:{store.server_address[1]}"
        stub.uploads["upload://sEc.png"] = PNG
        stub.post(10, "pat", "![plot.png](upload://sEc.png)")
        bridge.poll()
    finally:
        store.shutdown()

    assert slack.files[0]["file_uploads"][0]["content"] == PNG
    assert len(seen) == 1 and not {k.lower() for k in seen[0]} & {"api-key", "api-username"}


def test_an_image_in_a_topics_first_post_follows_its_parent(world):
    stub, slack, bridge = world
    stub.uploads["upload://xYz.png"] = PNG
    stub.topic(11, "A plot", 1, "pat")
    stub.post(11, "pat", "Here: ![](upload://xYz.png)")
    bridge.poll()

    (parent,) = [m for m in slack.messages if not m.get("thread_ts")]
    (upload,) = slack.files
    assert upload["thread_ts"] == bridge.state.thread_for(11)[1]
    # With no name of its own, it goes under the stored one.
    assert upload["file_uploads"][0]["filename"] == "xYz.png"
    assert "[image: untitled]" in parent["text"]


def test_an_image_elsewhere_is_linked_not_fetched(world):
    """A post names any address it likes, and the host is no place to fetch it from."""
    stub, slack, bridge = world
    stub.post(10, "pat", "![Fig. 2](https://arxiv.org/html/x/Fig2.png)")
    bridge.poll()

    assert slack.files == [] and stub.downloaded_as == []
    assert "<https://arxiv.org/html/x/Fig2.png|image: Fig. 2>" in replies(slack)[0]["text"]


@pytest.mark.parametrize("failure", ["download", "slack"])
def test_an_image_that_cannot_be_carried_costs_only_the_image(world, failure):
    """The text is in Slack by then, and relaying the post again would repeat it."""
    stub, slack, bridge = world
    stub.uploads["upload://aBc.png"] = None if failure == "download" else PNG
    slack.refuse_files = "missing_scope" if failure == "slack" else None
    stub.post(10, "pat", "See ![plot](upload://aBc.png)")

    assert bridge.poll() == 1
    assert len(replies(slack)) == 1 and slack.files == []
    assert bridge.poll() == 0
    assert len(replies(slack)) == 1


def test_an_image_in_a_dm_follows_it(world):
    stub, slack, bridge = world
    stub.uploads["upload://dM.png"] = PNG
    stub.topic(41, "The d=7 sweep finished", None, "sunada-agent", archetype="private_message",
               allowed_users=["sunada-agent", "sunada"])
    stub.post(41, "sunada-agent", "Threshold plot: ![sweep.png](upload://dM.png)")
    bridge.poll()

    (upload,) = slack.files
    assert upload["channel"] == "DUS" and upload["thread_ts"] == bridge.state.thread_for(41)[1]


def test_a_link_only_topic_is_a_link_and_nothing_else(world):
    """The policy and interests topics are edited often, so a copy in Slack
    would soon say something they no longer do."""
    stub, slack, bridge = world
    stub.topic(13, "Agent interests: one post per researcher", 1, "system")
    stub.post(13, "system", "One post per researcher.")
    bridge.link_only = {13}
    stub.post(13, "sunada-agent", "**Researcher:** sunada")
    bridge.poll()
    channel, parent = bridge.state.thread_for(13)
    (link,) = [m for m in slack.messages if m["channel"] == channel and "interests" in m["text"]]
    assert "/t/agent-interests:-one-post-per-researcher/13|Agent interests" in link["text"]
    assert "not copied here" in link["text"]
    assert "Researcher:" not in link["text"] and replies(slack) == []

    # A reply in its thread is pointed at the forum, not posted.
    event = reply_event(slack, bridge, "US", "My profile needs updating.", topic_id=13)
    assert bridge.handle_message(event) == "a topic read on the forum, not posted"
    assert stub.created_as == [] and "/t/13" in slack.ephemerals[-1]["text"]


def test_a_link_only_topics_old_copies_are_cut_back_to_the_link(world):
    stub, slack, bridge = world
    stub.topic(11, "Agent policy (pinned, wiki)", 1, "system")
    stub.post(11, "system", "The policy, version one.")
    stub.post(11, "sunada-agent", "A question about rule 3.")
    bridge.poll()
    channel, parent = bridge.state.thread_for(11)
    assert len([t for t, m in slack.sent.items() if m["channel"] == channel]) == 2

    bridge.link_only = {11}
    bridge.sync_categories(force=True)
    assert [t for t, m in slack.sent.items() if m["channel"] == channel] == [parent]
    assert "not copied here" in slack.sent[parent]["text"] and "version one" not in slack.sent[parent]["text"]
    # Done once: the next sync leaves the thread alone.
    calls = len(slack.calls)
    bridge.sync_categories(force=True)
    assert [c for c in slack.calls[calls:] if c[0] in ("update", "delete")] == []


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


def test_a_group_dm_is_not_carried(world):
    stub, slack, bridge = world
    event = {"type": "message", "channel_type": "mpim", "channel": "GSP", "user": "US",
             "text": "Shall we write the comparison up?", "ts": "7100.1"}
    assert bridge.handle_message(event) == "group DM, not relayed"
    assert stub.created_as == []
    assert "message this app directly" in slack.ephemerals[-1]["text"]


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
    assert parent["text"].endswith("\n\n_A private message on the forum. Reply in this thread to post to it._")
    assert "View this post" not in parent["text"]
    thread_ts = bridge.state.thread_for(41)[1]

    # Answered from the DM, the reply goes to the message as sunada.
    event = dm("US", "Great, write it up.", "8000.2", thread_ts=thread_ts)
    assert bridge.handle_message(event).startswith("posted as sunada")
    assert stub.created_as[-1] == ("sunada", {"topic_id": "41", "raw": "Great, write it up."})


def test_a_message_between_people_stays_on_the_forum_and_brings_their_agents(world):
    stub, slack, bridge = world
    stub.topic(41, "About your threshold result", None, "pat", archetype="private_message",
               allowed_users=["pat", "sunada"])
    stub.post(41, "pat", "Can we compare notes?")
    assert bridge.poll() == 0
    assert sorted(a for _, a in stub.added) == ["pat-agent", "sunada-agent"]
    assert slack.dms == [] and slack.messages == []


def test_a_message_someone_joins_stays_on_the_forum_from_then_on(world):
    stub, slack, bridge = world
    stub.topic(41, "The d=7 sweep finished", None, "sunada-agent", archetype="private_message",
               allowed_users=["sunada-agent", "sunada"])
    stub.post(41, "sunada-agent", "Threshold holds at 0.57%.")
    assert bridge.poll() == 1
    stub.topics[41]["allowed_users"].append("pat")
    stub.post(41, "sunada", "Adding pat.")
    assert bridge.poll() == 0
    assert stub.added == [(41, "pat-agent")]


def test_an_agent_provisioned_a_moment_ago_is_known_as_one(world):
    """The forum's first message to a new agent arrives seconds after the
    account, while the bridge's list of agents is older than that."""
    stub, slack, bridge = world
    assert bridge.is_agent("sunada-agent")
    stub.users["new-agent"] = {"id": 9, "username": "new-agent", "email": "new-agent@forum", "active": True}
    stub.groups["agents"].append("new-agent")
    reads = []
    group_members = bridge.forum.group_members
    bridge.forum.group_members = lambda group: reads.append(group) or group_members(group)
    # Cached a minute ago, which is within the ten-minute life of the list.
    bridge._groups["agents"].at -= 60
    assert bridge.is_agent("new-agent") and reads == ["agents"]
    # A person is still a person, and asking again soon reads the group no more.
    assert not bridge.is_agent("sunada") and not bridge.is_agent("pat")
    assert reads == ["agents"]


def test_the_forums_own_messages_reach_slack_without_agents_or_keys(world):
    stub, slack, bridge = world
    key = "0123456789abcdef" * 4
    stub.post(40, "system", f"Your key is `{key}`.")
    assert bridge.poll() == 1
    assert stub.added == []
    reply = [m for m in slack.dms if m["channel"] == "DUS" and m.get("thread_ts")][-1]
    assert key not in reply["text"] and "possibly a credential" in reply["text"]


SETUP_CODE = "qupa-setup-aHR0cHM6Ly9mb3J1bS5leGFtcGxlLm9yZwowMTIzNDU2Nzg5YWJjZGVm"


def test_a_setup_code_reaches_its_researchers_dm_and_nowhere_else(world):
    """The forum's own message to one person, the credential message, carries
    their setup code to their DM. Anywhere else a setup code is replaced."""
    stub, slack, bridge = world
    stub.post(40, "system", f"Your setup code: `{SETUP_CODE}`")
    bridge.poll()
    credential = [m for m in slack.dms if m["channel"] == "DUS"][-1]
    assert SETUP_CODE in credential["text"]

    stub.post(10, "sunada", f"Oops, I pasted mine: {SETUP_CODE}")
    bridge.handle_message(dm("US", "Can you rerun the threshold sweep at d=7?", "7000.1"))
    stub.post(max(stub.topics), "sunada-agent", f"Yours is {SETUP_CODE}; rotate it.")
    bridge.poll()
    for message in replies(slack) + in_thread(slack, "DUS", "7000.1"):
        assert SETUP_CODE not in message["text"]
    assert "a setup code: read it on the forum" in replies(slack)[-1]["text"]


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


def test_somebody_with_no_agent_is_told(world):
    stub, slack, bridge = world
    stub.groups["agents"] = ["pat-agent"]
    assert bridge.handle_message(dm("US", "Anyone there?", "7300.1")) == "no agent to send to"
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


def test_bots_their_edits_and_unbridged_channels_are_ignored(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    base = reply_event(slack, bridge, "US", "text")
    assert bridge.handle_message({**base, "bot_id": "B1"}) == "not from a person"
    edited = {**base, "user": None, "subtype": "message_changed", "message": {"bot_id": "B1", "text": "x"}}
    assert bridge.handle_message(edited) == "not an edit by a person"
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
    """A thread opens with the topic's title, and every reply, whatever its
    length, is posted whole and ends with a link to it."""
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

    stub.post(10, "pat", ("word " * 799) + "last")
    bridge.poll()
    long = replies(slack)[2]["text"]
    assert long.startswith(("word " * 799) + "last\n<")
    assert long.endswith("|View this post>")


# ---------------------------------------------------------------- Slack edits


def written_in_slack(stub, slack, bridge, text="Written in Slack, at length."):
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    event = reply_event(slack, bridge, "US", text)
    assert bridge.handle_message(event).startswith("posted as sunada")
    return stub.posts[-1], event


def edit_event(event, text, **message):
    edited = {
        "type": "message", "subtype": "message_changed", "channel": event["channel"],
        "message": {"user": event["user"], "text": text, "ts": event["ts"], **message},
    }
    if event.get("thread_ts"):
        edited["message"]["thread_ts"] = event["thread_ts"]
    if event.get("channel_type"):
        edited["channel_type"] = event["channel_type"]
    return edited


def test_an_edit_in_slack_edits_the_post_as_its_writer(world):
    stub, slack, bridge = world
    post, event = written_in_slack(stub, slack, bridge)
    outcome = bridge.handle_message(edit_event(event, "Corrected in Slack, see <@UP>."))
    assert outcome == f"edited post {post['id']} as sunada"
    assert stub.edited_as == [("sunada", post["id"], "Corrected in Slack, see @pat.")]
    # The same text again, as Slack sends when a link preview arrives.
    assert bridge.handle_message(edit_event(event, "Corrected in Slack, see <@UP>.")) == "unchanged"
    assert len(stub.edited_as) == 1


def test_an_edit_to_a_message_opening_a_private_message_edits_it(world):
    stub, slack, bridge = world
    event = dm("US", "Can you rerun the threshold sweep at d=7?", "7000.1")
    bridge.handle_message(event)
    post = stub.posts[-1]
    assert bridge.handle_message(edit_event(event, "Can you rerun the sweep at d=9?")) == f"edited post {post['id']} as sunada"
    assert stub.edited_as[-1][2] == "Can you rerun the sweep at d=9?"


def test_a_link_preview_is_not_an_edit(world):
    stub, slack, bridge = world
    _, event = written_in_slack(stub, slack, bridge)
    assert bridge.handle_message(edit_event(event, event["text"], attachments=[{"title": "preview"}])) == "unchanged"
    assert stub.edited_as == []


def test_an_edit_the_forum_refuses_is_told_to_its_writer(world):
    stub, slack, bridge = world
    post, event = written_in_slack(stub, slack, bridge)
    post["past_edit_limit"] = True
    outcome = bridge.handle_message(edit_event(event, "Corrected in Slack."))
    assert outcome == "refused: You can no longer edit this post." and stub.edited_as == []
    told = slack.ephemerals[-1]
    assert told["user"] == "US" and "did not accept that edit" in told["text"]
    # Refused, the edit is not remembered, so making it again tries again.
    post["past_edit_limit"] = False
    assert bridge.handle_message(edit_event(event, "Corrected in Slack.")).startswith("edited post")


def test_an_edit_to_a_message_not_on_the_forum_changes_nothing(world):
    stub, slack, bridge = world
    stub.post(10, "pat", "Starting the thread.")
    bridge.poll()
    event = {"channel": bridge.state.thread_for(10)[0], "user": "US", "ts": "3000.000001"}
    assert bridge.handle_message(edit_event(event, "anything")) == "an edit to a message that is not on the forum"
    assert stub.edited_as == []
