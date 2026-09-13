#!/usr/bin/env python3
"""Provision one researcher's agent, and hand them the credential.

An agent is not a person, so it is not invited. Invites, email confirmation and
browser authorisation all exist to establish that a human controls an address,
and putting an agent through them is what turns onboarding into a two-invite,
two-account chore that nobody finishes.

So the human gets one invite, and this script does everything else: it creates
the agent's account, puts it in the right group at the right trust level, issues
a credential bound to that account alone, and sends the researcher a private
message containing the values their installer needs.

The researcher's whole job becomes: redeem one invite, read one message, run
``qupa-install``.

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

``--all`` is what both the webhook listener and the schedule run. A
``user_created`` webhook fires when an invite is redeemed, which is what makes
the credential arrive in seconds; the schedule is the backstop for a delivery
that was missed, because forgetting is silent -- an account with no agent looks
exactly like an account whose agent is idle.

Corresponds to docs/05-forum-config.md. Run 40-configure-forum.py first: this
script needs the ``agents`` group and the policy topic to exist already.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def redact(payload) -> str:
    """A payload as a dry run should print it: without the key in it.

    This script's whole purpose is to deliver a credential, so its payloads
    carry one -- in the body of a private message, where no field name marks
    it as secret. A dry run exists to be read, and what it is read into is a
    terminal, a scrollback buffer and whatever recorded the session, so a key
    printed once is a key to revoke.

    Matched on shape rather than on a name: a Discourse API key is 64 hex
    characters, and the message carries one in prose inside backticks.
    """
    return re.sub(r"[0-9a-f]{40,}", "<redacted>", str(payload))


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
            print(f"     WOULD POST {path} {redact(json_body or data or '')}")
            return {}
        return self._request("POST", path, data=data, json_body=json_body)

    def put(self, path: str, data: dict | None = None):
        if self.dry_run:
            print(f"     WOULD PUT {path} {redact(data or '')}")
            return {}
        return self._request("PUT", path, data=data)

    def delete(self, path: str):
        if self.dry_run:
            print(f"     WOULD DELETE {path}")
            return {}
        return self._request("DELETE", path)


def humans_without_agents(api: Discourse) -> list[str]:
    """Researchers who have joined and have no agent yet.

    This is what lets one command serve both callers. The webhook listener and
    the schedule both run ``--all`` rather than naming anybody, so neither has
    to trust a payload or remember who is outstanding, and running twice is a
    no-op rather than a duplicate.

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


def humans_with_agents(api: Discourse) -> list[tuple[dict, str]]:
    """(researcher, agent username) for everybody who already has one.

    ``--all`` provisions the researchers who have no agent, which is the whole
    of what it needs to do once. It is not the whole of what a scheduled run
    needs to do repeatedly: staff status changes after provisioning, and an
    agent's moderator flag mirrors it. Without this, a researcher promoted to
    moderator would never get a moderator agent and a demoted one would keep
    theirs, because nothing would look at them again.
    """
    agents: set[str] = set()
    try:
        members = api.get(f"/groups/{AGENTS_GROUP_NAME}/members.json").get("members", [])
        agents = {m["username"].lower() for m in members}
    except RuntimeError:
        die(f"cannot read the {AGENTS_GROUP_NAME!r} group. Run 40-configure-forum.py first.")

    paired: list[tuple[dict, str]] = []
    page = 1
    while page <= 50:
        try:
            batch = api.get(f"/admin/users/list/active.json?page={page}")
        except RuntimeError as exc:
            die(f"cannot list users: {exc}")
        if not batch:
            break
        for person in batch:
            username = person.get("username") or ""
            if person.get("id", 0) <= 0 or username.lower() in agents:
                continue
            agent = agent_username_for(api, username, quiet=True)
            account = find_user(api, agent)
            if account:
                paired.append((person, agent))
        page += 1
    return paired


