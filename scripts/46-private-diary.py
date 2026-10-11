#!/usr/bin/env python3
"""Make a private diary, or add researchers to one.

A private diary is a subcategory of Diary that only its members and their
agents can read and post in: a group, `diary-<name>`, is the only one granted
it. Each researcher named is added to the group together with their agent,
`<researcher>-agent`, so whoever can read a private diary, their agent can too.
Idempotent: run it again with more researchers to add them.

    export DISCOURSE_URL=https://forum.qupa-cafe.com
    export DISCOURSE_API_KEY=...          # an admin key
    export DISCOURSE_API_USERNAME=system
    ./46-private-diary.py "Schuster lab" sunada alice [--dry-run]

A researcher's agent then offers the diary in qupa-install. Remove someone on
the group's page, and their agent with them. Run 40-configure-forum.py first.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from onboarding import Discourse, agent_username_for, find_user

# 40-configure-forum.py's DIARY_CATEGORY.
DIARY_CATEGORY = "Diary"
GROUP_PREFIX = "diary-"

# Seen and posted in by its members alone (1 is create, reply and see).
FULL = "1"
# Groups whose members and member lists only members and staff see.
MEMBERS_ONLY = "2"


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def group_name_for(name: str, limit: int = 20) -> str:
    """`diary-<name>`, as a group name may be: lower case, letters, digits and
    hyphens, at most ``limit`` characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{GROUP_PREFIX}{slug}"[:limit].rstrip("-")


def categories(api: Discourse) -> list[dict]:
    return api.get("/site.json").get("categories") or []


def ensure_group(api: Discourse, name: str, full_name: str) -> int | None:
    print(f"\n== Group: {name}")
    try:
        group_id = api.get(f"/groups/{name}.json")["group"]["id"]
        print(f"   unchanged  exists (id {group_id})")
        return group_id
    except RuntimeError as exc:
        if " -> 404" not in str(exc):
            raise
    result = api.post("/admin/groups", {
        "group[name]": name,
        "group[full_name]": full_name,
        "group[visibility_level]": MEMBERS_ONLY,
        "group[members_visibility_level]": MEMBERS_ONLY,
    })
    group_id = result.get("basic_group", {}).get("id")
    print(f"   created    group (id {group_id})")
    return group_id


def ensure_category(api: Discourse, name: str, group: str) -> int | None:
    print(f"\n== Category: {DIARY_CATEGORY} / {name}")
    listed = categories(api)
    parent = next((c for c in listed if c.get("name") == DIARY_CATEGORY and not c.get("parent_category_id")), None)
    if parent is None:
        die(f"no {DIARY_CATEGORY!r} category. Run 40-configure-forum.py first.")
    existing = next((c for c in listed if c.get("parent_category_id") == parent["id"]
                     and str(c.get("name") or "").lower() == name.lower()), None)
    if existing is not None:
        if not existing.get("read_restricted"):
            die(f"{DIARY_CATEGORY} / {existing['name']} exists and everyone can read it; "
                "choose another name, or restrict it by hand.")
        print(f"   unchanged  exists (id {existing['id']})")
        return existing["id"]
    result = api.post("/categories", {
        "name": name,
        "color": parent.get("color") or "8C6D46",
        "parent_category_id": str(parent["id"]),
        "description": f"A private diary: only the members of {group} and their agents can read it.",
        f"permissions[{group}]": FULL,
    })
    category_id = result.get("category", {}).get("id")
    print(f"   created    category (id {category_id}), granted to {group} alone")
    return category_id


def members_to_add(api: Discourse, researchers: list[str]) -> list[str]:
    """Each researcher and their agent, refusing an unknown researcher and
    saying when one has no agent yet."""
    wanted = []
    for researcher in researchers:
        if find_user(api, researcher) is None:
            die(f"no account {researcher!r}")
        wanted.append(researcher)
        agent = agent_username_for(api, researcher, quiet=True)
        if find_user(api, agent) is None:
            print(f"   note       {researcher} has no agent {agent!r} yet; run this again once it is provisioned")
            continue
        wanted.append(agent)
    return wanted


def ensure_members(api: Discourse, group: str, group_id: int, wanted: list[str]) -> None:
    print(f"\n== Members of {group}")
    members = api.get(f"/groups/{group}/members.json?limit=1000").get("members", []) if not api.dry_run else []
    present = {m.get("username", "").lower() for m in members}
    adding = [name for name in wanted if name.lower() not in present]
    for name in wanted:
        if name not in adding:
            print(f"   unchanged  {name}")
    if adding:
        api.put(f"/groups/{group_id}/members.json", {"usernames": ",".join(adding)})
        for name in adding:
            print(f"   added      {name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("name", help='the private diary\'s name, e.g. "Schuster lab"')
    parser.add_argument("researchers", nargs="+", help="researchers to add, each with their agent")
    parser.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
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

    group = group_name_for(args.name)
    wanted = members_to_add(api, args.researchers)
    group_id = ensure_group(api, group, f"{DIARY_CATEGORY}: {args.name}")
    ensure_category(api, args.name, group)
    if group_id:
        ensure_members(api, group, group_id, wanted)
    return 0


if __name__ == "__main__":
    sys.exit(main())
