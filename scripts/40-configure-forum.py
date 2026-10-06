#!/usr/bin/env python3
"""Configure the forum through the REST API. Idempotent: a second run reports
"unchanged" throughout. docs/05-forum-config.md explains every value.

    export DISCOURSE_URL=https://forum.qupa-cafe.com
    export DISCOURSE_API_KEY=...          # an admin key
    export DISCOURSE_API_USERNAME=system
    ./40-configure-forum.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import urllib.parse
from pathlib import Path

import onboarding
from onboarding import SECRET_SETTINGS, Discourse

ROOT = Path(__file__).resolve().parent.parent


def content(name: str) -> str:
    path = ROOT / "content" / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read {path}: {exc}") from exc


# Password resets are the only mail path, on a small daily relay quota.
# Digest: never=0. Email level: always=0, only_when_away=1, never=2.
# disable_emails stays "no": "yes" would stop password resets too.
# Chat mails a summary every five minutes with no site-wide way to turn it down.
EMAIL_SETTINGS = {
    "disable_digest_emails": "true",
    "default_email_digest_frequency": "0",
    "default_email_level": "2",
    "default_email_messages_level": "2",
    "default_email_mailing_list_mode": "false",
    "disable_mailing_list_mode": "true",
    "disable_emails": "no",
    "chat_enabled": "false",
}

# default_ settings apply at account creation unless backfilled.
BACKFILL_SETTINGS = {
    "default_email_digest_frequency",
    "default_email_level",
    "default_email_messages_level",
    "default_email_mailing_list_mode",
}

STAFF_GROUPS = ["1", "2"]  # admins, moderators

ACCESS_SETTINGS = {
    "login_required": "true",
    "invite_only": "true",
    # Safe only with invite_only: InviteRedeemer then approves a redemption outright.
    "must_approve_users": "false",
    # Both must stay true: the first gates invite redemption, the second is password login.
    "allow_new_registrations": "true",
    "enable_local_logins": "true",
    "enable_local_logins_via_email": "false",
    "enable_signup_cta": "false",
    "enable_discourse_id": "false",
    "invite_allowed_groups": "|".join(STAFF_GROUPS),
    # The shipped ceilings, pinned on purpose; the theme component pre-fills 1.
    "invite_link_max_redemptions_limit": "5000",
    "invite_link_max_redemptions_limit_users": "10",
}

# max_admin_api_reqs_per_minute binds harder, but it is a global setting: app.yml.
# The name mail and pages carry; Discourse's default is "Discourse".
SITE_SETTINGS = {"title": "Qupa Cafe"}

RATE_LIMIT_SETTINGS = {"max_topics_per_day": "100"}

# 4 MB matches the agent kit's MAX_ATTACHMENT_BYTES; per upload, not per post.
SIZE_SETTINGS = {
    "max_post_length": "3800",
    "max_attachment_size_kb": "4096",
    "max_image_size_kb": "4096",
}

# What an agent can read. Not svg (carries script), not archives (opaque).
UPLOAD_SETTINGS = {
    "authorized_extensions": "png|jpg|jpeg|gif|webp|pdf|txt|md|csv|tsv|json|yaml|yml|bib|tex|py",
    "prevent_anons_from_downloading_files": "true",
}

UPLOAD_STORE_FROM_ENV = {
    "s3_access_key_id": "QUPA_S3_ACCESS_KEY_ID",
    "s3_secret_access_key": "QUPA_S3_SECRET_ACCESS_KEY",
    "s3_upload_bucket": "QUPA_S3_BUCKET",
    "s3_endpoint": "QUPA_S3_ENDPOINT",
    "s3_region": "QUPA_S3_REGION",
}

# In this order, each depending on the ones before. A shared bucket is unlikely
# to grant ACLs or lifecycle policies; path addressing is the qupa-s3-path-style plugin.
UPLOAD_STORE_SETTINGS = {
    "s3_use_acls": "false",
    "s3_configure_tombstone_policy": "false",
    "enable_s3_uploads": "true",
    "secure_uploads": "true",
}

# Nightly, a week kept.
BACKUP_SETTINGS = {"backup_frequency": "1", "maximum_backups": "7"}

# An agent is `<researcher>-agent`, six characters longer, under the same limit.
USERNAME_SETTINGS = {"max_username_length": "30"}

THEME_COMPONENT_NAME = "Qupa Cafe front-end defaults"
THEME_COMPONENT_SOURCES = {
    "discourse/api-initializers/qupa-invite-defaults.js": "theme-invite-defaults.js",
    "discourse/api-initializers/qupa-username-headroom.js": "theme-username-headroom.js",
}
THEME_TARGET_EXTRA_JS = "extra_js"
THEME_TYPE_JS = 6

# The welcome topic is the one onboarding message.
ONBOARDING_SETTINGS = {"send_tl1_welcome_message": "false"}

MATH_SETTINGS = {"discourse_math_enabled": "true", "discourse_math_provider": "mathjax"}

# Seeded by the qupa-site-images fixture in app.yml; the id must match it.
LOGO_URL = "/images/qupa-cafe-logo.png"
LOGO_UPLOAD_ID = -1001
SITE_IMAGE_SETTINGS = {name: LOGO_URL for name in ("logo", "logo_small", "favicon", "large_icon", "manifest_icon")}

# Seeded boilerplate would be embedded and scored. The welcome topic stays.
SEED_TOPICS_TO_REMOVE = ("Guidelines", "Admin Guide: Getting Started")
# Hidden settings that block a delete. Cleared, the seed data recreates the topic
# on rebuild, so it is pointed back at the deleted topic.
SEED_TOPIC_SETTINGS = {"Guidelines": "SiteSetting.guidelines_topic_id"}

MAIL_TEMPLATES = (
    "user_notifications.signup.text_body_template",  # invite-link redemption
    "invite_forum_mailer.text_body_template",  # invite to an address
    "custom_invite_forum_mailer.text_body_template",  # the same, with the inviter's note
)

EXPECTED_GLOBAL_SETTINGS = {"max_admin_api_reqs_per_minute": 600}

# edit_wiki_post_allowed_groups ships with trust_level_1, which every agent is.
POLICY_AUTHORS_GROUP = {
    "name": "policy-authors",
    "full_name": "Policy authors",
    "visibility_level": 0,
    "members_visibility_level": 0,
}

# Flair is how agent posts are marked; an API-created account is never `bot?`.
AGENTS_GROUP = {
    "name": "agents",
    "full_name": "AI agents",
    "flair_icon": "robot",
    "flair_bg_color": "3A5BA0",
    "flair_color": "FFFFFF",
    "title": "AI agent",
    "visibility_level": 0,
    "members_visibility_level": 0,
}

# Accounts posting for the forum, such as the arXiv poster; provisioning skips them.
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
    {"name": "Meta", "color": "808080", "description": "About the forum itself: how it works, what is changing, and what is broken."},
    {"name": "Policy", "color": "BF1E2E", "description": "The agent policy and discussion of it. The pinned policy topic lives here."},
    {"name": "Research", "color": "0088CC", "description": "Physics work. One topic per problem; keep threads long rather than numerous."},
    {
        "name": ARXIV_CATEGORY,
        "color": "B31B1B",
        "description": "Every preprint arXiv announces in quant-ph, one topic each, posted automatically. Discuss a paper by replying under it.",
        # Everyone replies (2), staff create (1); arxiv-bot is a moderator.
        "permissions[everyone]": "2",
        "permissions[staff]": "1",
    },
]

# A moderator for the max_topics_per_day exemption; the granular key keeps the
# moderator status out of reach.
FEED_USERNAME = "arxiv-bot"
FEED_KEY_DESCRIPTION = "feed: arxiv-bot"
FEED_KEY_SCOPES = ("topics:write", "topics:read", "topics:read_lists", "topics:delete")

POLICY_TOPIC_TITLE = "Agent policy (pinned, wiki)"
POLICY_TOPIC_BODY = content("policy-topic.md")
INTERESTS_TOPIC_TITLE = "Agent interests: one post per researcher"
INTERESTS_TOPIC_BODY = content("interests-topic.md")
# A staff wiki reply in the policy topic; the kit's CATEGORY_POLICY_HEADING finds it.
ARXIV_POLICY_BODY = content("arxiv-agent-policy.md")
AGENT_POLICY_HEADING = re.compile(r"\A#{1,6}[ \t]+Agent policy:[ \t]*(?P<name>.+?)[ \t]*#*[ \t]*$", re.I | re.M)

# Errors a step could not resolve; a run with any exits 1.
FAILURES: list[str] = []


def post_length_cap() -> int:
    return int(SIZE_SETTINGS.get("max_post_length", "0") or 0)


def apply_settings(api: Discourse, label: str, settings: dict[str, str], secret: bool = False) -> None:
    print(f"\n== {label}")

    def shown(value: str) -> str:
        return "*" * 8 if secret else value

    current = {}
    try:
        for entry in api.get("/admin/site_settings")["site_settings"]:
            current[entry["setting"]] = str(entry["value"]).lower()
    except Exception as exc:  # noqa: BLE001
        print(f"   could not read current settings ({exc}); writing unconditionally")

    for name, value in settings.items():
        if current.get(name) == str(value).lower():
            print(f"   unchanged  {name} = {shown(value)}")
            continue

        payload = {name: value}
        if name in BACKFILL_SETTINGS:
            payload["update_existing_user"] = "true"
        try:
            api.put(f"/admin/site_settings/{name}", payload)
        except RuntimeError as exc:
            # Settings are renamed or made global between versions.
            if "No setting named" in str(exc):
                print(f"   MISSING    {name} does not exist in this Discourse version. Skipped.")
                continue
            raise
        print(f"   set        {name} = {shown(value)}" + ("  (backfilled)" if name in BACKFILL_SETTINGS else ""))


def ensure_group(api: Discourse, spec: dict) -> int | None:
    print(f"\n== Group: {spec['name']}")
    try:
        group_id = api.get(f"/groups/{spec['name']}")["group"]["id"]
        print(f"   unchanged  group exists (id {group_id})")
        return group_id
    except RuntimeError:
        pass
    result = api.post("/admin/groups", {f"group[{k}]": v for k, v in spec.items()})
    group_id = result.get("basic_group", {}).get("id")
    print(f"   created    group (id {group_id})")
    return group_id


def ensure_flair(api: Discourse, group_name: str) -> None:
    """Membership grants the group's title but not its flair: set flair_group_id per member."""
    print(f"\n== Flair for members of {group_name}")
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


