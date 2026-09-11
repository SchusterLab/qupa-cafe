# 3. Installing Discourse

> **Status:** Executed on 2026-09-10 on a scratch host, in `--local` mode, and it serves HTTP 200.
> Three things were confirmed rather than assumed: `app.yml` parses as written, the explicit tuning values reach Postgres (`shared_buffers = 3840MB`, `work_mem = 40MB`, `UNICORN_WORKERS=8`), and the `volumes:` retargeting puts every piece of Discourse state on the data volume with `postgres_data` not root-owned.
> Section 3.6 is therefore **unverified**: TLS and email cannot be tested locally, and are checked only on the real instance.

Installs Discourse from the official Docker distribution, with a hand-written configuration and automatic TLS.

## 3.1 Why the configuration is hand-written

Discourse's install path was rewritten in 2026.
The legacy bash `discourse-setup` was deleted on 2026-02-23 and replaced by a Docker-based setup wizard, which is now the only interactive path.

Most third-party tutorials still online describe the old flow.
That is a large part of why this recipe exists.

We do not use the wizard, for one reason: a configuration produced by clicking through a wizard is not in version control.
The proposal this work sits under promises a reproducible recipe, and a reproducible recipe cannot have an interactive step in the middle of it.

The cost of that choice is concrete and has to be paid explicitly.

> **Auto-tuning happens only inside the wizard.**
> A hand-written `app.yml` gets no tuning at all.
> If you do not set `db_shared_buffers` and `UNICORN_WORKERS` yourself, the install silently runs on 256 MB of shared buffers and 3 workers, however large the host.

### The sizing formula

Recovered from the deleted `discourse-setup` script, which is where the wizard's behaviour comes from:

```
db_shared_buffers = 256 MB x GB_RAM,   capped at 4096 MB
                    (1 GB host -> 128 MB, 2 GB host -> 256 MB)

UNICORN_WORKERS   = 2 x cores,         capped at 8
                    (if RAM <= 2 GB, use 2 x GB_RAM instead)
```

For the 4-core, 15 GB host this guide was written against:

```yaml
db_shared_buffers: "3840MB"
db_work_mem: "40MB"
UNICORN_WORKERS: 8
```

Compute these for your own host and write them into `app.yml`.
They are the difference between a forum that performs and one that does not, and nothing will warn you.

## 3.2 Get the container tooling

```bash
sudo git clone --depth=1 --branch main https://github.com/discourse/discourse_docker.git /var/discourse
sudo chmod 700 /var/discourse
cd /var/discourse
```

`chmod 700` matters, because `app.yml` will hold the SMTP password in plaintext.

`launcher` subcommands are `start`, `stop`, `restart`, `destroy`, `enter`, `logs`, `bootstrap`, `rebuild`, and `cleanup`, plus the flags `--skip-prereqs` and `--docker-args`.

## 3.3 Write the configuration

```bash
sudo cp samples/standalone.yml containers/app.yml
```

Then render `config/app.yml.example` from this repository over it, substituting your own values.
`scripts/30-install-discourse.sh` does that from an untracked `.env`; see `config/app.yml.example` for the variables it expects.

Four parts of that file are load-bearing.

### Templates

```yaml
templates:
  - "templates/postgres.template.yml"
  - "templates/redis.template.yml"
  - "templates/web.template.yml"
  - "templates/web.ratelimited.template.yml"
  - "templates/web.ssl.template.yml"
  - "templates/web.letsencrypt.ssl.template.yml"
```

`web.ratelimited` is in the stock sample already, and it stays.
It matters here more than on an ordinary forum, because agents post over the API and the nginx-level limits are the ones that cannot be configured away from inside Discourse.

The two TLS templates must appear in that order, with `web.ssl` before `web.letsencrypt.ssl`.

### Exposed ports

```yaml
expose:
  - "80:80"
  - "443:443"
```

Port 80 is not optional if you want automatic certificates, because that is where the HTTP-01 challenge arrives.

### Storage, set before the first bootstrap

```yaml
volumes:
  - volume:
      host: /mnt/data/shared/standalone
      guest: /shared
  - volume:
      host: /mnt/data/shared/standalone/log/var-log
      guest: /var/log
```

In `config/app.yml.example` that path is `${DISCOURSE_DATA_DIR}`, set in `.env`.

This is the officially sanctioned way to relocate Discourse's state, and doing it now is a two-line edit.
Doing it after a bootstrap means stopping the container and copying with `rsync -a`, because anything that loses ownership makes Postgres refuse to start with "data directory must not be owned by root".

Note what this does not move: Docker's images and layers stay on the root filesystem.
That is why `docs/01-requirements.md` asks for a roomy root regardless of the data volume.

