# Repo conventions

## One paragraph per line

Every tracked Markdown file puts each paragraph on a single line. No hard wrapping, a blank line between paragraphs, and no line breaks inside a paragraph.

This is not a preference about editors. Markdown this repository publishes to the forum is rendered by Discourse, where `traditional_markdown_linebreaks` is false and a single newline becomes a `<br>`. Hard-wrapped prose therefore renders as a column of ragged short lines, in the post and in the HTML part of every mail built from it, while the markdown itself looks perfectly fine. The rule is the same everywhere because two opposite conventions in one repository is how content ends up written in the wrong one.

What it costs is diff granularity: rewording a sentence now shows as a changed paragraph rather than a changed line. That is the trade, and it is worth it for text whose line breaks are visible to readers.

Tables, code blocks, headings, link reference definitions and YAML frontmatter are exempt, since breaking those changes their meaning. Consecutive list items and consecutive blockquote lines are deliberate rather than wrapping, and are left alone.

`python3 scripts/check-docs.py` reports every wrapped line and `--fix` joins them.

## The guide is provider-agnostic

`docs/` is the deployment recipe and it names no vendor. It is written against requirements — a Linux host with enough RAM, a resolvable hostname, an SMTP relay, a block device — never against product names.

Everything specific to where we happen to host this lives under `platforms/`. A reader deploying to a VPS or a departmental server should be able to ignore that directory entirely.

The check is mechanical. `python3 scripts/check-docs.py` greps `docs/` for vendor terms, and any hit is a documentation bug. It also checks the paragraph rule above, so it is the one command to run before committing documentation.

## Document only what we deploy and test

Every command in `docs/` has been run, on the scratch forum or on the real instance. Untested alternatives do not go in, however plausible they look.

A recipe whose steps have never been run is worse than no recipe. It fails at an unpredictable point and the reader cannot tell whether they made the mistake.

Where a real alternative exists that we did not take, name it in one line and link to upstream documentation. Do not write a procedure for it.

A document carries a `> **Status:**` line only while something in it is unverified, and the line says what. Once every procedure in a document has been executed, the line goes. A status note on a fully tested document is decoration, and decoration teaches the reader to skip the notes that matter.

## Docs and server cannot drift

Any change to server configuration updates the corresponding document in the same commit. `CHANGELOG.md` records every server-affecting change, dated.

If a commit changes what the server does and touches no document, that is a bug in the commit.

## Naming

The project is **Qupa Cafe**. `qupa-cafe` in identifiers, hostnames, and file names; `qupa_cafe` for the Python package.
