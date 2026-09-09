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

  # THE RENEWAL HOOK. certbot renews the certificate and never reloads nginx,
  # which holds ssl_certificate in memory — the hook's own header has the full
  # story. Installed from the repo on every deploy so a reprovisioned box cannot
  # come back without it: a hook that lives only on the VM is precisely the
  # hand-written drift the header of site-http.conf is about.
  # Deliberately NOT gated on the certificate existing — it is inert without one
  # (certbot only runs deploy hooks after an actual renewal), and that is one
  # less ordering rule to get wrong the first time a fresh box is issued one.
  HOOK_SRC="${DEPLOY_PATH}/deploy/letsencrypt/reload-nginx.sh"
  if [ -f "${HOOK_SRC}" ]; then
    sudo install -D -m 0755 "${HOOK_SRC}" \
      /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
  fi

  # THE TLS BLOCK ONLY WHEN THERE IS A CERTIFICATE. nginx will not start with
  # an `ssl_certificate` that does not exist, and certbot's HTTP-01 challenge
  # needs nginx already up — so a fresh box comes up on HTTP, gets its
  # certificate through that, and gains HTTPS on the next deploy.
  # `sudo test`, NOT `[ -f ]`. This script runs as the deploy user, and
  # certbot makes /etc/letsencrypt/live and /archive mode 0700 root — so a
  # plain existence check is FALSE for a certificate that is plainly there.
  # Found by shipping it: the first run printed "No certificate yet" on a box
  # already serving HTTPS and installed the HTTP-only config over a working
  # TLS one, taking the site off 443 while reporting success.
  CERT_DIR="/etc/letsencrypt/live/digycorp-cx-test.uaenorth.cloudapp.azure.com"
  if sudo test -f "${CERT_DIR}/fullchain.pem"; then
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

# 120 SECONDS, NOT 30, AND IT IS MEASURED (2026-08-31).
#
#     Started Digycorp CX Agent API   18:15:24
#     Application startup complete    18:16:04   -> 40s
#
# The old `seq 1 15` with a 2s sleep allowed about thirty, which was ample
# while the box had no .env and therefore did almost nothing at boot. A
# CONFIGURED server is slower, and for good reasons: PREWARM_PROMPT_CACHE
# makes eight live prefix calls (~29s of that 40) and RETRIEVAL_ENABLED loads
# the 118 MB ONNX model and rebuilds the index (~9s). Both are wanted.
#
# So the timeout has to bound the SLOWEST HONEST BOOT, not the fastest — a cold
# start on a 2 vCPU box with a cold prompt cache is the case that matters, and
# failing it rolls back a deploy that was fine. Waiting longer costs nothing
# when the server is healthy, because the loop exits on the first 200.
echo "==> Waiting for the API to come up (up to 120s)"
for attempt in $(seq 1 60); do
  if curl -sf --max-time 5 http://127.0.0.1:8000/health >/dev/null; then
    echo "==> API healthy after ~$(( attempt * 2 ))s"
    echo "==> Deploy to ${ENVIRONMENT} complete"
    exit 0
  fi
  sleep 2
done

echo "!! API did not become healthy within 120s. Recent logs:" >&2
sudo journalctl -u "${API_SERVICE}" -n 50 --no-pager >&2 || true
exit 1
