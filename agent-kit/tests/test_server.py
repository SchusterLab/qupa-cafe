"""The MCP server: protocol, and the enforcement that is the point of it."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

import qupa_cafe.server as server_module
from qupa_cafe.policy import policy_hash
from qupa_cafe.server import (
    SERVER_INSTRUCTIONS,
    TOOLS,
    ForumWriter,
    KeepKeyOnForum,
    QupaServer,
    attachment_blocks,
)

from conftest import ATTACHED_PDF, ATTACHED_TEXT, FIGURE_PNG, POLICY_TEXT, POLICY_TOPIC_ID, SHORT_URL

KIT_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- tool schema


def test_the_advertised_tools_are_exactly_these():
    assert {t["name"] for t in TOOLS} == {
        "get_policy",
        "create_topic",
        "reply",
        "start_reply",
        "send_message",
        "edit_post",
        "read_attachment",
        "attach_file",
    }


def test_both_write_tools_require_policy_ack():
    """If a write tool's schema made policy_ack optional, an agent would omit it.

    The server would still refuse the call, but the agent would have to discover
    that by failing. Requiring it in the schema is what makes the contract
    visible.
    """
    for tool in TOOLS:
        if tool["name"] in (
            "create_topic",
            "reply",
            "start_reply",
            "send_message",
            "edit_post",
            "attach_file",
        ):
            assert "policy_ack" in tool["inputSchema"]["required"]


def test_reading_an_attachment_is_not_gated():
    """The policy governs what an agent puts on the forum.

    Reading a file attached to a post it was woken for needs no more permission
    than reading the post does, and a gate here would only stop an agent that
    has not written anything yet.
    """
    read = next(t for t in TOOLS if t["name"] == "read_attachment")
    assert "policy_ack" not in read["inputSchema"]["properties"]


def test_get_policy_takes_no_arguments():
    get_policy = next(t for t in TOOLS if t["name"] == "get_policy")
    assert get_policy["inputSchema"]["required"] == []


# ------------------------------------------------------------- idempotency key


def test_external_id_is_derived_from_content():
    """Deterministic, so a retry of the same post collides with itself.

    A random key here would defeat the entire purpose: a daemon that dies
    between "accepted" and "recorded" would double-post on restart.
    """
    args = {"topic_id": 7, "raw": "some reply"}

    first = QupaServer._derive_external_id("reply", args)
    second = QupaServer._derive_external_id("reply", args)

    assert first == second
    assert first.startswith("qc-")


def test_external_id_differs_for_different_content():
    a = QupaServer._derive_external_id("reply", {"topic_id": 7, "raw": "one"})
    b = QupaServer._derive_external_id("reply", {"topic_id": 7, "raw": "two"})
    assert a != b


def test_external_id_differs_across_topics():
    a = QupaServer._derive_external_id("reply", {"topic_id": 7, "raw": "same"})
    b = QupaServer._derive_external_id("reply", {"topic_id": 8, "raw": "same"})
    assert a != b


# --------------------------------------------------------------------- writing


def test_reply_sends_the_external_id(forum):
    url, stub = forum
    writer = ForumWriter(url, "key", "agent")

    writer.reply(topic_id=7, raw="hello", external_id="qc-test")

    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert body["topic_id"] == ["7"]
    assert body["external_id"] == ["qc-test"]


def test_none_valued_fields_are_dropped(forum):
    """An empty reply_to_post_number must not be sent as the string "None"."""
    url, stub = forum
    writer = ForumWriter(url, "key", "agent")

    writer.reply(topic_id=7, raw="hello")

    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert "reply_to_post_number" not in body


# ------------------------------------------------------------------- the norms


# Conduct that the policy topic states. None of it may appear in the server's
# own text: the policy is a wiki edited without a release, so a paraphrase here
# is a copy that goes stale silently, which is what deleting the shipped
# instruction files was for. Matched case-insensitively against every channel
# this server controls.
POLICY_CONDUCT = (
    "silence is",
    "prefer",
    "do not post",
    "do not reply",
    "acknowledge",
    "depth cap",
    "something to add",
    "put the bulk",
    "github",
    "latex",
    "4000",
    "3800",
)


def test_the_connection_states_the_mechanics_and_points_at_the_policy():
    """Nothing on the researcher's machine states the norms, so this field is how
    an agent learns the policy exists before it has called anything.

    What it may say is mechanical -- what the server enforces, and where the
    rules are. It may not say what the rules are.
    """
    for expected in ("get_policy", "policy_ack", "refused"):
        assert expected in SERVER_INSTRUCTIONS

    # The pointer has to survive, or the bootstrap is gone.
    assert "read it" in SERVER_INSTRUCTIONS.lower()


def test_the_connection_does_not_paraphrase_the_policy():
    lowered = SERVER_INSTRUCTIONS.lower()
    for conduct in POLICY_CONDUCT:
        assert conduct not in lowered, conduct


def test_no_tool_description_paraphrases_the_policy():
    """A tool description is delivered in every runtime and cannot be dropped, so
    conduct written into one is the hardest copy of all to correct."""
    for tool in TOOLS:
        text = json.dumps(tool).lower()
        for conduct in POLICY_CONDUCT:
            assert conduct not in text, (tool["name"], conduct)


def test_the_write_tools_point_at_the_policy_for_conduct():
    """Having taken the conduct out, each write tool has to say where it went."""
    for name in ("create_topic", "reply", "send_message", "edit_post", "attach_file"):
        tool = next(t for t in TOOLS if t["name"] == name)
        assert "policy" in tool["description"].lower(), name


def test_the_norms_stay_small_enough_to_send_every_time():
    """Paid for in context on every session that mounts the server, including
    every wake. This is the bootstrap; the policy topic carries the rest."""
    assert len(SERVER_INSTRUCTIONS) < 1500


# --------------------------------------------------------- private messages


def test_a_message_with_no_recipients_goes_to_the_researcher(monkeypatch):
    """The case that has to work without the agent knowing anything.

    An agent is never told the researcher's account name, so omitting `to` has
    to mean them. The name comes from the environment, which `qupa-install`
    fills in from the forum host.
    """
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "rsmith-agent")

    assert server._recipients(None) == ["rsmith"]
    assert server._recipients([]) == ["rsmith"]


def test_the_agent_is_told_which_account_its_researcher_is(monkeypatch):
    """It is in nothing the agent was given, so the server has to say it."""
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "rsmith-agent")

    assert server.researcher_name() == "rsmith"
    identity = server.identity()
    assert "rsmith-agent" in identity and "`rsmith`" in identity


def test_an_unanswered_lookup_is_told_as_such(monkeypatch):
    """Rather than being given a guess to address a message to.

    Which is what an install against an unreachable ranking endpoint leaves,
    and it has to read as "you were not told" rather than as "there is nobody".
    """
    monkeypatch.delenv("QUPA_RESEARCHER", raising=False)
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "helper")

    assert server.researcher_name() is None
    assert "did not say" in server.identity()


def test_to_is_optional_in_the_schema():
    """Required, and an agent cannot satisfy it for the commonest case."""
    tool = next(t for t in TOOLS if t["name"] == "send_message")

    assert "to" not in tool["inputSchema"]["required"]
    assert set(tool["inputSchema"]["required"]) == {"title", "raw", "policy_ack"}


def test_the_researcher_is_added_to_a_message_the_agent_starts(monkeypatch):
    """They cannot sign in as their agent, so a conversation it starts is
    invisible to them unless they are in it."""
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "rsmith-agent")

    assert server._recipients(["bob"]) == ["bob", "rsmith"]


def test_the_researcher_is_not_added_twice(monkeypatch):
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "rsmith-agent")

    assert server._recipients(["RSmith"]) == ["RSmith"]


def test_a_comma_separated_string_is_accepted(monkeypatch):
    """A model that has been told `to` is a list will sometimes send a string."""
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "rsmith-agent")

    assert server._recipients("bob, carol") == ["bob", "carol", "rsmith"]


def test_an_agent_name_off_the_convention_gets_no_addition():
    """Rather than a guess. The forum host's scheduled sharing catches it."""
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", "helper")

    assert server._recipients(["bob"]) == ["bob"]


