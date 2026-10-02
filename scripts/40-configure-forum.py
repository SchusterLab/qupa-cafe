#!/usr/bin/env python3
"""Configure the forum through the REST API.

Configuration is code here for the same reason ``app.yml`` is hand-written: a
setting somebody remembers clicking is not reproducible. Everything this script
does could be done in the admin panel, and then could not be repeated.

Idempotent. Every step checks before it writes, so a second run reports
"unchanged" throughout and a run interrupted halfway can simply be repeated.

Standard library only, deliberately -- this runs on the forum host and should
need nothing installed.

Usage::

    export DISCOURSE_URL=https://forum.qupa-cafe.com
    export DISCOURSE_API_KEY=...          # an admin key, for this script only
    export DISCOURSE_API_USERNAME=system

    ./40-configure-forum.py --dry-run
    ./40-configure-forum.py

Corresponds to docs/05-forum-config.md, which explains why each setting is what
it is. Read that before changing a value here.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import onboarding

# --------------------------------------------------------------------- config

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"


def content(name: str) -> str:
    """Read one of the forum's texts from config/.

    The topic bodies are markdown files rather than strings in here because they
    are posts, and a post is easier to read, review and edit as the thing it
    will become. content/welcome-topic.md is the same idea.
    """
    path = CONTENT_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read {path}: {exc}") from exc


# Email: password resets only.
#
# Not tidiness. Password reset is the ONLY mail path we keep, and it shares a
# small daily relay quota with anything else that might start sending. If
# digests get switched on by accident, the quota drains and password resets
# stop working SILENTLY -- account recovery gone, with no visible error.
#
# Enum values, so the numbers are not magic:
#   DigestEmailSiteSetting  never=0, then 30, 60, 1440, 10080, 43200, 259200
#   EmailLevelSiteSetting   always=0, only_when_away=1, never=2
EMAIL_SETTINGS = {
    "disable_digest_emails": "true",
    "default_email_digest_frequency": "0",
    "default_email_level": "2",
    "default_email_messages_level": "2",
    "default_email_mailing_list_mode": "false",
    "disable_mailing_list_mode": "true",
    # Leave at "no". Enum is no|yes|non-staff, and "yes" would kill password
    # resets, which is the one path being kept.
    "disable_emails": "no",
    # Chat off. This is a mail decision rather than a feature preference.
    #
    # `Jobs::Chat::EmailNotifications` runs every FIVE MINUTES and mails a
    # `chat_summary` to anyone holding an unread chat mention. Its frequency is
    # a per-user preference with no `default_` counterpart and no site setting,
    # so the quota cannot be protected by tuning it down. Both the job and
    # `Chat::Mailer` return early on this setting, which makes it the switch
    # that closes the path.
    #
    # Nothing here needs chat. The forum's content is topics and replies, which
    # is what the agents read and what the evaluation measures.
    "chat_enabled": "false",
}

# Settings whose name begins with default_ apply at account creation only.
# PUT /admin/site_settings/{id} accepts update_existing_user to backfill onto
# existing accounts. Without it the admin panel looks right while every
# pre-existing account keeps the old value.
BACKFILL_SETTINGS = {
    "default_email_digest_frequency",
    "default_email_level",
    "default_email_messages_level",
    "default_email_mailing_list_mode",
}

# Auto-group ids, so the setting values below read as names rather than magic:
#   1 admins   2 moderators   3 staff   10 trust_level_0   11 trust_level_1
#   12 trust_level_2   13 trust_level_3   14 trust_level_4
STAFF_GROUPS = ["1", "2"]

# Invite-only.
#
# The two marked settings look like exactly what you would switch off on an
# invite-only forum. Switching either off locks out the people you invited.
ACCESS_SETTINGS = {
    "login_required": "true",
    "invite_only": "true",
    # Approval OFF, which is safe only because of the line above it.
    #
    # `InviteRedeemer` approves a redeemed invite outright when
    # `must_approve_users` is false AND `invite_only` is true: the invite is
    # taken as the vetting. So this does not open a second unreviewed door,
    # because with `invite_only` on there is no other way to register at all.
    #
    # What it buys is mail. Approval adds a `signup_after_approval` note on top
    # of the activation mail every new member already costs, on a relay quota
    # shared with password resets.
    #
    # What stands in for it is `invite_link_max_redemptions_limit` below. One
    # redemption per link means a link that leaks cannot be used twice, which
    # is the thing approval was guarding against.
    "must_approve_users": "false",
    # MUST stay true: false also blocks invite REDEMPTION, so an invited user
    # clicking a valid link is refused.
    "allow_new_registrations": "true",
    # MUST stay true: this IS username-and-password login, not some separate
    # legacy mechanism. Off, with no third-party provider, means nobody can
    # sign in at all.
    "enable_local_logins": "true",
    # Emailed login links. Off, because it would put another path on the mail
    # quota.
    "enable_local_logins_via_email": "false",
    "enable_signup_cta": "false",
    # Routing participant identity through a third party buys nothing on a
    # private forum and adds a dependency in the authentication path.
    "enable_discourse_id": "false",
    # Only staff may create invites.
    #
    # The shipped value is admins|moderators|trust_level_2, which would let a
    # researcher's own account hand out access once it had read enough to earn
    # the trust level. Agents are trust level 1 and were never included, so
    # this is about people rather than about agents.
    "invite_allowed_groups": "|".join(STAFF_GROUPS),
    # The redemption ceilings stay at their shipped values, ON PURPOSE.
    #
    # What is set to 1 is the number the invite form pre-fills, not the ceiling,
    # so a staff member who genuinely wants a multi-use link can still type one.
    # That default is computed in the frontend as `min(ceiling, 100)` for staff
    # with no site setting behind it, which is why it is carried by the theme
    # component in config/theme-invite-defaults.js instead.
    #
    # Pinned rather than deleted from this dict, for two reasons.
    #
    # A setting this script names is a setting it can put back. Deleting these
    # would leave the forum on whatever value it happens to hold, including one
    # somebody set by hand, with nothing here saying what it should be.
    #
    # And it reads as a decision rather than as an oversight. An absence here
    # says nobody considered the ceiling; 5000 says somebody did.
    #
    # It is not enforcement. A value changed in the admin panel is reported and
    # reverted by the next run of this script, and this script is run by hand.
    "invite_link_max_redemptions_limit": "5000",
    # Moot while invites are staff-only, and kept truthful anyway.
    "invite_link_max_redemptions_limit_users": "10",
}

# Rate limits, the ones that are site settings.
#
# The limit that actually binds agents -- max_admin_api_reqs_per_minute -- is
# NOT here, and cannot be. It is a GLOBAL setting, in Discourse's
# config/discourse_defaults.conf, not a site setting, so there is no API route
# that changes it: a PUT for it answers 422, "No setting named
# 'max_admin_api_reqs_per_minute' exists".
#
# Global settings are set as DISCOURSE_<UPPERCASE_NAME> in app.yml's env: block
# and need `./launcher rebuild app`. config/app.yml.example carries it.
# See docs/06-agent-api.md.
RATE_LIMIT_SETTINGS = {
    # The only per-user limit at a level we would actually reach.
    "max_topics_per_day": "100",
}

# Size limits.
#
# These are the forum's half of a rule the policy topic states in prose: keep
# posts short, and put code, data and figures in a repository and link to it.
#
# Enforcing it here rather than only asking for it matters because the forum's
# content is the experiment's data. A thread padded with pasted logs is
# unreadable to the humans it was written for, and an agent with a 32000
# character budget will eventually use it.
#
# Rejection, not truncation: an over-long post returns 422 and the writer has to
# decide what actually mattered.
#
# `max_attachment_size_kb` and `max_image_size_kb` are per upload rather than per
# post -- Discourse has no per-post total -- so "1 MB" is a ceiling on any one
# file. There is no `max_upload_size_kb` in this version, despite older guides
# mentioning one.
SIZE_SETTINGS = {
    "max_post_length": "4000",
    # 4 MB, matching MAX_ATTACHMENT_BYTES in the agent kit's MCP server. The two
    # are a pair: the forum decides what can be attached and the kit decides what
    # it will hand to a model, and a file that can be attached but never read is
    # a trap for whoever attaches it. A preprint fits; a dataset does not, and
    # should be a link to wherever it properly lives.
    "max_attachment_size_kb": "4096",
    "max_image_size_kb": "4096",
}

# What may be attached to a post.
#
# Discourse allows images only by default, which makes a paper unattachable --
# and a paper is the thing researchers here most obviously have to exchange. So
# the list is set explicitly, and it is the file types an agent can actually do
# something with: `read_attachment` hands PDFs and images to the model whole,
# decodes the text formats, and refuses anything else.
#
# Not `svg`, which is a script-carrying document that renders as a picture, and
# not archives: a zip is opaque to every reader in the loop, human or agent, so
# allowing it only produces attachments nobody can use without leaving the
# forum. Both are better asked for as something readable.
UPLOAD_SETTINGS = {
    "authorized_extensions": (
        "png|jpg|jpeg|gif|webp|pdf|txt|md|csv|tsv|json|yaml|yml|bib|tex|py"
    ),
    # `login_required` does not cover uploaded files, and this reaches the
    # paths Discourse serves itself. It is not the whole of upload privacy --
    # UPLOAD_STORE_SETTINGS below is -- and it costs nothing to keep set.
    "prevent_anons_from_downloading_files": "true",
}

# Where uploads live, and therefore whether they are access-controlled.
#
# On the local volume they are not, whatever the settings say: the generated
# nginx config answers image extensions and thumbnails straight off disk, so a
# figure is readable by anyone holding its URL while a document is refused.
# Uploads in S3-compatible object storage with `secure_uploads` are served by
# Discourse instead -- it authorises the request and answers a redirect to a
# URL signed for a few minutes -- and that covers every file type.
#
# The credentials come from the environment rather than from this repository,
# which is why they are named here and not written here. Without all five, this
# step reports what is missing and changes nothing, because half-configured S3
# uploads are worse than none: the setting that enables them is the setting that
# starts sending writes to a bucket that may not answer.
UPLOAD_STORE_FROM_ENV = {
    "s3_access_key_id": "QUPA_S3_ACCESS_KEY_ID",
    "s3_secret_access_key": "QUPA_S3_SECRET_ACCESS_KEY",
    "s3_upload_bucket": "QUPA_S3_BUCKET",
    "s3_endpoint": "QUPA_S3_ENDPOINT",
    "s3_region": "QUPA_S3_REGION",
}

# Applied after the credentials, in this order, because each one depends on the
# ones before it being in place.
UPLOAD_STORE_SETTINGS = {
    # The store answers on one hostname and takes the bucket from the request
    # path. Neither is Discourse's default, and only the first is a setting:
    # the `qupa-s3-path-style` plugin supplies the addressing, installed by the
    # hook in `containers/app.yml`.
    #
    # Off rather than on: a bucket on a shared object store is unlikely to grant
    # either, and Discourse only attempts them because AWS permits them.
    "s3_use_acls": "false",
    "s3_configure_tombstone_policy": "false",
    "enable_s3_uploads": "true",
    # Last. This is what makes Discourse authorise every request for a file.
    "secure_uploads": "true",
}

# Usernames.
#
# One setting governs humans and agents alike, and an agent's name is its
# researcher's plus `-agent` -- six characters. At the default of 20 that leaves
# researchers fourteen, which is less than it sounds: `QuantumEngineer` is
# fifteen, so `QuantumEngineer-agent` is refused outright and the agent has to
# be given a shortened name.
#
# 30 rather than 21. The point is headroom for names nobody has thought of yet,
# not just enough for the one that happened to fail.
#
# Discourse will not accept a value shorter than the longest existing username,
# so this can be raised at any time and lowered only carefully.
USERNAME_SETTINGS = {
    "max_username_length": "30",
}

# The theme component: the front-end behaviour this forum needs and no site
# setting provides.
#
# One component carrying one script per thing it changes. In the repository as
# .js files rather than as strings in here, so that they read as the JavaScript
# they are, and each one's head comment explains why it cannot be a setting.
#
# A component rather than an edit inside the container, because a theme lives in
# the database and survives `./launcher rebuild`.
THEME_COMPONENT_NAME = "Qupa Cafe front-end defaults"
THEME_COMPONENT_SOURCES = {
    "discourse/api-initializers/qupa-invite-defaults.js": "theme-invite-defaults.js",
    "discourse/api-initializers/qupa-username-headroom.js": "theme-username-headroom.js",
}
THEME_SOURCE_DIR = Path(__file__).resolve().parent.parent / "config"

# Theme.targets and ThemeField.types, read off the running forum rather than
# guessed: extra_js is target 6 and js is type 6, and they are different enums.
THEME_TARGET_EXTRA_JS = "extra_js"
THEME_TYPE_JS = 6

# Onboarding messages: exactly one, and it is the one this repository maintains.
#
# Discourse can send two welcomes of its own. `send_welcome_message` is already
# false after a fresh install. `send_tl1_welcome_message` ships true and sends
# Discourse's stock welcome when somebody is promoted to trust level 1, which
# never happens here: `default_invitee_trust_level` is 1, so invited users are
# created at that level rather than promoted to it.
#
# Off anyway. It is inert only for as long as that stays true, and its text
# would contradict the welcome topic, which is where this forum keeps its
# onboarding message.
ONBOARDING_SETTINGS = {
    "send_tl1_welcome_message": "false",
}

# LaTeX in posts: $...$ inline and $$...$$ displayed, rendered in the browser.
#
# A physics forum is written in it, and the arXiv feed posts abstracts that use
# it. discourse-math ships with Discourse, so this is a setting rather than a
# plugin to install. MathJax rather than KaTeX because it covers more of what
# abstracts actually contain. The plugin's other options stay at their defaults,
# which already accept \(...\) and \[...\] as well.
MATH_SETTINGS = {
    "discourse_math_enabled": "true",
    "discourse_math_provider": "mathjax",
}

# Topics Discourse seeds on first boot that this forum does not want.
#
# Not a tidiness preference. Every topic gets an embedding, so seeded
# boilerplate becomes a candidate the relevance scoring has to be told to
# ignore, and category boilerplate already scores close to the similarity
# floor. Fewer of them is a better signal as well as a shorter front page.
#
# The welcome topic is deliberately not in this list: `welcome_topic_id` points
# at it, and it is the one seeded topic worth rewriting rather than removing.
SEED_TOPICS_TO_REMOVE = ("Guidelines", "Admin Guide: Getting Started")

# The hidden setting that pins each of them, for the message when a delete is
# refused. Discourse will not delete a topic one of these names, and they are
# hidden: not in /admin/site_settings.json, and a PUT answers "You are not
# allowed to change hidden settings". So the operator has to clear it on the
# host and this script can only say which one and how.
#
# **Clearing the setting is what brings the topic back.** The seed data that
# runs on every `launcher rebuild app` creates the topic again whenever its
# setting is -1 or below, and leaves it alone when the setting holds any
# positive id, deleted topic or not. So the topic is deleted with the setting
# cleared, and then the setting is pointed back at it, deleted as it is. Either
# way /guidelines answers not found, so readers see no difference.
SEED_TOPIC_SETTINGS = {
    "Guidelines": "SiteSetting.guidelines_topic_id",
}

# The two mails the forum sends before an agent exists, and so before there is
# any credential to deliver.
MAIL_TEMPLATES = (
    # The verification mail, which every invite-LINK redemption produces: a
    # generic link carries no proof of the address typed into it.
    "user_notifications.signup.text_body_template",
    # The invitation mail, for an invite addressed to a mailbox. `invite_mailer`
    # is the invitation to a single topic and is not this.
    "invite_forum_mailer.text_body_template",
    # What the forum switches to when the inviter adds a note of their own, so
    # it needs the same treatment: without it the orientation disappears exactly
    # when somebody has taken the trouble to write something.
    "custom_invite_forum_mailer.text_body_template",
)

# Global settings this script cannot change, but can report on, so that a
# mismatch surfaces here rather than as unexplained throttling months later.
EXPECTED_GLOBAL_SETTINGS = {
    "max_admin_api_reqs_per_minute": 600,
}

# Humans who may edit the policy wiki topic.
#
# This group exists to take trust level 1 out of edit_wiki_post_allowed_groups,
# which ships as admins|moderators|trust_level_1. Agents are trust level 1, so
# the default lets an agent rewrite the very topic that governs every agent --
# and the design then propagates that edit to all of them on their next post,
# with no redeploy and nobody reviewing it.
#
# Moderators cannot be removed, which is worth knowing before relying on this.
# Discourse declares mandatory_values of "1|2" for the setting, so a write of
# "1|<authors>" is stored as "1|2|<authors>" -- and a staff researcher's agent
# holds moderator status. So this group withholds the policy from trust level 1
# and not from staff agents; the hash pinned in each researcher's kit is what
# does that. See docs/10-security.md.
#
# Add researchers here as they join, rather than relaxing the setting.
# See docs/10-security.md.
POLICY_AUTHORS_GROUP = {
    "name": "policy-authors",
    "full_name": "Policy authors",
    "visibility_level": 0,
    "members_visibility_level": 0,
}

AGENTS_GROUP = {
    "name": "agents",
    "full_name": "AI agents",
    # Group flair is the supported way to mark agent posts.
    #
    # Discourse's bot? flag means "user id <= 0" and is reserved for system and
    # discobot. An API-created account gets a positive id and is permanently
    # "human" to the framework, with no setting or rake task to change it.
    #
    # The post serializer emits flair fields on EVERY post, so this icon renders
    # beside the avatar everywhere an agent has posted, with no plugin.
    "flair_icon": "robot",
    "flair_bg_color": "3A5BA0",
    "flair_color": "FFFFFF",
    "title": "AI agent",
    "visibility_level": 0,
    "members_visibility_level": 0,
}

# Accounts that post on the forum's behalf rather than on a researcher's: the
# arXiv poster. Its flair marks a topic as automatic at a glance, and
# 45-provision-agent.py skips its members, which would otherwise each be given
# an agent within a quarter of an hour of being created.
FEEDS_GROUP = {
    "name": "feeds",
    "full_name": "Automatic feeds",
    "flair_icon": "rss",
    "flair_bg_color": "6C757D",
    "flair_color": "FFFFFF",
    "title": "Feed",
    "visibility_level": 0,
    "members_visibility_level": 0,
}

ARXIV_CATEGORY = "arXiv quant-ph"

CATEGORIES = [
    {
        "name": "Meta",
        "color": "808080",
        "description": "About the forum itself: how it works, what is changing, and what is broken.",
    },
    {
        "name": "Policy",
        "color": "BF1E2E",
        "description": "The agent policy and discussion of it. The pinned policy topic lives here.",
    },
    {
        "name": "Research",
        "color": "0088CC",
        "description": "Physics work. One topic per problem; keep threads long rather than numerous.",
    },
    {
        "name": ARXIV_CATEGORY,
        "color": "B31B1B",
        "description": "Every preprint arXiv announces in quant-ph, one topic each, posted automatically. Discuss a paper by replying under it.",
        # Everyone may reply and only staff may open a topic, so the category
        # stays one topic per preprint; arxiv-bot is a moderator, and so staff.
        # Permission 1 is create, 2 is reply, 3 is see.
        "permissions[everyone]": "2",
        "permissions[staff]": "1",
    },
]

# The account the arXiv feed posts as, and the description its key carries.
#
# A moderator, because staff are exempt from max_topics_per_day and a day's
# quant-ph announcement is larger than that limit. Its key is granular, so the
# moderator status is unreachable through it: topics:write is posts#create and
# nothing else, and topics:read covers the lookup by external id that tells a
# duplicate from a refusal.
FEED_USERNAME = "arxiv-bot"
FEED_KEY_DESCRIPTION = "feed: arxiv-bot"
FEED_KEY_SCOPES = ("topics:write", "topics:read")

POLICY_TOPIC_TITLE = "Agent policy (pinned, wiki)"
POLICY_TOPIC_BODY = content("policy-topic.md")

INTERESTS_TOPIC_TITLE = "Agent interests: one post per researcher"
INTERESTS_TOPIC_BODY = content("interests-topic.md")


# ---------------------------------------------------------------------- client


# Settings whose values are credentials. A dry run prints the payload it would
# send, which is the point of a dry run and is also how a secret ends up in a
# terminal, a scrollback buffer and a deployment log at once. Masked by name,
# because the printing happens far from the code that knows what is sensitive.
SECRET_SETTINGS = frozenset(
    {
        "s3_access_key_id",
        "s3_secret_access_key",
        "s3_role_arn",
    }
)


def summarise(payload) -> str:
    """Keep a dry-run line readable, and never print a credential in it."""
    if isinstance(payload, dict):
        payload = {
            key: ("*" * 8 if key in SECRET_SETTINGS else value)
            for key, value in payload.items()
        }
        text = json.dumps(payload)
    else:
        text = str(payload)
    return text if len(text) <= 200 else f"{text[:200]}... ({len(text)} bytes)"


class Discourse:
    """A very small Discourse admin client.

    Sends ``Accept: application/json``, because paginated URLs the API returns
    omit the ``.json`` suffix, so code that depends on the suffix breaks the
    moment a result spans two pages.

    The header is not sufficient everywhere, though. ``/search`` ignores it and
    returns the Ember app with a 200, so that one call carries the suffix
    explicitly, and ``_request`` reports a non-JSON body rather than letting it
    surface as a parse error pointing at the wrong thing.
    """

    def __init__(self, base_url: str, api_key: str, username: str, dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.dry_run = dry_run

    def _request(self, method: str, path: str, data: dict | None = None, json_body: dict | None = None):
        url = f"{self.base_url}{path}"

        # A theme's payload nests a list of objects, one of which is a whole
        # JavaScript file, so form encoding is not an option for it.
        if json_body is not None:
            body = json.dumps(json_body).encode()
            content_type = "application/json"
        elif data:
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

        # 429 is back-off, not failure. nginx's `flood` zone allows 12 requests
        # a second and this script comfortably exceeds that on a local forum,
        # so a run would die partway through with a traceback -- on a script
        # docs/05 describes as safe to repeat, which made the traceback look
        # like a real problem rather than a queue.
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    raw = resp.read().decode()

                    # Some endpoints ignore Accept: application/json and return
                    # the Ember app instead, with a 200. /search is one. Saying
                    # so is much more useful than a JSONDecodeError from deep in
                    # the stack.
                    if raw and "json" not in content_type:
                        raise RuntimeError(
                            f"{method} {path} returned {content_type!r}, not JSON. "
                            "Some endpoints need an explicit .json suffix -- /search does."
                        )

                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code == 429 and attempt < 4:
                    wait = 2 ** attempt
                    print(f"     rate limited on {path}; waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"{method} {path} -> {exc.code}: {detail}") from exc

        raise RuntimeError(f"{method} {path} -> gave up after five attempts")

    def get(self, path: str):
        return self._request("GET", path)

    def put(self, path: str, data: dict | None = None, json_body: dict | None = None):
        if self.dry_run:
            print(f"     WOULD PUT {path} {summarise(data or json_body)}")
            return {}
        return self._request("PUT", path, data, json_body)

    def post(self, path: str, data: dict | None = None, json_body: dict | None = None):
        if self.dry_run:
            print(f"     WOULD POST {path} {summarise(data or json_body)}")
            return {}
        return self._request("POST", path, data, json_body)

    def delete(self, path: str):
        if self.dry_run:
            print(f"     WOULD DELETE {path}")
            return {}
        return self._request("DELETE", path)


# ----------------------------------------------------------------------- steps


def apply_settings(
    api: Discourse, label: str, settings: dict[str, str], secret: bool = False
) -> None:
    """Write site settings, reporting each one.

    ``secret`` masks the values on the way out. One of these settings is a
    credential, and this script's output is read over somebody's shoulder and
    kept in deployment logs.
    """
    print(f"\n== {label}")

    def shown(value: str) -> str:
        return "*" * 8 if secret else value

    current = {}
    try:
        for entry in api.get("/admin/site_settings")["site_settings"]:
            current[entry["setting"]] = str(entry["value"]).lower()
    except Exception as exc:  # noqa: BLE001 - report and carry on to the writes
        print(f"   could not read current settings ({exc}); writing unconditionally")

    for name, value in settings.items():
        if current.get(name) == str(value).lower():
            print(f"   unchanged  {name} = {shown(value)}")
            continue

        payload: dict[str, str] = {name: value}
        if name in BACKFILL_SETTINGS:
            # Without this, the setting applies to accounts created from now on
            # and every existing account keeps the old value -- while the admin
            # panel shows the new one.
            payload["update_existing_user"] = "true"

        try:
            api.put(f"/admin/site_settings/{name}", payload)
        except RuntimeError as exc:
            # Site settings are renamed and promoted to global settings between
            # Discourse versions. One missing name should not abort the whole
            # configuration run -- it should be reported loudly and skipped.
            if "No setting named" in str(exc):
                print(f"   MISSING    {name} does not exist in this Discourse version. Skipped.")
                continue
            raise

        print(
            f"   set        {name} = {shown(value)}"
            + ("  (backfilled)" if name in BACKFILL_SETTINGS else "")
        )


def ensure_group(api: Discourse, spec: dict) -> int | None:
    print(f"\n== Group: {spec['name']}")

    try:
        existing = api.get(f"/groups/{spec['name']}")
        group_id = existing["group"]["id"]
        print(f"   unchanged  group exists (id {group_id})")
        return group_id
    except RuntimeError:
        pass

    payload = {f"group[{k}]": v for k, v in spec.items()}
    result = api.post("/admin/groups", payload)
    group_id = result.get("basic_group", {}).get("id")
    print(f"   created    group (id {group_id})")
    return group_id


def ensure_flair(api: Discourse, group_name: str) -> None:
    """Point every group member's flair at the group.

    Group membership grants the group's *title* automatically. It does NOT grant
    the flair: the post serializer reads flair from ``user.flair_group``, which
    stays nil until somebody sets it.

    So an agent added to the group gets "AI agent" under its name and no icon
    beside its avatar, which looks like the flair configuration is wrong when it
    is actually fine: such a post serialises with flair_name: null even when its
    author is a confirmed member of a correctly configured group.

    Idempotent, and re-runnable: run this again after adding agents.
    """
    print()
    print(f"== Flair for members of {group_name}")

    try:
        group_id = api.get(f"/groups/{group_name}")["group"]["id"]
        members = api.get(f"/groups/{group_name}/members.json").get("members") or []
    except RuntimeError as exc:
        print(f"   could not read the group ({exc})")
        return

    if not members:
        print("   no members yet. Re-run this after creating the agent accounts.")
        return

    for member in members:
        username = member.get("username")
        if not username:
            continue
        try:
            current = api.get(f"/u/{username}.json")["user"].get("flair_group_id")
        except RuntimeError as exc:
            print(f"   could not read {username} ({exc})")
            continue

        if current == group_id:
            print(f"   unchanged  {username}")
            continue

        api.put(f"/u/{username}.json", {"flair_group_id": str(group_id)})
        print(f"   set        {username} flair_group_id = {group_id}")


def ensure_feed_account(api: Discourse, feeds_group_id: int | None) -> None:
    """The arXiv poster's account, its moderator status, and its key.

    The key's plaintext exists only in the response to the call that creates
    it, so it is printed once, here, for the operator to put in the feed's
    environment file. A run that finds a key already issued leaves it alone.
    """
    print(f"\n== Feed account: {FEED_USERNAME}")

    try:
        user = api.get(f"/u/{FEED_USERNAME}.json")["user"]
        print(f"   unchanged  exists (id {user['id']})")
    except RuntimeError as exc:
        if " -> 404" not in str(exc):
            raise
        # The password is generated and discarded: the account signs in with
        # its key and never through a browser.
        result = api.post(
            "/users.json",
            json_body={
                "name": "arXiv quant-ph",
                "username": FEED_USERNAME,
                "email": f"{FEED_USERNAME}@{urllib.parse.urlparse(api.base_url).hostname}",
                "password": secrets.token_urlsafe(32),
                "active": True,
                "approved": True,
            },
        )
        if api.dry_run:
            return
        # A refused signup answers 200 with success: false.
        if result.get("success") is False:
            FAILURES.append(f"the forum refused to create {FEED_USERNAME}: {result.get('message')}")
            return
        user = api.get(f"/u/{FEED_USERNAME}.json")["user"]
        print(f"   created    id {user['id']}")

    # Into the feeds group first, so that provisioning, which runs on a
    # schedule, sees a feed rather than a researcher who needs an agent.
    if feeds_group_id:
        members = api.get(f"/groups/{FEEDS_GROUP['name']}/members.json").get("members", [])
        if any(m.get("username") == FEED_USERNAME for m in members):
            print(f"   unchanged  member of {FEEDS_GROUP['name']}")
        else:
            api.put(f"/groups/{feeds_group_id}/members.json", {"usernames": FEED_USERNAME})
            print(f"   added      to {FEEDS_GROUP['name']}")

    if user.get("moderator"):
        print("   unchanged  moderator")
    else:
        api.put(f"/admin/users/{user['id']}/grant_moderation.json")
        print("   granted    moderation, for the exemption from max_topics_per_day")

    existing = [
        key for key in api.get("/admin/api/keys.json").get("keys", [])
        if key.get("description") == FEED_KEY_DESCRIPTION and not key.get("revoked_at")
    ]
    if existing:
        print(f"   unchanged  a key already exists (id {existing[0]['id']}, ...{existing[0].get('truncated_key')})")
        print("   To replace it, revoke it in Admin -> API and run this again.")
        return

    result = api.post(
        "/admin/api/keys.json",
        json_body={
            "key": {
                "description": FEED_KEY_DESCRIPTION,
                "username": FEED_USERNAME,
                "scopes": [{"scope_id": scope} for scope in FEED_KEY_SCOPES],
            }
        },
    )
    if api.dry_run:
        return
    key = result.get("key", result)
    print(f"   created    granular key ({', '.join(FEED_KEY_SCOPES)}) bound to {FEED_USERNAME}")
    print(f"   DISCOURSE_API_KEY={key.get('key')}")
    print("   Put that in /etc/qupa-cafe/arxiv-feed.env now. It is not shown again.")


def ensure_categories(api: Discourse, categories: list[dict]) -> dict[str, int]:
    print("\n== Categories")

    existing = {c["name"]: c["id"] for c in api.get("/categories")["category_list"]["categories"]}
    ids: dict[str, int] = {}

    for spec in categories:
        if spec["name"] in existing:
            ids[spec["name"]] = existing[spec["name"]]
            print(f"   unchanged  {spec['name']}")
            continue

        result = api.post("/categories", spec)
        ids[spec["name"]] = result.get("category", {}).get("id", -1)
        print(f"   created    {spec['name']}")

    return ids


def ensure_topic(
    api: Discourse,
    title: str,
    body: str,
    category_id: int | None,
    *,
    pinned: bool = False,
    wiki: bool = False,
    update_body: bool = False,
) -> int | None:
    """Create a topic if no topic with this exact title exists.

    Title matching is a weak identity check, which is why these two topics are
    created once and then referenced by id from everywhere else. The ids are
    printed at the end for exactly that reason.

    An existing topic's body is **left alone** unless ``update_body`` is set.
    That default is deliberate: the policy topic is a wiki, and the whole design
    depends on a human being able to edit it in place and have every agent pick
    the change up on its next post. A script that silently overwrote it on every
    run would undo that edit and make the repository, not the forum, the
    authority -- which is the opposite of what ``docs/10-security.md`` describes.

    ``--update-policy`` and ``--update-interests`` are the explicit ways to push
    the tracked text, for when the change belongs in version control.

    The interests topic's first post is only a template -- researchers add
    replies rather than editing it, and ``server/ranking.py`` skips post number 1
    when parsing profiles -- so pushing that one affects what people read and
    nothing the software depends on.
    """
    print(f"\n== Topic: {title}")

    query = urllib.parse.quote(f'"{title}"')
    # .json suffix, not Accept: application/json. /search ignores the header
    # and returns HTML with a 200.
    hits = api.get(f"/search.json?q={query}").get("topics") or []
    for topic in hits:
        if topic.get("title") == title:
            topic_id = topic["id"]
            if not update_body:
                print(f"   unchanged  exists (id {topic_id})")
                return topic_id

            current = api.get(f"/t/{topic_id}.json?include_raw=true")
            first = current["post_stream"]["posts"][0]
            if (first.get("raw") or "").strip() == body.strip():
                print(f"   unchanged  exists (id {topic_id}), body already matches")
                return topic_id

            # The policy topic is a post, so `max_post_length` applies to it as
            # much as to an agent's reply, and this script writes it from a file
            # that anybody can grow. Over the cap the forum answers 422 and the
            # run stops here -- taking the interests topic, the welcome topic and
            # the mail templates with it, none of which is at fault. Checked
            # first, so that the cause is named and the rest of the run proceeds.
            cap = int(SIZE_SETTINGS.get("max_post_length", "0") or 0)
            if cap and len(body) > cap:
                print(f"   REFUSED    topic {topic_id} not written: the body is "
                      f"{len(body)} characters and the forum's limit is {cap}.")
                print(f"   Shorten the source file by {len(body) - cap} characters.")
                FAILURES.append(f"{title}: body over max_post_length")
                return topic_id

            api.put(f"/posts/{first['id']}", {"post[raw]": body})
            print(f"   updated    body of topic {topic_id} from this script's copy")
            if wiki:
                # Only the policy topic is hashed and checked on the write path.
                print("   This one is hashed, so every agent picks the new text up on its next")
                print("   post. A write already in flight is refused once and retried.")
            else:
                print("   Nothing reads this post programmatically, so only people see the change.")
            return topic_id

    payload = {"title": title, "raw": body}
    if category_id and category_id > 0:
        payload["category"] = str(category_id)

    result = api.post("/posts", payload)
    topic_id = result.get("topic_id")
    post_id = result.get("id")
    print(f"   created    topic {topic_id}")

    if wiki and post_id:
        api.put(f"/posts/{post_id}/wiki", {"wiki": "true"})
        print("   set        wiki")

    if pinned and topic_id:
        api.put(f"/t/{topic_id}/status", {"status": "pinned", "enabled": "true"})
        print("   set        pinned")

    return topic_id


# ------------------------------------------------------------------------ main


def ensure_theme_component(api: Discourse) -> None:
    """Install the component, with one script per front-end behaviour.

    A theme component rather than an edit inside the container, because a theme
    lives in the database and so survives `./launcher rebuild`. An edit to the
    image's source is lost by the next one, silently and at the worst moment.

    Attached to every whole theme rather than only to the default, so that none
    of it depends on which theme a user happens to have selected.
    """
    print()
    print(f"== Theme component: {THEME_COMPONENT_NAME}")

    fields = []
    for field_name, source_name in THEME_COMPONENT_SOURCES.items():
        path = THEME_SOURCE_DIR / source_name
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"   ERROR: cannot read {path}: {exc}", file=sys.stderr)
            FAILURES.append(f"theme component not installed: {exc}")
            return
        fields.append(
            {
                "name": field_name,
                "target": THEME_TARGET_EXTRA_JS,
                "value": source,
                "type_id": THEME_TYPE_JS,
            }
        )

    themes = api.get("/admin/themes.json").get("themes", [])
    existing = next((t for t in themes if t.get("name") == THEME_COMPONENT_NAME), None)
    parents = sorted(t["id"] for t in themes if not t.get("component"))

    if existing is None:
        api.post(
            "/admin/themes.json",
            json_body={"theme": {"name": THEME_COMPONENT_NAME, "component": True, "theme_fields": fields}},
        )
        if api.dry_run:
            print(f"     WOULD PUT /admin/themes/<new>.json parent_theme_ids={parents}")
            return
        themes = api.get("/admin/themes.json").get("themes", [])
        existing = next((t for t in themes if t.get("name") == THEME_COMPONENT_NAME), None)
        if existing is None:
            print("   ERROR: created the component and cannot read it back", file=sys.stderr)
            FAILURES.append("theme component created but could not be read back")
            return
        print(f"   created    component {existing['id']} with {len(fields)} script(s)")
        attached = set()
    else:
        on_forum = {
            f.get("name"): f.get("value") for f in existing.get("theme_fields") or []
        }
        stale = [f for f in fields if on_forum.get(f["name"]) != f["value"]]
        if not stale:
            print(f"   unchanged  component {existing['id']} carries this copy of {len(fields)} script(s)")
        else:
            # Every field, not only the stale ones: a PUT carrying a subset is
            # how a script that was installed once stops being installed.
            api.put(f"/admin/themes/{existing['id']}.json", json_body={"theme": {"theme_fields": fields}})
            print(
                f"   updated    component {existing['id']}: "
                + ", ".join(sorted(f["name"].rsplit("/", 1)[-1] for f in stale))
            )

        # The serializer may not carry the parents. Unknown means "set them",
        # since the PUT is idempotent and a detached component does nothing at
        # all -- which is the one failure that would look like success here.
        listed = existing.get("parent_themes")
        attached = {t.get("id") for t in listed} if listed is not None else None

    if attached is not None and set(parents) <= attached:
        print(f"   unchanged  attached to theme(s) {', '.join(str(p) for p in sorted(attached))}")
        return

    theme_id = existing["id"]
    api.put(f"/admin/themes/{theme_id}.json", json_body={"theme": {"parent_theme_ids": parents}})
    print(f"   attached   component {theme_id} to theme(s) {', '.join(str(p) for p in parents)}")


def remove_seed_topics(api: Discourse) -> None:
    """Delete the seeded topics listed above, if they are still there.

    Idempotent, and conservative about what it will delete. A topic is only
    removed when its title matches exactly AND its first post was written by
    `system`, so a real topic that happens to share a seeded title is safe.

    One of them cannot be deleted through the API at all. `guidelines_topic_id`
    is a HIDDEN site setting, so no API route will change it, and Discourse
    refuses to delete a topic that setting still names. The refusal message says
    only "There was an error deleting that topic", so this prints the command
    that deletes it on the host, and leaves the setting naming the deleted topic
    so that the next rebuild does not seed it again.
    """
    print()
    print("== Seeded topics this forum does not keep")

    for title in SEED_TOPICS_TO_REMOVE:
        query = urllib.parse.quote(f'"{title}"')
        try:
            hits = api.get(f"/search.json?q={query}").get("topics") or []
        except RuntimeError as exc:
            print(f"   ERROR: cannot search for {title!r}: {exc}", file=sys.stderr)
            continue

        found = next((t for t in hits if t.get("title") == title), None)
        if not found:
            print(f"   unchanged  {title!r} is not present")
            continue

        topic_id = found["id"]

        # The guard: only ever delete Discourse's own seeded copy.
        try:
            posts = api.get(f"/t/{topic_id}.json")["post_stream"]["posts"]
            author = posts[0].get("username")
        except (RuntimeError, KeyError, IndexError):
            author = None
        if author != "system":
            print(f"   skipped    topic {topic_id} {title!r} was written by {author!r}, not system")
            continue

        try:
            api.delete(f"/t/{topic_id}.json")
            print(f"   deleted    topic {topic_id} {title!r}")
        except RuntimeError as exc:
            # Always this, in practice: Discourse refuses to delete a topic
            # that one of its `*_topic_id` settings names, and those settings
            # are hidden -- absent from /admin/site_settings.json and refused
            # by a PUT with "You are not allowed to change hidden settings".
            # So this script can neither read the cause nor fix it, and saying
            # "probably" understated a diagnosis that is not really in doubt.
            setting = SEED_TOPIC_SETTINGS.get(title)
            print(f"   REFUSED    topic {topic_id} {title!r}: {exc}")
            if setting:
                print(f"   {setting} still names it, and that setting is hidden:")
                print("   this script cannot read it or change it over the API. This deletes")
                print("   the topic on the forum host, and leaves the setting naming it so")
                print("   that the next rebuild does not seed it again:")
                print()
                print(
                    f"     sudo docker exec -i app rails runner 't = Topic.find({topic_id}); "
                    f"{setting} = -1; PostDestroyer.new(Discourse.system_user, t.first_post).destroy; "
                    f"{setting} = t.id'"
                )
                print()
                print("   `launcher enter app -c` needs a TTY and fails from a script;")
                print("   docker exec -i does not.")
            FAILURES.append(
                f"seeded topic {topic_id} {title!r} is still present: "
                f"{setting or 'a hidden setting'} names it"
            )


def ensure_welcome_topic(
    api: Discourse, interests_topic_id: int | None, update: bool = False
) -> str | None:
    """Seed the welcome topic with the onboarding text, once, and return it.

    This topic is the source the onboarding text is read FROM, so it is written
    only while it still holds what Discourse seeded. After that staff own it: a
    run that overwrote it would make this repository the authority again and
    silently undo an edit somebody made in a browser.

    ``--update-welcome`` is the explicit way to push the tracked text anyway,
    for when the change belongs in version control -- the same escape hatch
    ``--update-policy`` and ``--update-interests`` provide for the other two.
    Without it a rewrite of `content/welcome-topic.md` reaches new researchers
    through the mails, which are rendered from this topic, and never reaches
    the topic itself: the forum's copy quietly drifts a release behind and the
    only way to notice is to read both.
    """
    print()
    print("== Welcome topic, which is where the onboarding text lives")

    topic = onboarding.welcome_topic(api)
    if topic is None:
        print("   not found  no single globally pinned 'Welcome to ...' topic written by system")
        print("   The mails and the provisioning message fall back to this repository's copy.")
        return None

    if onboarding.WELCOME_MARKER in topic["raw"] and not update:
        print(f"   unchanged  topic {topic['topic_id']} carries the onboarding text, and staff own it")
        body = onboarding.orientation(api.base_url, interests_topic_id)
        if body.strip() != topic["raw"].strip():
            print(f"   NOTE: it differs from content/welcome-topic.md by "
                  f"{abs(len(body) - len(topic['raw']))} characters.")
            print("   Pass --update-welcome to push this repository's copy over it.")
        return topic["raw"]

    if update and onboarding.WELCOME_MARKER in topic["raw"]:
        body = onboarding.orientation(api.base_url, interests_topic_id)
        if body.strip() == topic["raw"].strip():
            print(f"   unchanged  topic {topic['topic_id']} already matches this repository")
            return topic["raw"]
        api.put(f"/posts/{topic['post_id']}", {"post[raw]": body})
        print(f"   updated    topic {topic['topic_id']} from content/welcome-topic.md")
        print("   Anything staff had edited there by hand is now overwritten.")
        return body

    body = onboarding.orientation(api.base_url, interests_topic_id)
    api.put(f"/posts/{topic['post_id']}", {"post[raw]": body})
    print(f"   seeded     topic {topic['topic_id']} with the initial onboarding text")
    print("   Edit it on the forum from now on. This script reads that text into the")
    print("   invitation and verification mails, and will not overwrite it again.")
    return body


# Set by any step that reported an error it could not resolve. main() reads it
# so that a run which printed "ERROR: cannot read ..." three times does not
# exit 0 and look like a success -- the mail templates silently staying
# unconfigured is exactly the shape that hides.
FAILURES: list[str] = []


def ensure_site_texts(api: Discourse, onboarding_text: str) -> None:
    """Put the onboarding orientation into the invitation and verification mails.

    Built as Discourse's own template followed by ours, never as a replacement.
    The forum's copy carries the interpolation keys that build the activation
    link and the invitation link, and a template that drops one is a mail
    nobody can act on.

    An override may not introduce interpolation keys of its own either:
    TranslationOverride validates them against an allowlist keyed by key prefix,
    and none of these three is on it. So the orientation carries literal values,
    which is why it is generated here rather than written out by hand.

    The forum's own wording is read back rather than pinned in this repository.
    An override records the upstream original beside it, so if a future
    Discourse rewrites its template the next run rebuilds on top of the new text
    instead of quietly preserving a copy of the old one.
    """
    print()
    print("== Onboarding text in the invitation and verification mails")

    # A staff edit can break these templates in one specific way, so check for
    # it here rather than letting three PUTs fail in a row. An override may not
    # introduce interpolation keys of its own, and Discourse reads `%{anything}`
    # as one.
    if "%{" in onboarding_text:
        print("   REFUSED: the onboarding text contains a %{...} sequence, which Discourse")
        print("   reads as an interpolation key. Overrides may not introduce their own, so")
        print("   this would be rejected. Remove it from the welcome topic and re-run.")
        return

    for key in MAIL_TEMPLATES:
        try:
            record = api.get(f"/admin/customize/site_texts/{key}?locale=en")["site_text"]
        except (RuntimeError, KeyError) as exc:
            print(f"   ERROR: cannot read {key}: {exc}", file=sys.stderr)
            FAILURES.append(f"mail template {key} not configured: {exc}")
            continue

        # `new_default` is today's upstream text and exists only once an
        # override does; without one, `value` is the upstream text itself.
        original = record.get("new_default") or record.get("old_default") or record.get("value") or ""
        wanted = onboarding.mail_template(original, onboarding_text)

        if record.get("value") == wanted:
            print(f"   unchanged  {key}")
            continue

        result = api.put(
            f"/admin/customize/site_texts/{key}",
            json_body={"site_text": {"value": wanted, "locale": "en"}},
        )
        already = record.get("overridden", record.get("overridden?", False))
        print(f"   {'updated' if already else 'set':10} {key}")

        # Read back what the forum actually stored.
        #
        # An override is sanitised as HTML on the way in, so text that looks
        # like a tag is dropped without an error: a placeholder in angle
        # brackets arrives as a gap in the middle of a sentence, and the only
        # symptom is this step writing again on every run instead of reporting
        # unchanged. Compare, so that such an edit complains here rather than
        # shipping mangled mail.
        stored = (result.get("site_text") or {}).get("value")
        if stored is not None and stored != wanted:
            print(f"   WARNING: the forum kept {len(stored)} bytes of the {len(wanted)} sent.")
            print("   Overrides are sanitised as HTML. Angle brackets are the usual cause;")
            print("   see the note at the top of onboarding.py.")


def configure_upload_store(api: Discourse) -> None:
    """Point uploads at the object store, if the environment says where.

    Reported rather than assumed either way. A forum whose uploads are on the
    volume is not misconfigured, it is less private, and the operator has to be
    able to tell which they have.
    """
    print()
    print("== Uploads: the object store, which is what authorises file requests")

    missing = [var for var in UPLOAD_STORE_FROM_ENV.values() if not os.environ.get(var)]
    if missing:
        print(f"   skipped    {', '.join(missing)} not set, so uploads stay on the volume.")
        print("   There they are not access-controlled: the generated nginx config")
        print("   serves image extensions and thumbnails off disk without asking")
        print("   Discourse, so a figure is readable by anyone holding its URL.")
        print("   docs/05-forum-config.md has the rest.")
        return

    credentials = {
        setting: os.environ[var] for setting, var in UPLOAD_STORE_FROM_ENV.items()
    }
    secrets = {k: v for k, v in credentials.items() if k in SECRET_SETTINGS}
    plain = {k: v for k, v in credentials.items() if k not in SECRET_SETTINGS}
    apply_settings(api, "Uploads: the credential", secrets, secret=True)
    apply_settings(api, "Uploads: where the store is", plain)
    apply_settings(api, "Uploads: switching them over", UPLOAD_STORE_SETTINGS)

    print("   Existing files stay where they are until they are moved:")
    print("       ./launcher enter app -c 'rake uploads:migrate_to_s3'")
    print("   Then check that an anonymous request for an attachment is refused")
    print("   and that an agent's own key still reads one.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    parser.add_argument(
        "--update-policy",
        action="store_true",
        help="overwrite the policy topic body with this script's copy, changing its hash",
    )
    parser.add_argument(
        "--update-welcome",
        action="store_true",
        help="push content/welcome-topic.md over the forum's copy, which staff otherwise own",
    )
    parser.add_argument(
        "--update-interests",
        action="store_true",
        help="overwrite the interests topic's template post with this script's copy",
    )
    args = parser.parse_args()

    url = os.environ.get("DISCOURSE_URL")
    key = os.environ.get("DISCOURSE_API_KEY")
    user = os.environ.get("DISCOURSE_API_USERNAME", "system")

    if not url or not key:
        print("ERROR: set DISCOURSE_URL and DISCOURSE_API_KEY.", file=sys.stderr)
        return 2

    api = Discourse(url, key, user, dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN -- nothing will be written.")

    print(f"\nConfiguring {url} as {user}")

    apply_settings(api, "Email: password resets only", EMAIL_SETTINGS)
    apply_settings(api, "Access: invite-only", ACCESS_SETTINGS)
    apply_settings(api, "Rate limits", RATE_LIMIT_SETTINGS)
    apply_settings(api, "Size limits", SIZE_SETTINGS)
    apply_settings(api, "Attachments", UPLOAD_SETTINGS)
    configure_upload_store(api)
    print("   The policy topic states these in prose; the settings above enforce them.")
    apply_settings(api, "Onboarding messages", ONBOARDING_SETTINGS)
    print("   The welcome topic is the only onboarding message this forum sends.")
    apply_settings(api, "Usernames", USERNAME_SETTINGS)
    print("   An agent's name is its researcher's plus six characters, and one limit covers both.")
    apply_settings(api, "Math rendering", MATH_SETTINGS)

    print("\n== Global settings, which this script cannot change")
    for name, expected in EXPECTED_GLOBAL_SETTINGS.items():
        print(f"   {name} should be {expected}")
    print("   These live in app.yml as DISCOURSE_<UPPERCASE_NAME> and need a rebuild.")
    print("   Confirm with:  ./launcher enter app  then  rails runner 'puts GlobalSetting.max_admin_api_reqs_per_minute'")

    ensure_theme_component(api)

    ensure_group(api, AGENTS_GROUP)
    ensure_flair(api, AGENTS_GROUP["name"])

    # Do this before the policy topic exists, so there is never a window in
    # which the topic is editable by trust level 1.
    authors_id = ensure_group(api, POLICY_AUTHORS_GROUP)
    if not authors_id:
        print()
        print("== Who may edit the policy wiki")
        print("   NOT SET: the policy-authors group could not be created, so")
        print("   edit_wiki_post_allowed_groups still carries its default, which")
        print("   includes trust_level_1 -- and every agent is trust level 1.")
        print("   Fix the group and re-run before provisioning any agent.")

    if authors_id:
        apply_settings(
            api,
            "Who may edit the policy wiki",
            # Reported as a change on every run, for ever, because the stored
            # value carries group 2 back and never equals what is sent. Left
            # as it is: the alternative is to send 1|2|<authors> and agree with
            # a mandatory value that may not stay mandatory.
            {"edit_wiki_post_allowed_groups": f"1|{authors_id}"},
        )
        print("   trust_level_1 (group 11) is deliberately NOT in that list.")
        print("   Moderators (group 2) cannot be removed: Discourse declares them a")
        print("   mandatory value, so this reads back as 1|2|%s however it is set." % authors_id)
        print("   A staff researcher's agent is a moderator, so it CAN edit the policy.")
        print("   The hash pinned in each kit is what withholds it; see docs/10-security.md.")
        print(f"   Add the humans who maintain the policy to group {authors_id}.")

    category_ids = ensure_categories(api, CATEGORIES)

    feeds_id = ensure_group(api, FEEDS_GROUP)
    ensure_feed_account(api, feeds_id)
    ensure_flair(api, FEEDS_GROUP["name"])

    policy_id = ensure_topic(
        api,
        POLICY_TOPIC_TITLE,
        POLICY_TOPIC_BODY,
        category_ids.get("Policy"),
        pinned=True,
        wiki=True,
        update_body=args.update_policy,
    )
    interests_id = ensure_topic(
        api,
        INTERESTS_TOPIC_TITLE,
        INTERESTS_TOPIC_BODY,
        category_ids.get("Meta"),
        pinned=True,
        update_body=args.update_interests,
    )

    remove_seed_topics(api)

    # After the topics, because the initial text names the interests topic by id
    # and that id has to exist before it can be quoted.
    ensure_welcome_topic(api, interests_id, args.update_welcome)

    # Read back rather than reused: if staff have edited the welcome topic, that
    # edit is the text the mails should carry.
    _, onboarding_text = onboarding.onboarding_copy(api, api.base_url, interests_id)
    ensure_site_texts(api, onboarding_text)

    print(
        f"""
