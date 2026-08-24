#!/usr/bin/env bash
# Deploy the working tree to the PRODUCTION VM (elite-demo-vm — the live demo).
#
#   ./deploy/prod/deploy.sh                 # asks for confirmation, then deploys
#   ./deploy/prod/deploy.sh --yes           # skip the confirmation prompt
#   ./deploy/prod/deploy.sh --reset-demo    # ...then drop + re-seed the database
#   ./deploy/prod/deploy.sh --logs          # ...then tail logs until Ctrl-C
#
# THE CONFIRMATION PROMPT IS THE POINT OF THIS FILE. Production is the live
# client demo; every deploy hits it. A prod deploy must be a decision, never
# muscle memory — so this wrapper makes you type the word before anything runs.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${HERE}/config.sh"
source "${HERE}/../lib.sh"

# Filter --yes out of the args we pass through; deploy-core does not know it.
CONFIRMED=0
ARGS=()
for arg in "$@"; do
  case "$arg" in
    --yes) CONFIRMED=1 ;;
    *)     ARGS+=("$arg") ;;
  esac
done

if [[ $CONFIRMED -ne 1 ]]; then
  warn "Target: PRODUCTION — ${VM_NAME} (${RESOURCE_GROUP}), serving ${SITE_HOST}"
  warn "This is the live client demo."
  printf "    Type 'prod' to continue: "
  read -r REPLY || die "No interactive terminal. Pass --yes to deploy to production non-interactively."
  [[ "$REPLY" == "prod" ]] || die "Aborted — production deploy not confirmed."
fi

# ${ARGS[@]+...}: bash 3.2 (macOS default) treats expanding an EMPTY array as an
# unbound variable under set -u; this idiom expands to nothing instead of dying.
source "${HERE}/../deploy-core.sh" ${ARGS[@]+"${ARGS[@]}"}
