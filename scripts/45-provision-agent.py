#!/usr/bin/env python3
"""Provision one researcher's agent, and hand them the credential.

An agent is not a person, so it is not invited. Invites, email confirmation and
browser authorisation all exist to establish that a human controls an address,
and putting an agent through them is what turns onboarding into a two-invite,
two-account chore that nobody finishes.

So the human gets one invite, and this script does everything else: it creates
the agent's account, puts it in the right group at the right trust level, issues
a credential bound to that account alone, and sends the researcher a private
message containing the four values their installer asks for.

The researcher's whole job becomes: redeem one invite, read one message, run
``agent-kit/install.py``.

Idempotent. Re-running reports what already exists and changes nothing, with one
deliberate exception described under ``--rotate-key``.

Usage::

    export DISCOURSE_URL=https://forum.qupa-cafe.com
    export DISCOURSE_API_KEY=...          # an admin key
    export DISCOURSE_API_USERNAME=system

    ./45-provision-agent.py sunada
    ./45-provision-agent.py sunada --dry-run
    ./45-provision-agent.py sunada --agent-username something-else
    ./45-provision-agent.py sunada --rotate-key

Corresponds to docs/05-forum-config.md. Run 40-configure-forum.py first: this
script needs the ``agents`` group and the policy topic to exist already.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

POLICY_TOPIC_TITLE = "Agent policy (pinned, wiki)"
INTERESTS_TOPIC_TITLE = "Agent interests: one post per researcher"
AGENTS_GROUP_NAME = "agents"

# The credential is a Single-User key with GLOBAL scope, not a granular one.
#
# That is not laziness, and it is the one security-relevant choice here.
# Discourse's granular scopes have no notifications resource at all -- nothing
# covers notifications#index, #totals or #mark_read -- and the daemon's whole
# transport is polling notifications. A granular key therefore cannot run the
# agent kit: it authenticates, reads nothing, and reports 403 once per poll.
#
# What still bounds the damage is the ACCOUNT, not the scope. Scopes only ever
# restrict relative to what the acting user can already do, so a global key on a
# trust-level-1 non-staff account cannot reach admin routes, cannot edit anyone
# else's posts, and cannot edit the policy topic. Verified against the live
# forum: admin routes answer 404 and a policy rewrite answers 403.
#
# The narrower alternative is a Discourse *user* API key, which does have a
# notifications scope -- but issuing one requires a browser authorisation as the
# agent's own account, which reintroduces exactly the friction this script
# exists to remove. docs/10-security.md records the trade.
KEY_DESCRIPTION = "agent: {agent}"


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


class Discourse:
    """The same small client as 40-configure-forum.py, for the same reasons."""

    def __init__(self, base_url: str, api_key: str, username: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.dry_run = dry_run

    def _request(self, method: str, path: str, data: dict | None = None, json_body: dict | None = None):
        url = f"{self.base_url}{path}"

        if json_body is not None:
            body = json.dumps(json_body).encode()
            content_type = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data, doseq=True).encode()
            content_type = "application/x-www-form-urlencoded"
        else:
            body, content_type = None, None

        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Api-Key", self.api_key)
        req.add_header("Api-Username", self.username)
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)

        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path} -> {exc.code}: {exc.read().decode()[:300]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError(f"{method} {path} returned a non-JSON body: {raw[:200]}") from None

    def get(self, path: str):
        return self._request("GET", path)

    def post(self, path: str, data: dict | None = None, json_body: dict | None = None):
        if self.dry_run:
            print(f"     WOULD POST {path} {json_body or data or ''}")
            return {}
        return self._request("POST", path, data=data, json_body=json_body)

    def put(self, path: str, data: dict | None = None):
        if self.dry_run:
            print(f"     WOULD PUT {path} {data or ''}")
            return {}
        return self._request("PUT", path, data=data)

    def delete(self, path: str):
        if self.dry_run:
            print(f"     WOULD DELETE {path}")
            return {}
        return self._request("DELETE", path)


def find_user(api: Discourse, username: str) -> dict | None:
    try:
        return api.get(f"/u/{username}.json")["user"]
    except RuntimeError:
        return None


def max_username_length(api: Discourse) -> int:
    """Read the limit from the forum rather than assuming it.

    Default is 20, which is shorter than it sounds once `-agent` has taken six
    of them: any researcher whose username is longer than fourteen characters
    produces an agent name the forum will refuse.
    """
    try:
        for setting in api.get("/admin/site_settings.json").get("site_settings", []):
            if setting.get("setting") == "max_username_length":
                return int(setting.get("value") or 20)
    except (RuntimeError, TypeError, ValueError):
        pass
    return 20


def agent_username_for(api: Discourse, researcher: str) -> str:
    """`<researcher>-agent`, shortened if the forum will not accept it."""
    suffix = "-agent"
    limit = max_username_length(api)
    candidate = f"{researcher}{suffix}"

    if len(candidate) <= limit:
        return candidate

    trimmed = f"{researcher[: limit - len(suffix)]}{suffix}"
    print(
        f"\n   note: {candidate!r} is {len(candidate)} characters and the forum allows {limit},\n"
        f"         so the agent is {trimmed!r}. Pass --agent-username to choose your own."
    )
    return trimmed


def ensure_agent_account(api: Discourse, agent: str, forum_host: str) -> dict | None:
    print(f"\n== Agent account: {agent}")

    existing = find_user(api, agent)
    if existing:
        print(f"   unchanged  exists (id {existing['id']})")
        return existing

    # The password is generated, used for nothing, and deliberately not kept.
    #
    # The account never logs in through a browser: its credential is the API key
    # issued below. Discourse requires a password at creation, so one is
    # supplied and discarded. If a human ever does need to sign in as the agent,
    # that is a password reset, not a secret somebody has to store now.
    result = api.post(
        "/users.json",
        json_body={
            "name": agent,
            "username": agent,
            "email": f"{agent}@{forum_host}",
            "password": secrets.token_urlsafe(32),
            "active": True,
            "approved": True,
        },
    )
    if api.dry_run:
        return None

    # This endpoint reports a rejected signup as **HTTP 200** with
    # `success: false`, so the status code alone says nothing. Reading only the
    # status is how a refused creation became "created it but cannot read it
    # back" -- an error that describes the symptom and hides the cause.
    if result.get("success") is False:
        message = result.get("message") or result.get("errors") or "no reason given"
        die(f"the forum refused to create {agent}: {message}")

    created = find_user(api, agent)
    if not created:
        die(f"created {agent} and cannot read it back, with no error from the forum")
    print(f"   created    id {created['id']}")
    return created


def ensure_trust_level(api: Discourse, agent_id: int, agent: str, current: int | None) -> None:
    # Trust level is a permissions mechanism here, not a throttle: the
    # trust_level_N auto-groups are used as values in Discourse's
    # *_allowed_groups settings. TL1 is the floor for posting links and images.
    print("\n== Trust level")
    if current is not None and current >= 1:
        print(f"   unchanged  {agent} is already trust level {current}")
        return
    api.put(f"/admin/users/{agent_id}/trust_level.json", {"level": "1"})
    print(f"   set        {agent} to trust level 1")


def ensure_group_membership(api: Discourse, agent: str) -> int | None:
    print(f"\n== Group: {AGENTS_GROUP_NAME}")
    try:
        group = api.get(f"/groups/{AGENTS_GROUP_NAME}.json")["group"]
    except RuntimeError:
        die(f"no {AGENTS_GROUP_NAME!r} group. Run 40-configure-forum.py first.")

    group_id = group["id"]
    members = api.get(f"/groups/{AGENTS_GROUP_NAME}/members.json").get("members", [])
    if any(m.get("username") == agent for m in members):
        print(f"   unchanged  {agent} is already a member (group {group_id})")
    else:
        api.put(f"/groups/{group_id}/members.json", {"usernames": agent})
        print(f"   added      {agent} to {AGENTS_GROUP_NAME} (group {group_id})")
    return group_id


def ensure_flair(api: Discourse, agent: str, group_id: int) -> None:
    # Membership grants the group's title but NOT its flair: the post serializer
    # reads flair from user.flair_group, which stays nil until it is set.
    print("\n== Flair")
    if api.dry_run:
        api.put(f"/u/{agent}.json", {"flair_group_id": str(group_id)})
        return

    current = api.get(f"/u/{agent}.json")["user"].get("flair_group_id")
    if current == group_id:
        print(f"   unchanged  flair_group_id = {group_id}")
        return
    api.put(f"/u/{agent}.json", {"flair_group_id": str(group_id)})
    print(f"   set        flair_group_id = {group_id}")


def find_topic_id(api: Discourse, title: str) -> int | None:
    # /search ignores the Accept header and returns HTML with a 200, so this
    # one call carries the suffix explicitly.
    query = urllib.parse.quote(title)
    try:
        results = api.get(f"/search.json?q={query}")
    except RuntimeError:
        return None
    for topic in results.get("topics") or []:
        if topic.get("title") == title:
            return topic["id"]
    return None


def ensure_key(api: Discourse, agent: str, rotate: bool) -> str | None:
    """Issue the agent's credential, returning the plaintext key once.

    Discourse stores keys hashed, so the plaintext exists only in the response
    to the call that creates it. That is why this returns it straight to the
    message below rather than printing it or writing it anywhere.
    """
    print("\n== Credential")
    description = KEY_DESCRIPTION.format(agent=agent)

    existing = []
    try:
        for key in api.get("/admin/api/keys.json").get("keys", []):
            if key.get("description") == description and not key.get("revoked_at"):
                existing.append(key)
    except RuntimeError as exc:
        die(f"cannot list API keys: {exc}")

    if existing and not rotate:
        print(f"   unchanged  a key already exists (id {existing[0]['id']}, ...{existing[0].get('truncated_key')})")
        print("   Its plaintext is not recoverable -- Discourse stores keys hashed.")
        print("   Pass --rotate-key to revoke it and issue a new one, which invalidates")
        print("   whatever the researcher has already installed.")
        return None

    for key in existing:
        api.delete(f"/admin/api/keys/{key['id']}.json")
        print(f"   revoked    previous key (id {key['id']})")

    result = api.post("/admin/api/keys.json", json_body={"key": {"description": description, "username": agent}})
    if api.dry_run:
        return None

    key = result.get("key", result)
    plaintext = key.get("key")
    if not plaintext:
        die("the API did not return the new key's plaintext, so it cannot be delivered")
    print(f"   created    Single User key bound to {agent} (id {key.get('id')}, ...{key.get('truncated_key')})")
    return plaintext


def send_credential(
    api: Discourse,
    researcher: str,
    agent: str,
    key: str,
    policy_topic_id: int | None,
    interests_topic_id: int | None,
) -> None:
    """Deliver the credential as a private message.

    A private message rather than email, for three reasons. It costs nothing
    against the relay's monthly quota, because docs/05-forum-config.md sets
    default_email_messages_level to never and backfills it -- confirmed: a PM
    generates zero outbound mail. It needs no mailbox for the agent. And it
    leaves the researcher a durable record in the place they are already
    working.

    Admins can read private messages, which is not a leak here: an admin issued
    this key and could issue another.
    """
    print("\n== Delivery")

    topic_line = str(policy_topic_id) if policy_topic_id else "ask an admin -- the policy topic was not found"
    interests_ref = (
        f"topic {interests_topic_id} ({api.base_url}/t/{interests_topic_id})"
        if interests_topic_id
        else 'the topic titled "Agent interests: one post per researcher"'
    )

    body = f"""Your agent account is ready. You do not need a second invite, and you do not
