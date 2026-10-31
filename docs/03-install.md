# 3. Installing Discourse

Installs Discourse from the official Docker distribution with a hand-written `app.yml`. The setup wizard is not used, because a configuration produced by clicking is not in version control.

## 3.1 Configure

```bash
cp .env.example .env          # then fill it in; it is gitignored
sudo ./scripts/30-install-discourse.sh --render-only
```

The script checks that `DISCOURSE_HOSTNAME` resolves, and that it resolves to `EXPECTED_IP` if that is set. It clones `discourse_docker` into `/var/discourse` and makes the directory mode 700. It renders `config/app.yml.example` into `/var/discourse/containers/app.yml` with mode 600, since that file holds the SMTP password. It also copies the logo to the data volume for 5.6. `--render-only` stops there.

## 3.2 What the template sets

In `config/app.yml.example`:

- **Sizing is manual.** Nothing tunes a hand-written `app.yml`. Left unset, Discourse runs on 256 MB of shared buffers and 3 workers. Use `db_shared_buffers` = 256 MB × GB of RAM (at most 4096 MB) and `UNICORN_WORKERS` = 2 × cores (at most 8). The file carries the values for 4 cores and 15 GB of RAM.
- `web.ssl` must come before `web.letsencrypt.ssl`. Port 80 must be exposed for the certificate challenge.
- `volumes:` puts `/shared` on `DISCOURSE_DATA_DIR`. Docker's images stay on the root filesystem.
- The SMTP password is quoted, because an unquoted `#` starts a YAML comment and silently cuts the password short.
- Do not set `LETSENCRYPT_ACCOUNT_EMAIL`. The certificate request uses the address in `DISCOURSE_DEVELOPER_EMAILS`.
- `DISCOURSE_HOSTNAME` must match the DNS record exactly.

`/var/discourse` is root-only, so call `launcher` by absolute path: `sudo /var/discourse/launcher <command> app`.

## 3.3 Rehearse locally (optional)

On a scratch machine, `sudo ./scripts/30-install-discourse.sh --local` drops TLS and port 443, skips the DNS check, and replaces the SMTP credentials. It shows that `app.yml` parses and that the plugins activate. It does not test certificates or mail.

## 3.4 Bootstrap

```bash
sudo ./scripts/30-install-discourse.sh
```

This runs `launcher rebuild app`, which builds, migrates, requests the certificate and starts the forum. The output prints every environment variable, including the SMTP password. If you keep the output, write it to a file created with mode 600.

## 3.5 If the certificate is refused

> **Let's Encrypt allows five certificates a week for the same set of names.** Each rebuild of a forum on a fresh data volume requests a new certificate. When the request is refused, the bootstrap still succeeds but leaves empty certificate files, and nginx in the container stays down with `PEM_read_bio_X509_AUX() failed`. To recover, wait until the time given in `shared/standalone/letsencrypt/acme.sh.log`. Then delete `shared/standalone/ssl/*` and the `letsencrypt/<hostname>*` directories, and rebuild.

## 3.6 The gate before you call it done

```bash
curl -sSI https://forum.example.com | head -1
echo | openssl s_client -connect forum.example.com:443 -servername forum.example.com 2>/dev/null | openssl x509 -noout -issuer -dates
```

1. HTTPS serves a valid certificate.
2. Register an account with the address in `DISCOURSE_DEVELOPER_EMAILS`, which makes it an admin. Do it now, because once you turn on invite-only, following `docs/05-forum-config.md`, nobody can register.
3. In Admin → Email, send a test message to a major mailbox provider. In its headers, confirm `dmarc=pass` and a DKIM `d=` that matches the `From:` domain. `spf=none` is fine.

If mail fails, make the admin account without it, then fix the relay:

```bash
sudo /var/discourse/launcher enter app
rake admin:create          # not admin:invite, which queues mail
```

## 3.7 Where the files live

Under `$DISCOURSE_DATA_DIR/shared/standalone/`: `postgres_data`, `backups/default`, `letsencrypt`, `ssl`, `log/rails/production.log`, `log/var-log`.
