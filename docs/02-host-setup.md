# 2. Host setup

Prepares a fresh Debian-family host with a blank data volume. Creating the machine and opening ports belongs in `platforms/`.

```bash
sudo ./scripts/20-host-setup.sh /dev/sdb       # the data volume, not the root disk
```

The script does everything below and is safe to re-run. It refuses a device mounted elsewhere or one that looks like the root disk. `SKIP_UFW=1` skips the firewall, for a container or WSL.

## 2.1 What it does

1. `apt-get update && apt-get upgrade`, and installs `unattended-upgrades`, `fail2ban` and `ufw`.
2. Stops if Docker is missing. Install it from [Docker's repository](https://docs.docker.com/engine/install/ubuntu/) if your image lacks it; `launcher` needs 20.10 or newer.
3. `mkfs.ext4 -F` on the device if it has no filesystem, and an `/etc/fstab` line **by UUID with `nofail`**: device letters can change across reboots, and without `nofail` a missing volume stops the boot at an emergency prompt.
   ```
   UUID=<uuid>  /mnt/qupa-cafe  ext4  defaults,noatime,nofail  0 2
   ```
4. Enables `unattended-upgrades`, which installs security updates and never reboots; `/var/run/reboot-required` marks a pending reboot.
5. Enables `fail2ban` for SSH.
6. `ufw` allows TCP 22, 80 and 443, and is enabled.

`MOUNT_POINT` overrides `/mnt/qupa-cafe`.

## 2.2 The firewall, both directions

`ufw` does not restrict published container ports; Docker bypasses it. It does restrict the other direction: a container connecting to a service on the host arrives on `docker0` and meets `ufw`'s default deny, and the connection hangs rather than being refused. Each host service a container reaches needs a rule naming the interface, address and port:

```bash
sudo ufw allow in on docker0 to 172.17.0.1 port 8090 proto tcp comment "ranking, container to host only"
sudo ufw allow in on docker0 to 172.17.0.1 port 8091 proto tcp comment "provision hook, container to host only"
```

`server/README.md` and `docs/05-forum-config.md` 5.3 cover those two services.

## 2.3 Verification

```bash
df -h /mnt/qupa-cafe                  # mounted, full size
systemctl is-enabled unattended-upgrades fail2ban
sudo reboot
findmnt /mnt/qupa-cafe                # after the reboot: no output means fstab did not mount it
```

The reboot is part of the test. Reboot from inside the OS: a platform delete-lock may refuse API power actions. `lsblk` needs no privileges; `dmesg` needs `sudo` on Ubuntu 24.04.
