#!/usr/bin/env bash
# Runs ON the Azure VM, invoked over SSH by .github/workflows/deploy.yml.
#
# Contract:
#   $1          absolute deploy path on the VM (e.g. /opt/digycorp-cx-agent)
#   ENVIRONMENT test | elite-prod | general-prod
#
# Assumes two systemd units exist on the VM. Rename here if yours differ:
#   digycorp-api.service   uvicorn serving backend/
#   nginx                  serving frontend/dist/
set -euo pipefail

DEPLOY_PATH="${1:?deploy path is required}"
ENVIRONMENT="${ENVIRONMENT:-unknown}"

API_SERVICE="digycorp-api"
WEB_SERVICE="nginx"

echo "==> Deploying to ${ENVIRONMENT} at ${DEPLOY_PATH}"

cd "${DEPLOY_PATH}/backend"

# Virtualenv lives outside the rsync target so --delete never wipes it.
VENV="${DEPLOY_PATH}/venv"
if [ ! -d "${VENV}" ]; then
  echo "==> Creating virtualenv at ${VENV}"
  python3 -m venv "${VENV}"
fi

echo "==> Installing backend dependencies"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet -r requirements.txt

# Placeholder for the migration step. Uncomment once a DB and Alembic exist.
# echo "==> Running database migrations"
# "${VENV}/bin/alembic" upgrade head

echo "==> Restarting ${API_SERVICE}"
sudo systemctl restart "${API_SERVICE}"

echo "==> Reloading ${WEB_SERVICE}"
sudo systemctl reload "${WEB_SERVICE}" || sudo systemctl restart "${WEB_SERVICE}"

echo "==> Waiting for the API to come up"
for attempt in $(seq 1 15); do
  if curl -sf --max-time 5 http://127.0.0.1:8000/health >/dev/null; then
    echo "==> API healthy on attempt ${attempt}"
    echo "==> Deploy to ${ENVIRONMENT} complete"
    exit 0
  fi
  sleep 2
done

echo "!! API did not become healthy. Recent logs:" >&2
sudo journalctl -u "${API_SERVICE}" -n 50 --no-pager >&2 || true
exit 1
