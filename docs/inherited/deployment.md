# Docker & deployment — the long form

> **Spoke of `../CLAUDE.md`.** Moved out of the root file on 2026-08-05, wording
> unchanged. **The permanent rule — ALWAYS ask which target and WAIT for the
> answer — stays in root and is not repeated here as a substitute for it.**
> See also `../deploy/README.md` for the full safety model.

## Docker (AD-G)

- `elite-group-backend/Dockerfile` — python:3.11-slim, non-root user, uvicorn
  entrypoint with `--timeout-keep-alive 75` (AD-D).
- `elite-group-frontend/Dockerfile` — multi-stage: node build → nginx serving
  `dist/`, with `/api` proxied to the backend service.
- `docker-compose.yml` at `Elite-group/` root: services `backend`, `frontend`,
  `postgres` (with volume); backend gets `DATABASE_URL` pointing at the postgres
  service and `ANTHROPIC_API_KEY` from the root `.env` (never baked into images).
- Local non-Docker dev (uvicorn + `npm run dev` with SQLite) must keep working —
  Docker is additive, not a replacement.

**⚠️ `TZ: Asia/Dubai` IS SET ON `backend`, `sf-sync` AND `postgres`, AND THE THREE
MOVE TOGETHER OR NOT AT ALL (2026-08-03).** Containers default to UTC, and slot
times are stored as NAIVE wall-clock values meaning Dubai — so before this the
backend believed it was 11:00 while Dubai was 15:00, and every rule that reads the
clock was four hours out. The visible symptom was availability: `start >= now`
happily returned a "1:00 PM today" slot two hours after it had passed. The guards
were correct; the clock was wrong.

**Why all three, and why `postgres` needs `-c timezone=` rather than just the env
var.** Some columns default to the DATABASE's clock (`server_default=func.now()`)
and some to the app's — they agreed only because both were UTC, and moving one
alone opens a real 4-hour skew (this is the same class of bug the `FeedbackSurvey`
timestamp note documents, where 24h meant 19h). Two things break silently:
the console dashboard's rolling windows drift, and **the `sf-sync` watermark
(`datetime.now()` vs `bookings.updated_at`) stops matching, so nothing reaches
Salesforce with no error in any log.** On postgres the `TZ` env var is read only by
`initdb` when a cluster is FIRST created, so an existing `pgdata` volume — i.e.
every environment already running — would silently keep its old setting; the
explicit `command: ["postgres", "-c", "timezone=Asia/Dubai"]` applies on every
boot, new volume or old.

**VENUE TIME, FOR EVERY VIEWER (product decision 2026-08-03).** A slot is a
physical appointment at a UAE showroom, so "7:00 PM" must mean 7pm at the door
whether the customer is in Dubai, Karachi or London. **Nothing in this system
converts to the viewer's timezone**, and per-viewer conversion was considered and
rejected: WhatsApp confirmations and reminders have no browser and could never
follow, so a converted chat would contradict the message the customer rereads the
night before. If it is ever wanted, the honest form is DUAL display in the UI
(venue time primary, "· 8:00 PM your time" secondary, computed browser-side),
never a replacement.

**Salesforce is unaffected**, checked rather than assumed: both write paths
(`sf_sync.py`, `salesforce-setup/reset_sf_demo.py`) already stamp `+0400`
explicitly, so the instants stored in the org were always right — the org's own
timezone setting only ever changed how they were DISPLAYED (set to UAE
2026-08-03, so the SF UI now agrees with the app).

**Local bare-metal dev is NOT covered and cannot be**: `uvicorn` on the developer's
own machine runs on that machine's clock, and SQLite's `CURRENT_TIMESTAMP` is
always UTC. The skew documented on `FeedbackSurvey` therefore still applies there.
Docker is the environment this rule makes correct.

## Deployment — two separate Azure environments (2026-07-31)

Everything deployment lives under `deploy/`, split per environment; see
`deploy/README.md` for the full safety model. In brief:

- **`deploy/dev/`** → `elite-dev-vm` in `elite-dev-rg`
  (`https://elite-dev.uaenorth.cloudapp.azure.com`). Safe to break. WhatsApp is
  OFF (simulator only — the Meta number is production's), LangSmith traces to
  `elite_group_test_drive_dev`, and no custom domain, ever.
- **`deploy/prod/`** → `elite-demo-vm` in `elite-demo-rg`
  (`https://elite-group.digycorp.com` + Azure FQDN fallback). The live
  client-facing environment. Its deploy script demands a typed `prod` confirmation.
- The *logic* is shared (`deploy/lib.sh`, `deploy-core.sh`,
  `provision-core.sh` — one implementation, so environments cannot drift in
  behaviour); the *identity* (resource group, VM, DNS label, custom domain) is
  hard-coded per environment with NO env-var overrides, and `lib.sh` refuses to
  run a non-prod deploy whose config names any production resource.
- Each environment ships **its own env file** (`deploy/<env>/.env`) and **its
  own compose overlay** (`deploy/<env>/docker-compose.yml`). The root `.env`
  serves local docker compose only and is never deployed.
- `./deploy/deploy.sh` (the old single script) is a tombstone that refuses to
  run — there is no default deployment target anywhere.

**⚠️ PERMANENT RULE — DEPLOYMENT TARGET IS ALWAYS THE DEVELOPER'S CALL.
Before performing ANY deployment, Claude Code must ask which target:**

- **Development VM** (`./deploy/dev/deploy.sh`)
- **Production VM** (`./deploy/prod/deploy.sh`)

**Never assume the deployment target — not from context, not from what was just
edited, not from which environment was deployed last. Ask, then WAIT for the
developer's explicit confirmation before running any deploy command. This holds
even when the change "obviously" belongs to one environment, and it stacks on
top of (does not replace) the standing rule that no deploy runs at all without
a separate explicit go-ahead.**
