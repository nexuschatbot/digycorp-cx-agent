#!/usr/bin/env bash
# Shared deploy library. Sourced AFTER an environment config (deploy/dev/config.sh
# or deploy/prod/config.sh) by that environment's deploy.sh / provision.sh.
#
# It does three things, in order:
#   1. defines the output helpers (one definition, every caller — the copies
#      drifted once and a missing warn() died mid-provision),
#   2. enforces the CROSS-TARGET GUARD: a non-prod config that names any piece
#      of the production identity is refused before a single az/ssh call runs,
#   3. derives everything downstream (FQDN, SITE_ADDRESS, paths, the remote
#      compose invocation) from the environment config.
#
# Never source this directly from a shell; it is plumbing for the entry points.

# --- Output helpers ----------------------------------------------------------
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[0;32m\xe2\x9c\x93\033[0m %s\n' "$*"; }
warn() { printf '    \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Sanity: an environment config must have been sourced first --------------
for req in DEPLOY_ENV RESOURCE_GROUP LOCATION VM_NAME VM_SIZE VM_IMAGE \
           ADMIN_USER DNS_LABEL ACME_EMAIL; do
  [[ -n "${!req:-}" ]] || die "lib.sh sourced without an environment config (missing ${req}).
  Use the per-environment entry points:
      ./deploy/dev/deploy.sh
      ./deploy/prod/deploy.sh"
done
# CUSTOM_DOMAIN may legitimately be empty (dev), so only check it is SET.
[[ -n "${CUSTOM_DOMAIN+x}" ]] || die "environment config did not set CUSTOM_DOMAIN"

# --- Cross-target guard ------------------------------------------------------
# THE PRODUCTION IDENTITY, RESTATED HERE ON PURPOSE. This guard must not source
# deploy/prod/config.sh to learn what production looks like — a guard that reads
# its blocklist from the thing it guards can be edited out of existence in one
# file. If the prod identity ever changes, change it in prod/config.sh AND here.
PROD_RESOURCE_GROUP_ID="elite-demo-rg"
PROD_VM_NAME_ID="elite-demo-vm"
PROD_DNS_LABEL_ID="elite-demo"
PROD_CUSTOM_DOMAIN_ID="elite-group.digycorp.com"

if [[ "$DEPLOY_ENV" != "prod" ]]; then
  [[ "$RESOURCE_GROUP" != "$PROD_RESOURCE_GROUP_ID" ]] \
    || die "refusing: ${DEPLOY_ENV} config names the PRODUCTION resource group (${PROD_RESOURCE_GROUP_ID})"
  [[ "$VM_NAME" != "$PROD_VM_NAME_ID" ]] \
    || die "refusing: ${DEPLOY_ENV} config names the PRODUCTION VM (${PROD_VM_NAME_ID})"
  [[ "$DNS_LABEL" != "$PROD_DNS_LABEL_ID" ]] \
    || die "refusing: ${DEPLOY_ENV} config claims the PRODUCTION DNS label (${PROD_DNS_LABEL_ID})"
  # A non-prod VM must never answer for ANY custom domain, not just prod's:
  # this is how the dev VM once ended up serving the prod hostname and burning
  # failed ACME attempts against it (2026-07-28).
  [[ -z "$CUSTOM_DOMAIN" ]] \
    || die "refusing: ${DEPLOY_ENV} config sets CUSTOM_DOMAIN='${CUSTOM_DOMAIN}'.
  Only production answers for a custom domain. Non-prod serves its Azure FQDN only."
fi

# --- Derived values — do not set directly ------------------------------------
FQDN="${DNS_LABEL}.${LOCATION}.cloudapp.azure.com"

# BOTH names, comma-separated, is Caddy's own multi-host site syntax: the Azure
# FQDN stays in the list as the fallback that still works when a custom DNS
# record is wrong, mid-propagation, or not yet created.
if [[ -n "$CUSTOM_DOMAIN" ]]; then
  SITE_ADDRESS="${CUSTOM_DOMAIN}, ${FQDN}"
  SITE_HOST="${CUSTOM_DOMAIN}"
else
  SITE_ADDRESS="${FQDN}"
  SITE_HOST="${FQDN}"
fi
SITE_URL="https://${SITE_HOST}"

# --- Layout ------------------------------------------------------------------
REMOTE_DIR="/opt/elite"

# Repo root = the directory ABOVE deploy/, resolved from this file's location so
# the scripts work regardless of the caller's working directory.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Each environment ships ITS OWN env file — never the root .env (that one serves
# local docker compose only) and never the other environment's.
ENV_DIR="${REPO_ROOT}/deploy/${DEPLOY_ENV}"
ENV_FILE="${ENV_DIR}/.env"

# Each environment has ITS OWN compose overlay, so editing one stack can never
# change the other. The path is relative because it is executed ON the VM.
COMPOSE_REMOTE="docker compose -f docker-compose.yml -f deploy/${DEPLOY_ENV}/docker-compose.yml"
