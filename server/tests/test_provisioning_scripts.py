"""Tests for scripts/40-configure-forum.py and scripts/45-provision-agent.py.

Each runs against a stub of the admin API that answers the reads the step
makes and records its writes.

Run from the repository root::

    python3 -m pytest server/tests -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import onboarding  # noqa: E402


def load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


configure = load("40-configure-forum.py", "configure_forum")
provisioning = load("45-provision-agent.py", "provision_agent")

POLICY_ID, INTERESTS_ID = 7, 8
HOOK_URL = "http://172.17.0.1:8091/"
SECRET = "0123456789abcdef0123456789abcdef"


class Forum(onboarding.Discourse):
    """The admin API as far as these steps read it. Writes go through
    Discourse._write, so a dry run prints them and a real run lands here."""

    def __init__(self, dry_run: bool = False, agent_exists: bool = False, key_exists: bool = False):
        super().__init__("https://forum.example.org", "k" * 64, "system", dry_run=dry_run)
        self.gets: list[str] = []
        self.writes: list[tuple[str, str, dict | None]] = []
        self.agent_exists = agent_exists
        self.key_exists = key_exists
        self.hooks: list[dict] = []

    def _request(self, method, path, data=None, json_body=None):
        if method != "GET":
            self.writes.append((method, path, json_body or data))
            return self._write_answer(method, path, json_body)
        self.gets.append(path)
        if path == "/admin/site_settings.json":
            return {"site_settings": [{"setting": "max_username_length", "value": 30}]}
        if path == "/u/sunada.json":
            return {"user": {"id": 2, "username": "sunada", "admin": False, "moderator": False}}
        if path == "/u/sunada-agent.json":
            if not self.agent_exists:
                raise RuntimeError(f"GET {path} -> 404: not found")
            return {"user": {"id": 3, "username": "sunada-agent", "trust_level": 1, "moderator": False}}
        if path == "/groups/agents.json":
            return {"group": {"id": 41}}
        if path == "/groups/agents/members.json":
            return {"members": [{"username": "sunada-agent"}] if self.agent_exists else []}
        if path.startswith("/search.json?q="):
            return {"topics": self._search(urllib.parse.unquote(path.split("=", 1)[1]))}
        if path == "/admin/api/keys.json":
            keys = [{"id": 5, "description": "agent: sunada-agent", "truncated_key": "abcd"}] if self.key_exists else []
            return {"keys": keys}
        if path == "/admin/api/web_hooks.json":
            return {"web_hooks": json.loads(json.dumps(self.hooks))}
        raise AssertionError(f"unexpected GET {path}")

    @staticmethod
    def _search(query: str) -> list[dict]:
        """A phrase finds its topic. Unquoted, the title's words match apart,
        and other topics that share them fill the page of results."""
        topics = {onboarding.POLICY_TOPIC_TITLE: POLICY_ID, onboarding.INTERESTS_TOPIC_TITLE: INTERESTS_ID}
        title = query[1:-1] if query.startswith('"') and query.endswith('"') else None
        if title in topics:
            return [{"id": topics[title], "title": title}]
        return [{"id": 100 + n, "title": f"Another topic about agents {n}"} for n in range(50)]

    def _write_answer(self, method, path, body):
        if method == "POST" and path == "/admin/api/web_hooks.json":
            hook = body["web_hook"]
            created = {
                "id": 11 + len(self.hooks),
                **{k: v for k, v in hook.items() if k != "web_hook_event_type_ids"},
                "web_hook_event_types": [{"id": i, "name": "user_created"} for i in hook["web_hook_event_type_ids"]],
            }
            self.hooks.append(created)
            return {"web_hook": created}
        if method == "PUT" and path.startswith("/admin/api/web_hooks/"):
            hook_id = int(path.rsplit("/", 1)[1].split(".")[0])
            target = next(h for h in self.hooks if h["id"] == hook_id)
            update = body["web_hook"]
            target.update({k: v for k, v in update.items() if k != "web_hook_event_type_ids"})
            target["web_hook_event_types"] = [{"id": i} for i in update["web_hook_event_type_ids"]]
            return {"web_hook": target}
        return {}


# ------------------------------------------------------- 45-provision-agent.py


def test_the_username_limit_is_read_once_however_many_agents_are_named():
    forum = Forum()
    for researcher in ("sunada", "rsmith", "a-researcher-with-a-long-name"):
        provisioning.agent_username_for(forum, researcher, quiet=True)
    assert forum.gets.count("/admin/site_settings.json") == 1


def test_provisioning_finds_the_topics_by_their_exact_titles(capsys):
    forum = Forum(dry_run=True)
    provisioning.provision(forum, "sunada", "forum.example.org", None, False)
    out = capsys.readouterr().out
    assert f"policy     id {POLICY_ID}" in out
    assert f"interests  id {INTERESTS_ID}" in out
    assert "NOT FOUND" not in out


def run_provisioning(monkeypatch, capsys, forum: Forum) -> str:
    monkeypatch.setattr(provisioning, "Discourse", lambda *args, **kwargs: forum)
    monkeypatch.setattr(sys, "argv", ["45-provision-agent.py", "sunada", "--dry-run"])
    monkeypatch.setenv("DISCOURSE_URL", forum.base_url)
    monkeypatch.setenv("DISCOURSE_API_KEY", forum.api_key)
    assert provisioning.main() == 0
    return capsys.readouterr().out


def test_a_dry_run_for_a_new_agent_says_a_real_run_would_deliver(monkeypatch, capsys):
    out = run_provisioning(monkeypatch, capsys, Forum(dry_run=True))
    assert "a real run would issue a key and send the onboarding message" in out
    assert "already been issued" not in out


def test_a_dry_run_for_an_agent_with_a_key_says_one_was_issued(monkeypatch, capsys):
    out = run_provisioning(monkeypatch, capsys, Forum(dry_run=True, agent_exists=True, key_exists=True))
    assert "No credential delivered, because one had already been issued" in out
    assert "a real run would issue" not in out


# ------------------------------------------------------- 40-configure-forum.py


@pytest.fixture(autouse=True)
def no_failures():
    configure.FAILURES.clear()
    yield
    configure.FAILURES.clear()


def hook_writes(forum: Forum) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _ in forum.writes if "web_hooks" in path]


def test_the_webhook_is_created_signed_and_pinged(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    forum = Forum()
    configure.ensure_provision_hook(forum)
    assert hook_writes(forum) == [("POST", "/admin/api/web_hooks.json"), ("POST", "/admin/api/web_hooks/11/ping.json")]
    sent = forum.writes[0][2]["web_hook"]
    assert sent == {
        "payload_url": HOOK_URL,
        "content_type": 1,
        "wildcard_web_hook": False,
        "active": True,
        "web_hook_event_type_ids": [304],
        "secret": SECRET,
    }
    assert not configure.FAILURES


def test_a_second_run_finds_the_webhook_and_adds_none(monkeypatch, capsys):
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    forum = Forum()
    configure.ensure_provision_hook(forum)
    forum.writes.clear()
    configure.ensure_provision_hook(forum)
    assert hook_writes(forum) == [("POST", "/admin/api/web_hooks/11/ping.json")]
    assert "unchanged  webhook 11" in capsys.readouterr().out
    assert len(forum.hooks) == 1


def test_a_new_secret_updates_the_webhook_in_place(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    forum = Forum()
    configure.ensure_provision_hook(forum)
    forum.writes.clear()
    monkeypatch.setenv("HOOK_SECRET", "another-secret-of-enough-length")
    configure.ensure_provision_hook(forum)
    assert hook_writes(forum) == [("PUT", "/admin/api/web_hooks/11.json"), ("POST", "/admin/api/web_hooks/11/ping.json")]
    assert len(forum.hooks) == 1 and forum.hooks[0]["secret"] == "another-secret-of-enough-length"


def test_an_inactive_webhook_is_switched_back_on(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    forum = Forum()
    configure.ensure_provision_hook(forum)
    forum.hooks[0]["active"] = False
    forum.writes.clear()
    configure.ensure_provision_hook(forum)
    assert ("PUT", "/admin/api/web_hooks/11.json") in hook_writes(forum)
    assert forum.hooks[0]["active"] is True


def test_without_a_secret_no_webhook_is_registered(monkeypatch, capsys):
    monkeypatch.delenv("HOOK_SECRET", raising=False)
    forum = Forum()
    configure.ensure_provision_hook(forum)
    assert forum.writes == [] and "/admin/api/web_hooks.json" not in forum.gets
    assert "skipped    HOOK_SECRET not set" in capsys.readouterr().out
    assert not configure.FAILURES


def test_a_secret_discourse_would_refuse_is_a_failure(monkeypatch):
    monkeypatch.setenv("HOOK_SECRET", "short")
    forum = Forum()
    configure.ensure_provision_hook(forum)
    assert forum.writes == []
    assert configure.FAILURES


def test_a_dry_run_names_the_step_and_not_the_secret(monkeypatch, capsys):
    secret = "Not-hex-so-the-redaction-would-miss-it"
    monkeypatch.setenv("HOOK_SECRET", secret)
    forum = Forum(dry_run=True)
    configure.ensure_provision_hook(forum)
    out = capsys.readouterr().out
    assert forum.writes == []
    assert "WOULD POST the webhook" in out
    assert secret not in out


def test_a_category_s_about_post_links_to_the_policy_after_its_description():
    """The first paragraph is the category's description, so the link goes
    after it; a run with new wording replaces the link rather than adding one."""
    link = configure.policy_link(12, 4, "Diary")
    assert "[agent policy](/t/12)" in link and "[rules for Diary](/t/12/4)" in link
    assert "every subcategory of Diary" in link
    # A comment opening a line would make it an HTML block, unrendered.
    assert not link.startswith("<!--") and link.endswith(configure.POLICY_LINK_MARKER)
    assert "rules for" not in configure.policy_link(12, None, "Research")

    about = "Each agent's diary of its researcher's work."
    once = configure.with_policy_link(about, link)
    assert once == about + "\n\n" + link
    newer = configure.policy_link(12, 5, "Diary")
    assert configure.with_policy_link(once, newer) == about + "\n\n" + newer
    assert configure.with_policy_link(once, link) == once


def test_the_edit_webhook_goes_to_the_bridge_beside_the_provisioning_one(monkeypatch, capsys):
    monkeypatch.setenv("HOOK_SECRET", SECRET)
    monkeypatch.setenv("BRIDGE_HOOK_SECRET", "the-bridge-s-own-secret")
    forum = Forum()
    configure.ensure_provision_hook(forum)
    configure.ensure_edit_hook(forum)
    assert len(forum.hooks) == 2
    sent = [body["web_hook"] for method, path, body in forum.writes if path == "/admin/api/web_hooks.json"][1]
    assert sent["payload_url"] == "http://172.17.0.1:8092/"
    assert sent["web_hook_event_type_ids"] == [103, 202] and sent["secret"] == "the-bridge-s-own-secret"

    forum.writes.clear()
    configure.ensure_edit_hook(forum)
    assert hook_writes(forum) == [("POST", "/admin/api/web_hooks/12/ping.json")]

    monkeypatch.delenv("BRIDGE_HOOK_SECRET")
    configure.ensure_edit_hook(forum)
    assert "skipped    BRIDGE_HOOK_SECRET not set" in capsys.readouterr().out