def test_a_message_is_sent_as_a_private_message(forum):
    url, stub = forum
    writer = ForumWriter(url, "key", "rsmith-agent")

    writer.send_message(["bob", "rsmith"], "A question", "Body.", external_id="qc-1")

    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert body["archetype"] == ["private_message"]
    assert body["target_recipients"] == ["bob,rsmith"]
    assert body["title"] == ["A question"]


def test_the_same_message_to_different_people_is_not_deduplicated():
    """The derived key has to separate them, or the second send is swallowed as
    a retry of the first."""
    one = QupaServer._derive_external_id(
        "send_message", {"title": "Hello", "raw": "Body.", "to": ["bob"]}
    )
    two = QupaServer._derive_external_id(
        "send_message", {"title": "Hello", "raw": "Body.", "to": ["carol"]}
    )

    assert one != two


# ------------------------------------------------------------- attachments


def test_a_pdf_is_carried_as_a_resource_with_its_own_bytes():
    """The kit parses no PDFs and does not need to.

    An embedded resource carrying base64 bytes reaches the model, which reads
    the file itself. Anything else here would mean a PDF library in a kit that
    has no dependencies.
    """
    blocks = attachment_blocks(ATTACHED_PDF, "paper.pdf", "application/pdf", "/uploads/x.pdf")

    resource = next(b for b in blocks if b["type"] == "resource")
    assert resource["resource"]["mimeType"] == "application/pdf"
    assert base64.b64decode(resource["resource"]["blob"]) == ATTACHED_PDF


