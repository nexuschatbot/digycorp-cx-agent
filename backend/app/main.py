"""FastAPI application — CORS, table creation on startup, and route assembly.

Wires the chat SSE stream, the direct-path REST routes (conversation, vehicles,
bookings, intents, voice, speech), the admin console, and the liveness routes
GET /api/health, GET /health and GET / — see health_root for why there are three.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import (
    bookings_router,
    brochure_router,
    call_router,
    chat_router,
    console_router,
    conversation_router,
    feedback_router,
    insights_router,
    intents_router,
    speech_router,
    takeover_router,
    vehicles_router,
    voice_router,
    whatsapp_router,
)
from app.config import (
    NOTIFY_DISPATCH_ENABLED,
    NOTIFY_POLL_SECONDS,
    PREWARM_PROMPT_CACHE,
    RETRIEVAL_ENABLED,
    RTC_DEBUG,
)
from app.db import SessionLocal

# Import models so their tables are registered on Base.metadata before the
# migration runs — app/migrate.py compares against it to decide whether this is
# a fresh database or a create_all one being adopted.
from app import migrate, models  # noqa: F401

# uvicorn configures only its own loggers, so the app's INFO logs (agent
# iterations, AD-E aborts) would go nowhere. Attach a stream handler to the "app"
# logger so operational events — including "graph stopped on client abort" — are
# visible in the server output.
_app_log = logging.getLogger("app")
if not _app_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
    _app_log.addHandler(_h)
    _app_log.setLevel(logging.INFO)
    _app_log.propagate = False

# ICE DIAGNOSTICS (RTC_DEBUG, 2026-08-04). aioice logs every connectivity check
# and its verdict at DEBUG; nothing else can say WHICH candidate pair was tried
# and why it failed. Without it a call that never reaches `connected` looks the
# same whether the pairs were never formed, never sent, or sent and rejected —
# which is how this bug survived three rounds of diagnosis by inference.
#
# OFF BY DEFAULT and deliberately not left on: it is per-packet chatty, and the
# check lines carry the customer's public IP unredacted (unlike our own summary
# line, which cuts the last octet). Turn it on to diagnose, then turn it off.
if RTC_DEBUG:
    _ice_log = logging.getLogger("aioice")
    if not _ice_log.handlers:
        _ih = logging.StreamHandler()
        _ih.setFormatter(logging.Formatter("DEBUG:    ice: %(message)s"))
        _ice_log.addHandler(_ih)
    _ice_log.setLevel(logging.DEBUG)
    _ice_log.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: bring the schema to head (T0.3). This REPLACED
    # `Base.metadata.create_all`, which created missing tables and silently did
    # nothing else — no added columns, no changed nullability, no error. A
    # database that already exists is adopted by stamping rather than rebuilt;
    # see app/migrate.py. Deliberately NOT wrapped in a try: a schema the code
    # disagrees with must stop the boot, not become a runtime surprise.
    migrate.upgrade_to_head()
    # Then, if enabled, write the cached prompt prefix so the first customer of
    # the demo reads it rather than creating it. Off by default — one live API
    # call per boot. Imported here, not at module scope, so importing app.main
    # never pulls in the agent's SDK client.
    if PREWARM_PROMPT_CACHE:
        from app.agent.graph import prewarm_cache

        prewarm_cache()

    # Prompt versions (T2.3/T2.4): verify every live version BEFORE serving, and
    # record an audit row if the activation changed since last boot.
    #
    # LOUD, NOT FATAL. In Phase 2 v1 is the first version, so there is no
    # previous one to fall back to and refusing the boot would help nobody. The
    # gate's job here is that a broken prompt is IMPOSSIBLE TO MISS in the logs;
    # from Phase 5, when versions accumulate, the fallback becomes real.
    try:
        from app.agents import gate as _prompt_gate

        _reports = _prompt_gate.enforce()
        if all(r.ok for r in _reports.values()):
            from app.db import SessionLocal as _S

            with _S() as _s:
                if _prompt_gate.record_activation(_s):
                    _s.commit()
    except Exception:  # noqa: BLE001
        _app_log.exception("prompt version check failed — serving whatever loaded")

    # Retrieval index (T1.4): build or fetch the brochure and knowledge vectors
    # so the first customer does not pay the ~14s embedding pass. Gated on the
    # same flag as the rest of retrieval — with it off nothing here runs, and
    # the 113MB model is never even loaded.
    #
    # WRAPPED, unlike the migration above. A schema the code disagrees with must
    # stop the boot; a cold index must not. Retrieval degrades to the catalogue,
    # which is the pre-Phase-1 behaviour.
    if RETRIEVAL_ENABLED:
        try:
            from app.retrieval import index as _retrieval_index

            _app_log.info("retrieval index: %s", _retrieval_index.warm())
        except Exception:  # noqa: BLE001
            _app_log.exception("retrieval index could not be warmed — degrading")

    # Notification dispatcher (AD-F): the in-process APScheduler that reads
    # Notification.send_at and sends due WhatsApp rows (confirmation + T-2h
    # reminder) via services/dispatcher. OFF by default so unit tests and plain
    # dev never auto-dispatch — the demo enables NOTIFY_DISPATCH_ENABLED in .env.
    # Imported here, not at module scope, so importing app.main stays cheap and
    # apscheduler is only required when the dispatcher is actually turned on.
    # Salesforce read path: pre-warm caches + start the background refresher
    # so the live voice call never waits on a Salesforce round-trip mid-turn.
    try:
        from app.services import salesforce
        if salesforce.enabled():
            salesforce.prewarm()
            salesforce.start_background_refresh()
    except Exception:
        _app_log.warning("salesforce prewarm/refresh not started", exc_info=True)

    scheduler = None
    if NOTIFY_DISPATCH_ENABLED:
        from apscheduler.schedulers.background import BackgroundScheduler

        from app.services.dispatcher import run_dispatch_tick

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            run_dispatch_tick,
            "interval",
            seconds=NOTIFY_POLL_SECONDS,
            id="notify_dispatch",
            max_instances=1,  # never overlap ticks
            coalesce=True,  # a backlog collapses to one run, not a burst
        )
        scheduler.start()
        _app_log.info("notification dispatcher started (every %ss)", NOTIFY_POLL_SECONDS)

    yield

    # Shutdown: stop the scheduler cleanly if it was started.
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Elite Test Drive Booking API", lifespan=lifespan)

# Dev CORS: the Vite frontend proxies /api, but allow direct calls too.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# POST /api/chat — SSE agent stream (AD-A/AD-B).
app.include_router(chat_router)

# WS /api/call/{session_id} — the voice-call gateway (voice phase). The ONLY
# WebSocket in the system (AD-C amendment: WS exists for this channel alone;
# chat stays SSE). NOT a second Aisha: it wraps the same run_turn as chat, so
# same graph, same tools, same audit choke point — with STT in front and TTS
# behind. Flagged by CALL_ENABLED, off by default.
app.include_router(call_router)

# GET/POST /api/conversation/... — reload-resume + "New chat" close (rule 19).
# The DIRECT path: same services as the agent's tools, no LLM in the loop.
app.include_router(conversation_router)

# WS /api/takeover/{session_id} — human takeover of an escalated session
# (2026-08-31). The second WebSocket after the call gateway, and for the same
# reason SSE could not serve: a human reply must reach a customer who is only
# waiting, holding no request open. No LLM anywhere on this path.
app.include_router(takeover_router)

# GET /api/vehicles/{id} — full detail for the UI dialog, both languages.
# Also the DIRECT path: browsing a car is fully determined by its id, so it
# never needs the model. Costs zero tokens.
app.include_router(vehicles_router)

# GET /api/booking/{ref} — one booking, for rebuilding a booking card after a
# reload (the transcript stores refs, never a frozen card). The DIRECT path too,
# but with a twist the others do not have: `session_id` names the VIEWER, read
# from the checkpoint, and decides whether the customer's name and mobile are in
# the response at all. See services/booking._booking_dict.
app.include_router(bookings_router)

# POST /api/intent/... — tap intents that COMMIT state (slot lock, consent,
# licence ack). The DIRECT path: the tap already carries the answer, so it never
# reaches the model. Audited as actor=customer; the agent's checkpoint is
# updated with a synthetic tool pair so its memory matches reality.
app.include_router(intents_router)
app.include_router(feedback_router)
app.include_router(insights_router)
# C3: the model-summary PDF. Public and unauthenticated by design — the link
# is sent over WhatsApp and must open on a phone with no session, no cookie
# and no app. It carries no personal data. See api/brochure.py.
app.include_router(brochure_router)

# POST /api/voice/transcribe + GET /api/audio/{id} — voice notes, INPUT ONLY
# (2026-07-21 scope override). Also the DIRECT path, and pointedly so: there is
# no transcription tool, so the model can neither ask for a transcription nor
# reach a recording. STT runs to completion BEFORE /api/chat is called, and what
# arrives there is an ordinary `message` string — which is why none of the agent,
# the tools, the SSE contract or AD-C had to change.
app.include_router(voice_router)

# GET /api/speech/{turn_id} — read one of Aisha's replies aloud (2026-07-21).
# The DIRECT path, and pointedly keyed on a TURN rather than on text: everything
# spoken in her voice is something she actually said and is in the transcript to
# prove it, and no caller can use this as a free text-to-speech proxy.
app.include_router(speech_router)

# GET/POST /api/whatsapp/... — INBOUND WhatsApp (2026-07-22): the Meta webhook
# and the reply-with-a-link handoff into the web chat. The DIRECT path (no LLM):
# a customer's WhatsApp message is identified and answered with a one-time link;
# opening it mints a web session already bound to that customer in the checkpoint.
# Dormant unless WHATSAPP_INBOUND_ENABLED — the route registers regardless so the
# Meta callback URL is stable, but accepts/mints nothing until the demo enables it.
app.include_router(whatsapp_router)

# GET /api/console/... — the admin console (2026-07-21). Read-only: the three
# endpoints the API contract has specified since the console was scoped (audit,
# metrics, notifications) plus the data browser added alongside them. Thin
# wrappers over services/console.py like every other route (AD-I).
# ⚠️ UNGATED by explicit decision — /customers and /bookings expose names and
# +971 mobiles to anyone who can reach the host. See the router's docstring and
# the D1 PDPL checklist.
app.include_router(console_router)

# Vehicle imagery (rule 18): the seed media dir is served read-only at /media, so
# get_vehicle_details' grounded /media/... URLs load in the browser. Backend owns
# both the catalogue data AND the bytes (AD-I); no image is copied into the
# frontend. Mounted only if present so tests/CI without the media dir still boot.
_MEDIA_DIR = Path(__file__).resolve().parent.parent / "seed" / "media"
if _MEDIA_DIR.is_dir():
    app.mount("/media", StaticFiles(directory=str(_MEDIA_DIR)), name="media")


# THE NAME THIS SERVICE ANSWERS TO (open decision 5, settled 2026-08-31).
#
# Not a contradiction of `FastAPI(title="Elite Test Drive Booking API")` above:
# that title is the human-facing label on the OpenAPI docs, and it names the
# PRODUCT. This names the DEPLOYED SERVICE, and everything around it already
# agrees — the repository, the deploy path, `digycorp-cx-test-vm`,
# `digycorp-cx-test-rg`. A machine asking "what is running here" gets the
# machine's answer; a person reading /docs gets Elite's.
_SERVICE_NAME = "digycorp-cx-agent"


def _liveness() -> dict[str, str]:
    """Report API liveness plus a real database round-trip.

    The DB check runs `SELECT 1` against a live session (not just "the engine
    exists"). Any failure is reported as an error state — the endpoint must not
    crash if the database is unreachable.

    ALWAYS HTTP 200, INCLUDING WHEN THE DATABASE IS DOWN, which is the contract
    this endpoint has always had and is deliberately not changed here. Both
    deploy gates treat any non-2xx as "roll back": `remote-deploy.sh` with
    `curl -sf`, the workflow by comparing against 200. Reporting a transient DB
    blip as a failed deploy would turn a database problem into a deployment
    problem, and the two want different people. `status` carries the honest
    answer for anything that reads the body.
    """
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "api": "ok", "database": "ok"}
    except Exception as exc:  # noqa: BLE001 — surface any DB failure gracefully
        return {
            "status": "degraded",
            "api": "ok",
            "database": "error",
            "detail": str(exc),
        }
    finally:
        db.close()


@app.get("/api/health")
def health() -> dict[str, str]:
    """Liveness, under /api with every other route.

    `status` was ADDED here on 2026-08-31 and nothing was removed: Elite's
    inherited `deploy/inherited/deploy-core.sh` polls this path and reads the
    body, so `api` and `database` keep their meaning exactly.
    """
    return _liveness()


@app.get("/health")
def health_root() -> dict[str, str]:
    """The same liveness check, at the bare path (added 2026-08-31).

    TWO SCRIPTS POLL TWO DIFFERENT PATHS AND BOTH ARE LOAD-BEARING:

        scripts/remote-deploy.sh:46        /health       ← this repo's deploy
        deploy/inherited/deploy-core.sh    /api/health   ← Elite's, inherited

    S1 replaced `app/` wholesale with Elite's ported application, which serves
    its health route under `/api`. The scaffold's deploy script and its tests
    survived that port; the scaffold's ROUTE did not. Nothing ran a deploy
    afterwards, so nothing noticed — `remote-deploy.sh` retried fifteen times
    and exited 1 on a service that had started perfectly.

    SERVING BOTH IS THE FIX, NOT CHOOSING ONE. The workflow's own gate reads
    `${ENV}_HEALTH_URL` from a repository secret (deploy.yml:47), so no change
    to a script here can guarantee that gate points somewhere real. A route that
    answers on both paths is correct whichever the secret names.
    """
    return _liveness()


@app.get("/")
def root() -> dict[str, str]:
    """What is running here, for anything that asks the bare origin.

    Nothing in the frontend calls this — the SPA is served by nginx, not by
    FastAPI. It exists for the human and the uptime check that land on the
    origin and currently get a 404 from a healthy service.
    """
    return {"service": _SERVICE_NAME, "status": "ok"}
