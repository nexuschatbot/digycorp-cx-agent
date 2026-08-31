# Docker — Elite Test Drive Booking (AD-G)

Container setup for the monorepo. **Additive only**: local non-Docker dev
(uvicorn + `npm run dev` with SQLite) still works exactly as before — see
`elite-group-backend/SETUP.md`. Docker adds a reproducible backend + frontend +
Postgres stack that mirrors the HLD deploy target.

## Artifacts

| File | Purpose |
|---|---|
| `elite-group-backend/Dockerfile` | python:3.11-slim, non-root, uvicorn `--timeout-keep-alive 75` (AD-D) |
| `elite-group-frontend/Dockerfile` | Node build → nginx serving `dist/` |
| `elite-group-frontend/nginx.conf` | SPA fallback + SSE-safe `/api` proxy to the backend |
| `docker-compose.yml` | backend + frontend + postgres |
| `.env.example` | root env template (copy to `.env`) |

## Prerequisites

- Docker Engine + Compose v2 (`docker compose version`).
- A root `.env` with your Anthropic key:

  ```bash
  cp .env.example .env
  # edit .env and set ANTHROPIC_API_KEY=sk-ant-...
  ```

  `.env` is gitignored and is **only** interpolated at run time — it is never
  baked into an image layer.

## Build & run

```bash
docker compose build        # build backend + frontend images
docker compose up           # start postgres → backend → frontend
# or: docker compose up -d --build   (build + detached)
```

Startup order is enforced: Postgres becomes healthy (`pg_isready`) before the
backend starts, and the backend creates its tables on first boot
(`Base.metadata.create_all`).

Stop / reset:

```bash
docker compose down         # stop containers (keeps the pgdata volume)
docker compose down -v      # also delete the Postgres volume (fresh DB)
```

## Ports

| Service | Container | Host | URL |
|---|---|---|---|
| frontend (nginx) | 80 | **8080** | http://localhost:8080 |
| backend (uvicorn) | 8000 | 8000 | http://localhost:8000 |
| postgres | 5432 | *(not published)* | reached in-network as `postgres:5432` |

Health check through the nginx proxy (the path the browser uses):

```bash
curl http://localhost:8080/api/health   # {"api":"ok","database":"ok"}
```

Or hit the backend directly: `curl http://localhost:8000/api/health`.

## Environment variables

| Variable | Where | Default (compose) | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | root `.env` → backend | *(required)* | `up` fails fast if unset. Server-side only. |
| `OPENAI_API_KEY` | root `.env` → backend | *(empty)* | Voice-note transcription. **Optional** — see below. |
| `DATABASE_URL` | set in `docker-compose.yml` | `postgresql+psycopg://elite:elite@postgres:5432/elite` | Points the backend at the Postgres service. |

The backend also reads `CLAUDE_MODEL`, `SLOT_LOCK_TTL_MINUTES`, and
`SLOT_HORIZON_DAYS` (defaults in `app/config.py`); add them to the backend
service's `environment:` block if you need to override them in Docker. The voice
knobs `STT_MODEL`, `VOICE_MAX_SECONDS`, `VOICE_MAX_BYTES` and
`VOICE_RETENTION_HOURS` work the same way.

## Voice notes in Docker (optional)

`OPENAI_API_KEY` is interpolated with `:-` rather than `:?`, so unlike the
Anthropic key it is **optional and the stack starts without it**. That is
deliberate: "no voice today" must not become "the containers will not boot" for
a feature that is purely additive. Unset, the transcribe endpoint answers
`{"ok": false, "reason": "stt_not_configured"}` and the UI offers typing.

⚠️ **The mic will NOT work through the compose stack as shipped.**
`getUserMedia` is blocked on plain HTTP everywhere except `localhost`, and the
frontend container is published on `http://localhost:5173` — which means it
works on the machine running Docker, and fails on every other device pointed at
`http://<host-ip>:5173`. There is no code fix. For a demo where attendees use
their own phones, front the stack with HTTPS (a tunnel such as
`cloudflared tunnel --url http://localhost:5173`, or a reverse proxy with a
certificate).

⚠️ **PDPL:** with the key set, customer audio leaves the UAE to a US endpoint.
On the D1 checklist. Recordings live in the Postgres volume (not on disk, not in
an image layer), are hard-deleted by "forget me", and expire after
`VOICE_RETENTION_HOURS` (48h default). `docker compose down -v` drops them with
the volume.

## SQLite local dev vs Postgres compose

Both paths use the **same schema and code** — only `DATABASE_URL` differs
(`app/db.py` applies the SQLite-only `check_same_thread` arg conditionally).

- **Local dev (SQLite):** don't use Docker. Follow `elite-group-backend/SETUP.md`
  — `uvicorn app.main:app --reload` reads `elite-group-backend/.env`, whose
  default `DATABASE_URL=sqlite:///./elite_demo.db` writes a local file.
- **Compose (Postgres):** `docker compose up`. The backend's `DATABASE_URL` is
  set to the `postgres` service in `docker-compose.yml`; the `psycopg` driver
  (in `requirements.txt`) is what makes this connection work.

To point a compose backend at SQLite instead (e.g. debugging without Postgres),
override `DATABASE_URL` for the backend service — but the default and intended
compose path is Postgres.
