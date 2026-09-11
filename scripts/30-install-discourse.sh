#!/usr/bin/env bash
#
# Install Discourse from config/app.yml.example.
#
# Renders the tracked template into /var/discourse/containers/app.yml with
# values from an untracked .env, then bootstraps.
#
# Usage:
#   cp .env.example .env      # then fill it in
#   sudo ./30-install-discourse.sh
#   sudo ./30-install-discourse.sh --render-only    # write app.yml, do not build
#
# Corresponds to docs/03-install.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DISCOURSE_DIR="${DISCOURSE_DIR:-/var/discourse}"
TEMPLATE="$REPO_ROOT/config/app.yml.example"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

RENDER_ONLY=0
[ "${1:-}" = "--render-only" ] && RENDER_ONLY=1

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- preflight

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ -f "$TEMPLATE" ]   || die "Template not found at $TEMPLATE"
[ -f "$ENV_FILE" ]   || die "No $ENV_FILE. Copy .env.example and fill it in."

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

REQUIRED=(
  DISCOURSE_HOSTNAME
  DISCOURSE_DEVELOPER_EMAILS
  DISCOURSE_SMTP_ADDRESS
  DISCOURSE_SMTP_USER_NAME
  DISCOURSE_SMTP_PASSWORD
)
for var in "${REQUIRED[@]}"; do
  [ -n "${!var:-}" ] || die "$var is not set in $ENV_FILE"
done

# Optional, but must be defined so envsubst renders it as empty rather than
# leaving the literal placeholder in the file.
export DISCOURSE_HOSTNAME_ALIASES="${DISCOURSE_HOSTNAME_ALIASES:-}"

# ------------------------------------------------------- the DNS precondition

say "Checking DNS for $DISCOURSE_HOSTNAME"

# The Let's Encrypt HTTP-01 challenge happens during the bootstrap below. If the
# A record is not resolving yet, the bootstrap fails partway and leaves a
# half-configured container. Better to stop here.
RESOLVED="$(getent hosts "$DISCOURSE_HOSTNAME" | awk '{print $1}' | head -1 || true)"
if [ -z "$RESOLVED" ]; then
  die "$DISCOURSE_HOSTNAME does not resolve. Add the A record and wait for it before installing."
fi
printf '   resolves to %s\n' "$RESOLVED"

if [ -n "${EXPECTED_IP:-}" ] && [ "$RESOLVED" != "$EXPECTED_IP" ]; then
  die "$DISCOURSE_HOSTNAME resolves to $RESOLVED, expected $EXPECTED_IP."
fi

# ---------------------------------------------------- the password YAML trap

case "$DISCOURSE_SMTP_PASSWORD" in
  *'#'*)
    printf '\n   NOTE: the SMTP password contains "#". The template quotes it, so this\n'
    printf '         is fine here -- but if you ever hand-edit app.yml, an unquoted "#"\n'
    printf '         begins a YAML comment and the rest of the password is discarded.\n'
    ;;
esac

# ------------------------------------------------------------------- tooling

say "Container tooling at $DISCOURSE_DIR"
if [ -d "$DISCOURSE_DIR/.git" ]; then
  printf '   already cloned\n'
else
  git clone --depth=1 --branch main https://github.com/discourse/discourse_docker.git "$DISCOURSE_DIR"
fi

# app.yml holds the SMTP password in plaintext.
chmod 700 "$DISCOURSE_DIR"

# -------------------------------------------------------------------- render

say "Rendering app.yml"

mkdir -p "$DISCOURSE_DIR/containers"
TARGET="$DISCOURSE_DIR/containers/app.yml"

if [ -f "$TARGET" ]; then
  cp "$TARGET" "$TARGET.bak.$(date +%Y%m%d%H%M%S)"
  printf '   backed up the existing app.yml\n'
fi

# The variable list is explicit and NOT optional. A bare `envsubst` would also
# expand $home in the plugin hooks, which Discourse's own templating owns, and
# silently break the plugin clone.
envsubst '
  ${DISCOURSE_HOSTNAME}
  ${DISCOURSE_HOSTNAME_ALIASES}
  ${DISCOURSE_DEVELOPER_EMAILS}
  ${DISCOURSE_SMTP_ADDRESS}
  ${DISCOURSE_SMTP_USER_NAME}
  ${DISCOURSE_SMTP_PASSWORD}
' < "$TEMPLATE" > "$TARGET"

chmod 600 "$TARGET"

if grep -q '\${' "$TARGET"; then
  printf '\n   Unsubstituted placeholders remain:\n'
  grep -n '\${' "$TARGET"
  die "Refusing to bootstrap with unsubstituted placeholders."
fi

printf '   wrote %s\n' "$TARGET"

# Sizing is not automatic outside the wizard, so refuse to proceed without it.
grep -q 'db_shared_buffers' "$TARGET" || die "db_shared_buffers is missing from app.yml. Nothing auto-tunes it."
grep -q 'UNICORN_WORKERS'   "$TARGET" || die "UNICORN_WORKERS is missing from app.yml. Nothing auto-tunes it."

say "Sizing in the rendered file"
grep -E 'db_shared_buffers|db_work_mem|UNICORN_WORKERS' "$TARGET" | sed 's/^/   /'
cat <<'SIZING'

   These are for a 4 vCPU / 15 GB host. If yours differs, edit
   config/app.yml.example and re-run:

     db_shared_buffers = 256 MB x GB_RAM, capped at 4096 MB
     UNICORN_WORKERS   = 2 x cores,       capped at 8

SIZING

if [ "$RENDER_ONLY" -eq 1 ]; then
  printf 'Render-only: stopping before bootstrap.\n'
  exit 0
fi

# ----------------------------------------------------------------- bootstrap

say "Bootstrapping (this takes a while)"
cd "$DISCOURSE_DIR"
./launcher rebuild app

# -------------------------------------------------------------------- report

cat <<REPORT

============================================================
Bootstrap finished. It is NOT done until three things are true.

  1. HTTPS serves a valid certificate:

       curl -sSI https://$DISCOURSE_HOSTNAME | head -1
       openssl s_client -connect $DISCOURSE_HOSTNAME:443 -servername $DISCOURSE_HOSTNAME \\
         < /dev/null 2> /dev/null | openssl x509 -noout -issuer -dates

  2. An admin account exists. Register $DISCOURSE_DEVELOPER_EMAILS on the site;
     it is granted admin automatically.

  3. A TEST EMAIL ACTUALLY ARRIVES. Admin -> Email, send to an address at a
     major mailbox provider, and read the received headers for spf=pass and
     dkim=pass with an aligned From domain.

Number 3 is the one people skip. Nothing in the bootstrap validates SMTP beyond
a literal comparison against "smtp.example.com", so a wrong relay config
installs cleanly and fails at the first password reset, months later, silently.

If email is what fails, do not let it block you -- this sends zero mail:

    cd $DISCOURSE_DIR && sudo ./launcher enter app
    rake admin:create
    exit

Use admin:create, not admin:invite. Then fix the relay separately.

Next: docs/04-embeddings.md.
============================================================
REPORT
