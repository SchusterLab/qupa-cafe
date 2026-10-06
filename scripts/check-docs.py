#!/usr/bin/env python3
"""Enforce the two documentation rules from CLAUDE.md.

1. ``docs/`` names no vendor. Anything host-specific belongs in ``platforms/``.
2. Markdown prose is one paragraph per line, never hard-wrapped.

Rule 2 is decidable: a paragraph is wrapped exactly when a prose line is
followed by another prose line that does not begin a block of its own. So this
reports the continuation line rather than guessing at sentences, and ``--fix``
joins it back.

Getting the exemptions right matters more than the pattern does. A checker
that cries wolf gets switched off, and then the rule is not enforced at all. So
this skips code blocks, tables, headings, link definitions and YAML
frontmatter, and it treats consecutive list items and consecutive blockquote
lines as deliberate rather than as wrapping.

Usage::

    python3 scripts/check-docs.py
    python3 scripts/check-docs.py --fix    # join wrapped paragraphs in place

Exits non-zero if either rule is violated.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vendors. A hit anywhere in docs/ is a documentation bug.
VENDOR_TERMS = ["jetstream", "openstack", "access-ci", "brevo", "porkbun", "exosphere", "slack"]

# "SU" is checked separately because it collides with ordinary words.
SU_PATTERN = re.compile(r"\bSUs?\b")

# A sentence end, then whitespace, then something that starts a new sentence.
THEMATIC_BREAK = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")

# Stripped before checking, because none of these are sentence ends.
LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.|\d+\))\s+")
TABLE_ROW = re.compile(r"^\s*\|")
LINK_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:")
HEADING = re.compile(r"^\s*#{1,6}\s")
BLOCKQUOTE_MARKER = re.compile(r"^\s*>\s?")


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in REPO_ROOT.rglob("*.md")
        if ".git" not in path.parts and "node_modules" not in path.parts
    )


def check_vendor_terms() -> list[str]:
    problems = []
    docs = REPO_ROOT / "docs"

    for path in sorted(docs.rglob("*.md")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            for term in VENDOR_TERMS:
                if term in lowered:
                    problems.append(f"{path.relative_to(REPO_ROOT)}:{number}: vendor term {term!r}: {line.strip()}")
            if SU_PATTERN.search(line):
                problems.append(f"{path.relative_to(REPO_ROOT)}:{number}: 'SU' is a platform billing unit: {line.strip()}")

    return problems


def _block_start(line: str) -> bool:
    """Does this line begin a markdown block rather than continue a paragraph?"""
    return bool(
        HEADING.match(line)
        or TABLE_ROW.match(line)
        or LINK_DEFINITION.match(line)
        or LIST_MARKER.match(line)
        or BLOCKQUOTE_MARKER.match(line)
        or THEMATIC_BREAK.match(line)
    )


def wrapped_lines(text: str):
    """Yield (line number, line) for each line that continues a wrapped paragraph.

    A list item and a blockquote line are prose that can itself be wrapped, so
    they set the flag. But a line that begins its own block never counts as a
    continuation, which is what keeps consecutive list items and the rows of a
    quoted template from being reported.
    """
    in_code = False
    in_frontmatter = False
    previous_was_prose = False

    for number, line in enumerate(text.splitlines(), 1):
        if number == 1 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() in ("---", "..."):
                in_frontmatter = False
            continue

        if line.lstrip().startswith("```"):
            in_code = not in_code
            previous_was_prose = False
            continue
        if in_code:
            continue

        if not line.strip():
            previous_was_prose = False
            continue

        # Indented code blocks.
        if line.startswith("    ") and not LIST_MARKER.match(line):
            previous_was_prose = False
            continue

        if _block_start(line):
            previous_was_prose = bool(LIST_MARKER.match(line) or BLOCKQUOTE_MARKER.match(line))
            continue

        if previous_was_prose:
            yield number, line
        previous_was_prose = True


def rewrap(text: str) -> str:
    """Join every wrapped paragraph onto one line, leaving blocks alone."""
    wrapped = {number for number, _ in wrapped_lines(text)}
    if not wrapped:
        return text

    out: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        if number in wrapped and out:
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)

    joined = "\n".join(out)
    return joined + "\n" if text.endswith("\n") else joined


# Markdown that is not prose for a reader, and whose line breaks are part of
# the artefact rather than a habit of whoever typed it.
#
# The wake prompt is sent to a model verbatim. Nothing renders it, so the rule
# this check enforces -- that a single newline becomes a line break in a
# Discourse post -- does not apply to it, and joining its paragraphs would
# change the text an agent is handed for no reader's benefit.
NOT_PROSE = {"content/wake-prompt.md"}


def check_paragraphs(fix: bool) -> list[str]:
    problems = []

    for path in markdown_files():
        if path.relative_to(REPO_ROOT).as_posix() in NOT_PROSE:
            continue
        text = path.read_text(encoding="utf-8")
        found = list(wrapped_lines(text))
        if not found:
            continue

        if fix:
            path.write_text(rewrap(text), encoding="utf-8", newline="\n")
            print(f"  joined {len(found)} line(s) in {path.relative_to(REPO_ROOT)}")
            continue

        for number, line in found:
            problems.append(
                f"{path.relative_to(REPO_ROOT)}:{number}: paragraph wrapped across lines: {line.strip()[:60]}"
            )

    return problems


# Markdown links, and the backticked paths the prose uses instead of links.
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
PLACEHOLDER = re.compile(r"%[A-Z_]+%")
BACKTICKED_PATH = re.compile(r"`([a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)+)`")

# Top-level directories of this repository, used to tell our own paths apart
# from the many upstream ones the prose mentions.
OUR_DIRECTORIES = {
    "docs", "scripts", "server", "agent-kit", "platforms", "config", "content",
    "providers", "tests",
}


def check_references() -> list[str]:
    """Every path this repository points at must exist.

    Both forms are checked. Markdown links are the obvious ones; the backticked
    paths matter more in practice, because the prose refers to files that way
    constantly and nothing renders them, so a rename leaves them silently wrong.
    """
    problems = []

    for path in markdown_files():
        relative = path.relative_to(REPO_ROOT)
        # History names files that have since gone.
        if relative.name == "CHANGELOG.md":
            continue

        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A `%PLACEHOLDER%` target in content/ is a URL filled in when the
            # text is sent, not a path.
            targets = [
                target for target in MARKDOWN_LINK.findall(line)
                if not target.startswith(("http://", "https://", "#", "mailto:"))
                and not PLACEHOLDER.fullmatch(target)
            ]
            # A whitelist, not a blacklist. The prose is full of backticked
            # paths that are not ours -- `containers/app.yml` inside the
            # Discourse install, `templates/web.ssl.template.yml` upstream,
            # `BAAI/bge-small-en-v1.5` on HuggingFace -- and enumerating those
            # is a losing game. Checking only paths that start with a directory
            # this repository actually has is both simpler and stricter.
            targets += [
                target for target in BACKTICKED_PATH.findall(line)
                if target.split("/")[0] in OUR_DIRECTORIES
            ]

            for target in targets:
                clean = target.split("#")[0].rstrip("/")
                if not clean:
                    continue
                if (path.parent / clean).exists() or (REPO_ROOT / clean).exists():
                    continue
                problems.append(f"{relative}:{number}: no such path: {target}")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix", action="store_true", help="join wrapped paragraphs in place")
    args = parser.parse_args()

    failed = False

    print("== vendor terms in docs/ ==")
    vendor_problems = check_vendor_terms()
    if vendor_problems:
        for problem in vendor_problems:
            print(f"  {problem}")
        print("  FAIL: move the above to platforms/.")
        failed = True
    else:
        print("  ok")

    print("\n== references ==")
    reference_problems = check_references()
    if reference_problems:
        for problem in reference_problems:
            print(f"  {problem}")
        print("  FAIL: the paths above do not exist.")
        failed = True
    else:
        print("  ok")

    print("\n== one paragraph per line ==")
    wrap_problems = check_paragraphs(args.fix)
    if wrap_problems:
        for problem in wrap_problems:
            print(f"  {problem}")
        print(f"  FAIL: {len(wrap_problems)} wrapped line(s). Run with --fix. See CLAUDE.md.")
        failed = True
    else:
        print("  ok")

    print()
    if failed:
        print("Documentation checks failed.")
        return 1

    print("All documentation checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
