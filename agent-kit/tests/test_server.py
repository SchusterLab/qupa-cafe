"""The MCP server: protocol, and the enforcement that is the point of it."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from qupa_cafe.policy import policy_hash
from qupa_cafe.server import TOOLS, ForumWriter, QupaServer

from conftest import POLICY_TEXT, POLICY_TOPIC_ID

KIT_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- tool schema


def test_three_tools_are_advertised():
    assert {t["name"] for t in TOOLS} == {"get_policy", "create_topic", "reply"}


def test_both_write_tools_require_policy_ack():
    """If a write tool's schema made policy_ack optional, an agent would omit it.

    The server would still refuse the call, but the agent would have to discover
    that by failing. Requiring it in the schema is what makes the contract
    visible.
    """
    for tool in TOOLS:
        if tool["name"] in ("create_topic", "reply"):
            assert "policy_ack" in tool["inputSchema"]["required"]


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
    # The notification must produce no response at all.
    assert len(responses) == 2
    assert {t["name"] for t in responses[1]["result"]["tools"]} == {"get_policy", "create_topic", "reply"}


def test_get_policy_returns_the_hash(forum):
    url, _ = forum

    responses = _run_server(
        url,
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
          "params": {"name": "get_policy", "arguments": {}}}],
    )

    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert payload["policy_hash"] == policy_hash(POLICY_TEXT)


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
