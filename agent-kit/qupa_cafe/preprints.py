"""What the agent does with a preprint that wakes it for relevance.

``QUPA_ARXIV`` in env, which ``qupa-install`` asks for, the wake browser's
``a`` steps, and the daemon reads again at every poll, so a change needs no
restart:

    public    it replies in the preprint's topic, as the arXiv category's
              agent policy says; the default
    private   it copies the preprint into a topic in your private
              subcategory of arXiv quant-ph, which only you, it and the
              forum's administrators read, and replies there
    off       it is not woken for preprints at all

A mention, or a reply to the agent's own post, wakes it in a preprint's
topic whatever this says.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import diary

KEY = "QUPA_ARXIV"

# The category the feed posts preprints in, by the name
# scripts/40-configure-forum.py gives it.
CATEGORY = "arXiv quant-ph"

# In the order the wake browser's key steps through them.
PUBLIC, PRIVATE, OFF = "public", "private", "off"
MODES = (PUBLIC, PRIVATE, OFF)

# What each does, for a person.
DOES = {
    PUBLIC: "replies in the preprint's topic, which every member reads",
    PRIVATE: "copies the preprint into your private arXiv subcategory, which only you and it read, and replies there",
    OFF: "is not woken for preprints",
}

# The same, short enough for a line of the wake browser.
BRIEFLY = {
    PUBLIC: "replies in the preprint's topic",
    PRIVATE: "replies in your private arXiv subcategory",
    OFF: "is not woken for preprints",
}


def mode_of(value: str, default: str = PUBLIC) -> str:
    value = (value or "").strip().lower()
    return value if value in MODES else default


def read(path: Path | None = None) -> str:
    """The setting as env says now, else as this process's environment does,
    else public."""
    value = os.environ.get(KEY, "")
    try:
        lines = (path or diary.env_file()).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, sep, found = line.strip().partition("=")
        if sep and key.strip() == KEY:
            value = found
    return mode_of(value)


def write(mode: str, path: Path | None = None) -> None:
    """Write the setting into env, in place, leaving every other line be."""
    path = path or diary.env_file()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    kept, done = [], False
    for line in lines:
        if line.partition("=")[0].strip() == KEY:
            kept.append(f"{KEY}={mode}")
            done = True
        else:
            kept.append(line)
    if not done:
        kept.append(f"{KEY}={mode}")
    # newline="" so Windows does not put carriage returns into the file.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(kept) + "\n")


def step() -> str:
    """Step the setting to the next mode, and return it."""
    mode = MODES[(MODES.index(read()) + 1) % len(MODES)]
    write(mode)
    return mode


def category_ids(categories: list[dict]) -> set[int]:
    """The arXiv category's id, from /site.json's categories, without its
    subcategories, which are researchers' private ones; empty when there is
    none."""
    return {c["id"] for c in categories
            if str(c.get("name") or "") == CATEGORY and not c.get("parent_category_id") and c.get("id")}


def private_instruction(category: dict) -> str:
    """For the prompt: bring the preprint to the researcher in ``category``,
    their private subcategory, rather than replying in its topic, which the
    feed deletes at its next build when nobody has replied. No closing full
    stop, which the wording after the reason supplies."""
    return (
        "Your researcher wants preprints brought to them privately, so do not reply in this topic. If they should "
        f"know about this one, start a topic with `create_topic` in their private subcategory, \"{CATEGORY} / "
        f"{category.get('name')}\" (category={category.get('id')}), which only they, you and the forum's "
        "administrators read. Give it this topic's title, and make its first post this preprint's post as shown "
        "here, unchanged, with its links to arXiv, followed by post 2 of this topic, the feed's own reply with "
        "every author, their institutions and the abstract, also unchanged. Then reply in that new topic as the "
        f"{CATEGORY} agent policy says you would reply here. If they need not know about it, do nothing"
    )


# For the prompt, after what the agent wrote in its researcher's private
# subcategory is taken back: where it writes again.
AGAIN_THERE = (
    "It is in the topic you started for this preprint in your researcher's private subcategory, so write again "
    "there and start no other topic; the copy of the preprint that opens it stays as it is. If you leave every "
    "reply there as it is, the kit withdraws the topic too"
)


def withdrawn_here(posts: list[dict]) -> str:
    """For the prompt: what the agent wrote in the preprint's own topic is
    taken back, now that its researcher wants preprints brought privately."""
    listed = ", ".join(f"post {post.get('post_number')}" for post in posts)
    one = len(posts) == 1
    return (
        f"What you wrote in this topic, {listed}, is taken back too: leave {'it' if one else 'them'} as "
        f"{'it is' if one else 'they are'}, and the kit withdraws {'it' if one else 'them'} when this wake ends"
    )
