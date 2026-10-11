"""The wake viewer and the tray icon's reading of the journal."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import qupa_cafe.daemon as daemon
from qupa_cafe import browse, tray, wakes

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
        agent_dir="/agent",
        topic_id=59,
        post_number=3,
        title="Test",
        reason="you were mentioned",
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


def _response(message_id, model, fresh, written, read, out, *blocks):
    usage = {"input_tokens": fresh, "cache_creation_input_tokens": written,
             "cache_read_input_tokens": read, "output_tokens": out}
    return {"type": "assistant",
            "message": {"id": message_id, "model": model, "usage": usage, "content": list(blocks)}}


def test_a_wake_shows_the_tokens_it_took_counting_each_response_once(tmp_path, claude_home):
    """Claude Code writes a record for each block of a response, each with the
    response's whole usage, so counting records would count it twice."""
    thought = {"type": "thinking", "thinking": "hmm", "signature": "x"}
    said = {"type": "text", "text": "Nothing to add."}
    records = [
        WAKE[1],
        _response("msg_1", "claude-sonnet-5-5", 2, 18000, 30000, 140, thought),
        _response("msg_1", "claude-sonnet-5-5", 2, 18000, 30000, 140, said),
        _response("msg_2", "claude-sonnet-5-5", 3, 1000, 48000, 60, said),
    ]
    _transcript(claude_home, SESSION, records)
    [row] = wakes.recent(_journal(tmp_path), 20)

    usage = wakes.transcript_for(row).usage
    assert (usage.fresh, usage.cache_written, usage.cache_read, usage.output) == (5, 19000, 78000, 200)
    assert usage.total == 97205 and usage.models == ["claude-sonnet-5-5"]
    # No TTL split here, so the writes are priced as the five-minute cache's.
    assert round(usage.equivalent) == 5 + 19000 * 1.25 + 78000 * 0.1 + 200 * 5
    assert ("cost: 32.6k input-token equivalents, from 97.0k in (78.0k read from the cache, 19.0k written "
            "to it, 5 uncached) and 200 out; claude-sonnet-5-5") in "\n".join(wakes.render(row))
    assert browse.tokens_column(browse.Wake(row)).strip() == "32.6k eq"


def test_a_subagent_s_tokens_are_the_wake_s(tmp_path, claude_home):
    """Claude Code keeps each subagent's transcript in its own file beside the session's."""
    said = {"type": "text", "text": "Done."}
    path = _transcript(claude_home, SESSION, [WAKE[1], _response("msg_1", "claude-opus-5-5", 1, 100, 1000, 10, said)])
    subagents = path.with_suffix("") / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "agent-a1.jsonl").write_text(
        json.dumps(_response("msg_9", "claude-haiku-5-5", 2, 200, 2000, 20, said)), encoding="utf-8")
    [row] = wakes.recent(_journal(tmp_path), 20)

    usage = wakes.transcript_for(row).usage
    assert usage.total == 1111 + 2222 and usage.models == ["claude-opus-5-5", "claude-haiku-5-5"]


def test_a_wake_with_no_usage_recorded_shows_no_tokens(tmp_path, claude_home):
    _transcript(claude_home, SESSION, WAKE)
    [row] = wakes.recent(_journal(tmp_path), 20)
    assert "cost:" not in "\n".join(wakes.render(row))
    # As wide as a count, so the rows stay aligned.
    assert browse.tokens_column(browse.Wake(row)) == " " * 8


def test_each_token_is_weighed_at_its_model_s_prices():
    """Output at five times input, an hour's cache write at twice and five
    minutes' at 1.25 times, a cache read at a tenth, a twentieth on Opus 5.5
    and a fortieth on Fable 5.1, and fast mode at double."""
    split = {"ephemeral_1h_input_tokens": 1000, "ephemeral_5m_input_tokens": 400}
    numbers = {"input_tokens": 10, "cache_creation_input_tokens": 1400, "cache_creation": split,
               "cache_read_input_tokens": 10000, "output_tokens": 100}
    written = 1000 * 2 + 400 * 1.25
    assert wakes.equivalent(numbers, "claude-sonnet-5-5") == 10 + written + 1000 + 500
    assert wakes.equivalent(numbers, "claude-opus-5-5") == 10 + written + 500 + 500
    assert wakes.equivalent(numbers, "claude-fable-5-1") == 10 + written + 250 + 500
    fast = {**numbers, "speed": "fast"}
    assert wakes.equivalent(fast, "claude-opus-5-5") == 2 * (10 + written + 500 + 500)