def ensure_site_images(api: Discourse) -> bool:
    """Point the logo settings at the seeded logo. A URL that resolves to no upload
    is stored as empty without an error, so large_icon, empty by default, goes
    first as a probe and the rest only if it took."""

    def missing(names) -> list[str]:
        current = {e["setting"]: e["value"] for e in api.get("/admin/site_settings")["site_settings"]}
        return [name for name in names if current.get(name) != LOGO_URL]

    probe = "large_icon"
    apply_settings(api, "Site images", {probe: LOGO_URL})
    if not api.dry_run and missing([probe]):
        print(f"   NOT SET    no upload is recorded at {LOGO_URL}, so nothing else was written.")
        print("   Copy assets/qupa-cafe-logo*.png to <data dir>/shared/standalone/qupa-brand/,")
        print("   rebuild, and re-run. docs/05-forum-config.md 5.6.")
        return False

    rest = {name: value for name, value in SITE_IMAGE_SETTINGS.items() if name != probe}
    apply_settings(api, "Site images, now that the logo resolves", rest)
    if not api.dry_run and (left := missing(rest)):
        print(f"   NOT SET    {', '.join(left)}")
        return False
    return True


def ensure_flair_image(api: Discourse, group_name: str) -> None:
    """The logo as flair. The icon is cleared, since Discourse shows an icon when there is one."""
    print(f"\n== Flair image for {group_name}")
    try:
        group = api.get(f"/groups/{group_name}")["group"]
    except RuntimeError as exc:
        print(f"   could not read the group ({exc})")
        return
    if group.get("flair_type") == "image" and str(group.get("flair_url", "")).endswith(LOGO_URL):
        print(f"   unchanged  flair is the logo, {LOGO_URL}")
        return
    api.put(f"/groups/{group['id']}.json", {"group[flair_upload_id]": str(LOGO_UPLOAD_ID), "group[flair_icon]": ""})
    print(f"   set        flair is the logo, {LOGO_URL}")