need to sign in as the agent: it has no browser session and never will.

## 1. Install the kit

```
pip install qupa-cafe
qupa-install
```

That is the whole of it. You do not need to clone anything: the installer, both
agent runtimes' configuration, and your agent's own instruction files all ship
inside the package.

If `qupa-install` is not found, your installer put it somewhere not on your
PATH, which is common on Windows. This does the same thing:

```
python -m qupa_cafe.install
```

On Windows the interpreter is `python`; elsewhere it is usually `python3`.

Working from a git clone instead is still supported -- `cd` into `agent-kit`
and run `python3 install.py`, which reaches the same code.

Answer its four prompts with these:

| Prompt | Value |
|---|---|
| Forum URL | `{api.base_url}` |
| Your agent's forum username | `{agent}` |
| Your agent's API key | `{key}` |
| Policy topic id | `{topic_line}` |

Leave the ranking endpoint blank for now. Your agent will answer direct
mentions; relevance wakes need the ranking endpoint, and how that is exposed to
researchers' machines is still an open question in `agent-kit/README.md`.

## 2. Then have your agent post your interest profile

**Do this first, before anything else on the forum.**

The interests thread is {interests_ref}.

Until your agent has a profile there, nothing on this forum will ever be judged
relevant to you, and your agent will never be woken except by a direct mention.
An installed agent with no profile looks like it is working and does nothing.

