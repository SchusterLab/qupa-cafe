#!/usr/bin/env bash
#
# Report on the things that fail quietly: backups, disk, certificate,
# embedding server, pending reboot. Exits non-zero if any check fails.
#   ./50-backup-check.sh [--quiet]      --quiet prints only failures
# docs/07-backups-and-ops.md.

set -uo pipefail

BACKUP_DIR="${BACKUP_DIR:-/mnt/qupa-cafe/shared/standalone/backups/default}"
DATA_MOUNT="${DATA_MOUNT:-/mnt/qupa-cafe}"
HOSTNAME_TO_CHECK="${DISCOURSE_HOSTNAME:-}"

# The embedding server is on the docker bridge gateway, not on loopback.
if [ -z "${TEI_URL:-}" ]; then
  TEI_HOST="$(docker network inspect bridge \
                --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)"
  TEI_URL="http://${TEI_HOST:-172.17.0.1}:8081"
fi

MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-36}"
MIN_BACKUP_BYTES="${MIN_BACKUP_BYTES:-1000000}"
DISK_WARN_PERCENT="${DISK_WARN_PERCENT:-80}"
CERT_WARN_DAYS="${CERT_WARN_DAYS:-21}"

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

status=0

ok()   { [ "$QUIET" -eq 1 ] || printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; status=1; }
fail() { printf '  FAIL  %s\n' "$*"; status=1; }
section() { [ "$QUIET" -eq 1 ] || printf '\n== %s\n' "$*"; }

# ------------------------------------------------------------------ backups

section "Backups"

if [ ! -d "$BACKUP_DIR" ]; then
  fail "backup directory $BACKUP_DIR does not exist"
else
  LATEST="$(find "$BACKUP_DIR" -name '*.tar.gz' -type f -printf '%T@ %p\n' 2> /dev/null | sort -rn | head -1)"

  if [ -z "$LATEST" ]; then
    fail "no backups in $BACKUP_DIR"
  else
    LATEST_PATH="${LATEST#* }"
    LATEST_EPOCH="${LATEST%% *}"
    AGE_HOURS=$(( ( $(date +%s) - ${LATEST_EPOCH%.*} ) / 3600 ))
    SIZE=$(stat -c %s "$LATEST_PATH")

    if [ "$AGE_HOURS" -gt "$MAX_BACKUP_AGE_HOURS" ]; then
      fail "newest backup is ${AGE_HOURS}h old (limit ${MAX_BACKUP_AGE_HOURS}h): $(basename "$LATEST_PATH")"
    else
      ok "newest backup is ${AGE_HOURS}h old: $(basename "$LATEST_PATH")"
    fi

    # A truncated backup, typically from a full disk.
    if [ "$SIZE" -lt "$MIN_BACKUP_BYTES" ]; then
      fail "newest backup is only ${SIZE} bytes -- suspiciously small"
    else
      ok "size ${SIZE} bytes"
    fi

    ok "count: $(find "$BACKUP_DIR" -name '*.tar.gz' -type f | wc -l)"
  fi
fi

# --------------------------------------------------------------------- disk

section "Disk"

for mount in / "$DATA_MOUNT"; do
  if ! mountpoint -q "$mount" && [ "$mount" != "/" ]; then
    fail "$mount is not mounted"
    continue
  fi
  USED=$(df --output=pcent "$mount" 2> /dev/null | tail -1 | tr -dc '0-9')
  if [ -z "$USED" ]; then
    fail "cannot read usage for $mount"
  elif [ "$USED" -ge "$DISK_WARN_PERCENT" ]; then
    warn "$mount is ${USED}% full"
  else
    ok "$mount is ${USED}% full"
  fi
done

# ---------------------------------------------------------------------- TLS

section "TLS"

if [ -z "$HOSTNAME_TO_CHECK" ]; then
  ok "skipped (set DISCOURSE_HOSTNAME to enable)"
else
  EXPIRY="$(echo | openssl s_client -connect "$HOSTNAME_TO_CHECK:443" -servername "$HOSTNAME_TO_CHECK" 2> /dev/null \
            | openssl x509 -noout -enddate 2> /dev/null | cut -d= -f2)"
  if [ -z "$EXPIRY" ]; then
    fail "could not read the certificate for $HOSTNAME_TO_CHECK"
  else
    DAYS_LEFT=$(( ( $(date -d "$EXPIRY" +%s) - $(date +%s) ) / 86400 ))
    if [ "$DAYS_LEFT" -lt "$CERT_WARN_DAYS" ]; then
      fail "certificate expires in ${DAYS_LEFT} days. Automatic renewal may be broken."
    else
      ok "certificate valid for ${DAYS_LEFT} more days"
    fi
  fi
fi

# --------------------------------------------------------------- embeddings

section "Embedding server"

if curl -fsS -m 10 -X POST "$TEI_URL/embed" \
     -H 'Content-Type: application/json' \
     -d '{"inputs":"healthcheck"}' > /dev/null 2>&1; then
  ok "$TEI_URL responds"
else
  warn "$TEI_URL is not responding. Relevance ranking degrades to mentions only."
fi

# ------------------------------------------------------------------- kernel

section "Pending reboot"

if [ -f /var/run/reboot-required ]; then
  warn "a reboot is pending (kernel updates are installed but not active)"
else
  ok "none"
fi

# ------------------------------------------------------------------- result

section "Result"
if [ "$status" -eq 0 ]; then
  [ "$QUIET" -eq 1 ] || printf '  all checks passed\n'
else
  printf '  one or more checks failed\n'
fi
exit "$status"
