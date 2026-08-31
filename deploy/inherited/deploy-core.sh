#!/usr/bin/env bash
# Deploy BOTH the frontend and the backend to ONE environment's Azure VM (AD-G).
#
# NOT AN ENTRY POINT. Run the per-environment wrappers, which load the right
# config first and then source this:
#
#   ./deploy/dev/deploy.sh                 # ship working tree to the DEV VM
#   ./deploy/prod/deploy.sh               # ship working tree to PROD (asks first)
#
# Flags (accepted by both wrappers and passed through):
#   --reset-demo    ...then run the demo-morning ritual (destructive)
#   --logs          ...then tail logs until Ctrl-C
#
# SAFE TO RE-RUN. First run provisions the environment's VM (via its
# provision.sh) and issues the TLS certificate; every subsequent run is just
# "sync code, rebuild, restart".
#
# WHAT SHIPS: the working tree, not a git commit. That is deliberate for a demo —
# you can deploy an uncommitted fix at 8am on demo day — but it also means
# whatever is on your disk right now is what lands on the VM.

set -euo pipefail

# say/ok/warn/die and every derived value come from lib.sh, which the wrapper
# sourced after its config. If they are missing, someone ran this file directly —
# refuse rather than guess a target.
if ! declare -F die >/dev/null || [[ -z "${DEPLOY_ENV:-}" ]]; then
  printf 'deploy-core.sh is not an entry point. Run:\n' >&2
  printf '    ./deploy/dev/deploy.sh   or   ./deploy/prod/deploy.sh\n' >&2
  exit 2
fi

RESET_DEMO=0
FOLLOW_LOGS=0
for arg in "$@"; do
  case "$arg" in
    --reset-demo) RESET_DEMO=1 ;;
    --logs)       FOLLOW_LOGS=1 ;;
    -h|--help)    sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            printf 'Unknown option: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

say "Deploy target: ${DEPLOY_ENV} — ${VM_NAME} (${RESOURCE_GROUP}) — ${SITE_HOST}"

# --- Preflight: fail before touching anything -------------------------------
command -v az    >/dev/null || die "Azure CLI not found. brew install azure-cli"
command -v rsync >/dev/null || die "rsync not found. brew install rsync"
az account show  >/dev/null 2>&1 || die "Not logged in to Azure. Run: az login"

[[ -f "$ENV_FILE" ]] || die "Missing ${ENV_FILE}
  Each environment ships ITS OWN env file — the root .env serves local docker
  compose only and is never deployed. Create this one from the template:

      cp ${REPO_ROOT}/.env.example ${ENV_FILE}

  then set ANTHROPIC_API_KEY (required) and OPENAI_API_KEY (optional; needed for
  voice notes)."

grep -qE '^ANTHROPIC_API_KEY=.+' "$ENV_FILE" \
  || die "ANTHROPIC_API_KEY is empty in ${ENV_FILE} — the backend will not start."

if grep -qE '^OPENAI_API_KEY=.+' "$ENV_FILE"; then
  ok "OPENAI_API_KEY set — voice notes enabled"
else
  warn "OPENAI_API_KEY empty — voice notes will answer stt_not_configured"
  warn "(the text demo is unaffected; the mic UI offers typing instead)"
fi

# --- Infrastructure ---------------------------------------------------------
if az vm show -g "$RESOURCE_GROUP" -n "$VM_NAME" >/dev/null 2>&1; then
  ok "vm ${VM_NAME} exists"
else
  say "No VM found — provisioning first (one time, several minutes)"
  bash "${ENV_DIR}/provision.sh"
fi

SSH_TARGET="${ADMIN_USER}@${FQDN}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)

# --- Wait for SSH, then for cloud-init --------------------------------------
say "Waiting for the VM to accept SSH"
for i in $(seq 1 40); do
  if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" true 2>/dev/null; then
    ok "ssh ready"
    break
  fi
  [[ $i -eq 40 ]] && die "VM never accepted SSH. Check: az vm show -g ${RESOURCE_GROUP} -n ${VM_NAME}"
  sleep 10
done

