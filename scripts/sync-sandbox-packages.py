#!/usr/bin/env python3
"""Bring the sandbox's Python packages up to the newest scipy-notebook build.

    python3 scripts/sync-sandbox-packages.py [--wiki DIR] [--no-lock]

The sandbox's manifest, agent-kit/qupa_cafe/sandbox_files/pixi.toml, holds the
packages that quay.io/jupyter/scipy-notebook installs with mamba, at the
versions of one of its builds. This finds the newest x86_64 build in the build
manifests Jupyter Docker Stacks publish in their wiki, takes the packages its
Dockerfiles name and the versions its `mamba list` gives them, rewrites the
manifest's pins, adds what the image added and drops what it dropped, and then
locks the manifest again with `pixi lock`. It is run before each release of
the agent kit (agent-kit/RELEASING.md), and qupa-install brings an installed
sandbox up to the manifest it ships.

--wiki reads a clone of https://github.com/jupyter/docker-stacks.wiki.git
already on disk; otherwise a shallow one is made in a temporary directory.
--no-lock rewrites the manifest and leaves pixi.lock alone.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "agent-kit" / "qupa_cafe" / "sandbox_files" / "pixi.toml"
WIKI = "https://github.com/jupyter/docker-stacks.wiki.git"
DOCKERFILE = "https://raw.githubusercontent.com/jupyter/docker-stacks/{commit}/images/{image}/Dockerfile"

# The images whose own mamba installs make up scipy-notebook, each with the
# comment that opens its section of the manifest.
IMAGES = (("docker-stacks-foundation", None), ("base-notebook", "# base-notebook"),
          ("scipy-notebook", "# scipy-notebook"))
# Named by the foundation image and kept out of the manifest: pixi manages the
# environment, and a conda in it would invite the agent to install around it.
LEFT_OUT = {"conda", "mamba"}
# Pinned by the manifest in a form of its own, not with "==".
OWN_FORM = {"python", "blas"}

BUILD_ROW = re.compile(r"quay\.io/jupyter/scipy-notebook:x86_64-([0-9a-f]{12})`")
PACKAGE_LINE = re.compile(r"^  (\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
PIN = re.compile(r'^([A-Za-z0-9_.-]+) = "==([^"]+)"$')


def newest_build(wiki: Path) -> tuple[str, Path]:
    """The newest x86_64 scipy-notebook build's commit and its manifest."""
    for month in sorted((wiki / "monthly-files").glob("*/*.md"), reverse=True):
        found = BUILD_ROW.search(month.read_text(encoding="utf-8"))
        if found:
            commit = found.group(1)
            manifest = wiki / "manifests" / month.parent.name / month.stem / f"x86_64-default-scipy-notebook-{commit}.md"
            if manifest.exists():
                return commit, manifest
    sys.exit("no x86_64 scipy-notebook build manifest found in the wiki")


def build_info(manifest: str) -> tuple[str, str, dict[str, str]]:
    """The build's full commit, its date and each package's version from its
    `mamba list`."""
    commit = re.search(r"Git commit SHA: \[([0-9a-f]{40})\]", manifest)
    when = re.search(r"Build timestamp: (\d{4}-\d{2}-\d{2})", manifest)
    if not (commit and when):
        sys.exit("the build manifest gives no commit or timestamp")
    listing = manifest.split("`mamba list`:", 1)[1].split("```", 2)[1]
    versions = {m.group(1): m.group(2) for line in listing.splitlines() if (m := PACKAGE_LINE.match(line))}
    return commit.group(1), when.group(1), versions


def named(dockerfile: str) -> list[str]:
    """The packages a Dockerfile's mamba installs name, as names alone."""
    names = []
    for block in re.findall(r"mamba install(.*?)&&", dockerfile, re.S):
        for line in block.splitlines():
            line = line.split("#", 1)[0]
            for spec in QUOTED.findall(line):
                if spec.startswith("$") or spec.startswith("--"):
                    continue
                spec = spec.split("::")[-1]
                names.append(re.split(r"[=<>!~ ]", spec, maxsplit=1)[0])
    return names


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read().decode("utf-8")


def rewrite(text: str, commit: str, when: str, versions: dict[str, str],
            wanted: dict[str, list[str]]) -> tuple[str, list[str]]:
    """The manifest with every pin at the build's version, each image's
    section holding the packages that image names, and the header naming the
    build; and what changed, for the maintainer to read."""
    changes: list[str] = []
    out: list[str] = []
    section: str | None = None
    seen: set[str] = set()

    def close(name: str) -> None:
        for package in wanted[name]:
            if package not in seen and package not in OWN_FORM:
                out.append(f'{package} = "=={versions[package]}"')
                changes.append(f"added {package} {versions[package]}, which the image now installs")

    for line in text.splitlines():
        if section and (line in wanted or not line.strip() or line.startswith(("#", "["))):
            close(section)
            section = None
        if line in wanted:
            section = line
            out.append(line)
            continue
        if found := re.match(r'^python = "([^"]+)"$', line):
            new = versions["python"] + ".*"
            if new != found.group(1):
                changes.append(f"python {found.group(1)} -> {new}")
            line = f'python = "{new}"'
        elif found := PIN.match(line):
            name, old = found.groups()
            seen.add(name)
            if section and name not in wanted[section]:
                changes.append(f"dropped {name} {old}, which the image no longer installs")
                continue
            new = versions.get(name)
            if new is None:
                sys.exit(f"{name} is in the manifest but not in the build's mamba list")
            if new != old:
                changes.append(f"{name} {old} -> {new}")
            line = f'{name} = "=={new}"'
        out.append(line)
    if section:
        close(section)
    text = re.sub(r"build [0-9a-f]{12} \(\d{4}-\d{2}-\d{2}\)", f"build {commit[:12]} ({when})", "\n".join(out))
    return text + "\n", changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wiki", type=Path, help="a clone of the docker-stacks wiki")
    parser.add_argument("--no-lock", action="store_true", help="leave pixi.lock as it is")
    args = parser.parse_args()

    scratch = None
    wiki = args.wiki
    if wiki is None:
        scratch = Path(tempfile.mkdtemp(prefix="docker-stacks-wiki-"))
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", WIKI, str(scratch)], check=True)
        wiki = scratch
    try:
        _, manifest_path = newest_build(wiki)
        commit, when, versions = build_info(manifest_path.read_text(encoding="utf-8"))
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)

    wanted: dict[str, list[str]] = {}
    for image, section in IMAGES:
        names = [n for n in named(fetch(DOCKERFILE.format(commit=commit, image=image))) if n not in LEFT_OUT]
        if section is None:
            # The foundation's own packages, but python, go in base-notebook's section.
            wanted.setdefault("# base-notebook", []).extend(n for n in names if n != "python")
        else:
            wanted.setdefault(section, []).extend(names)

    text, changes = rewrite(MANIFEST.read_text(encoding="utf-8"), commit, when, versions, wanted)
    MANIFEST.write_text(text, encoding="utf-8", newline="\n")
    print(f"scipy-notebook build {commit[:12]} of {when}")
    for change in changes or ["no pin changed"]:
        print(f"  {change}")
    if not args.no_lock:
        pixi = shutil.which("pixi")
        if pixi is None:
            sys.exit("pixi is not on PATH, so pixi.lock is not updated; run `pixi lock` on the manifest")
        subprocess.run([pixi, "lock", "--manifest-path", str(MANIFEST)], check=True)
        print("  pixi.lock updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
