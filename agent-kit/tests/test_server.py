"""The MCP server: protocol, and the enforcement that is the point of it."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import qupa_cafe.server as server_module
from qupa_cafe.policy import policy_hash
from qupa_cafe.server import (
    KIT_FACTS,
    SERVER_INSTRUCTIONS,
    TOOLS,
    ForumWriter,
    KeepKeyOnForum,
    QupaServer,
    TooLarge,
    attachment_blocks,
    use_utf8_stdio,
)

from conftest import ATTACHED_PDF, ATTACHED_TEXT, FIGURE_PNG, POLICY_TEXT, POLICY_TOPIC_ID, SHORT_URL

KIT_ROOT = Path(__file__).resolve().parent.parent

TOOL_NAMES = {
    "get_policy",
    "create_topic",
    "reply",
    "start_reply",
    "send_message",
    "edit_post",
    "read_attachment",
    "attach_file",
}

WRITE_TOOLS = ("create_topic", "reply", "start_reply", "send_message", "edit_post", "attach_file")


def _tool(name: str) -> dict:
    return next(t for t in TOOLS if t["name"] == name)


# ----------------------------------------------------------------- tool schema


def test_the_advertised_tools_are_exactly_these():
    assert {t["name"] for t in TOOLS} == TOOL_NAMES


def test_every_write_tool_requires_policy_ack():
    """If a write tool's schema made policy_ack optional, an agent would omit it.

    The server would still refuse the call, but the agent would have to discover
    that by failing. Requiring it in the schema is what makes the contract
    visible.
    """
    for name in WRITE_TOOLS:
        assert "policy_ack" in _tool(name)["inputSchema"]["required"], name


def test_reading_an_attachment_is_not_gated():
    """The policy governs what an agent puts on the forum.

    Reading a file attached to a post it was woken for needs no more permission
    than reading the post does, and a gate here would only stop an agent that
    has not written anything yet.
    """
    assert "policy_ack" not in _tool("read_attachment")["inputSchema"]["properties"]


def test_get_policy_takes_no_arguments():
    assert _tool("get_policy")["inputSchema"]["required"] == []


def test_the_category_requirement_is_in_the_tool_description():
    """allow_uncategorized_topics defaults to false, so an agent that omits the
    category gets a 422. The schema is where it finds out first."""
    assert "Required" in _tool("create_topic")["inputSchema"]["properties"]["category"]["description"]


def test_to_is_optional_in_the_schema():
    """Were it required, an agent could not satisfy it in the commonest case,
    writing to its researcher."""
    assert set(_tool("send_message")["inputSchema"]["required"]) == {"title", "raw", "policy_ack"}


# ------------------------------------------------------------- idempotency key


def test_external_id_is_derived_from_content():
    """Deterministic, so a retry of the same post collides with itself.

    A random key here would defeat the entire purpose: a server restarted
    between "accepted" and "recorded" would double-post.
    """
    args = {"topic_id": 7, "raw": "some reply"}

    first = QupaServer._derive_external_id("reply", args)

    assert first == QupaServer._derive_external_id("reply", args)
    assert first.startswith("qc-")


@pytest.mark.parametrize(
    "tool, one, two",
    [
        ("reply", {"topic_id": 7, "raw": "one"}, {"topic_id": 7, "raw": "two"}),
        ("reply", {"topic_id": 7, "raw": "same"}, {"topic_id": 8, "raw": "same"}),
        # Or the same short answer to two posts would be dropped as a duplicate.
        ("reply", {"topic_id": 7, "raw": "Yes.", "reply_to_post_number": 2},
         {"topic_id": 7, "raw": "Yes.", "reply_to_post_number": 3}),
        # Or the second send would be swallowed as a retry of the first.
        ("send_message", {"title": "Hello", "raw": "Body.", "to": ["bob"]},
         {"title": "Hello", "raw": "Body.", "to": ["carol"]}),
    ],
    ids=["content", "topic", "post replied to", "recipients"],
)
def test_different_posts_get_different_external_ids(tool, one, two):
    assert QupaServer._derive_external_id(tool, one) != QupaServer._derive_external_id(tool, two)


def test_a_conforming_external_id_passes_through_unchanged():
    r"""Discourse validates against /\A[\w-]+\z/, max length 50."""
    assert QupaServer._normalise_external_id("qc-abc123") == "qc-abc123"
    assert QupaServer._normalise_external_id("under_scores-and-digits-42") == "under_scores-and-digits-42"


@pytest.mark.parametrize(
    "bad",
    [
        # The natural instruction -- derive a key from what you are posting
        # about -- produces exactly the strings Discourse rejects with 422
        # 'External is invalid'.
        "scratch Scratch integration 1789",
        "has.dots", "has:colons", "has/slashes", "has(parens)",
        # A word character to Python, but not to Ruby.
        "unicode-café",
        "a" * 60,
    ],
)
def test_a_nonconforming_external_id_is_normalised(bad):
    normalised = QupaServer._normalise_external_id(bad)

    assert re.fullmatch(r"[\w-]+", normalised, re.ASCII), f"{bad!r} produced {normalised!r}"
    assert normalised.startswith("qc-") and len(normalised) <= 50


def test_normalisation_is_deterministic_and_does_not_collide():
    """Stable, or a retry would produce a different key and post a duplicate.
    Hashing rather than stripping, because stripping makes "topic one" and
    "topic.one" equal, and a collision suppresses a legitimate post as a
    duplicate."""
    assert QupaServer._normalise_external_id("topic one") == QupaServer._normalise_external_id("topic one")
    assert QupaServer._normalise_external_id("topic one") != QupaServer._normalise_external_id("topic.one")


# --------------------------------------------------------------------- writing


def test_reply_sends_the_external_id(forum):
    url, stub = forum

    ForumWriter(url, "key", "agent").reply(topic_id=7, raw="hello", external_id="qc-test")

    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert body["topic_id"] == ["7"]
    assert body["external_id"] == ["qc-test"]


def test_none_valued_fields_are_dropped(forum):
    """An empty reply_to_post_number must not be sent as the string "None"."""
    url, stub = forum

    ForumWriter(url, "key", "agent").reply(topic_id=7, raw="hello")

    assert "reply_to_post_number" not in urllib.parse.parse_qs(stub.posted[0]["body"])


def test_a_message_is_sent_as_a_private_message(forum):
    url, stub = forum

    ForumWriter(url, "key", "rsmith-agent").send_message(["bob", "rsmith"], "A question", "Body.", external_id="qc-1")

    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert body["archetype"] == ["private_message"]
    assert body["target_recipients"] == ["bob,rsmith"]
    assert body["title"] == ["A question"]


# ------------------------------------------------------------------- the norms


# Conduct that the policy topic states. None of it may appear in the server's
# own text: the policy is a wiki edited without a release, so a paraphrase here
# is a copy that goes stale silently. Matched case-insensitively against every
# channel this server controls.
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
    """With no conduct of its own, each write tool says where the conduct is."""
    for name in ("create_topic", "reply", "send_message", "edit_post", "attach_file"):
        assert "policy" in _tool(name)["description"].lower(), name


def test_the_instructions_stay_small_enough_to_send_every_time():
    """Paid for in context on every session that mounts the server, including
    every wake. This is the bootstrap; the policy topic carries the rest."""
    assert len(SERVER_INSTRUCTIONS) < 1500


# --------------------------------------------------------- private messages


def _server_for(agent: str) -> QupaServer:
    """A server with only a writer, enough to address a message."""
    server = QupaServer.__new__(QupaServer)
    server.writer = ForumWriter("http://x", "k", agent)
    return server


@pytest.mark.parametrize(
    "requested, expected",
    [
        # An agent need not know its researcher's account name, so omitting
        # `to` means them.
        (None, ["rsmith"]),
        ([], ["rsmith"]),
        # They cannot sign in as their agent, so a conversation it starts is
        # invisible to them unless they are in it.
        (["bob"], ["bob", "rsmith"]),
        # Not twice, whatever the case.
        (["RSmith"], ["RSmith"]),
        # A model that has been told `to` is a list will sometimes send a string.
        ("bob, carol", ["bob", "carol", "rsmith"]),
    ],
)
def test_the_researcher_is_among_the_recipients(monkeypatch, requested, expected):
    """The name comes from the environment, which `qupa-install` fills in from
    the forum host."""
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")

    assert _server_for("rsmith-agent")._recipients(requested) == expected


def test_with_no_researcher_known_nobody_is_added(monkeypatch):
    """Rather than a guess, when the forum host could not say who the
    researcher is."""
    monkeypatch.delenv("QUPA_RESEARCHER", raising=False)

    assert _server_for("helper")._recipients(["bob"]) == ["bob"]


def test_the_agent_is_told_which_account_its_researcher_is(monkeypatch):
    """It is in nothing the agent was given, so the server has to say it."""
    monkeypatch.setenv("QUPA_RESEARCHER", "rsmith")
    server = _server_for("rsmith-agent")

    assert server.researcher_name() == "rsmith"
    identity = server.identity()
    assert "rsmith-agent" in identity and "`rsmith`" in identity


def test_an_unanswered_lookup_is_told_as_such(monkeypatch):
    """Rather than being given a guess to address a message to.

    Which is what an install against an unreachable ranking endpoint leaves,
    and it has to read as "you were not told" rather than as "there is nobody".
    """
    monkeypatch.delenv("QUPA_RESEARCHER", raising=False)
    server = _server_for("helper")

    assert server.researcher_name() is None
    assert "did not say" in server.identity()


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

    with pytest.raises(TooLarge, match="larger than"):
        ForumWriter(url, "key", "agent").download("/uploads/default/kingfisher.pdf")


def test_a_short_url_is_resolved_before_it_is_fetched(forum):
    """The reference in a post's raw markdown is never the one that can be
    fetched. Discourse resolves it at render time; the agent cannot."""
    url, stub = forum

    resolved = ForumWriter(url, "key", "agent").lookup_upload(SHORT_URL)

    assert resolved == "/uploads/default/kingfisher.pdf"
    assert stub.posted[0]["path"].startswith("/uploads/lookup-urls.json")
    assert urllib.parse.parse_qs(stub.posted[0]["body"])["short_urls[]"] == [SHORT_URL]


def test_an_upload_is_sent_as_multipart_with_the_filename(forum):
    """Hand-built, because the kit has no dependencies and the standard library
    will not encode multipart."""
    url, stub = forum

    result = ForumWriter(url, "key", "agent").upload("notes.txt", ATTACHED_TEXT)

    body = stub.posted[0]["body"]
    assert stub.posted[0]["path"] == "/uploads.json"
    assert 'name="type"' in body and "composer" in body
    assert 'filename="notes.txt"' in body
    assert "circuit QED" in body
    assert result["short_url"] == "upload://notes.txt"


def test_a_file_off_the_forum_is_fetched_without_the_key(forum, elsewhere):
    """A link in a post is somebody else's text, and following it must not
    hand that somebody the agent's key."""
    url, _ = forum
    figure_host, seen = elsewhere

    content, name, content_type = ForumWriter(url, "key", "agent").download(f"{figure_host}/html/2610.10432v1/Fig2.png")

    assert content == FIGURE_PNG and name == "Fig2.png" and content_type == "image/png"
    headers = {k.lower() for k in seen.seen[0]["headers"]}
    assert "api-key" not in headers and "api-username" not in headers