def test_token_counts_are_short():
    assert [wakes.count(n) for n in (940, 12_345, 345_678, 1_234_567)] == ["940", "12.3k", "345k", "1.2M"]


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


def test_a_wake_with_no_session_is_listed_without_a_transcript(tmp_path, claude_home):
    [row] = wakes.recent(_journal(tmp_path, session=""), 20)
    assert "No session was recorded" in "\n".join(wakes.render(row))


def test_the_newest_wake_is_last(tmp_path):
    state = _journal(tmp_path, topic_id=1)
    journal = daemon.Journal(state)
    journal.begin(daemon.Wake("/a", 2, 1, "Second", "r", SESSION))
    assert [row.topic_id for row in wakes.recent(state, 20)] == [1, 2]
    assert [row.topic_id for row in wakes.recent(state, 1)] == [2]


def test_a_state_file_with_no_journal_shows_nothing(tmp_path):
    state = str(tmp_path / "state.sqlite3")
    daemon.Handled(state)
    assert wakes.recent(state, 20) == []
    assert wakes.status(state).stopped
    assert wakes.recent(str(tmp_path / "absent.sqlite3"), 20) == []


def test_a_relevance_wake_shows_its_score_and_a_mention_none(tmp_path, claude_home):
    state = _journal(tmp_path, score=0.83, reason="a post scored as relevant to your interests")
    journal = daemon.Journal(state)
    journal.begin(daemon.Wake("/a", 2, 1, "Second", "you were mentioned", SESSION))

    scored, mention = wakes.recent(state, 20)

    assert (scored.score, mention.score) == (0.83, None)
    assert "relevance 0.83" in "\n".join(wakes.render(scored))
    assert "relevance" not in "\n".join(wakes.render(mention))


def test_the_floor_is_read_from_the_journal(tmp_path):
    state = str(tmp_path / "state.sqlite3")
    assert wakes.status(state).floor is None

    daemon.Journal(state).record_floor({"similarity_floor": 0.72, "floor_min": 0.6, "floor_max": 0.9})

    status = wakes.status(state)
    assert (status.floor, status.floor_min, status.floor_max) == (0.72, 0.6, 0.9)
    assert "Relevance threshold 0.72" in wakes.floor_line(status)


def test_a_daemon_is_stopped_after_three_missed_polls():
    now = datetime.now(timezone.utc)
    assert not wakes.Status(now - timedelta(seconds=90), 60).stopped
    assert wakes.Status(now - timedelta(minutes=10), 60).stopped
    assert wakes.Status(None, 60).stopped


def test_a_wake_past_its_timeout_is_not_running_anywhere():
    """The wake list, the tray and the browser agree on one age."""
    now = datetime.now(timezone.utc)
    within, past = (
        wakes.Row((now - timedelta(seconds=age)).strftime("%Y-%m-%d %H:%M:%S"), None, "running",
                  SESSION, "/a", 7, 1, "T", "r")
        for age in (wakes.WAKE_TIMEOUT_SECONDS - 60, wakes.WAKE_TIMEOUT_SECONDS + 60)
    )

    assert wakes.outcome(within, None)[0] == "still running" and wakes.outcome(past, None)[0] == "did not finish"
    assert not tray.stale(within) and tray.stale(past)
    assert browse.still_running(within) and not browse.still_running(past)


def test_the_icon_follows_the_daemon(tmp_path):
    state = str(tmp_path / "state.sqlite3")
    settings = daemon.Settings(state_path=state)
    journal = daemon.Journal(state)

    assert tray.describe(settings)[0] == "stopped"

    journal.beat(60)
    assert tray.describe(settings)[0] == "polling"

    journal.begin(daemon.Wake("/a", 7, 1, "T", "r", SESSION))
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


def test_the_newer_kit_the_daemon_found_is_what_the_tray_reads(tmp_path):
    state = str(tmp_path / "daemon.sqlite3")
    assert wakes.newer_kit(state) is None
    journal = daemon.Journal(state)
    journal.record_kit("9.9.9")
    assert wakes.newer_kit(state) == "9.9.9"
    journal.record_kit(None)
    assert wakes.newer_kit(state) is None