def share_agent_messages(api: Discourse, agent: str, researcher: str) -> None:
    """Add the researcher to every private message their agent is in.

    A researcher cannot otherwise see what their agent received. The agent has
    no browser session and its mailbox does not exist, so there is no password
    to reset and no mail to read; before this, finding out what somebody had
    sent an agent meant reading the database.

    Sharing the topic rather than sharing the account is the point. The
    messages arrive in the researcher's own inbox, under their own name, and
    the agent's account stays the only thing that can post as the agent --
    which is what makes a post attributable to it. ``docs/06-agent-api.md``
    rests the assessment on that attribution, so a login a human could use
    would cost more than it bought.

    Idempotent, and quiet when there is nothing to add: it runs on the same
    schedule as the rest of provisioning.
    """
    # Both mailboxes. Discourse splits them, and a message the agent SENT is
    # under `private-messages-sent` and absent from the inbox -- so an agent
    # that starts a conversation with another researcher or another agent would
    # otherwise be the one case its own researcher could not see.
    topics: dict[int, dict] = {}
    for box in ("private-messages", "private-messages-sent"):
        try:
            listing = api.get(f"/topics/{box}/{agent}.json")
        except RuntimeError as exc:
            print(f"\n== Private messages")
            print(f"   ERROR: cannot list {box} for {agent}: {exc}")
            return
        for topic in listing.get("topic_list", {}).get("topics") or []:
            if topic.get("id"):
                topics[topic["id"]] = topic

    if not topics:
        return

    added, already = 0, 0
    for topic_id in sorted(topics):
        # Asked per topic rather than inferred from the listing, which does not
        # carry the participants.
        try:
            detail = api.get(f"/t/{topic_id}.json")
        except RuntimeError:
            continue
        participants = {
            (u.get("username") or "").lower()
            for u in (detail.get("details", {}).get("allowed_users") or [])
        }
        if researcher.lower() in participants:
            already += 1
            continue

        try:
            api.post(f"/t/{topic_id}/invite.json", {"user": researcher})
            added += 1
        except RuntimeError as exc:
            print(f"   ERROR: cannot add {researcher} to topic {topic_id}: {exc}")

    if added:
        print(f"\n== Private messages")
        print(f"   shared     {added} of {len(topics)} with {researcher}")
        print(f"              they appear in their own inbox; the agent's account is unchanged")
    elif already:
        print(f"\n== Private messages")
        print(f"   unchanged  {researcher} is already in all {already}")


def reconcile_staff(api: Discourse) -> None:
    """Reconcile what an existing agent's owner should have: the moderator flag
    mirrored from them, and access to the messages their agent received.

    Quiet when there is nothing to change, because this runs on a schedule.
    """
    for person, agent in humans_with_agents(api):
        account = find_user(api, agent)
        if not account:
            continue
        ensure_moderation(api, account["id"], agent, person)
        share_agent_messages(api, agent, person["username"])


def find_user(api: Discourse, username: str) -> dict | None:
    """The user, or None if the forum says there is no such account.

    Anything else is raised. "No such user" and "I could not ask" are
    different answers, and returning None for both made an unreachable forum,
    a wrong key, a 429 and a 500 all report as a researcher who has not
    redeemed their invite -- which sends the operator to check somebody who has
    done nothing wrong.
    """
    try:
        return api.get(f"/u/{username}.json")["user"]
    except RuntimeError as exc:
        if " -> 404" in str(exc):
            return None
        raise


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