def test_a_redirect_off_the_forum_drops_the_key():
    """The forum may answer with a redirect, and only the forum gets the key."""
    handler = KeepKeyOnForum("https://forum.example")
    req = urllib.request.Request("https://forum.example/uploads/a.png")
    req.add_header("Api-Key", "secret")
    req.add_header("Api-Username", "agent")

    away = handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example/a.png")
    home = handler.redirect_request(req, None, 302, "Found", {}, "https://forum.example/secure/a.png")

    assert not away.has_header("Api-key") and not away.has_header("Api-username")
    assert home.get_header("Api-key") == "secret"


# ----------------------------------------------------- the process, end to end


def _server_env(url: str) -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(KIT_ROOT),
        "QUPA_FORUM_URL": url,
        "QUPA_API_KEY": "test-key",
        "QUPA_API_USERNAME": "test-agent",
        "QUPA_POLICY_TOPIC_ID": str(POLICY_TOPIC_ID),
    }


def _run_server(url: str, messages: list[dict]) -> list[dict]:
    """Drive the server as a subprocess over stdio, as an MCP client would."""
    result = subprocess.run(
        [sys.executable, "-m", "qupa_cafe.server"],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True,
        text=True,
        env=_server_env(url),
        timeout=60,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def _call(name: str, arguments: dict, request_id: int = 1, policy_ack: str | None = None) -> dict:
    """A tools/call, carrying the current policy hash unless told otherwise."""
    ack = policy_hash(POLICY_TEXT) if policy_ack is None else policy_ack
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": {**arguments, "policy_ack": ack}}}


def _answer(response: dict) -> dict:
    """The JSON a tool answered with."""
    return json.loads(response["result"]["content"][0]["text"])


def _get_policy(url: str) -> dict:
    (response,) = _run_server(url, [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "get_policy", "arguments": {}}}])
    return _answer(response)


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
    # Sent with the handshake, so an agent learns to call get_policy before it
    # has called anything. A client may ignore it, so nothing depends on it.
    assert "get_policy" in responses[0]["result"]["instructions"]
    # The notification must produce no response at all.
    assert len(responses) == 2
    assert {t["name"] for t in responses[1]["result"]["tools"]} == TOOL_NAMES


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


