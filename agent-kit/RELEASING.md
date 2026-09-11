# Releasing the agent kit

> **Status:** Everything on both sides is in place and nothing has been published.
> The pending publisher is registered on PyPI, the workflow is on the default branch, both environments exist with the `pypi` one restricted to `v*` tags, the package is MIT-licensed, and both guards pass against a real build.
> What remains is a version tag and a release, which has not been attempted.

Maintainer-facing.
A researcher installing the kit needs none of this.

Publishing uses a **trusted publisher** rather than an API token.
The workflow exchanges a short-lived OIDC token from the forge for an upload token from the index, so there is no long-lived secret in this repository to leak, rotate, or forget.

`.github/workflows/publish-pypi.yml` is the whole of the repository's side.
It is named for its one job: it publishes this package to PyPI and its test instance, and nothing else.

## One-time registration, which has to be done by hand

This cannot be scripted from here: it needs a logged-in session on the index.

On PyPI, under the account's **Publishing** settings, add a new **pending publisher** with exactly these values:

| Field | Value |
|---|---|
| PyPI Project Name | `qupa-cafe` |
| Owner | `SchusterLab` |
| Repository name | `qupa-cafe` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

Then repeat it on TestPyPI, which is a separate account and a separate registration, with the environment name `testpypi`.

A registered pending publisher does **not** create a project page, so the name still answers 404 until the first upload.
That is worth knowing because `server/ranking.py` asks that same endpoint what version is published: until the first release it gets a 404, reads it as "no opinion", and judges nobody stale.

Three things about that form are worth knowing before filling it in.

**It is called a *pending* publisher because the project does not exist yet.**
Registering one **reserves the name** `qupa-cafe` on the index for this repository.
That is useful — it holds the name before the first release rather than after — and it is undone by deleting the pending publisher.

**The workflow filename is part of the trust relationship.**
Renaming `publish-pypi.yml` breaks publishing until the publisher is updated to match, and the failure is an authorisation error rather than anything that mentions the rename.

**The environment name is checked too.**
The two publish jobs declare `environment: pypi` and `environment: testpypi`, and those environments have to exist in the repository settings.
Create them there; an environment that is only named in a workflow is not an environment.

## What guards the `pypi` environment

An upload cannot be undone: a version can be yanked but never replaced, and the name and version are spent either way.
So the last step wants something between an accident and a permanent public artefact.

**A required reviewer is the obvious answer and is not available here.**
Environment protection rules of that kind need a billing plan that covers them for a **private** repository, and this repository is private.
The API refuses it outright:

```
Failed to create the environment protection rule.
Please ensure the billing plan supports the required reviewers protection rule.
```

Two ways to get it, if it is wanted: make the repository public, which makes those rules free, or move to a plan that includes them.
Making it public is on the cards anyway, since the deployment recipe is a promised deliverable of the allocation, and the licence that was blocking it is now settled.

**What is in place instead, and it costs nothing.**
The `pypi` environment carries a deployment branch policy admitting **only `v*` tags**:

| Environment | May deploy from | Protection |
|---|---|---|
| `pypi` | `v*` tags only | branch policy |
| `testpypi` | anything | none |

So publishing to the real index cannot happen from `main`, from a feature branch, or from an accidental dispatch.
It can only happen from a tag that was deliberately created for a version, which is itself a human act.

The asymmetry is intended: rehearsals on TestPyPI should be cheap and runnable from anywhere, and the irreversible one should not.

One consequence to know.
Running the workflow by hand against `pypi` requires selecting a `v*` tag as the ref.
Dispatching it from `main` is blocked by the environment rather than by the workflow, so it appears as a skipped deployment rather than as a failed step.

## When the upload is refused

The error to expect from a missing or mismatched registration is this, and it is worth reading carefully because the first clause sounds like the second is impossible:

```
Trusted publishing exchange failure:
* `invalid-publisher`: valid token, but no corresponding publisher
```

"Valid token" means the forge authenticated correctly and the problem is entirely on the index side: no publisher there matches the claims presented.
It is not a credential to fix, and there is no secret to rotate.

The action then prints the claims it sent, which is what to compare against the registration:

| Claim | What it must match |
|---|---|
| `repository` | Owner and repository name |
| `workflow_ref` | The workflow filename |
| `environment` | The environment name |

Observed once, on the first TestPyPI rehearsal: every claim was correct and the registration simply did not exist, because **PyPI and TestPyPI are separate accounts** and only the former had been done.
Registering the second one is the whole fix.

## The release itself

1. Bump `__version__` in `agent-kit/qupa_cafe/__init__.py`.
   It is the only place the version lives; `pyproject.toml` reads it from there.
2. Update `CHANGELOG.md`.
3. Commit, and tag `v<version>`.
4. Create a release on that tag.

Publishing follows the release, so the tag, the notes and the upload are one act rather than three.

For a rehearsal, run the workflow by hand and choose `testpypi`.
That is how you discover that the description renders badly without spending a version number on the real index.

## Two guards, and why they exist

Both run before anything is uploaded, and both were tested against the real build.

**A missing licence stops the publish.**
The workflow reads the built wheel's metadata and refuses if no licence is declared, or if it reads `TBD`.
It also refuses if a licence is declared but its text does not ship, because a package nobody can read the terms of is one nobody can comply with.
Neither can be corrected in place after an upload.

It reads **two** field names, and that detail earned itself.
A PEP 639 declaration produces `License-Expression` and **no** `License` field at all, so a guard reading only the older spelling rejects a correctly licensed build -- failing in the direction that blocks good work rather than bad.
This package declares `license = "MIT"`, so its metadata carries `License-Expression: MIT` and the guard passes on the first of the two.

**A tag that disagrees with the built version stops the publish.**
On a release, the workflow compares the tag against the version in the built distribution and refuses if they differ, accepting either `v0.1.0` or `0.1.0` as the tag.
Otherwise it is possible to tag `v0.2.0`, publish `0.1.0`, and have the index and the repository disagree permanently.

## What runs before the build

`make check` in all but name: the documentation rules, both test suites, and the syntax pass.

Nothing is published that would not pass on a laptop, and this is also the repository's only continuous integration — so a release is currently the only thing that runs the tests automatically.
That is worth changing independently of publishing.
