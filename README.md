# digycorp-cx-agent

Customer-experience agent. FastAPI backend, React frontend.

```
backend/    FastAPI service  (app/, tests/, requirements.txt)
frontend/   React + Vite SPA (src/, package.json)
scripts/    remote-deploy.sh — runs on the VM during a deploy
.github/    CI, branch policy, and deploy workflows
```

## Branches

| Branch         | Role                                  | Deploys to    |
| -------------- | ------------------------------------- | ------------- |
| `test`         | Default. All development starts here  | Test VM       |
| `elite-prod`   | Elite production release line         | Elite prod VM |
| `general-prod` | General production release line       | Gen prod VM   |

Work always flows in one direction:

```
feature branch  →  test  →  elite-prod
                        →  general-prod
```

Branch from `test`, open a PR back into `test`. After QA verifies the test VM,
a release approver promotes `test` into a production branch. Production never
receives a PR from anywhere but `test` — CI enforces this.

Full workflow, including the exact commands: [CONTRIBUTING.md](CONTRIBUTING.md).

## Local development

Backend:

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
uvicorn app.main:app --reload      # http://127.0.0.1:8000
pytest -q
```

Frontend:

```bash
cd frontend
npm install
npm run dev                        # http://localhost:5173, proxies /api to :8000
npm run build
```

## Environments

| Environment | URL | Status |
| ----------- | --- | ------ |
| test | http://digycorp-cx-test.uaenorth.cloudapp.azure.com | live |
| elite-prod | not provisioned | — |
| general-prod | not provisioned | — |

The test VM is **shut down at 20:00 and started at 10:00 Pakistan time** to save
cost. If the site is unreachable, start it with:

```bash
az vm start -g digycorp-cx-test-rg -n digycorp-cx-test-vm
```

Full details — VM specs, SSH access, services, schedules, known gaps — are in
[docs/INFRASTRUCTURE.md](docs/INFRASTRUCTURE.md).

## Deployment

Merging into `test`, `elite-prod`, or `general-prod` triggers
`.github/workflows/deploy.yml`, which builds the frontend, rsyncs the app to
that branch's Azure VM over SSH, runs `scripts/remote-deploy.sh` to install
dependencies and restart services, then polls `/health` until it returns 200.

> ⚠️ **That poll cannot succeed.** The ported app serves `/api/health` and
> nothing serves the bare path, so `remote-deploy.sh` retries fifteen times and
> exits 1 on a service that started fine. Open decision 5 in
> [docs/MIGRATION.md](docs/MIGRATION.md) — detail and the two ways out in
> [docs/OPEN-ISSUES.md](docs/OPEN-ISSUES.md#6-health-404s-so-every-deploy-fails-its-health-gate).

Each target reads its own repository secrets:

| Secret                  | Purpose                                    |
| ----------------------- | ------------------------------------------ |
| `<ENV>_HOST`            | VM hostname or public IP                   |
| `<ENV>_SSH_USER`        | SSH user on the VM                         |
| `<ENV>_SSH_KEY`         | Private key for that user                  |
| `<ENV>_DEPLOY_PATH`     | Absolute deploy directory on the VM        |
| `<ENV>_HEALTH_URL`      | Public health URL (optional; skips check)  |

where `<ENV>` is `TEST`, `ELITE_PROD`, or `GENERAL_PROD`. A deploy fails fast
and touches nothing if any required secret for its target is unset.
