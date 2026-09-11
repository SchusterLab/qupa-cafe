#!/usr/bin/env python3
"""Enforce the two documentation rules from CLAUDE.md.

1. ``docs/`` names no vendor. Anything host-specific belongs in ``platforms/``.
2. Markdown prose uses semantic line breaks: one sentence per line.

Rule 2 is checked heuristically, because "sentence" is not decidable. The check
looks for sentence-ending punctuation followed by a space and a capital, which
catches the common case of two sentences left on one line.

Getting the exemptions right matters more than the pattern does. A checker that
cries wolf on every heading and numbered list gets switched off, and then the
rule is not enforced at all. So this skips code blocks, tables, headings, link
definitions, and YAML frontmatter, and it strips list markers and bold labels
before looking at what remains.

Usage::

    python3 scripts/check-docs.py
    python3 scripts/check-docs.py --fix-hints    # print suggested split points

Exits non-zero if either rule is violated.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Vendors. A hit anywhere in docs/ is a documentation bug.
VENDOR_TERMS = ["jetstream", "openstack", "access-ci", "brevo", "porkbun", "exosphere"]

# "SU" is checked separately because it collides with ordinary words.
SU_PATTERN = re.compile(r"\bSUs?\b")

# A sentence end, then whitespace, then something that starts a new sentence.
SENTENCE_BREAK = re.compile(r"[.!?][\"')\]]?\s+[A-Z(\"'`]")

# Stripped before checking, because none of these are sentence ends.
LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+\.|\d+\))\s+")
BOLD_LABEL = re.compile(r"^\s*\*\*[^*]+\*\*\s*")
TABLE_ROW = re.compile(r"^\s*\|")
LINK_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:")
HEADING = re.compile(r"^\s*#{1,6}\s")
BLOCKQUOTE_MARKER = re.compile(r"^\s*>\s?")

# Abbreviations that end in a period mid-sentence. Without these, ordinary prose
# trips the check and the checker loses its credibility.
ABBREVIATIONS = ["e.g.", "i.e.", "etc.", "vs.", "cf.", "Dr.", "Mr.", "Ms.", "St.", "approx."]


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


def prose_lines(text: str):
    """Yield (line number, prose) for lines the line-break rule applies to."""
    in_code = False
    in_frontmatter = False

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
            continue
        if in_code:
            continue

        if not line.strip():
            continue
        if HEADING.match(line) or TABLE_ROW.match(line) or LINK_DEFINITION.match(line):
            continue
        # Indented code blocks.
        if line.startswith("    ") and not LIST_MARKER.match(line):
            continue

        prose = BLOCKQUOTE_MARKER.sub("", line)
        prose = LIST_MARKER.sub("", prose)
        prose = BOLD_LABEL.sub("", prose)

        # Inline code can hold anything, including sentence-shaped text.
        prose = re.sub(r"`[^`]*`", "CODE", prose)
        # Link targets are not prose.
        prose = re.sub(r"\]\([^)]*\)", "](LINK)", prose)

        for abbreviation in ABBREVIATIONS:
            prose = prose.replace(abbreviation, abbreviation.replace(".", "_"))

        yield number, prose


def check_line_breaks(show_hints: bool) -> list[str]:
    problems = []

    for path in markdown_files():
        for number, prose in prose_lines(path.read_text(encoding="utf-8")):
            match = SENTENCE_BREAK.search(prose)
            if not match:
                continue

            message = f"{path.relative_to(REPO_ROOT)}:{number}: more than one sentence on a line"
            if show_hints:
                message += f"\n    split after: ...{prose[max(0, match.start() - 40):match.start() + 1]}"
            problems.append(message)

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fix-hints", action="store_true", help="show where to split each long line")
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

    print("\n== semantic line breaks ==")
    break_problems = check_line_breaks(args.fix_hints)
    if break_problems:
        for problem in break_problems:
            print(f"  {problem}")
        print(f"  FAIL: {len(break_problems)} line(s) hold more than one sentence. See CLAUDE.md.")
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