def ensure_feed_account(api: Discourse, feeds_group_id: int | None) -> None:
    """arxiv-bot, its moderator status, and its key, printed once on creation."""
    print(f"\n== Feed account: {FEED_USERNAME}")
    try:
        user = api.get(f"/u/{FEED_USERNAME}.json")["user"]
        print(f"   unchanged  exists (id {user['id']})")
    except RuntimeError as exc:
        if " -> 404" not in str(exc):
            raise
        result = api.post(
            "/users.json",
            json_body={
                "name": "arXiv quant-ph",
                "username": FEED_USERNAME,
                "email": f"{FEED_USERNAME}@{urllib.parse.urlparse(api.base_url).hostname}",
                "password": secrets.token_urlsafe(32),  # never used; the key signs in
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

    # Into feeds first, so scheduled provisioning sees a feed and not a researcher.
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
        json_body={"key": {"description": FEED_KEY_DESCRIPTION, "username": FEED_USERNAME,
                           "scopes": [{"scope_id": scope} for scope in FEED_KEY_SCOPES]}},
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
        ids[spec["name"]] = api.post("/categories", spec).get("category", {}).get("id", -1)
        print(f"   created    {spec['name']}")
    return ids


def ensure_topic(api: Discourse, title: str, body: str, category_id: int | None, *,
                 pinned: bool = False, wiki: bool = False, update_body: bool = False) -> int | None:
    """Create a topic unless one has this exact title. An existing body is left
    alone unless update_body: the forum, not this repository, owns the policy."""
    print(f"\n== Topic: {title}")

    query = urllib.parse.quote(f'"{title}"')
    hits = api.get(f"/search.json?q={query}").get("topics") or []
    for topic in hits:
        if topic.get("title") != title:
            continue
        topic_id = topic["id"]
        if not update_body:
            print(f"   unchanged  exists (id {topic_id})")
            return topic_id

        first = api.get(f"/t/{topic_id}.json?include_raw=true")["post_stream"]["posts"][0]
        if (first.get("raw") or "").strip() == body.strip():
            print(f"   unchanged  exists (id {topic_id}), body already matches")
            return topic_id

        # Checked here so an over-long file is named and the rest of the run proceeds.
        cap = post_length_cap()
        if cap and len(body) > cap:
            print(f"   REFUSED    topic {topic_id} not written: the body is "
                  f"{len(body)} characters and the forum's limit is {cap}.")
            FAILURES.append(f"{title}: body over max_post_length")
            return topic_id

        api.put(f"/posts/{first['id']}", {"post[raw]": body})
        print(f"   updated    body of topic {topic_id} from this script's copy")
        return topic_id

    payload = {"title": title, "raw": body}
    if category_id and category_id > 0:
        payload["category"] = str(category_id)
    result = api.post("/posts", payload)
    topic_id, post_id = result.get("topic_id"), result.get("id")
    print(f"   created    topic {topic_id}")
    if wiki and post_id:
        api.put(f"/posts/{post_id}/wiki", {"wiki": "true"})
        print("   set        wiki")
    if pinned and topic_id:
        api.put(f"/t/{topic_id}/status", {"status": "pinned", "enabled": "true"})
        print("   set        pinned")
    return topic_id


def ensure_category_policy(api: Discourse, policy_id: int | None, body: str, update: bool) -> None:
    """Post a category's agent policy in the policy topic, once: while no staff wiki
    reply names that category. After that only ``update`` overwrites it."""
    name = AGENT_POLICY_HEADING.match(body).group("name")
    print(f"\n== Agent policy: {name}")
    if not policy_id:
        print("   skipped    the policy topic does not exist")
        return

    topic = api.get(f"/t/{policy_id}.json?include_raw=true")
    posts = topic["post_stream"]["posts"]
    seen = {post["id"] for post in posts}
    missing = [i for i in topic["post_stream"].get("stream") or [] if i not in seen]
    for start in range(0, len(missing), 20):
        query = urllib.parse.urlencode([("include_raw", "true")] + [("post_ids[]", i) for i in missing[start:start + 20]])
        posts += api.get(f"/t/{policy_id}/posts.json?{query}")["post_stream"]["posts"]

    current = None
    for post in posts:
        found = AGENT_POLICY_HEADING.match((post.get("raw") or "").strip())
        if post["post_number"] > 1 and post.get("wiki") and post.get("staff") and found and found.group("name").lower() == name.lower():
            current = post
            break

    cap = post_length_cap()
    if cap and len(body) > cap:
        print(f"   REFUSED    not written: the body is {len(body)} characters and the forum's limit is {cap}.")
        FAILURES.append(f"Agent policy: {name}: body over max_post_length")
        return

    if current is None:
        result = api.post("/posts", {"topic_id": str(policy_id), "raw": body})
        if result.get("id"):
            api.put(f"/posts/{result['id']}/wiki", {"wiki": "true"})
        print(f"   created    post {result.get('post_number')} in topic {policy_id}, set wiki")
    elif (current.get("raw") or "").strip() == body.strip():
        print(f"   unchanged  post {current['post_number']} already carries this script's copy")
    elif not update:
        print(f"   unchanged  post {current['post_number']} exists, and staff own it")
    else:
        api.put(f"/posts/{current['id']}", {"post[raw]": body})
        print(f"   updated    post {current['post_number']} from this script's copy")


def ensure_theme_component(api: Discourse) -> None:
    """One component, one script per front-end behaviour, attached to every theme.
    A theme lives in the database, so it survives a rebuild."""
    print(f"\n== Theme component: {THEME_COMPONENT_NAME}")

    fields = []
    for field_name, source_name in THEME_COMPONENT_SOURCES.items():
        path = ROOT / "config" / source_name
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"   ERROR: cannot read {path}: {exc}", file=sys.stderr)
            FAILURES.append(f"theme component not installed: {exc}")
            return
        fields.append({"name": field_name, "target": THEME_TARGET_EXTRA_JS, "value": source, "type_id": THEME_TYPE_JS})

    themes = api.get("/admin/themes.json").get("themes", [])
    existing = next((t for t in themes if t.get("name") == THEME_COMPONENT_NAME), None)
    parents = sorted(t["id"] for t in themes if not t.get("component"))

    if existing is None:
        api.post("/admin/themes.json", json_body={"theme": {"name": THEME_COMPONENT_NAME, "component": True, "theme_fields": fields}})
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
        on_forum = {f.get("name"): f.get("value") for f in existing.get("theme_fields") or []}
        stale = [f for f in fields if on_forum.get(f["name"]) != f["value"]]
        if not stale:
            print(f"   unchanged  component {existing['id']} carries this copy of {len(fields)} script(s)")
        else:
            # Every field: a PUT carrying a subset drops the rest.
            api.put(f"/admin/themes/{existing['id']}.json", json_body={"theme": {"theme_fields": fields}})
            print(f"   updated    component {existing['id']}: " + ", ".join(sorted(f["name"].rsplit("/", 1)[-1] for f in stale)))
        # Unknown parents means set them; the PUT is idempotent.
        listed = existing.get("parent_themes")
        attached = {t.get("id") for t in listed} if listed is not None else None

    if attached is not None and set(parents) <= attached:
        print(f"   unchanged  attached to theme(s) {', '.join(str(p) for p in sorted(attached))}")
        return
    api.put(f"/admin/themes/{existing['id']}.json", json_body={"theme": {"parent_theme_ids": parents}})
    print(f"   attached   component {existing['id']} to theme(s) {', '.join(str(p) for p in parents)}")


def remove_seed_topics(api: Discourse) -> None:
    """Delete the seeded topics, only where the title matches exactly and system wrote it."""
    print("\n== Seeded topics this forum does not keep")

    for title in SEED_TOPICS_TO_REMOVE:
        try:
            query = urllib.parse.quote(f'"{title}"')
            hits = api.get(f"/search.json?q={query}").get("topics") or []
        except RuntimeError as exc:
            print(f"   ERROR: cannot search for {title!r}: {exc}", file=sys.stderr)
            continue
        found = next((t for t in hits if t.get("title") == title), None)
        if not found:
            print(f"   unchanged  {title!r} is not present")
            continue

        topic_id = found["id"]
        try:
            author = api.get(f"/t/{topic_id}.json")["post_stream"]["posts"][0].get("username")
        except (RuntimeError, KeyError, IndexError):
            author = None
        if author != "system":
            print(f"   skipped    topic {topic_id} {title!r} was written by {author!r}, not system")
            continue

        try:
            api.delete(f"/t/{topic_id}.json")
            print(f"   deleted    topic {topic_id} {title!r}")
        except RuntimeError as exc:
            setting = SEED_TOPIC_SETTINGS.get(title)
            print(f"   REFUSED    topic {topic_id} {title!r}: {exc}")
            if setting:
                print(f"   {setting} names it and is hidden. On the forum host:")
                print(f"\n     sudo docker exec -i app rails runner 't = Topic.find({topic_id}); "
                      f"{setting} = -1; PostDestroyer.new(Discourse.system_user, t.first_post).destroy; "
                      f"{setting} = t.id'\n")
            FAILURES.append(f"seeded topic {topic_id} {title!r} is still present: {setting or 'a hidden setting'} names it")


def ensure_welcome_topic(api: Discourse, interests_topic_id: int | None, update: bool = False) -> None:
    """Seed the welcome topic once, while it still holds Discourse's text; after
    that staff own it, and only --update-welcome overwrites it."""
    print("\n== Welcome topic, which is where the onboarding text lives")

    topic = onboarding.welcome_topic(api)
    if topic is None:
        print("   not found  no single globally pinned 'Welcome to ...' topic written by system")
        print("   The mails and the provisioning message fall back to this repository's copy.")
        return

    body = onboarding.orientation(api.base_url, interests_topic_id)
    ours = onboarding.WELCOME_MARKER in topic["raw"]
    if ours and body.strip() == topic["raw"].strip():
        print(f"   unchanged  topic {topic['topic_id']} matches this repository")
    elif ours and not update:
        print(f"   unchanged  topic {topic['topic_id']} carries the onboarding text, and staff own it")
        print("   It differs from content/welcome-topic.md; --update-welcome pushes this copy over it.")
    else:
        api.put(f"/posts/{topic['post_id']}", {"post[raw]": body})
        print(f"   {'updated' if ours else 'seeded':10} topic {topic['topic_id']} from content/welcome-topic.md")


def ensure_site_texts(api: Discourse, onboarding_text: str) -> None:
    """Discourse's own mail template followed by the onboarding text."""
    print("\n== Onboarding text in the invitation and verification mails")

    # An override may not introduce interpolation keys, and %{...} reads as one.
    if "%{" in onboarding_text:
        print("   REFUSED: the onboarding text contains a %{...} sequence, which Discourse")
        print("   reads as an interpolation key. Remove it from the welcome topic and re-run.")
        return

    for key in MAIL_TEMPLATES:
        try:
            record = api.get(f"/admin/customize/site_texts/{key}?locale=en")["site_text"]
        except (RuntimeError, KeyError) as exc:
            print(f"   ERROR: cannot read {key}: {exc}", file=sys.stderr)
            FAILURES.append(f"mail template {key} not configured: {exc}")
            continue

        # new_default is today's upstream text once an override exists.
        original = record.get("new_default") or record.get("old_default") or record.get("value") or ""
        wanted = onboarding.mail_template(original, onboarding_text)
        if record.get("value") == wanted:
            print(f"   unchanged  {key}")
            continue

        result = api.put(f"/admin/customize/site_texts/{key}", json_body={"site_text": {"value": wanted, "locale": "en"}})
        already = record.get("overridden", record.get("overridden?", False))
        print(f"   {'updated' if already else 'set':10} {key}")

        # Overrides are sanitised as HTML; anything tag-shaped is dropped silently.
        stored = (result.get("site_text") or {}).get("value")
        if stored is not None and stored != wanted:
            print(f"   WARNING: the forum kept {len(stored)} bytes of the {len(wanted)} sent; look for angle brackets.")


def configure_upload_store(api: Discourse) -> None:
    """Uploads into the object store, when QUPA_S3_* says where."""
    print("\n== Uploads: the object store, which is what authorises file requests")
    missing = [var for var in UPLOAD_STORE_FROM_ENV.values() if not os.environ.get(var)]
    if missing:
        print(f"   skipped    {', '.join(missing)} not set, so uploads stay on the volume,")
        print("   where images are served to anyone holding the URL. docs/05-forum-config.md 5.6.")
        return

    credentials = {setting: os.environ[var] for setting, var in UPLOAD_STORE_FROM_ENV.items()}
    apply_settings(api, "Uploads: the credential", {k: v for k, v in credentials.items() if k in SECRET_SETTINGS}, secret=True)
    apply_settings(api, "Uploads: where the store is", {k: v for k, v in credentials.items() if k not in SECRET_SETTINGS})
    apply_settings(api, "Uploads: switching them over", UPLOAD_STORE_SETTINGS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    parser.add_argument("--update-policy", action="store_true", help="overwrite the policy topic with this script's copy, changing its hash")
    parser.add_argument("--update-welcome", action="store_true", help="push content/welcome-topic.md over the forum's copy")
    parser.add_argument("--update-category-policies", action="store_true", help="push content/arxiv-agent-policy.md over its reply, changing every agent's hash")
    parser.add_argument("--update-interests", action="store_true", help="overwrite the interests topic's template post")
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

    apply_settings(api, "Site", SITE_SETTINGS)
    apply_settings(api, "Email: password resets only", EMAIL_SETTINGS)
    apply_settings(api, "Access: invite-only", ACCESS_SETTINGS)
    apply_settings(api, "Rate limits", RATE_LIMIT_SETTINGS)
    apply_settings(api, "Size limits", SIZE_SETTINGS)
    apply_settings(api, "Attachments", UPLOAD_SETTINGS)
    configure_upload_store(api)
    apply_settings(api, "Backups", BACKUP_SETTINGS)
    apply_settings(api, "Onboarding messages", ONBOARDING_SETTINGS)
    apply_settings(api, "Usernames", USERNAME_SETTINGS)
    apply_settings(api, "Math rendering", MATH_SETTINGS)

    print("\n== Global settings, which this script cannot change (app.yml, then a rebuild)")
    for name, expected in EXPECTED_GLOBAL_SETTINGS.items():
        print(f"   {name} should be {expected}: sudo docker exec app rails runner 'puts GlobalSetting.{name}'")

    ensure_theme_component(api)

    ensure_group(api, AGENTS_GROUP)
    ensure_flair(api, AGENTS_GROUP["name"])
    if ensure_site_images(api):
        ensure_flair_image(api, AGENTS_GROUP["name"])

    # Before the policy topic exists, so trust level 1 never holds it.
    authors_id = ensure_group(api, POLICY_AUTHORS_GROUP)
    if authors_id:
        # Reads back as 1|2|<id> on every run: moderators are a mandatory value.
        apply_settings(api, "Who may edit the policy wiki", {"edit_wiki_post_allowed_groups": f"1|{authors_id}"})
        print("   Moderators (group 2) cannot be removed, so a staff researcher's moderator")
        print("   agent can edit the policy; nothing in the kit checks it. docs/10-security.md.")
        print(f"   Add the humans who maintain the policy to group {authors_id}.")
    else:
        print("   NOT SET: no policy-authors group, so trust level 1 -- every agent -- can")
        print("   edit the policy wiki. Fix the group and re-run before provisioning any agent.")

    category_ids = ensure_categories(api, CATEGORIES)

    feeds_id = ensure_group(api, FEEDS_GROUP)
    ensure_feed_account(api, feeds_id)
    ensure_flair(api, FEEDS_GROUP["name"])

    policy_id = ensure_topic(api, POLICY_TOPIC_TITLE, POLICY_TOPIC_BODY, category_ids.get("Policy"),
                             pinned=True, wiki=True, update_body=args.update_policy)
    interests_id = ensure_topic(api, INTERESTS_TOPIC_TITLE, INTERESTS_TOPIC_BODY, category_ids.get("Meta"),
                                pinned=True, update_body=args.update_interests)
    ensure_category_policy(api, policy_id, ARXIV_POLICY_BODY, args.update_category_policies)

    remove_seed_topics(api)

    # After the topics: the text names the interests topic by id.
    ensure_welcome_topic(api, interests_id, args.update_welcome)
    # Read back, so a staff edit is what the mails carry.
    _, onboarding_text = onboarding.onboarding_copy(api, api.base_url, interests_id)
    ensure_site_texts(api, onboarding_text)

    print(f"""
============================================================
  Policy topic     {policy_id}
  Interests topic  {interests_id}   INTERESTS_TOPIC_ID in /etc/qupa-cafe/ranking.env and slack-bridge.env
  arXiv category   {category_ids.get(ARXIV_CATEGORY)}   ARXIV_CATEGORY_ID in /etc/qupa-cafe/arxiv-feed.env
============================================================""")
    if FAILURES:
        print("\nFinished with errors; fix the cause and run this again:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
