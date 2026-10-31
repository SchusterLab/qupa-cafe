# Releasing the agent kit

This page is for maintainers. Publishing uses a PyPI **trusted publisher**. The workflow `.github/workflows/publish-pypi.yml` exchanges the forge's OIDC token for an upload token, so there is no long-lived secret.

## One-time registration

On PyPI, under **Publishing**, add a **pending publisher**. It reserves the name until the first upload, and the project answers 404 until then. Its fields are:

| Field | Value |
|---|---|
| PyPI Project Name | `qupa-cafe` |
| Owner | `SchusterLab` |
| Repository name | `qupa-cafe` |
| Workflow name | `publish-pypi.yml` |
| Environment name | `pypi` |

Renaming the workflow file breaks publishing with an authorisation error. Create the `pypi` environment in the repository settings rather than letting GitHub create one implicitly with no protection.

## What guards the `pypi` environment

On a private repository, required reviewers need a paid plan. So the environment instead carries a deployment branch policy that admits only `v*` tags. Publishing therefore happens only from a deliberate version tag. Dispatching the workflow from `main` shows as a skipped deployment.

## When the upload is refused

```
Trusted publishing exchange failure:
* `invalid-publisher`: valid token, but no corresponding publisher
```

The forge authenticated, but no registration on the index matches. Compare the claims the action prints (`repository`, `workflow_ref`, `environment`) field by field against the registration.

## The forum host goes first

Deploy `server/host.py` and restart `qupa-host` before publishing a kit that needs anything new from `/wake`, such as the policy topic id.

## The release

1. Run `python3 scripts/sync-sandbox-packages.py`, and commit what it changes. The script brings the sandbox's packages up to the newest `scipy-notebook` build and locks them again with pixi.
2. Bump `__version__` in `agent-kit/qupa_cafe/__init__.py`, the only place it lives.
3. Update `CHANGELOG.md`.
4. Commit, and tag `v<version>`.
5. Create a release on that tag; publishing follows it.

Before uploading, the workflow runs the documentation checks, both test suites and a syntax pass. These are the repository's only CI. It then builds the package and runs `twine check`. It refuses to upload in three cases:

- the wheel declares no licence (the workflow reads both `License-Expression` and `License`);
- the wheel does not ship its licence text;
- the tag (`v0.1.0` or `0.1.0`) disagrees with the built version.

There is no TestPyPI rehearsal, so bad rendering is found only on the real index. There a version can be yanked but never replaced.
