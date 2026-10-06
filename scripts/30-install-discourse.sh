#!/usr/bin/env bash
#
# Install Discourse from config/app.yml.example and .env. docs/03-install.md.
#
#   sudo ./30-install-discourse.sh                  render and bootstrap
#   sudo ./30-install-discourse.sh --render-only    render app.yml, check DNS, stop
#   sudo ./30-install-discourse.sh --local          scratch host: no TLS, no DNS check

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DISCOURSE_DIR="${DISCOURSE_DIR:-/var/discourse}"
TEMPLATE="$REPO_ROOT/config/app.yml.example"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

RENDER_ONLY=0
LOCAL=0
for argument in "$@"; do
  case "$argument" in
    --render-only) RENDER_ONLY=1 ;;
    --local)       LOCAL=1 ;;
    *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ -f "$ENV_FILE" ]   || die "No $ENV_FILE. Copy .env.example and fill it in."

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

for var in DISCOURSE_HOSTNAME DISCOURSE_DEVELOPER_EMAILS DISCOURSE_DATA_DIR \
           DISCOURSE_SMTP_ADDRESS DISCOURSE_SMTP_USER_NAME DISCOURSE_SMTP_PASSWORD; do
  [ -n "${!var:-}" ] || die "$var is not set in $ENV_FILE"
done

if [ "$LOCAL" -eq 1 ]; then
  # A scratch forum has no relay, and should not hold a copy of the live password.
  export DISCOURSE_SMTP_ADDRESS=localhost DISCOURSE_SMTP_USER_NAME=scratch DISCOURSE_SMTP_PASSWORD=scratch
else
  # The certificate is requested during the bootstrap, so a wrong A record
  # fails it halfway. A parked domain's wildcard resolves too, hence EXPECTED_IP.
  say "Checking DNS for $DISCOURSE_HOSTNAME"
  RESOLVED="$(getent hosts "$DISCOURSE_HOSTNAME" | awk '{print $1}' | head -1 || true)"
  [ -n "$RESOLVED" ] || die "$DISCOURSE_HOSTNAME does not resolve."
  printf '   resolves to %s\n' "$RESOLVED"
  if [ -n "${EXPECTED_IP:-}" ] && [ "$RESOLVED" != "$EXPECTED_IP" ]; then
    die "$DISCOURSE_HOSTNAME resolves to $RESOLVED, expected $EXPECTED_IP."
  fi
fi

say "Container tooling at $DISCOURSE_DIR"
if [ ! -d "$DISCOURSE_DIR/.git" ]; then
  git clone --depth=1 --branch main https://github.com/discourse/discourse_docker.git "$DISCOURSE_DIR"
fi
chmod 700 "$DISCOURSE_DIR"

say "Rendering app.yml"
mkdir -p "$DISCOURSE_DIR/containers"
TARGET="$DISCOURSE_DIR/containers/app.yml"
[ -f "$TARGET" ] && cp "$TARGET" "$TARGET.bak.$(date +%Y%m%d%H%M%S)"

# The list is explicit so that $home in the plugin hooks, which the launcher
# owns, is left alone.
envsubst '
  ${DISCOURSE_HOSTNAME}
  ${DISCOURSE_DEVELOPER_EMAILS}
  ${DISCOURSE_DATA_DIR}
  ${DISCOURSE_SMTP_ADDRESS}
  ${DISCOURSE_SMTP_USER_NAME}
  ${DISCOURSE_SMTP_PASSWORD}
' < "$TEMPLATE" > "$TARGET"

if [ "$LOCAL" -eq 1 ]; then
  sed -i '/web\.ssl\.template\.yml/d; /web\.letsencrypt\.ssl\.template\.yml/d; /"443:443"/d' "$TARGET"
fi
chmod 600 "$TARGET"

if grep -vE '^\s*#' "$TARGET" | grep -q '\${'; then
  grep -nE '\$\{' "$TARGET" | grep -vE ':\s*#'
  die "Unsubstituted placeholders remain."
fi
printf '   wrote %s\n' "$TARGET"
grep -E '^\s*(db_shared_buffers|UNICORN_WORKERS):' "$TARGET" | sed 's/^ */   /'

# The qupa-site-images hook copies the logo from here on every rebuild.
BRAND_DIR="$DISCOURSE_DATA_DIR/shared/standalone/qupa-brand"
mkdir -p "$BRAND_DIR"
cp "$REPO_ROOT"/assets/qupa-cafe-logo*.png "$BRAND_DIR/"

[ "$RENDER_ONLY" -eq 1 ] && exit 0

say "Bootstrapping"
"$DISCOURSE_DIR/launcher" rebuild app

printf '\nDone. Now check HTTPS, the admin account and a test mail: docs/03-install.md 3.6.\n'