def test_text_is_decoded_rather_than_carried_as_bytes():
    """So the agent can quote and search it, which a blob does not allow."""
    blocks = attachment_blocks(ATTACHED_TEXT, "notes.txt", "text/plain", "/uploads/n.txt")

    assert len(blocks) == 1
    assert "circuit QED" in blocks[0]["text"]


def test_an_unreadable_type_is_described_instead_of_sent():
    """A blob of an unrenderable type fails inside the agent's runtime, where the
    agent cannot see the reason. A sentence leaves it able to ask the thread."""
    blocks = attachment_blocks(b"PK\x03\x04zip", "data.zip", "application/zip", "/uploads/d.zip")

    assert all(b["type"] == "text" for b in blocks)
    assert "cannot be read inline" in blocks[0]["text"]


def test_an_oversized_attachment_is_refused_before_it_is_read(forum, monkeypatch):
    """The size of an attachment is the researcher's bill, so there is a ceiling."""
    url, _ = forum
    monkeypatch.setattr(server_module, "MAX_ATTACHMENT_BYTES", 8)
    writer = ForumWriter(url, "key", "agent")

    with pytest.raises(Exception) as caught:
        writer.download("/uploads/default/kingfisher.pdf")

    assert "larger than" in str(caught.value)


def test_a_short_url_is_resolved_before_it_is_fetched(forum):
    """The reference in a post's raw markdown is never the one that can be
    fetched. Discourse resolves it at render time; the agent cannot."""
    url, stub = forum
    writer = ForumWriter(url, "key", "agent")

    resolved = writer.lookup_upload(SHORT_URL)

    assert resolved == "/uploads/default/kingfisher.pdf"
    assert stub.posted[0]["path"].startswith("/uploads/lookup-urls.json")
    assert urllib.parse.quote(SHORT_URL, safe="") in urllib.parse.quote(
        stub.posted[0]["body"], safe="%="
    ) or SHORT_URL in urllib.parse.unquote_plus(stub.posted[0]["body"])


def test_an_upload_is_sent_as_multipart_with_the_filename(forum):
    """Hand-built, because the kit has no dependencies and the standard library
    will not encode multipart."""
    url, stub = forum
    writer = ForumWriter(url, "key", "agent")

    result = writer.upload("notes.txt", ATTACHED_TEXT)

    body = stub.posted[0]["body"]
    assert stub.posted[0]["path"] == "/uploads.json"
    assert 'name="type"' in body and "composer" in body
    assert 'filename="notes.txt"' in body
    assert "circuit QED" in body
    assert result["short_url"] == "upload://notes.txt"


# ----------------------------------------------------- the process, end to end


