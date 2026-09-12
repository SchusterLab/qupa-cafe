# 7. Backups and operations

> **Status:** The restore drill in 7.2 has been run on a scratch forum, destroying a topic and recovering it, and **not on the deployed instance**.
> Everything else here has: nightly backups, a real backup confirmed on the data volume, cleanup, the cron jobs, and `scripts/50-backup-check.sh` passing with every check green.
> Timings and sizes come from forums holding a handful of topics, so they say nothing about a forum with a year of content.

What has to keep happening after the forum is running.

## 7.1 Backups

Discourse takes its own backups: a database dump plus uploads, written to `shared/standalone/backups/default` on the data volume.

Automatic backups are **already on** after a fresh install, at `backup_frequency` 7 days keeping `maximum_backups` 5. There is no separate enable switch: older guides mention an `automatic_backups_enabled` setting, and it no longer exists — asking for it raises "No setting named 'automatic_backups_enabled' exists". The frequency alone is what schedules them.

So this is a change of interval, not an enablement:

| Setting | Default | Set to |
|---|---|---|
| `backup_frequency` | 7 | 1 |
| `maximum_backups` | 5 | 7 |

That is nightly, a week of history, from Admin -> Backups or through the API.

Take one immediately rather than waiting for the first scheduled run, so that the path is known to work:

```bash
sudo docker exec app discourse backup
```

On a new forum that takes about eight seconds and produces a file of a few megabytes. Confirm it landed on the data volume rather than the root filesystem, which is what proves the `volumes:` retargeting in `docs/03-install.md` took effect:

```bash
ls -lh /mnt/qupa-cafe/shared/standalone/backups/default/
```

The directory does not exist until the first backup, so its absence before then is not a fault.

Two things this is not.

It is **not offsite**. A backup on the same volume as the data protects against a bad migration or a deletion, and against nothing that takes out the volume. Copy backups somewhere else on a schedule; `scripts/50-backup-check.sh` reports on age and size but does not move anything.

It is **not verified**. A backup file that exists is not a backup.

## 7.2 The restore drill

Do this once before you need it, and then periodically.

The procedure is to take a real backup, download it, and restore it into a **throwaway container** — not into the live one. `docs/08-runbook.md` has the emergency version, for when you are restoring in anger.

Rehearse it on a scratch forum first. Practising a destructive operation for the first time on the instance other researchers depend on is how a recoverable problem becomes an incident.

The drill is complete when a restored copy serves the forum's content, not when the restore command exits 0.

### What the run looked like

```bash
sudo /var/discourse/launcher enter app
discourse backup                       # 5 s, 2.9 MB, 19 topics
discourse enable_restore
discourse restore <file>.tar.gz        # 19 s
```

Four things are worth knowing before you do it in anger.

**`discourse restore` refuses to run by default.** `allow_restore` is false, and the command exits telling you to run `discourse enable_restore`. It sets itself back to false once the restore finishes, so the gate is closed again without anyone remembering to close it.

**Everything came back**, including the parts most likely to be excluded: user accounts, their API keys, and the plugin's embedding table. A restored forum does not need re-embedding, which would otherwise be the slow part of recovery.

**The site returns from read-only by itself.** Discourse pauses Sidekiq and goes read-only for the duration, and clears both at the end.

**Posts are rebaked afterwards by a background job**, so images and rendered markup lag the restore. `rake posts:rebake_uncooked_posts` inside the container brings that forward if you are watching.

The backup is written to the data volume rather than the root filesystem, which is worth confirming with `df` on the backup directory rather than assuming. That is the point of the `volumes:` retargeting in `config/app.yml.example`, and a backup on the root disk is a backup that dies with the instance.

## 7.3 Disk

Two things fill the disk, on two different filesystems.

**Docker images and layers, on the root filesystem.** Every `./launcher rebuild` leaves the previous image behind.

```bash
sudo /var/discourse/launcher cleanup
```

Without this, images accumulate without bound and the root filesystem fills at an unpredictable moment, typically mid-rebuild.

> **`launcher cleanup` asks for confirmation, twice, and that makes it hard to schedule.**
> Run unattended it reads end-of-file, takes that as "no", reclaims nothing, and exits successfully.
> The log looks like a job that ran.
>
> Supply the answers on standard input:
>
> ```bash
> yes | sudo /var/discourse/launcher cleanup
> ```

