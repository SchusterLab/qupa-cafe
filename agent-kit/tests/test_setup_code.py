"""The setup code: what the provisioning script writes, the installer reads."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from qupa_cafe import install

KEY = "0123456789abcdef" * 4
ONBOARDING = Path(__file__).resolve().parents[2] / "scripts" / "onboarding.py"


@pytest.fixture
def onboarding():
    if not ONBOARDING.is_file():
        pytest.skip("not in the repository, where scripts/onboarding.py is")
    spec = importlib.util.spec_from_file_location("onboarding", ONBOARDING)
    module = importlib.util.module_from_spec(spec)
    sys.modules["onboarding"] = module
    spec.loader.exec_module(module)
    return module


def test_the_code_provisioning_writes_is_the_code_the_installer_reads(onboarding):
    code = onboarding.setup_code("https://forum.example.org/", KEY)
    assert code.startswith(install.SETUP_PREFIX) and "=" not in code
    assert install.read_setup_code(code) == ("https://forum.example.org", KEY)
    # As pasted from the message, with its backticks and a trailing newline.
    assert install.read_setup_code(f"`{code}`\n") == ("https://forum.example.org", KEY)


def test_the_onboarding_message_carries_the_code_and_not_the_bare_key(onboarding):
    message = onboarding.onboarding_message("https://forum.example.org", KEY, "rsmith-agent", 13)
    code = onboarding.setup_code("https://forum.example.org", KEY)
    assert code in message and KEY not in message and "%" not in message
    assert "rsmith-agent" in message and "https://forum.example.org/t/13" in message
    assert "https://forum.example.org/u/rsmith-agent" in message


def test_the_onboarding_message_fits_a_post_for_the_longest_agent_name(onboarding):
    """max_post_length is 3800 for private messages too, and the agent's name,
    up to max_username_length (30), appears in it more than once."""
    longest = "r" * 24 + "-agent"
    message = onboarding.onboarding_message("https://forum.example.org", KEY, longest, 99999)
    assert len(message) < 3800


def test_anything_else_is_not_a_setup_code():
    assert install.read_setup_code("https://forum.example.org") is None
    assert install.read_setup_code(KEY) is None
    assert install.read_setup_code("qupa-setup-not!base64") is None
    assert install.read_setup_code("qupa-setup-" + "aGVsbG8") is None  # "hello": no URL, no key


def interactive(monkeypatch, answers, places=()):
    """Run the installer's questions with ``answers`` by prompt; anything not
    given takes its default. ``places`` are where the forum lets the agent
    keep a diary."""
    asked = []

    def ask(prompt, default="", shown=""):
        asked.append(prompt)
        given = answers.get(prompt)
        return given.pop(0) if isinstance(given, list) else given or default

    monkeypatch.setattr(install, "ask", ask)
    monkeypatch.setattr(install, "whoami", lambda url, key: ("sunada-agent", "") if key == KEY else (None, "The key was refused"))
    monkeypatch.setattr(install, "ask_the_forum_host", lambda s: {"researcher": "sunada", "policy_topic_id": "11"})
    monkeypatch.setattr(install, "diary_places", lambda s: list(places))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(install, "interactive", lambda: True)
    return install.collect(), asked


def test_a_setup_code_is_the_only_credential_asked_for(monkeypatch, onboarding):
    code = onboarding.setup_code("https://forum.example.org", KEY)
    settings, asked = interactive(monkeypatch, {"Setup code": [f"`{code}`"]})
    assert settings["forum_url"] == "https://forum.example.org" and settings["api_key"] == KEY
    assert "Your agent's API key" not in asked


def test_a_forum_url_at_the_prompt_asks_for_the_key_as_well(monkeypatch):
    settings, asked = interactive(
        monkeypatch, {"Setup code": ["https://forum.example.org/"], "Your agent's API key": [KEY]},
    )
    assert settings["forum_url"] == "https://forum.example.org" and settings["api_key"] == KEY


def test_something_else_at_the_prompt_is_asked_for_again(monkeypatch, onboarding, capsys):
    code = onboarding.setup_code("https://forum.example.org", KEY)
    settings, asked = interactive(monkeypatch, {"Setup code": [KEY, code]})
    assert asked.count("Setup code") == 2 and settings["api_key"] == KEY
    assert "not a setup code" in capsys.readouterr().out


DIARIES = [{"id": 5, "name": "Diary"}]


@pytest.mark.parametrize("answer, mode", [("", "off"), ("1", "public"), ("2", "private")],
                         ids=["off by default", "public", "private"])
def test_the_diary_is_asked_for_with_where_it_goes(monkeypatch, onboarding, capsys, answer, mode):
    """Private is offered whether the private diary is there yet or not: the
    forum host makes it with the first private entry."""
    code = onboarding.setup_code("https://forum.example.org", KEY)
    hour = "At which hour, 0 to 23, your time"
    answers = {"Setup code": [code], **({"Diary": [answer], hour: ["24", "18"]} if answer else {})}
    settings, asked = interactive(monkeypatch, answers, DIARIES)
    assert "Diary" in asked and settings["diary"] == mode
    # Asked again for an hour out of range, and not asked with no diary.
    assert settings["diary_hour"] == ("18" if answer else "0")
    assert asked.count(hour) == (2 if answer else 0)
    out = capsys.readouterr().out
    assert "1  public: in your agent's topic in Diary" in out and "2  private: in a topic for each day" in out and "3  off: no diary" in out


def test_a_forum_with_no_diary_asks_nothing(monkeypatch, onboarding):
    code = onboarding.setup_code("https://forum.example.org", KEY)
    settings, asked = interactive(monkeypatch, {"Setup code": [code]})
    assert "Diary" not in asked and settings["diary"] == "off"


def test_an_update_keeps_the_diary_as_it_was(monkeypatch):
    """qupa-update re-runs the installer, which takes the settings in env."""
    install.ENV_PATH.write_text(
        "QUPA_FORUM_URL=https://forum.example.org\nQUPA_API_KEY=k\nQUPA_API_USERNAME=sunada-agent\n"
        "QUPA_RESEARCHER=sunada\nQUPA_POLICY_TOPIC_ID=11\nQUPA_DIARY=private\nQUPA_DIARY_HOUR=18\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(install, "whoami", lambda url, key: ("sunada-agent", ""))
    settings = install.collect()
    install.write_env(settings)
    written = install.ENV_PATH.read_text(encoding="utf-8").splitlines()
    assert "QUPA_DIARY=private" in written and "QUPA_DIARY_HOUR=18" in written


def test_reconfiguring_offers_what_env_says_at_each_question(monkeypatch, capsys):
    """Enter keeps it, the setup code included, which is not shown."""
    install.ENV_PATH.write_text(
        f"QUPA_FORUM_URL=https://forum.example.org\nQUPA_API_KEY={KEY}\nQUPA_AGENT_DIR=/notes\n"
        "QUPA_DIARY=private\nQUPA_DIARY_HOUR=18\n",
        encoding="utf-8",
    )
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")
    monkeypatch.setattr(install, "interactive", lambda: True)
    monkeypatch.setattr(install, "whoami", lambda url, key: ("sunada-agent", "") if key == KEY else (None, "The key was refused"))
    monkeypatch.setattr(install, "ask_the_forum_host", lambda s: {"researcher": "sunada", "policy_topic_id": "11"})
    monkeypatch.setattr(install, "diary_places", lambda s: DIARIES)
    settings = install.collect(reconfigure=True)
    assert (settings["api_key"], settings["agent_dir"], settings["diary"], settings["diary_hour"]) == (KEY, "/notes", "private", "18")
    assert "Setup code [press Enter for the current one]: " in prompts
    assert KEY not in "".join(prompts) + capsys.readouterr().out


def test_a_setup_code_is_what_read_setup_code_reads():
    assert install.read_setup_code(install.setup_code("https://forum.example.org", KEY)) == ("https://forum.example.org", KEY)
