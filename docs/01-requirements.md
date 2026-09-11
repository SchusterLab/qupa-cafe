# 1. Requirements

> **Status:** Requirements confirmed against upstream documentation and source on 2026-09-10.
> The sizing formula and the hostname constraint are quoted from Discourse's own material; each is cited inline.

This guide deploys Discourse in its supported Docker configuration, with automatic TLS, a transactional email relay, and persistent state on a separate block device.

It is written against requirements rather than against a particular host.
Wherever a requirement can be met more than one way, the guide states the requirement and then documents the single way we actually used.

## Host

| Resource | Minimum | What we ran | Why |
|---|---|---|---|
| RAM | 4 GB | 15 GB | Postgres shared buffers plus one Unicorn worker per core; 4 GB is Discourse's own floor |
| CPU | 2 cores | 4 cores | Two Unicorn workers per core, plus the embedding container |
| Root filesystem | 40 GB | 60 GB | Docker images and layers; see [Storage](#storage) |
| Data volume | 100 GB | 100 GB | Database, uploads, and backups |
| OS | Debian-family, current LTS | Ubuntu 24.04 LTS | Docker is packaged and `unattended-upgrades` is available |
| Docker | 20.10 | 29.7 | `launcher` refuses to run below 20.10 and recommends 24.0.7 |
| Kernel | 4.4 | — | Enforced by `launcher` |

A smaller host will run Discourse.
It will not comfortably run Discourse *and* a local embedding model, which is what `docs/04-embeddings.md` adds.

### Sizing is not automatic

Discourse's setup wizard auto-tunes two Postgres and Unicorn values from the host's RAM and core count.
This guide writes `app.yml` by hand and therefore **gets no auto-tuning at all**.

Both values must be set explicitly or the install silently runs on 256 MB of shared buffers and 3 workers.
The formula and our values are in `docs/03-install.md`.

## Hostname

A **resolvable hostname is a hard prerequisite**.
Discourse's sample configuration states that it "will not work with a bare IP number" ([samples/standalone.yml](https://github.com/discourse/discourse_docker/blob/main/samples/standalone.yml)).

You need:

- A DNS A record pointing at the host's public address, resolving before you install.
- The record on plain DNS, not behind a proxying CDN, if you want the container to obtain its own certificates.
  The HTTP-01 challenge must reach the host.

Verify with `dig +short forum.example.com` and compare against the host's address.
Do this **before** installing, because the certificate request in `docs/03-install.md` will otherwise fail and leave a half-configured container.

### Use a subdomain

Put the forum on a subdomain rather than an apex domain.

Two reasons, both practical.
An apex domain cannot carry a CNAME, which constrains what you can do later.
And a transactional email provider will want you to verify the **subdomain you send from**, not the apex — Discourse's own documentation is explicit that verifying `example.com` while sending as `forum.example.com` leaves mail misconfigured.

## Email

Discourse needs an SMTP relay.
Without one, **password reset does not work**, which means account recovery does not work.

### Use a relay, not direct-to-MX

Send through a transactional relay on port 587 rather than delivering directly.
A freshly allocated server address has no matching reverse DNS and sits in a range with poor sender reputation, so direct delivery lands in spam even when the host permits outbound port 25.

### Authenticate the sending domain

Publish SPF, DKIM, and DMARC for the sending domain, and verify the sending **subdomain** with the provider.

This matters more than it used to.
Major mailbox providers tightened enforcement against unauthenticated mail through 2025, and a password reset in a spam folder is indistinguishable from one that never arrived.

Start DMARC at `p=none` so you can observe before you enforce.

### Nothing validates that SMTP works

This is the sharpest trap in the whole install.

The only SMTP check during bootstrap is a literal string comparison against `smtp.example.com` in `web.template.yml`.
A subtly wrong relay configuration bootstraps "successfully" and then fails silently, forever, at the moment someone needs a password reset.

**Send a real test message and confirm it arrives.**
`docs/03-install.md` treats that as a gate on declaring the install finished, not as a follow-up task.

### Budget the quota

Transactional relays meter by message.
Decide up front what is allowed to send.

This guide configures Discourse so that **password resets are the only mail path**, with digests and notification mail off and invites delivered by link.
That is not only tidiness: on a small monthly quota, a digest setting switched on by accident drains the quota and takes account recovery down with it, silently.
`docs/05-forum-config.md` covers the settings and the check that nothing else sends.

> **What we used.** A free transactional tier with a **300 messages per month** allowance, over `smtp-relay.<provider>:587` with STARTTLS.
> Note that Discourse's `INSTALL-email.md` describes that tier as 300 per *day*; it is 300 per *month*, and that document disclaims its own pricing accuracy.
> Take quota figures from the provider, not from Discourse's documentation.

## Storage

Discourse's own state — the database, uploads, and backups — can be redirected to any path with the `volumes:` section of `app.yml`.
Docker's images and layers **cannot**, and they are the larger of the two early on.

So the layout is:

```
/                  40 GB+    Docker images and layers, /var/discourse
/mnt/<data>       100 GB     postgres_data, uploads, backups
```

Give the root filesystem room for Docker regardless of where the data goes.
A 20 GB root cannot hold Discourse's images and a rebuild's worth of layers, and retargeting `volumes:` does not help, because it moves the state and not the images.

Set `volumes:` **before the first bootstrap**.
Doing it then is a config edit.
Doing it afterwards means stopping the container and `rsync -a` to preserve ownership, and Postgres refuses to start if it gets that wrong.

Two consequences worth planning for rather than discovering:

- `./launcher cleanup` must be scheduled, or images accumulate without bound.
- A future major Postgres upgrade needs two to three times the database size free on whichever filesystem holds `postgres_data`.

## Network

| Port | Direction | Purpose |
|---|---|---|
| 22 | inbound | Administration |
| 80 | inbound | HTTP-01 certificate challenge, and the redirect to HTTPS |
| 443 | inbound | The forum |
| 587 | outbound | SMTP relay |

**A host firewall will not constrain Discourse's published ports.**
Docker writes its own iptables chain and bypasses `ufw`, so whatever ingress control your host platform provides — a security group, a cloud firewall — is the real one.
Configure it there and treat `ufw` as defence for anything *not* published by a container.

## What this guide does not cover

Named so you know they exist, not instructed, because we did not deploy them:

- **A host-level reverse proxy.** If you need port 443 for a second service on the same host, Discourse supports it through `web.socketed.template.yml`.
  See Discourse's [reverse proxy howto](https://meta.discourse.org/t/run-other-websites-on-the-same-machine-as-discourse/17247).
- **Multisite.** One forum per host here.
- **A managed Postgres or Redis.** Both run in the container.
- **Object storage for uploads.** Uploads are on the data volume.
