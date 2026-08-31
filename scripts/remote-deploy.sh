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

# NGINX CONFIG IS DEPLOYED, NOT HAND-MAINTAINED (2026-08-31).
#
# It used to be hand-written on the box and version-controlled nowhere, so it
# drifted from the application the moment `app/` was ported over the scaffold
# and no diff could show it. What that cost, all of it invisible until someone
# curled the server: `proxy_pass .../` with a trailing slash rewrote the prefix
# away so EVERY /api route 404ed; no `proxy_buffering off` so SSE token
# streaming stalled; no /media block so vehicle images rendered broken; no
# WebSocket blocks so voice and human takeover died at the proxy; and nginx's
# default 1 MB body limit rejected voice notes before the app could see them.
NGINX_SRC="${DEPLOY_PATH}/deploy/nginx"
if [ -d "${NGINX_SRC}" ]; then
  echo "==> Installing nginx config"
  sudo install -D -m 0644 "${NGINX_SRC}/digycorp-app.conf" \
    /etc/nginx/snippets/digycorp-app.conf

  # THE TLS BLOCK ONLY WHEN THERE IS A CERTIFICATE. nginx will not start with
  # an `ssl_certificate` that does not exist, and certbot's HTTP-01 challenge
  # needs nginx already up — so a fresh box comes up on HTTP, gets its
  # certificate through that, and gains HTTPS on the next deploy.
  CERT_DIR="/etc/letsencrypt/live/digycorp-cx-test.uaenorth.cloudapp.azure.com"
  if [ -f "${CERT_DIR}/fullchain.pem" ]; then
    echo "==> Certificate present — enabling HTTPS"
    cat "${NGINX_SRC}/site-http.conf" "${NGINX_SRC}/site-tls.conf" \
      | sudo tee /etc/nginx/sites-available/digycorp-cx-agent >/dev/null
  else
    echo "==> No certificate yet — HTTP only (run certbot, then redeploy)"
    sudo install -m 0644 "${NGINX_SRC}/site-http.conf" \
      /etc/nginx/sites-available/digycorp-cx-agent
  fi

  # TESTED BEFORE RELOADED. A bad config makes `nginx -t` fail and the running
  # server keep serving; reloading blindly is how a config typo becomes an
  # outage. Verified the hard way — `http2 on;` is nginx >= 1.25 and this box
  # is 1.18, and only the test caught it.
  if sudo nginx -t; then
    echo "==> Reloading ${WEB_SERVICE}"
    sudo systemctl reload "${WEB_SERVICE}" || sudo systemctl restart "${WEB_SERVICE}"
  else
    echo "!! nginx config rejected — leaving the running config in place" >&2
    exit 1
  fi
else
  echo "==> Reloading ${WEB_SERVICE}"
  sudo systemctl reload "${WEB_SERVICE}" || sudo systemctl restart "${WEB_SERVICE}"
fi

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
