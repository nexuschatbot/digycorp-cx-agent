# Inherited deploy artefacts — NOT WIRED

Ported verbatim from the Elite-group repo so nothing is lost, but deliberately
left disconnected: this repo deploys via GitHub Actions → rsync → systemd +
nginx (see ../../docs/INFRASTRUCTURE.md), while these files assume
docker-compose with four containers.

Reconciling the two is open decision 2 in ../../docs/MIGRATION.md. Until it is
decided, nothing here runs.

    docker-compose.yml        postgres + backend + frontend + sf-sync
    compose.env.example       root env compose interpolates at `up` time
    Caddyfile                 TLS/reverse proxy (target uses nginx)
    dev/, prod/               per-environment provision + deploy scripts
    deploy.sh, lib.sh, ...    shared shell helpers

The two things here with no home in the target deploy model are the **Postgres
container** and the **sf-sync sidecar** (../../sidecars/sf_sync.py).
