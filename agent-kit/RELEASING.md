# Releasing the agent kit

> **Status:** The workflow is written and its guards are tested locally; nothing has been published.
> The trusted publisher on the index side has not been registered, and the licence is still undecided, which the workflow deliberately refuses to publish around.

Maintainer-facing.
A researcher installing the kit needs none of this.

Publishing uses a **trusted publisher** rather than an API token.
The workflow exchanges a short-lived OIDC token from the forge for an upload token from the index, so there is no long-lived secret in this repository to leak, rotate, or forget.

`.github/workflows/publish.yml` is the whole of the repository's side.

## One-time registration, which has to be done by hand

This cannot be scripted from here: it needs a logged-in session on the index.

On PyPI, under the account's **Publishing** settings, add a new **pending publisher** with exactly these values:

| Field | Value |
|---|---|
| PyPI Project Name | `qupa-cafe` |
| Owner | `SchusterLab` |
| Repository name | `qupa-cafe` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Then repeat it on TestPyPI, which is a separate account and a separate registration, with the environment name `testpypi`.

Three things about that form are worth knowing before filling it in.

**It is called a *pending* publisher because the project does not exist yet.**
Registering one **reserves the name** `qupa-cafe` on the index for this repository.
That is useful — it stops the name being taken while the licence is settled — and it is undone by deleting the pending publisher.

**The workflow filename is part of the trust relationship.**
Renaming `publish.yml` breaks publishing until the publisher is updated to match, and the failure is an authorisation error rather than anything that mentions the rename.

**The environment name is checked too.**
The two publish jobs declare `environment: pypi` and `environment: testpypi`, and those environments have to exist in the repository settings.
Create them there; an environment that is only named in a workflow is not an environment.

## Worth doing on the `pypi` environment

Add a **required reviewer**.

An upload cannot be undone: a version can be yanked but never replaced, and the name and version are spent either way.
A required reviewer turns the last step into something a human agrees to, which is the appropriate weight for an irreversible public action.

The workflow cannot enforce this — it can only name the environment, which is why it is written down here instead.

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
The workflow reads the built wheel's metadata and refuses if `License` is empty or `TBD`.
Publishing code with no licence grant means nobody may legally use it, which defeats the point of publishing, and it cannot be corrected in place afterwards.

This is not hypothetical: the package currently declares `License: TBD`, so the workflow will refuse until a licence is chosen and a `LICENSE` file added.

**A tag that disagrees with the built version stops the publish.**
On a release, the workflow compares the tag against the version in the built distribution and refuses if they differ, accepting either `v0.1.0` or `0.1.0` as the tag.
Otherwise it is possible to tag `v0.2.0`, publish `0.1.0`, and have the index and the repository disagree permanently.

## What runs before the build

`make check` in all but name: the documentation rules, both test suites, and the syntax pass.

Nothing is published that would not pass on a laptop, and this is also the repository's only continuous integration — so a release is currently the only thing that runs the tests automatically.
That is worth changing independently of publishing.
