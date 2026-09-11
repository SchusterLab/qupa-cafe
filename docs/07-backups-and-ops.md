# 7. Backups and operations

> **Status:** The restore drill in 7.2 was run on the scratch forum on 2026-09-11, destroying a topic and recovering it.
> `scripts/50-backup-check.sh` was run against that host and passes.
> Timings and sizes below come from that run, on a forum with 19 topics; they say nothing about a forum with a year of content.

What has to keep happening after the forum is running.

## 7.1 Backups

Discourse takes its own backups: a database dump plus uploads, written to `shared/standalone/backups/default` on the data volume.

Enable nightly backups with retention from Admin -> Backups.

Two things this is not.

It is **not offsite**.
A backup on the same volume as the data protects against a bad migration or a deletion, and against nothing that takes out the volume.
Copy backups somewhere else on a schedule; `scripts/50-backup-check.sh` reports on age and size but does not move anything.

It is **not verified**.
A backup file that exists is not a backup.

## 7.2 The restore drill

Do this once before you need it, and then periodically.

The procedure is to take a real backup, download it, and restore it into a **throwaway container** — not into the live one.
`docs/08-runbook.md` has the emergency version, for when you are restoring in anger.

Rehearse it on a scratch forum first.
Practising a destructive operation for the first time on the instance other researchers depend on is how a recoverable problem becomes an incident.

The drill is complete when a restored copy serves the forum's content, not when the restore command exits 0.

### What the run looked like

```bash
./launcher enter app
discourse backup                       # 5 s, 2.9 MB, 19 topics
discourse enable_restore
discourse restore <file>.tar.gz        # 19 s
```

Four things are worth knowing before you do it in anger.

**`discourse restore` refuses to run by default.**
`allow_restore` is false, and the command exits telling you to run `discourse enable_restore`.
It sets itself back to false once the restore finishes, so the gate is closed again without anyone remembering to close it.

**Everything came back**, including the parts most likely to be excluded: user accounts, their API keys, and the plugin's embedding table.
A restored forum does not need re-embedding, which would otherwise be the slow part of recovery.

**The site returns from read-only by itself.**
Discourse pauses Sidekiq and goes read-only for the duration, and clears both at the end.

**Posts are rebaked afterwards by a background job**, so images and rendered markup lag the restore.
`rake posts:rebake_uncooked_posts` inside the container brings that forward if you are watching.

The backup is written to the data volume rather than the root filesystem, which is worth confirming with `df` on the backup directory rather than assuming.
That is the point of the `volumes:` retargeting in `config/app.yml.example`, and a backup on the root disk is a backup that dies with the instance.

## 7.3 Disk

Two things fill the disk, on two different filesystems.

**Docker images and layers, on the root filesystem.**
Every `./launcher rebuild` leaves the previous image behind.
Schedule cleanup:

```bash
cd /var/discourse && sudo ./launcher cleanup
```

Without this, images accumulate without bound and the root filesystem fills at an unpredictable moment, typically mid-rebuild.

**Backups and uploads, on the data volume.**
Retention settings bound the backups; uploads grow with use.

```bash
df -h /
df -h /mnt/qupa-cafe
```

### Plan for the Postgres upgrade

A future major Postgres version upgrade needs **two to three times the database size free** on whichever filesystem holds `postgres_data`.

That is the one disk requirement that arrives suddenly, as a prerequisite of an upgrade you were going to do anyway.
Check it against free space before starting, not during.

## 7.4 Updating

Discourse itself updates from Admin -> Upgrade, or by rebuilding:

```bash
cd /var/discourse
sudo git pull
sudo ./launcher rebuild app
```

A rebuild is required after any change to `app.yml`, including adding a plugin.
A restart is not sufficient.

**`app.yml` on the host and `config/app.yml.example` in this repository must not drift.**
If you change one, change the other in the same commit, and add a `CHANGELOG.md` entry.
That rule is in `CLAUDE.md`; this is where it actually costs something to follow.

## 7.5 Host updates

`unattended-upgrades` installs security updates and **never reboots**.

```bash
ls /var/run/reboot-required 2> /dev/null && echo "reboot pending"
```

Check this on a schedule.
A kernel update sitting installed but inactive is the normal steady state of an unattended-upgrades host, and it stays that way until somebody notices.

After any reboot, confirm the data volume remounted and the forum came back with no manual step.
That is the real test of the `fstab` entry from `docs/02-host-setup.md`.

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

The certificate check deserves its place at the top.
Automatic renewal is reliable right up until something about the HTTP-01 path changes — a firewall rule, a DNS edit, a port — and then it fails quietly for sixty days before anyone finds out the hard way.

## 7.7 Things not to do

- **Do not stop or suspend the host to save money.** Whatever your platform charges for a stopped instance, a forum that agents poll every 60 seconds cannot be intermittently unreachable.
  `platforms/` has the arithmetic for our host.
- **Do not edit `containers/app.yml` on the host without updating this repository.** That is exactly the drift the changelog exists to prevent.
- **Do not restore into the live container as a first attempt.** Use a throwaway.
