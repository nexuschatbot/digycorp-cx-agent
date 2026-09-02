# digycorp-cx-agent

Customer-experience agent. FastAPI backend, React frontend.

> ## 🔴 New here? Start from `test`. Never push to `test` directly.
>
> **`test` is the base branch for all work.** Branch from it, PR back into it.
> `test` deploys on merge, so a direct push ships to the live test VM unreviewed
> — every change, however small, goes through a pull request.
>
> ```bash
> git checkout test && git pull origin test
> git checkout -b fix/your-change        # ← work on a branch, never on test
> ```
>
> `elite-prod` and `general-prod` are release targets, not places to develop —
> they only ever receive a promotion PR from `test`.
>
> Full pipeline, and the failures it has already produced:
> **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

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

Branch from `test`, open a PR back into `test`, and **never commit or push to
`test` directly** — it moves only when a PR merges. After QA verifies the test
VM, a release approver promotes `test` into a production branch. Production never
receives a PR from anywhere but `test` — `branch-policy.yml` enforces this.

**How the direct-push rule is enforced.** GitHub branch protection on `test`
requires a pull request and rejects a direct push (see
[docs/DEPLOYMENT.md §1](docs/DEPLOYMENT.md)); `scripts/install-git-hooks.sh`
adds a local `pre-push` hook that stops it before it leaves your machine; and
the PR template asks you to confirm the base. `branch-policy.yml` additionally
guards the production branches — no check verifies your feature branch was cut
from `test`, so a branch cut from anywhere else is cut from code nobody has
watched run.

Full workflow and commands: [CONTRIBUTING.md](CONTRIBUTING.md).
Deployment pipeline: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

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

The backend serves liveness at **`/health`, `/` and `/api/health`** — three paths,
one check. Two deploy scripts poll two different ones, so a test reads the paths
out of the scripts and asserts the app routes each of them
(`backend/tests/test_health.py`).

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
