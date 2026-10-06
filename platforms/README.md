# Platforms

The host-specific parts, one directory per platform. If you deploy elsewhere, ignore this directory.

A platform directory ends where `docs/02-host-setup.md` begins, with:

| What | Why |
|---|---|
| A Debian-family host, 4 GB RAM or more | Runs the container |
| A root filesystem with room for Docker images | Images cannot be relocated |
| A block device for persistent state | `volumes:` in `app.yml` |
| A public IP address, inbound TCP 22, 80, 443 | Administration, the certificate challenge, the forum |
| Outbound TCP 587 | The SMTP relay |
| SSH as a sudo-capable user | `docs/02-host-setup.md` |
| `DISCOURSE_HOSTNAME`, resolving to the host on plain DNS | The one value the guide takes from a platform |

`scripts/check-docs.py` checks that `docs/` names no vendor.

## Available platforms

- [`jetstream2/`](jetstream2/), an academic OpenStack cloud, which this project runs on.

A new platform directory should hold a `README.md`, a provisioning walkthrough of the commands actually run, and an idempotent script under `scripts/`.
