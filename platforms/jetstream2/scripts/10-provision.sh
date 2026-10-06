#!/usr/bin/env bash
#
# Provision the Qupa Cafe host on Jetstream2. Idempotent: every step checks
# first, so a second run is a no-op. platforms/jetstream2/02-provision.md.
#
# Needs python3-openstackclient and python3-designateclient, the openrc sourced
# in a fresh terminal, and an ed25519 or RSA public key at $KEY_FILE.
#   ./10-provision.sh [--dry-run]

set -euo pipefail

# ------------------------------------------------------------------- settings

INSTANCE="${INSTANCE:-qupa-cafe}"
VOLUME="${VOLUME:-qupa-cafe-data}"
VOLUME_GB="${VOLUME_GB:-100}"
ROOT_GB="${ROOT_GB:-60}"
KEYPAIR="${KEYPAIR:-qupa-cafe}"
KEY_FILE="${KEY_FILE:-$HOME/.ssh/qupa-cafe.pub}"
FLAVOR="${FLAVOR:-m3.quad}"
IMAGE="${IMAGE:-Featured-Ubuntu24}"
SG_SSH="${SG_SSH:-qupa-cafe-ssh}"
SG_WEB="${SG_WEB:-qupa-cafe-web}"

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# -------------------------------------------------------------------- helpers

say()  { printf '\n== %s\n' "$*"; }
skip() { printf '   already present: %s\n' "$*"; }
did()  { printf '   created: %s\n' "$*"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '   WOULD RUN: %s\n' "$*"
  else
    "$@"
  fi
}

# `openstack <thing> show` fails when the thing does not exist.
exists() {
  openstack "$@" > /dev/null 2>&1
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

say "Preflight"

command -v openstack > /dev/null || die "openstack CLI not found. See platforms/jetstream2/02-provision.md."

[ -n "${OS_AUTH_URL:-}" ] || die "No credentials in the environment. Source your openrc in a FRESH terminal."

openstack flavor list > /dev/null 2>&1 \
  || die "Cannot talk to OpenStack. If you sourced an openrc over another one, open a new terminal."

[ -f "$KEY_FILE" ] || die "No public key at $KEY_FILE. Jetstream2 accepts only RSA or ed25519."

case "$(cut -d' ' -f1 < "$KEY_FILE")" in
  ssh-ed25519|ssh-rsa) ;;
  *) die "$KEY_FILE is not ed25519 or RSA. Jetstream2 supports only those two." ;;
esac

printf '   ok\n'

# ------------------------------------------------------------------- keypair

# First: a keypair can only be attached when the instance is created.
say "Keypair: $KEYPAIR"
if exists keypair show "$KEYPAIR"; then
  skip "$KEYPAIR"
else
  run openstack keypair create --public-key "$KEY_FILE" "$KEYPAIR"
  did "$KEYPAIR"
fi

# ------------------------------------------------------------ security groups

say "Security group: $SG_SSH"
if exists security group show "$SG_SSH"; then
  skip "$SG_SSH"
else
  run openstack security group create --description "ssh and icmp enabled" "$SG_SSH"
  run openstack security group rule create --protocol tcp --dst-port 22:22 --remote-ip 0.0.0.0/0 "$SG_SSH"
  run openstack security group rule create --protocol icmp "$SG_SSH"
  did "$SG_SSH"
fi

say "Security group: $SG_WEB"
if exists security group show "$SG_WEB"; then
  skip "$SG_WEB"
else
  run openstack security group create --description "http and https" "$SG_WEB"
  run openstack security group rule create --protocol tcp --dst-port 80:80   --remote-ip 0.0.0.0/0 "$SG_WEB"
  run openstack security group rule create --protocol tcp --dst-port 443:443 --remote-ip 0.0.0.0/0 "$SG_WEB"
  did "$SG_WEB"
fi

# -------------------------------------------------------------------- volume

say "Data volume: $VOLUME (${VOLUME_GB} GB)"
if exists volume show "$VOLUME"; then
  skip "$VOLUME"
else
  run openstack volume create --size "$VOLUME_GB" "$VOLUME"
  did "$VOLUME"
fi

# ------------------------------------------------------------------ instance

say "Instance: $INSTANCE ($FLAVOR, $IMAGE, ${ROOT_GB} GB root)"
if exists server show "$INSTANCE"; then
  skip "$INSTANCE"
else
  # No --nic: the single auto_allocated_network carries the DNS domain.
  run openstack server create "$INSTANCE" \
    --flavor "$FLAVOR" \
    --image "$IMAGE" \
    --boot-from-volume "$ROOT_GB" \
    --key-name "$KEYPAIR" \
    --security-group "$SG_SSH" \
    --security-group "$SG_WEB" \
    --wait
  did "$INSTANCE"
fi

# ------------------------------------------------------------ attach volume

say "Attach $VOLUME to $INSTANCE"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '   WOULD RUN: openstack server add volume %s %s\n' "$INSTANCE" "$VOLUME"
else
  # Attachments record the server's UUID, not its name.
  SERVER_ID="$(openstack server show "$INSTANCE" -f value -c id)"
  if openstack volume show "$VOLUME" -f value -c attachments | grep -q "$SERVER_ID"; then
    skip "attachment"
  else
    openstack server add volume "$INSTANCE" "$VOLUME"
    did "attachment"
  fi
fi

# -------------------------------------------------------------- floating IP

say "Floating IP"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '   WOULD RUN: openstack floating ip create public; openstack server add floating ip %s <IP>\n' "$INSTANCE"
  FLOATING_IP="<dry-run>"
else
  # Reuse a floating IP already attached, found by looking each address up,
  # since Nova lists fixed and floating addresses in no promised order.
  FLOATING_IP=""
  for addr in $(openstack server show "$INSTANCE" -f value -c addresses \
                | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+'); do
    if openstack floating ip list -f value -c "Floating IP Address" | grep -qx "$addr"; then
      FLOATING_IP="$addr"
      break
    fi
  done

  if [ -n "$FLOATING_IP" ]; then
    skip "$FLOATING_IP"
  else
    FLOATING_IP="$(openstack floating ip create public -f value -c floating_ip_address)"
    openstack server add floating ip "$INSTANCE" "$FLOATING_IP"
    did "$FLOATING_IP"
  fi
fi

# ---------------------------------------------------------------------- DNS

# Designate creates this record when the floating IP is attached.
say "Automatic DNS record"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '   WOULD RUN: openstack recordset list <zone>\n'
else
  ZONE="$(openstack zone list -f value -c name 2> /dev/null | head -1 || true)"
  if [ -n "$ZONE" ]; then
    printf '   zone: %s\n' "$ZONE"
    openstack recordset list "$ZONE" -f value -c name -c records 2> /dev/null | grep -i "$INSTANCE" || \
      printf '   no record matching %s yet; Designate may still be catching up\n' "$INSTANCE"
  else
    printf '   no zone visible. Is python3-designateclient installed?\n'
  fi
fi

# -------------------------------------------------------------------- report

cat <<REPORT

Done: $INSTANCE ($FLAVOR), $VOLUME attached, floating IP $FLOATING_IP.
  ssh -i ${KEY_FILE%.pub} ubuntu@$FLOATING_IP
Next: point the forum's A record at $FLOATING_IP and set EXPECTED_IP in .env,
openstack server lock $INSTANCE, re-run this to confirm a no-op, then
docs/02-host-setup.md.
REPORT