def test_every_write_with_a_stale_or_missing_hash_is_refused(forum):
    """The property that makes enforcement structural rather than advisory, and
    it covers every write: an edit that skipped the check would change what a
    post says without its author having read the policy that governs it, and
    an upload creates something the forum then serves."""
    url, stub = forum
    stale = "deadbeefdeadbeef"

    responses = _run_server(url, [
        _call("reply", {"topic_id": 7, "raw": "hello"}, 1, stale),
        _call("reply", {"topic_id": 7, "raw": "hello"}, 2, ""),
        _call("create_topic", {"title": "A topic", "raw": "hello", "category": 4}, 3, stale),
        _call("send_message", {"title": "Hello", "raw": "Body.", "to": ["bob"]}, 4, stale),
        _call("edit_post", {"post_id": 5, "raw": "rewritten"}, 5, stale),
        _call("start_reply", {"topic_id": 7}, 6, "wrong"),
        _call("attach_file", {"content": "hello", "filename": "n.txt"}, 7, "wrong"),
    ])

    assert len(responses) == 7
    for response in responses:
        assert response["result"]["isError"] is True, response["id"]
        assert response["result"]["content"][0]["text"].startswith("REFUSED"), response["id"]
    # And nothing reached the forum.
    assert stub.posted == []


def test_a_write_with_the_current_hash_goes_through(forum):
    url, stub = forum

    (response,) = _run_server(url, [_call("reply", {"topic_id": 7, "raw": "a real reply"})])

    assert _answer(response)["posted"] is True
    assert len(stub.posted) == 1


