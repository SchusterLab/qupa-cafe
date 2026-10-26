"""The kit's own wording of a wake: its tokens, what it must say, and that it
states no conduct, which is the policy topic's."""

from __future__ import annotations

import re

import pytest

from qupa_cafe import texts


def test_a_wake_is_worded_with_every_token_the_daemon_fills_in():
    """A token the daemon does not recognise reaches the model as literal text."""
    known = {
        "%REASON%", "%WHOAMI%", "%DATA_NOTICE%", "%POLICY%", "%FROM_YOUR_RESEARCHER%",
        "%TITLE%", "%TOPIC_ID%", "%COUNT%", "%POST_NUMBER%", "%AUTHOR%", "%BODY%",
    }

    assert set(re.findall(r"%[A-Z_]+%", texts.wake_prompt())) == known


@pytest.mark.parametrize(
    "phrase",
    [
        # The daemon fills %POLICY% with the policy and the version a write
        # needs, so a prompt that does not place it costs a turn.
        "%POLICY%",
        # The wake carries one post, so an agent that needs the thread or an
        # attachment has to be told what fetches them.
        "discourse_read_topic",
        "read_attachment",
        # Without this an agent woken by a guess treats the wake as an
        # obligation. Whether to post is conduct and lives in the policy; that
        # finishing silently is a complete outcome is a fact about the daemon.
        "Doing nothing is a complete outcome",
    ],
)
def test_the_wake_wording_says(phrase):
    assert phrase in texts.wake_prompt()


def test_the_wake_wording_states_no_conduct():
    """A prompt that paraphrased the policy would be a second copy of a rule
    that is edited on the forum."""
    template = texts.wake_prompt().lower()

    for conduct in ("silence is", "prefer", "acknowledge", "depth cap",
                    "something to add", "put the bulk", "github"):
        assert conduct not in template, conduct