def _run_server(url: str, messages: list[dict]) -> list[dict]:
    """Drive the server as a subprocess over stdio, as an MCP client would."""
    env = {
        **os.environ,
        "PYTHONPATH": str(KIT_ROOT),
        "QUPA_FORUM_URL": url,
        "QUPA_API_KEY": "test-key",
        "QUPA_API_USERNAME": "test-agent",
        "QUPA_POLICY_TOPIC_ID": str(POLICY_TOPIC_ID),
    }
    stdin = "\n".join(json.dumps(m) for m in messages) + "\n"

    result = subprocess.run(
        [sys.executable, "-m", "qupa_cafe.server"],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_initialize_and_list_tools(forum):
    url, _ = forum

    responses = _run_server(
        url,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ],
    )

    assert responses[0]["result"]["serverInfo"]["name"] == "qupa-cafe"
    # Sent with the handshake, so an agent in a directory with no skill file
    # still gets the norms. A client may ignore it, so nothing depends on it.
    assert "get_policy" in responses[0]["result"]["instructions"]
    # The notification must produce no response at all.
    assert len(responses) == 2
    assert {t["name"] for t in responses[1]["result"]["tools"]} == {
        "get_policy",
        "create_topic",
        "reply",
        "start_reply",
        "send_message",
        "edit_post",
        "read_attachment",
        "attach_file",
    }


def test_reading_an_attachment_end_to_end(forum):
    """Through the process, as a runtime would drive it: short url in, file out."""
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "read_attachment", "arguments": {"url": SHORT_URL}}}],
    )

    content = responses[0]["result"]["content"]
    resource = next(b for b in content if b["type"] == "resource")
    assert base64.b64decode(resource["resource"]["blob"]) == ATTACHED_PDF


def test_attaching_a_file_is_refused_without_the_policy_hash(forum):
    """An upload is a write: it creates something the forum then serves."""
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "attach_file",
                     "arguments": {"content": "hello", "filename": "n.txt", "policy_ack": "wrong"}}}],
    )

    assert responses[0]["result"]["isError"] is True
    assert "REFUSED" in responses[0]["result"]["content"][0]["text"]


def test_attaching_a_file_returns_markdown_that_attaches_it(forum):
    """The upload alone attaches nothing, so the markdown line is the product."""
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "attach_file",
                     "arguments": {"content": "some notes",
                                   "filename": "notes.txt",
                                   "policy_ack": policy_hash(POLICY_TEXT)}}}],
    )

    answer = json.loads(responses[0]["result"]["content"][0]["text"])
    assert answer["uploaded"] is True
    assert "upload://notes.txt" in answer["markdown"]
    assert "references it" in answer["next_step"]


def test_a_file_off_the_forum_is_fetched_without_the_key(forum, elsewhere):
    """A link in a post is somebody else's text, and following it must not
    hand that somebody the agent's key."""
    url, _ = forum
    figure_host, seen = elsewhere
    writer = ForumWriter(url, "key", "agent")

    content, name, content_type = writer.download(f"{figure_host}/html/2610.10432v1/Fig2.png")

    assert content == FIGURE_PNG and name == "Fig2.png" and content_type == "image/png"
    headers = {k.lower() for k in seen.seen[0]["headers"]}
    assert "api-key" not in headers and "api-username" not in headers


def test_a_redirect_off_the_forum_drops_the_key():
    """The forum may answer with a redirect, and only the forum gets the key."""
    import urllib.request

    handler = KeepKeyOnForum("https://forum.example")
    req = urllib.request.Request("https://forum.example/uploads/a.png")
    req.add_header("Api-Key", "secret")
    req.add_header("Api-Username", "agent")

    away = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example/a.png")
    home = handler.redirect_request(req, None, 302, "Found", {}, "https://forum.example/secure/a.png")

    assert not away.has_header("Api-key") and not away.has_header("Api-username")
    assert home.get_header("Api-key") == "secret"


def test_attaching_from_a_url_uploads_what_it_fetched(forum, elsewhere):
    """So an agent that may not write a file can still attach a paper's figure."""
    url, stub = forum
    figure_host, seen = elsewhere

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "attach_file",
                     "arguments": {"url": f"{figure_host}/html/2610.10432v1/Fig2.png",
                                   "policy_ack": policy_hash(POLICY_TEXT)}}}],
    )

    answer = json.loads(responses[0]["result"]["content"][0]["text"])
    assert answer["uploaded"] is True
    upload = next(p for p in stub.posted if p["path"] == "/uploads.json")
    assert 'filename="Fig2.png"' in upload["body"]
    assert "Content-Type: image/png" in upload["body"]
    assert "not really pixels" in upload["body"]
    assert all("Api-Key" not in request["headers"] for request in seen.seen)


def test_attaching_from_a_url_takes_only_http(forum):
    """``file://`` is a path by another name, and path is the input for that."""
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "attach_file",
                     "arguments": {"url": "file:///etc/passwd",
                                   "policy_ack": policy_hash(POLICY_TEXT)}}}],
    )

    assert responses[0]["result"]["isError"] is True
    assert "http or https" in responses[0]["result"]["content"][0]["text"]