# Docker is installed by cloud-init AFTER first boot, so "SSH works" does not
# mean "docker exists".
#
# TEST DOCKER DIRECTLY rather than looking for a sentinel file. The sentinel
# used to live at ${REMOTE_DIR}/.provisioned, which the rsync below wipes with
# --delete because no such file exists locally: the FIRST deploy passed the
# check and then deleted the evidence, so every LATER deploy hung for the full
# ten-minute timeout waiting for a file it had erased itself.
#
# `docker info` is also a strictly better check than the file ever was. It
# proves the daemon is RUNNING and reachable by this user without sudo (the
# usermod -aG in cloud-init actually took effect), where a touched file only
# ever proved a script reached its last line.
say "Waiting for Docker"
for i in $(seq 1 60); do
  if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "docker info" >/dev/null 2>&1; then
    ok "docker ready"
    break
  fi
  [[ $i -eq 60 ]] && die "Docker never became usable. Inspect:
  ssh ${SSH_TARGET} 'sudo cat /var/log/cloud-init-output.log | tail -50'
  ssh ${SSH_TARGET} 'docker info'"
  sleep 10
done

# --- Ship the code ----------------------------------------------------------
# Excludes are not just noise-reduction: node_modules and .venv are host-arch
# binaries that would break inside a Linux container, and the *.db files are
# local SQLite state that must never overwrite the VM's Postgres-backed demo.
#
# deploy/*/.env are excluded so that NEITHER environment's secrets ever land on
# the other environment's VM via the source sync — the one env file this VM
# needs is written explicitly below.
say "Syncing source to ${SSH_TARGET}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.git/' \
  --exclude 'node_modules/' \
  --exclude '.venv/' \
  --exclude 'dist/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' \
  --exclude '.env' \
  -e "ssh ${SSH_OPTS[*]}" \
  "${REPO_ROOT}/" "${SSH_TARGET}:${REMOTE_DIR}/"
ok "source synced"

# --- Ship the environment ---------------------------------------------------
# .env files are excluded from the rsync above; THIS environment's file is sent
# separately, so the deployed environment is built explicitly rather than
# inherited by accident. SITE_ADDRESS and ACME_EMAIL are appended here because
# they are deployment facts (which hostname this VM answers to), not developer
# secrets — they have no business in the env file itself.
say "Writing remote environment (from deploy/${DEPLOY_ENV}/.env)"
REMOTE_ENV="$(mktemp -t elite-remote-env.XXXXXX)"
trap 'rm -f "$REMOTE_ENV"' EXIT
# PUBLIC_WEB_BASE_URL IS DERIVED, NOT COPIED (2026-07-28). It is the origin of
# the handoff link we WhatsApp to a customer, so it must be a hostname this VM
# actually serves — which is SITE_URL, by construction. Copied from a local
# .env instead, it drifts — and it HAS to differ per environment (dev must link
# to the DEV host, or a dev test hands the customer a prod link), which is now
# guaranteed structurally: each environment derives it from its own config.
#
# The local line is FILTERED OUT rather than shadowed by an appended duplicate:
# `docker compose` takes the last definition of a repeated key, but relying on
# that is a coin-flip nobody should have to look up.
{
  grep -vE '^[[:space:]]*(PUBLIC_WEB_BASE_URL|SITE_ADDRESS|ACME_EMAIL)=' "$ENV_FILE"
  echo ""
  echo "# --- appended by deploy [${DEPLOY_ENV}] (deployment facts, not secrets) ---"
  echo "SITE_ADDRESS=${SITE_ADDRESS}"
  echo "ACME_EMAIL=${ACME_EMAIL}"
  echo "PUBLIC_WEB_BASE_URL=${SITE_URL}"
} >"$REMOTE_ENV"

scp "${SSH_OPTS[@]}" -q "$REMOTE_ENV" "${SSH_TARGET}:${REMOTE_DIR}/.env"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "chmod 600 ${REMOTE_DIR}/.env"
ok "environment written (mode 600)"

# --- Build and start --------------------------------------------------------
COMPOSE="$COMPOSE_REMOTE"

say "Building images on the VM (slowest step — a few minutes on first run)"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cd ${REMOTE_DIR} && ${COMPOSE} build"
ok "images built"

say "Starting the stack"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cd ${REMOTE_DIR} && ${COMPOSE} up -d --remove-orphans"
ok "containers up"

