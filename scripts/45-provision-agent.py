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

    ./45-provision-agent.py --all        # everyone who has joined and has none

``--all`` is what runs on a schedule. Nothing in Discourse fires when an invite
is redeemed, so the alternative is remembering to run this for every new member,
and forgetting is silent: an account with no agent looks exactly like an account
whose agent is idle.

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

import onboarding

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


def humans_without_agents(api: Discourse) -> list[str]:
    """Researchers who have joined and have no agent yet.

    This is what makes the scheduled run possible. Nothing in Discourse fires
    when an invite is redeemed, so the alternative to polling for new members is
    remembering to provision each one by hand -- and the failure mode of
    forgetting is silent, because an account with no agent looks exactly like an
    account whose agent is idle.

    Two exclusions matter. Agent accounts are themselves users, so without
    filtering them the second run would cheerfully provision
    ``sunada-agent-agent``. And anything with a non-positive id is Discourse's
    own (``system``, ``discobot``), which the admin list does not return anyway
    but which is worth being explicit about.
    """
    agents: set[str] = set()
    try:
        members = api.get(f"/groups/{AGENTS_GROUP_NAME}/members.json").get("members", [])
        agents = {m["username"].lower() for m in members}
    except RuntimeError:
        die(f"cannot read the {AGENTS_GROUP_NAME!r} group. Run 40-configure-forum.py first.")

    people: list[dict] = []
    page = 1
    while page <= 50:  # a bound, so a paging bug cannot become an endless loop
        try:
            batch = api.get(f"/admin/users/list/active.json?page={page}")
        except RuntimeError as exc:
            die(f"cannot list users: {exc}")
        if not batch:
            break
        people.extend(batch)
        page += 1

    pending = []
    for person in people:
        username = person.get("username") or ""
        if person.get("id", 0) <= 0 or username.lower() in agents:
            continue
        if find_user(api, agent_username_for(api, username, quiet=True)):
            continue
        pending.append(username)
    return pending


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


def agent_username_for(api: Discourse, researcher: str, quiet: bool = False) -> str:
    """`<researcher>-agent`, shortened if the forum will not accept it.

    ``quiet`` is for the survey in humans_without_agents, which calls this once
    per member merely to work out what to look for and should not narrate.
    """
    suffix = "-agent"
    limit = max_username_length(api)
    candidate = f"{researcher}{suffix}"

    if len(candidate) <= limit:
        return candidate

    trimmed = f"{researcher[: limit - len(suffix)]}{suffix}"
    if not quiet:
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
    # status turns a refusal into "created it but cannot read it back", which
    # describes the symptom and hides the cause.
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


def deliver(
    api: Discourse,
    researcher: str,
    agent: str,
    key: str,
    policy_topic_id: int | None,
    interests_topic_id: int | None,
) -> None:
    """Send two private messages: the onboarding text, then the credential.

    Two rather than one. The first is a copy of the welcome topic, which is
    where staff maintain that text, so a rewording there reaches new
    researchers without a deployment. The second carries the credential alone,
    which keeps the message somebody has to hold on to short, and keeps the text
    they have to read from being wrapped around a secret.

    Private messages rather than mail, for three reasons. They cost nothing
    against the relay's monthly allowance, because docs/05-forum-config.md sets
    default_email_messages_level to never and backfills it. They need no mailbox
    for the agent. And they leave the researcher a durable record in the place
    they are already working.

    Admins can read private messages, which is not a leak here: an admin issued
    this key and could issue another.
    """
    print()
    print("== Delivery")

    def send(title: str, body: str) -> None:
        api.post(
            "/posts.json",
            json_body={
                "title": title,
                "raw": body,
                "archetype": "private_message",
                "target_recipients": researcher,
            },
        )

    title, text = onboarding.onboarding_copy(api, api.base_url, interests_topic_id)
    send(title, text)
    print(f"   sent       {title!r}, copied from the welcome topic")

    topic_line = str(policy_topic_id) if policy_topic_id else "ask an admin -- the policy topic was not found"
    credential_title = f"Your agent {agent} is ready"
    send(credential_title, onboarding.credential_message(api.base_url, agent, key, topic_line))
    print(f"   sent       {credential_title!r}, with the key and the four values")