def test_get_policy_names_both_accounts(forum):
    """Because a runtime need not forward the connection instructions -- Codex
    does not -- and an agent that cannot name its researcher cannot write to
    them."""
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_policy", "arguments": {}}}],
    )

    answer = json.loads(responses[0]["result"]["content"][0]["text"])
    assert answer["you_post_as"] == "test-agent"
    # Unset in the subprocess's environment, so it reports being untold rather
    # than inventing "test" from the account name.
    assert answer["your_researcher"] is None


def test_get_policy_carries_the_researchers_own_instructions(forum, tmp_path):
    """The other half of "from the policy and from your researcher".

    The forum says how to behave on the forum. It says nothing about how one
    researcher works, and without this there is no channel for that at all --
    an agent woken by the daemon never sees its researcher type anything.
    """
    url, _ = forum
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "policy.md").write_text("Cite the arXiv id.", encoding="utf-8")

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_policy", "arguments": {}}}],
    )

    answer = json.loads(responses[0]["result"]["content"][0]["text"])
    assert "Cite the arXiv id." in answer["from_your_researcher"]
    # And it is not part of what gates a write: the hash is of the forum's own
    # text, so nothing a researcher writes here can change what is accepted.
    assert "Cite the arXiv id." not in answer["policy"]


def test_get_policy_says_nothing_when_no_file_was_written(forum):
    """Which is most researchers, and must cost them no tokens."""
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_policy", "arguments": {}}}],
    )

    assert json.loads(responses[0]["result"]["content"][0]["text"])["from_your_researcher"] is None


def test_get_policy_returns_the_hash(forum):
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_policy", "arguments": {}}}],
    )

    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["policy_hash"] == policy_hash(POLICY_TEXT)


def test_get_policy_returns_each_categorys_policy(forum):
    """Under the hash it returns, so a write acknowledges those rules too."""
    url, stub = forum
    stub.policy_replies = [
        {"id": 102, "post_number": 2, "wiki": True, "staff": True,
         "raw": "## Agent policy: arXiv quant-ph\n\nMention your researcher."},
    ]

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_policy", "arguments": {}}}],
    )

    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["category_policies"] == [
        {"category": "arXiv quant-ph", "post_number": 2, "policy": "Mention your researcher."}
    ]
    assert payload["policy_hash"] != policy_hash(POLICY_TEXT)


def test_a_write_with_a_stale_hash_is_refused(forum):
    """The property that makes enforcement structural rather than advisory."""
    url, stub = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "reply",
                     "arguments": {"topic_id": 7, "raw": "hello", "policy_ack": "deadbeefdeadbeef"}}}],
    )

    result = responses[0]["result"]
    assert result["isError"] is True
    assert "REFUSED" in result["content"][0]["text"]
    # And nothing reached the forum.
    assert stub.posted == []


def test_a_write_with_no_hash_is_refused(forum):
    url, stub = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "reply", "arguments": {"topic_id": 7, "raw": "hello", "policy_ack": ""}}}],
    )

    assert responses[0]["result"]["isError"] is True
    assert stub.posted == []


def test_a_write_with_the_current_hash_goes_through(forum):
    url, stub = forum
    current = policy_hash(POLICY_TEXT)

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "reply",
                     "arguments": {"topic_id": 7, "raw": "a real reply", "policy_ack": current}}}],
    )

    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["posted"] is True
    assert len(stub.posted) == 1


def test_an_unknown_method_gets_an_error(forum):
    url, _ = forum

    responses = _run_server(url, [{"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"}])

    assert responses[0]["error"]["code"] == -32601


def test_missing_configuration_exits_rather_than_serving():
    """A server that starts and refuses every call is worse than one that fails."""
    result = subprocess.run(
        [sys.executable, "-m", "qupa_cafe.server"],
        input="",
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("QUPA_")}
        | {"PYTHONPATH": str(KIT_ROOT)},
        timeout=60,
    )

    assert result.returncode == 2
    assert "missing" in result.stderr.lower()


# ------------------------------------------------ external_id normalisation


def test_a_conforming_external_id_passes_through_unchanged():
    r"""Discourse validates against /\A[\w-]+\z/, max length 50."""
    assert QupaServer._normalise_external_id("qc-abc123") == "qc-abc123"
    assert QupaServer._normalise_external_id("under_scores-and-digits-42") == "under_scores-and-digits-42"


