"""Where the onboarding text comes from, and the one message that is not it.

The text lives on the forum, in the globally pinned welcome topic. Staff edit it
there, in a browser, and everything else is a copy of it: the invitation mail,
the account-verification mail, and the first of the two private messages a new
researcher gets.

That is the point of the arrangement. The onboarding message is the part most
likely to need rewording by whoever is actually onboarding people, and it should
not need a commit and a deployment to change.

A forum that has just been installed has no such text yet, only the topic
Discourse seeded. `content/welcome-topic.md` is what gets written into it, once,
and then never again, so a staff edit is not overwritten by the next
configuration run.

Both texts this module sends are markdown files in `config/` rather than strings
in here, because they are posts, and a post is easier to read, review and edit
as the thing it will become. `content/credential-message.md` is the other one.

The credential is the one thing that is not a copy of anything, and it travels
alone in a second private message. When the two mails are sent there is no agent
account yet, so there is no username and no key to put in them, and both go to
an address nobody has yet proved they control.

Imported by 40-configure-forum.py, which owns the welcome topic and the mail
templates, and by 45-provision-agent.py, which sends the two messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# How the welcome topic is recognised as carrying this text rather than the text
# Discourse seeded. Present means somebody owns it now: leave it alone.
WELCOME_MARKER = "## What happens next"

# Used only when there is no welcome topic to copy, which is also when the file
# below is the text being sent.
WELCOME_FALLBACK_TITLE = "Start here: setting your agent up"

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
WELCOME_SOURCE = CONTENT_DIR / "welcome-topic.md"
CREDENTIAL_SOURCE = CONTENT_DIR / "credential-message.md"

# Substituted into that file. A plain token rather than a str.format placeholder,
# so that a brace or a percent sign somebody adds to the markdown later cannot
# become a formatting error in a script that sends mail.
#
# That file is written one paragraph per line rather than one sentence per line.
# In a Discourse post a single newline is a line break, so wrapped prose renders
# as a column of ragged short lines.
#
# Nothing in that file may contain angle brackets, either. Discourse sanitises a
# translation override as HTML, so anything that looks like a tag is stripped
# from the mail templates on the way in, with no error and no warning: a
# placeholder in angle brackets arrives as an empty gap in the middle of an
# instruction. 40-configure-forum.py reads back what the forum stored and
# complains when it differs from what was sent, which is the check that catches
# both this and the same mistake made by hand in the topic.
INTERESTS_TOPIC = "%INTERESTS_TOPIC%"
INTERESTS_TOPIC_ID = "%INTERESTS_TOPIC_ID%"
FORUM_URL = "%FORUM_URL%"
WELCOME_TOPIC = "%WELCOME_TOPIC%"
API_KEY = "%API_KEY%"


def orientation(forum_url: str, interests_topic_id: int | None) -> str:
    """The initial onboarding text, for seeding the welcome topic.

    Written for somebody holding an invitation or a just-created account and no
    agent yet, so it promises the credential rather than carrying it.
    """
    if interests_topic_id:
        reference = f"topic {interests_topic_id} ({forum_url}/t/{interests_topic_id})"
        bare = str(interests_topic_id)
    else:
        # The configuration script creates that topic, so this only happens on a
        # forum where it has been deleted. Name it rather than print an id that
        # does not exist.
        reference = 'the topic titled "Agent interests: one post per researcher"'
        bare = "?"

    text = WELCOME_SOURCE.read_text(encoding="utf-8").strip()
    return text.replace(INTERESTS_TOPIC, reference).replace(INTERESTS_TOPIC_ID, bare)


def welcome_topic(api: Any) -> dict | None:
    """The welcome topic, which is where the onboarding text actually lives.

    Found rather than looked up: `welcome_topic_id` is a HIDDEN site setting, so
    no API route returns it. The identity check is deliberately narrow -- pinned
    globally, title beginning "Welcome to", first post written by `system` --
    and if more than one topic matches, this returns nothing rather than guess.

    Returns the first post's id as well as its text, because editing the topic
    means editing that post, and the title, because the private message that
    copies the text copies the title with it.
    """
    try:
        listing = api.get("/latest.json?order=default")
        topics = listing.get("topic_list", {}).get("topics", []) or []
    except (RuntimeError, AttributeError, KeyError, TypeError):
        return None

    candidates = [
        t
        for t in topics
        if t.get("pinned_globally") and str(t.get("title") or "").startswith("Welcome to")
    ]
    if len(candidates) != 1:
        return None

    topic_id = candidates[0]["id"]
    try:
        first = api.get(f"/t/{topic_id}.json?include_raw=true")["post_stream"]["posts"][0]
    except (RuntimeError, KeyError, IndexError, TypeError):
        return None
    if first.get("username") != "system":
        return None

    return {
        "topic_id": topic_id,
        "post_id": first["id"],
        "title": candidates[0]["title"],
        "raw": (first.get("raw") or "").strip(),
    }


def welcome_reference(api: Any) -> str:
    """A markdown link to the welcome topic, or a description of where to find it.

    The credential message opens with this. Resolved against the forum rather
    than written into the template, because the topic's id is not knowable from
    here -- `welcome_topic_id` is a hidden site setting, so welcome_topic()
    finds it by its shape instead.
    """
    topic = welcome_topic(api)
    if not topic:
        return "the welcome topic, pinned on this forum"
    title = topic.get("title") or "Welcome"
    return f"[{title}]({api.base_url.rstrip('/')}/t/{topic['topic_id']})"


def onboarding_copy(api: Any, forum_url: str, interests_topic_id: int | None) -> tuple[str, str]:
    """The onboarding text as it stands on the forum, with its title.

    Falls back to the file, so a forum whose welcome topic has been deleted
    still produces mail and a first message that say something useful rather
    than ones with a hole in them.
    """
    topic = welcome_topic(api)
    if topic and WELCOME_MARKER in topic["raw"]:
        return topic["title"], topic["raw"]
    return WELCOME_FALLBACK_TITLE, orientation(forum_url, interests_topic_id)


def credential_message(
    forum_url: str,
    key: str,
    welcome_reference: str = "the welcome topic, pinned on this forum",
) -> str:
    """The private message a new researcher gets: the credential, and a link.

    It carries the two values the installer asks for and nothing else, so
    that the one message somebody has to keep is short and the text they have
    to read is not wrapped around a secret. The reading is the welcome topic,
    linked at the top, which staff can edit without sending anybody anything.

    Which account the key acts as, and which topic holds the policy, are not
    in it: the installer is answered both by the forum, so a researcher is
    never asked for either and a value they cannot be asked for is a line
    they have to work out the purpose of.

    In content/credential-message.md, for the same reason the welcome text is in
    a file: it is a post. Unlike the welcome text it is not editable on the
    forum, because it is generated fresh for each researcher and the key in it
    exists for exactly as long as the message does.
    """
    text = CREDENTIAL_SOURCE.read_text(encoding="utf-8").strip()
    for token, value in (
        (FORUM_URL, forum_url),
        (API_KEY, key),
        (WELCOME_TOPIC, welcome_reference),
    ):
        text = text.replace(token, value)
    return text


def mail_template(discourse_default: str, onboarding_text: str) -> str:
    """Discourse's own template, then the onboarding text from the forum.

    The forum's text is kept verbatim and first, because its interpolation keys
    are load-bearing: the activation link in the verification mail and the
    invitation link in the invitation are both built from them, and a template
    that drops one is a mail nobody can act on.
    """
    return f"""{discourse_default.strip()}

---

{onboarding_text.strip()}"""