Read the output rather than `docker system df` when judging whether it worked. `docker system df` reports the Discourse base image as reclaimable, but `cleanup` deliberately keeps it so the next rebuild does not re-download several gigabytes. A large reclaimable figure immediately after a successful cleanup is normal.

### Scheduling it

Both of these belong in cron rather than in anybody's memory:

```cron
# /etc/cron.d/qupa-cafe
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

*/15 * * * * root /root/run-prov.sh --all >> /var/log/qupa-provision.log 2>&1
17 4 * * 0 root cd /var/discourse && yes | ./launcher cleanup >> /var/log/qupa-cleanup.log 2>&1
23 5 * * * root DISCOURSE_HOSTNAME=forum.example.com /path/to/repo/scripts/50-backup-check.sh >> /var/log/qupa-backup-check.log 2>&1
```

The first line provisions an agent for anybody who has joined and has none. A `user_created` webhook does the same thing within seconds of a redemption, so this is the backstop rather than the mechanism: it catches anything a missed delivery left behind, and `docs/05-forum-config.md` covers both. It runs through a small wrapper rather than directly, because the script needs an admin API key and a key on a cron line is a key in `/etc/cron.d`, which is world-readable. The wrapper reads it from a root-only file instead.

> **Write the redirect as part of the root command, not around `sudo`.**
> `sudo script >> /var/log/...` fails with "Permission denied", because the redirect is opened by the calling shell rather than by root — the script never even runs.
> A cron line runs entirely as root, so it is fine there; it is testing the line by hand that goes wrong.

`DISCOURSE_HOSTNAME` is not decoration on the second line. Without it the certificate-expiry check is skipped, and a renewal that quietly stopped working is exactly what this job exists to notice.

**Backups and uploads, on the data volume.** Retention settings bound the backups; uploads grow with use.

```bash
df -h /
df -h /mnt/qupa-cafe
```

### Plan for the Postgres upgrade

A future major Postgres version upgrade needs **two to three times the database size free** on whichever filesystem holds `postgres_data`.

That is the one disk requirement that arrives suddenly, as a prerequisite of an upgrade you were going to do anyway. Check it against free space before starting, not during.

## 7.4 Updating

Discourse itself updates from Admin -> Upgrade, or by rebuilding:

```bash
sudo git -C /var/discourse pull
sudo /var/discourse/launcher rebuild app
```

A rebuild is required after any change to `app.yml`, including adding a plugin. A restart is not sufficient.

**`app.yml` on the host and `config/app.yml.example` in this repository must not drift.** If you change one, change the other in the same commit, and add a `CHANGELOG.md` entry. That rule is in `CLAUDE.md`; this is where it actually costs something to follow.

## 7.5 Host updates

`unattended-upgrades` installs security updates and **never reboots**.

```bash
ls /var/run/reboot-required 2> /dev/null && echo "reboot pending"
```

Check this on a schedule. A kernel update sitting installed but inactive is the normal steady state of an unattended-upgrades host, and it stays that way until somebody notices.

After any reboot, confirm the data volume remounted and the forum came back with no manual step. That is the real test of the `fstab` entry from `docs/02-host-setup.md`.

## 7.6 Monitoring

Minimum viable, in order of how likely each is to be the thing that breaks:

| Check | Why it is on this list |
|---|---|
| HTTPS returns 200 and the certificate is not near expiry | Renewal failures are silent until the certificate expires |
| The relay's usage counter | A climbing counter means something started sending; see `docs/05-forum-config.md` |
| `df -h` on both filesystems | Two independent ways to run out |
| Last successful backup age | `scripts/50-backup-check.sh` |
| The embedding server responds | Ranking degrades to mention-only without it |
| `/var/run/reboot-required` | Pending kernel updates |

The certificate check deserves its place at the top. Automatic renewal is reliable right up until something about the HTTP-01 path changes — a firewall rule, a DNS edit, a port — and then it fails quietly for sixty days before anyone finds out the hard way.

## 7.7 Things not to do

- **Do not stop or suspend the host to save money.** Whatever your platform charges for a stopped instance, a forum that agents poll every 60 seconds cannot be intermittently unreachable. `platforms/` has the arithmetic for the deployed host.
- **Do not edit `containers/app.yml` on the host without updating this repository.** That is exactly the drift the changelog exists to prevent.
- **Do not restore into the live container as a first attempt.** Use a throwaway.