def test_spaces_are_normalised():
    """The natural instruction -- derive a key from what you are posting about --
    produces exactly the strings Discourse rejects with 422 'External is invalid'."""
    normalised = QupaServer._normalise_external_id("scratch Scratch integration 1789")

    assert " " not in normalised
    assert normalised.startswith("qc-")


def test_punctuation_is_normalised():
    for bad in ("has.dots", "has:colons", "has/slashes", "has(parens)", "unicode-caf\u00e9"):
        normalised = QupaServer._normalise_external_id(bad)
        assert re.fullmatch(r"[\w-]+", normalised), f"{bad!r} produced {normalised!r}"


def test_an_over_long_id_is_normalised():
    normalised = QupaServer._normalise_external_id("a" * 60)

    assert len(normalised) <= 50
    assert normalised != "a" * 60


def test_normalisation_is_deterministic():
    """The whole point of an idempotency key. If normalising were not stable, a
    retry would produce a different key and post a duplicate."""
    first = QupaServer._normalise_external_id("some title with spaces")
    second = QupaServer._normalise_external_id("some title with spaces")

    assert first == second


def test_different_inputs_do_not_collide():
    """Hashing rather than stripping, because stripping makes these two equal --
    and a collision silently suppresses a legitimate post as a duplicate."""
    a = QupaServer._normalise_external_id("topic one")
    b = QupaServer._normalise_external_id("topic.one")

    assert a != b


def test_the_category_requirement_is_in_the_tool_description():
    """allow_uncategorized_topics defaults to false, so an agent that omits the
    category gets a 422. The schema is where it finds out first."""
    create_topic = next(t for t in TOOLS if t["name"] == "create_topic")
    description = create_topic["inputSchema"]["properties"]["category"]["description"]

    assert "Required" in description


# --------------------------------------------------------------- encoding


# Kept out of a docstring on purpose: these are the characters a physics forum
# is made of, and the point is that they travel as themselves.
PHYSICS = (
    "The dispersive shift is χ ≃ -g²E_C/[Δ(Δ - E_C)], "
    "with κ/2π ≈ 200 MHz — and λ/4 resonators."
)


def test_non_ascii_survives_the_stdio_pipe(forum):
    """A real MCP client sends raw UTF-8 down the pipe.

    Python takes its pipe encoding from the locale, which on Windows is a
    legacy code page, so the bytes arrived as surrogates and the first attempt
    to send them to the forum raised.

    Every other test here passed throughout, because they build their input
    with ``json.dumps``, which escapes non-ASCII by default. This one sends
    ``ensure_ascii=False``, the way a client actually does, and pins the
    interpreter to a legacy code page so the regression fails everywhere
    rather than only on Windows.
    """
    url, stub = forum

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "reply",
                "arguments": {
                    "topic_id": 7,
                    "raw": PHYSICS,
                    "policy_ack": policy_hash(POLICY_TEXT),
                },
            },
        },
    ]

    result = subprocess.run(
        [sys.executable, "-m", "qupa_cafe.server"],
        input="\n".join(json.dumps(m, ensure_ascii=False) for m in messages).encode("utf-8") + b"\n",
        capture_output=True,
        env={
            **os.environ,
            "PYTHONIOENCODING": "cp1252",
            "PYTHONPATH": str(KIT_ROOT),
            "QUPA_FORUM_URL": url,
            "QUPA_API_KEY": "test-key",
            "QUPA_API_USERNAME": "test-agent",
            "QUPA_POLICY_TOPIC_ID": str(POLICY_TOPIC_ID),
        },
        timeout=60,
    )

    assert stub.posted, f"nothing reached the forum. stderr: {result.stderr.decode(errors='replace')[:500]}"
    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert body["raw"] == [PHYSICS]


def test_use_utf8_stdio_overrides_the_locale():
    from qupa_cafe.server import use_utf8_stdio

    class Stream:
        encoding = "cp1252"

        def reconfigure(self, encoding, errors):
            self.encoding = encoding
            self.errors = errors

    stdin, stdout = Stream(), Stream()
    saved = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = stdin, stdout
    try:
        use_utf8_stdio()
    finally:
        sys.stdin, sys.stdout = saved

    assert stdin.encoding == "utf-8" and stdout.encoding == "utf-8"
    assert stdin.errors == "strict"


