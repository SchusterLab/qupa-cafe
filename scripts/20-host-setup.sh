#!/usr/bin/env bash
#
# Prepare a Debian-family host to run Discourse.
#
# Platform-independent: assumes a Linux host, a block device, and apt. Nothing
# about where those came from. Anything platform-specific belongs in platforms/.
#
# Idempotent. Safe to re-run.
#
# Usage:
#   sudo ./20-host-setup.sh /dev/sdb
#   sudo ./20-host-setup.sh --dry-run /dev/sdb
#
# Corresponds to docs/02-host-setup.md, which explains the two judgement calls
# this script makes (mount by UUID with nofail, and add fail2ban).

set -euo pipefail

MOUNT_POINT="${MOUNT_POINT:-/mnt/qupa-cafe}"

DRY_RUN=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=1
  shift
fi

DEVICE="${1:-}"

say()  { printf '\n== %s\n' "$*"; }
skip() { printf '   already done: %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '   WOULD RUN: %s\n' "$*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------- preflight

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ -n "$DEVICE" ]     || die "Usage: $0 [--dry-run] /dev/sdX  (the data volume, not the root disk)"
[ -b "$DEVICE" ]     || die "$DEVICE is not a block device. Run lsblk."

# Refusing to touch a mounted device is the guard that makes a typo survivable.
if findmnt --source "$DEVICE" > /dev/null 2>&1; then
  MOUNTED_AT="$(findmnt -n -o TARGET --source "$DEVICE" | head -1)"
  [ "$MOUNTED_AT" = "$MOUNT_POINT" ] \
    || die "$DEVICE is already mounted at $MOUNTED_AT, not $MOUNT_POINT. Refusing to touch it."
fi

if [ "$DEVICE" = "$(findmnt -n -o SOURCE / | sed 's/[0-9]*$//')" ]; then
  die "$DEVICE looks like the root disk. Refusing."
fi

export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------------ packages

say "System update"
run apt-get update -qq
run apt-get upgrade -y -qq

say "Packages"
run apt-get install -y -qq unattended-upgrades fail2ban ufw

# -------------------------------------------------------------------- docker

say "Docker"
if command -v docker > /dev/null 2>&1; then
  printf '   %s\n' "$(docker --version)"
else
  # Deliberately not automated. Install from Docker's own repository rather than
  # the distribution's: distribution packages lag, and launcher enforces a
  # minimum version. https://docs.docker.com/engine/install/ubuntu/
  die "Docker is not installed. Install it from Docker's repository, then re-run."
fi

# ------------------------------------------------------------------ storage

say "Data volume: $DEVICE -> $MOUNT_POINT"

if blkid "$DEVICE" > /dev/null 2>&1; then
  skip "$DEVICE already has a filesystem"
else
  printf '   making ext4 on %s\n' "$DEVICE"
  # -F because this is a bare device with no partition table, which is what a
  # single-purpose data volume wants. mkfs would otherwise prompt.
  run mkfs.ext4 -F "$DEVICE"
fi

UUID="$(blkid -s UUID -o value "$DEVICE" 2> /dev/null || echo "PENDING")"
printf '   UUID: %s\n' "$UUID"

run mkdir -p "$MOUNT_POINT"

# By UUID, not by device path: device letters are assigned in discovery order
# and are not stable across reboots.
#
# With nofail: without it, a missing or detached device stops the boot at an
# emergency prompt, which on a remote host with no console is an outage needing
# platform-level recovery. With it, the machine boots, the forum fails to start,
# and you can log in and fix it.
FSTAB_LINE="UUID=$UUID  $MOUNT_POINT  ext4  defaults,noatime,nofail  0 2"

if grep -q "$MOUNT_POINT" /etc/fstab; then
  skip "fstab entry for $MOUNT_POINT"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '   WOULD APPEND to /etc/fstab: %s\n' "$FSTAB_LINE"
  else
    cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
    printf '%s\n' "$FSTAB_LINE" >> /etc/fstab
    printf '   appended to /etc/fstab\n'
  fi
fi

if mountpoint -q "$MOUNT_POINT"; then
  skip "$MOUNT_POINT mounted"
else
  run mount -a
fi

# ------------------------------------------------------- automatic updates

say "Automatic security updates"
run systemctl enable --now unattended-upgrades

# It installs security updates and NEVER reboots, so kernel updates sit
# installed but inactive until someone reboots. docs/07-backups-and-ops.md
# schedules the check for /var/run/reboot-required.
printf '   note: installs updates, does not reboot\n'

# ------------------------------------------------------------------ fail2ban

say "fail2ban"
run systemctl enable --now fail2ban

# Our addition, not a Discourse requirement. Its value here is bounded and
# specific: it rate-limits SSH brute force against port 22.

# ---------------------------------------------------------------------- ufw

say "Firewall"
run ufw allow 22/tcp
run ufw allow 80/tcp
run ufw allow 443/tcp
run ufw --force enable

cat <<'WARNING'

   READ THIS BEFORE TRUSTING `ufw status`.

   Docker publishes container ports by writing its own iptables chain, which is
   consulted before ufw's. A published container port is reachable whether or
   not ufw allows it, and `ufw status` will not tell you that.

   So ufw here protects services running ON the host, outside a container, and
   nothing else. The real ingress control for the forum is whatever your
   platform puts in front of the host -- a security group, a cloud firewall.

   Enabling ufw, seeing "Status: active", and concluding the host is firewalled
   is wrong in exactly the case that matters.

WARNING

# ------------------------------------------------------------------- report

say "Verification"

if [ "$DRY_RUN" -eq 1 ]; then
  printf '   dry run: nothing changed\n'
  exit 0
fi

df -h "$MOUNT_POINT" || true
docker run --rm hello-world > /dev/null 2>&1 \
  && printf '   docker: ok\n' \
  || printf '   docker: FAILED to run hello-world\n'
systemctl is-enabled unattended-upgrades fail2ban || true

cat <<REPORT

============================================================
Host setup complete.

The reboot below is part of the test, not a formality. A data volume that
mounts by hand but not from fstab is a failure that otherwise surfaces months
later during an unrelated restart.

  sudo reboot
  # then, once it is back:
  df -h $MOUNT_POINT

Then continue at docs/03-install.md.
============================================================
REPORT
