#!/usr/bin/env bash
# Run the backend test suite against the SAME image production runs.
#
# WHY A THROWAWAY CONTAINER. pytest is not in the production image and should not
# be: the Dockerfile ships app/ and seed/ but not tests/, so the deployed
# container could not run them regardless. This mounts the repo over /app, adds
# the dev dependencies to a container that is deleted on exit, and runs there —
# same Python, same installed versions, no test runner in the image anyone
# deploys.
#
# Usage:  ops/run-tests.sh              # everything
#         ops/run-tests.sh tests/test_recognition.py -x
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${TEST_IMAGE:-elite-backend}"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Image '$IMAGE' not found. Build it first (docker compose build backend)," >&2
  echo "or set TEST_IMAGE to one that exists." >&2
  exit 1
fi

exec docker run --rm \
  -v "$REPO":/app -w /app \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e ANTHROPIC_API_KEY=test-key-not-used \
  -e DATABASE_URL=sqlite:// \
  "$IMAGE" \
  sh -c "pip install --quiet --no-cache-dir -r requirements-dev.txt && python -m pytest ${*:-tests} -q"
