#!/usr/bin/env bash
#
# Provision the Qupa Cafe host on Jetstream2.
#
# Idempotent: every step is guarded by an existence check, so a second run is a
# no-op and a run interrupted halfway can simply be repeated. That property is
# the point of the script -- see platforms/jetstream2/02-provision.md.
#
# Prerequisites:
#   - python3-openstackclient and python3-designateclient installed
#   - an application credential sourced IN A FRESH TERMINAL:
#       source app-cred-qupa-cafe-openrc.sh
#   - an ed25519 or RSA public key at $KEY_FILE
#
# Usage:
#   ./10-provision.sh              provision
#   ./10-provision.sh --dry-run    print what would happen, change nothing
#
# Ends at an instance you can SSH into. Continue at docs/02-host-setup.md.

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

# `openstack <thing> show` exits non-zero when the thing does not exist, which
# is the existence check every step below uses.
exists() {
  openstack "$@" > /dev/null 2>&1
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

say "Preflight"

command -v openstack > /dev/null || die "openstack CLI not found. See platforms/jetstream2/02-provision.md."

[ -n "${OS_AUTH_URL:-}" ] || die "No credentials in the environment. Source your openrc in a FRESH terminal."

# The smoke test doubles as a check that the application credential has not
# expired -- a blank expiration field defaults to today, so this fails often.
openstack flavor list > /dev/null 2>&1 \
  || die "Cannot talk to OpenStack. If you sourced an openrc over another one, open a new terminal."

[ -f "$KEY_FILE" ] || die "No public key at $KEY_FILE. Jetstream2 accepts only RSA or ed25519."

case "$(cut -d' ' -f1 < "$KEY_FILE")" in
  ssh-ed25519|ssh-rsa) ;;
  *) die "$KEY_FILE is not ed25519 or RSA. Jetstream2 supports only those two." ;;
esac

printf '   ok\n'

# ------------------------------------------------------------------- keypair

# This comes first because a keypair can ONLY be attached at instance creation.
# An instance created without one is unreachable and has to be rebuilt.
say "Keypair: $KEYPAIR"
if exists keypair show "$KEYPAIR"; then
  skip "$KEYPAIR"
else
  run openstack keypair create --public-key "$KEY_FILE" "$KEYPAIR"
  did "$KEYPAIR"
fi

# ------------------------------------------------------------ security groups

# Groups are additive, and the OpenStack-provided `default` group is left alone.
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
  # [extrapolated] These two follow the documented pattern for the port 22 rule
  # but were not published by Jetstream2. Verify against the group after creation.
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
  # --nic is deliberately omitted. With a single network OpenStack selects
  # auto_allocated_network, which is what Jetstream2 recommends and what
  # carries the DNS domain.
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
  # The attachments field records the server's UUID, never its name, so the
  # name has to be resolved before it can be looked for.
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
  # An address already on the instance is reused rather than a second allocated.
  # The instance reports its fixed and floating addresses in one list, in an
  # order Nova does not promise, so the floating one is identified by looking
  # each address up rather than by taking a position in that list.
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

# Jetstream2 runs OpenStack Designate and creates a record automatically when a
# floating IP is attached. Instance names are lowercased and underscores become
# dashes, so the record is read rather than predicted.
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

============================================================
Provisioning complete.

  Instance     $INSTANCE
  Flavor       $FLAVOR
  Floating IP  $FLOATING_IP
  Data volume  $VOLUME (${VOLUME_GB} GB), attached
  SSH          ssh -i ${KEY_FILE%.pub} ubuntu@$FLOATING_IP

Next, by hand:

  1. Add an A record for forum.qupa-cafe.com -> $FLOATING_IP at the registrar.
  2. Wait for it:  dig +short forum.qupa-cafe.com
     This MUST return the floating IP before you install Discourse, or the
     Let's Encrypt HTTP-01 challenge fails and leaves a half-configured
     container.
  3. Lock the instance against accidental deletion:
       openstack server lock $INSTANCE
  4. Continue at docs/02-host-setup.md.

Re-run this script to confirm it is a no-op. An idempotent script that has
only been run once is an untested claim.
============================================================
REPORT