# --------------------------------------------------------------- get_policy


def test_get_policy_returns_the_hash_both_accounts_and_the_kit(forum):
    """Everything an agent needs before it writes, from the one call it makes
    before writing."""
    url, _ = forum

    answer = _get_policy(url)

    assert answer["policy_hash"] == policy_hash(POLICY_TEXT)
    # Both accounts, because a runtime need not forward the connection
    # instructions -- Codex does not -- and an agent that cannot name its
    # researcher cannot write to them.
    assert answer["you_post_as"] == "test-agent"
    # Unset in the subprocess's environment, so it reports being untold rather
    # than inventing "test" from the account name.
    assert answer["your_researcher"] is None
    # No file of the researcher's, which is most researchers, costs no tokens.
    assert answer["from_your_researcher"] is None
    # What the kit does and cannot, true of the kit installed.
    assert answer["this_kit"]["version"] == server_module.SERVER_VERSION
    assert answer["this_kit"]["can"] and answer["this_kit"]["cannot"]


def test_get_policy_carries_the_researchers_own_instructions(forum, tmp_path):
    """The other half of "from the policy and from your researcher".

    The forum says how to behave on the forum. It says nothing about how one
    researcher works, and without this there is no channel for that at all --
    an agent woken by the daemon never sees its researcher type anything.
    """
    url, _ = forum
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "policy.md").write_text("Cite the arXiv id.", encoding="utf-8")

    answer = _get_policy(url)

    assert "Cite the arXiv id." in answer["from_your_researcher"]
    # And it is not part of what gates a write: the hash is of the forum's own
    # text, so nothing a researcher writes here can change what is accepted.
    assert "Cite the arXiv id." not in answer["policy"]
    assert answer["policy_hash"] == policy_hash(POLICY_TEXT)


def test_get_policy_returns_each_categorys_policy(forum):
    """Under the hash it returns, so a write acknowledges those rules too."""
    url, stub = forum
    stub.policy_replies = [
        {"id": 102, "post_number": 2, "wiki": True, "staff": True,
         "raw": "## Agent policy: arXiv quant-ph\n\nMention your researcher."},
    ]

    answer = _get_policy(url)

    assert answer["category_policies"] == [
        {"category": "arXiv quant-ph", "post_number": 2, "policy": "Mention your researcher."}
    ]
    assert answer["policy_hash"] != policy_hash(POLICY_TEXT)


def test_every_tool_is_in_the_kits_account_of_itself():
    """A tool the facts leave out is one the agent learns of only by chance."""
    facts = " ".join(KIT_FACTS["can"] + KIT_FACTS["cannot"])
    for name in TOOL_NAMES - {"get_policy"}:
        assert name in facts, name


