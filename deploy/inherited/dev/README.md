# Development environment

Safe to break. Everything here targets `elite-dev-vm` in `elite-dev-rg` — its
own resource group, so nothing in it can touch production (see the safety model
in `deploy/README.md`).

```bash
./deploy/dev/deploy.sh                 # sync working tree, rebuild, restart
./deploy/dev/deploy.sh --reset-demo    # ...then drop + re-seed the database
./deploy/dev/deploy.sh --logs          # ...then tail logs
```

- URL: **https://elite-dev.uaenorth.cloudapp.azure.com** (real HTTPS — mic and
  voice features work on phones)
- Env file shipped to the VM: `deploy/dev/.env` — WhatsApp is **off** (simulator
  only) and LangSmith traces go to the separate `elite_group_test_drive_dev`
  project. Those differences are deliberate; don't "sync" them from prod.
- Compose overlay: `deploy/dev/docker-compose.yml` (intentionally identical to
  prod's today; separate file so editing this stack can never change prod).
- No custom domain, ever — `lib.sh` enforces it.

Cost: deallocate when not in use (keeps disk, data, certificate and FQDN):

```bash
az vm deallocate -g elite-dev-rg -n elite-dev-vm
az vm start      -g elite-dev-rg -n elite-dev-vm
```
