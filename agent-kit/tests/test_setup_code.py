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


def test_the_credential_message_carries_the_code_and_not_the_bare_key(onboarding):
    message = onboarding.credential_message("https://forum.example.org", KEY, "the welcome topic")
    code = onboarding.setup_code("https://forum.example.org", KEY)
    assert code in message and KEY not in message and "%" not in message


def test_anything_else_is_not_a_setup_code():
    assert install.read_setup_code("https://forum.example.org") is None
    assert install.read_setup_code(KEY) is None
    assert install.read_setup_code("qupa-setup-not!base64") is None
    assert install.read_setup_code("qupa-setup-" + "aGVsbG8") is None  # "hello": no URL, no key


def test_the_installer_takes_a_setup_code_from_the_environment(monkeypatch):
    code = "qupa-setup-aHR0cHM6Ly9mb3J1bS5leGFtcGxlLm9yZwowMTIzNDU2Nzg5YWJjZGVm"
    monkeypatch.setenv("QUPA_SETUP_CODE", code)
    for name in ("QUPA_FORUM_URL", "QUPA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QUPA_API_USERNAME", "sunada-agent")
    monkeypatch.setenv("QUPA_RESEARCHER", "sunada")
    monkeypatch.setenv("QUPA_POLICY_TOPIC_ID", "11")
    settings = install.collect(non_interactive=True)
    assert settings["forum_url"] == "https://forum.example.org" and settings["api_key"] == "0123456789abcdef"


def interactive(monkeypatch, answers):
    """Run the installer's questions with ``answers`` by prompt; anything not
    given takes its default."""
    asked = []

    def ask(prompt, default=""):
        asked.append(prompt)
        given = answers.get(prompt)
        return given.pop(0) if isinstance(given, list) else given or default

    monkeypatch.setattr(install, "ask", ask)
    monkeypatch.setattr(install, "whoami", lambda url, key: ("sunada-agent", "") if key == KEY else (None, "The key was refused"))
    monkeypatch.setattr(install, "ask_the_forum_host", lambda s: {"researcher": "sunada", "policy_topic_id": "11"})
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    return install.collect(non_interactive=False), asked


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
