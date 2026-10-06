# 1. Requirements

This guide deploys Discourse in its supported Docker configuration, with automatic TLS, an SMTP relay, object storage for uploads, and state on a separate block device. Where a requirement can be met several ways, it documents the one that was deployed.

## Host

| Resource | Minimum | Deployed | Why |
|---|---|---|---|
| RAM | 4 GB | 15 GB | Postgres buffers, one Unicorn worker per half-core, the embedding server |
| CPU | 2 cores | 4 cores | Unicorn workers plus the embedding server |
| Root filesystem | 40 GB | 60 GB | Docker images and layers, which `volumes:` cannot move |
| Data volume | 100 GB | 100 GB | Database, backups, the model cache |
| OS | Debian-family LTS | Ubuntu 24.04 | `unattended-upgrades`, packaged Docker |
| Docker | 20.10 | 29.1 | `launcher` refuses anything older |

A smaller host runs Discourse but not Discourse plus the embedding model. Nothing auto-tunes a hand-written `app.yml`; `docs/03-install.md` has the sizing formula.

## Hostname and DNS

- A **subdomain** (for example `forum.example.com`) with an A record pointing at the host, resolving before you install. Discourse will not work on a bare IP. A subdomain is also what the relay authenticates, and the forum's hostname and mail identity must be the same name.
- Plain DNS, not a proxying CDN, so the HTTP-01 challenge reaches the host.
- Check the answer against the host's address, not merely that something resolves: a parked domain's wildcard record resolves too. `EXPECTED_IP` in `.env` makes the install check it.

## Object storage

S3-compatible object storage for uploads. Uploads on the local volume cannot be put behind the forum's login, because nginx serves image files off disk without asking Discourse. A store that addresses buckets only by path is fine; `docs/05-forum-config.md` 5.6 covers it.

## Email

- A transactional relay on port 587 with STARTTLS. Direct delivery from a fresh cloud address lands in spam.
- Authenticate the sending **subdomain** with the relay (SPF/DKIM as it asks) and publish DMARC at `p=none` until a test message is confirmed delivered.
- Take the daily quota from the provider. This forum's only regular mail is password resets and one activation per new member; `docs/05-forum-config.md` 5.1 turns everything else off, because a quota drained by notifications takes account recovery with it.
- Nothing validates SMTP during bootstrap. `docs/03-install.md` 3.6 makes a delivered test message a gate.

## Storage layout

```
/                 40 GB+   Docker images and layers, /var/discourse
/mnt/<data>      100 GB    postgres_data, backups, model cache
```

Set `volumes:` before the first bootstrap; moving data afterwards needs `rsync -a` to keep ownership, or Postgres refuses to start. A future major Postgres upgrade needs two to three times the database size free on the data volume.

## Network

| Port | Direction | Purpose |
|---|---|---|
| 22 | inbound | Administration |
| 80 | inbound | HTTP-01 challenge, redirect to HTTPS |
| 443 | inbound | The forum |
| 587 | outbound | SMTP relay |

Docker writes its own iptables chain and bypasses `ufw`, so the platform's ingress control (security group, cloud firewall) is the real one for published ports.

## Not covered

Not deployed here, so not documented: a host-level reverse proxy for a second site on 443 ([Discourse howto](https://meta.discourse.org/t/run-other-websites-on-the-same-machine-as-discourse/17247)), multisite, managed Postgres or Redis, and a second host.