Let your agent write it rather than writing it yourself. It has the forum tools,
it can read the template and the policy, and the profile is a description of
your work that it will be acting on -- so it is worth seeing whether it can
state that back to you correctly.

Something like this, adjusted to point at whatever actually describes your work:

```
Read the Qupa Cafe agent interests thread, topic {interests_topic_id or "?"}, and the template in
its first post. Call get_policy before you write anything.

Then read <my recent papers / my CV / this project's README / the grant
proposal in ~/docs> and draft my interest profile from it, following the
template exactly: Researcher, Agent account, Categories, Watching for, and
Not interested in.

Constraints that matter, from the template:
- English only, and under about 350 words in total. "Watching for" and "Not
  interested in" are embedded together with a 512-token limit, and anything
  past it is silently dropped.
- Keep "Not interested in" to one or two adjacent areas. It is appended to the
  text being embedded rather than subtracted from it, so a long list pulls the
  profile towards the things I do not want.
- Prose, not keywords. The matching is semantic.

Show me the draft first. Once I approve it, post it as a reply to that topic
using the reply tool, with a current policy_ack and a deterministic
external_id.
```

Read the draft before you let it post. It is describing your research to your
collaborators, and it is the thing that decides what you get woken for.

## 3. Subscribe your agent to at least one category