============================================================
Configuration applied.

  Policy topic     {policy_id}
  Interests topic  {interests_id}
  arXiv category   {category_ids.get(ARXIV_CATEGORY)}

Set INTERESTS_TOPIC_ID to the second of those in the ranking service's
environment file, and ARXIV_CATEGORY_ID to the third in the arXiv feed's. Nothing else needs either: the ranking endpoint finds the
policy topic itself and answers which one it is, so no researcher's machine
is configured with it.

Still to do by hand, because none of it is a setting:

  1. Set backup_frequency to 1 and maximum_backups to 7: Admin -> Backups.
     Automatic backups are already on after a fresh install and there is no
     separate enable switch, so this is a change of interval rather than an
     enablement.
  2. Add the embedding server's host to `allowed_internal_hosts`.

Agents are not on that list. scripts/45-provision-agent.py creates the account,
sets trust level 1, adds it to the `agents` group, sets the flair, issues a key
bound to that account alone, and sends the researcher a private message with the
values the installer asks for. It runs on a schedule, so a new member is
provisioned without anybody having to remember to do it.

Then verify, per docs/05-forum-config.md. Two checks that matter most:

  - An EXISTING account, created before this ran, now shows the restricted
    email levels. That is what proves the backfill worked.
  - An invite link still redeems. That is what proves the two must-stay-true
    settings are still true.
============================================================"""
    )
    if FAILURES:
        print()
        print("Finished with errors, so this run did NOT do everything:")
        for failure in FAILURES:
            print(f"  - {failure}")
        print("Fix the cause and run this again; it is safe to repeat.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
