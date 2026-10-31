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

A smaller host can run Discourse, but not Discourse and the embedding model together. Nothing tunes a hand-written `app.yml` for the host, so size it yourself with the formula in `docs/03-install.md` 3.2.

## Hostname and DNS

- A **subdomain** (for example `forum.example.com`) with an A record that points at the host. It must resolve before you install. Discourse does not work on a bare IP address. The mail relay authenticates this same subdomain. The forum's hostname and its mail identity must be the same name.
- Plain DNS, not a proxying CDN, so that the certificate's HTTP-01 challenge reaches the host.
- Check that the name resolves to the host's address, not just that it resolves. A parked domain's wildcard record also resolves. If you set `EXPECTED_IP` in `.env`, the install script checks the address.

## Object storage

S3-compatible object storage for uploads. Uploads kept on the local volume cannot be put behind the forum's login, because nginx serves image files straight from disk without asking Discourse. A store that addresses buckets only by path also works (`docs/05-forum-config.md` 5.6).

## Email

- A transactional relay on port 587 with STARTTLS. Direct delivery from a fresh cloud address lands in spam.
- Authenticate the sending **subdomain** with the relay, using the SPF and DKIM records it asks for. Publish DMARC at `p=none` until a test message is confirmed delivered.
- Note the relay's daily quota. The only mail this forum sends regularly is password resets and one activation mail per new member. `docs/05-forum-config.md` 5.1 turns all other mail off, because if notifications use up the quota, password resets stop too.
- The bootstrap does not test SMTP. So `docs/03-install.md` 3.6 requires a delivered test message before the install counts as done.

## Storage layout

```
/                 40 GB+   Docker images and layers, /var/discourse
/mnt/<data>      100 GB    postgres_data, backups, model cache
```

Set `volumes:` before the first bootstrap. To move the data afterwards, copy it with `rsync -a` so that file ownership is kept. Postgres refuses to start on data with the wrong owner. A future major Postgres upgrade needs free space on the data volume of two to three times the database's size.

## Network

| Port | Direction | Purpose |
|---|---|---|
| 22 | inbound | Administration |
| 80 | inbound | HTTP-01 challenge, redirect to HTTPS |
| 443 | inbound | The forum |
| 587 | outbound | SMTP relay |

Docker writes its own iptables chain and bypasses `ufw`. For published ports, the firewall that counts is the platform's ingress control, such as a security group or cloud firewall.

## Not covered

Not deployed here, so not documented: a host-level reverse proxy for a second site on 443 ([Discourse howto](https://meta.discourse.org/t/run-other-websites-on-the-same-machine-as-discourse/17247)), multisite, managed Postgres or Redis, and a second host.
