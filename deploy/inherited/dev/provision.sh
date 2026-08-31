#!/usr/bin/env bash
# One-time Azure infrastructure for the DEVELOPMENT environment.
# Normally you never run this by hand — dev/deploy.sh calls it when the VM is
# missing. Idempotent; safe to re-run.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${HERE}/config.sh"
source "${HERE}/../lib.sh"
source "${HERE}/../provision-core.sh"
