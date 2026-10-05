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
#   sudo ./30-install-discourse.sh --local          # scratch host: no TLS, no DNS check
#
# --render-only is worth running first even on the real host. It performs the
# DNS check and the substitution, so an unset variable or an A record that has
# not propagated surfaces in a second rather than partway through a bootstrap.
#
# Corresponds to docs/03-install.md.

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

# ---------------------------------------------------------------- preflight

[ "$(id -u)" -eq 0 ] || die "Run with sudo."
[ -f "$TEMPLATE" ]   || die "Template not found at $TEMPLATE"
[ -f "$ENV_FILE" ]   || die "No $ENV_FILE. Copy .env.example and fill it in."

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

REQUIRED=(
  DISCOURSE_HOSTNAME
  DISCOURSE_DEVELOPER_EMAILS
  DISCOURSE_DATA_DIR
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

if [ "$LOCAL" -eq 1 ]; then
  say "Local scratch mode"

  # The relay credentials are dropped rather than rendered.
  #
  # --local is the rehearsal path, and the .env a reader has to hand is the one
  # they filled in for their real forum. Rendering it writes the production
  # hostname and the live SMTP password, in plaintext, into a scratch app.yml
  # on a disposable host -- a copy of a credential nobody asked to make. A
  # local forum has no relay to reach anyway, so there is nothing to lose by
  # replacing them. Set ENV_FILE to use a scratch .env instead.
  if [ -n "${DISCOURSE_SMTP_PASSWORD:-}" ]; then
    printf '   dropping the SMTP credentials from this render: a scratch app.yml
'
    printf '   has no relay to reach and should not hold a live password.
'
  fi
  export DISCOURSE_SMTP_ADDRESS="localhost"
  export DISCOURSE_SMTP_USER_NAME="scratch"
  export DISCOURSE_SMTP_PASSWORD="scratch"

  cat <<'LOCALNOTE'
   No TLS, and no DNS check. Two things this therefore does NOT validate:
   certificate issuance, which needs public DNS and port 80, and email
   deliverability. Both are verified only on the real instance.

   What it does validate is that app.yml parses, that the tuning values are
   accepted, and that the volumes: retargeting works -- before any of it
   touches a live host.

   Two variables worth knowing here. ENV_FILE points at a different .env, and
   DISCOURSE_DIR at a different tooling directory, so a scratch install can sit
   beside a real one without either touching the other.
LOCALNOTE
else
  say "Checking DNS for $DISCOURSE_HOSTNAME"

  # The Let's Encrypt HTTP-01 challenge happens during the bootstrap below. If
  # the A record is not resolving yet, the bootstrap fails partway and leaves a
  # half-configured container. Better to stop here.
  RESOLVED="$(getent hosts "$DISCOURSE_HOSTNAME" | awk '{print $1}' | head -1 || true)"
  if [ -z "$RESOLVED" ]; then
    die "$DISCOURSE_HOSTNAME does not resolve. Add the A record and wait for it before installing."
  fi
  printf '   resolves to %s\n' "$RESOLVED"

  if [ -n "${EXPECTED_IP:-}" ] && [ "$RESOLVED" != "$EXPECTED_IP" ]; then
    die "$DISCOURSE_HOSTNAME resolves to $RESOLVED, expected $EXPECTED_IP."
  fi
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
  ${DISCOURSE_DATA_DIR}
  ${DISCOURSE_SMTP_ADDRESS}
  ${DISCOURSE_SMTP_USER_NAME}
  ${DISCOURSE_SMTP_PASSWORD}
' < "$TEMPLATE" > "$TARGET"

if [ "$LOCAL" -eq 1 ]; then
  # Drop the two TLS templates and port 443. Let's Encrypt needs public DNS and
  # a reachable port 80, neither of which a laptop has; leaving the templates in
  # makes the bootstrap fail at the certificate request.
  sed -i '/web\.ssl\.template\.yml/d; /web\.letsencrypt\.ssl\.template\.yml/d; /"443:443"/d' "$TARGET"
  printf '   local mode: removed the TLS templates and port 443\n'
fi

chmod 600 "$TARGET"

# Comments are excluded: the template's own header explains the substitution and
# necessarily contains a placeholder-shaped example.
if grep -vE '^\s*#' "$TARGET" | grep -q '\${'; then
  printf '\n   Unsubstituted placeholders remain:\n'
  grep -nE '\$\{' "$TARGET" | grep -vE ':\s*#'
  die "Refusing to bootstrap with unsubstituted placeholders."
fi

printf '   wrote %s\n' "$TARGET"

# The forum's logo, on the shared volume where the qupa-site-images hook in
# app.yml copies it into the image on every rebuild. docs/05-forum-config.md 5.6.
BRAND_DIR="$DISCOURSE_DATA_DIR/shared/standalone/qupa-brand"
mkdir -p "$BRAND_DIR"
cp "$REPO_ROOT"/assets/qupa-cafe-logo*.png "$BRAND_DIR/"
printf '   copied the logo to %s\n' "$BRAND_DIR"

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

if [ "$LOCAL" -eq 1 ]; then
  cat <<REPORT

============================================================
Scratch forum bootstrapped.

  curl -sSI http://localhost | head -1

Get an admin account without touching SMTP:

    sudo $DISCOURSE_DIR/launcher enter app
    rake admin:create
    exit

What this run validated: app.yml parses, the explicit tuning values are
accepted, and the volumes: retargeting to $DISCOURSE_DATA_DIR works.

What it did NOT validate, and cannot: certificate issuance, email
deliverability, and anything platform-specific. Those are verified only on the
real instance.
============================================================
REPORT
  exit 0
fi

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
     major mailbox provider, and read the received headers for dmarc=pass with
     the DKIM signature's d= matching the domain in From. spf=none is fine.

Number 3 is the one people skip. Nothing in the bootstrap validates SMTP beyond
a literal comparison against "smtp.example.com", so a wrong relay config
installs cleanly and fails at the first password reset, months later, silently.

If email is what fails, do not let it block you -- this sends zero mail:

    sudo $DISCOURSE_DIR/launcher enter app
    rake admin:create
    exit

Use admin:create, not admin:invite. Then fix the relay separately.

Next: docs/04-embeddings.md.
============================================================
REPORT