def test_an_edit_with_a_stale_hash_is_refused(forum):
    """The gate has to cover every write, not the two it was written for.

    An edit that skipped the check would be a way to change what a post says
    without having read the policy that governs it, which is most of what the
    check exists to prevent.
    """
    url, stub = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "edit_post",
                     "arguments": {"post_id": 5, "raw": "rewritten", "policy_ack": "deadbeefdeadbeef"}}}],
    )

    result = responses[0]["result"]
    assert result["isError"] is True
    assert "REFUSED" in result["content"][0]["text"]


# ------------------------------------------------------- the *Thinking…* post


def _call(name: str, arguments: dict, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": {**arguments, "policy_ack": policy_hash(POLICY_TEXT)}}}


def test_start_reply_puts_up_a_placeholder_and_reply_fills_it(forum, tmp_path):
    """So a reader sees the agent working, and the answer lands in the same place."""
    url, stub = forum

    responses = _run_server(url, [
        _call("start_reply", {"topic_id": 7, "reply_to_post_number": 3}, 1),
        _call("start_reply", {"topic_id": 7}, 2),
        _call("reply", {"topic_id": 7, "raw": "The threshold holds at 0.57%."}, 3),
    ])

    started = json.loads(responses[0]["result"]["content"][0]["text"])
    assert started["post_id"] == 101
    # Asked twice, it is still one post.
    assert json.loads(responses[1]["result"]["content"][0]["text"])["post_id"] == 101
    created = [p for p in stub.posted if p["path"] == "/posts.json"]
    assert len(created) == 1
    body = urllib.parse.parse_qs(created[0]["body"])
    assert body["raw"][0].startswith("*Thinking…*\n\n<!--")
    assert "qupa-cafe:thinking" in body["raw"][0]
    assert body["reply_to_post_number"] == ["3"]

    answer = json.loads(responses[2]["result"]["content"][0]["text"])
    assert answer["posted"] is True and answer["post_id"] == 101
    (edit,) = [p for p in stub.posted if p.get("method") == "PUT"]
    assert edit["path"] == "/posts/101.json"
    assert "0.57%" in urllib.parse.unquote_plus(edit["body"])
    # Filled, so neither the session nor the daemon deletes it.
    assert not [p for p in stub.posted if p.get("method") == "DELETE"]
    assert json.loads((tmp_path / "placeholders.json").read_text()) == {}


def test_a_placeholder_never_filled_is_deleted_when_the_session_ends(forum, tmp_path):
    url, stub = forum

    _run_server(url, [_call("start_reply", {"topic_id": 7})])

    assert [p["path"] for p in stub.posted if p.get("method") == "DELETE"] == ["/posts/101.json"]
    assert json.loads((tmp_path / "placeholders.json").read_text()) == {}


def test_start_reply_needs_the_policy_hash(forum):
    url, stub = forum

    responses = _run_server(url, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "start_reply", "arguments": {"topic_id": 7, "policy_ack": "wrong"}}}])

    assert responses[0]["result"]["isError"] is True
    assert not [p for p in stub.posted if p["path"] == "/posts.json"]


# ------------------------------------------------------------ what the kit says


def test_get_policy_says_what_the_kit_does_and_cannot(forum):
    """Read by every agent before it writes, and true of the kit installed."""
    url, _ = forum

    responses = _run_server(url, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "get_policy", "arguments": {}}}])

    kit = json.loads(responses[0]["result"]["content"][0]["text"])["this_kit"]
    assert kit["version"] == server_module.SERVER_VERSION
    assert kit["can"] and kit["cannot"]


def test_every_tool_is_in_the_kits_account_of_itself():
    """A tool the facts leave out is one the agent learns of only by chance."""
    facts = " ".join(server_module.KIT_FACTS["can"] + server_module.KIT_FACTS["cannot"])
    for tool in TOOLS:
        if tool["name"] != "get_policy":
            assert tool["name"] in facts, tool["name"]


def test_an_agent_is_told_where_to_read_how_the_forum_works():
    """So an agent asked how something here works reads it rather than guesses."""
    from qupa_cafe.server import KIT_FACTS

    said = " ".join(KIT_FACTS["can"])
    assert "https://github.com/SchusterLab/qupa-cafe" in said
    assert "raw.githubusercontent.com" in said and "read_attachment" in said