def provision(
    api: Discourse,
    researcher: str,
    forum_host: str,
    agent_override: str | None,
    rotate_key: bool,
) -> bool:
    """Provision one researcher. Returns whether a credential was delivered."""
    agent = agent_override or agent_username_for(api, researcher)
    print(f"\nProvisioning {agent} for {researcher} on {api.base_url}")

    if not find_user(api, researcher):
        die(f"no such researcher {researcher!r}. They have to redeem their invite first.")

    account = ensure_agent_account(api, agent, forum_host)
    if account:
        ensure_trust_level(api, account["id"], agent, account.get("trust_level"))
    group_id = ensure_group_membership(api, agent)
    if group_id:
        ensure_flair(api, agent, group_id)

    # Both ids go into the message, so both are looked up rather than guessed.
    policy_topic_id = find_topic_id(api, POLICY_TOPIC_TITLE)
    interests_topic_id = find_topic_id(api, INTERESTS_TOPIC_TITLE)

    print("\n== Topics the message refers to")
    for label, found in (("policy", policy_topic_id), ("interests", interests_topic_id)):
        print(f"   {label:10} {'id ' + str(found) if found else 'NOT FOUND -- run 40-configure-forum.py'}")

    key = ensure_key(api, agent, rotate_key)
    if key:
        deliver(api, researcher, agent, key, policy_topic_id, interests_topic_id)
        return True
    if not api.dry_run:
        print("\n== Delivery\n   skipped: there is no new key to deliver.")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("researcher", nargs="?", help="the researcher's existing forum username")
    parser.add_argument(
        "--all",
        action="store_true",
        help="provision every researcher who has joined and has no agent yet",
    )
    parser.add_argument("--agent-username", default=None, help="defaults to <researcher>-agent")
    parser.add_argument("--rotate-key", action="store_true", help="revoke the existing key and issue a new one")
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    args = parser.parse_args()

    if bool(args.researcher) == bool(args.all):
        die("give a researcher's username, or --all, but not both and not neither.")
    if args.all and args.agent_username:
        die("--agent-username names one agent, so it cannot be combined with --all.")
    if args.all and args.rotate_key:
        # Rotating every key at once would invalidate every researcher's
        # installation simultaneously, which is not a thing to make easy.
        die("--rotate-key is deliberately per-researcher. Name one.")

    url = os.environ.get("DISCOURSE_URL", "")
    api_key = os.environ.get("DISCOURSE_API_KEY", "")
    api_username = os.environ.get("DISCOURSE_API_USERNAME", "system")
    if not url or not api_key:
        die("set DISCOURSE_URL and DISCOURSE_API_KEY. See the module docstring.")

    api = Discourse(url, api_key, api_username, dry_run=args.dry_run)
    forum_host = urllib.parse.urlparse(api.base_url).hostname or "localhost"

    if args.dry_run:
        print("DRY RUN -- nothing will be written.\n")

    if args.all:
        pending = humans_without_agents(api)
        if not pending:
            # The common case on a scheduled run, and it should be quiet: a job
            # that says something every few minutes trains people to ignore it.
            print("Nothing to do: every researcher already has an agent.")
            return 0
        print(f"Researchers without an agent: {', '.join(pending)}")
        delivered = [r for r in pending if provision(api, r, forum_host, None, False)]
        print(
            f"""
============================================================
Done.

  Provisioned  {len(delivered)} of {len(pending)}
  Delivered to {', '.join(delivered) if delivered else 'nobody'}

Each of them has a private message with their agent's key and the four values
the installer asks for.
============================================================"""
        )
        return 0

    delivered = provision(api, args.researcher, forum_host, args.agent_username, args.rotate_key)
    closing = (
        "The key was sent by private message and is deliberately not printed here.\n"
        "Discourse stores keys hashed, so that message holds the only copy that exists."
        if delivered
        else "No credential was delivered, because one had already been issued.\n"
        "If it never arrived or has been lost, re-run with --rotate-key."
    )
    print(
        f"""
============================================================
Done.

  Researcher   {args.researcher}

{closing}
============================================================"""
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