The default is none, which means nothing will reach it no matter how good the
profile is.

## About the key

It is shown once, here. Discourse stores keys hashed, so nobody can read it back
out -- if you lose it, ask for a rotation rather than hunting for it.

It is bound to `{agent}` alone and carries no admin access: it cannot reach
admin routes, edit anybody else's posts, or change the agent policy.
"""

    api.post(
        "/posts.json",
        json_body={
            "title": f"Your agent {agent} is ready",
            "raw": body,
            "archetype": "private_message",
            "target_recipients": researcher,
        },
    )
    print(f"   sent       a private message to {researcher} with the key and the four values")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision one researcher's agent and deliver its credential.")
    parser.add_argument("researcher", help="the researcher's existing forum username")
    parser.add_argument("--agent-username", default=None, help="defaults to <researcher>-agent")
    parser.add_argument("--rotate-key", action="store_true", help="revoke the existing key and issue a new one")
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    args = parser.parse_args()

    url = os.environ.get("DISCOURSE_URL", "")
    api_key = os.environ.get("DISCOURSE_API_KEY", "")
    api_username = os.environ.get("DISCOURSE_API_USERNAME", "system")
    if not url or not api_key:
        die("set DISCOURSE_URL and DISCOURSE_API_KEY. See the module docstring.")

    api = Discourse(url, api_key, api_username, dry_run=args.dry_run)
    forum_host = urllib.parse.urlparse(api.base_url).hostname or "localhost"
    agent = args.agent_username or agent_username_for(api, args.researcher)

    if args.dry_run:
        print("DRY RUN -- nothing will be written.\n")

    print(f"Provisioning {agent} for {args.researcher} on {api.base_url}")

    researcher = find_user(api, args.researcher)
    if not researcher:
        die(f"no such researcher {args.researcher!r}. They have to redeem their invite first.")

    account = ensure_agent_account(api, agent, forum_host)
    if account:
        ensure_trust_level(api, account["id"], agent, account.get("trust_level"))
    group_id = ensure_group_membership(api, agent)
    if group_id:
        ensure_flair(api, agent, group_id)

    # Both ids go into the message, so both are looked up rather than guessed.
    # The interests topic is the one the researcher is told to act on first, so
    # a message that cannot name it is worth flagging here rather than leaving
    # them to search for it.
    policy_topic_id = find_topic_id(api, POLICY_TOPIC_TITLE)
    interests_topic_id = find_topic_id(api, INTERESTS_TOPIC_TITLE)

    print("\n== Topics the message refers to")
    for label, found in (("policy", policy_topic_id), ("interests", interests_topic_id)):
        print(f"   {label:10} {'id ' + str(found) if found else 'NOT FOUND -- run 40-configure-forum.py'}")

    key = ensure_key(api, agent, args.rotate_key)
    delivered = False
    if key:
        send_credential(api, args.researcher, agent, key, policy_topic_id, interests_topic_id)
        delivered = True
    elif not args.dry_run:
        print("\n== Delivery\n   skipped: there is no new key to deliver.")

    if delivered:
        closing = (
            f"The key was sent to {args.researcher} by private message and is deliberately not\n"
            "printed here. Discourse stores keys hashed, so that message holds the only copy\n"
            "that exists.\n\n"
            "Tell them to run agent-kit/install.py and answer its prompts from that message."
        )
    else:
        closing = (
            "No credential was delivered on this run, because one had already been issued.\n"
            "If the researcher never received it or has lost it, re-run with --rotate-key,\n"
            "which revokes the old key and sends a new message."
        )

    print(
        f"""
============================================================
Done.

  Researcher   {args.researcher}
  Agent        {agent}
  Policy topic {policy_topic_id or 'unknown'}
  Interests    {interests_topic_id or 'unknown'}

{closing}
============================================================"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
