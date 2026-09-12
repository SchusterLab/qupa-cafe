# Platforms

`docs/` is the deployment guide and it names no vendor. This directory holds the host-specific parts, one directory per platform, each presented as a worked example rather than as the way to do it.

If you are deploying somewhere else, you can ignore everything here.

## The contract

A platform directory has one job: get you to a host that satisfies `docs/01-requirements.md`, and then hand you off.

Concretely, it must produce:

| What | Why the guide needs it |
|---|---|
| A Debian-family Linux host, 4 GB RAM or more | Runs the container |
| Root filesystem with room for Docker images | Images and layers cannot be relocated |
| A block device for persistent state | `volumes:` in `app.yml` points at it |
| A public IP address | Reachable forum |
| Inbound TCP 22, 80, 443 | Administration, certificate challenge, the forum |
| Outbound TCP 587 | SMTP relay |
| SSH access as a sudo-capable user | Everything in `docs/02-host-setup.md` |

Plus one value, which is the only thing the generic guide takes from a platform:

```
DISCOURSE_HOSTNAME
```

A hostname that resolves publicly to the host's address, on plain DNS rather than behind a proxying CDN. Everything else in `docs/` is written against the host, not against how the host came to exist.

## The handoff point

A platform directory ends where `docs/02-host-setup.md` begins.

If a platform note is telling you how to mount a filesystem or install Docker, it has overreached. If `docs/` is telling you about a security group or a floating IP, it has leaked.

`scripts/check-docs.py` checks the second of those mechanically. The first needs a reader.

## Available platforms

- [`jetstream2/`](jetstream2/) — the host this project actually runs on. An academic OpenStack cloud.

## Adding one

A new platform directory should contain:

- `README.md` — what this platform is, and what is different about it.
- A provisioning walkthrough, with the commands that were actually run.
- An idempotent provisioning script under `scripts/`.
- The cost model, if the platform has one worth stating.

Write it from what happened, not from the platform's documentation. The gap between those two is the entire value of a platform note.
