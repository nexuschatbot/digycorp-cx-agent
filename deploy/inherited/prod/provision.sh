#!/usr/bin/env bash
# One-time Azure infrastructure for the PRODUCTION environment.
# Normally you never run this by hand — prod/deploy.sh calls it when the VM is
# missing. Idempotent; safe to re-run against the existing prod VM.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${HERE}/config.sh"
source "${HERE}/../lib.sh"
source "${HERE}/../provision-core.sh"
