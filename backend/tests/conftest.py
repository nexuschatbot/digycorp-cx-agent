"""Test fixtures: an in-memory DB loaded from the REAL seed fixtures.

Tests run against seed/vehicle_models.json and seed/showrooms.json rather than
hand-made rows, so a test that passes here is a claim about the catalogue the
demo actually ships — if the seed data drifts, the tests notice.

Slots follow the same principle one step further: the `slots` fixture GENERATES
inventory through slots.generate() rather than inserting hand-made rows, so the
calendar rules (FR-SL-02, S-22) and the (showroom, vehicle) grain that the demo
runs on are the ones under test.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import app.config as _config_module
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import FuelType, LocationType, Showroom, Slot, SlotStatus, VehicleModel
from app.services import slots as slots_service

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def _load(name: str) -> list[dict]:
    return json.loads((SEED_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# NO OUTBOUND NETWORK, ENFORCED (added during the migration, 2026-08-23)
# --------------------------------------------------------------------------- #
# _hermetic_demo_toggles below removes the CREDENTIALS a vendor call would need.
# This removes the CALL. The two are different guarantees: blanking a key stops
# the paths we thought of, while this stops the ones we did not — a new import
# that reads its key straight from os.environ, a transitive dependency that
# phones home, a future test that forgets to stub.
#
# Loopback stays open: the WebRTC/aiortc and websockets tests bind and dial local
# sockets, and TestClient is in-process. AF_UNIX addresses (plain strings) pass
# through untouched. Anything else raises, naming the host, so the failure says
# what tried to leave rather than hanging on a connect timeout.
#
# A test that genuinely needs the network does not exist in this suite. If one
# ever does, it should monkeypatch this off inside its own scope and say why.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


# --------------------------------------------------------------------------- #
# THE SUITE MUST NOT TOUCH THE REAL CHECKPOINT FILE (added 2026-08-24)
# --------------------------------------------------------------------------- #
# AGENT_CHECKPOINT_PATH defaults to ./agent_checkpoints.db — a real file in the
# repo root — and six modules import it BY VALUE at import time. Nothing pinned
# it for tests, so every test that ran a turn wrote to the same SQLite file a
# locally running server holds open.
#
# HOW THIS SHOWED UP: a full suite that takes 165s alone took 3h 3m with a dev
# server running. Not a hang and not a failure — WAL lock contention, invisible
# except as a wall-clock number nobody reads. It also means the suite could
# mutate real conversation state, and that a developer's live session could
# perturb a test.
#
# Patched per test on the MODULES, not on app.config: `from app.config import
# AGENT_CHECKPOINT_PATH` binds the value at import, so rebinding config alone
# would change nothing.
_CHECKPOINT_CONSUMERS = (
    "app.agent.runner",
    "app.api.bookings",
    "app.api.intents",
    "app.api.whatsapp",
    "app.api.call_realtime",
)


@pytest.fixture(autouse=True)
def _isolated_checkpoint(monkeypatch, tmp_path):
    """Point every checkpoint consumer at a throwaway file for this test."""
    import importlib

    path = str(tmp_path / "agent_checkpoints.db")
    for name in _CHECKPOINT_CONSUMERS:
        try:
            module = importlib.import_module(name)
        except Exception:
            continue
        if hasattr(module, "AGENT_CHECKPOINT_PATH"):
            monkeypatch.setattr(module, "AGENT_CHECKPOINT_PATH", path)
    monkeypatch.setattr(_config_module, "AGENT_CHECKPOINT_PATH", path)


@pytest.fixture(autouse=True)
def _no_outbound_network(monkeypatch):
    """Fail any test that tries to open a non-loopback socket."""
    import socket as _socket

    real_connect = _socket.socket.connect
    real_connect_ex = _socket.socket.connect_ex

    def _check(address):
        # AF_UNIX and anything not (host, port): not our business.
        if not isinstance(address, tuple) or not address:
            return
        host = address[0]
        if isinstance(host, str) and host not in _LOOPBACK:
            raise RuntimeError(
                f"outbound network blocked in tests: attempted connection to "
                f"{host!r}. The suite is offline by design — stub the call, or "
                f"monkeypatch tests/conftest.py::_no_outbound_network in scope."
            )

    def guarded_connect(self, address, *args, **kwargs):
        _check(address)
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        _check(address)
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(_socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(_socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _hermetic_demo_toggles(monkeypatch):
    """Reset the dev .env's DEMO toggles to their CODE defaults for every test.

    The .env carries demo-only switches — WHATSAPP_ENABLED=true (real sends) and
    STRICT_UAE_MOBILE=false (accept an international test number) — and config
    reads them at import. Left in place they leak into the suite: any test
    reaching services/whatsapp.send_text would fire a REAL WhatsApp message to a
    real phone, and the mobile-validation tests (test_customers) would see a
    landline parse as a valid E.164 number, losing the very `reason` they assert.

    So the suite is made hermetic to these toggles: WhatsApp OFF (send_text
    short-circuits to `skipped`, no network) and STRICT mobile ON (the code
    default the validation tests are written against). A test that genuinely
    needs either flipped — the inbound-handoff tests need STRICT off for +92 —
    monkeypatches it back within its own scope.

    LANGSMITH TRACING IS FORCED OFF HERE FOR THE SAME REASON, and it needs one
    more step than the others. The LangSmith vars live in the ROOT .env (which
    deploy.sh ships to the VM) and not in elite-group-backend/.env, so today the
    suite never sees them — this makes that a GUARANTEE rather than a filing
    convention, because the failure is silent and expensive: langchain-core
    instruments LangGraph automatically, so every scripted graph test would
    export a trace of a fake conversation to a live US endpoint. That is both a
    vendor call from the offline suite (forbidden — see CLAUDE.md) and noise in
    the project you actually read on demo morning.

    THE ENV IS WHAT MATTERS, NOT config.LANGSMITH_TRACING. Our flag only decides
    whether the Anthropic client gets wrapped; the langsmith SDK reads the
    environment ITSELF, so both are set. `get_env_var` is lru_cached, so the
    cache is cleared too — otherwise a value read before the first test would
    outlive this fixture. The API key is removed as the backstop: with no
    credential, even an unforeseen path that re-enables tracing has nothing to
    export with.
    """
    from app import config as _config

    # ROUTING OFF FOR THE PORTED SUITE, DELIBERATELY.
    #
    # These 1000+ tests are the SINGLE-AGENT contract: they were written against
    # one combined prompt and the whole tool registry, and that contract is
    # exactly what the migration must not break. Left on, the pre-filter would
    # quietly re-point a test like "I want to cancel my booking" at
    # BookingManagement's shorter prompt and 13 tools — the test would still
    # pass or fail, but it would no longer be testing what it was written to
    # test, which is worse than either outcome.
    #
    # The ROUTED path has its own tests (test_router.py, test_routed_turn.py),
    # which turn this back on inside their own scope with a stubbed classifier.
    monkeypatch.setattr(_config, "ROUTER_ENABLED", False)
    monkeypatch.setattr(_config, "WHATSAPP_ENABLED", False)
    monkeypatch.setattr(_config, "STRICT_UAE_MOBILE", True)

    # THE STREAMING STT RELAY IS FORCED OFF THE SAME WAY (P3). make_transcriber
    # keys on the module's own OPENAI_API_KEY copy, and the dev .env carries a
    # real key — without this, any test that opens a call WebSocket would dial
    # wss://api.openai.com from the offline suite (forbidden, see CLAUDE.md).
    # Relay tests inject a scripted transcriber through the factory seam and
    # never need the key.
    from app.services import realtime_stt as _realtime_stt

    monkeypatch.setattr(_realtime_stt, "OPENAI_API_KEY", "")

    # CALL_MODE is a dev .env toggle too (the realtime engine, 2026-07-27).
    # Left in place it reroutes every pipeline-mode call test into the
    # speech-to-speech session — which waits on vendor events that never come,
    # so half the suite HANGS rather than failing. Reset to the code default;
    # the realtime tests set "realtime" explicitly in their own scope.
    from app.api import call as _call_api

    monkeypatch.setattr(_call_api, "CALL_MODE", "pipeline")
    from app.services import realtime_agent as _realtime_agent

    monkeypatch.setattr(_realtime_agent, "OPENAI_API_KEY", "")

    # CALL_PERSONA is a dev .env toggle too (the natural persona, 2026-07-29).
    # Reset to the code default so the classic-path tests keep proving the
    # classic call is byte-for-byte unchanged; the natural-persona test flips
    # it in its own scope.
    from app.api import call_realtime as _call_realtime

    monkeypatch.setattr(_call_realtime, "CALL_PERSONA", "classic")

    # CALL_TURN_DETECTION likewise (semantic_vad flag, 2026-07-30): reset to
    # the code default so the session-shape test pins server_vad regardless of
    # what the dev .env is trialling; the semantic branch is flipped in-test.
    monkeypatch.setattr(_realtime_agent, "CALL_TURN_DETECTION", "server_vad")

    # CALL_TRANSPORT pinned to the code default so the protocol tests see the
    # rtc invitation regardless of what the dev .env is trialling.
    monkeypatch.setattr(_call_realtime, "CALL_TRANSPORT", "webrtc")

    # Dial-until-ready (2026-08-03): zero the offer wait so every realtime
    # test greets INLINE, exactly as before the deferral existed — otherwise
    # each TestClient call would ring for the full offer window and the
    # pre-greet mic gate would swallow the scripts' opening audio frames.
    # The dial-until-ready tests set their own waits in scope.
    monkeypatch.setattr(_call_realtime, "_RTC_OFFER_WAIT_S", 0.0)

    # The Silero speech gate is OFF in the suite (2026-07-30): the gateway
    # tests drive scripted sessions with synthetic PCM that a real speech
    # detector would rightly drop, starving every script keyed on the Nth
    # send_audio call. The gate has its own tests (test_speech_gate.py, real
    # inference against local fixtures) and one gateway test flips this on.
    monkeypatch.setattr(_call_realtime, "CALL_SPEECH_GATE", False)

    monkeypatch.setattr(_config, "LANGSMITH_TRACING", False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")  # the legacy spelling
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

    from langsmith import utils as ls_utils

    ls_utils.get_env_var.cache_clear()


@pytest.fixture()
def db() -> Session:
    # In-memory, per test. StaticPool + check_same_thread=False so the SAME
    # in-memory database is visible from another thread: FastAPI's TestClient
    # runs routes on a worker thread, and sqlite's default per-thread pooling
    # would hand that thread a brand-new EMPTY database instead. Mirrors the
    # connect_args app/db.py already applies to sqlite.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    for r in _load("showrooms.json"):
        session.add(
            Showroom(
                id=r["id"],
                brand=r["brand"],
                name=r["name"],
                emirate=r["emirate"],
                city=r["city"],
                address=r["address"],
                map_link=r["map_link"],
                phone=r["phone"],
                hours=r["hours"],
                hours_by_day=r["hours_by_day"],
            )
        )
    for r in _load("vehicle_models.json"):
        session.add(
            VehicleModel(
                id=r["id"],
                model_code=r["model_code"],
                brand=r["brand"],
                name=r["name"],
                body_type=r["body_type"],
                seats_min=r["seats_min"],
                seats_max=r["seats_max"],
                price_aed_min=r["price_aed_min"],
                price_aed_max=r["price_aed_max"],
                fuel_type=FuelType(r["fuel_type"]),
                headline_features=r["headline_features"],
                # Carried like every other field: without it the column would
                # silently default to [] in tests, and a test asserting on the
                # Arabic dialog would pass against empty data.
                headline_features_ar=r["headline_features_ar"],
                showroom_ids=r["showroom_ids"],
                data_provenance=r["data_provenance"],
                images=r["images"],
            )
        )
    session.commit()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
# The window starts TOMORROW, never today: query_available excludes past slots
# (yesterday's inventory is not availability), so a window starting today would
# make the fixture's contents depend on the wall-clock hour the suite runs at —
# green in the morning, flaky by evening.
SLOTS_START = date.today() + timedelta(days=1)

# prebooked=0.0: every slot is open. The deterministic pre-booking that makes
# "that time's taken" (S-05) happen naturally in the demo would here just make
# "which slot am I getting?" depend on a seeded RNG.
@pytest.fixture()
def slots(db) -> Session:
    """Generate a 7-day, fully-open inventory window through the real service."""
    slots_service.generate(db, days=7, prebooked=0.0, start_day=SLOTS_START)
    return db


@pytest.fixture()
def open_slot(slots) -> Slot:
    """The earliest open SHOWROOM slot."""
    return slots.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.showroom)
        .order_by(Slot.start, Slot.id)
    ).first()


@pytest.fixture()
def doorstep_slot(slots) -> Slot:
    """The earliest open DOORSTEP slot (FR-SL-07)."""
    return slots.scalars(
        select(Slot)
        .where(Slot.status == SlotStatus.open, Slot.location_type == LocationType.doorstep)
        .order_by(Slot.start, Slot.id)
    ).first()
