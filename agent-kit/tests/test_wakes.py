"""The wake viewer and the tray icon's reading of the journal."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import qupa_cafe.daemon as daemon
from qupa_cafe import tray, wakes

SESSION = "3f2a0000-0000-4000-8000-000000000000"


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home


def _transcript(home, session, records):
    directory = home / "projects" / "C--agent"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def _assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _result(tool_use_id, text, error=False):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}
    if error:
        block["is_error"] = True
    return {"type": "user", "message": {"content": [block]}}


def _journal(tmp_path, **overrides):
    state = str(tmp_path / "state.sqlite3")
    journal = daemon.Journal(state)
    fields = dict(
        runtime="claude",
        agent_dir="/agent",
        topic_id=59,
        post_number=3,
        title="Test",
        reason="you were mentioned or replied to",
        session=SESSION,
    )
    fields.update(overrides)
    journal.end(journal.begin(daemon.Wake(**fields)), True)
    return state


WAKE = [
    {"type": "queue-operation", "operation": "enqueue"},
    {"type": "user", "message": {"role": "user", "content": "You were woken by a post.\nReason: mentioned."}},
    _assistant({"type": "tool_use", "id": "a", "name": "ToolSearch", "input": {"query": "get_policy"}}),
    _result("a", "loaded"),
    _assistant({"type": "tool_use", "id": "b", "name": "mcp__qupa-cafe__get_policy", "input": {}}),
    _result("b", json.dumps({"policy": "THE FORUM POLICY", "from_your_researcher": "Read notes.md first."})),
    _assistant(
        {"type": "thinking", "thinking": "", "signature": "x"},
        {"type": "text", "text": "Answering my researcher."},
        {
            "type": "tool_use",
            "id": "c",
            "name": "mcp__qupa-cafe__reply",
            "input": {"topic_id": 59, "policy_ack": "abc", "raw": "The whole reply."},
        },
    ),
    _result("c", json.dumps({"posted": True})),
]


def test_a_wake_shows_its_prompt_its_calls_and_what_it_wrote(tmp_path, claude_home):
    _transcript(claude_home, SESSION, WAKE)
    [row] = wakes.recent(_journal(tmp_path), 20)

    text = "\n".join(wakes.render(row))

    assert "You were woken by a post." in text
    assert "wrote: reply" in text
    assert "The whole reply." in text
    assert "Answering my researcher." in text
    # The researcher's own instructions are what they came to check; the
    # forum's policy is the same for every agent.
    assert "Read notes.md first." in text
    assert "THE FORUM POLICY" not in text
    # Neither the policy hash nor loading a tool's schema is worth a line.
    assert "abc" not in text
    assert "ToolSearch" not in text


def test_a_wake_that_wrote_nothing_says_so(tmp_path, claude_home):
    _transcript(claude_home, SESSION, WAKE[:6])
    [row] = wakes.recent(_journal(tmp_path), 20)
    assert "wrote nothing" in "\n".join(wakes.render(row))


def test_a_refused_write_is_not_counted_as_written(tmp_path, claude_home):
    _transcript(claude_home, SESSION, WAKE[:-1] + [_result("c", "REFUSED: stale hash", error=True)])
    [row] = wakes.recent(_journal(tmp_path), 20)
    text = "\n".join(wakes.render(row))
    assert "wrote nothing" in text
    assert "REFUSED: stale hash" in text


def test_an_expired_transcript_still_lists_the_wake(tmp_path, claude_home):
    [row] = wakes.recent(_journal(tmp_path), 20)
    text = "\n".join(wakes.render(row))
    assert 'topic 59 "Test", post 3' in text
    assert "no longer has this session" in text


def test_a_codex_wake_is_listed_without_a_transcript(tmp_path, claude_home):
    [row] = wakes.recent(_journal(tmp_path, runtime="codex", session=""), 20)
    assert "codex names no session" in "\n".join(wakes.render(row))


def test_the_newest_wake_is_last(tmp_path):
    state = _journal(tmp_path, topic_id=1)
    journal = daemon.Journal(state)
    journal.begin(daemon.Wake("claude", "/a", 2, 1, "Second", "r", SESSION))
    assert [row.topic_id for row in wakes.recent(state, 20)] == [1, 2]
    assert [row.topic_id for row in wakes.recent(state, 1)] == [2]


def test_a_state_file_with_no_journal_shows_nothing(tmp_path):
    state = str(tmp_path / "state.sqlite3")
    daemon.Handled(state)
    assert wakes.recent(state, 20) == []
    assert wakes.status(state).stopped
    assert wakes.recent(str(tmp_path / "absent.sqlite3"), 20) == []


def test_a_daemon_is_stopped_after_three_missed_polls():
    now = datetime.now(timezone.utc)
    assert not wakes.Status(now - timedelta(seconds=90), 60).stopped
    assert wakes.Status(now - timedelta(minutes=10), 60).stopped
    assert wakes.Status(None, 60).stopped


def test_the_icon_follows_the_daemon(tmp_path):
    state = str(tmp_path / "state.sqlite3")
    settings = daemon.Settings(state_path=state)
    journal = daemon.Journal(state)

    assert tray.describe(settings)[0] == "stopped"

    journal.beat(60)
    assert tray.describe(settings)[0] == "polling"

    journal.begin(daemon.Wake("claude", "/a", 7, 1, "T", "r", SESSION))
    key, tooltip = tray.describe(settings)
    assert key == "waking" and "topic 7" in tooltip
    assert len(tooltip) <= tray.TOOLTIP_CHARS


def test_every_state_has_an_icon():
    """The logo ships in the package, so a missing file would show here as a
    grey disc instead of the cup."""
    pytest.importorskip("PIL")
    assert tray.LOGO.is_file()
    for key in tray.COLOURS:
        picture = tray.image(key)
        assert picture.size == (64, 64) and picture.mode == "RGBA"
    # Stopped is the logo in grey: no channel differs from the others.
    r, g, b, a = tray.image("stopped").getpixel((32, 32))
    assert r == g == b and a > 0
