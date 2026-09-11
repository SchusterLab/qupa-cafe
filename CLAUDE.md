# Repo conventions

## Semantic line breaks

Every tracked Markdown file uses semantic line breaks.
A line break after every sentence, two after every paragraph, and no soft wrapping.

This keeps diffs meaningful: editing one sentence touches one line.
A reviewer reading `git diff` sees the sentence that changed rather than a reflowed paragraph.

The rule applies to prose.
Tables, code blocks, and link reference definitions are exempt, since breaking those changes their meaning.

## The guide is provider-agnostic

`docs/` is the deployment recipe and it names no vendor.
It is written against requirements — a Linux host with enough RAM, a resolvable hostname, an SMTP relay, a block device — never against product names.

Everything specific to where we happen to host this lives under `platforms/`.
A reader deploying to a VPS or a departmental server should be able to ignore that directory entirely.

The check is mechanical.
`python3 scripts/check-docs.py` greps `docs/` for vendor terms, and any hit is a documentation bug.
It also checks the line-break rule below, so it is the one command to run before committing documentation.

## Document only what we deploy and test

Every command in `docs/` has been run, on the scratch forum or on the real instance.
Untested alternatives do not go in, however plausible they look.

A recipe whose steps have never been run is worse than no recipe.
It fails at an unpredictable point and the reader cannot tell whether they made the mistake.

Where a real alternative exists that we did not take, name it in one line and link to upstream documentation.
Do not write a procedure for it.

Each document carries a `> **Status:**` line stating whether its procedure has been executed and where.
That line is not decoration: it is the reader's warning that a section is still unverified.

## Docs and server cannot drift

Any change to server configuration updates the corresponding document in the same commit.
`CHANGELOG.md` records every server-affecting change, dated.

If a commit changes what the server does and touches no document, that is a bug in the commit.

## Naming

The project is **Qupa Cafe**.
`qupa-cafe` in identifiers, hostnames, and file names; `qupa_cafe` for the Python package.
