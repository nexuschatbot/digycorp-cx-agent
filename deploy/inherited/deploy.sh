#!/usr/bin/env bash
# TOMBSTONE (2026-07-31). This used to be THE deploy script, and it deployed to
# production by default. There are now two environments, and the target must be
# an explicit choice — muscle-memory `./deploy/deploy.sh` no longer reaches prod.
cat >&2 <<'EOF'
This script no longer deploys anywhere. Pick a target explicitly:

    ./deploy/dev/deploy.sh     # DEVELOPMENT — elite-dev-vm  (safe to break)
    ./deploy/prod/deploy.sh    # PRODUCTION  — elite-demo-vm (the live demo;
                               #               asks for confirmation first)

See deploy/README.md for the environment layout and safety model.
EOF
exit 2
