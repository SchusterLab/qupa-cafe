#!/usr/bin/env bash
#
# Prepare a Debian-family host to run Discourse. Idempotent.
#   sudo ./20-host-setup.sh [--dry-run] /dev/sdb
# docs/02-host-setup.md.

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

# Refusing a mounted device or the root disk makes a typo survivable.
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
  die "Docker is not installed. Install it from Docker's repository, then re-run."
fi

# ------------------------------------------------------------------ storage

say "Data volume: $DEVICE -> $MOUNT_POINT"

if blkid "$DEVICE" > /dev/null 2>&1; then
  skip "$DEVICE already has a filesystem"
else
  printf '   making ext4 on %s\n' "$DEVICE"
  # -F: a bare device with no partition table, without the prompt.
  run mkfs.ext4 -F "$DEVICE"
fi

UUID="$(blkid -s UUID -o value "$DEVICE" 2> /dev/null || echo "PENDING")"
printf '   UUID: %s\n' "$UUID"

run mkdir -p "$MOUNT_POINT"

# By UUID, since device letters move between boots; nofail, so a missing
# device leaves a bootable host rather than an emergency prompt.
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

# ------------------------------------------------------------------ fail2ban

say "fail2ban"
run systemctl enable --now fail2ban

# ---------------------------------------------------------------------- ufw

say "Firewall"

# SKIP_UFW=1 where a host firewall breaks networking: WSL, a container.
if [ "${SKIP_UFW:-0}" = "1" ]; then
  printf '   skipped (SKIP_UFW=1)\n'
else
  run ufw allow 22/tcp
  run ufw allow 80/tcp
  run ufw allow 443/tcp
  run ufw --force enable
fi
printf '   ufw does not filter published container ports: Docker bypasses it.\n'

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

printf '\nDone. Now reboot, and check that %s remounted (docs/02-host-setup.md).\n' "$MOUNT_POINT"
