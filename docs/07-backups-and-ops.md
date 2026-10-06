# 7. Backups and operations

> **Status:** The restore drill in 7.2 has been run on a scratch forum and not on the deployed instance.

What has to keep happening after the forum is running.

## 7.1 Backups

Discourse backs itself up (database dump plus uploads) to `shared/standalone/backups/default` on the data volume. Automatic backups are on after install, weekly; `40-configure-forum.py` makes them nightly with a week kept (`backup_frequency` 1, `maximum_backups` 7).

Take one now to prove the path, and confirm it landed on the data volume, which proves the `volumes:` retargeting took effect:

```bash
sudo docker exec app discourse backup
ls -lh /mnt/qupa-cafe/shared/standalone/backups/default/
```

These backups are on the same volume as the data, so copy them offsite on a schedule. `scripts/50-backup-check.sh` reports age and size and moves nothing.

## 7.2 The restore drill

Restore a real backup into a throwaway container, not the live one, and call it done when the copy serves the forum's content:

```bash
sudo /var/discourse/launcher enter app
discourse backup
discourse enable_restore
discourse restore <file>.tar.gz
```

`discourse restore` refuses until `enable_restore`, which resets itself afterwards. Accounts, API keys and the embedding table come back, so nothing needs re-embedding. The site goes read-only during the restore and returns by itself. Posts are rebaked in the background afterwards; `rake posts:rebake_uncooked_posts` hurries it.

## 7.3 Scheduled jobs

`server/qupa-cafe.cron` goes to `/etc/cron.d/qupa-cafe`:

```bash
sudo install -m 644 /opt/qupa-cafe/server/qupa-cafe.cron /etc/cron.d/qupa-cafe
```

It runs three jobs:

- Provisioning (`docs/05-forum-config.md` 5.3) every fifteen minutes, as `/usr/local/sbin/qupa-provision --all`, the backstop for a missed webhook. The wrapper reads the admin key from `/etc/qupa-cafe/provision.env` rather than the cron line, because `/etc/cron.d` is world-readable.
- `yes | ./launcher cleanup` weekly. Each rebuild leaves its image on the root filesystem, and `cleanup` asks for confirmation twice: run unattended without `yes` it reclaims nothing and exits 0. `docker system df` still shows the base image as reclaimable afterwards; `cleanup` keeps it on purpose.
- `scripts/50-backup-check.sh` daily, with `DISCOURSE_HOSTNAME` set, or its certificate-expiry check is skipped.

Testing a line by hand, put the redirect inside the root command: `sudo script >> /var/log/x` fails with "Permission denied" before the script runs.

The data volume fills with backups (bounded by retention) and uploads. A future major Postgres upgrade needs two to three times the database size free on the volume holding `postgres_data`.

## 7.4 Updating

```bash
sudo git -C /var/discourse pull
sudo install -m 600 /dev/null /var/log/qupa-rebuild-$(date +%Y%m%d).log
sudo bash -c '/var/discourse/launcher rebuild app >> /var/log/qupa-rebuild-$(date +%Y%m%d).log 2>&1'
```

Any change to `app.yml` needs a rebuild, not a restart. The rebuild's output carries every environment variable from `app.yml`, the SMTP password included, so it goes to a mode-600 file and not into a paste.

Keep `containers/app.yml` and `config/app.yml.example` in step, with a `CHANGELOG.md` entry, per `CLAUDE.md`.

## 7.5 Host updates

`unattended-upgrades` never reboots, so check `/var/run/reboot-required` (the backup check does) and reboot from inside the host. Afterwards confirm the data volume remounted and the forum came back with no manual step.

## 7.6 Monitoring

| Check | Why |
|---|---|
| HTTPS answers and the certificate is not near expiry | Renewal fails silently when the HTTP-01 path changes |
| The relay's usage counter | A climbing counter means something started sending (5.1) |
| `df -h` on both filesystems | Two independent ways to run out |
| Age of the newest backup | `scripts/50-backup-check.sh` |
| The embedding server responds | Without it, ranking degrades to mentions only |
| `systemctl --failed` is empty | The arXiv feed exits 1 on a refused preprint; `journalctl -u qupa-arxiv-feed` says which |
| `/var/run/reboot-required` | Pending kernel updates |

Never stop or suspend the host to save money: agents poll it every minute.