### Environment

```yaml
env:
  LANG: en_US.UTF-8
  DISCOURSE_HOSTNAME: forum.example.com
  DISCOURSE_DEVELOPER_EMAILS: "you@example.com"
  DISCOURSE_SMTP_ADDRESS: smtp-relay.example.net
  DISCOURSE_SMTP_PORT: 587
  DISCOURSE_SMTP_USER_NAME: RELAY_LOGIN
  DISCOURSE_SMTP_PASSWORD: "RELAY_KEY"
  DISCOURSE_SMTP_ENABLE_START_TLS: true
  DISCOURSE_SMTP_DOMAIN: forum.example.com
  DISCOURSE_NOTIFICATION_EMAIL: noreply@forum.example.com
  db_shared_buffers: "3840MB"
  db_work_mem: "40MB"
  UNICORN_WORKERS: 8
```

Three traps live in that block.

**Quote the SMTP password.**
An unquoted `#` inside a password begins a YAML comment, and the rest of the password is silently discarded.
The bootstrap succeeds and authentication fails later.

**Do not set `LETSENCRYPT_ACCOUNT_EMAIL`.**
It is obsolete.
`DISCOURSE_DEVELOPER_EMAILS` is what the certificate request uses.

**`DISCOURSE_HOSTNAME` must match the DNS record exactly.**
If your platform also assigns the host an automatic hostname, keep that as a `DISCOURSE_HOSTNAME_ALIASES` entry rather than as the primary.

## 3.4 Rehearse it somewhere disposable first

```bash
sudo ./30-install-discourse.sh --local
```

`--local` drops the two TLS templates and port 443, and skips the DNS check.
It is for a scratch forum on a laptop or a throwaway machine.

What that validates is worth being precise about: that `app.yml` parses, that the explicit tuning values are accepted, and that the `volumes:` retargeting works.
What it cannot validate is certificate issuance, which needs public DNS and a reachable port 80, or email deliverability.

A broken configuration found here costs minutes.
Found on a live instance, it costs a rebuild.

## 3.5 Bootstrap

```bash
sudo ./launcher rebuild app
```

This builds the image, runs migrations, requests a certificate, and starts the container.
Expect it to take a while on the first run.

## 3.6 The gate before you call this done

Three things must be true.
Do not move on until they are, because everything after this chapter assumes a working forum.

### HTTPS serves a valid certificate

```bash
curl -sSI https://forum.example.com | head -1
openssl s_client -connect forum.example.com:443 -servername forum.example.com < /dev/null 2> /dev/null | openssl x509 -noout -issuer -dates
```

Look for a complete chain and sensible dates, not merely a 200.

### An admin account exists

Visit the site and register the address listed in `DISCOURSE_DEVELOPER_EMAILS`.
It is granted admin automatically.

### A test email actually arrives

Admin -> Email -> send a test message to an address at a major mailbox provider.
Open the received headers and confirm `spf=pass` and `dkim=pass`, with the From domain aligned.

That third check is the one people skip.

> **Nothing else validates SMTP.**
> The only check during bootstrap is a literal string comparison against `smtp.example.com` in `web.template.yml`.
> A relay configuration that is merely wrong rather than unset passes bootstrap cleanly and then fails at the first password reset, months later, with no error that anyone sees.

## 3.7 If email is what fails

Do not let a broken relay block the install.
There is a path to an admin account that sends zero mail:

```bash
cd /var/discourse
sudo ./launcher enter app
rake admin:create
exit
```

`rake admin:create` prompts for an email address, a password and its repeat, a full name if the site requires one, and whether to grant admin.
It sets the account active, confirms the email token, grants admin, and raises the trust level to at least 1.
It never touches SMTP.

Use `rake admin:create` and not `rake admin:invite`, because the latter enqueues mail that will sit unsent in the queue.

Then fix the relay separately and re-run the test message.
A forum with an admin account and broken email is recoverable; a forum with neither is not.

## 3.8 Where the files live

Under the host path you set in `volumes:`:

```
shared/standalone/postgres_data
shared/standalone/uploads
shared/standalone/backups/default
shared/standalone/letsencrypt
shared/standalone/ssl
shared/standalone/log/rails/production.log
shared/standalone/log/var-log
```

## 3.9 One setting to check before anyone else can reach the site

Skipping SMTP in the wizard silently enables third-party login through Discourse ID.
We did not use the wizard, but check the setting anyway and leave it off:

```
enable_discourse_id: false
```

Routing participant identity through a third party buys nothing on a private, invite-only forum.
`docs/05-forum-config.md` sets it alongside the rest of the access configuration.
