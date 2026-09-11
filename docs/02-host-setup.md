# 2. Host setup

> **Status:** Executed on the deployed host, on Ubuntu 24.04 against a real 100 GB block device, and previously on a scratch host against a loopback device.
> `scripts/20-host-setup.sh` ran clean both times: filesystem created, mounted by UUID through `fstab`, Docker verified.
> The reboot check in 2.7 was performed on the real host and the volume came back with no manual step.

Prepares a freshly installed Debian-family host to run Discourse.
Everything here is deliberately generic: it assumes a Linux host, a block device, and a working package manager, and nothing about where those came from.

If your platform needs steps before this — creating the machine, attaching the device, opening ports — those belong in `platforms/`.

Run `scripts/20-host-setup.sh /dev/sdX` to perform this chapter, or follow it by hand.
The script is idempotent and safe to re-run.

It refuses to touch a device that is already mounted somewhere else, or one that looks like the root disk.
Those guards are what make a typo in the device name survivable.

`SKIP_UFW=1` skips section 2.6, for environments where a host firewall is the wrong tool — a container, or a host whose ingress is managed elsewhere.

## 2.1 Update and reboot

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo reboot
```

Do this first, and reboot even if nothing obviously requires it.
Unattended security upgrades install kernel packages but **never reboot the machine**, so a host that has been up for a while is often running an older kernel than it has installed.
Better to discover a boot problem now than during an install.

## 2.2 Docker

```bash
docker --version
```

If Docker is absent, install it from Docker's own repository rather than the distribution's, following [Docker's instructions](https://docs.docker.com/engine/install/ubuntu/).
Distribution packages lag, and `launcher` enforces a minimum version.

The required floor is 20.10; Discourse recommends 24.0.7 or newer.

## 2.3 Persistent storage

Identify the device, make a filesystem on it, and mount it by UUID.

```bash
lsblk
sudo mkfs.ext4 /dev/sdb
sudo blkid /dev/sdb
sudo mkdir -p /mnt/qupa-cafe
```

`mkfs.ext4` on a bare device warns that it is about to format the entire device rather than a partition, and waits for confirmation.
That is the intent here: a single-purpose data volume needs no partition table.
Answer `y`, or pass `-F` to skip the prompt, which is what `scripts/20-host-setup.sh` does so that it can run unattended.

Add the mount to `/etc/fstab`, **by UUID and with `nofail`**:

```
UUID=<uuid-from-blkid>  /mnt/qupa-cafe  ext4  defaults,noatime,nofail  0 2
```

```bash
sudo mount -a
df -h /mnt/qupa-cafe
```

Two choices here are this guide's own, and worth stating rather than adopting silently.

**By UUID, not by device path.** Device letters are assigned in discovery order and are not guaranteed stable across reboots.
A `/dev/sdb` entry that becomes `/dev/sdc` mounts the wrong filesystem or nothing at all.

**With `nofail`.** Without it, a missing or detached device stops the boot at an emergency prompt.
On a remote host with no console that is an outage requiring platform-level recovery; with `nofail` the machine boots, the forum fails to start, and you can log in and fix it.

## 2.4 Automatic security updates

```bash
sudo apt-get install -y unattended-upgrades
sudo systemctl enable --now unattended-upgrades
systemctl status unattended-upgrades --no-pager
```

Many cloud images enable this already; the commands are idempotent.

Understand what it does and does not do.
It installs security updates automatically.
It **does not reboot**, so kernel updates sit installed but inactive until someone reboots.

`/var/run/reboot-required` exists when a reboot is pending.
`docs/07-backups-and-ops.md` schedules checking it.

## 2.5 Intrusion mitigation

```bash
sudo apt-get install -y fail2ban
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
```

`fail2ban` is this guide's addition rather than anything Discourse requires.
Its value here is bounded and specific: it rate-limits SSH brute-force attempts against port 22.

## 2.6 Firewall, and why it matters less than you think

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

**Read this before trusting the output.**

Docker publishes container ports by writing its own iptables chain, which is consulted before `ufw`'s.
A published container port is reachable whether or not `ufw` allows it, and `ufw status` will not tell you that.

So `ufw` here protects services running *on the host* — anything you install outside a container — and nothing else.
The real ingress control for the forum is whatever your platform puts in front of the host.

This is a common and expensive misunderstanding.
Enabling `ufw`, seeing "Status: active", and concluding the host is firewalled is wrong in exactly the case that matters.

## 2.7 Verification

```bash
df -h /mnt/qupa-cafe                  # the data volume is mounted, with its full size
docker run --rm hello-world           # Docker works
systemctl is-enabled unattended-upgrades fail2ban
sudo reboot                           # then confirm /mnt/qupa-cafe remounted
```

The reboot is part of the test, not a formality.
A data volume that mounts by hand but not from `fstab` is a failure that otherwise surfaces months later, during an unrelated restart.

Reboot from inside the operating system, as above, rather than through a platform control panel or API.
If your platform offers a delete-protection setting and `platforms/` told you to enable it, that setting may also refuse power actions, so an API reboot can fail with a conflict while `sudo reboot` works.

After it comes back, check that the mount returned on its own:

```bash
findmnt /mnt/qupa-cafe
```

No output means `fstab` did not mount it, and `nofail` is why the host booted anyway rather than stopping to tell you.
That is the trade `nofail` makes: the failure is survivable, and therefore silent.