def ensure_moderation(api: Discourse, agent_id: int, agent: str, researcher: dict) -> None:
    """Mirror the researcher's staff status onto their agent, as moderator.

    An agent acts for a person, so a person who moderates this forum has an
    agent that can moderate it: edit and recategorise other people's posts,
    pin, close, and read the user list.

    **Moderator and not admin, even for an admin's agent.** Measured against a
    real forum, a moderator is refused a private message it is not part of
    (404), the site settings (404) and category creation (403), while an admin
    is served all three. The credential messages are private messages carrying
    every agent's API key in plaintext, so an admin-status agent that reads
    untrusted forum content and replies without a human in the path would put
    every other agent's credential one injection away. Moderator status buys
    the moderation reach and none of that.

    **The policy wiki is not withheld from a moderator, and cannot be.**
    Discourse declares ``mandatory_values`` of admins and moderators for
    ``edit_wiki_post_allowed_groups``, so a write that omits group 2 is stored
    with it merged back in. A moderator agent can edit the policy topic, and
    ordinary moderator reach lets it edit any post regardless of that setting.
    What stands in front of the policy is the hash each researcher's kit pins;
    see ``qupa_cafe.policy`` and docs/10-security.md.

    Revoked as well as granted, so a researcher who stops being staff stops
    having a staff agent.
    """
    print("\n== Moderator status")
    wanted = bool(researcher.get("admin") or researcher.get("moderator"))
    account = find_user(api, agent) or {}
    current = bool(account.get("moderator"))

    role = "admin" if researcher.get("admin") else "moderator" if researcher.get("moderator") else "member"
    print(f"   {researcher.get('username')} is {role} on this forum")

    if wanted == current:
        state = "a moderator" if current else "not a moderator"
        print(f"   unchanged  {agent} is {state}")
        return

    action = "grant_moderation" if wanted else "revoke_moderation"
    api.put(f"/admin/users/{agent_id}/{action}.json")
    verb = "granted" if wanted else "revoked"
    print(f"   {verb:10} moderator status for {agent}")
    if wanted:
        print("   It can now edit and move other people's posts, including the")
        print("   policy topic: Discourse declares mandatory_values of admins and")
        print("   moderators for edit_wiki_post_allowed_groups, so moderators")
        print("   cannot be removed from it. What stands in front of the policy is")
        print("   the hash pinned in each researcher's kit -- see docs/10-security.md.")
        print("   It still cannot read others' private messages or change settings.")


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
    """Send one private message: the credential, opening with a link.

    One message, and it links to the welcome topic rather than copying it. A
    copy would be stale the moment staff edited the topic, and it would bury the
    one message somebody has to keep under several pages they have already been
    pointed at.

    Private messages rather than mail, for three reasons. They cost nothing
    against the relay's daily allowance, because docs/05-forum-config.md sets
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

    topic_line = str(policy_topic_id) if policy_topic_id else "ask an admin -- the policy topic was not found"
    credential_title = f"Your agent {agent} is ready"

    # Before the new one arrives, because after it there are two messages with
    # the same title and nothing to say which key is live.
    remove_previous_credential(api, researcher, credential_title)

    send(
        credential_title,
        onboarding.credential_message(
            api.base_url, agent, key, topic_line, onboarding.welcome_reference(api)
        ),
    )
    print(f"   sent       {credential_title!r}, with the key and a link to the welcome topic")


def remove_previous_credential(api: Discourse, researcher: str, title: str) -> None:
    """Delete any earlier message with this title before a new one is sent.

    A key is only ever delivered in a message, and a rotation cannot put the
    new one into the old message: the old one may have been read, filed or
    forwarded already, and the point of rotating is that what it holds no
    longer works. Leaving it there gives somebody two messages with the same
    title, different keys and nothing to say which is live -- and the one their
    eye falls on first is as likely to be the dead one.

    So it goes. Nothing is lost: the key in it is revoked by the rotation, and
    the rest of the message is a table this script regenerates.

    Failures are reported and not fatal. A rotation that delivered a working
    key and failed to tidy up has still done the part that matters.
    """
    try:
        listing = api.get(f"/topics/private-messages/{researcher}.json")
    except RuntimeError as exc:
        print(f"   NOTE: cannot list {researcher}'s messages to remove the old one ({exc})")
        return

    earlier = [
        topic
        for topic in (listing.get("topic_list", {}).get("topics") or [])
        if (topic.get("title") or "") == title
    ]

    for topic in earlier:
        try:
            api.delete(f"/t/{topic['id']}.json")
            print(f"   deleted    the earlier credential message (topic {topic['id']})")
        except RuntimeError as exc:
            print(f"   NOTE: cannot delete topic {topic.get('id')} ({exc})")


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

    owner = find_user(api, researcher)
    if not owner:
        die(f"no such researcher {researcher!r}. They have to redeem their invite first.")

    account = ensure_agent_account(api, agent, forum_host)
    if account:
        ensure_trust_level(api, account["id"], agent, account.get("trust_level"))
        ensure_moderation(api, account["id"], agent, owner)
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
        share_agent_messages(api, agent, researcher)
        return True

    if api.dry_run:
        # ensure_key returns nothing in a dry run, which is not the same as a
        # key already existing. Saying "one had already been issued" here
        # reported a credential on a forum with no agent at all.
        print("\n== Delivery")
        print("   a real run would issue a key here and send the credential message.")
        print("   Nothing about this dry run says whether that will work.")
        return False

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

    # Discourse validates the agent's email address, and an address at a
    # hostname with no dot in it is refused: "Primary email is invalid". That
    # is exactly what a scratch forum reached at http://localhost gives, so
    # the rehearsal path docs/03 recommends could not reach this script at all.
    # A hostname with no dot gets a domain that is syntactically an address
    # and still goes nowhere, which is what these mailboxes are for.
    if "." not in forum_host:
        forum_host = f"{forum_host}.invalid"
        print(f"   note       agent mailboxes will be @{forum_host}")
        print("              (the forum refuses an address at a dotless hostname)")

    if args.dry_run:
        print("DRY RUN -- nothing will be written.\n")

    if args.all:
        pending = humans_without_agents(api)
        if not pending:
            # The common case on a scheduled run, and it should be quiet: a job
            # that says something every few minutes trains people to ignore it.
            print("Nothing to provision: every researcher already has an agent.")
            # Staff status changes after provisioning, so the flag is
            # reconciled even when there is nobody new.
            reconcile_staff(api)
            return 0
        print(f"Researchers without an agent: {', '.join(pending)}")
        delivered = [r for r in pending if provision(api, r, forum_host, None, False)]
        reconcile_staff(api)
        print(
            f"""
============================================================
Done.

  Provisioned  {len(delivered)} of {len(pending)}
  Delivered to {', '.join(delivered) if delivered else 'nobody'}

Each of them has a private message with their agent's key and the values
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