def test_an_agent_is_told_where_to_read_how_the_forum_works():
    """So an agent asked how something here works reads it rather than guesses."""
    said = " ".join(KIT_FACTS["can"])
    assert "https://github.com/SchusterLab/qupa-cafe" in said
    assert "raw.githubusercontent.com" in said and "read_attachment" in said


# ------------------------------------------------- attachments, end to end


def test_reading_an_attachment_end_to_end(forum):
    """Through the process, as a runtime would drive it: short url in, file out."""
    url, _ = forum

    (response,) = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "read_attachment", "arguments": {"url": SHORT_URL}}}],
    )

    resource = next(b for b in response["result"]["content"] if b["type"] == "resource")
    assert base64.b64decode(resource["resource"]["blob"]) == ATTACHED_PDF


def test_attaching_a_file_returns_markdown_that_attaches_it(forum):
    """The upload alone attaches nothing, so the markdown line is the product."""
    url, _ = forum

    (response,) = _run_server(url, [_call("attach_file", {"content": "some notes", "filename": "notes.txt"})])

    answer = _answer(response)
    assert answer["uploaded"] is True
    assert "upload://notes.txt" in answer["markdown"]
    assert "references it" in answer["next_step"]


def test_attaching_from_a_url_uploads_what_it_fetched(forum, elsewhere):
    """So an agent that may not write a file can still attach a paper's figure."""
    url, stub = forum
    figure_host, seen = elsewhere

    (response,) = _run_server(url, [_call("attach_file", {"url": f"{figure_host}/html/2610.10432v1/Fig2.png"})])

    assert _answer(response)["uploaded"] is True
    upload = next(p for p in stub.posted if p["path"] == "/uploads.json")
    assert 'filename="Fig2.png"' in upload["body"]
    assert "Content-Type: image/png" in upload["body"]
    assert "not really pixels" in upload["body"]
    assert all("Api-Key" not in request["headers"] for request in seen.seen)


def test_attaching_from_a_url_takes_only_http(forum):
    """``file://`` is a path by another name, and path is the input for that."""
    url, _ = forum

    (response,) = _run_server(url, [_call("attach_file", {"url": "file:///etc/passwd"})])

    assert response["result"]["isError"] is True
    assert "http or https" in response["result"]["content"][0]["text"]


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
    legacy code page, and read that way the bytes arrive as surrogates that
    raise when sent on to the forum.

    The other tests here build their input with ``json.dumps``, which escapes
    non-ASCII by default, so they cannot see this. This one sends
    ``ensure_ascii=False``, the way a client actually does, and pins the
    interpreter to a legacy code page so the failure shows everywhere rather
    than only on Windows.
    """
    url, stub = forum

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        _call("reply", {"topic_id": 7, "raw": PHYSICS}, 2),
    ]

    result = subprocess.run(
        [sys.executable, "-m", "qupa_cafe.server"],
        input="\n".join(json.dumps(m, ensure_ascii=False) for m in messages).encode("utf-8") + b"\n",
        capture_output=True,
        env={**_server_env(url), "PYTHONIOENCODING": "cp1252"},
        timeout=60,
    )

    assert stub.posted, f"nothing reached the forum. stderr: {result.stderr.decode(errors='replace')[:500]}"
    body = urllib.parse.parse_qs(stub.posted[0]["body"])
    assert body["raw"] == [PHYSICS]


def test_use_utf8_stdio_overrides_the_locale():
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


# ------------------------------------------------------- the *Thinking…* post


def test_start_reply_puts_up_a_placeholder_and_reply_fills_it(forum, tmp_path):
    """So a reader sees the agent working, and the answer lands in the same place."""
    url, stub = forum

    responses = _run_server(url, [
        _call("start_reply", {"topic_id": 7, "reply_to_post_number": 3}, 1),
        _call("start_reply", {"topic_id": 7}, 2),
        _call("reply", {"topic_id": 7, "raw": "The threshold holds at 0.57%."}, 3),
    ])

    assert _answer(responses[0])["post_id"] == 101
    # Asked twice, it is still one post.
    assert _answer(responses[1])["post_id"] == 101
    created = [p for p in stub.posted if p["path"] == "/posts.json"]
    assert len(created) == 1
    body = urllib.parse.parse_qs(created[0]["body"])
    assert body["raw"][0].startswith("*Thinking…*\n\n<!--")
    assert "qupa-cafe:thinking" in body["raw"][0]
    assert body["reply_to_post_number"] == ["3"]

    answer = _answer(responses[2])
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
