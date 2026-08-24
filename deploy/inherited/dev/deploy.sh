#!/usr/bin/env bash
# Deploy the working tree to the DEVELOPMENT VM.
#
#   ./deploy/dev/deploy.sh                 # sync, rebuild, restart
#   ./deploy/dev/deploy.sh --reset-demo    # ...then drop + re-seed the database
#   ./deploy/dev/deploy.sh --logs          # ...then tail logs until Ctrl-C
#
# Touches ONLY elite-dev-vm in elite-dev-rg. lib.sh refuses to run if this
# environment's config ever names a production resource.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${HERE}/config.sh"
source "${HERE}/../lib.sh"
source "${HERE}/../deploy-core.sh" "$@"
