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

# THE INTERPRETER, AND WHY IT IS PINNED HERE (2026-08-31).
#
# This VM is Ubuntu 22.04, whose `python3` is 3.10. The application needs 3.12
# — `.github/workflows/ci.yml` pins it, and 3.10 cannot resolve
# `onnxruntime==1.28.0` at all: pip offers it nothing newer than 1.23.2 and the
# install dies before a single file is written. Keep this in step with ci.yml;
# two different Pythons testing and running the same code is how a green build
# ships a broken server.
REQUIRED_PY="3.12"
PYTHON="python${REQUIRED_PY}"

if ! command -v "${PYTHON}" >/dev/null 2>&1; then
  echo "==> ${PYTHON} not present — installing it"
  # deadsnakes is the only source of 3.12 for jammy; 24.04 would carry it
  # natively, which is the better long-term answer (see docs/OPEN-ISSUES.md).
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -qq
  sudo apt-get install -y -qq software-properties-common
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -qq
  # -venv is a SEPARATE package on Debian/Ubuntu and `python3.12 -m venv` fails
  # without it; -dev is here for any sdist that has to compile.
  sudo apt-get install -y -qq "${PYTHON}" "${PYTHON}-venv" "${PYTHON}-dev"
fi

# Virtualenv lives outside the rsync target so --delete never wipes it.
VENV="${DEPLOY_PATH}/venv"

# REBUILT WHEN ITS INTERPRETER IS THE WRONG ONE, not merely when it is absent.
# The venv on this box was created by a scaffold-era deploy and is 3.10; a
# `[ ! -d ]` check leaves it in place forever and every deploy fails the same
# way. A venv is disposable — requirements.txt is the source of truth — so
# replacing it costs one slower deploy and nothing else.
venv_py="$("${VENV}/bin/python" -V 2>/dev/null || true)"
case "${venv_py}" in
  *"${REQUIRED_PY}"*)
    echo "==> Reusing virtualenv (${venv_py})"
    ;;
  *)
    if [ -n "${venv_py}" ]; then
      echo "==> Virtualenv is ${venv_py}, need ${REQUIRED_PY} — rebuilding"
    else
      echo "==> Creating virtualenv at ${VENV}"
    fi
    rm -rf "${VENV}"
    "${PYTHON}" -m venv "${VENV}"
    ;;
esac

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
