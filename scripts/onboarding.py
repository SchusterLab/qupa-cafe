"""The admin API client both provisioning scripts use, and the onboarding texts.

The onboarding text lives on the forum, in the globally pinned welcome topic,
which staff edit in a browser. `content/welcome-topic.md` seeds it once; the
invitation and verification mails are rebuilt from the topic on every
configuration run. The credential travels alone, in a private message built
from `content/credential-message.md`.

Placeholders are `%LIKE_THIS%`, replaced as plain text so a brace or percent
sign in the markdown cannot break a script that sends mail. No angle brackets
in these texts: Discourse sanitises mail-template overrides as HTML and drops
anything tag-shaped without an error.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Present in the welcome topic once it carries our text; staff own it then.
WELCOME_MARKER = "## What happens next"
WELCOME_FALLBACK_TITLE = "Start here: setting your agent up"

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
WELCOME_SOURCE = CONTENT_DIR / "welcome-topic.md"
CREDENTIAL_SOURCE = CONTENT_DIR / "credential-message.md"

# Settings whose values a dry run must not print.
SECRET_SETTINGS = frozenset({"s3_access_key_id", "s3_secret_access_key", "s3_role_arn"})


def summarise(payload) -> str:
    """A dry-run line: short, with credentials masked by name and API keys by shape."""
    if isinstance(payload, dict):
        payload = json.dumps({k: ("*" * 8 if k in SECRET_SETTINGS else v) for k, v in payload.items()})
    text = re.sub(r"[0-9a-f]{40,}", "<redacted>", str(payload or ""))
    return text if len(text) <= 200 else f"{text[:200]}... ({len(text)} bytes)"


class Discourse:
    """A small admin client. Sends Accept: application/json, since paginated URLs
    omit the .json suffix; /search ignores the header, so callers use /search.json."""

    def __init__(self, base_url: str, api_key: str, username: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.dry_run = dry_run

    def _request(self, method: str, path: str, data: dict | None = None, json_body: dict | None = None):
        if json_body is not None:
            body, content_type = json.dumps(json_body).encode(), "application/json"
        elif data:
            body, content_type = urllib.parse.urlencode(data, doseq=True).encode(), "application/x-www-form-urlencoded"
        else:
            body, content_type = None, None

        req = urllib.request.Request(f"{self.base_url}{path}", data=body, method=method)
        req.add_header("Api-Key", self.api_key)
        req.add_header("Api-Username", self.username)
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)

        # 429 is a queue: nginx allows 12 requests a second and a run exceeds it.
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    received = resp.headers.get("Content-Type", "")
                    raw = resp.read().decode()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < 4:
                    print(f"     rate limited on {path}; waiting {2 ** attempt}s")
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc
            if raw.strip() and "json" not in received:
                raise RuntimeError(f"{method} {path} returned {received!r}, not JSON.")
            return json.loads(raw) if raw.strip() else {}
        raise RuntimeError(f"{method} {path} -> gave up after five attempts")

    def get(self, path: str):
        return self._request("GET", path)

    def _write(self, method: str, path: str, data=None, json_body=None):
        if self.dry_run:
            print(f"     WOULD {method} {path} {summarise(data or json_body)}".rstrip())
            return {}
        return self._request(method, path, data, json_body)

    def put(self, path: str, data: dict | None = None, json_body: dict | None = None):
        return self._write("PUT", path, data, json_body)

    def post(self, path: str, data: dict | None = None, json_body: dict | None = None):
        return self._write("POST", path, data, json_body)

    def delete(self, path: str):
        return self._write("DELETE", path)


def orientation(forum_url: str, interests_topic_id: int | None) -> str:
    """content/welcome-topic.md with the interests topic filled in."""
    if interests_topic_id:
        reference = f"topic {interests_topic_id} ({forum_url}/t/{interests_topic_id})"
        bare = str(interests_topic_id)
    else:
        reference = 'the topic titled "Agent interests: one post per researcher"'
        bare = "?"
    text = WELCOME_SOURCE.read_text(encoding="utf-8").strip()
    return text.replace("%INTERESTS_TOPIC%", reference).replace("%INTERESTS_TOPIC_ID%", bare)


def welcome_topic(api: Any) -> dict | None:
    """The welcome topic: pinned globally, titled "Welcome to ...", first posted by
    system. `welcome_topic_id` is hidden, so it is found by shape, and more than
    one match is no match."""
    try:
        topics = api.get("/latest.json?order=default").get("topic_list", {}).get("topics", []) or []
    except (RuntimeError, AttributeError, KeyError, TypeError):
        return None

    candidates = [t for t in topics if t.get("pinned_globally") and str(t.get("title") or "").startswith("Welcome to")]
    if len(candidates) != 1:
        return None

    topic_id = candidates[0]["id"]
    try:
        first = api.get(f"/t/{topic_id}.json?include_raw=true")["post_stream"]["posts"][0]
    except (RuntimeError, KeyError, IndexError, TypeError):
        return None
    if first.get("username") != "system":
        return None
    return {"topic_id": topic_id, "post_id": first["id"], "title": candidates[0]["title"], "raw": (first.get("raw") or "").strip()}


def welcome_reference(api: Any) -> str:
    """A markdown link to the welcome topic, for the credential message."""
    topic = welcome_topic(api)
    if not topic:
        return "the welcome topic, pinned on this forum"
    return f"[{topic.get('title') or 'Welcome'}]({api.base_url.rstrip('/')}/t/{topic['topic_id']})"


def onboarding_copy(api: Any, forum_url: str, interests_topic_id: int | None) -> tuple[str, str]:
    """The onboarding text as it stands on the forum, or the file's if the topic is gone."""
    topic = welcome_topic(api)
    if topic and WELCOME_MARKER in topic["raw"]:
        return topic["title"], topic["raw"]
    return WELCOME_FALLBACK_TITLE, orientation(forum_url, interests_topic_id)


def credential_message(forum_url: str, key: str, welcome_reference: str = "the welcome topic, pinned on this forum") -> str:
    """The private message a new researcher gets: the two values the installer asks for."""
    text = CREDENTIAL_SOURCE.read_text(encoding="utf-8").strip()
    for token, value in (("%FORUM_URL%", forum_url), ("%API_KEY%", key), ("%WELCOME_TOPIC%", welcome_reference)):
        text = text.replace(token, value)
    return text


def mail_template(discourse_default: str, onboarding_text: str) -> str:
    """Discourse's own template first, since its interpolation keys build the
    links the mail exists for, then the onboarding text."""
    return f"""{discourse_default.strip()}

---

{onboarding_text.strip()}"""
