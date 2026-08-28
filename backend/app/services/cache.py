"""Redis cache for the retrieval index — and nothing else (T1.3).

SCOPE IS THE POINT. This holds the vector index and its chunks. It does NOT hold
cars, variants, stock or showrooms: nine models, fifteen variants, 66 stock rows
and five showrooms are served from process memory faster than a round trip to
Redis would take, and caching STOCK is the one staleness that reaches a customer
as a promise we cannot keep. Slots and bookings never, as before.

FAIL OPEN, EXACTLY LIKE services/salesforce.py. Every function returns None or
False when Redis is absent, unreachable or slow. A dead cache costs the CACHE —
the index is rebuilt in process, ~14 seconds once — never the turn. There is no
configuration in which a Redis problem produces a customer-visible error.

WHY THE CLIENT IS INJECTABLE, AND WHY THAT MATTERS MORE HERE THAN USUAL.
tests/conftest.py fails any test that opens a non-loopback socket, on purpose —
so a real Redis can never be reached from the suite, and a naive implementation
would ship its entire serialisation path having never executed. `set_client()`
lets a test drive a stub with the same get/set/ping surface, which exercises
every line except the socket itself. That was a stated risk when Redis was
chosen over a disk cache; this is the mitigation.

KEYS CARRY THE CONTENT HASH, so nothing is ever invalidated by hand. A changed
brochure produces a new key and the old one expires on its TTL — see
retrieval/index.py. "Rebuilt on seed change" fails silently the first time
somebody edits a file and forgets; a key that no longer exists cannot.
"""

from __future__ import annotations

import logging

from app.config import REDIS_URL

log = logging.getLogger("app.services.cache")

# Prefix everything, so this database can be shared without collision.
PREFIX = "elite:retrieval:"

# A month. The key already carries the content hash, so an entry is either
# current or unreachable — the TTL exists only to stop superseded indexes
# accumulating forever, not to expire live data.
DEFAULT_TTL_SECONDS = 30 * 24 * 3600

_state: dict = {}


def set_client(client) -> None:
    """Inject a client (or None to reset). Tests only — see the module docstring."""
    _state["client"] = client
    _state.pop("failed", None)


def _client():
    """The Redis client, or None. Caches the failure as well as the success so a
    missing server costs one connection attempt per process, not one per call."""
    if "client" in _state:
        return _state["client"]
    if _state.get("failed"):
        return None
    if not REDIS_URL:
        _state["failed"] = True
        return None
    try:
        import redis

        client = redis.Redis.from_url(
            REDIS_URL,
            # Short and explicit: this is a cache. Waiting five seconds for it
            # would cost more than the ~14s rebuild it is meant to avoid.
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
        client.ping()
        _state["client"] = client
        log.info("retrieval cache connected")
        return client
    except Exception:  # noqa: BLE001 — a dead cache is not an error
        log.warning("retrieval cache unavailable — the index will be built in process")
        _state["failed"] = True
        return None


def enabled() -> bool:
    """Whether the cache is usable. Callers do not need this — every function
    below already degrades — but it makes intent readable at call sites."""
    return _client() is not None


def get(key: str) -> bytes | None:
    client = _client()
    if client is None:
        return None
    try:
        return client.get(PREFIX + key)
    except Exception:  # noqa: BLE001
        log.warning("cache read failed for %s", key, exc_info=True)
        return None


def set(key: str, value: bytes, ttl: int = DEFAULT_TTL_SECONDS) -> bool:
    """True if it was stored. A False here is not worth acting on — it means the
    next boot rebuilds, which is the un-cached behaviour and is fine."""
    client = _client()
    if client is None:
        return False
    try:
        client.set(PREFIX + key, value, ex=ttl)
        return True
    except Exception:  # noqa: BLE001
        log.warning("cache write failed for %s", key, exc_info=True)
        return False