# --- Optional: demo-morning ritual ------------------------------------------
# Runs the SAME cli command documented in CLAUDE.md, inside the backend
# container against Postgres. Destructive — hence opt-in via a flag.
if [[ $RESET_DEMO -eq 1 ]]; then
  say "Running reset-demo (drops and re-seeds the database)"
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "cd ${REMOTE_DIR} && ${COMPOSE} exec -T backend python -m app.cli reset-demo"
  ok "demo data reset"
fi

# --- Health ------------------------------------------------------------------
# The first deploy must also wait for Let's Encrypt to issue, which happens on
# Caddy's first request and takes ~10-40s. Retrying here is what turns "deploy
# finished but the site 502s for a minute" into a green check.
#
# THE AZURE FQDN IS THE ONE THAT GATES THE DEPLOY. A custom domain (prod only)
# is checked too, but only warns: its A record lives outside this repo and
# outside Azure, so a stale or mid-propagation record is not a reason to declare
# a working deploy failed on demo morning. The warning is what stops it dying
# SILENTLY, which is exactly how it went unnoticed the first time.
say "Waiting for HTTPS and the health endpoint"
for i in $(seq 1 30); do
  BODY="$(curl -fsS --max-time 10 "https://${FQDN}/api/health" 2>/dev/null || true)"
  if [[ "$BODY" == *'"api"'* ]]; then
    ok "health (${FQDN}): ${BODY}"
    HEALTHY=1
    break
  fi
  [[ $i -eq 1 ]] && printf '    waiting for certificate issuance'
  printf '.'
  sleep 5
done
echo ""

if [[ -n "$CUSTOM_DOMAIN" ]]; then
  for i in $(seq 1 24); do
    CUSTOM_BODY="$(curl -fsS --max-time 10 "https://${CUSTOM_DOMAIN}/api/health" 2>/dev/null || true)"
    [[ "$CUSTOM_BODY" == *'"api"'* ]] && break
    [[ $i -eq 1 ]] && printf '    waiting for %s certificate' "$CUSTOM_DOMAIN"
    printf '.'
    sleep 5
  done
  echo ""
  if [[ "${CUSTOM_BODY:-}" == *'"api"'* ]]; then
    ok "health (${CUSTOM_DOMAIN}): ${CUSTOM_BODY}"
  else
    warn "${CUSTOM_DOMAIN} is NOT serving yet — the Azure FQDN above is unaffected."
    warn "Check the A record points at this VM:  dig +short ${CUSTOM_DOMAIN}"
    warn "  expected: $(az vm list-ip-addresses -g "$RESOURCE_GROUP" -n "$VM_NAME" \
      --query '[0].virtualMachine.network.publicIpAddresses[0].ipAddress' -o tsv 2>/dev/null || echo '<vm public ip>')"
    warn "Then:  ssh ${SSH_TARGET} 'cd ${REMOTE_DIR} && ${COMPOSE} logs --tail 40 caddy'"
  fi
fi

if [[ "${HEALTHY:-0}" -ne 1 ]]; then
  warn "Health check did not pass yet. The stack may still be settling."
  warn "Inspect with:  ssh ${SSH_TARGET} 'cd ${REMOTE_DIR} && ${COMPOSE} logs --tail 80 caddy backend'"
else
  say "Deployed [${DEPLOY_ENV}]"
  echo ""
  printf '    \033[1;32m%s\033[0m\n' "${SITE_URL}"
  echo ""
  if [[ "$DEPLOY_ENV" == "prod" ]]; then
    echo "    This is the demo-day QR target. It is real HTTPS, so the mic works"
    echo "    on attendees' phones (voice notes capped at 30s)."
  else
    echo "    This is the DEV environment — safe to break. Real HTTPS, so the"
    echo "    mic and voice features work on phones here too."
  fi
  echo ""
  echo "    logs   : ssh ${SSH_TARGET} 'cd ${REMOTE_DIR} && ${COMPOSE} logs -f'"
  echo "    stop   : az vm deallocate -g ${RESOURCE_GROUP} -n ${VM_NAME}   # stops billing"
  echo "    start  : az vm start -g ${RESOURCE_GROUP} -n ${VM_NAME}"
fi

if [[ $FOLLOW_LOGS -eq 1 ]]; then
  say "Tailing logs (Ctrl-C to stop — this does NOT stop the site)"
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "cd ${REMOTE_DIR} && ${COMPOSE} logs -f --tail 50"
fi
